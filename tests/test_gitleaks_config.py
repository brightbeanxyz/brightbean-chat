"""``.gitleaks.toml`` parses, and its allowlists stay narrow.

This file is edited by nearly every branch — each one that adds a
credential-shaped fixture adds a block — and nothing else in the repo reads it,
so a syntax error is invisible until CI runs. When that happens gitleaks does
not report a leak, it refuses to start:

    FTL unable to load gitleaks config, err: While parsing config: toml:
    incomplete number

which fails the Secret scan job on every branch until somebody notices the
message is about the config rather than about a secret. That is how a merge
resolution that dropped one ``]`` reached CI.

The scoping assertions are the second half. An allowlist is the only exemption
from the scan (``.gitleaks.toml``'s own header says so), so one that quietly
grew a bare directory or dropped its ``targetRules`` would turn the whole
scanner off for everything under it without looking any different in a diff.
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest

CONFIG = Path(__file__).resolve().parents[1] / ".gitleaks.toml"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    with CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def test_the_config_parses(config: dict[str, Any]) -> None:
    """A malformed file stops gitleaks starting at all, on every branch."""
    assert config["extend"]["useDefault"] is True


def test_every_allowlist_says_what_it_is_for(config: dict[str, Any]) -> None:
    for allowlist in config.get("allowlists", []):
        assert allowlist.get("description"), "an exemption without a stated reason is not reviewable"


def test_every_allowlist_is_scoped_to_specific_rules(config: dict[str, Any]) -> None:
    """Without ``targetRules`` a path is exempt from *every* rule, not one."""
    for allowlist in config.get("allowlists", []):
        assert allowlist.get("targetRules"), allowlist["description"]


def test_every_allowlisted_path_is_a_file_pattern(config: dict[str, Any]) -> None:
    """A bare directory prefix would exempt anything added under it later."""
    for allowlist in config.get("allowlists", []):
        for path in allowlist.get("paths", []):
            assert path.endswith(("\\.py''", "\\.py", "\\.json", "\\.md", "$")) or "\\." in path, (
                f"{path!r} in {allowlist['description']!r} does not name a file extension"
            )


def test_the_allowlisted_paths_still_exist(config: dict[str, Any]) -> None:
    """A stale exemption outlives the file it was written for.

    Only the patterns naming one literal file are checked — a regex covering a
    directory is deliberately left alone, since matching it here would mean
    reimplementing gitleaks' own matching.
    """
    root = CONFIG.parent
    for allowlist in config.get("allowlists", []):
        for path in allowlist.get("paths", []):
            literal = path.replace("\\.", ".")
            if any(char in literal for char in "*+?[]()|"):
                continue
            assert (root / literal).exists(), f"{literal} is allowlisted but no longer exists"
