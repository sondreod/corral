import subprocess
import sys

import pytest

from scriptrunner.project import ProjectError, declared_scripts, project_command


def make_project(tmp_path, pyproject):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "pyproject.toml").write_text(pyproject)
    return project


TWO_SCRIPTS = """
[project]
name = "demo"
version = "1"
[project.scripts]
demo-serve = "demo.cli:serve"
demo-work = "demo.cli:work"
"""


def test_named_script(tmp_path):
    project = make_project(tmp_path, TWO_SCRIPTS)
    assert project_command(project, "demo-work") == ["demo-work"]


def test_unknown_script_lists_choices(tmp_path):
    project = make_project(tmp_path, TWO_SCRIPTS)
    with pytest.raises(ProjectError, match=r"no script named 'nope'.*demo-serve, demo-work"):
        project_command(project, "nope")


def test_omitted_entrypoint_needs_exactly_one_script(tmp_path):
    with pytest.raises(ProjectError, match="exactly one.*demo-serve, demo-work"):
        project_command(make_project(tmp_path, TWO_SCRIPTS), None)
    with pytest.raises(ProjectError, match="exactly one.*declared: none"):
        project_command(make_project(tmp_path, '[project]\nname = "demo"\n'), None)

    single = make_project(tmp_path, '[project]\nname = "demo"\n[project.scripts]\nonly = "demo:main"\n')
    assert project_command(single, None) == ["only"]


def test_gui_scripts_count(tmp_path):
    project = make_project(tmp_path, '[project]\nname = "demo"\n[project.gui-scripts]\ngui = "demo:main"\n')
    assert project_command(project, None) == ["gui"]
    assert declared_scripts(project) == ({"gui": "demo:main"}, False)


def test_dynamic_scripts_are_not_second_guessed(tmp_path):
    project = make_project(tmp_path, '[project]\nname = "demo"\ndynamic = ["scripts"]\n')
    assert project_command(project, "from-setup-py") == ["from-setup-py"]


@pytest.mark.parametrize("pyproject", [None, "not toml [", "[tool.x]\n"])
def test_broken_project(tmp_path, pyproject):
    project = tmp_path / "proj"
    project.mkdir()
    if pyproject is not None:
        (project / "pyproject.toml").write_text(pyproject)
    with pytest.raises(ProjectError, match="pyproject.toml"):
        project_command(project, "x")
    if pyproject is None:
        with pytest.raises(ProjectError, match="does not exist"):
            project_command(project, "pkg:main")


def test_reference_runs_like_a_console_script(tmp_path):
    project = make_project(tmp_path, '[project]\nname = "demo"\n')
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "__init__.py").write_text("")
    (tmp_path / "demo" / "jobs.py").write_text(
        "import sys\n"
        "class Worker:\n"
        "    @staticmethod\n"
        "    def main():\n"
        "        print(sys.argv)\n"
        "        return 7\n"
    )
    command = project_command(project, "demo.jobs:Worker.main")
    assert command[0] == "python"
    result = subprocess.run(
        [sys.executable, *command[1:], "--flag", "value"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.stdout.strip() == "['demo.jobs:Worker.main', '--flag', 'value']"
    assert result.returncode == 7
