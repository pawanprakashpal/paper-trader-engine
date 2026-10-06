"""
Live paper day-trading engine (learning exercise, NO REAL MONEY, never places orders anywhere).

Run by cron every 5 minutes (1 minute after each 5-minute bar closes). Each run is short and stateless in spirit:
  1. fetch today's completed 5-minute bars (+ the previous session) from the market's data source,
  2. run EXACTLY the research rules (research-intraday/backtest_intraday.py, partial-day mode) on them,
  3. diff against what was already announced -> Telegram alerts for new entries / exits,
  4. write live.json (for the Vercel page) and, after square-off, the day's final file.
Because every run recomputes the whole day from the bars, a missed cron run loses nothing.

    python live/engine.py india            # one step, real data (needs live/.env)
    python live/engine.py us
    python live/engine.py india --replay 2026-10-01   # simulate a past day on a fake clock (testing)

Data: India = Angel One SmartAPI historical candles (login with client code + PIN + TOTP, Historical Data API key).
      US = Alpaca market data (IEX feed, free), bars endpoint. Credentials: live/.env (never committed).
"""
import base64, hashlib, hmac, json, os, struct, sys, time
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
import backtest_intraday as B      # noqa: E402
import rules as DT                 # noqa: E402  (strategy list, charges, universe: same as the evening replay)

TZ = {"india": "Asia/Kolkata", "us": "America/New_York"}
SESSION = {"india": ("09:15", "15:30"), "us": ("09:30", "16:00")}
OUT = Path(os.environ.get("LIVE_OUT", HERE / "out"))          # where live.json / days/*.json go (web folder on cPanel)
STATE = HERE / "state"
CUR = {"india": "₹", "us": "$"}
# Exchange holidays (weekdays only) so a scheduled run exits at once instead of waiting for data that never comes.
HOLIDAYS = {"india": {"2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"},
            "us": {"2026-11-26", "2026-12-25"}}


# ---------------------------------------------------------------- config
def env():
    """Read live/.env (KEY=VALUE lines). Values are never printed."""
    e, p = dict(os.environ), HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return e


def totp(secret_b32, at=None):
    """RFC 6238 TOTP (30 s, 6 digits, SHA1), as Angel One's authenticator setup uses."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    counter = int((at or time.time()) // 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


# ---------------------------------------------------------------- data adapters -> {"Open": df, ..., "Volume": df}, tz-aware local index
def _frame(rows, tz):
    """rows: list of (ticker, timestamp, o, h, l, c, v) -> yfinance-like dict of DataFrames."""
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["t", "ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(tz)
    return {f: df.pivot_table(index="ts", columns="t", values=f, aggfunc="last").sort_index()
            for f in ["Open", "High", "Low", "Close", "Volume"]}


class AngelData:
    ROOT = "https://apiconnect.angelone.in"
    MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state

    def _headers(self, jwt=None):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": self.cfg.get("ANGEL_PUBLIC_IP", "127.0.0.1"),
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.cfg["ANGEL_HIST_API_KEY"]}
        if jwt: h["Authorization"] = f"Bearer {jwt}"
        return h

    def _jwt(self, today):
        a = self.state.setdefault("angel", {})
        if a.get("day") == today and a.get("jwt"):
            return a["jwt"]
        r = requests.post(self.ROOT + "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(), timeout=30,
                          json={"clientcode": self.cfg["ANGEL_CLIENT_CODE"], "password": self.cfg["ANGEL_PIN"],
                                "totp": totp(self.cfg["ANGEL_TOTP_SECRET"])}).json()
        if not r.get("status"):
            raise RuntimeError(f"Angel One login failed: {r.get('message')} ({r.get('errorcode')})")
        a.update(day=today, jwt=r["data"]["jwtToken"])
        return a["jwt"]

    def _tokens(self, tickers):
        p = STATE / "angel_tokens.json"
        tok = json.loads(p.read_text()) if p.exists() else {}
        need = [t for t in tickers if t not in tok]
        if need:
            master = requests.get(self.MASTER, timeout=120).json()
            by = {(m["symbol"], m["exch_seg"]): m["token"] for m in master if m.get("exch_seg") == "NSE"}
            for t in need:
                sym = t.replace(".NS", "")
                tok[t] = by.get((f"{sym}-EQ", "NSE")) or by.get((sym, "NSE"))
            p.write_text(json.dumps(tok, indent=1))
        return tok

    def bars(self, tickers, now):
        today = f"{now:%Y-%m-%d}"
        jwt, tok = self._jwt(today), self._tokens(tickers)
        frm = (now - pd.Timedelta(days=6)).strftime("%Y-%m-%d 09:15")
        rows = []
        for t in tickers:
            if not tok.get(t): continue
            resp = requests.post(self.ROOT + "/rest/secure/angelbroking/historical/v1/getCandleData",
                                 headers=self._headers(jwt), timeout=30,
                                 json={"exchange": "NSE", "symboltoken": tok[t], "interval": "FIVE_MINUTE",
                                       "fromdate": frm, "todate": now.strftime("%Y-%m-%d %H:%M")})
            try:
                r = resp.json()
            except ValueError:                                 # Angel sometimes returns HTML/empty on brief outages or rate-limits
                raise RuntimeError(f"Angel candle API non-JSON (status {resp.status_code}): {resp.text[:200]!r}")
            for ts, o, h, l, c, v in (r.get("data") or []):
                rows.append((t, ts, o, h, l, c, v))
            time.sleep(0.35)                                   # historical API allows ~3 requests/second
        return _frame(rows, TZ["india"])


class AlpacaData:
    URL = "https://data.alpaca.markets/v2/stocks/bars"

    def __init__(self, cfg, state):
        self.h = {"APCA-API-KEY-ID": cfg["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": cfg["ALPACA_SECRET"]}

    def bars(self, tickers, now):
        params = {"symbols": ",".join(tickers), "timeframe": "5Min", "feed": "iex", "adjustment": "raw", "limit": 10000,
                  "start": (now - pd.Timedelta(days=6)).tz_convert("UTC").isoformat(), "end": now.tz_convert("UTC").isoformat()}
        rows, page = [], None
        while True:
            if page: params["page_token"] = page
            r = requests.get(self.URL, headers=self.h, params=params, timeout=30).json()
            for t, bs in (r.get("bars") or {}).items():
                rows += [(t, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bs]
            page = r.get("next_page_token")
            if not page: break
        return _frame(rows, TZ["us"])


class ReplayData:
    """Recorded yfinance 5-minute bars, revealed bar by bar on a simulated clock (for testing)."""
    def __init__(self, mkt):
        import yfinance as yf
        raw = yf.download(DT.universe(mkt), period="10d", interval="5m", auto_adjust=True, progress=False, group_by="column")
        self.b = {f: raw[f] for f in ["Open", "High", "Low", "Close", "Volume"]}

    def bars(self, tickers, now):
        return {f: v.loc[v.index <= now] for f, v in self.b.items()}


# ---------------------------------------------------------------- one engine step
def completed(bars, now):
    """Keep only bars that have finished (start + 5 min <= now); some feeds include the forming bar."""
    keep = bars["Close"].index + pd.Timedelta(minutes=5) <= now
    return {f: v.loc[keep] for f, v in bars.items()}


def day_trades(mkt, bars, day):
    out = []
    for name, (rule, only) in DT.STRATS[mkt].items():
        t = B.backtest(mkt, bars, rule, min_bars=3, partial=True)
        if t.empty: continue
        t = t[pd.to_datetime(t["date"]) == day]
        if only: t = t[t["side"] == only]
        for r in t.to_dict("records"):
            if mkt == "india":                                # Angel One: whole shares only
                k = int(r["shares"]) / r["shares"]
                for f in ("shares", "gross", "buy_val", "sell_val"): r[f] *= k
            ch = DT.charges(mkt, r)
            out.append({"strategy": name, "ticker": r["ticker"].replace(".NS", ""), "side": r["side"],
                        "qty": round(r["shares"], 4), "entry_time": r["entry_time"], "entry_px": round(r["entry_px"], 2),
                        "status": "open" if r["exit"] == "open" else "closed", "exit": r["exit"],
                        "exit_time": r["exit_time"], "exit_px": round(r["exit_px"], 2),
                        "gross": round(r["gross"], 2), "charges": round(sum(ch.values()), 2),
                        "net": round(r["gross"] - sum(ch.values()), 2)})
    return out


def telegram(cfg, text):
    """Send an alert. Never raises (an alert failing must never stop the engine), but logs Telegram's own error."""
    tok = (cfg.get("TELEGRAM_BOT_TOKEN") or "").strip().strip('"').strip("'").strip("<>")
    chat = (cfg.get("TELEGRAM_CHAT_ID") or "").strip().strip('"').strip("'")
    import re
    m = re.search(r"(\d{6,}:[A-Za-z0-9_-]{30,})", tok)         # the token inside whatever was pasted (bare, "bot..." or a full URL)
    tok = m.group(1) if m else tok
    if not tok or not chat:
        print("  (Telegram not configured: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)"); return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                          data={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not j.get("ok"):
            # Telegram's description only, e.g. "Unauthorized" or "Bad Request: chat not found" (never the token)
            print(f"  (Telegram alert failed: HTTP {r.status_code} {j.get('description', '')})"); return False
        return True
    except requests.RequestException as e:
        print(f"  (Telegram alert failed: {type(e).__name__})"); return False


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def step(mkt, now, data, cfg, alert=True):
    cur = CUR[mkt]
    day = now.tz_localize(None).normalize() if now.tzinfo else now.normalize()
    key = f"{day:%Y-%m-%d}"
    start, end = SESSION[mkt]
    hm = now.strftime("%H:%M")
    if now.dayofweek >= 5 or hm < start or hm > "16:10":
        return "outside session"
    sp = STATE / f"{mkt}.json"
    st = json.loads(sp.read_text()) if sp.exists() else {}
    if st.get("day") != key:
        st = {"day": key, "sent": [], "angel": st.get("angel", {}), "final": False}
    if st["final"]:
        return "day already final"
    if isinstance(data, AngelData): data.state = st
    bars = data.bars(DT.universe(mkt), now)
    if bars is None or bars["Close"].empty:
        return "no bars (holiday or data source down)"
    bars = completed(bars, now)
    days = bars["Close"].index.normalize().tz_localize(None)
    if not (days == day).any():
        return "no bars for today yet"
    keep_days = sorted(set(days[days <= day]))[-2:]               # today + the previous session (gap fade needs its close)
    bars = {f: v.loc[days.isin(keep_days)] for f, v in bars.items()}
    trades = day_trades(mkt, bars, day)
    last_bar = bars["Close"].index.max()

    # alerts: one per new entry and per new exit
    msgs = []
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        k_in = f"in|{t['strategy']}|{t['ticker']}|{t['entry_time']}"
        if k_in not in st["sent"]:
            st["sent"].append(k_in)
            msgs.append(f"🟢 <b>{t['strategy']}</b> {'BUY' if t['side'] == 'long' else 'SHORT'} {t['qty']:,.4g} "
                        f"{t['ticker']} @ {t['entry_px']:,.2f} ({t['entry_time']})")
        if t["status"] == "closed":
            k_out = f"out|{t['strategy']}|{t['ticker']}|{t['entry_time']}"
            if k_out not in st["sent"]:
                st["sent"].append(k_out)
                icon = "✅" if t["net"] > 0 else "🔴"
                msgs.append(f"{icon} <b>{t['strategy']}</b> exit {t['ticker']} ({t['exit']}) @ {t['exit_px']:,.2f} "
                            f"({t['exit_time']}) · net {cur}{t['net']:+,.0f}")

    # live snapshot for the page
    strategies = []
    for name in DT.STRATS[mkt]:
        mine = [t for t in trades if t["strategy"] == name]
        strategies.append({"name": name, "trades": len(mine),
                           "realized": round(sum(t["net"] for t in mine if t["status"] == "closed"), 2),
                           "unrealized": round(sum(t["net"] for t in mine if t["status"] == "open"), 2),
                           "open": [t for t in mine if t["status"] == "open"],
                           "closed": [t for t in mine if t["status"] == "closed"]})
    # final once the square-off bar (India 15:10, US 15:50) has completed and nothing is open
    final = last_bar.strftime("%H:%M") >= ("15:10" if mkt == "india" else "15:50") and all(t["status"] == "closed" for t in trades)
    snap = {"market": mkt, "currency": cur, "session": key, "updated": now.isoformat(), "last_bar": last_bar.isoformat(),
            "final": final, "paper_only": True, "strategies": strategies}
    write_json(OUT / f"live-{mkt}.json", snap)
    if final:
        write_json(OUT / "days" / f"{key}-{mkt}.json", snap)
        st["final"] = True
        day_net = sum(s["realized"] for s in strategies)
        msgs.append(f"🏁 <b>{'NSE' if mkt == 'india' else 'US'} day trading {key} (paper)</b>\n" + "\n".join(
            f"{s['name']}: {s['trades']} trades, {cur}{s['realized']:+,.0f}" for s in strategies) + f"\nTotal {cur}{day_net:+,.0f}")
    if alert and msgs:
        telegram(cfg, "\n".join(msgs))
    write_json(sp, st)
    return f"{key} {hm}: {len(trades)} trades, {len(msgs)} new alerts" + (" (FINAL)" if final else "")


def upload(cfg, mkt, final_key=None):
    """Push live-<mkt>.json (and the final day file) to the paper.* subdomain over FTPS, if configured."""
    if not cfg.get("FTP_HOST"): return
    from ftplib import FTP_TLS
    files = [OUT / f"live-{mkt}.json"] + ([OUT / "days" / final_key] if final_key else [])
    try:
        with FTP_TLS(cfg["FTP_HOST"], timeout=30) as ftp:
            ftp.login(cfg["FTP_USER"], cfg["FTP_PASS"]); ftp.prot_p()
            base = cfg.get("FTP_DIR", "").strip("/")
            import io as _io                                    # let the Vercel page read these files (CORS)
            ftp.storbinary(f"STOR {'/'.join(x for x in [base, '.htaccess'] if x)}",
                           _io.BytesIO(b'Header set Access-Control-Allow-Origin "*"\nHeader set Cache-Control "no-store"\n'))
            for f in files:
                if not f.exists(): continue
                remote = "/".join(x for x in [base, "days" if f.parent.name == "days" else "", f.name] if x)
                if f.parent.name == "days":
                    try: ftp.mkd("/".join(x for x in [base, "days"] if x))
                    except Exception: pass
                with open(f, "rb") as fh: ftp.storbinary(f"STOR {remote}", fh)
    except Exception as e:                                     # the page going stale must never stop the engine
        print(f"  (upload failed: {type(e).__name__}: {e})")


def scrub_state(mkt):
    """Drop the broker session token before the state file leaves this machine/job (it is passed between jobs)."""
    p = STATE / f"{mkt}.json"
    if p.exists():
        st = json.loads(p.read_text()); st.pop("angel", None); write_json(p, st)


def watch(mkt, cfg, until=None, first=True):
    """Manual mode: started by the user, runs one step a minute after each 5-minute bar closes, stops after square-off.
    until="HH:MM" (market time) hands over early (GitHub job limit); first=False skips the start alert (second job)."""
    tz = TZ[mkt]
    now = pd.Timestamp.now(tz=tz)
    print(f"Live paper day trading ({mkt}) started {now:%Y-%m-%d %H:%M %Z}. Ctrl+C to stop.")
    # fail loudly (non-zero exit -> red run, second job skipped) instead of quietly doing nothing
    need = {"india": ["ANGEL_HIST_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"],
            "us": ["ALPACA_KEY_ID", "ALPACA_SECRET"]}[mkt]
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        print(f"Missing secrets for {mkt}: {', '.join(missing)}  (repo Settings -> Secrets and variables -> Actions)")
        sys.exit(2)
    if now.dayofweek >= 5 or f"{now:%Y-%m-%d}" in HOLIDAYS[mkt]:
        print("The market is closed today (weekend/holiday). Nothing to do.")
        sys.exit(3 if os.environ.get("GITHUB_EVENT_NAME") != "schedule" else 0)
    if until:   # GitHub morning job: it must reach `until` within the ~6-hour job limit
        earliest = now.normalize() + pd.Timedelta(until + ":00") - pd.Timedelta(minutes=345)
        if now < earliest:
            print(f"Too early: start at or after {earliest:%H:%M %Z} "
                  f"({earliest.tz_convert('America/Chicago'):%I:%M %p} Central) so the job doesn't hit GitHub's time limit.")
            sys.exit(3)
    data = AngelData(cfg, {}) if mkt == "india" else AlpacaData(cfg, {})
    errors = 0
    if mkt == "india":                      # check the Angel One login now, not at the first bar
        try:
            data._jwt(f"{now:%Y-%m-%d}")
            print("Angel One login OK.")
            login_note = "Angel One login OK ✅"
        except Exception as e:
            print(f"Angel One login FAILED: {e}")
            telegram(cfg, f"⚠️ Live paper engine (NSE): Angel One login failed: {str(e)[:200]}")
            sys.exit(4)
    else:
        login_note = "Alpaca data"
    if first:
        telegram(cfg, f"▶️ Live paper day trading started ({'NSE' if mkt == 'india' else 'US'}) · {login_note}")
    while True:
        now = pd.Timestamp.now(tz=tz)
        if until and now.strftime("%H:%M") >= until:
            scrub_state(mkt)
            print(f"Reached {until}; handing over to the next job."); return
        nxt = now.floor("5min") + pd.Timedelta(minutes=6)       # 1 minute after the next bar closes
        if now.strftime("%H:%M") < SESSION[mkt][0]:
            print(f"  waiting for the open ({SESSION[mkt][0]} {tz})")
        else:
            try:
                msg = step(mkt, now, data, cfg); errors = 0
            except Exception as e:                             # data hiccup: report, retry next bar
                msg = f"error: {type(e).__name__}: {e}"; errors += 1
            print(f"  {now:%H:%M}  {msg}")
            if errors >= 3:
                telegram(cfg, f"⚠️ Live paper engine ({mkt}) stopped after 3 errors in a row: {msg[:200]}")
                scrub_state(mkt); sys.exit(1)
            upload(cfg, mkt, f"{now:%Y-%m-%d}-{mkt}.json" if "FINAL" in msg else None)
            if "FINAL" in msg or "already final" in msg or (msg == "outside session" and now.strftime("%H:%M") > SESSION[mkt][1]):
                scrub_state(mkt); print("Session over. Bye."); return
            if "no bars (holiday" in msg and now.strftime("%H:%M") > "11:00":
                print("No data for today (holiday?). Bye."); return
        time.sleep(max(5, (nxt - pd.Timestamp.now(tz=tz)).total_seconds()))


def apply_sizing(cfg):
    """Position sizing comes from secrets/.env (CAPITAL_INDIA, PER_TRADE_INDIA, MAX_TRADES_INDIA, same for _US),
    so the public copy of the code carries only generic defaults."""
    for mkt in ("india", "us"):
        for k, field, cast in (("CAPITAL", "capital", float), ("PER_TRADE", "per_trade", float), ("MAX_TRADES", "max_trades", int)):
            v = cfg.get(f"{k}_{mkt.upper()}")
            if v: B.MKT[mkt][field] = cast(v)


def main():
    mkt = sys.argv[1]
    cfg = env()
    apply_sizing(cfg)
    STATE.mkdir(exist_ok=True)
    if "--check" in sys.argv:                                   # quick setup test: Angel One login + Telegram, then exit
        ok = True
        if mkt == "india":
            try:
                AngelData(cfg, {})._jwt(f"{pd.Timestamp.now(tz=TZ['india']):%Y-%m-%d}"); print("Angel One login OK.")
            except Exception as e:
                print(f"Angel One login FAILED: {e}"); ok = False
        sent = telegram(cfg, f"🔧 Setup check ({'NSE' if mkt == 'india' else 'US'}): Telegram alerts work."
                             + (" Angel One login OK." if mkt == "india" and ok else ""))
        print("Telegram test message sent." if sent else "Telegram test message NOT sent (see the reason above).")
        sys.exit(0 if ok and sent else 5)
    if "--watch" in sys.argv:
        until = sys.argv[sys.argv.index("--until") + 1] if "--until" in sys.argv else None
        return watch(mkt, cfg, until, first="--continue" not in sys.argv)
    if "--replay" in sys.argv:                                  # simulate a past day on a fake clock, no alerts
        day = sys.argv[sys.argv.index("--replay") + 1]
        global OUT
        OUT = HERE / "replay-out"
        (STATE / f"{mkt}.json").unlink(missing_ok=True)
        data = ReplayData(mkt)
        start, _ = SESSION[mkt]
        for now in pd.date_range(f"{day} {start}", f"{day} 16:05", freq="5min", tz=TZ[mkt]) + pd.Timedelta(minutes=1):
            print(step(mkt, now, data, cfg, alert=False))
        (STATE / f"{mkt}.json").unlink(missing_ok=True)
        return
    now = pd.Timestamp.now(tz=TZ[mkt])
    data = AngelData(cfg, {}) if mkt == "india" else AlpacaData(cfg, {})
    print(step(mkt, now, data, cfg))


if __name__ == "__main__":
    main()
