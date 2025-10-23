import os
import logging
import asyncio
from typing import Optional, Tuple

import discord

from execution import ExecSignal, execute_signal

# If your parser lives in another module, keep this import the same as in your repo.
# It must expose `parse_signal(text: str)` -> object with fields shown below.
from parser import parse_signal  # type: ignore

log = logging.getLogger("discord_listener")
log.setLevel(logging.INFO)


def _get_env_channel_id() -> Optional[int]:
    raw = os.getenv("CHANNEL_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _coerce_entry_band(parsed) -> Tuple[Optional[float], Optional[float]]:
    """
    Support multiple parser shapes:

    - parsed.entry_low / parsed.entry_high
    - parsed.band_low / parsed.band_high
    - parsed.entry_band = (low, high)

    Returns (low, high) as floats or (None, None).
    """
    # 1) explicit entry_low / entry_high
    low = getattr(parsed, "entry_low", None)
    high = getattr(parsed, "entry_high", None)
    if low is not None and high is not None:
        try:
            return float(low), float(high)
        except Exception:
            pass

    # 2) band_low / band_high
    low = getattr(parsed, "band_low", None)
    high = getattr(parsed, "band_high", None)
    if low is not None and high is not None:
        try:
            return float(low), float(high)
        except Exception:
            pass

    # 3) entry_band = tuple
    band = getattr(parsed, "entry_band", None)
    if band and isinstance(band, (tuple, list)) and len(band) == 2:
        try:
            return float(band[0]), float(band[1])
        except Exception:
            pass

    return None, None


def _norm_side(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    up = s.strip().upper()
    if up in ("LONG", "SHORT"):
        return up
    return None


def _norm_symbol(sym: Optional[str]) -> Optional[str]:
    if not sym:
        return None
    s = sym.strip().upper()
    # normalize common forms like "BTCUSDT" -> "BTC/USD" if you prefer.
    # For now, accept what the parser provides.
    return s


def _maybe_float(x, default=None) -> Optional[float]:
    if x is None:
        return default
    try:
        return float(x)
    except Exception:
        return default


def _maybe_int(x, default=None) -> Optional[int]:
    if x is None:
        return default
    try:
        return int(x)
    except Exception:
        return default


class SignalClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.target_channel_id = _get_env_channel_id()
        self._user_id: Optional[int] = None

    async def setup_hook(self) -> None:
        pass

    async def on_ready(self):
        self._user_id = self.user.id if self.user else None
        log.info(
            "[READY] Logged in as %s | target CHANNEL_ID=%s",
            f"{self.user}#{self.user.discriminator}" if self.user else "(unknown)",
            str(self.target_channel_id),
        )
        # Friendly hello in the channel (ignore our own message later)
        if self.target_channel_id:
            ch = self.get_channel(self.target_channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send("👋 Ready. Post a signal.")
                    log.info("[READY] Sent hello message successfully.")
                except Exception:
                    log.info("[DROP] our own message")

            # Log channel resolve
            try:
                if ch:
                    typename = getattr(ch, "type", None)
                    parent_id = getattr(ch, "category_id", None) or getattr(ch, "parent_id", None)
                    log.info(
                        "[READY] Resolved channel: %s type=%s parent_id=%s",
                        getattr(ch, "name", "(unknown)"),
                        str(typename),
                        str(parent_id),
                    )
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        try:
            # Skip DMs, threads, or channels other than the configured one (if set)
            if self.target_channel_id and message.channel.id != self.target_channel_id:
                return

            # Drop our own messages
            if self._user_id and message.author.id == self._user_id:
                log.info("[DROP] our own message")
                return

            content = (message.content or "").strip()
            author = getattr(message.author, "name", "unknown")
            log.info(
                "[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%s",
                str(message.id),
                author,
                str(message.channel.id),
                getattr(message.channel, "name", "(unknown)"),
                str(len(content)),
            )

            # Parse
            parsed = parse_signal(content)
            side = _norm_side(getattr(parsed, "side", None))
            symbol = _norm_symbol(getattr(parsed, "symbol", None))
            entry_low, entry_high = _coerce_entry_band(parsed)

            stop_loss = _maybe_float(getattr(parsed, "stop_loss", None))
            leverage = _maybe_float(getattr(parsed, "leverage", None))
            tpn = _maybe_int(getattr(parsed, "tpn", None))
            timeframe = getattr(parsed, "timeframe", None)
            tif = getattr(parsed, "tif", None) or getattr(parsed, "tif_str", None)

            log.info(
                "[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%s lev=%s TF=%s",
                str(side), str(symbol), str(entry_low), str(entry_high),
                str(stop_loss), str(tpn), str(leverage), str(timeframe),
            )

            # Must have side, symbol, and an entry band
            if not side or not symbol:
                log.warning("[SKIP] Parser did not provide side/symbol (got side=%s, symbol=%s).", side, symbol)
                return
            if entry_low is None or entry_high is None:
                log.warning(
                    "[SKIP] Parser did not provide an entry band (got entry_low=%s, entry_high=%s).",
                    entry_low, entry_high
                )
                return

            # Build ExecSignal exactly as execution.py expects
            exec_sig = ExecSignal(
                side=side,
                symbol=symbol,
                entry_low=float(entry_low),
                entry_high=float(entry_high),
                stop_loss=stop_loss,
                leverage=leverage,
                tpn=tpn,
                timeframe=timeframe,
                tif=tif,
            )

            # Execute
            execute_signal(exec_sig)

        except Exception as e:
            log.error("[ERR] on_message: %s", e, exc_info=True)


def start() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set.")

    intents = discord.Intents.default()
    intents.message_content = True

    client = SignalClient(intents=intents)
    client.run(token)
