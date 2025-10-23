# discord_listener.py
from __future__ import annotations

import asyncio
import logging
import os
import re
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


class Bot(discord.Client):
    async def on_ready(self):
        tag = f"{self.user}#{getattr(self.user, 'discriminator', '')}"
        log.info("[READY] Logged in as %s | target CHANNEL_ID=%s", tag, DISCORD_CHANNEL_ID)
        # Say hi so we know the bot is alive
        try:
            ch = await self.fetch_channel(DISCORD_CHANNEL_ID)
            await ch.send("👋 bot online")
            log.info("[READY] Sent hello message successfully.")
            log.info("[READY] Resolved channel: %s type=%s", getattr(ch, "name", "<unknown>"), "text")
        except Exception:
            log.exception("[READY] Could not send hello message")

    async def on_message(self, message: discord.Message):
        try:
            # ignore our own messages
            if message.author.id == self.user.id:
                log.info("[DROP] our own message")
                return

            # restrict channel
            if message.channel.id != DISCORD_CHANNEL_ID:
                return

            # restrict authors (optional)
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

            # IMPORTANT: ExecSignal now expects symbol=<...> (not symbol_sig)
            exec_sig = ExecSignal(
                side=psig.side,
                symbol=psig.symbol,
                entry_band=psig.entry_band,
                stop_loss=psig.stop_loss,
                # If your ExecSignal uses a different name than 'tps', change here.
                tps=psig.take_profits,
                leverage=psig.leverage,
                timeframe=psig.timeframe,
            )

            # Be tolerant about attribute name for logs
            sym_for_log = getattr(exec_sig, "symbol", getattr(exec_sig, "symbol_sig", "<?>"))
            log.info(
                "[EXEC] %s %s band=(%.6f, %.6f) SL=%s lev=%s TF=%s",
                exec_sig.side,
                sym_for_log,
                exec_sig.entry_band[0],
                exec_sig.entry_band[1],
                f"{exec_sig.stop_loss:.6f}" if exec_sig.stop_loss is not None else "None",
                exec_sig.leverage,
                exec_sig.timeframe,
            )

            execute_signal(exec_sig)

        except Exception as e:
            log.exception("[ERR] on_message: %s", e)


def _extract_text(message: discord.Message) -> str:
    """Return message text with code blocks stripped."""
    text = getattr(message, "clean_content", None) or message.content or ""
    # remove fenced code blocks
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return text.strip()


def start():
    intents = discord.Intents.default()
    intents.message_content = True
    client = Bot(intents=intents)

    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN")

    asyncio.run(client.start(DISCORD_BOT_TOKEN))


if __name__ == "__main__":
    start()
