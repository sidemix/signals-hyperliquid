import os
import asyncio
import inspect
import discord

from parser import parse_signal_from_text
import execution as _exec  # we'll pick the right function from here dynamically

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")
DEBUG = str(os.getenv("DEBUG", "")).lower() in ("1", "true", "yes", "on")


def _log(msg: str):
    if DEBUG:
        print(f"[listener] {msg}", flush=True)


# ---- find an executable function inside execution.py ----
_CANDIDATES = (
    "execute_signal",        # our preferred entrypoint
    "handle_signal",
    "run_signal",
    "place_from_signal",
    "process_signal",
    "run_oto_signal",
)
EXECUTE_FN = None
for _name in _CANDIDATES:
    if hasattr(_exec, _name):
        EXECUTE_FN = getattr(_exec, _name)
        _log(f"using execution function: execution.{_name}")
        break

if EXECUTE_FN is None:
    raise ImportError(
        "discord_listener: no execution function found in execution.py. "
        f"Tried: {', '.join(_CANDIDATES)}. "
        "Export one of these or `execute_signal(sig)`."
    )


def _extract_text_from_message(message: discord.Message) -> str:
    parts = []
    if message.content:
        parts.append(message.content)
    for emb in message.embeds or []:
        if emb.title:
            parts.append(emb.title)
        if emb.description:
            parts.append(emb.description)
        for f in emb.fields or []:
            if f.name:
                parts.append(str(f.name))
            if f.value:
                parts.append(str(f.value))
        try:
            ft = getattr(emb.footer, "text", None)
            if ft:
                parts.append(ft)
        except Exception:
            pass
    return "\n".join(p for p in parts if p)


class SignalClient(discord.Client):
    async def on_ready(self):
        print(
            f"Logged in as {self.user} and listening to channel {DISCORD_CHANNEL_ID}",
            flush=True,
        )

    async def on_message(self, message: discord.Message):
        # right channel?
        if not DISCORD_CHANNEL_ID or message.channel.id != DISCORD_CHANNEL_ID:
            return
        # ignore our own posts
        if message.author == self.user:
            return

        raw = _extract_text_from_message(message)
        if not raw.strip():
            _log("skip: empty message/embeds")
            return

        sig = parse_signal_from_text(raw)
        if not sig:
            _log("skip: parser returned None (format didn’t match)")
            return

        _log(
            f"parsed: {sig.symbol} {sig.side} entry={sig.entry_band} "
            f"sl={sig.stop} tps={sig.take_profits[:3]}..."
        )

        try:
            if inspect.iscoroutinefunction(EXECUTE_FN):
                await EXECUTE_FN(sig)
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, EXECUTE_FN, sig)
        except Exception as e:
            _log(f"execute error: {e}")


def start():
    """Entry point used by main.py"""
    intents = discord.Intents.default()
    intents.message_content = True  # also enable this in the Discord Dev Portal
    client = SignalClient(intents=intents)
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    start()

