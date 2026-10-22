#!/usr/bin/env python3
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


REQUIRED = ["open", "high", "low", "close", "volume"]


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


def load_data(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".json":
        return pd.read_json(path)
    raise ValueError("Only .csv and .json are supported.")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    aliases = {
        "date": "timestamp",
        "datetime": "timestamp",
        "time": "timestamp",
        "adj close": "close",
        "adj_close": "close",
        "vol": "volume",
    }
    for old, new in aliases.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    else:
        raise ValueError("Missing timestamp/date column.")

    for c in REQUIRED:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["timestamp"] + REQUIRED)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    prev_close = df["close"].shift(1)
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)

    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_pct_of_range"] = np.where(df["range"] > 0, df["body"] / df["range"], 0.0)
    df["close_pos_in_bar"] = np.where(
        df["range"] > 0, (df["close"] - df["low"]) / df["range"], 0.5
    )

    df["bull_bar"] = df["close"] > df["open"]
    df["bear_bar"] = df["close"] < df["open"]

    df["inside_bar"] = (df["high"] <= prev_high) & (df["low"] >= prev_low)
    df["outside_bar"] = (df["high"] >= prev_high) & (df["low"] <= prev_low)
    df["gap_up"] = df["low"] > prev_high
    df["gap_down"] = df["high"] < prev_low

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["true_range"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["atr14"] = df["true_range"].rolling(14).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    overlap = np.minimum(df["high"], prev_high) - np.maximum(df["low"], prev_low)
    overlap = overlap.clip(lower=0)
    df["overlap_pct"] = np.where(df["range"] > 0, overlap / df["range"], 0.0)

    df["hh"] = df["high"] > prev_high
    df["hl"] = df["low"] > prev_low
    df["lh"] = df["high"] < prev_high
    df["ll"] = df["low"] < prev_low

    return df


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    out = (
        x.resample(rule)
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
        .reset_index()
    )
    return add_features(out)


def classify_bias(df: pd.DataFrame) -> dict:
    if len(df) < 25:
        return {"bias": "insufficient", "strength": 0.0}

    last = df.iloc[-1]
    lookback = min(5, len(df) - 1)
    ema_slope = last["ema20"] - df["ema20"].iloc[-1 - lookback]
    atr = last["atr14"] if pd.notna(last["atr14"]) and last["atr14"] > 0 else df["range"].tail(20).mean()

    close_above = float(last["close"] > last["ema20"])
    close_below = float(last["close"] < last["ema20"])
    slope_up = float(ema_slope > 0)
    slope_down = float(ema_slope < 0)
    bull_ratio = df["bull_bar"].tail(10).mean()
    bear_ratio = df["bear_bar"].tail(10).mean()

    bull_strength = (
        close_above * 30
        + slope_up * 25
        + bull_ratio * 20
        + (df["hh"].tail(10).mean() * 15)
        + (df["hl"].tail(10).mean() * 10)
    )
    bear_strength = (
        close_below * 30
        + slope_down * 25
        + bear_ratio * 20
        + (df["lh"].tail(10).mean() * 15)
        + (df["ll"].tail(10).mean() * 10)
    )

    bull_strength = clamp(bull_strength)
    bear_strength = clamp(bear_strength)

    if bull_strength >= 60 and bull_strength > bear_strength + 10:
        return {"bias": "bull", "strength": bull_strength}
    if bear_strength >= 60 and bear_strength > bull_strength + 10:
        return {"bias": "bear", "strength": bear_strength}
    return {"bias": "neutral", "strength": max(bull_strength, bear_strength)}


def mtf_alignment(daily: pd.DataFrame, weekly: pd.DataFrame, monthly: pd.DataFrame, quarterly: pd.DataFrame) -> dict:
    states = {
        "daily": classify_bias(daily),
        "weekly": classify_bias(weekly),
        "monthly": classify_bias(monthly),
        "quarterly": classify_bias(quarterly),
    }

    biases = [v["bias"] for v in states.values() if v["bias"] in {"bull", "bear", "neutral"}]
    bull_count = sum(x == "bull" for x in biases)
    bear_count = sum(x == "bear" for x in biases)

    if bull_count >= 3:
        alignment = "bull aligned"
    elif bear_count >= 3:
        alignment = "bear aligned"
    elif bull_count > 0 and bear_count > 0:
        alignment = "conflicted"
    else:
        alignment = "mixed"

    return {"alignment": alignment, "timeframes": states}


def find_pivots(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[dict]:
    pivots = []
    for i in range(left, len(df) - right):
        hi_window = df["high"].iloc[i - left : i + right + 1]
        lo_window = df["low"].iloc[i - left : i + right + 1]

        if df["high"].iloc[i] == hi_window.max():
            pivots.append(
                {
                    "index": int(i),
                    "type": "H",
                    "price": float(df["high"].iloc[i]),
                    "timestamp": str(df["timestamp"].iloc[i]),
                }
            )
        if df["low"].iloc[i] == lo_window.min():
            pivots.append(
                {
                    "index": int(i),
                    "type": "L",
                    "price": float(df["low"].iloc[i]),
                    "timestamp": str(df["timestamp"].iloc[i]),
                }
            )

    pivots = sorted(pivots, key=lambda x: x["index"])

    deduped = []
    for p in pivots:
        if not deduped:
            deduped.append(p)
            continue
        last = deduped[-1]
        if p["type"] == last["type"]:
            if p["type"] == "H" and p["price"] >= last["price"]:
                deduped[-1] = p
            elif p["type"] == "L" and p["price"] <= last["price"]:
                deduped[-1] = p
        else:
            deduped.append(p)

    return deduped


def leg_structure(df: pd.DataFrame) -> dict:
    pivots = find_pivots(df)
    recent = pivots[-12:]

    if len(recent) < 3:
        return {
            "current_direction": "unclear",
            "leg_count": 0,
            "label": "insufficient structure",
            "recent_pivots": recent,
        }

    last = recent[-1]
    direction = "up" if last["type"] == "H" else "down"

    if direction == "up":
        highs = [p for p in recent if p["type"] == "H"]
        count = 1
        for i in range(len(highs) - 1, 0, -1):
            if highs[i]["price"] > highs[i - 1]["price"]:
                count += 1
            else:
                break
        count = min(count, 3)
    else:
        lows = [p for p in recent if p["type"] == "L"]
        count = 1
        for i in range(len(lows) - 1, 0, -1):
            if lows[i]["price"] < lows[i - 1]["price"]:
                count += 1
            else:
                break
        count = min(count, 3)

    if count == 1:
        label = "first leg / first push"
    elif count == 2:
        label = "second leg / second push"
    else:
        label = "third push or wedge-like exhaustion risk"

    return {
        "current_direction": direction,
        "leg_count": int(count),
        "label": label,
        "recent_pivots": recent,
    }


def regime_scores(df: pd.DataFrame) -> dict:
    x = df.tail(30).copy()
    if len(x) < 20:
        return {
            "bull_trend_score": 0.0,
            "bear_trend_score": 0.0,
            "range_score": 0.0,
            "breakout_mode_score": 0.0,
        }

    atr = x["atr14"].dropna().tail(10).mean()
    atr = atr if pd.notna(atr) and atr > 0 else x["range"].tail(10).mean()
    ema_slope = x["ema20"].iloc[-1] - x["ema20"].iloc[0]

    bull_closes = x["bull_bar"].mean()
    bear_closes = x["bear_bar"].mean()
    overlap = x["overlap_pct"].fillna(0).mean()
    inside_ratio = x["inside_bar"].mean()
    hh_hl_ratio = ((x["hh"] & x["hl"]).mean())
    lh_ll_ratio = ((x["lh"] & x["ll"]).mean())
    close_above_ema = (x["close"] > x["ema20"]).mean()
    close_below_ema = (x["close"] < x["ema20"]).mean()

    avg_range_10 = x["range"].tail(10).mean()
    avg_range_30 = x["range"].mean()
    compression = 1.0 - min(avg_range_10 / (avg_range_30 + 1e-9), 1.0)

    bull = (
        close_above_ema * 25
        + max(0.0, ema_slope / (atr + 1e-9)) * 10
        + bull_closes * 20
        + hh_hl_ratio * 25
        + (x["close_pos_in_bar"].tail(10).mean() * 20)
        - overlap * 15
    )

    bear = (
        close_below_ema * 25
        + max(0.0, -ema_slope / (atr + 1e-9)) * 10
        + bear_closes * 20
        + lh_ll_ratio * 25
        + ((1 - x["close_pos_in_bar"].tail(10).mean()) * 20)
        - overlap * 15
    )

    range_score = (
        overlap * 35
        + inside_ratio * 20
        + (1.0 - min(abs(ema_slope) / (atr + 1e-9), 1.0)) * 20
        + (1.0 - abs(bull_closes - bear_closes)) * 15
        + (1.0 - min(abs((x["close"].iloc[-1] - x["ema20"].iloc[-1]) / (atr + 1e-9)), 1.0)) * 10
    )

    breakout_mode_score = (
        compression * 35
        + inside_ratio * 20
        + (1.0 - min((x["close"].max() - x["close"].min()) / ((avg_range_30 * 12) + 1e-9), 1.0)) * 20
        + overlap * 15
        + (1.0 - min(abs(ema_slope) / (atr + 1e-9), 1.0)) * 10
    )

    return {
        "bull_trend_score": round(clamp(bull), 1),
        "bear_trend_score": round(clamp(bear), 1),
        "range_score": round(clamp(range_score), 1),
        "breakout_mode_score": round(clamp(breakout_mode_score), 1),
    }


def key_levels(df: pd.DataFrame) -> dict:
    return {
        "recent_20_high": float(df["high"].tail(20).max()),
        "recent_20_low": float(df["low"].tail(20).min()),
        "recent_60_high": float(df["high"].tail(60).max()),
        "recent_60_low": float(df["low"].tail(60).min()),
        "all_time_high_visible": float(df["high"].max()),
        "all_time_low_visible": float(df["low"].min()),
    }


def summarize(df: pd.DataFrame) -> dict:
    daily = add_features(df)
    weekly = resample_ohlcv(daily, "W-FRI")
    monthly = resample_ohlcv(daily, "M")
    quarterly = resample_ohlcv(daily, "Q")

    last = daily.iloc[-1]

    return {
        "bars": int(len(daily)),
        "start": str(daily["timestamp"].iloc[0]),
        "end": str(daily["timestamp"].iloc[-1]),
        "last_bar": {
            "timestamp": str(last["timestamp"]),
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": float(last["close"]),
            "volume": float(last["volume"]),
            "ema20": float(last["ema20"]) if pd.notna(last["ema20"]) else None,
            "atr14": float(last["atr14"]) if pd.notna(last["atr14"]) else None,
        },
        "multi_timeframe_alignment": mtf_alignment(daily, weekly, monthly, quarterly),
        "leg_structure": leg_structure(daily),
        "regime_scores": regime_scores(daily),
        "key_levels": key_levels(daily),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/build_market_context.py <csv-or-json>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    df = load_data(path)
    df = normalize_columns(df)
    summary = summarize(df)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()