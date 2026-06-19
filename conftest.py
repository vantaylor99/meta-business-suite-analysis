"""Pytest configuration shared across the test suite.

Redirect pytest's temporary directory into a repo-local, user-owned folder.

On some Windows machines the shared system temp location (``%LOCALAPPDATA%\\Temp``)
can end up with a ``pytest-of-<user>`` directory whose ACLs are corrupted (often
by a killed process or antivirus). When that happens pytest fails at fixture
setup with ``PermissionError: [WinError 5]`` before any test runs, even though
the code under test is fine. Pointing ``basetemp`` at a dedicated folder inside
the repo avoids that entirely and behaves identically on every machine. The
folder is git-ignored.

Set ``--basetemp`` explicitly on the command line to override this.
"""

from __future__ import annotations

from pathlib import Path


def pytest_configure(config):
    if config.option.basetemp:
        return
    base = Path(__file__).parent / ".pytmp"
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base / "pt")
