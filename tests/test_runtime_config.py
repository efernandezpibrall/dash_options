import pytest

import db_fallback
from runtime_config import clear_runtime_config_cache, config_bool, config_value
from vol_calibration.feature_flags import (
    background_jobs_enabled,
    publication_enabled,
    writes_enabled,
)


@pytest.fixture(autouse=True)
def _reset_runtime_config_between_tests():
    clear_runtime_config_cache()
    yield
    clear_runtime_config_cache()


def _use_empty_config(monkeypatch, tmp_path):
    config_path = tmp_path / "empty.ini"
    config_path.write_text("")
    monkeypatch.setenv("OPTIONS_CONFIG_PATH", str(config_path))
    clear_runtime_config_cache()


def test_trino_ssl_verification_defaults_to_enabled(monkeypatch, tmp_path):
    _use_empty_config(monkeypatch, tmp_path)
    monkeypatch.delenv("TRINOS_VERIFY_SSL", raising=False)
    clear_runtime_config_cache()

    assert config_bool("TRINOS", "VERIFY_SSL", fallback=True) is True


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("true", True),
        ("YES", True),
        ("1", True),
        ("false", False),
        ("Off", False),
        ("0", False),
    ],
)
def test_trino_ssl_verification_parses_explicit_environment_override(
    monkeypatch,
    tmp_path,
    raw_value,
    expected,
):
    _use_empty_config(monkeypatch, tmp_path)
    monkeypatch.setenv("TRINOS_VERIFY_SSL", raw_value)
    clear_runtime_config_cache()

    assert config_bool("TRINOS", "VERIFY_SSL", fallback=True) is expected


def test_invalid_ssl_verification_setting_fails_closed(monkeypatch, tmp_path):
    _use_empty_config(monkeypatch, tmp_path)
    monkeypatch.setenv("TRINOS_VERIFY_SSL", "sometimes")
    clear_runtime_config_cache()

    with pytest.raises(ValueError, match="must be a boolean"):
        config_bool("TRINOS", "VERIFY_SSL", fallback=True)


def test_aspect_environment_settings_are_available_without_a_config_section(
    monkeypatch,
    tmp_path,
):
    _use_empty_config(monkeypatch, tmp_path)
    monkeypatch.setenv("ASPECT_USERNAME", "aspect-user")
    monkeypatch.setenv("ASPECT_PASSWORD", "aspect-password")
    monkeypatch.setenv("ASPECT_VERIFY_SSL", "true")
    clear_runtime_config_cache()

    assert config_value("ASPECT", "USERNAME") == "aspect-user"
    assert config_value("ASPECT", "PASSWORD") == "aspect-password"
    assert config_bool("ASPECT", "VERIFY_SSL", fallback=True) is True


def test_trino_connection_receives_the_configured_ssl_setting(monkeypatch):
    captured = {}

    class _Connection:
        def close(self):
            captured["closed"] = True

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _Connection()

    monkeypatch.setattr("trino.dbapi.connect", fake_connect)
    monkeypatch.setattr("trino.auth.JWTAuthentication", lambda token: token)
    monkeypatch.setattr(db_fallback, "TRINOS_HOST", "trino.example.com")
    monkeypatch.setattr(db_fallback, "TRINOS_USERNAME", "options")
    monkeypatch.setattr(db_fallback, "TRINOS_TOKEN", "token")
    monkeypatch.setattr(db_fallback, "TRINOS_VERIFY_SSL", False)
    monkeypatch.setattr(db_fallback, "read_table_conn", lambda connection, query: query)

    assert db_fallback.read_trino_query("SELECT 1") == "SELECT 1"
    assert captured["verify"] is False
    assert captured["closed"] is True


def test_all_calibration_mutation_flags_default_to_disabled(monkeypatch):
    for name in (
        "VOL_CALIBRATION_WRITES_ENABLED",
        "VOL_CALIBRATION_PUBLISH_ENABLED",
        "VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    assert writes_enabled() is False
    assert publication_enabled() is False
    assert background_jobs_enabled() is False


def test_background_jobs_cannot_bypass_disabled_writes(monkeypatch):
    monkeypatch.setenv("VOL_CALIBRATION_WRITES_ENABLED", "false")
    monkeypatch.setenv("VOL_CALIBRATION_BACKGROUND_JOBS_ENABLED", "true")

    assert background_jobs_enabled() is False
