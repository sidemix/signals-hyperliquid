# discord_listener.py (super-verbose)
import os
import textwrap
import discord

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal, is_symbol_allowed

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in Dev Portal
client = discord.Client(intents=intents)


def _short(s: str, n: int = 300) -> str:
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def _collect_text(message: discord.Message) -> str:
    txt = message.content or ""
    # add embed pieces too
    for e in getattr(message, "embeds", []) or []:
        if e.title:
            txt += f"\n{e.title}"
        if e.description:
            txt += f"\n{e.description}"
        for f in getattr(e, "fields", []) or []:
            if f.name:
                txt += f"\n{f.name}"
            if f.value:
                txt += f"\n{f.value}"
    return txt.strip()


def _to_exec(sig):
    return ExecSignal(
        symbol=sig.symbol,
        side=sig.side,
        entry_band=sig.entry_band,
        stop=sig.stop,
        tps=sig.take_profits,
    )


@client.event
async def on_ready():
    print(f"[READY] Logged in as {client.user} | target CHANNEL_ID={CHANNEL_ID}", flush=True)

    # Try to fetch the channel and send a hello (helps verify perms + id)
    try:
        ch = client.get_channel(CHANNEL_ID)
        if ch is None:
            print("[READY] get_channel returned None; attempting fetch_channel()", flush=True)
            ch = await client.fetch_channel(CHANNEL_ID)
        print(f"[READY] Resolved channel: {type(ch).__name__} id={getattr(ch,'id',None)} "
              f"name={getattr(ch,'name',None)} parent_id={getattr(ch,'parent_id',None)}", flush=True)
        try:
            await ch.send("👋 Bot online (debug). I can read this channel.")
            print("[READY] Sent hello message successfully.", flush=True)
        except Exception as e:
            print(f"[READY] Could not send hello message: {e}", flush=True)
    except Exception as e:
        print(f"[READY] Channel resolve failed: {e}", flush=True)


@client.event
async def on_message(message: discord.Message):
    # Log *every* message we receive before filtering
    try:
        gid = getattr(getattr(message.channel, "guild", None), "id", None)
        pname = getattr(getattr(message.channel, "parent", None), "name", None)
        print(
            f"[RX] msg_id={message.id} author='{getattr(message.author,'name','?')}' "
            f"chan_id={message.channel.id} chan_name={getattr(message.channel,'name',None)} "
            f"type={type(message.channel).__name__} parent_id={getattr(message.channel,'parent_id',None)} "
            f"guild_id={gid} len={len(message.content or '')}",
            flush=True,
        )
    except Exception as e:
        print(f"[RX] pre-log error: {e}", flush=True)

    # Ignore ourselves
    if message.author == client.user:
        print("[DROP] our own message", flush=True)
        return

    # Accept messages in the configured channel...
    in_channel = (message.channel.id == CHANNEL_ID)
    # ...or in any thread whose parent is the configured channel
    parent_id = getattr(message.channel, "parent_id", None)
    in_thread = (parent_id == CHANNEL_ID)

    if not (in_channel or in_thread):
        print(f"[DROP] not target channel/thread (CHANNEL_ID={CHANNEL_ID})", flush=True)
        return

    where = "channel" if in_channel else f"thread<{message.channel.id}>"
    txt = _collect_text(message)
    print(f"[PASS] from '{getattr(message.author,'name','?')}' in {where}: {_short(txt)}", flush=True)

    sig = parse_signal_from_text(txt)
    if not sig:
        print("[PARSE] no match", flush=True)
        return

    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not allowed by HYPER_ONLY_EXECUTE_SYMBOLS", flush=True)
        return

    res = execute_signal(_to_exec(sig))
    print(f"[EXEC] {sig.symbol} {sig.side} -> {res}", flush=True)


def start():
    if not TOKEN or CHANNEL_ID == 0:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing.")
    client.run(TOKEN)
