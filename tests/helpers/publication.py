"""Shared settlement publication fixture helper."""
from options.vol_calibration import api


def publish(engine, candidate, key, **kwargs):
    return api.publish_hybrid_surface(
        engine,
        candidate["surface"],
        candidate["results"],
        commodity="BRENT",
        trading_date="2026-10-02",
        settlement_cob="2026-10-02",
        actor_subject="integration",
        created_by="integration",
        base_publication_id=None,
        expected_current_publication_id=None,
        idempotency_key=key,
        input_manifest=candidate["manifest"],
        validation_certificate=candidate["certificate"],
        expected_expiries=candidate["surface"].contract_date.unique(),
        **kwargs,
    )
