import os
import sys
import time

import pytest

from corral.config import Config
from corral.monitor import Monitor, ProcInfo, parse_stat, process_tree
from corral.store import LogStore
from corral.supervisor import Supervisor

from test_supervisor import script as make_script, wait_for

needs_proc = pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs Linux /proc")


def test_parse_stat_with_awkward_command_name():
    text = (
        "4242 (my (odd) name) S 100 4242 4000 0 -1 4194304 500 0 0 0 "
        "30 12 0 0 20 0 3 0 12345 1000000 256 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 1 0 0"
    )
    info = parse_stat(text)
    assert (info.pid, info.ppid, info.session, info.ticks, info.threads) == (4242, 100, 4000, 42, 3)
    assert info.rss == 256 * os.sysconf("SC_PAGE_SIZE")


def test_process_tree_follows_children_and_session():
    def proc(pid, ppid, session):
        return ProcInfo(pid=pid, ppid=ppid, session=session, ticks=1, rss=1, threads=1)

    processes = {
        p.pid: p
        for p in [
            proc(1, 0, 1),
            proc(10, 1, 10),  # the script (session leader)
            proc(11, 10, 10),  # child
            proc(12, 11, 10),  # grandchild
            proc(13, 1, 10),  # orphan re-parented to init, same session
            proc(14, 12, 14),  # descendant that started its own session
            proc(20, 1, 20),  # unrelated
            proc(21, 20, 20),
        ]
    }
    assert sorted(p.pid for p in process_tree(processes, 10)) == [10, 11, 12, 13, 14]
    assert process_tree(processes, 999) == []


@needs_proc
def test_sampling_a_running_script(tmp_path):
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('one'); print('two'); print('oops', file=sys.stderr, flush=True)\n"
        "end = time.time() + 60\n"
        "while time.time() < end: pass\n"  # burn CPU so there is something to measure
    )
    store = LogStore(tmp_path / "db")
    supervisor = Supervisor(Config(scripts=(make_script(tmp_path, "busy", body),)), store)
    monitor = Monitor(supervisor, store, interval=3600, retention_days=7)  # sampled by hand below
    supervisor.start()
    try:
        script = supervisor.scripts["busy"]
        wait_for(lambda: script.status()["state"] == "running")
        wait_for(lambda: (store.flush(), len(store.query("busy", streams=("stdout", "stderr"))))[1] == 3)

        monitor.sample()
        first = monitor.latest("busy")
        assert first["cpu"] is None  # needs two samples
        assert first["rss"] > 1_000_000 and first["threads"] >= 2 and first["fds"] >= 3

        time.sleep(0.5)
        monitor.sample()
        second = monitor.latest("busy")
        assert 20 < second["cpu"] < 400

        store.flush()
        now = time.time()
        (point,) = store.metrics("busy", now - 60, 120)
        assert point["out_lines"] == 2 and point["err_lines"] == 1
        assert point["cpu"] == second["cpu"] and point["rss_max"] >= first["rss"]

        script.stop()
        wait_for(lambda: script.status()["state"] == "stopped")
        monitor.sample()
        assert monitor.latest("busy") is None
        store.flush()
        assert len(store.metrics("busy", now - 60, 0.001)) == 2  # nothing recorded while stopped
    finally:
        supervisor.shutdown()
        store.close()
