"""Guardrail Layer 1 and 2: argument validation and query bounds."""
import pytest

from app.ai.validation import (
    CompareSymbolsArgs,
    GuardrailError,
    LargestMovesArgs,
    OHLCVSeriesArgs,
    OHLCVSummaryArgs,
    validate_args,
)
from app.schemas import validate_symbol_format

WINDOW = {"start": "2026-08-18T00:00:00Z", "end": "2026-08-19T00:00:00Z"}


@pytest.mark.parametrize("raw,expected", [("btcusdt", "BTCUSDT"), (" ethusdt ", "ETHUSDT")])
def test_symbol_normalised(raw, expected):
    assert validate_symbol_format(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "BTC'; DROP TABLE ticks; --",
        "BTCUSDT UNION SELECT 1",
        "BTC",                      # too short
        "A" * 21,                   # too long
        "BTC-USDT",                 # punctuation
        "BTC USDT",                 # whitespace
        "",
        "ai_query_log",
    ],
)
def test_symbol_rejects_hostile_input(bad):
    with pytest.raises(ValueError):
        validate_symbol_format(bad)


def test_window_rejects_reversed_range():
    with pytest.raises(GuardrailError):
        validate_args(
            OHLCVSummaryArgs,
            {"symbol": "BTCUSDT", "start": WINDOW["end"], "end": WINDOW["start"]},
        )


def test_window_rejects_oversized_range():
    with pytest.raises(GuardrailError) as exc:
        validate_args(
            OHLCVSummaryArgs,
            {"symbol": "BTCUSDT", "start": "1970-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
        )
    assert "maximum" in str(exc.value)


def test_window_rejects_future_start():
    with pytest.raises(GuardrailError):
        validate_args(
            OHLCVSummaryArgs,
            {"symbol": "BTCUSDT", "start": "2099-01-01T00:00:00Z", "end": "2099-01-02T00:00:00Z"},
        )


def test_naive_timestamps_are_treated_as_utc():
    args = validate_args(
        OHLCVSummaryArgs,
        {"symbol": "BTCUSDT", "start": "2026-08-18T00:00:00", "end": "2026-08-19T00:00:00"},
    )
    assert args.start.tzinfo is not None
    assert args.start.utcoffset().total_seconds() == 0


def test_offset_timestamps_are_converted_to_utc():
    args = validate_args(
        OHLCVSummaryArgs,
        {"symbol": "BTCUSDT", "start": "2026-08-18T05:30:00+05:30", "end": "2026-08-19T00:00:00Z"},
    )
    assert args.start.hour == 0  # 05:30 IST is 00:00 UTC


def test_max_points_is_bounded():
    with pytest.raises(GuardrailError):
        validate_args(OHLCVSeriesArgs, {"symbol": "BTCUSDT", **WINDOW, "max_points": 10_000_000})


def test_top_n_rejects_negative():
    with pytest.raises(GuardrailError):
        validate_args(LargestMovesArgs, {"symbol": "BTCUSDT", **WINDOW, "top_n": -1})


def test_compare_symbols_caps_fanout():
    with pytest.raises(GuardrailError):
        validate_args(
            CompareSymbolsArgs, {"symbols": [f"SYM{i:04d}" for i in range(50)], **WINDOW}
        )


def test_type_confusion_rejected():
    with pytest.raises(GuardrailError):
        validate_args(OHLCVSummaryArgs, {"symbol": {"$ne": None}, **WINDOW})
