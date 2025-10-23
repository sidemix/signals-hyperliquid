# discord_listener.py
from __future__ import annotations

import os
import logging
import inspect
from typing import Any, List

import discord

from execution import ExecSignal, execute_signal
import parser as sig_parser  # your existing parser

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_listener")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
AUTHOR_ALLOWLIST = {s.strip() for s in os.getenv("AUTHOR_ALLOWLIST", "").split(",") if s.strip()}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def _pick(o: Any, *names: str):
    for n in names:
        if hasattr(o, n):
            v = getattr(o, n)
            if v is not None:
                return v
    return None

def _as_float_list(v: Any) -> List[float]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out: List[float] = []
        for x in v:
            try:
                out.append(float(x))
            except Exception:
                pass
        return out
    try:
        return [float(v)]
    except Exception:
        return []

@client.event
async def on_ready():
    log.info("[READY] Logged in as %s | target CHANNEL_ID=%s", client.user, CHANNEL_ID)
    try:
        channel = await client.fetch_channel(CHANNEL_ID)
        await channel.send("👋 Ready. I’ll execute on allowed symbols.")
        log.info("[READY] Sent hello message successfully.")
        log.info("[READY] Resolved channel: %s type=%s", getattr(channel, "name", "?"), getattr(channel, "type", "?"))
    except Exception:
        log.exception("[READY] Failed to send hello or resolve channel.")

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        log.info("[DROP] our own message")
        return

    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    author_name = str(message.author.display_name or message.author.name)
    content = message.content or ""
    log.info("[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%s",
             message.id, author_name, message.channel.id, getattr(message.channel, "name", "?"), len(content))

    if AUTHOR_ALLOWLIST and author_name not in AUTHOR_ALLOWLIST:
        log.info("[SKIP] Author '%s' not in allowlist.", author_name)
        return

    # -------- Parse the message --------
    try:
        parsed = sig_parser.parse_signal(content)
    except Exception:
        log.exception("[SKIP] Could not parse signal from message.")
        return

    # Light logging of what we got from the parser
    try:
        s_side = _pick(parsed, "side")
        s_symbol = _pick(parsed, "symbol", "pair", "coin")
        s_low = _pick(parsed, "entry_low", "band_low", "low")
        s_high = _pick(parsed, "entry_high", "band_high", "high")
        if (s_low is None or s_high is None):
            band = _pick(parsed, "entry_band", "band")
            if band and isinstance(band, (list, tuple)) and len(band) >= 2:
                s_low, s_high = band[0], band[1]
        s_sl = _pick(parsed, "stop_loss", "sl", "stop", "stoploss")
        s_lev = _pick(parsed, "leverage", "lev")
        s_tf = _pick(parsed, "timeframe", "tf")
        tps = _as_float_list(_pick(parsed, "tps", "targets", "take_profits"))
        log.info("[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%s lev=%s TF=%s",
                 s_side, s_symbol, s_low, s_high, s_sl, len(tps), s_lev, s_tf)
    except Exception:
        pass

    # -------- Normalize into ExecSignal arguments --------
    try:
        side = str(_pick(parsed, "side")).upper()
        symbol = str(_pick(parsed, "symbol", "pair", "coin"))

        entry_low = _pick(parsed, "entry_low", "band_low", "low", "min_entry")
        entry_high = _pick(parsed, "entry_high", "band_high", "high", "max_entry")
        if entry_low is None or entry_high is None:
            band = _pick(parsed, "entry_band", "band")
            if band and isinstance(band, (list, tuple)) and len(band) >= 2:
                entry_low, entry_high = band[0], band[1]

        # If still no band, skip gracefully with clear reason.
        if entry_low is None or entry_high is None:
            log.warning(
                "[SKIP] Parser did not provide an entry band (got entry_low=%r, entry_high=%r).",
                entry_low, entry_high
            )
            return

        stop_loss = _pick(parsed, "stop_loss", "sl", "stop", "stoploss")
        leverage = _pick(parsed, "leverage", "lev")
        timeframe = _pick(parsed, "timeframe", "tf")
        tps = _as_float_list(_pick(parsed, "tps", "targets", "take_profits"))

        # Build the superset of kwargs we *might* pass
        sup_kwargs = dict(
            side=side,
            symbol=symbol,
            entry_low=float(entry_low),
            entry_high=float(entry_high),
            stop_loss=(float(stop_loss) if stop_loss is not None else None),
            leverage=(float(leverage) if leverage is not None else None),
            timeframe=(str(timeframe) if timeframe is not None else None),
            take_profits=tps,  # include, but only pass if ExecSignal supports it
        )

        # Filter by actual ExecSignal constructor signature (prevents unexpected kwarg errors)
        sig = inspect.signature(ExecSignal)  # dataclass __init__ signature
        allowed = set(sig.parameters.keys()) - {"self"}
        kwargs = {k: v for k, v in sup_kwargs.items() if k in allowed}

        # Create and execute
        exec_sig = ExecSignal(**kwargs)
        execute_signal(exec_sig)

    except Exception as e:
        log.exception("[ERR] on_message: %s", e)

def start():
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing.")
    client.run(TOKEN, log_handler=None)

if __name__ == "__main__":
    start()
