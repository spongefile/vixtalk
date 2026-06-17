#!/usr/bin/env python3
"""
VIX / VVIX monitor.

Pulls current VIX and VVIX levels (via Yahoo Finance, free, no API key)
and prints a plain-English read of what they mean, both individually
and in combination via the VVIX/VIX ratio.

Also tracks the 2s10s Treasury curve (via FRED, free, no API key) and
classifies day-over-day moves as a bear/bull flattener/steepener -- see
the "Yield curve" section of the comments below for how that's done.

Usage:
    python3 vix_vvix_monitor.py                 # run once, print, exit
    python3 vix_vvix_monitor.py --loop 900       # poll every 900 seconds (15 min)
    python3 vix_vvix_monitor.py --log out.csv    # also append a row to a CSV log

Notes / honest limitations:
- Data source is Yahoo Finance via yfinance. This is delayed, not tick-level
  real-time, and Yahoo has been known to rate-limit or change response format
  without notice. This script is for situational awareness, not execution timing.
- The thresholds below (e.g. "VIX > 30 = high") are conventional market
  heuristics, not laws of nature. They're reasonable defaults, not predictions.
- VVIX/VIX ratio interpretation is more speculative than VIX or VVIX alone --
  it's a thinner, noisier signal. Treated as such in the output (flagged).
- The yield curve classification needs --json to persist state across runs
  (it diffs today's 2Y/10Y against whatever was in the json file from the
  last run). Without --json, you'll get today's level but no regime call.
- The curve's noise threshold (3bp) and the "average the two yield moves to
  call bear/bull" simplification are both judgment calls, not derived from
  data -- see the comments above classify_curve_move().
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency. Install with:\n  pip install yfinance --break-system-packages", file=sys.stderr)
    sys.exit(1)


# ---- Thresholds (conventional heuristics, not statistically derived) ----

VIX_BANDS = [
    (12, "very low — market is pricing in unusual calm; complacency risk"),
    (16, "low — normal/benign conditions"),
    (20, "moderate — average historical level, no particular stress"),
    (25, "elevated — market pricing meaningful uncertainty"),
    (30, "high — real fear, typically correlates with a notable equity selloff"),
    (40, "very high — crisis-level pricing (financial crisis, COVID-crash territory)"),
    (float("inf"), "extreme — historically rare, panic-level pricing"),
]

VVIX_BANDS = [
    (80, "low — options market sees little risk of VIX itself swinging hard"),
    (95, "normal — typical vol-of-vol regime"),
    (110, "elevated — meaningful uncertainty about how much VIX could move"),
    (130, "high — market pricing a real chance of a sharp vol spike"),
    (float("inf"), "very high — substantial tail-risk pricing in vol markets"),
]

def ratio_read_for(vix, vvix):
    """
    The VVIX/VIX ratio is mechanically sensitive to the VIX level itself --
    dividing by a small VIX inflates the ratio even with no real change in
    convexity demand. So this isn't a single fixed threshold; the read
    explicitly accounts for the VIX regime rather than pretending the ratio
    is regime-independent.
    """
    ratio = vvix / vix
    if vix < 14:
        # At very low VIX the ratio is structurally elevated and not very
        # informative -- flag that honestly instead of a false-precision band.
        return ratio, "not very informative at this VIX level (ratio is mechanically inflated when VIX is this low)"
    if ratio < 4.5:
        return ratio, "low — vol-of-vol cheap relative to spot fear; no particular convexity demand"
    if ratio < 6.0:
        return ratio, "normal range"
    if ratio < 7.5:
        return ratio, "elevated — some demand for protection against a vol spike beyond current fear level"
    return ratio, "high — market paying up for convexity; often seen when current calm is viewed as fragile"


def finite_thresholds(bands):
    """Extract just the finite threshold values from a bands list, for charting."""
    return [t for t, _ in bands if t != float("inf")]


def band_lookup(value, bands):
    for threshold, label in bands:
        if value < threshold:
            return label
    return bands[-1][1]


def fetch_latest(ticker_symbol):
    """Fetch the most recent close for a ticker. Returns (value, timestamp) or (None, None) on failure."""
    try:
        t = yf.Ticker(ticker_symbol)
        hist = t.history(period="5d", interval="1d")
        if hist.empty:
            return None, None
        last_row = hist.iloc[-1]
        ts = hist.index[-1].to_pydatetime()
        return float(last_row["Close"]), ts
    except Exception as e:
        print(f"  [error fetching {ticker_symbol}: {e}]", file=sys.stderr)
        return None, None


# ---- Yield curve (2s10s flattener/steepener) ----
#
# Unlike VIX/VVIX, "flattener" and "steepener" describe a CHANGE, not a level --
# you need yesterday's reading as well as today's. There's no separate state
# file for this; the script reads the existing --json output file (yesterday's
# committed data) before overwriting it, and pulls yesterday's 2Y/10Y back out
# of it. If you don't pass --json, there's nothing to diff against and the
# curve section will just report today's level with no regime call.

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
CURVE_NOISE_BP = 3.0  # spread moves smaller than this (bp) are called "no clear regime", not forced into a bucket

REGIME_TEXT = {
    "bear_flattener": (
        "Bear flattener — short-term yields rising faster than long-term yields "
        "(curve flattening, rates overall moving up). Classic late-hiking-cycle "
        "signature: the front end is dragged up by central bank policy while the "
        "long end resists because the market expects growth/inflation to cool."
    ),
    "bull_flattener": (
        "Bull flattener — long-term yields falling faster than short-term yields "
        "(curve flattening, rates overall moving down). Usually reflects the market "
        "pricing in weaker long-run growth/inflation expectations — a flight to "
        "duration — while the short end stays anchored by current policy."
    ),
    "bear_steepener": (
        "Bear steepener — long-term yields rising faster than short-term yields "
        "(curve steepening, rates overall moving up). Often shows up around fiscal "
        "worries, inflation re-acceleration fears, or term-premium repricing "
        "(investors demanding more compensation to hold long bonds)."
    ),
    "bull_steepener": (
        "Bull steepener — short-term yields falling faster than long-term yields "
        "(curve steepening, rates overall moving down). Classic early-easing-cycle "
        "signature: central bank cutting the front end while the long end holds up "
        "because growth/inflation expectations haven't shifted as much."
    ),
    "no_clear_regime": (
        "No clear regime — the spread moved less than the noise threshold. Not "
        "enough signal to call a flattener or steepener; likely just day-to-day noise."
    ),
}


def fetch_fred_yield(series_id):
    """
    Pull the latest non-null daily value from a FRED constant-maturity Treasury
    series (e.g. DGS2, DGS10) via FRED's plain CSV endpoint. No API key needed.
    Returns (value, date_str) or (None, None) on failure.
    """
    url = FRED_CSV_URL.format(series=series_id)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            text = resp.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        for date_str, val in reversed(rows[1:]):
            if val not in (".", ""):
                return float(val), date_str
        return None, None
    except Exception as e:
        print(f"  [error fetching {series_id} from FRED: {e}]", file=sys.stderr)
        return None, None


def classify_curve_move(prev_2y, prev_10y, curr_2y, curr_10y, noise_bp=CURVE_NOISE_BP):
    """
    Returns (regime, d2_bp, d10_bp, spread_change_bp).
    regime is one of: bear_flattener, bull_flattener, bear_steepener,
    bull_steepener, no_clear_regime.

    "bear" = average of the two yield moves is upward (rates rising overall,
    bad for bond prices). "bull" = average is downward. "Flattener" = the
    10Y-2Y spread narrowed; "steepener" = it widened. This averaging is a
    simplification -- in a genuinely mixed move (e.g. 2Y up 5bp, 10Y down 2bp)
    it can mask a real divergence, so the raw d2/d10 figures are surfaced
    alongside the label rather than hidden behind it.
    """
    d2 = (curr_2y - prev_2y) * 100
    d10 = (curr_10y - prev_10y) * 100
    spread_now = curr_10y - curr_2y
    spread_prev = prev_10y - prev_2y
    spread_change_bp = (spread_now - spread_prev) * 100

    if abs(spread_change_bp) < noise_bp:
        return "no_clear_regime", d2, d10, spread_change_bp

    flattening = spread_change_bp < 0
    rising = (d2 + d10) / 2 > 0

    if flattening and rising:
        regime = "bear_flattener"
    elif flattening and not rising:
        regime = "bull_flattener"
    elif not flattening and rising:
        regime = "bear_steepener"
    else:
        regime = "bull_steepener"

    return regime, d2, d10, spread_change_bp


def load_previous_curve(json_path):
    """Read yesterday's 2Y/10Y back out of the existing --json output file, if any."""
    if not json_path or not os.path.isfile(json_path):
        return None
    try:
        with open(json_path) as f:
            prev = json.load(f)
        curve = prev.get("curve")
        if not curve or not curve.get("available"):
            return None
        return {
            "y2": curve["y2"], "y2_as_of": curve["y2_as_of"],
            "y10": curve["y10"], "y10_as_of": curve["y10_as_of"],
        }
    except Exception:
        return None


def build_curve_result(json_path):
    """Fetch current 2Y/10Y, diff against yesterday's stored reading if available."""
    y2, y2_date = fetch_fred_yield("DGS2")
    y10, y10_date = fetch_fred_yield("DGS10")

    if y2 is None or y10 is None:
        return {"available": False, "error": "Could not fetch 2Y/10Y Treasury yields from FRED."}

    spread_bp = (y10 - y2) * 100
    result = {
        "available": True,
        "y2": round(y2, 2), "y2_as_of": y2_date,
        "y10": round(y10, 2), "y10_as_of": y10_date,
        "spread_bp": round(spread_bp, 0),
        "inverted": spread_bp < 0,
    }

    prev = load_previous_curve(json_path)
    if prev is None:
        result["prev_available"] = False
        result["regime_text"] = (
            "No previous reading on file yet — can't classify a flattener/steepener "
            "move until there's a second data point to compare against. Will be "
            "available from the next update onward."
        )
        return result

    regime, d2, d10, spread_change_bp = classify_curve_move(prev["y2"], prev["y10"], y2, y10)
    result["prev_available"] = True
    result["since_date"] = prev["y2_as_of"]
    result["d2_bp"] = round(d2, 0)
    result["d10_bp"] = round(d10, 0)
    result["spread_change_bp"] = round(spread_change_bp, 0)
    result["regime"] = regime
    result["regime_text"] = REGIME_TEXT[regime]
    return result


def interpret(vix, vvix):
    """Build the interpretation as structured data (used for both console and JSON output)."""
    vix_read = band_lookup(vix, VIX_BANDS)
    vvix_read = band_lookup(vvix, VVIX_BANDS)
    ratio, ratio_read = ratio_read_for(vix, vvix)

    vix_low = vix < 16
    vix_high = vix > 25
    vvix_low = vvix < 90
    vvix_high = vvix > 110

    if vix_low and vvix_high:
        combined = (
            "Spot fear is low but vol-of-vol is elevated. This is the classic "
            "'calm that doesn't feel earned' pattern -- the market isn't currently "
            "scared, but options pricing suggests real uncertainty about whether "
            "that calm holds. Worth treating as a caution flag rather than "
            "confirmation that conditions are stable."
        )
    elif vix_high and vvix_low:
        combined = (
            "Spot fear is high but vol-of-vol is low. This suggests the market "
            "believes the current stress is well-defined and unlikely to spiral "
            "further -- fear without much uncertainty about the fear itself. "
            "Sometimes seen after a known catalyst has already been priced in."
        )
    elif vix_high and vvix_high:
        combined = (
            "Both spot fear and vol-of-vol are elevated. Genuine stress with "
            "genuine uncertainty about how much worse it could get -- the more "
            "dangerous combination, since it implies the market itself doesn't "
            "have a confident read on the range of outcomes."
        )
    elif vix_low and vvix_low:
        combined = (
            "Both spot fear and vol-of-vol are low. Genuinely calm by both "
            "measures -- no particular reason from these two indicators alone "
            "to expect a near-term vol event."
        )
    else:
        combined = "No strong signal either way -- both measures are in unremarkable middle ranges."

    return {
        "vix": round(vix, 2),
        "vix_read": vix_read,
        "vix_thresholds": finite_thresholds(VIX_BANDS),
        "vvix": round(vvix, 2),
        "vvix_read": vvix_read,
        "vvix_thresholds": finite_thresholds(VVIX_BANDS),
        "ratio": round(ratio, 2),
        "ratio_read": ratio_read,
        "combined_read": combined,
    }


def format_console(result):
    lines = [
        f"VIX  = {result['vix']:.2f}  -> {result['vix_read']}",
        f"VVIX = {result['vvix']:.2f}  -> {result['vvix_read']}",
        f"VVIX/VIX ratio = {result['ratio']:.2f}  -> {result['ratio_read']}  [noisier signal, treat as secondary]",
        "",
        f"Combined read: {result['combined_read']}",
    ]
    return "\n".join(lines)


def format_curve_console(curve):
    if not curve.get("available"):
        return f"Yield curve: unavailable -- {curve.get('error', 'unknown error')}"

    lines = [
        f"2Y Treasury  = {curve['y2']:.2f}% (as of {curve['y2_as_of']})",
        f"10Y Treasury = {curve['y10']:.2f}% (as of {curve['y10_as_of']})",
        f"2s10s spread = {curve['spread_bp']:+.0f}bp ({'inverted' if curve['inverted'] else 'normal/positive'})",
    ]
    if not curve.get("prev_available"):
        lines.append("")
        lines.append(curve["regime_text"])
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Since {curve['since_date']}: 2Y {curve['d2_bp']:+.0f}bp, "
                  f"10Y {curve['d10_bp']:+.0f}bp, spread {curve['spread_change_bp']:+.0f}bp")
    lines.append("")
    lines.append(curve["regime_text"])
    return "\n".join(lines)


def run_once(log_path=None, json_path=None, quiet=False):
    vix, vix_ts = fetch_latest("^VIX")
    vvix, vvix_ts = fetch_latest("^VVIX")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if vix is None or vvix is None:
        msg = "Could not fetch one or both series. Yahoo may be rate-limiting or unreachable."
        if not quiet:
            print(f"--- {now} ---")
            print(msg)
        if json_path:
            # Still try the curve independently -- FRED and Yahoo are unrelated
            # data sources. Compute this BEFORE opening json_path for writing:
            # open(..., "w") truncates immediately, so doing this inline inside
            # the dict literal below would read back an empty/just-truncated file.
            curve_data = build_curve_result(json_path)
            with open(json_path, "w") as f:
                json.dump({"timestamp_utc": now, "error": msg, "curve": curve_data}, f, indent=2)
        return

    result = interpret(vix, vvix)
    result["timestamp_utc"] = now
    result["vix_as_of"] = str(vix_ts.date())
    result["vvix_as_of"] = str(vvix_ts.date())
    result["data_note"] = (
        "Yahoo Finance daily close, not live tick data. "
        "Intended for situational awareness, not execution timing."
    )

    # Curve diff needs yesterday's values, which live in the existing json_path
    # file (if any) -- read it BEFORE it gets overwritten below.
    result["curve"] = build_curve_result(json_path)

    if not quiet:
        print(f"--- {now} ---")
        print(f"(VIX as of {vix_ts.date()}, VVIX as of {vvix_ts.date()} -- "
              f"Yahoo's index data is end-of-day/delayed, not live tick data)")
        print()
        print(format_console(result))
        print()
        print(format_curve_console(result["curve"]))
        print()

    if log_path:
        file_exists = os.path.isfile(log_path)
        with open(log_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp_utc", "vix", "vvix", "vvix_vix_ratio"])
            writer.writerow([now, f"{vix:.2f}", f"{vvix:.2f}", f"{result['ratio']:.2f}"])

    if json_path:
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Monitor VIX and VVIX with plain-English interpretation.")
    parser.add_argument("--loop", type=int, default=0,
                         help="Poll repeatedly every N seconds. Omit to run once and exit.")
    parser.add_argument("--log", type=str, default=None,
                         help="Path to a CSV file to append each reading to.")
    parser.add_argument("--json", type=str, default=None,
                         help="Path to a JSON file to write the latest reading to (overwritten each run). "
                              "Use this to feed a static website.")
    parser.add_argument("--quiet", action="store_true",
                         help="Suppress console output (useful when only writing --json, e.g. in a cron job).")
    args = parser.parse_args()

    if args.loop <= 0:
        run_once(log_path=args.log, json_path=args.json, quiet=args.quiet)
        return

    print(f"Polling every {args.loop} seconds. Ctrl+C to stop.\n")
    try:
        while True:
            run_once(log_path=args.log, json_path=args.json, quiet=args.quiet)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
