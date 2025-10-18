import os, asyncio, discord
from parser import parse_signal_from_text
from execution import Executor

CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
executor = Executor()

@client.event
async def on_ready():
    print(f"Logged in as {client.user} and listening to channel {CHANNEL_ID}")

@client.event
async def on_message(message: discord.Message):
    if message.channel.id != CHANNEL_ID:
        return

    text = message.content or ""
    if message.embeds:
        for e in message.embeds:
            text += "\n" + (e.title or "") + "\n" + (e.description or "")
            for f in e.fields:
                text += f"\n{f.name}: {f.value}"

    sig = parse_signal_from_text(text)
    if not sig:
        return

    # execute in a worker thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, executor.execute_signal_oto, sig)

def start():
    if not TOKEN:
        print("[FATAL] DISCORD_BOT_TOKEN is missing")
        import sys; sys.exit(1)
    if CHANNEL_ID == 0:
        print("[WARN] DISCORD_CHANNEL_ID is 0 (listener will ignore all messages)")
    client.run(TOKEN)
