# Signal Bot

A Telegram VIP channel signal tracker that reads trading signals,
opens positions on Binance Demo (testnet), and shows live performance
statistics on a dashboard.

## What it does

1. **Listens** to a Telegram channel via Telethon (userbot).
2. **Parses** every message with AI (DeepSeek v4 Flash) into structured JSON.
3. **Opens** LONG positions on Binance Futures Demo.
4. **Tracks** stop-loss and take-profit levels in the database.
5. **Closes** positions when a CLOSE signal arrives.
6. **Shows** real-time stats on a dashboard (http://localhost:8080).

## Stack

| Layer       | Technology               |
|-------------|--------------------------|
| Telegram    | Telethon (userbot)       |
| AI parser   | DeepSeek v4 Flash (Ollama Cloud) |
| Exchange    | Binance Futures Demo API (httpx) |
| Database    | SQLite + SQLAlchemy (async) |
| Dashboard   | FastAPI + HTML/JS        |
| Python      | 3.11+, asyncio everywhere |

## Project structure

```
signal-bot/
├── app/
│   ├── config.py           # Settings from .env
│   ├── db/
│   │   ├── models.py       # SQLAlchemy models (RawSignal, Trade, TradeLog)
│   │   └── session.py      # Async engine + session factory
│   ├── parser/
│   │   └── ai_parser.py    # LLM signal extractor
│   ├── exchange/
│   │   └── broker.py       # Binance Demo REST client
│   ├── telegram/
│   │   └── listener.py     # Telethon userbot — main event loop
│   └── dashboard/
│       ├── server.py       # FastAPI app
│       └── templates/      # HTML dashboard
├── scripts/
│   ├── backfill.py         # Import channel message history
│   ├── simulate_trades.py  # Create Trade entries from past signals
│   └── test_signal.py      # Quick test trade on Binance Demo
├── run.sh                  # One-command launcher
├── Dockerfile
├── docker-compose.yml
└── .env.example            # Template for your API keys
```

## Quick start

### 1. Set up

```bash
# Copy and fill in your keys
cp .env.example .env
# Edit .env: add TG_PHONE, BINANCE_API_KEY, etc.

# Install dependencies
uv venv --python 3.11
uv pip install -e .
```

### 2. Get API keys

- **Telegram**: get `TG_API_ID` and `TG_API_HASH` from https://my.telegram.org/apps
- **Binance Demo**: create API key at https://testnet.binancefuture.com (login with GitHub, no KYC needed)
- **LLM**: the project uses Ollama Cloud by default. Change `LLM_BASE_URL` and `LLM_API_KEY` for another provider.

### 3. Run

```bash
# Import channel history (first time only)
./run.sh backfill

# Simulate trades from history
./run.sh simulate

# Start dashboard + listener
./run.sh all
```

### Commands cheat sheet

```bash
./run.sh dashboard   # Dashboard only (http://localhost:8080)
./run.sh listener    # Telegram listener only (auto-trades signals)
./run.sh all         # Both dashboard + listener
./run.sh backfill    # Import last 200 messages from the channel
./run.sh simulate    # Create Trade entries from history
./run.sh test        # Open a test trade on Binance Demo
./run.sh docker      # Start with Docker Compose
```

## How it works

### Signal lifecycle

```
New message → AI parser → {pair, entry, tp, sl, leverage}
                             │
                    ┌────────▼────────┐
                    │  OPEN position   │
                    │  on Binance Demo │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         UPDATE_STOP    MOVE_TO_BE      CLOSE
         (new SL)       (SL = entry)    (market sell)
              │              │              │
              └──────────────┴──────────────┘
                             │
                    ┌────────▼────────┐
                    │  Save to DB      │
                    │  Show on dashboard│
                    └─────────────────┘
```

### AI parser output

Every message goes through `deepseek-v4-flash`. The LLM returns structured JSON:

```json
// Open signal
{"action": "OPEN", "pair": "DOGEUSDT", "entry": 0.10401,
 "entry_type": "MARKET", "take_profit": 0.15, "stop_loss": 0.10028}

// Update signal (reply to an open position)
{"action": "UPDATE_STOP", "new_stop": 0.02042}

// Close signal
{"action": "CLOSE"}
```

## Limitations (demo environment)

The Binance Futures Demo API does **not** support:
- `STOP_MARKET` orders (stop-loss)
- `TAKE_PROFIT` / `TAKE_PROFIT_LIMIT` orders

SL and TP levels are tracked in the database. When a CLOSE signal arrives,
the position is closed at market price and PnL is calculated against
the tracked levels.

This is fine for collecting statistics. On a real account, SL/TP orders
work normally.

## Dashboard

Open http://localhost:8080 after starting the server.

Shows:
- Total PnL ($ and %)
- Win rate (wins / losses / breakeven)
- Open and closed trades count
- Best and worst trade
- Per-pair breakdown
- Recent signals with parse latency

The page refreshes every 10 seconds automatically.
