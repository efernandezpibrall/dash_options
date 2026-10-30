"""Paired-release replay against the real migration schema and lifecycle guards."""
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import text

from options.valuation.publications import select_publication
from options.quotes.jkm_quote_valuation import price_jkm_market, JKMQuoteValuationError
from dash_options.features.pricer import surface_reference as adapter


@pytest.fixture
def frozen_publications(database):
    # Read the paired analytics capture without importing checkout code, so the
    # same test also verifies installed wheels. Numerical inputs are unchanged;
    # non-pricing grid metadata below is synthetic migration-fixture metadata.
    capture_path = (Path(__file__).parents[3] / 'options' / 'tests' / 'fixtures'
                    / 'valuation_replay' / 'jkm_quote.json')
    capture = json.loads(capture_path.read_text())
    snapshot = capture['valuation']['snapshot']
    component = capture['valuation']['components'][0]
    month = date(2027, 1, 1)
    market = SimpleNamespace(received_at='2026-10-01T08:15:29.377+00:00',
        contract_month=month, strip_months=(month,), structure_code=None,
        future_price=snapshot['forward'], future_contract_month=month,
        strike=capture['strike'], option_type='P', bid=capture['bid'],
        offer=capture['offer'], single_price=None)
    ids = {}
    with database.begin() as connection:
        for name, stamp, state in [('old', '08:00:00', 'superseded'),
                                    ('new', '09:00:00', 'published'),
                                    ('draft', '10:00:00', 'approved'),
                                    ('rejected', '11:00:00', 'rejected')]:
            run, publication = str(uuid4()), str(uuid4())
            ids[name] = publication
            params = dict(run=run, publication=publication, cob=snapshot['surface_cob_date'],
                          stamp='2026-10-01T'+stamp+'Z')
            connection.execute(text("""INSERT INTO at_lng.vol_calibration_runs
                (run_id,commodity,cob_date,input_fingerprint,engine_version,configuration,created_by,idempotency_key)
                VALUES (:run,'JKM',:cob,'frozen','test','{}','test',:run)"""), params)
            connection.execute(text("""INSERT INTO at_lng.vol_surface_publications
                (publication_id,run_id,commodity,cob_date,status,is_active,approved_by,approved_at,idempotency_key)
                VALUES (:publication,:run,'JKM',:cob,'approved',false,'test',:stamp,:publication)"""), params)
            points = [dict(params, month=month, expiry=snapshot['option_expiration_date'],
                           strike=float(strike), vol=float(vol), forward=component['saved_forward'])
                for strike, vol in zip(np.array(snapshot['strikes']) * component['saved_forward'] / snapshot['forward'],
                                       snapshot['volatilities'])]
            connection.execute(text("""INSERT INTO at_lng.implied_volatility_surface_calibrated
                (publication_id,run_id,commodity,cob_date,contract_date,option_expiration_date,
                 strike,put_call,volatility,working_forward,source_name,input_fingerprint,
                 delta,total_variance,surface_region,blend_classification,calibration_basis,
                 calibration_method,calibration_policy_version)
                VALUES (:publication,:run,'JKM',:cob,:month,:expiry,:strike,'C',:vol,:forward,'official','frozen',
                 0.5,:vol*:vol*(CAST(:expiry AS date)-CAST(:cob AS date))/365.25,
                 'core','pchip_core','observed','PCHIP-core/Wing-v2-tail','frozen_reader_replay')"""), points)
            if state in {'published', 'superseded'}:
                connection.execute(text("""UPDATE at_lng.vol_surface_publications
                    SET status='published',is_active=true,published_by='test',published_at=:stamp
                    WHERE publication_id=:publication"""), params)
            if state in {'superseded', 'rejected'}:
                connection.execute(text("""UPDATE at_lng.vol_surface_publications
                    SET status=:state,is_active=false WHERE publication_id=:publication"""), dict(params, state=state))
    adapter.clear_published_surface_reference_cache()
    try:
        yield database, ids, market, capture
    finally:
        adapter.clear_published_surface_reference_cache()


def test_real_lifecycle_timestamp_and_identity_selection(frozen_publications):
    engine, ids, market, _ = frozen_publications
    def selected(**kw):
        result = select_publication(engine, 'JKM', '2026-10-01', **kw)
        return None if result is None else str(result['publication_id'])
    assert selected() == ids['new']  # Restated-COB selection is unchanged.
    assert selected(timestamp=market.received_at) == ids['old']
    assert selected(timestamp='2026-10-01T09:00:00Z') == ids['new']
    assert selected(timestamp='2026-10-01T07:59:59Z') is None
    assert selected(publication_id=ids['old']) == ids['old']
    assert selected(publication_id=ids['new'], timestamp=market.received_at) is None
    for key in ['draft', 'rejected']:
        assert selected(publication_id=ids[key]) is None
    assert select_publication(engine, 'TTF', '2026-10-01', publication_id=ids['old']) is None
    assert select_publication(engine, 'JKM', '2026-09-29') is None


def test_database_quote_reader_replays_frozen_prices_and_greeks(frozen_publications):
    engine, ids, market, capture = frozen_publications
    actual = price_jkm_market(market, engine)
    assert actual.snapshot.surface_publication_id == ids['old']
    for name in ['theoretical_price', 'our_volatility', 'delta', 'gamma', 'theta', 'vega',
                 'rho', 'bid_implied_volatility', 'offer_implied_volatility']:
        assert getattr(actual, name) == pytest.approx(capture['valuation'][name], abs=1e-10)
    assert price_jkm_market(market, engine, publication_id=ids['old']) == actual
    for key in ['new', 'draft', 'rejected']:
        with pytest.raises(JKMQuoteValuationError, match='immutable publication'):
            price_jkm_market(market, engine, publication_id=ids[key])


def test_dash_database_slices_keep_timestamp_and_pinned_caches_separate(frozen_publications):
    engine, ids, market, capture = frozen_publications
    kwargs = dict(engine=engine)
    current = adapter.load_published_surface_slice('JKM', '2026-10-01', market.strip_months, **kwargs)
    historical = adapter.load_published_surface_slice('JKM', '2026-10-01', market.strip_months,
        valuation_timestamp=market.received_at, **kwargs)
    pinned = adapter.load_published_surface_slice('JKM', '2026-10-01', market.strip_months,
        publication_id=ids['old'], **kwargs)
    expiry = adapter.load_published_surface_expiry_slice('JKM', '2026-10-01',
        capture['valuation']['snapshot']['option_expiration_date'], publication_id=ids['old'], **kwargs)
    assert current['publication_id'] == ids['new']
    assert historical['publication_id'] == pinned['publication_id'] == expiry['publication_id'] == ids['old']
    for result in [current, historical, pinned, expiry]:
        assert len(result['points']) == len(capture['valuation']['snapshot']['strikes'])
    assert np.array_equal(pinned['points'].volatility, capture['valuation']['snapshot']['volatilities'])
    with pytest.raises(adapter.SurfaceReferenceError):
        adapter.load_published_surface_slice('JKM', '2026-10-01', market.strip_months,
            publication_id=ids['new'], valuation_timestamp=market.received_at, **kwargs)
