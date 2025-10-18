# Hyperliquid Auto-Trade from Discord Signals

This service reads your Discord VIP signal messages and automatically places orders on Hyperliquid using a safe OTO flow:
- Place **entry limit** first.
- When the entry **fills** (even partially), place **6 take-profits** + **1 stop** for the filled size.

## Quick start (local)

1) Create a bot in the Discord Dev Portal. Enable "Message Content Intent". Invite it to your server and get the channel ID.
2) Copy `.env.example` to `.env` and fill:
   - `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`
   - leave `DRY_RUN=true` for safety first.
3) `pip install -r requirements.txt`
4) `python main.py`

When you see a signal in the channel, the bot will parse it and (in DRY_RUN) print what it **would** trade.

## Deploy on Render

- Create a new **Web Service** from this GitHub repo.
- Set environment variables (same as `.env`).
- `Docker` deploy (Render will pick the Dockerfile).
- Start the service.

## Going live

- Set `DRY_RUN=false`.
- Fill `HYPER_API_KEY`, `HYPER_API_SECRET`.
- Implement the HTTP calls in `broker/hyperliquid.py` (the placeholders marked `NotImplementedError`) according to Hyperliquid's official API docs.
- Keep `EXECUTION_MODE=OTO` for spot safety.

## Config tips

- `TRADE_SIZE_USD` sets your per-trade notional.
- `TP_WEIGHTS` must sum to 1.0 (how size is split across 6 TPs).
- `ENTRY_TIMEOUT_MIN` cancels stale entries.
- You can add validation, persistence (SQLite), and OCO behavior later.

---

