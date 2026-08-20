/** Thin API client. Every call carries the API key; the base URL is empty in
 *  dev so Vite's proxy handles it, and set explicitly in production builds. */

const BASE = import.meta.env.VITE_API_BASE ?? "";
const KEY = import.meta.env.VITE_API_KEY ?? "demo-key-public";

export interface LatestPrice {
  symbol: string;
  price: string;
  qty: string;
  ts: string;
  source?: string;
}

export interface Candle {
  symbol: string;
  bucket: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quote_volume: string;
  trade_count: number;
}

export interface SymbolInfo {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  is_active: boolean;
}

export interface Health {
  status: string;
  version: string;
  uptime_s: number;
  checks: Record<string, string>;
  ingest: {
    tick_rows_estimate?: number;
    ohlcv_rows?: number;
    last_tick_ts?: string | null;
    seconds_since_last_tick?: number;
    tick_partitions?: number;
    ticks_size?: string;
  };
}

export interface ToolCall {
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  error: string | null;
  row_count: number | null;
  duration_ms: number;
}

export interface AskResponse {
  answer: string;
  tool_calls: ToolCall[];
  blocked_by: string | null;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number; model: string };
  latency_ms: number;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", "X-API-Key": KEY, ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    // Surface the server's own message; a generic "request failed" hides the
    // 422 detail that tells the user exactly which parameter was wrong.
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  symbols: () => request<SymbolInfo[]>("/api/v1/symbols"),
  latestAll: () => request<LatestPrice[]>("/api/v1/prices/latest"),
  ohlcv: (symbol: string, hours: number, limit = 1000) =>
    request<Candle[]>(`/api/v1/ohlcv/${symbol}?hours=${hours}&limit=${limit}`),
  ohlcvRange: (symbol: string, start: string, end: string, limit = 1000) =>
    request<Candle[]>(
      `/api/v1/ohlcv/${symbol}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(
        end,
      )}&limit=${limit}`,
    ),
  health: () => request<Health>("/health"),
  ask: (question: string) =>
    request<AskResponse>("/api/v1/ask", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};

/** Live price socket with automatic reconnect and jittered backoff — the same
 *  reasoning as the ingestion worker, applied to the browser. */
export function connectPrices(
  onPrices: (rows: LatestPrice[]) => void,
  onStatus: (connected: boolean) => void,
): () => void {
  let socket: WebSocket | null = null;
  let attempt = 0;
  let timer: number | undefined;
  let closed = false;

  const open = () => {
    if (closed) return;
    const httpBase = BASE || window.location.origin;
    const wsBase = httpBase.replace(/^http/, "ws");
    socket = new WebSocket(`${wsBase}/ws/prices?api_key=${encodeURIComponent(KEY)}`);

    socket.onopen = () => {
      attempt = 0;
      onStatus(true);
    };
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "prices") onPrices(payload.data);
    };
    socket.onclose = () => {
      onStatus(false);
      if (closed) return;
      const delay = Math.min(15000, 500 * 2 ** attempt) * Math.random();
      attempt = Math.min(attempt + 1, 6);
      timer = window.setTimeout(open, delay);
    };
    socket.onerror = () => socket?.close();
  };

  open();
  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    socket?.close();
  };
}
