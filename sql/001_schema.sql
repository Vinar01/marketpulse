-- MarketPulse core schema.
--
-- Design notes that matter:
--  * Money is NUMERIC, never float. 0.1 + 0.2 != 0.3 in binary floating point and
--    a market-data store that silently loses cents is worthless.
--  * Timestamps are TIMESTAMPTZ. Exchanges publish UTC epoch millis; storing a
--    naive timestamp throws away the offset and breaks every window query.
--  * `ticks` is RANGE partitioned by day. Partition pruning turns a query for
--    "yesterday" into a scan of one small table instead of the whole history,
--    and retention becomes DROP TABLE (instant) instead of DELETE (bloat + vacuum).

CREATE TABLE IF NOT EXISTS symbols (
    symbol        TEXT PRIMARY KEY,
    base_asset    TEXT        NOT NULL,
    quote_asset   TEXT        NOT NULL,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- ticks: one row per aggregated trade from the exchange.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticks (
    symbol          TEXT           NOT NULL REFERENCES symbols(symbol),
    trade_id        BIGINT         NOT NULL,
    price           NUMERIC(20, 8) NOT NULL,
    qty             NUMERIC(20, 8) NOT NULL,
    quote_qty       NUMERIC(24, 8) NOT NULL,
    is_buyer_maker  BOOLEAN        NOT NULL,
    ts              TIMESTAMPTZ    NOT NULL,
    ingested_at     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    -- The partition key MUST be part of any unique constraint on a partitioned
    -- table. (symbol, trade_id) is what actually makes a trade unique; ts rides
    -- along to satisfy that rule. This ordering is deliberate: it makes the PK
    -- index a dedupe index, NOT a range-scan index -- see idx_ticks_symbol_ts.
    PRIMARY KEY (symbol, trade_id, ts)
) PARTITION BY RANGE (ts);

-- The workhorse index. Every time-range query is "one symbol, ts between A and B",
-- and the leading column of the PK index is symbol but its second column is
-- trade_id, so the PK cannot answer a range predicate on ts efficiently.
-- DESC because the hot query is "most recent first".
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks (symbol, ts DESC);

-- Safety net: any row whose day-partition does not exist yet lands here instead
-- of raising. The partition manager runs days ahead, so in practice this stays
-- empty -- but an empty default partition costs nothing and prevents data loss.
CREATE TABLE IF NOT EXISTS ticks_default PARTITION OF ticks DEFAULT;

-- ---------------------------------------------------------------------------
-- ohlcv_1m: continuous 1-minute rollup, derived from ticks.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv_1m (
    symbol        TEXT           NOT NULL REFERENCES symbols(symbol),
    bucket        TIMESTAMPTZ    NOT NULL,
    open          NUMERIC(20, 8) NOT NULL,
    high          NUMERIC(20, 8) NOT NULL,
    low           NUMERIC(20, 8) NOT NULL,
    close         NUMERIC(20, 8) NOT NULL,
    volume        NUMERIC(28, 8) NOT NULL,
    quote_volume  NUMERIC(28, 8) NOT NULL,
    trade_count   INTEGER        NOT NULL,
    updated_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bucket)
);

-- Cross-symbol scans ("which symbol moved most yesterday") filter on bucket
-- first, which the (symbol, bucket) PK cannot serve.
CREATE INDEX IF NOT EXISTS idx_ohlcv_bucket ON ohlcv_1m (bucket DESC);

-- ---------------------------------------------------------------------------
-- Operational tables. Deliberately NOT granted to the AI read-only role.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_watermark (
    name        TEXT PRIMARY KEY,
    value_ts    TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_query_log (
    id             BIGSERIAL PRIMARY KEY,
    question       TEXT        NOT NULL,
    answer         TEXT,
    tool_calls     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    input_tokens   INTEGER     NOT NULL DEFAULT 0,
    output_tokens  INTEGER     NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms     INTEGER     NOT NULL DEFAULT 0,
    blocked_by     TEXT,
    api_key_role   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_query_log_created ON ai_query_log (created_at DESC);

-- ---------------------------------------------------------------------------
-- Partition management.
-- ---------------------------------------------------------------------------

-- Create daily partitions for [today - back_days, today + ahead_days].
-- Idempotent: safe to call on every worker start and on a timer.
CREATE OR REPLACE FUNCTION ensure_tick_partitions(ahead_days INT DEFAULT 3, back_days INT DEFAULT 1)
RETURNS INT AS $$
DECLARE
    d           DATE;
    part_name   TEXT;
    created     INT := 0;
BEGIN
    FOR d IN
        SELECT generate_series(
            (now() AT TIME ZONE 'UTC')::DATE - back_days,
            (now() AT TIME ZONE 'UTC')::DATE + ahead_days,
            '1 day'::INTERVAL
        )::DATE
    LOOP
        part_name := format('ticks_%s', to_char(d, 'YYYYMMDD'));
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
            -- Bind the boundaries as explicit UTC instants. Passing a bare DATE
            -- would let Postgres interpret it in the session timezone, so a
            -- server running in IST would cut partitions at 00:00 IST and a
            -- "yesterday UTC" query would straddle two partitions.
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF ticks FOR VALUES FROM (%L) TO (%L)',
                part_name,
                (d::TIMESTAMP AT TIME ZONE 'UTC'),
                ((d + 1)::TIMESTAMP AT TIME ZONE 'UTC')
            );
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END;
$$ LANGUAGE plpgsql;

-- Retention: drop whole partitions older than `keep_days`. O(1) per partition,
-- no row-by-row DELETE, no dead tuples, no vacuum storm.
CREATE OR REPLACE FUNCTION drop_old_tick_partitions(keep_days INT DEFAULT 30)
RETURNS INT AS $$
DECLARE
    r        RECORD;
    cutoff   DATE := (now() AT TIME ZONE 'UTC')::DATE - keep_days;
    dropped  INT := 0;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'ticks'
          AND c.relname ~ '^ticks_[0-9]{8}$'
    LOOP
        IF to_date(right(r.relname, 8), 'YYYYMMDD') < cutoff THEN
            EXECUTE format('DROP TABLE %I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Aggregation: fold ticks into 1-minute candles for a half-open window.
-- Called by the aggregator worker; kept in SQL so the whole rollup is one
-- round trip and one transaction.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION aggregate_ohlcv_1m(win_start TIMESTAMPTZ, win_end TIMESTAMPTZ)
RETURNS INT AS $$
DECLARE
    affected INT;
BEGIN
    WITH windowed AS (
        SELECT
            symbol,
            date_trunc('minute', ts) AS bucket,
            price,
            qty,
            quote_qty,
            ts,
            trade_id,
            -- first/last trade inside the minute, ordered by exchange time then id
            ROW_NUMBER() OVER (
                PARTITION BY symbol, date_trunc('minute', ts) ORDER BY ts, trade_id
            ) AS rn_first,
            ROW_NUMBER() OVER (
                PARTITION BY symbol, date_trunc('minute', ts) ORDER BY ts DESC, trade_id DESC
            ) AS rn_last
        FROM ticks
        WHERE ts >= win_start AND ts < win_end
    ),
    rolled AS (
        SELECT
            symbol,
            bucket,
            MAX(price) FILTER (WHERE rn_first = 1) AS open,
            MAX(price)                            AS high,
            MIN(price)                            AS low,
            MAX(price) FILTER (WHERE rn_last = 1)  AS close,
            SUM(qty)                              AS volume,
            SUM(quote_qty)                        AS quote_volume,
            COUNT(*)::INT                         AS trade_count
        FROM windowed
        GROUP BY symbol, bucket
    )
    INSERT INTO ohlcv_1m AS o
        (symbol, bucket, open, high, low, close, volume, quote_volume, trade_count)
    SELECT symbol, bucket, open, high, low, close, volume, quote_volume, trade_count
    FROM rolled
    ON CONFLICT (symbol, bucket) DO UPDATE SET
        -- The newest pass is authoritative: a late-arriving trade can only widen
        -- the candle, and open/close are recomputed from the full window anyway.
        open         = EXCLUDED.open,
        high         = GREATEST(o.high, EXCLUDED.high),
        low          = LEAST(o.low, EXCLUDED.low),
        close        = EXCLUDED.close,
        volume       = EXCLUDED.volume,
        quote_volume = EXCLUDED.quote_volume,
        trade_count  = EXCLUDED.trade_count,
        updated_at   = now();

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;
