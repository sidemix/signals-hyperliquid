# discord_listener.py
from __future__ import annotations

import os
import re
import logging
import discord
from typing import Any, List, Optional

from execution import ExecSignal, execute_signal
import parser as sig_parser  # your existing parser module

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_listener")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
AUTHOR_ALLOWLIST = {s.strip() for s in os.getenv("AUTHOR_ALLOWLIST", "").split(",") if s.strip()}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _pick(o: Any, *names: str, default: Any = None) -> Any:
    """Return first present, non-None attribute from names on object o."""
    for n in names:
        if hasattr(o, n):
            v = getattr(o, n)
            if v is not None:
                return v
    return default


def _as_float_list(v: Any) -> List[float]:
    """Normalize targets / tps to a list[float]."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out: List[float] = []
        for x in v:
            try:
                out.append(float(x))
            except Exception:
                continue
        return out
    # single value
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
    # Ignore our own messages
    if message.author == client.user:
        log.info("[DROP] our own message")
        return

    # Channel filter
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    author_name = str(message.author.display_name or message.author.name)
    content = message.content or ""
    log.info("[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%s",
             message.id, author_name, message.channel.id, getattr(message.channel, "name", "?"), len(content))

    # Author allowlist (if provided)
    if AUTHOR_ALLOWLIST and author_name not in AUTHOR_ALLOWLIST:
        log.info("[SKIP] Author '%s' not in allowlist.", author_name)
        return

    # Parse
    try:
        parsed = sig_parser.parse_signal(content)
    except Exception:
        log.exception("[SKIP] Could not parse signal from message.")
        return

    # Best-effort logging of what parser produced
    try:
        s_side = _pick(parsed, "side")
        s_symbol = _pick(parsed, "symbol", "pair", "coin")
        s_low = _pick(parsed, "entry_low", "band_low", "low")
        s_high = _pick(parsed, "entry_high", "band_high", "high")
        s_sl = _pick(parsed, "stop_loss", "sl", "stop", "stoploss")
        s_lev = _pick(parsed, "leverage", "lev")
        s_tf = _pick(parsed, "timeframe", "tf")
        log.info("[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%s lev=%s TF=%s",
                 s_side, s_symbol, s_low, s_high, s_sl,
                 len(_as_float_list(_pick(parsed, "tps", "targets", "take_profits"))),
                 s_lev, s_tf)
    except Exception:
        pass

    # Normalize all possible field names to ExecSignal kwargs
    try:
        side: str = str(_pick(parsed, "side")).upper()
        symbol: str = str(_pick(parsed, "symbol", "pair", "coin"))

        # entry band: accept many variants
        entry_low = _pick(parsed, "entry_low", "band_low", "low", "min_entry")
        entry_high = _pick(parsed, "entry_high", "band_high", "high", "max_entry")

        # Some parsers give a tuple under entry_band/band; handle that too.
        if (entry_low is None or entry_high is None):
            band = _pick(parsed, "entry_band", "band")
            if band and isinstance(band, (list, tuple)) and len(band) >= 2:
                entry_low, entry_high = band[0], band[1]

        if entry_low is None or entry_high is None:
            raise ValueError("Parser did not provide entry band (entry_low/entry_high or equivalent).")

        stop_loss = _pick(parsed, "stop_loss", "sl", "stop", "stoploss")
        take_profits = _as_float_list(_pick(parsed, "tps", "targets", "take_profits"))
        leverage = _pick(parsed, "leverage", "lev")
        timeframe = _pick(parsed, "timeframe", "tf")

        # Build ExecSignal with the exact constructor names it requires
        kwargs = dict(
            side=side,
            symbol=symbol,
            entry_low=float(entry_low),
            entry_high=float(entry_high),
            stop_loss=(float(stop_loss) if stop_loss is not None else None),
            take_profits=take_profits,
            leverage=(float(leverage) if leverage is not None else None),
            timeframe=(str(timeframe) if timeframe is not None else None),
        )

        exec_sig = ExecSignal(**kwargs)
        execute_signal(exec_sig)

    except Exception as e:
        log.exception("[ERR] on_message: %s", e)


def start():
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing.")
    client.run(TOKEN, log_handler=None)  # let our logging config handle logs


if __name__ == "__main__":
    start()
