# discord_listener.py
from __future__ import annotations

import asyncio
import logging
import os
import re
import inspect
import discord

from parser import parse_signal
from execution import ExecSignal, execute_signal

log = logging.getLogger("discord_listener")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or "0")

AUTHOR_ALLOWLIST = {
    a.strip().lower()
    for a in os.environ.get("AUTHOR_ALLOWLIST", "").split(",")
    if a.strip()
}


def _extract_text(message: discord.Message) -> str:
    """Return message text with fenced code blocks removed."""
    text = getattr(message, "clean_content", None) or message.content or ""
    return re.sub(r"```.*?```", "", text, flags=re.S).strip()


def _build_execsignal_kwargs(psig) -> dict:
    """
    Map the parsed signal into ExecSignal's actual __init__ parameter names.
    Supports both old and new shapes:
      - entry_band=(low, high)  OR  band_low/band_high
      - symbol OR symbol_sig
      - tps OR take_profits
      - timeframe OR tf
      - stop_loss OR sl
      - leverage OR lev
    """
    params = set(inspect.signature(ExecSignal).parameters.keys())

    # Base values from parser
    low, high = psig.entry_band
    kwargs = {
        # side is consistent in all versions
        "side": psig.side,
    }

    # symbol
    if "symbol" in params:
        kwargs["symbol"] = psig.symbol
    elif "symbol_sig" in params:
        kwargs["symbol_sig"] = psig.symbol

    # band / entry range
    if "entry_band" in params:
        kwargs["entry_band"] = (low, high)
    else:
        # assume split band fields
        if "band_low" in params:
            kwargs["band_low"] = low
        if "band_high" in params:
            kwargs["band_high"] = high

    # stop loss
    if "stop_loss" in params:
        kwargs["stop_loss"] = psig.stop_loss
    elif "sl" in params:
        kwargs["sl"] = psig.stop_loss

    # take profits list
    if "tps" in params:
        kwargs["tps"] = psig.take_profits
    elif "take_profits" in params:
        kwargs["take_profits"] = psig.take_profits

    # leverage
    if "leverage" in params:
        kwargs["leverage"] = psig.leverage
    elif "lev" in params:
        kwargs["lev"] = psig.leverage

    # timeframe
    if "timeframe" in params:
        kwargs["timeframe"] = psig.timeframe
    elif "tf" in params:
        kwargs["tf"] = psig.timeframe

    return kwargs


class Bot(discord.Client):
    async def on_ready(self):
        tag = f"{self.user}#{getattr(self.user, 'discriminator', '')}"
        log.info("[READY] Logged in as %s | target CHANNEL_ID=%s", tag, DISCORD_CHANNEL_ID)
        try:
            ch = await self.fetch_channel(DISCORD_CHANNEL_ID)
            await ch.send("👋 bot online")
            log.info("[READY] Sent hello message successfully.")
            log.info("[READY] Resolved channel: %s type=%s", getattr(ch, "name", "<unknown>"), "text")
        except Exception:
            log.exception("[READY] Could not send hello message")

    async def on_message(self, message: discord.Message):
        try:
            # Ignore our own messages
            if message.author.id == self.user.id:
                log.info("[DROP] our own message")
                return

            if message.channel.id != DISCORD_CHANNEL_ID:
                return

            if AUTHOR_ALLOWLIST and message.author.name.lower() not in AUTHOR_ALLOWLIST:
                log.info("[SKIP] author '%s' not in allowlist", message.author.name)
                return

            content = _extract_text(message)
            log.info(
                "[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%d",
                message.id,
                message.author.name,
                message.channel.id,
                getattr(message.channel, "name", ""),
                len(content or ""),
            )

            psig = parse_signal(content or "")
            if not psig:
                log.info("[SKIP] Could not parse signal from message.")
                return

            band_low, band_high = psig.entry_band
            log.info(
                "[PASS] parsed: %s %s band=(%.6f, %.6f) SL=%s TPn=%d lev=%s TF=%s",
                psig.side,
                psig.symbol,
                band_low,
                band_high,
                f"{psig.stop_loss:.6f}" if psig.stop_loss is not None else "None",
                len(psig.take_profits),
                psig.leverage,
                psig.timeframe,
            )

            kwargs = _build_execsignal_kwargs(psig)
            exec_sig = ExecSignal(**kwargs)

            # For logging, be tolerant to symbol naming
            sym_for_log = getattr(exec_sig, "symbol", getattr(exec_sig, "symbol_sig", "<?>"))
            # entry band unified for logs
            if hasattr(exec_sig, "entry_band"):
                lo, hi = exec_sig.entry_band
            else:
                lo = getattr(exec_sig, "band_low", band_low)
                hi = getattr(exec_sig, "band_high", band_high)

            log.info(
                "[EXEC] %s %s band=(%.6f, %.6f) SL=%s lev=%s TF=%s",
                exec_sig.side,
                sym_for_log,
                lo,
                hi,
                f"{getattr(exec_sig, 'stop_loss', getattr(exec_sig, 'sl', None)):.6f}"
                if getattr(exec_sig, "stop_loss", getattr(exec_sig, "sl", None)) is not None
                else "None",
                getattr(exec_sig, "leverage", getattr(exec_sig, "lev", None)),
                getattr(exec_sig, "timeframe", getattr(exec_sig, "tf", None)),
            )

            execute_signal(exec_sig)

        except Exception as e:
            log.exception("[ERR] on_message: %s", e)


def start():
    intents = discord.Intents.default()
    intents.message_content = True
    client = Bot(intents=intents)

    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN")

    asyncio.run(client.start(DISCORD_BOT_TOKEN))


if __name__ == "__main__":
    start()
