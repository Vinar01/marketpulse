import { useEffect, useRef } from "react";
import { createChart, type IChartApi, type ISeriesApi } from "lightweight-charts";
import type { Candle } from "../lib/api";

interface Props {
  candles: Candle[];
  symbol: string;
}

/** The chart instance is imperative and expensive, so it is created once and
 *  then fed new data. Recreating it on every data change would drop the user's
 *  zoom and pan on every refresh. */
export function CandleChart({ candles, symbol }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!container.current) return;

    const instance = createChart(container.current, {
      layout: { background: { color: "#12161f" }, textColor: "#8b93a7" },
      grid: { vertLines: { color: "#1c2130" }, horzLines: { color: "#1c2130" } },
      rightPriceScale: { borderColor: "#232936" },
      timeScale: { borderColor: "#232936", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      autoSize: true,
    });

    candleSeries.current = instance.addCandlestickSeries({
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    });

    volumeSeries.current = instance.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    instance.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    chart.current = instance;
    return () => {
      instance.remove();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    if (!candleSeries.current || !volumeSeries.current) return;

    // lightweight-charts wants epoch seconds and strictly ascending, unique
    // timestamps; the API already returns ascending buckets.
    const bars = candles.map((c) => ({
      time: Math.floor(new Date(c.bucket).getTime() / 1000) as never,
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    }));

    const volumes = candles.map((c) => ({
      time: Math.floor(new Date(c.bucket).getTime() / 1000) as never,
      value: Number(c.volume),
      color: Number(c.close) >= Number(c.open) ? "rgba(38,166,154,.4)" : "rgba(239,83,80,.4)",
    }));

    candleSeries.current.setData(bars);
    volumeSeries.current.setData(volumes);
    chart.current?.timeScale().fitContent();
  }, [candles, symbol]);

  return <div className="chart" ref={container} />;
}
