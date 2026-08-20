import type { Health } from "../lib/api";

function statusClass(value?: string) {
  if (value === "ok") return "on";
  if (value === "degraded" || value === "stale") return "warn";
  return "off";
}

export function SystemHealth({ health, wsConnected }: { health: Health | null; wsConnected: boolean }) {
  if (!health) return <div className="panel"><h2>System</h2><span className="muted small">connecting…</span></div>;

  const ingest = health.ingest ?? {};
  const rows: [string, string | number | undefined][] = [
    ["Tick rows (est.)", ingest.tick_rows_estimate?.toLocaleString()],
    ["OHLCV candles", ingest.ohlcv_rows?.toLocaleString()],
    ["Tick partitions", ingest.tick_partitions],
    ["Ticks table size", ingest.ticks_size],
    [
      "Ingest lag",
      ingest.seconds_since_last_tick !== undefined
        ? `${ingest.seconds_since_last_tick.toFixed(1)} s`
        : "—",
    ],
    ["API uptime", `${Math.floor(health.uptime_s / 60)} min`],
  ];

  return (
    <div className="panel">
      <h2>System</h2>
      <div className="kv">
        <span className="k"><span className={`dot ${statusClass(health.checks.postgres)}`} />PostgreSQL</span>
        <span className="v small muted">{health.checks.postgres}</span>
      </div>
      <div className="kv">
        <span className="k"><span className={`dot ${statusClass(health.checks.redis)}`} />Redis</span>
        <span className="v small muted">{health.checks.redis}</span>
      </div>
      <div className="kv">
        <span className="k"><span className={`dot ${statusClass(health.checks.ingest)}`} />Ingestion</span>
        <span className="v small muted">{health.checks.ingest ?? "unknown"}</span>
      </div>
      <div className="kv">
        <span className="k"><span className={`dot ${wsConnected ? "on" : "off"}`} />Live socket</span>
        <span className="v small muted">{wsConnected ? "streaming" : "reconnecting"}</span>
      </div>
      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "12px 0" }} />
      {rows.map(([k, v]) => (
        <div className="kv" key={k}>
          <span className="k">{k}</span>
          <span className="v">{v ?? "—"}</span>
        </div>
      ))}
    </div>
  );
}
