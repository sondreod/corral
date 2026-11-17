import pytest

from corral.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_full_config(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
            [server]
            port = 9000
            [runner]
            data_dir = "data"
            sample_interval = "30s"
            history_days = 14
            [scripts.svc]
            path = "scripts/a.py"
            args = ["-v"]
            env = { KEY = "value" }
            restart_delay = 1
            [scripts.job]
            path = "/abs/b.py"
            mode = "schedule"
            every = "5m"
            [scripts.nightly]
            path = "c.py"
            mode = "schedule"
            cron = "0 3 * * *"
            enabled = false
            """,
        )
    )
    assert (cfg.host, cfg.port) == ("127.0.0.1", 9000)
    assert cfg.data_dir == tmp_path / "data"
    assert (cfg.sample_interval, cfg.history_days) == (30, 14)
    svc, job, nightly = cfg.scripts
    assert svc.path == tmp_path / "scripts/a.py"
    assert (svc.mode, svc.args, svc.env, svc.restart_delay) == ("service", ("-v",), {"KEY": "value"}, 1.0)
    assert str(job.path) == "/abs/b.py" and job.every == 300
    assert nightly.cron.expression == "0 3 * * *" and not nightly.enabled


def test_project_config(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
            [scripts.api]
            project = "apps/myapp"
            entrypoint = "myapp-serve"
            args = ["--port", "8000"]
            [scripts.worker]
            project = "/srv/myapp"
            entrypoint = "myapp.jobs:Worker.main"
            [scripts.only]
            project = "/srv/single"
            """,
        )
    )
    api, worker, only = cfg.scripts
    assert (api.path, api.project, api.entrypoint) == (None, tmp_path / "apps/myapp", "myapp-serve")
    assert (str(worker.project), worker.entrypoint) == ("/srv/myapp", "myapp.jobs:Worker.main")
    assert only.entrypoint is None


def test_empty_config(tmp_path):
    assert load_config(write(tmp_path, "")).scripts == ()


@pytest.mark.parametrize(
    "text, message",
    [
        ("[scripts.a]\n", "exactly one of 'path'"),
        ('[scripts.a]\npath = "a.py"\nproject = "proj"', "exactly one of 'path'"),
        ('[scripts.a]\npath = "a.py"\nentrypoint = "x"', "only valid together with 'project'"),
        ('[scripts.a]\nproject = "p"\nentrypoint = "a:b:c"', "invalid entrypoint"),
        ('[scripts.a]\nproject = "p"\nentrypoint = "pkg.mod:"', "invalid entrypoint"),
        ('[scripts.a]\nproject = "p"\nentrypoint = "--help"', "invalid entrypoint"),
        ('[scripts.a]\nproject = "p"\nentrypoint = "bin/x"', "invalid entrypoint"),
        ('[scripts.a]\npath = "a.py"\nmode = "x"', "'mode' must be"),
        ('[scripts.a]\npath = "a.py"\nmode = "schedule"', "exactly one of"),
        ('[scripts.a]\npath = "a.py"\nmode = "schedule"\nevery = "1m"\ncron = "* * * * *"', "exactly one of"),
        ('[scripts.a]\npath = "a.py"\nevery = "1m"', "only valid with"),
        ('[scripts.a]\npath = "a.py"\nmode = "schedule"\ncron = "nope"', "5 fields"),
        ('[scripts.a]\npath = "a.py"\ntypo = 1', "unknown key"),
        ('[scripts."a b"]\npath = "a.py"', "name may only contain"),
        ('[server]\nport = "80"', "'port' must be"),
        ('[runner]\nsample_interval = "soon"', "'sample_interval'"),
        ("[runner]\nhistory_days = 0", "'history_days' must be positive"),
        ("[oops]\n", "unknown key"),
        ("server = 5", "'server' must be a table"),
        ("runner = []", "'runner' must be a table"),
        ("scripts = 3", "'scripts' must be a table"),
        ('[[scripts]]\npath = "a.py"', "'scripts' must be a table"),
        ("[server]\nport = 99999", "'port' must be between"),
        ("[server]\nport = 0", "'port' must be between"),
        ('[server]\nhost = ""', "'host' must not be empty"),
        ('[server]\nallowed_hosts = "example.com"', "'allowed_hosts' must be"),
        ('[server]\nallowed_hosts = [""]', "'allowed_hosts' must be"),
        ('[runner]\nuv = ""', "'uv' must not be empty"),
        ("[runner]\nhistory_days = inf", "'history_days' must be a finite number"),
        ("[runner]\nsample_interval = nan", "'sample_interval'.*finite"),
        ('[scripts.a]\npath = "a.py"\nrestart_delay = -5', "'restart_delay' must be positive"),
        ('[scripts.a]\npath = "a.py"\nrestart_delay = 0', "'restart_delay' must be positive"),
        ('[scripts.a]\npath = "a.py"\nstop_timeout = -1', "'stop_timeout' must be positive"),
        ('[scripts.a]\npath = "a.py"\nrestart_delay = 30\nmax_restart_delay = 10', "'max_restart_delay' must not"),
        ('[scripts.a]\npath = "a.py"\nrestart_delay = nan', "finite"),
        ('[scripts.a]\npath = "a.py"\nmode = "schedule"\nevery = inf', "finite"),
        ('[scripts.a]\npath = "a.py"\ncwd = ""', "'cwd' must not be empty"),
        ("not toml", "config.toml"),
    ],
)
def test_invalid_config(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.toml")
