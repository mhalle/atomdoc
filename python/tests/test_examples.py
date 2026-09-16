"""The examples run, and say what they claim to."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"


def run(*args: str) -> str:
    result = subprocess.run([sys.executable, str(EXAMPLES / "local_storage.py"), *args],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("backend", [[], ["--sqlite"]])
def test_local_storage_demo(backend):
    out = run(*backend, "demo")
    assert "four edits, 1 save" in out
    assert "refused, then re-applied" in out and "(the phone's edit): True" in out
    assert "['scratch/session-1']; half a second unrenewed: []" in out


@pytest.mark.parametrize("backend", [[], ["--sqlite"]])
def test_local_storage_survives_the_process(tmp_path, backend):
    base = [*backend, "--dir", str(tmp_path)]
    run(*base, "add", "buy", "milk")
    run(*base, "add", "water", "plants")
    run(*base, "done", "1")
    shown = run(*base, "show")                                  # a fourth process
    assert "1. [x] buy milk" in shown and "2. [ ] water plants" in shown
    result = subprocess.run([sys.executable, str(EXAMPLES / "local_storage.py"),
                             *base, "done", "9"], capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "no item 9" in result.stderr
