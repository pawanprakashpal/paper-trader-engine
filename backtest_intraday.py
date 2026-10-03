"""
Day-trading backtest on 5-minute bars (last ~60 sessions), every position closed the same day.

Execution model (no look-ahead):
  - A signal on a bar's close enters at the NEXT bar's open, +/- slippage.
  - Stops trigger inside a bar when its low/high touches the stop (filled at the stop, or at the open if it gapped
    through), minus slippage. Profit targets are limit orders (no slippage). Anything open is closed at the
    square-off bar: India 15:10 bar (Angel One auto square-off ~15:15), US 15:50 bar.
  - Capital, no leverage: India capital split into up to 10 equal trades per day (first 10 signals by time);
    US cash account (no PDT limit) split into up to 3 equal trades per day; US is long-only.
Costs:
  - India intraday (Angel One): brokerage Rs20 or 0.03% per order (lower), STT 0.025% on the sell side, NSE txn
    0.00297% both sides, stamp 0.003% on the buy, SEBI 0.0001%, GST 18% on brokerage+txn+SEBI; slippage 0.03%/side.
  - US (zero-commission broker): $0 commission, SEC fee $27.80/$1M sold, FINRA TAF $0.000166/share sold; slippage 0.02%/side.
"""
import numpy as np, pandas as pd

MKT = {
    "india": {"cur": "Rs ", "capital": 10e5, "per_trade": 1e5, "max_trades": 10, "slip": 0.0003, "shorts": True,
              "squareoff": "15:10", "entry_cutoff": "14:30", "first_bar": "09:15"},
    "us": {"cur": "$", "capital": 10000, "per_trade": 3333, "max_trades": 3, "slip": 0.0002, "shorts": False,
           "squareoff": "15:50", "entry_cutoff": "15:00", "first_bar": "09:30"},
}


def costs(mkt, buy_val, sell_val, shares):
    if mkt == "india":
        brk = min(20, 0.0003 * buy_val) + min(20, 0.0003 * sell_val)
        txn = 0.0000297 * (buy_val + sell_val); sebi = 0.000001 * (buy_val + sell_val)
        return brk + 0.00025 * sell_val + txn + 0.00003 * buy_val + sebi + 0.18 * (brk + txn + sebi)
    return 27.80e-6 * sell_val + min(0.000166 * shares, 8.30)


def sessions(bars):
    """Yield (date, {ticker: day DataFrame}) with timestamps as local HH:MM strings."""
    c = bars["Close"]
    for day, idx in c.groupby(c.index.normalize()).groups.items():
        yield day, idx


# ---------------------------------------------------------------- signal rules
# each rule(df, ctx) -> list of (entry_bar_index, side, stop, target) ; df has columns O,H,L,C,V,t (HH:MM)
def orb(df, ctx, or_bars=3):
    hi, lo = df.H.iloc[:or_bars].max(), df.L.iloc[:or_bars].min()
    for i in range(or_bars, len(df) - 1):
        if df.t.iloc[i] > ctx["entry_cutoff"]: break
        if df.C.iloc[i] > hi: return [(i + 1, 1, lo, None)]
        if ctx["shorts"] and df.C.iloc[i] < lo: return [(i + 1, -1, hi, None)]
    return []


def vwap_rev(df, ctx, k=0.006, stop=0.015):
    tp = (df.H + df.L + df.C) / 3
    vw = (tp * df.V).cumsum() / df.V.cumsum().replace(0, np.nan)
    for i in range(len(df) - 1):
        if df.t.iloc[i] < "10:00": continue
        if df.t.iloc[i] > ctx["entry_cutoff"]: break
        if df.C.iloc[i] <= vw.iloc[i] * (1 - k):
            e = df.O.iloc[i + 1]
            return [(i + 1, 1, e * (1 - stop), ("vwap", vw))]
    return []


def gap_fade(df, ctx, gap=-0.01, stop=0.015):
    pc = ctx["prev_close"]
    if pc is None or len(df) < 3: return []
    if df.O.iloc[0] / pc - 1 <= gap:
        e = df.O.iloc[1]
        return [(1, 1, e * (1 - stop), pc)]
    return []


def first_hour_mom(df, ctx, thr=0.005):
    n = 12                                              # 12 x 5-min bars = first hour
    if len(df) <= n + 1: return []
    if df.C.iloc[n - 1] / df.O.iloc[0] - 1 > thr:
        return [(n, 1, df.L.iloc[:n].min(), None)]
    if ctx["shorts"] and df.C.iloc[n - 1] / df.O.iloc[0] - 1 < -thr:
        return [(n, -1, df.H.iloc[:n].max(), None)]
    return []


RULES = {"ORB 15m": orb, "VWAP reversion": vwap_rev, "Gap fade": gap_fade, "First-hour momentum": first_hour_mom}


# ---------------------------------------------------------------- simulate one trade
def run_trade(df, ctx, i0, side, stop, target, partial=False):
    """partial=True (live use): the day isn't over yet, so a trade with no stop/target hit stays 'open' at the last close."""
    slip = ctx["slip"]
    e = df.O.iloc[i0] * (1 + slip * side)
    so = df.index[df.t <= ctx["squareoff"]]
    last = df.index.get_loc(so[-1]) if len(so) else len(df) - 1
    for i in range(i0, last + 1):
        o, h, l = df.O.iloc[i], df.H.iloc[i], df.L.iloc[i]
        # stop first (conservative when both stop and target are inside one bar)
        if side == 1 and l <= stop:
            return e, min(o, stop) * (1 - slip), i, "stop"
        if side == -1 and h >= stop:
            return e, max(o, stop) * (1 + slip), i, "stop"
        tgt = target[1].iloc[i] if isinstance(target, tuple) else target
        if tgt is not None and i > i0 and not np.isnan(tgt):
            if side == 1 and h >= tgt: return e, max(o, tgt), i, "target"
    if partial and df.t.iloc[-1] < ctx["squareoff"]:
        return e, df.C.iloc[-1], len(df) - 1, "open"            # marked, not exited (no exit slippage yet)
    return e, df.C.iloc[last] * (1 - slip * side), last, "close"


def backtest(mkt, bars, rule, min_bars=20, partial=False, **kw):
    ctx0 = MKT[mkt]
    O, H, L, C, V = (bars[f] for f in ["Open", "High", "Low", "Close", "Volume"])
    tz_idx = C.index
    times = tz_idx.strftime("%H:%M")
    prev_close = {}
    trades = []
    for day, idx in sessions(bars):
        cands = []
        for tk in C.columns:
            df = pd.DataFrame({"O": O.loc[idx, tk], "H": H.loc[idx, tk], "L": L.loc[idx, tk], "C": C.loc[idx, tk],
                               "V": V.loc[idx, tk]}).dropna()
            if len(df) < min_bars:                      # incomplete session
                continue
            df["t"] = df.index.strftime("%H:%M")
            ctx = {**ctx0, "prev_close": prev_close.get(tk)}
            for i0, side, stop, target in rule(df, ctx, **kw):
                cands.append((df.index[i0], tk, df, ctx, i0, side, stop, target))
            prev_close[tk] = df.C.iloc[-1]
        for when, tk, df, ctx, i0, side, stop, target in sorted(cands, key=lambda x: x[0])[:ctx0["max_trades"]]:
            e, x, ix, why = run_trade(df, ctx, i0, side, stop, target, partial)
            sh = ctx0["per_trade"] / e
            buy_v, sell_v = (e * sh, x * sh) if side == 1 else (x * sh, e * sh)
            gross = (x - e) * sh * side
            cost = costs(mkt, buy_v, sell_v, sh)
            trades.append({"date": day.date(), "ticker": tk, "side": "long" if side == 1 else "short",
                           "entry_time": df.t.iloc[i0], "exit_time": df.t.iloc[ix], "exit": why,
                           "entry_px": e, "exit_px": x, "shares": sh, "buy_val": buy_v, "sell_val": sell_v,
                           "gross": gross, "costs": cost, "net": gross - cost,
                           "net_pct": (gross - cost) / ctx0["per_trade"] * 100})
    return pd.DataFrame(trades)


def summarize(mkt, t, n_sessions):
    cur = MKT[mkt]["cur"]
    if t.empty: return {"trades": 0}
    daily = t.groupby("date")["net"].sum()
    daily = daily.reindex(sorted(set(daily.index)), fill_value=0)
    eq = daily.cumsum()
    half = sorted(t["date"].unique())[len(t["date"].unique()) // 2]
    return {"trades": len(t), "days traded": f"{t['date'].nunique()}/{n_sessions}", "win %": round((t["net"] > 0).mean() * 100, 1),
            "avg net/trade %": round(t["net_pct"].mean(), 3), "gross": f"{cur}{t['gross'].sum():,.0f}",
            "costs": f"{cur}{t['costs'].sum():,.0f}", "NET": f"{cur}{t['net'].sum():,.0f}",
            "net % of capital": round(t["net"].sum() / MKT[mkt]["capital"] * 100, 2),
            "max DD": f"{cur}{(eq - eq.cummax()).min():,.0f}",
            "1st half": f"{cur}{t[t['date'] < half]['net'].sum():,.0f}", "2nd half": f"{cur}{t[t['date'] >= half]['net'].sum():,.0f}",
            "daily Sharpe": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() > 0 else np.nan}


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    all_trades = []
    for mkt in ("india", "us"):
        bars = pd.read_pickle(f"data/{mkt}_5m.pkl")
        n = bars["Close"].index.normalize().nunique()
        rows = {}
        for name, rule in RULES.items():
            t = backtest(mkt, bars, rule)
            rows[name] = summarize(mkt, t, n)
            if not t.empty:
                all_trades.append(t.assign(market=mkt, strategy=name))
                if mkt == "india" and (t["side"] == "short").any():
                    for sd in ("long", "short"):
                        rows[f"{name} [{sd} only]"] = summarize(mkt, t[t["side"] == sd], n)
        print(f"\n=== {mkt.upper()} — 5-minute bars, {n} sessions, capital {MKT[mkt]['cur']}{MKT[mkt]['capital']:,.0f} ===")
        print(pd.DataFrame(rows).T.to_string())
    pd.concat(all_trades).to_csv("trades_5m.csv", index=False)
