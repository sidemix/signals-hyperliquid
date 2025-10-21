# discord_listener.py
import os
import re
import discord
from typing import Optional

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID_STR = os.getenv("DISCORD_CHANNEL_ID", "").strip()
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.isdigit() else None

# Optional author allowlist; if empty we accept all authors
AUTHOR_ALLOWLIST = {a.strip().lower() for a in os.getenv("AUTHOR_ALLOWLIST", "").split(",") if a.strip()}

# ---------- intents ----------
intents = discord.Intents.none()
intents.guilds = True
intents.messages = True
intents.message_content = True  # REQUIRED to read message.content

client = discord.Client(intents=intents)

def _author_allowed(name: str) -> bool:
    if not AUTHOR_ALLOWLIST:
        return True
    return name.lower() in AUTHOR_ALLOWLIST

def _content_from_message(msg: discord.Message) -> str:
    """
    Return best-effort text to parse:
    - Prefer message.content
    - If empty (embeds), flatten embed title/description/fields into plain text
    """
    if msg.content and msg.content.strip():
        return msg.content

    parts = []
    for emb in msg.embeds:
        if emb.title:
            parts.append(str(emb.title))
        if emb.description:
            parts.append(str(emb.description))
        for f in emb.fields:
            # "Name: value" per field
            parts.append(f"{f.name}: {f.value}")
        if emb.footer and emb.footer.text:
            parts.append(str(emb.footer.text))
    return "\n".join(parts).strip()

@client.event
async def on_ready():
    try:
        print(f"[READY] Logged in as {client.user} | target CHANNEL_ID={CHANNEL_ID}")
        if CHANNEL_ID:
            ch = await client.fetch_channel(CHANNEL_ID)
            # A little extra info, very useful for mismatched IDs
            try:
                await ch.send("👋 Bot online (debug). I can read this channel.")
                print("[READY] Sent hello message successfully.")
            except Exception as e:
                print(f"[READY] Could not send hello message: {e}")
            print(f"[READY] Resolved channel: {getattr(ch, 'name', '?')} type={getattr(ch, 'type', '?')} parent_id={getattr(ch, 'category_id', None)}")
        else:
            print("[READY] WARNING: DISCORD_CHANNEL_ID not set or invalid.")
    except Exception as e:
        print(f"[READY] Error: {e}")

@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages
    if message.author == client.user:
        print(f"[DROP] our own message")
        return

    # Channel filter
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    # Verbose RX log
    chan_kind = getattr(message.channel, "type", "text")
    parent_id = getattr(message.channel, "category_id", None)
    guild_id = getattr(message.guild, "id", None)
    raw_len = len(message.content or "") if message.content else 0

    print(f"[RX] msg_id={message.id} author='{message.author.display_name or message.author.name}' "
          f"chan_id={message.channel.id} chan_name={getattr(message.channel, 'name', None)} "
          f"type={chan_kind} parent_id={parent_id} guild_id={guild_id} len={raw_len}")

    # Author allowlist (optional)
    if not _author_allowed(message.author.display_name or message.author.name):
        print(f"[SKIP] Author '{message.author.display_name or message.author.name}' not in allowlist.")
        return

    # Build parseable text (handles embeds)
    text = _content_from_message(message)
    if not text:
        print("[SKIP] Message has no text or parseable embed content.")
        return

    # Strip discord fancy punctuation en-dash
    text = text.replace("–", "-")

    # Quick sanity: make sure it contains something like 'VIP Signal'
    if "vip signal" not in text.lower():
        # Not strictly required, but avoids parsing unrelated chatter
        print("[SKIP] Not a VIP signal-looking message.")
        return

    sig = parse_signal_from_text(text)
    if not sig:
        print("[SKIP] Could not parse signal from message.")
        # Uncomment to see what was seen:
        # print("----- RAW TEXT BEGIN -----")
        # print(text)
        # print("----- RAW TEXT END -----")
        return

    print(f"[PASS] parsed: {sig.side} {sig.symbol} band={sig.entry_band} SL={sig.stop} TPn={len(sig.take_profits)} "
          f"lev={sig.leverage} TF={sig.timeframe}")

    try:
        es = ExecSignal(
            symbol=sig.symbol,
            side=sig.side,
            entry_band=sig.entry_band,
            stop=sig.stop,
            tps=sig.take_profits,
            leverage=sig.leverage,
            timeframe=sig.timeframe,
        )
        execute_signal(es)
    except Exception as e:
        print(f"[EXC] execution error: {e}")

def start():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set.")
    if not CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set or invalid.")
    client.run(DISCORD_TOKEN)
