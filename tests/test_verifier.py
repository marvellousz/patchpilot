"""Gate tests. These run pytest inside the Docker sandbox, so Docker must be available."""

import pytest

from patchpilot.tools.sandbox import ensure_image
from patchpilot.tools.workspace import Workspace
from patchpilot.verifier import Verifier

SOURCE = "def half(x):\n    return x // 2\n"
REPRO = "from calc import half\n\ndef test_half_of_odd():\n    assert half(3) == 1.5\n"


@pytest.fixture(scope="module", autouse=True)
def sandbox_image():
    ensure_image()


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / "src"
    (src / "tests").mkdir(parents=True)
    (src / "calc.py").write_text(SOURCE)
    (src / "tests" / "test_calc.py").write_text("from calc import half\n\ndef test_even():\n    assert half(4) == 2\n")
    (src / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    return Workspace.copy_local(src, tmp_path / "ws")


def test_reproduction_must_not_touch_source(ws):
    ws.write_file("tests/test_repro.py", REPRO)
    ws.replace_in_file("calc.py", "x // 2", "x / 2")
    assert not Verifier(ws).check_reproduction("tests/test_repro.py").ok


def test_reproduction_must_fail(ws):
    ws.write_file("tests/test_repro.py", "def test_ok():\n    assert True\n")
    assert not Verifier(ws).check_reproduction("tests/test_repro.py").ok


def test_full_repair_passes_gates(ws):
    v = Verifier(ws)
    ws.write_file("tests/test_repro.py", REPRO)
    assert v.check_reproduction("tests/test_repro.py").ok
    ws.replace_in_file("calc.py", "x // 2", "x / 2")
    assert v.check_fix().ok


def test_fix_rejected_if_repro_test_edited(ws):
    v = Verifier(ws)
    ws.write_file("tests/test_repro.py", REPRO)
    assert v.check_reproduction("tests/test_repro.py").ok
    ws.write_file("tests/test_repro.py", "def test_half_of_odd():\n    pass\n")
    ws.replace_in_file("calc.py", "x // 2", "x / 2")
    result = v.check_fix()
    assert not result.ok and "was changed" in result.message


def test_fix_rejected_if_existing_test_modified(ws):
    v = Verifier(ws)
    ws.write_file("tests/test_repro.py", REPRO)
    assert v.check_reproduction("tests/test_repro.py").ok
    ws.replace_in_file("calc.py", "x // 2", "x / 2")
    ws.replace_in_file("tests/test_calc.py", "half(4) == 2", "half(4) == 2.0")
    result = v.check_fix()
    assert not result.ok and "existing tests" in result.message
