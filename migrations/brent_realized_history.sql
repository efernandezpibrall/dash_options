CREATE TABLE IF NOT EXISTS at_lng.brent_realized_history_runs (
    run_id uuid PRIMARY KEY,
    start_date date NOT NULL,
    end_date date NOT NULL CHECK (end_date >= start_date),
    input_fingerprint text NOT NULL UNIQUE,
    input_payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('building','ready')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS at_lng.brent_realized_history_settlements (
    run_id uuid NOT NULL REFERENCES at_lng.brent_realized_history_runs(run_id),
    cob_date date NOT NULL,
    contract_date date NOT NULL,
    source_security text NOT NULL,
    settlement_price double precision NOT NULL CHECK (settlement_price > 0 AND settlement_price < 'Infinity'::float8),
    PRIMARY KEY (run_id,contract_date,cob_date)
);
