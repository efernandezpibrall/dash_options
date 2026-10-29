-- Retain the original immutable Jan/Feb run while adding Nov/Dec lifetimes.
ALTER TABLE at_lng.brent_delta_history_points
    DROP CONSTRAINT IF EXISTS brent_delta_history_points_contract_date_check,
    DROP CONSTRAINT IF EXISTS brent_delta_history_points_check1,
    DROP CONSTRAINT IF EXISTS ck_brent_history_contract_scope,
    DROP CONSTRAINT IF EXISTS ck_brent_history_security_scope;
ALTER TABLE at_lng.brent_delta_history_points
    ADD CONSTRAINT ck_brent_history_contract_scope CHECK
        (contract_date IN (DATE '2026-11-01',DATE '2026-12-01',DATE '2027-01-01',DATE '2027-02-01')),
    ADD CONSTRAINT ck_brent_history_security_scope CHECK
        ((contract_date=DATE '2026-11-01' AND source_security='COX6 Comdty')
        OR (contract_date=DATE '2026-12-01' AND source_security='COZ6 Comdty')
        OR (contract_date=DATE '2027-01-01' AND source_security='COF7 Comdty')
        OR (contract_date=DATE '2027-02-01' AND source_security='COG7 Comdty'));
-- 'ready' is deliberately invisible to the v1 reader, preserving rollback.
ALTER TABLE at_lng.brent_delta_history_runs
    DROP CONSTRAINT IF EXISTS brent_delta_history_runs_status_check;
ALTER TABLE at_lng.brent_delta_history_runs
    ADD CONSTRAINT brent_delta_history_runs_status_check
        CHECK (status IN ('building','complete','ready'));
CREATE TABLE IF NOT EXISTS at_lng.brent_delta_history_settlements (
    run_id uuid NOT NULL REFERENCES at_lng.brent_delta_history_runs(run_id),
    cob_date date NOT NULL,
    contract_date date NOT NULL CHECK
        (contract_date IN (DATE '2026-11-01',DATE '2026-12-01',DATE '2027-01-01',DATE '2027-02-01')),
    settlement_price double precision NOT NULL
        CHECK (settlement_price > 0 AND settlement_price < 'Infinity'::float8),
    source_security text NOT NULL,
    CHECK ((contract_date=DATE '2026-11-01' AND source_security='COX6 Comdty')
        OR (contract_date=DATE '2026-12-01' AND source_security='COZ6 Comdty')
        OR (contract_date=DATE '2027-01-01' AND source_security='COF7 Comdty')
        OR (contract_date=DATE '2027-02-01' AND source_security='COG7 Comdty')),
    PRIMARY KEY (run_id,cob_date,contract_date)
);
