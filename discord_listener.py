# discord_listener.py
import os
import asyncio
import re
import discord

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal, is_symbol_allowed

# ---- Config from env ----
DISCORD_TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
TARGET_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
# Optional: only accept messages from this user id (int). Leave empty to accept anyone.
AUTHOR_ID_ENV     = os.getenv("AUTHOR_ID", "").strip()
ONLY_AUTHOR_ID    = int(AUTHOR_ID_ENV) if AUTHOR_ID_ENV.isdigit() else None

def _log(msg: str):
    print(msg, flush=True)

def _norm_symbol_to_hl(s: str) -> str:
    # VIP feed uses XXX/USD while HL takes the same; keep as-is.
    return s.strip().upper().replace("USDT", "USD")

def _signal_to_exec(sig) -> ExecSignal:
    side = "buy" if sig.side.upper() == "LONG" else "sell"
    return ExecSignal(
        symbol=_norm_symbol_to_hl(sig.symbol),
        side=side,
        entry_low=sig.entry_band[0],
        entry_high=sig.entry_band[1],
        stop=sig.stop,
        tps=sig.take_profits,
        leverage=sig.leverage,
        timeframe=sig.timeframe or "",
    )

# -------- Discord client (make sure we request message content!) --------
intents = discord.Intents.default()
intents.message_content = True          # <-- critical
intents.guilds = True
intents.members = False

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    try:
        channel = client.get_channel(TARGET_CHANNEL_ID)
        _log(f"[READY] Logged in as {client.user} | target CHANNEL_ID={TARGET_CHANNEL_ID}")
        if channel is None:
            _log("[READY] Could not resolve channel object (None). "
                 "Check DISCORD_CHANNEL_ID and that the bot is in the same server.")
        else:
            _log(f"[READY] Resolved channel: {channel} "
                 f"type={getattr(channel, 'type', None)} parent_id={getattr(channel, 'category_id', None)}")
            # Say hello so we know we can write
            try:
                await channel.send("👋 Bot online (debug). I can read this channel.")
                _log("[READY] Sent hello message successfully.")
            except Exception as e:
                _log(f"[READY] Could not send hello message: {e}")
    except Exception as e:
        _log(f"[READY] on_ready error: {e}")

@client.event
async def on_message(message: discord.Message):
    # Log everything we get so we can see what Discord is delivering
    try:
        _log(f"[RX] msg_id={message.id} author='{message.author}' "
             f"chan_id={message.channel.id} chan_name={getattr(message.channel, 'name', None)} "
             f"type={getattr(message.channel, 'type', None)} "
             f"parent_id={getattr(getattr(message.channel, 'category', None), 'id', None)} "
             f"guild_id={getattr(getattr(message.guild, 'id', None), '__str__', lambda: None)() if message.guild else None} "
             f"len={len(message.content or '')}")
    except Exception:
        pass

    # Ignore our own messages to avoid loops
    if message.author.id == client.user.id:
        _log("[DROP] our own message")
        return

    # Only watch the configured channel
    if message.channel.id != TARGET_CHANNEL_ID:
        _log(f"[DROP] wrong channel: {message.channel.id}")
        return

    # Optional author filter
    if ONLY_AUTHOR_ID is not None and message.author.id != ONLY_AUTHOR_ID:
        _log(f"[DROP] author not allowed (msg_author_id={message.author.id} != ONLY_AUTHOR_ID={ONLY_AUTHOR_ID})")
        return

    text = message.content or ""
    if not text.strip():
        _log("[DROP] empty content (likely only embeds/attachments)")
        return

    # Try to parse the VIP signal text
    sig = parse_signal_from_text(text)
    if not sig:
        _log("[PARSE] No signal pattern matched this message.")
        return

    # Check symbol allow list (HYPER_ONLY_EXECUTE_SYMBOLS) before trading
    hl_symbol = _norm_symbol_to_hl(sig.symbol)
    if not is_symbol_allowed(hl_symbol):
        _log(f"[SKIP] Symbol '{hl_symbol}' not in HYPER_ONLY_EXECUTE_SYMBOLS (or allow list is empty).")
        return

    try:
        ex = _signal_to_exec(sig)
        _log(f"[EXEC] {ex.side.upper()} {ex.symbol} "
             f"entry {ex.entry_low}–{ex.entry_high} SL {ex.stop} "
             f"TPs {ex.tps} lev={ex.leverage or 'default'} tf={ex.timeframe or ''}")
        ok, resp = execute_signal(ex)
        if ok:
            await message.add_reaction("✅")
            _log(f"[OK] order placed: {resp}")
        else:
            await message.add_reaction("⚠️")
            _log(f"[ERR] order rejected: {resp}")
    except Exception as e:
        _log(f"[EXC] execution error: {e}")
        try:
            await message.add_reaction("❌")
        except Exception:
            pass

def start():
    client.run(DISCORD_TOKEN)
