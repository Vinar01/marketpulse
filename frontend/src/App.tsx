import { useCallback, useEffect, useMemo, useState } from "react";
import { api, connectPrices, type Candle, type Health, type LatestPrice } from "./lib/api";
import { PriceTicker } from "./components/PriceTicker";
import { CandleChart } from "./components/CandleChart";
import { SystemHealth } from "./components/SystemHealth";
import { AskPanel } from "./components/AskPanel";

const RANGES = [
  { label: "1H", hours: 1 },
  { label: "6H", hours: 6 },
  { label: "24H", hours: 24 },
  { label: "3D", hours: 72 },
  { label: "7D", hours: 168 },
];

export default function App() {
  const [prices, setPrices] = useState<LatestPrice[]>([]);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [hours, setHours] = useState(24);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingChart, setLoadingChart] = useState(false);

  // Live prices over the WebSocket; one connection for the whole page.
  useEffect(() => connectPrices(setPrices, setWsConnected), []);

  // Health is polled rather than pushed: it changes slowly and a failed poll
  // is itself the signal we care about.
  useEffect(() => {
    const load = () => api.health().then(setHealth).catch(() => setHealth(null));
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const loadCandles = useCallback(async () => {
    setLoadingChart(true);
    setError(null);
    try {
      setCandles(await api.ohlcv(symbol, hours, 1000));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setCandles([]);
    } finally {
      setLoadingChart(false);
    }
  }, [symbol, hours]);

  useEffect(() => {
    loadCandles();
  }, [loadCandles]);

  const stats = useMemo(() => {
    if (candles.length === 0) return null;
    const open = Number(candles[0].open);
    const close = Number(candles[candles.length - 1].close);
    return {
      open,
      close,
      high: Math.max(...candles.map((c) => Number(c.high))),
      low: Math.min(...candles.map((c) => Number(c.low))),
      volume: candles.reduce((sum, c) => sum + Number(c.volume), 0),
      trades: candles.reduce((sum, c) => sum + c.trade_count, 0),
      changePct: open > 0 ? ((close - open) / open) * 100 : 0,
    };
  }, [candles]);

  return (
    <div className="app">
      <header className="top">
        <h1>MarketPulse</h1>
        <span className="sub">Trading Ops Console</span>
        <span style={{ marginLeft: "auto" }} className="sub">
          <span className={`dot ${health?.status === "ok" ? "on" : "off"}`} />
          {health?.status ?? "connecting"} · v{health?.version ?? "—"}
        </span>
      </header>

      <PriceTicker prices={prices} selected={symbol} onSelect={setSymbol} />

      <div className="grid">
        <div>
          <div className="panel">
            <h2>{symbol} · 1-minute candles</h2>
            <div className="controls">
              <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
                {prices.map((p) => (
                  <option key={p.symbol} value={p.symbol}>{p.symbol}</option>
                ))}
              </select>
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  className={`range-btn ${hours === r.hours ? "active" : ""}`}
                  onClick={() => setHours(r.hours)}
                >
                  {r.label}
                </button>
              ))}
              <button onClick={loadCandles} disabled={loadingChart}>
                {loadingChart ? "…" : "Refresh"}
              </button>
              <span className="muted small">{candles.length} candles</span>
            </div>
            {error && <div className="error">{error}</div>}
            <CandleChart candles={candles} symbol={symbol} />
          </div>

          {stats && (
            <div className="panel">
              <h2>Window statistics</h2>
              <table>
                <thead>
                  <tr>
                    <th>Open</th><th>High</th><th>Low</th><th>Close</th>
                    <th>Change</th><th>Volume</th><th>Trades</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{stats.open.toLocaleString()}</td>
                    <td>{stats.high.toLocaleString()}</td>
                    <td>{stats.low.toLocaleString()}</td>
                    <td>{stats.close.toLocaleString()}</td>
                    <td className={stats.changePct >= 0 ? "up" : "down"}>
                      {stats.changePct >= 0 ? "+" : ""}{stats.changePct.toFixed(2)}%
                    </td>
                    <td>{stats.volume.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
                    <td>{stats.trades.toLocaleString()}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <SystemHealth health={health} wsConnected={wsConnected} />
          <AskPanel />
        </div>
      </div>
    </div>
  );
}
