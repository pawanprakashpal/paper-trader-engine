# paper-trader-engine

**Paper trading only. It never places a real order with any broker.**

A small engine that paper-trades four simple intraday strategies (opening-range breakout, VWAP reversion, gap fade,
first-hour momentum) on live 5-minute bars, for one NSE or US session at a time. It sends an alert for every
simulated entry and exit, and it publishes a live JSON snapshot for a dashboard page.

## How it runs
- **Runs automatically** on weekdays (NSE 09:00 IST, US 13:15 UTC = 09:15 ET in summer), skipping exchange holidays. Or press **Actions → live-paper-day-trading → Run workflow** and pick `india` or `us`. Start it from 06:30 IST (NSE; 8:00 PM US Central the evening before) or 07:00 ET (US); it waits for the open. A missing secret or a too-early start fails the run with a clear message.
- Every 5 minutes (1 minute after each bar closes), the engine re-runs the strategy rules on the day's completed bars
  (`backtest_intraday.py`, partial-day mode), works out which trades are new or have closed, alerts on them, and
  writes `live-<market>.json`. The whole day is recomputed every step, so a missed step loses nothing.
- Everything is flat by square-off (NSE 15:10 IST bar, US 15:50 ET bar). A session is longer than one GitHub job
  can run, so it uses two jobs.

## Data
- **NSE:** Angel One SmartAPI historical candles (Historical Data API key; login with client code, PIN and TOTP).
- **US:** Alpaca market data (free IEX feed).

## Secrets (Settings → Secrets and variables → Actions)
`ANGEL_HIST_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`, `ALPACA_KEY_ID`, `ALPACA_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FTP_HOST`, `FTP_USER`, `FTP_PASS`, `FTP_DIR`, and optionally the
position sizing `CAPITAL_INDIA`, `PER_TRADE_INDIA`, `MAX_TRADES_INDIA`, `CAPITAL_US`, `PER_TRADE_US`, `MAX_TRADES_US`.
Secrets are encrypted and are never shown in this public repo or in its logs.

## Local use
```bash
pip install pandas numpy requests        # + yfinance for --replay
cp env.template .env                     # fill in; .env is git-ignored
python engine.py india --watch           # run a live paper session
python engine.py india --replay 2026-10-01   # replay a past day on a simulated clock (needs yfinance)
```

Research behind the strategies: none of them beat trading costs over 3 years of data. This engine exists to
*watch* that happen live, not to make money.
