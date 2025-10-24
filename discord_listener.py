import os
import logging

from bootcheck import run_startup_checks
run_startup_checks()
# … then your normal bot setup/imports/logging

from typing import Optional, Tuple

import discord

from execution import ExecSignal, execute_signal
from parser import parse_signal  # your existing parser



log = logging.getLogger("discord_listener")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

def _env_int(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except Exception:
        return None

def _coerce_entry_band(parsed) -> Tuple[Optional[float], Optional[float]]:
    # entry_low / entry_high
    low = getattr(parsed, "entry_low", None)
    high = getattr(parsed, "entry_high", None)
    if low is not None and high is not None:
        try:
            return float(low), float(high)
        except Exception:
            pass
    # band_low / band_high
    low = getattr(parsed, "band_low", None)
    high = getattr(parsed, "band_high", None)
    if low is not None and high is not None:
        try:
            return float(low), float(high)
        except Exception:
            pass
    # entry_band tuple
    band = getattr(parsed, "entry_band", None)
    if isinstance(band, (tuple, list)) and len(band) == 2:
        try:
            return float(band[0]), float(band[1])
        except Exception:
            pass
    return None, None

def _norm_side(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s2 = s.strip().upper()
    return s2 if s2 in ("LONG", "SHORT") else None

def _norm_symbol(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return s.strip().upper()

def _maybe_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None

def _maybe_int(x) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None

class SignalClient(discord.Client):
    def __init__(self, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self.target_channel_id = _env_int("CHANNEL_ID")
        self._self_id: Optional[int] = None

    async def on_ready(self):
        self._self_id = self.user.id if self.user else None
        log.info(
            "[READY] Logged in as %s | target CHANNEL_ID=%s",
            str(self.user), str(self.target_channel_id)
        )
        if self.target_channel_id:
            ch = self.get_channel(self.target_channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send("👋 Ready. Post a signal.")
                    log.info("[READY] Sent hello message successfully.")
                except Exception:
                    log.info("[DROP] our own message")
            try:
                if ch:
                    log.info(
                        "[READY] Resolved channel: %s type=%s",
                        getattr(ch, "name", "(unknown)"), str(getattr(ch, "type", None))
                    )
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        try:
            if self.target_channel_id and message.channel.id != self.target_channel_id:
                return
            if self._self_id and message.author.id == self._self_id:
                log.info("[DROP] our own message")
                return

            content = (message.content or "").strip()
            log.info(
                "[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%s",
                str(message.id),
                getattr(message.author, "name", "unknown"),
                str(message.channel.id),
                getattr(message.channel, "name", "(unknown)"),
                str(len(content)),
            )

            parsed = parse_signal(content)

            side = _norm_side(getattr(parsed, "side", None))
            symbol = _norm_symbol(getattr(parsed, "symbol", None))
            entry_low, entry_high = _coerce_entry_band(parsed)

            # accept both tf and timeframe
            timeframe = getattr(parsed, "timeframe", None)
            if timeframe is None:
                timeframe = getattr(parsed, "tf", None)

            stop_loss = _maybe_float(getattr(parsed, "stop_loss", None) or getattr(parsed, "sl", None))
            leverage  = _maybe_float(getattr(parsed, "leverage", None) or getattr(parsed, "lev", None))
            tpn       = _maybe_int(getattr(parsed, "tpn", None))
            tif       = getattr(parsed, "tif", None) or getattr(parsed, "tif_str", None)

            log.info(
                "[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%s lev=%s TF=%s",
                str(side), str(symbol), str(entry_low), str(entry_high),
                str(stop_loss), str(tpn), str(leverage), str(timeframe),
            )

            if not side or not symbol:
                log.warning("[SKIP] Parser missing side/symbol.")
                return
            if entry_low is None or entry_high is None:
                log.warning("[SKIP] Parser missing entry band.")
                return

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

            execute_signal(exec_sig)

        except Exception as e:
            log.error("[ERR] on_message: %s", e, exc_info=True)

def start() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    intents = discord.Intents.default()
    intents.message_content = True
    client = SignalClient(intents)
    client.run(token)
