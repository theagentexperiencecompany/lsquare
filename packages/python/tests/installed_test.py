"""In CI the tests run against the built wheel, never the source tree: `LSQUARE_INSTALLED=1` says
so, and this checks it, for this process and for the children the process tests start."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import lsquare
import lsquare.mcp

SOURCE = Path(__file__).resolve().parent.parent / "src"

pytestmark = pytest.mark.skipif(os.environ.get("LSQUARE_INSTALLED") != "1", reason="only where the wheel is installed (CI)")


def test_lsquare_is_imported_from_the_installed_wheel() -> None:
    for module in (lsquare, lsquare.mcp):
        path = Path(module.__file__ or "").resolve()
        assert "site-packages" in path.parts and SOURCE not in path.parents, path


def test_a_child_process_imports_it_from_there_too() -> None:
    out = subprocess.run([sys.executable, "-c", "import lsquare; print(lsquare.__file__)"], capture_output=True, text=True, timeout=30)
    path = Path(out.stdout.strip()).resolve()
    assert "site-packages" in path.parts and SOURCE not in path.parents, path
