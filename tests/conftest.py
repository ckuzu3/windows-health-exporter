"""Shared test setup.

Almost everything here runs anywhere, because the collectors talk to an injected runner
rather than to PowerShell. The few tests that really call PowerShell carry the `windows`
marker and are skipped off Windows, so a Linux CI runner stays green without pretending
to have tested them.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001 - the hook's signature is fixed
    items: list[pytest.Item],
) -> None:
    """Skip the PowerShell tests when there is no PowerShell."""
    if os.name == "nt":
        return

    skip_windows = pytest.mark.skip(reason="needs Windows and PowerShell")
    for item in items:
        if "windows" in item.keywords:
            item.add_marker(skip_windows)
