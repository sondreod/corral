# corral

Runs Python scripts that declare their dependencies inline
([PEP 723](https://peps.python.org/pep-0723/)) under supervision, captures
their stdout and stderr, and serves a web interface: a dashboard with the
status and resource usage of every script, and per script a filterable log
view and monitoring charts.

It can also run entry points of whole Python projects (directories with a
`pyproject.toml`).

Scripts are started with `uv run`, so [uv](https://docs.astral.sh/uv/)
must be installed on the machine. corral itself uses only the Python
standard library (Python 3.11 or newer).

## Configuration

One TOML file; [packaging/config.toml](packaging/config.toml) documents every
option. In short:

```toml
[server]
host = "127.0.0.1"
port = 8723

[scripts.ticker]            # long-running: restarted whenever it exits
path = "scripts/ticker.py"

[scripts.report]            # runs to completion on a schedule
path = "scripts/report.py"
mode = "schedule"
every = "15m"               # or: cron = "30 6 * * 1-5"
```

### Project entry points

Instead of `path`, a table can name a project directory and one of its entry
points:

```toml
[scripts.api]
project = "/srv/myapp"            # directory containing pyproject.toml
entrypoint = "myapp-serve"        # a script name from [project.scripts]
args = ["--port", "8000"]

[scripts.worker]
project = "/srv/myapp"
entrypoint = "myapp.jobs:main"    # module:function, as written in pyproject.toml
```

`entrypoint` is one of:

- a **script name** declared in the project's `[project.scripts]` (or
  `[project.gui-scripts]`). An undeclared name is refused with the list of
  declared ones, unless the project marks its scripts as `dynamic`.
- an **object reference**, `module:function` (or `module:Class.method`), in the
  syntax pyproject.toml uses on the right-hand side of a script. The function
  is called like a console script would call it, so anything importable in
  the project's environment can be run without declaring a script for it.
  The colon is what tells the two forms apart.
- **omitted**, which runs the project's script if it declares exactly one.

The entry point runs as `uv run --project <dir> ...`: uv keeps the environment
in `<dir>/.venv` and creates or updates `<dir>/uv.lock`, so the user
corral runs as needs write access to the project directory. The project
must be installable (have a `[build-system]`) for its scripts and modules to
be available. `pyproject.toml` is read at every start, so problems show up in
the script's log (`runner` stream) and are retried like a failed start.

### Behaviour

- **service** scripts are restarted after `restart_delay` seconds. Each exit
  within 10 seconds of starting doubles the delay, up to `max_restart_delay`.
- **schedule** scripts never overlap themselves. With `every`, the first run
  happens at startup and later runs are timed from the start of the previous
  one. `cron` takes the usual five fields, in local time.
- Stopping a script sends SIGTERM to its whole process group, then SIGKILL
  after `stop_timeout` seconds.
- Stop/start/disable from the web page lasts until corral restarts; the
  `enabled` option decides the state at startup.

The web interface has no authentication. It listens on localhost by default;
put it behind a reverse proxy or an SSH tunnel to reach it from elsewhere.
While it listens on localhost it only answers requests addressed to a
localhost name or loopback address, so a web page cannot reach it by pointing
a domain of its own at 127.0.0.1. A reverse proxy that passes on its own host
name needs that name in `allowed_hosts` under `[server]` (`["*"]` turns the
check off).

## Logs

Lines are stored in SQLite (`logs.sqlite3` in `data_dir`) with a timestamp and
their stream: `stdout`, `stderr`, or `runner` for corral's own notes
(started, exited with code N, ...). The newest `max_log_lines` per script are
kept. The web page filters by substring (case-insensitive) and by stream.

## Monitoring

The start page shows one card per script with its state, current CPU and
memory use, runs and log lines in the last hour, and CPU/memory trend lines.
A script's **Monitoring** tab charts, over the last 15 minutes to 7 days:

- **CPU** (percent of one core) and **memory** (resident set size), summed
  over the script's whole process tree: uv, the Python process and anything
  they start
- **threads** and **open files** (file descriptors) of that tree
- **log output**, in lines per minute on stdout and stderr
- the **recent runs**, with start time, duration and result

Measurements are read from `/proc` every `sample_interval` (default 10
seconds) and stored in the same SQLite file as the logs; they and the run
history are dropped after `history_days` (default 7). Longer chart ranges
average the samples into coarser steps. A gap in a chart means the script was
not running. Memory is a sum of per-process figures, so pages shared between
processes are counted more than once. On systems without `/proc` the resource
charts stay empty; runs and log output are still recorded.

## Prometheus metrics

`GET /metrics` serves the runner's own figures in the Prometheus text format,
one series per script (label `script`):

| Metric | Type | Meaning |
| --- | --- | --- |
| `corral_script_state{state=…}` | gauge | 1 for the current state (`stopped`, `starting`, `running`, `stopping`, `backoff`, `waiting`), 0 for the others |
| `corral_script_up` | gauge | 1 while the script has a process |
| `corral_script_enabled` | gauge | 0 after Stop/Disable |
| `corral_script_runs_total` | counter | starts |
| `corral_script_run_failures_total` | counter | runs that could not start or exited non-zero (requested stops excluded) |
| `corral_script_log_lines_total{stream=…}` | counter | lines printed on `stdout` / `stderr` |
| `corral_script_cpu_seconds_total` | counter | CPU time of the process tree |
| `corral_script_resident_memory_bytes`, `…_threads`, `…_open_fds` | gauge | as on the dashboard; absent while not running |
| `corral_script_last_exit_code` | gauge | absent until a run has finished |
| `corral_script_last_start_time_seconds`, `…_last_exit_time_seconds`, `…_next_run_time_seconds` | gauge | Unix times |
| `corral_build_info{version=…}` | gauge | always 1 |

Counters start at zero when the runner starts. CPU, memory, threads and open
files come from the most recent sample, so they lag by up to
`sample_interval`; CPU used by a process that starts and exits between two
samples is not seen. A scrape job:

```yaml
scrape_configs:
  - job_name: corral
    static_configs:
      - targets: ["127.0.0.1:8723"]
```

For example, `rate(corral_script_cpu_seconds_total[1m])` is CPU use in
cores, and `increase(corral_script_run_failures_total[1h]) > 0` finds
scripts that failed in the last hour.

### Metrics from your scripts

Scripts can report their own metrics, which appear on the same `/metrics`
page with a `script` label added. In Python, use `corral.metrics`, a
module of this package made for the scripts; it needs only the standard
library and works like `prometheus_client`:

```python
# /// script
# dependencies = ["corral"]
# ///
from corral.metrics import Counter, Gauge, Histogram

JOBS = Counter("jobs_processed", "Jobs processed", ["queue"])
DEPTH = Gauge("queue_depth", "Jobs waiting")
DURATION = Histogram("job_duration_seconds", "Time spent per job")

JOBS.labels(queue="mail").inc()
DEPTH.set(12)
with DURATION.time():
    ...
```

The script gets the module by depending on the `corral` package like
on any other. The package is not on an index, so the header also says where
it is: the path of a checkout of this repository. Do not leave that out:
without the path uv looks for `corral` on PyPI, where this project is not
published, so it would fail or, if someone else ever publishes a package by
that name, install theirs. A relative path is taken
from the script's own directory, as in
[`examples/batch.py`](examples/batch.py):

```python
# /// script
# dependencies = ["corral"]
#
# [tool.uv.sources]
# corral = { path = "/path/to/corral" }
# ///
```

The runner's user must be able to read that directory. uv builds the package
from it once and caches the result. It rebuilds when the checkout's
`pyproject.toml` changes (a new version, say), not on every source edit;
`uv cache clean corral` forces it.

A project does the same in its `pyproject.toml`. The script's copy is
independent of the runner that starts it (the Debian package does not provide
it), and as the package needs Python 3.11, so does the script.

There are `Counter` (`inc`, `count_exceptions`), `Gauge` (`set`, `inc`,
`dec`, `set_to_current_time`, `time`), `Histogram` and `Summary` (`observe`,
`time`), each with `.labels(...)`. What differs from `prometheus_client`:

- **The runner holds the values, not the script.** A counter keeps counting
  across runs of a scheduled script, and a gauge keeps its last value after
  the script exits. Everything starts again when the runner restarts.
- A counter is exposed with `_total` added to its name if it is missing.
- A `Summary` has `_count` and `_sum` only, no quantiles.
- Run by hand, outside corral, the calls do nothing (set
  `CORRAL_METRICS=1` to see what would be sent).

Under the hood each call prints one line on standard output, `@metric`
followed by a JSON object, so any program can report metrics without the
library:

```
@metric {"name": "jobs_processed", "op": "declare", "type": "counter", "help": "Jobs processed"}
@metric {"name": "jobs_processed", "op": "inc", "value": 1, "labels": {"queue": "mail"}}
@metric {"name": "queue_depth", "op": "set", "value": 12}
@metric {"name": "job_duration_seconds", "op": "observe", "value": 0.31}
```

`op` is `declare` (with `type`: `counter`, `gauge`, `histogram` or
`summary`; optional `help`, and `buckets` for a histogram), `inc` (counter
or gauge; `value` defaults to 1), `set` (gauge) or `observe` (histogram or
summary). A metric that was not declared gets its type from the first
operation: `inc` makes a counter, `set` a gauge, `observe` a histogram.

Metric lines are not stored as log lines and do not count towards log
volume. A line the runner cannot use is reported in the script's log (the
first ten per run). A line only counts if `@metric` is at its very start, so
finish any partial line before reporting. Names starting with
`corral_` and the label `script` are reserved, and a script is limited
to 2000 series. If two scripts use the same name for different types, the
second one is left out and noted in a comment at the end of `/metrics`.

## Development

```sh
make test     # uv run pytest
make run      # uv run corral --config examples/dev-config.toml -> http://127.0.0.1:8724/
make deb      # build the Debian package into dist/
make help     # the other targets
```

## Debian package

```sh
make deb                          # needs uv and dpkg-deb
sudo apt install ./dist/corral_0.1.0_all.deb
```

The package installs a systemd service, `corral.service`, enabled and
started on installation and restarted if it ever exits. It runs as the
`corral` system user.

| Path | Purpose |
| --- | --- |
| `/etc/corral/config.toml` | configuration (kept on upgrade) |
| `/etc/corral/scripts/` | suggested place for your scripts |
| `/var/lib/corral/` | log database |
| `/var/cache/corral/` | uv's cache of script environments |
| `/usr/share/doc/corral/examples/` | example scripts |

After editing the configuration: `corral --check`, then
`sudo systemctl restart corral`. The service's own messages are in
`journalctl -u corral`.

uv is not packaged in Debian, so it is not a package dependency. Install it
somewhere on the system PATH (for example `/usr/local/bin/uv`) or set `uv` in
the `[runner]` section to its full path. The `corral` user needs network
access the first time a script runs, to fetch its dependencies.
