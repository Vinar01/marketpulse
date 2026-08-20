"""Prometheus metrics. Every number the Grafana dashboard shows originates here."""
from prometheus_client import Counter, Gauge, Histogram

# --- Ingestion ---
INGEST_TRADES_TOTAL = Counter(
    "ingest_trades_total", "Trades received from the exchange stream", ["symbol"]
)
INGEST_ROWS_WRITTEN = Counter("ingest_rows_written_total", "Tick rows committed to Postgres")
INGEST_ROWS_DUPLICATE = Counter(
    "ingest_rows_duplicate_total", "Rows skipped by ON CONFLICT DO NOTHING (idempotent replay)"
)
INGEST_LAG_SECONDS = Gauge(
    "ingest_lag_seconds", "Exchange event time -> local receive time", ["symbol"]
)
INGEST_QUEUE_DEPTH = Gauge("ingest_queue_depth", "Items waiting in the in-process buffer queue")
INGEST_DROPPED = Counter(
    "ingest_dropped_total", "Trades dropped because the queue was full (backpressure shed)"
)
INGEST_RECONNECTS = Counter("ingest_reconnects_total", "WebSocket reconnect attempts")
INGEST_CONNECTED = Gauge("ingest_connected", "1 when the exchange WebSocket is connected")
DB_WRITE_LATENCY = Histogram(
    "db_write_latency_seconds",
    "COPY/INSERT batch latency",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
DB_BATCH_SIZE = Histogram(
    "db_batch_size_rows", "Rows per write batch", buckets=(1, 10, 50, 100, 250, 500, 1000, 2500)
)
AGG_ROWS_UPSERTED = Counter("agg_rows_upserted_total", "1-minute OHLCV rows upserted")
AGG_RUN_SECONDS = Histogram("agg_run_seconds", "Duration of one aggregation pass")

# --- API ---
API_REQUESTS = Counter("api_requests_total", "HTTP requests", ["method", "path", "status"])
API_LATENCY = Histogram(
    "api_request_latency_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
API_RATE_LIMITED = Counter("api_rate_limited_total", "Requests rejected by the rate limiter")
WS_CONNECTIONS = Gauge("ws_connections", "Open dashboard WebSocket connections")

# --- Cache ---
CACHE_HITS = Counter("cache_hits_total", "Redis cache hits", ["key_kind"])
CACHE_MISSES = Counter("cache_misses_total", "Redis cache misses", ["key_kind"])
CACHE_ERRORS = Counter("cache_errors_total", "Redis errors (served from Postgres instead)")

# --- AI ---
AI_REQUESTS = Counter("ai_requests_total", "AI question requests", ["outcome"])
AI_LATENCY = Histogram(
    "ai_request_latency_seconds",
    "End-to-end AI answer latency",
    buckets=(0.5, 1, 2, 4, 8, 16, 32, 64),
)
AI_TOKENS = Counter("ai_tokens_total", "Anthropic tokens consumed", ["kind"])
AI_COST_USD = Counter("ai_cost_usd_total", "Estimated Anthropic spend in USD")
AI_TOOL_CALLS = Counter("ai_tool_calls_total", "Typed tool invocations", ["tool"])
AI_TOOL_ERRORS = Counter(
    "ai_tool_errors_total", "Tool calls rejected by the validation layer", ["tool", "reason"]
)
AI_GUARDRAIL_BLOCKS = Counter(
    "ai_guardrail_blocks_total", "Requests stopped by a guardrail", ["layer"]
)
