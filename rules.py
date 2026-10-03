"""
Strategy list, trading universe and itemised charges shared by the evening replay (paper-intraday/daytrade.py)
and the live engine (live/engine.py), so both always trade exactly the same rules.
"""
from pathlib import Path

import pandas as pd

import backtest_intraday as B

HERE = Path(__file__).resolve().parent

STRATS = {"india": {"ORB 15m": (B.orb, None), "ORB 15m long-only": (B.orb, "long"),
                    "VWAP reversion": (B.vwap_rev, None), "Gap fade": (B.gap_fade, None),
                    "First-hour momentum": (B.first_hour_mom, None)},
          "us": {"ORB 15m": (B.orb, None), "VWAP reversion": (B.vwap_rev, None), "Gap fade": (B.gap_fade, None),
                 "First-hour momentum": (B.first_hour_mom, None)}}


def universe(mkt):
    u = pd.read_csv(HERE / "universe.csv", index_col=0).iloc[:, 0]
    return u[mkt].split(",")


def charges(mkt, r):
    """Itemised charges for one round trip (r: a trade row from the backtester)."""
    bv, sv, sh = r["buy_val"], r["sell_val"], r["shares"]
    if mkt == "india":                                   # Angel One intraday (MIS)
        brk = min(20, 0.0003 * bv) + min(20, 0.0003 * sv)
        txn = 0.0000297 * (bv + sv); sebi = 0.000001 * (bv + sv)
        c = {"brokerage": brk, "stt": 0.00025 * sv, "txn": txn, "stamp": 0.00003 * bv, "sebi": sebi,
             "gst": 0.18 * (brk + txn + sebi)}
    else:                                                # US zero-commission broker
        c = {"sec": 27.80e-6 * sv, "taf": min(0.000166 * sh, 8.30)}
    return {k: round(v, 2) for k, v in c.items()}
