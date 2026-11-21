# corral

Runs the processes listed in Procfiles under supervision, captures their
stdout and stderr, and serves a web interface: a dashboard with the status
and resource usage of every process, per process a filterable log view and
monitoring charts, and dashboards you build yourself from stored metrics.

A Procfile line can run anything; Python scripts that declare their
dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)) run well
with `uv run --script`, and processes can report their own metrics. corral
itself uses only the Python standard library (Python 3.11 or newer).

## Configuration

One TOML file; [packaging/config.toml](packaging/config.toml) documents every
option. Each app is a folder with a `Procfile`:

```
# /srv/shop/Procfile
web: uv run --script app.py --port 8000
worker: uv run --project . shop-worker
report: ./report.sh --daily
```

```toml
[server]
host = "127.0.0.1"
port = 8723

[apps.shop]
dir = "/srv/shop"              # the folder with the Procfile
env = { LOG_LEVEL = "info" }   # optional, for every process

[apps.shop.processes.report]   # optional settings for one process
cron = "30 6 * * 1-5"          # or: every = "15m"
```

The processes are named `<app>.<process>`: `shop.web`, `shop.worker`,
`shop.report`.

- Each command runs with `/bin/sh -c` in the app's folder, so pipes,
  `&&` and `$VARIABLES` work. Blank lines and `#` comments are skipped.
- `KEY=value` lines in the folder's `.env` (if there is one) are added to
  the environment of every process, as Foreman and Honcho do. `env` in the
  config wins over `.env`, and a process's own `env` over the app's.
- `path` under `[runner]` puts directories in front of `PATH`, for programs
  such as uv that are not in the service's `PATH`.
- `restart_delay`, `max_restart_delay`, `stop_timeout` and `enabled` can be
  set for an app and overridden per process.
- A process mentioned under `processes` that is not in the Procfile is an
  error, so typos do not go unnoticed.

### Reloading

`systemctl reload corral` (SIGHUP) or the **Reload** button in the web
interface re-reads the config file, the Procfiles and the `.env` files:
new processes start, removed ones stop, and a running service whose command
or environment changed is restarted. Scheduled processes pick up changes at
their next run; everything else keeps running. If anything is wrong (a
Procfile line it cannot read, say) the reload is refused with the reason and
nothing changes. `[server]`, `[runner]` and `[metrics]` settings only apply
after a restart, which the reload reports.

### Behaviour

- Processes are long-running **services** unless they have `every` or
  `cron`. A service is restarted after `restart_delay` seconds. Each exit
  within 10 seconds of starting doubles the delay, up to `max_restart_delay`.
- **Scheduled** processes never overlap themselves. With `every`, the first
  run happens at startup and later runs are timed from the start of the
  previous one. `cron` takes the usual five fields, in local time.
- Stopping a process sends SIGTERM to its whole process group (the shell
  and everything it started), then SIGKILL after `stop_timeout` seconds.
  When the shell exits, whatever it left running gets the same treatment.
- Stop/start/disable from the web page lasts until corral restarts; the
  `enabled` option decides the state at startup (and when it changes in a
  reload).

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
(started, exited with code N, ...). The newest `max_log_lines` per process are
kept. The web page filters by substring (case-insensitive) and by stream.

## Monitoring

The start page shows the processes grouped by app, one card each with its
state, current CPU and memory use, runs and log lines in the last hour, and
CPU/memory trend lines. A process's **Monitoring** tab charts, over the last
15 minutes to 7 days:

- **CPU** (percent of one core) and **memory** (resident set size), summed
  over the process's whole tree: the shell, the program and anything they
  start
- **threads** and **open files** (file descriptors) of that tree
- **log output**, in lines per minute on stdout and stderr
- the **recent runs**, with start time, duration and result

Measurements are read from `/proc` every `sample_interval` (default 10
seconds) and stored in the same SQLite file as the logs; they and the run
history are dropped after `history_days` (default 7). Longer chart ranges
average the samples into coarser steps. A gap in a chart means the process was
not running. Memory is a sum of per-process figures, so pages shared between
processes are counted more than once. On systems without `/proc` the resource
charts stay empty; runs and log output are still recorded.

## Prometheus metrics

`GET /metrics` serves the runner's own figures in the Prometheus text format,
one series per process, labelled with its full name (`script="shop.web"`; the
label and metric names date from when corral ran scripts) and its app
(`app="shop"`):

| Metric | Type | Meaning |
| --- | --- | --- |
| `corral_script_state{state=…}` | gauge | 1 for the current state (`stopped`, `starting`, `running`, `stopping`, `backoff`, `waiting`), 0 for the others |
| `corral_script_up` | gauge | 1 while the process is running |
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
processes that failed in the last hour.

### Metrics from your scripts

Scripts can report their own metrics, which appear on the same `/metrics`
page with `script` and `app` labels added. In Python, use `corral.metrics`, a
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
`corral_` and the labels `script` and `app` are reserved, and a script is limited
to 2000 series. If two scripts use the same name for different types, the
second one is left out and noted in a comment at the end of `/metrics`.

## Dashboards

Once a minute corral reads its own `/metrics` page (so the runner's metrics
and every process's own) and stores each value in SQLite, `metrics.sqlite3`
in `data_dir`, for 30 days. The **Dashboards** page shows them as graphs:

- Create a dashboard with **New dashboard**, then **Edit** to add panels,
  drag them by their title bar and resize them from the bottom right corner.
  Changes are saved as you make them, also in SQLite.
- A panel shows one metric as a **line chart**, a **stat** (one number, with
  a small chart when it is one series), a **bar chart** or a **table** (the
  latest value per series, or their average, maximum or minimum), or a
  **heatmap** (how a histogram's observations spread over its buckets).
- Filter on label values, and pick a function: the **value** as stored, a
  **rate** per second or per minute or the **increase** per step (for
  counters; a counter that drops is taken to have restarted from zero), or a
  **quantile** of a histogram. Series can be combined (sum, average,
  maximum, minimum, count), kept separate by the labels you choose.
- The range buttons (15 minutes to 30 days) apply to the whole dashboard.
  Longer ranges add up or take the last value of several scrapes per point.
- **Kiosk** shows only the panels, full screen, for a wall screen; Esc or
  the corner button leaves it. Its link, `#/d/<id>?kiosk`, can be
  bookmarked, and `&theme=nord` (or another theme) picks the theme for that
  screen. A browser only goes full screen after a click, so for a screen
  that opens the link by itself, start the browser in its own kiosk mode
  (e.g. `chromium --kiosk 'http://host:8723/#/d/1?kiosk'`).

The theme menu in the header offers auto (follows the system), light,
dark, Solarized light, Solarized dark and Nord. The choice is remembered in
the browser.

```toml
[metrics]
scrape_interval = "1m"   # how often /metrics is read and stored
retention = "30d"        # how long stored values are kept
```

Each series takes roughly 20 bytes per scrape, so under 1 MB per series for
30 days at the default interval. A histogram is one series per bucket.

The same data is available as JSON: `GET /api/tsdb/metrics` lists the stored
metrics, `GET /api/tsdb/query?q={"metric": ..., "fn": "rate", ...}&range=3600`
returns series, and `/api/dashboards` stores dashboards (`POST`, `PUT` and
`DELETE` need an `X-Corral` header).

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
| `/var/lib/corral/` | log and metrics databases |
| `/var/cache/corral/` | uv's cache, for processes run with uv |
| `/usr/share/doc/corral/examples/` | an example app (Procfile, .env, scripts) |

Apps can live anywhere the `corral` user can read (and, for
`uv run --project`, write). After editing the configuration or a Procfile:
`corral --check`, then `sudo systemctl reload corral`. The service's own
messages are in `journalctl -u corral`.

uv is not packaged in Debian, so it is not a package dependency. Install it
somewhere on the system PATH (for example `/usr/local/bin/uv`) or add its
folder to `path` in the `[runner]` section. The `corral` user needs network
access the first time a uv script runs, to fetch its dependencies.
