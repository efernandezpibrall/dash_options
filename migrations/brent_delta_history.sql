-- Additive, history-only storage; never changes active surface publications.
CREATE TABLE IF NOT EXISTS at_lng.brent_delta_history_runs (
    run_id uuid PRIMARY KEY,
    product text NOT NULL CHECK (product = 'BRENT'),
    start_date date NOT NULL,
    end_date date NOT NULL CHECK (end_date = start_date + 89),
    session_count integer NOT NULL CHECK (session_count > 0 AND session_count <= 66),
    status text NOT NULL DEFAULT 'building' CHECK (status IN ('building','complete')),
    content_fingerprint text NOT NULL UNIQUE,
    input_fingerprint text NOT NULL,
    input_payload jsonb NOT NULL,
    implementation jsonb NOT NULL,
    diagnostics jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS at_lng.brent_delta_history_points (
    run_id uuid NOT NULL REFERENCES at_lng.brent_delta_history_runs(run_id),
    cob_date date NOT NULL,
    contract_date date NOT NULL CHECK (contract_date IN (DATE '2027-01-01',DATE '2027-02-01')),
    option_expiration_date date NOT NULL CHECK (option_expiration_date > cob_date),
    delta double precision NOT NULL CHECK (delta IN (0.01,0.10,0.20,0.30,0.40,0.50,0.60,0.70,0.80,0.90,0.99)),
    volatility double precision NOT NULL CHECK (volatility > 0 AND volatility < 2),
    strike double precision NOT NULL CHECK (strike > 0 AND strike < 'Infinity'::float8),
    total_variance double precision NOT NULL CHECK (total_variance > 0 AND total_variance < 'Infinity'::float8),
    settlement_price double precision NOT NULL CHECK (settlement_price > 0 AND settlement_price < 'Infinity'::float8),
    source_security text NOT NULL,
    CHECK ((contract_date=DATE '2027-01-01' AND source_security='COF7 Comdty')
        OR (contract_date=DATE '2027-02-01' AND source_security='COG7 Comdty')),
    PRIMARY KEY (run_id,cob_date,contract_date,delta)
);
