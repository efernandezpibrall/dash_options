import pytest
from dash_options.calibration import feature_flags
from dash_options.calibration.execution.settlement_worker import service_identity


def test_auto_flags_are_independent_and_default_off(monkeypatch):
    monkeypatch.setattr(feature_flags, "config_bool", lambda *a, **k: k["fallback"])
    for key in ("VOL_CALIBRATION_SETTLEMENT_JOBS_ENABLED", "VOL_CALIBRATION_HH_AUTO_SETTLEMENT_ENABLED", "VOL_CALIBRATION_BRENT_AUTO_SETTLEMENT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    assert not feature_flags.settlement_jobs_enabled()
    assert not feature_flags.settlement_auto_publication_enabled("HH")
    monkeypatch.setenv("VOL_CALIBRATION_SETTLEMENT_JOBS_ENABLED", "true")
    assert not feature_flags.settlement_auto_publication_enabled("HH")
    monkeypatch.setenv("VOL_CALIBRATION_HH_AUTO_SETTLEMENT_ENABLED", "true")
    assert feature_flags.settlement_auto_publication_enabled("HH")
    assert not feature_flags.settlement_auto_publication_enabled("BRENT")


def test_service_identity_requires_its_own_explicit_authority(monkeypatch):
    from dash_options.calibration.execution import settlement_worker
    monkeypatch.setattr(settlement_worker, "config_value", lambda *a, **k: k["fallback"])
    monkeypatch.delenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ACTOR", raising=False)
    monkeypatch.delenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ROLE", raising=False)
    monkeypatch.setenv("OPTIONS_LOCAL_AUTH_USER", "local-user")
    monkeypatch.setenv("OPTIONS_LOCAL_AUTH_ROLES", "publisher")
    with pytest.raises(PermissionError, match="Explicit settlement"):
        service_identity()
    monkeypatch.setenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ACTOR", "settlement-service")
    monkeypatch.setenv("VOL_CALIBRATION_SETTLEMENT_SERVICE_ROLE", "publisher")
    assert service_identity().subject == "settlement-service"
