"""Dependency-floor guards.

Some pins exist for security rather than compatibility, and a well-meaning
"relax the pin" change can silently reintroduce a CVE. Assert the floors here so
that shows up as a failing test instead of a shipped vulnerability.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _dependency_specifiers() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specifiers = {}
    for raw in data["project"]["dependencies"]:
        name, _, spec = raw.partition(">=")
        specifiers[name.strip().lower()] = ">=%s" % spec.strip()
    return specifiers


def test_curl_cffi_floor_excludes_ghsa_qw2m_4pqf_rmpp() -> None:
    """GHSA-qw2m-4pqf-rmpp (CVE-2026-33752): redirect SSRF, fixed in 0.15.0.

    Every curl_cffi release before 0.15.0 is vulnerable, so the declared floor
    must not allow one. This is not a cosmetic pin.
    """
    specifier = _dependency_specifiers()["curl_cffi"]
    assert specifier == ">=0.15.0", (
        "curl_cffi floor must stay >=0.15.0 to exclude GHSA-qw2m-4pqf-rmpp; got %s"
        % specifier
    )


def test_installed_curl_cffi_is_not_vulnerable() -> None:
    """The resolved environment, not just the declaration, must be patched."""
    curl_cffi = pytest.importorskip("curl_cffi")
    raw = getattr(curl_cffi, "__version__", "0")
    parts = []
    for chunk in raw.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    version = tuple(parts[:3])

    assert version >= (0, 15, 0), (
        "installed curl_cffi %s is affected by GHSA-qw2m-4pqf-rmpp" % raw
    )


def test_xclienttransaction_floor_can_parse_the_current_layout() -> None:
    """1.0.1 returns None for the numeric-module layout X ships since 2026-09.

    1.0.3 parses it; pinning lower silently breaks x-client-transaction-id.
    """
    assert _dependency_specifiers()["xclienttransaction"] == ">=1.0.3"
