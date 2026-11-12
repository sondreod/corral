# scriptrunner

Runs Python scripts that declare their dependencies inline
([PEP 723](https://peps.python.org/pep-0723/)) under supervision, captures
their stdout and stderr, and serves a web interface: a dashboard with the
status and resource usage of every script, and per script a filterable log
view and monitoring charts.

It can also run entry points of whole Python projects (directories with a
`pyproject.toml`).

Scripts are started with `uv run`, so [uv](https://docs.astral.sh/uv/)
must be installed on the machine. scriptrunner itself uses only the Python
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
scriptrunner runs as needs write access to the project directory. The project
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
- Stop/start/disable from the web page lasts until scriptrunner restarts; the
  `enabled` option decides the state at startup.

The web interface has no authentication. It listens on localhost by default;
put it behind a reverse proxy or an SSH tunnel to reach it from elsewhere.

## Logs

Lines are stored in SQLite (`logs.sqlite3` in `data_dir`) with a timestamp and
their stream: `stdout`, `stderr`, or `runner` for scriptrunner's own notes
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

## Development

```sh
make test     # uv run pytest
make run      # uv run scriptrunner --config examples/dev-config.toml -> http://127.0.0.1:8724/
make deb      # build the Debian package into dist/
make help     # the other targets
```

## Debian package

```sh
make deb                          # needs uv and dpkg-deb
sudo apt install ./dist/scriptrunner_0.1.0_all.deb
```

The package installs a systemd service, `scriptrunner.service`, enabled and
started on installation and restarted if it ever exits. It runs as the
`scriptrunner` system user.

| Path | Purpose |
| --- | --- |
| `/etc/scriptrunner/config.toml` | configuration (kept on upgrade) |
| `/etc/scriptrunner/scripts/` | suggested place for your scripts |
| `/var/lib/scriptrunner/` | log database |
| `/var/cache/scriptrunner/` | uv's cache of script environments |
| `/usr/share/doc/scriptrunner/examples/` | example scripts |

After editing the configuration: `scriptrunner --check`, then
`sudo systemctl restart scriptrunner`. The service's own messages are in
`journalctl -u scriptrunner`.

uv is not packaged in Debian, so it is not a package dependency. Install it
somewhere on the system PATH (for example `/usr/local/bin/uv`) or set `uv` in
the `[runner]` section to its full path. The `scriptrunner` user needs network
access the first time a script runs, to fetch its dependencies.
