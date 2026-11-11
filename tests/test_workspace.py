import pytest

from patchpilot.tools import ToolBox
from patchpilot.tools.workspace import Workspace, WorkspaceError


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    return Workspace.copy_local(src, tmp_path / "ws")


def test_rejects_paths_outside_repo(ws):
    with pytest.raises(WorkspaceError):
        ws.read_file("../secrets.txt")
    with pytest.raises(WorkspaceError):
        ws.write_file(".git/config", "x")


def test_replace_requires_unique_match(ws):
    with pytest.raises(WorkspaceError, match="found 0"):
        ws.replace_in_file("pkg/mod.py", "return 2", "return 3")
    ws.replace_in_file("pkg/mod.py", "return 1", "return 2")
    assert "return 2" in ws.read_file("pkg/mod.py")


def test_diff_includes_new_files(ws):
    ws.write_file("tests/test_new.py", "def test_x(): pass\n")
    assert "tests/test_new.py" in ws.changed_files()
    assert "+def test_x(): pass" in ws.diff()


def test_toolbox_reports_errors_as_text(ws):
    tools = ToolBox(ws)
    assert tools.call("read_file", '{"path": "nope.py"}').startswith("Error:")
    assert "pkg/mod.py:2" in tools.call("search_code", {"pattern": "return"})
