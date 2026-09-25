# Trading Agent

A trading bot built on [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (v0.5.1). It runs by itself in the cloud on **GitHub Actions** (your Mac can be off), on three schedules:

| Schedule | When (Melbourne time) | What |
|---|---|---|
| **Stocks** | Tue–Sat 8:30am (9:30am in daylight saving), after each US session | US stocks (traded) and ASX (signals) |
| **Crypto** | Every 8 hours, 7 days a week: 10:15am, 6:15pm, 2:15am (+1h in daylight saving) | BTC, ETH, SOL |
| **Weekly summary** | Friday 6pm (5pm outside daylight saving) | Last 7 days: P&L, trades, holdings, how the calls played out, benchmarks, spend |

GitHub schedules run on UTC, which is why the Melbourne times shift by an hour with daylight saving. GitHub can also start a scheduled run a few minutes late when it's busy.

US stocks are never analysed or traded for Saturday or Sunday (or US holidays). Crypto keeps going all weekend.

Each run:

1. Books any fills from earlier orders and checks the bot's own holdings.
2. Runs the TradingAgents team (market, sentiment, news and fundamentals analysts → bull/bear debate → trader → risk team → portfolio manager) on each ticker in your watchlist. Each ticker gets a rating: **Buy / Overweight / Hold / Underweight / Sell**.
3. Turns the ratings into a few small orders, inside hard risk limits.
4. **Paper mode:** places the orders on Alpaca automatically.
   **Live mode:** sends each order to Telegram with **Approve / Skip** buttons. Nothing is placed without your tap.
5. Saves a full report in `reports/` and messages you on Telegram. The morning stocks run always reports. The crypto checks only message you when something happened (a trade, an error, a halt), so your phone isn't buzzing for nothing.

ASX tickers are analysed and reported as signals only. There is no broker API for your Stake account, so you place those trades yourself.

---

## What each rating does

| Rating | Action (US stocks and crypto) |
|---|---|
| Buy | Build the position up to **10%** of the cap (US$50) |
| Overweight | Build the position up to **5%** of the cap (US$25) |
| Hold | Nothing |
| Underweight | Trim the position down to **2.5%** of the cap (US$12.50) |
| Sell | Sell **all** of what the bot holds |

## Safety rules (built into the code)

- **Capital cap: US$500.** The bot's holdings plus its open buy orders never go above this, even if the account holds more.
- **No shorting and no margin.** It only buys with cash and only sells what it owns.
- **It never touches your own positions.** It keeps its own ledger (`data/state-paper.json` / `data/state-live.json`) and only sells what it bought.
- **Limit orders only**, at most 1% from the last price. US stock orders placed after the close are queued for the next open. If the price gaps more than 1%, they don't fill. Crypto orders fill immediately or cancel.
- **Max 6 orders per run**, sells first.
- **Loss stop:** if the bot's total P&L falls below −US$100, buying stops and you get a Telegram alert. Send `/resume` to re-enable it.
- **Kill switch:** send `/stop` in Telegram, or run `./ta stop`. It still analyses and reports, but places nothing until `/resume` or `./ta resume`.
- **OpenAI budget:** it stops analysing more tickers once the day's spend across all runs passes US$3.00 (`daily_budget_usd`). The normal daily total is about US$1.15.
- **Live mode refuses to run** unless Telegram is set up. Unanswered approvals are skipped after 3 hours (60 minutes for crypto checks).
- **One run at a time.** If two schedules overlap, the second waits for the first to finish.

---

## Setup (one time, about 20 minutes)

Open **Terminal** and run:

```bash
cd ~/agentic-ai/trading-agent
./setup.sh
```

This creates a Python 3.12 environment inside the folder and installs TradingAgents and everything else. Your OpenAI key is already in `.env`.

### 1. Alpaca paper account (free, about 5 minutes)

1. Sign up at **https://alpaca.markets** with just an email. Australians are supported.
2. In the dashboard, switch to **Paper Trading** (top-left account menu).
3. On the right, under **API Keys**, click **Generate New Keys**. The secret is shown only once.
4. Open `.env` in a text editor (`open -e .env`) and fill in:
   ```
   ALPACA_PAPER_KEY_ID=PK...
   ALPACA_PAPER_SECRET_KEY=...
   ```

### 2. Telegram bot (about 3 minutes)

1. In Telegram, message **@BotFather** → send `/newbot` → pick a name → pick a username ending in `bot`.
2. Copy the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Run `./ta telegram-setup`, then send your new bot any message. It saves your chat ID and replies "Connected".

### 3. Check everything

```bash
./ta doctor
```

Every line should say `OK`. Fix any `FAIL` line before going further.

### 4. First test: one ticker, no orders

```bash
./ta run --dry-run --only NVDA -v
```

This takes a few minutes. It prints the rating, the order it *would* place, and **the real OpenAI cost for one ticker**. Multiply by 11 for a rough daily cost.

### 5. Full paper run, then turn on the schedules

```bash
./ta run                  # full watchlist, paper orders placed
./ta install-schedule     # stocks, crypto and weekly schedules
```

### Keeping it running 24/7 (only if you run it on the Mac)

The schedules only run while the Mac is on and awake:

- **Asleep at a scheduled time:** the run happens as soon as the Mac wakes. Missed crypto slots collapse into one run.
- **Shut down:** those runs are skipped.
- **During a run:** the Mac is kept awake until the run finishes.

For true round-the-clock crypto, keep the Mac **plugged in** and turn on **System Settings → Battery → Options → "Prevent automatic sleeping on power adapter when the display is off"**. With the lid closed, a MacBook sleeps unless it has an external display.

If you want it running with the laptop closed or away from home, the next step is a small cloud server (about US$5/month).

---

## Running in the cloud (GitHub Actions)

### One-time move from the Mac

```bash
brew install gh && gh auth login      # only if you don't have the GitHub CLI yet
cd ~/agentic-ai/trading-agent && ./scripts/move-to-github.sh
```

The script does five things:

1. Stops the Mac schedules, so two copies never trade the same account.
2. Creates a **private** repo called `trading-agent` and pushes the code. `.env`, `data/`, `logs/` and `reports/` are never uploaded.
3. Copies the bot's current book (holdings, open orders, history) to a `state` branch.
4. Copies your keys from `.env` into the repo's encrypted **Actions secrets**.
5. Starts a `doctor` run on GitHub to check every connection.

### How it works

- `.github/workflows/crypto.yml`, `stocks.yml` and `weekly.yml` hold the schedules (cron, UTC). All three run the shared job in `_run.yml`.
- Each run checks out the code and the `state` branch, runs one task, then commits the updated book and reports back to `state`. You can browse `reports/` on that branch on GitHub.
- Only one run happens at a time. If schedules overlap, the later run waits.
- If a run fails, you get a Telegram message with a link to the run.

### Everyday use

- **Run something now:** Actions tab → **manual run** → Run workflow, then pick a task:
  - `doctor`: check every connection
  - `status`: holdings, P&L, last runs
  - `crypto`, `stocks` or `all`: run the bot now
  - `weekly`: send the weekly summary now
- **Pause everything:** send `/stop` in Telegram (and `/resume` later), or disable the workflows in the Actions tab.
- **Change settings:** edit `config.yaml` on GitHub (or locally, then `git push`). To change how often crypto runs, edit the cron in `crypto.yml` and set `schedule.crypto_every_hours` to match.
- **Change keys:** repo Settings → Secrets and variables → Actions.
- **Update the code:** `git pull` on the Mac, edit, `git push`.

### Free minutes

Private repos get 2,000 free Actions minutes a month. This setup uses about 1,700: three crypto checks a day at about 12 minutes each, plus about 30 minutes per stocks run. Running crypto every 4 hours would need about 2,800 minutes, and the extra minutes cost US$0.006 each.

### The Mac copy

After the move, `./ta run` on the Mac refuses to trade. The cloud copy owns the book, and two copies trading one account would double every position. `./ta status` on the Mac shows an out-of-date book, so check the Actions tab or Telegram instead. To go back to running on the Mac, delete `.runs-on-github`, disable the GitHub workflows, then run `./ta install-schedule`.

---

## Daily use

| Command | What it does |
|---|---|
| `./ta status` | Bot holdings, P&L, open orders, OpenAI spend, schedules, last runs |
| `./ta weekly` | Build this week's summary now (and send it to Telegram) |
| `./ta run --scope crypto` | Crypto check now (`--scope stocks` for stocks + ASX) |
| `./ta run --dry-run` | Analyse and plan, place nothing |
| `./ta run --only BTC-USD,NVDA` | Run just these tickers |
| `./ta run --force` | Run even if this run already happened |
| `./ta stop` / `./ta resume` | Kill switch |
| `./ta install-schedule` | Install or update the schedules (after editing `config.yaml > schedule`) |
| `./ta remove-schedule` | Stop all automatic runs |

Telegram commands: `/stop`, `/resume`. They're read when a run starts and while it waits for approval.

Reports are saved in three places:

- **Stocks runs:** `reports/YYYY-MM-DD.md`
- **Crypto checks:** `reports/crypto/`
- **Weekly summaries:** `reports/weekly/`

Each report includes the agent team's full reasoning. Logs are in `logs/`, including one launchd log per schedule.

## Settings

Everything is in `config.yaml`: watchlist, cap, position sizes, loss stop, approval timeout, models and daily budget. Secrets live in `.env` only.

**Model cost.** The default uses `gpt-6-sol` for the two decision-making agents and `gpt-6-luna` for the rest. The first real run (25 Sep 2026) cost about **US$0.05 per ticker**, or about US$0.55 for all 11. With crypto checked every 4 hours, that comes to about US$1.15 a day, or roughly US$35 a month. Set `schedule.crypto_every_hours: 8` to bring it down to about US$23. `./ta status` shows the running total. Check it against your OpenAI usage page now and then.

**Optional data sources.** A free FRED key (https://fred.stlouisfed.org/docs/api/api_key.html → `FRED_API_KEY=` in `.env`) gives the news analyst real macro data: CPI, rates and unemployment. Polymarket is blocked in Australia, so the news analyst skips it. Those warnings are harmless and only appear in `logs/agent.log`.

---

## Going live (after at least 4 weeks of paper trading)

Checklist:

1. Read the paper reports. Would you have been happy with those trades? Check `./ta status` for P&L against costs.
2. Open an Alpaca **live** account. This needs ID verification and a W-8BEN form (both done in their app). Fund it by international wire in USD, and keep bank FX fees in mind.
3. Generate **live** API keys and add them to `.env`:
   ```
   ALPACA_LIVE_KEY_ID=AK...
   ALPACA_LIVE_SECRET_KEY=...
   ```
4. In `config.yaml`, set `mode: live`.
5. Run `./ta doctor`. It must show `Alpaca (LIVE)` and `Telegram` as OK.
6. From then on, every order arrives in Telegram for approval.

The paper and live ledgers are separate files in `data/`. Your paper history is kept, and the live bot starts with an empty book.

**Tax (Australia):** every sale, including crypto, is a CGT event. Download trade confirmations from Alpaca at EOFY. The bot also records every trade in `data/state-paper.json` / `data/state-live.json` the bot made.

---

**On GitHub:** a run waiting for your Telegram approval keeps using Actions minutes, up to 3 hours for a stocks run. Before going live, set `approval.timeout_minutes` to about 30, and add `ALPACA_LIVE_KEY_ID` / `ALPACA_LIVE_SECRET_KEY` as repo secrets.

## Security

- `.env` holds your keys on the Mac. It's readable only by your user account and is never uploaded to GitHub.
- On GitHub, the keys live in encrypted **Actions secrets**, which can't be read back, even by you. Keep the repo **private**: the `state` branch holds your trade history.
- If you rotate a key, update it in both `.env` and the repo secrets (`gh secret set OPENAI_API_KEY`, then paste the new key).
- **Rotate the OpenAI key** that was pasted into chat: create a new one at https://platform.openai.com/api-keys, put it in `.env`, and delete the old one.
- Set a monthly spend limit on your OpenAI account (Settings → Limits) as a backstop.

## Troubleshooting

- **`FAIL OpenAI ... no access to gpt-6-sol`**: your account tier may not include it yet. Set `deep_model: gpt-6-luna` in `config.yaml`.
- **Stock orders show "expired"**: the price moved more than 1% at the open. That's the price protection working. Raise `limit_buffer_pct` to 1.5 if it happens often.
- **Nothing ran this morning**: check `logs/launchd.err.log`, and that the Mac wasn't shut down.
- **`another run is already in progress`**: a scheduled run is still going (analysis plus up to 3 hours of approval wait).

Research software, not financial advice. LLM trading decisions are unproven, so only use money you can afford to lose.
