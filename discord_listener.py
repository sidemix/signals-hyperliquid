# discord_listener.py
from __future__ import annotations

import logging
import os
import re
import asyncio
import discord

from parser import parse_signal                   # <- new tolerant parser
from execution import ExecSignal, execute_signal  # your executor

log = logging.getLogger("discord_listener")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or "0")

# Optional: a comma-separated allowlist of author names (exact match, case-insensitive)
AUTHOR_ALLOWLIST = {
    a.strip().lower()
    for a in os.environ.get("AUTHOR_ALLOWLIST", "").split(",")
    if a.strip()
}


class Bot(discord.Client):
    async def setup_hook(self) -> None:
        # You can add sync startup work here if needed
        pass

    async def on_ready(self):
        log.info(
            "[READY] Logged in as %s | target CHANNEL_ID=%s",
            f"{self.user}#{getattr(self.user, 'discriminator', '')}",
            DISCORD_CHANNEL_ID,
        )
        # Send a hello in the channel so we know we're alive
        try:
            ch = await self.fetch_channel(DISCORD_CHANNEL_ID)
            await ch.send("👋 bot online")
            log.info("[READY] Sent hello message successfully.")
            log.info(
                "[READY] Resolved channel: %s type=%s",
                getattr(ch, "name", "<unknown>"),
                getattr(ch, "type", "<unknown>"),
            )
        except Exception:
            log.exception("[READY] Could not send hello message")

    async def on_message(self, message: discord.Message):
        try:
            # Ignore our own messages
            if message.author.id == self.user.id:
                log.info("[DROP] our own message")
                return

            # Restrict authors if allowlist is set
            if AUTHOR_ALLOWLIST:
                if message.author.name.lower() not in AUTHOR_ALLOWLIST:
                    log.info(
                        "[SKIP] author '%s' not in allowlist", message.author.name
                    )
                    return

            # Only handle target channel
            if message.channel.id != DISCORD_CHANNEL_ID:
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

            sig = parse_signal(content or "")
            if not sig:
                log.info("[SKIP] Could not parse signal from message.")
                return

            # NOTE: old code referenced sig.entry_low / sig.entry_high (no longer valid).
            band_low, band_high = sig.entry_band
            log.info(
                "[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%d lev=%s TF=%s",
                sig.side,
                sig.symbol,
                f"{band_low:.6f}",
                f"{band_high:.6f}",
                f"{sig.stop_loss:.6f}" if sig.stop_loss is not None else "None",
                len(sig.take_profits),
                sig.leverage,
                sig.timeframe,
            )

            exec_sig = ExecSignal(
                side=sig.side,
                symbol_sig=sig.symbol,
                entry_band=sig.entry_band,
                stop_loss=sig.stop_loss,
                tps=sig.take_profits,
                leverage=sig.leverage,
                timeframe=sig.timeframe,
            )
            log.info(
                "[EXEC] %s %s band=(%s, %s) SL=%s lev=%s TF=%s",
                exec_sig.side,
                exec_sig.symbol_sig,
                f"{exec_sig.entry_band[0]:.6f}",
                f"{exec_sig.entry_band[1]:.6f}",
                f"{exec_sig.stop_loss:.6f}" if exec_sig.stop_loss is not None else "None",
                exec_sig.leverage,
                exec_sig.timeframe,
            )

            execute_signal(exec_sig)

        except Exception as e:
            log.exception("[ERR] on_message: %s", e)


def _extract_text(message: discord.Message) -> str:
    """
    Prefer the 'clean' text; fall back to raw content if needed.
    Strips code blocks and trims excess whitespace.
    """
    text = getattr(message, "clean_content", None) or message.content or ""
    # strip fenced code blocks
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return text.strip()


def start():
    intents = discord.Intents.default()
    intents.message_content = True  # required to read message text
    client = Bot(intents=intents)

    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN")

    asyncio.run(client.start(DISCORD_BOT_TOKEN))


if __name__ == "__main__":
    start()
