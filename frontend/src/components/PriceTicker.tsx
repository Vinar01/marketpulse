import { useEffect, useRef } from "react";
import type { LatestPrice } from "../lib/api";

interface Props {
  prices: LatestPrice[];
  selected: string;
  onSelect: (symbol: string) => void;
}

/** Colour each tile by whether the price rose or fell since the last frame.
 *  The previous values live in a ref rather than state: they are only read
 *  during render to pick a colour, so storing them in state would cause an
 *  extra render on every tick for no visual difference. */
export function PriceTicker({ prices, selected, onSelect }: Props) {
  const previous = useRef<Record<string, number>>({});

  useEffect(() => {
    const next: Record<string, number> = {};
    for (const p of prices) next[p.symbol] = Number(p.price);
    previous.current = next;
  }, [prices]);

  return (
    <div className="ticker">
      {prices.map((p) => {
        const current = Number(p.price);
        const prior = previous.current[p.symbol];
        const direction = prior === undefined || current === prior ? "" : current > prior ? "up" : "down";
        return (
          <div
            key={p.symbol}
            className={`tile ${p.symbol === selected ? "active" : ""}`}
            onClick={() => onSelect(p.symbol)}
          >
            <div className="sym">{p.symbol}</div>
            <div className={`px ${direction}`}>
              {current.toLocaleString(undefined, {
                minimumFractionDigits: 2,
                maximumFractionDigits: current < 1 ? 6 : 2,
              })}
            </div>
          </div>
        );
      })}
      {prices.length === 0 && <div className="muted small">waiting for the first tick…</div>}
    </div>
  );
}
