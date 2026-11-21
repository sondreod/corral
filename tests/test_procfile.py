import pytest

from corral.procfile import ProcfileError, parse_env, parse_procfile, read_env


def test_procfile():
    text = """
# comment
web:   uv run --script app.py --port 8000
worker:bundle exec sidekiq -q a:b
release-1 : ./migrate && echo "done: ok"
"""
    assert parse_procfile(text) == {
        "web": "uv run --script app.py --port 8000",
        "worker": "bundle exec sidekiq -q a:b",
        "release-1": './migrate && echo "done: ok"',
    }


@pytest.mark.parametrize(
    "text, message",
    [
        ("web uv run x\n", "line 1: expected 'name: command'"),
        ("we.b: x\n", "line 1: expected"),
        ("\nweb:\n", "line 2: process 'web' has no command"),
        ("web: a\nweb: b\n", "line 2: process 'web' is defined twice"),
        ("# only a comment\n", "defines no processes"),
    ],
)
def test_procfile_errors(text, message):
    with pytest.raises(ProcfileError, match=message):
        parse_procfile(text)


def test_env():
    text = r"""
# comment
PLAIN=value
export EXPORTED=yes
SPACED = some value  # trailing comment
HASH=a#b
EMPTY=
DOUBLE="line one\nline \"two\""  # comment
SINGLE='no \n escapes $HERE'
URL=postgres://u:p@host/db?x=1
"""
    assert parse_env(text) == {
        "PLAIN": "value",
        "EXPORTED": "yes",
        "SPACED": "some value",
        "HASH": "a#b",
        "EMPTY": "",
        "DOUBLE": 'line one\nline "two"',
        "SINGLE": "no \\n escapes $HERE",
        "URL": "postgres://u:p@host/db?x=1",
    }


@pytest.mark.parametrize(
    "text, message",
    [
        ("NOEQUALS\n", "line 1: expected KEY=value"),
        ("1BAD=x\n", "expected KEY=value"),
        ('A="open\n', "missing closing double quote"),
        ("A='open\n", "missing closing single quote"),
        ('A="x" y\n', "unexpected text after the closing quote"),
    ],
)
def test_env_errors(text, message):
    with pytest.raises(ProcfileError, match=message):
        parse_env(text)


def test_missing_env_file_is_empty(tmp_path):
    assert read_env(tmp_path / ".env") == {}
