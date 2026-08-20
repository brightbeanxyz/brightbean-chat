"""Production settings refuse to boot without their secrets (SECURITY-BASELINE §8).

Settings are imported once per process, so this has to run the boot in a real
subprocess to be a real test rather than a mock of one. ``manage.py check``
never opens a database connection, so no server is needed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[3]

# Variables that must not leak in from the developer's shell or from CI.
_STRIPPED = ("SECRET_KEY", "ENCRYPTION_KEY_SALT", "ALLOWED_HOSTS", "DEBUG", "DJANGO_SETTINGS_MODULE")


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED}
    # A developer's local .env sits next to the settings package and would
    # otherwise supply the very variables this test removes.
    env["DJANGO_ENV_FILE"] = str(BASE_DIR / "does-not-exist.env")
    return env


def _check(settings_module: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    # S603/S607 flag untrusted input reaching a subprocess. Every argument here
    # is a literal and shell=False, so neither applies.
    return subprocess.run(  # noqa: S603
        [sys.executable, "manage.py", "check", f"--settings={settings_module}"],
        env=env,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_COMPLETE = {
    "SECRET_KEY": "a-real-secret-key",
    "ENCRYPTION_KEY_SALT": "a-real-salt",
    "ALLOWED_HOSTS": "chat.example.com",
}


@pytest.mark.parametrize(
    ("missing", "expected_in_error"),
    [
        ("SECRET_KEY", "SECRET_KEY"),
        ("ENCRYPTION_KEY_SALT", "ENCRYPTION_KEY_SALT"),
        # ALLOWED_HOSTS is the one that used to slip through: settings imported
        # fine and every request 400'd instead, healthcheck included.
        ("ALLOWED_HOSTS", "ALLOWED_HOSTS"),
    ],
)
def test_production_refuses_to_boot_with_a_variable_missing(missing, expected_in_error):
    env = {**_clean_env(), **{k: v for k, v in _COMPLETE.items() if k != missing}}

    result = _check("config.settings.production", env)

    assert result.returncode != 0, result.stdout
    assert "ImproperlyConfigured" in result.stderr
    assert expected_in_error in result.stderr


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_count_as_missing(blank):
    """A variable set to whitespace is a misconfiguration, not a value."""
    env = {**_clean_env(), **dict.fromkeys(_COMPLETE, blank)}

    result = _check("config.settings.production", env)

    assert result.returncode != 0, result.stdout
    for name in _COMPLETE:
        assert name in result.stderr


def test_the_error_names_every_missing_variable_at_once():
    """One boot, one complete list — not a fix-and-retry loop per variable."""
    result = _check("config.settings.production", _clean_env())

    assert result.returncode != 0, result.stdout
    for name in _COMPLETE:
        assert name in result.stderr


def test_production_boots_when_everything_is_present():
    result = _check("config.settings.production", {**_clean_env(), **_COMPLETE})

    assert result.returncode == 0, result.stderr
    assert "System check identified no issues" in result.stdout


def test_development_boots_with_no_secrets_at_all():
    """A fresh clone must run ``manage.py`` before anyone has written a .env."""
    result = _check("config.settings.development", _clean_env())

    assert result.returncode == 0, result.stderr
