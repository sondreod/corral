import pytest

from corral.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def app_dir(tmp_path, name="shop", procfile="web: uv run app.py\nworker: ./work.sh --fast\n", env=None):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "Procfile").write_text(procfile)
    if env is not None:
        (folder / ".env").write_text(env)
    return folder


def test_full_config(tmp_path):
    app_dir(tmp_path, env="SHARED=from-dotenv\nPORT=1\n")
    other = app_dir(tmp_path, "other", procfile="report: python report.py\n")
    (other / "Procs").write_text("nightly: ./nightly\n")
    cfg = load_config(
        write(
            tmp_path,
            """
            [server]
            port = 9000
            [runner]
            data_dir = "data"
            path = ["/opt/uv/bin", "bin"]
            sample_interval = "30s"
            history_days = 14
            [apps.shop]
            dir = "shop"
            env = { PORT = "2", APP = "shop" }
            restart_delay = 1
            [apps.shop.processes.worker]
            env = { PORT = "3" }
            enabled = false
            stop_timeout = 30
            [apps.other]
            dir = "other"
            procfile = "Procs"
            [apps.other.processes.nightly]
            cron = "0 3 * * *"
            """,
        )
    )
    assert (cfg.host, cfg.port) == ("127.0.0.1", 9000)
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.path == ("/opt/uv/bin", str(tmp_path / "bin"))
    assert (cfg.sample_interval, cfg.history_days) == (30, 14)
    assert [a.name for a in cfg.apps] == ["shop", "other"]
    assert cfg.apps[1].procfile == tmp_path / "other" / "Procs"

    web, worker, nightly = cfg.scripts
    assert (web.name, web.app, web.process, web.command) == ("shop.web", "shop", "web", "uv run app.py")
    assert web.cwd == tmp_path / "shop" and web.path == cfg.path
    assert web.env == {"SHARED": "from-dotenv", "PORT": "2", "APP": "shop"}  # app env wins over .env
    assert (web.mode, web.enabled, web.restart_delay, web.stop_timeout) == ("service", True, 1.0, 10.0)
    assert worker.command == "./work.sh --fast"
    assert worker.env["PORT"] == "3" and not worker.enabled and worker.stop_timeout == 30
    assert worker.restart_delay == 1.0  # from the app
    assert nightly.name == "other.nightly" and nightly.mode == "schedule"
    assert nightly.cron.expression == "0 3 * * *" and nightly.cwd == tmp_path / "other"


def test_schedule_every(tmp_path):
    app_dir(tmp_path, procfile="report: ./report\n")
    cfg = load_config(write(tmp_path, '[apps.shop]\ndir = "shop"\n[apps.shop.processes.report]\nevery = "5m"\n'))
    (report,) = cfg.scripts
    assert report.every == 300 and report.schedule_text == "every 300s"


def test_empty_config(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.apps == () and cfg.scripts == ()


def test_procfile_problems(tmp_path):
    with pytest.raises(ConfigError, match=r"\[apps.shop\]: cannot read .*Procfile"):
        load_config(write(tmp_path, '[apps.shop]\ndir = "shop"\n'))
    app_dir(tmp_path, procfile="web uv run app.py\n")
    with pytest.raises(ConfigError, match="Procfile line 1: expected 'name: command'"):
        load_config(write(tmp_path, '[apps.shop]\ndir = "shop"\n'))
    app_dir(tmp_path, "two", procfile="web: a\n")
    with pytest.raises(ConfigError, match="has no process named 'wbe' \\(it has web\\)"):
        load_config(write(tmp_path, '[apps.two]\ndir = "two"\n[apps.two.processes.wbe]\nenabled = false\n'))
    app_dir(tmp_path, "three", procfile="web: a\n", env="not valid\n")
    with pytest.raises(ConfigError, match=r"\.env line 1: expected KEY=value"):
        load_config(write(tmp_path, '[apps.three]\ndir = "three"\n'))


@pytest.mark.parametrize(
    "text, message",
    [
        ("[apps.shop]\n", "'dir' .* is required"),
        ('[apps.shop]\ndir = "shop"\ntypo = 1', r"unknown key\(s\) in \[apps.shop\]: typo"),
        ('[apps.shop]\ndir = "shop"\n[apps.shop.processes.web]\ntypo = 1', r"\[processes.web\]: unknown key"),
        ('[apps.shop]\ndir = "shop"\n[apps.shop.processes.web]\nevery = "1m"\ncron = "* * * * *"', "not both"),
        ('[apps.shop]\ndir = "shop"\n[apps.shop.processes.web]\ncron = "nope"', "5 fields"),
        ('[apps.shop]\ndir = "shop"\n[apps.shop.processes.web]\nevery = inf', "finite"),
        ('[apps.shop]\ndir = "shop"\nprocesses = 3', "'processes' must be a table"),
        ('[apps.shop]\ndir = "shop"\nenv = { A = 1 }', "'env' values must be strings"),
        ('[apps."a.b"]\ndir = "shop"', "name may only contain"),
        ('[apps.shop]\ndir = "shop"\nrestart_delay = -5', "'restart_delay' must be positive"),
        ('[apps.shop]\ndir = "shop"\nrestart_delay = 0', "'restart_delay' must be positive"),
        ('[apps.shop]\ndir = "shop"\nstop_timeout = -1', "'stop_timeout' must be positive"),
        ('[apps.shop]\ndir = "shop"\nrestart_delay = 30\nmax_restart_delay = 10', "'max_restart_delay' must not"),
        ('[apps.shop]\ndir = "shop"\nrestart_delay = nan', "finite"),
        ('[scripts.a]\npath = "a.py"', r"\[scripts.\*\] is no longer supported"),
        ('[runner]\nuv = "uv"', "'uv' is no longer used"),
        ('[runner]\npath = "/bin"', "'path' must be"),
        ('[runner]\npath = [""]', "'path' must be a list of directories"),
        ('[server]\nport = "80"', "'port' must be"),
        ('[runner]\nsample_interval = "soon"', "'sample_interval'"),
        ("[runner]\nhistory_days = 0", "'history_days' must be positive"),
        ("[oops]\n", "unknown key"),
        ("server = 5", "'server' must be a table"),
        ("runner = []", "'runner' must be a table"),
        ("apps = 3", r"'apps' must be a table, written \[apps.<name>\]"),
        ("[server]\nport = 99999", "'port' must be between"),
        ("[server]\nport = 0", "'port' must be between"),
        ('[server]\nhost = ""', "'host' must not be empty"),
        ('[server]\nallowed_hosts = "example.com"', "'allowed_hosts' must be"),
        ('[server]\nallowed_hosts = [""]', "'allowed_hosts' must be"),
        ("[runner]\nhistory_days = inf", "'history_days' must be a finite number"),
        ("[runner]\nsample_interval = nan", "'sample_interval'.*finite"),
        ('[metrics]\nscrape_interval = "0.5s"', "at least one second"),
        ('[metrics]\nscrape_interval = "1h"\nretention = "10m"', "'retention' must not be shorter"),
        ("not toml", "config.toml"),
    ],
)
def test_invalid_config(tmp_path, text, message):
    app_dir(tmp_path, procfile="web: ./web\n")
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.toml")
