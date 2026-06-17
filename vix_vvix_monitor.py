#!/usr/bin/env python3
"""
VIX / VVIX monitor.

Pulls current VIX and VVIX levels (via Yahoo Finance, free, no API key)
and prints a plain-English read of what they mean, both individually
and in combination via the VVIX/VIX ratio.

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
"""

import argparse
import csv
import json
import os
import sys
import time
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
            with open(json_path, "w") as f:
                json.dump({"timestamp_utc": now, "error": msg}, f, indent=2)
        return

    result = interpret(vix, vvix)
    result["timestamp_utc"] = now
    result["vix_as_of"] = str(vix_ts.date())
    result["vvix_as_of"] = str(vvix_ts.date())
    result["data_note"] = (
        "Yahoo Finance daily close, not live tick data. "
        "Intended for situational awareness, not execution timing."
    )

    if not quiet:
        print(f"--- {now} ---")
        print(f"(VIX as of {vix_ts.date()}, VVIX as of {vvix_ts.date()} -- "
              f"Yahoo's index data is end-of-day/delayed, not live tick data)")
        print()
        print(format_console(result))
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
