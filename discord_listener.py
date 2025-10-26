# discord_listener.py
import os
import sys
import time
import logging
import asyncio
import sqlite3
from typing import Optional

import discord

# ---- Optional Redis for cross-container idempotency
_IDEMP_REDIS_URL = os.getenv("IDEMP_REDIS_URL", "").strip()
_redis = None
if _IDEMP_REDIS_URL:
    try:
        import redis  # make sure 'redis>=5.0.0' is in requirements.txt
        _redis = redis.Redis.from_url(_IDEMP_REDIS_URL, decode_responses=True)
    except Exception as e:
        # Don't crash if Redis isn't reachable; we will fall back to SQLite.
        _redis = None

# ---- Optional file lock (Linux)
_filelock_supported = True
try:
    import fcntl  # not available on Windows
except Exception:
    _filelock_supported = False

# ---- Your code modules
# Expect these modules to exist in your project:
#  - parser.parse_signal(text) -> Sig or None
#  - execution.execute_signal(sig) -> any
from parser import parse_signal
from execution import execute_signal

# ---- Logging
log = logging.getLogger("discord_listener")
log.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
if not log.handlers:
    log.addHandler(handler)
log.propagate = False

# ---- Env
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
WATCH_CHANNEL_IDS = [s.strip() for s in (os.getenv("WATCH_CHANNEL_IDS", "") or "").split(",") if s.strip()]
WATCH_CHANNEL_IDS = [int(x) for x in WATCH_CHANNEL_IDS if x.isdigit()]

# Idempotency storage (listener level)
_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", "604800"))  # 7 days by default
_IDEMP_DB_PATH = os.getenv("IDEMP_DB_PATH", "/tmp/discord_idemp.db")
_IDEMP_LOCKFILE = os.getenv("IDEMP_LOCKFILE", "/tmp/discord_idemp.lock")

# Process tag for logs
_PROC_TAG = os.getenv("DYNO") or os.getenv("RENDER_INSTANCE_ID") or hex(abs(hash(os.getpid())) & 0xFFFFFFFF)[2:]

# In-process belt & suspenders
_PROCESSED_LOCAL: set[str] = set()


# =========================
# Idempotency helpers
# =========================
def _redis_claim_msg(msg_id: str) -> Optional[bool]:
    """Returns True if claimed, False if duplicate, None if Redis unavailable/error."""
    if not _redis:
        return None
    try:
        key = f"discord:msg:{msg_id}"
        ok = _redis.set(name=key, value="1", nx=True, ex=_IDEMP_TTL_SECS)
        if ok:
            log.info("[IDEMP][redis] claimed message %s", msg_id)
            return True
        log.info("[IDEMP][redis] duplicate message %s -> skip", msg_id)
        return False
    except Exception as e:
        log.exception("[IDEMP][redis] error (fallback to sqlite): %s", e)
        return None


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_IDEMP_DB_PATH, timeout=10, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_msgs (
            msg_id TEXT PRIMARY KEY,
            ts INTEGER NOT NULL
        )
    """)
    return conn


def _sqlite_claim_msg(msg_id: str) -> bool:
    """Returns True if we inserted the msg_id (first), False if already there."""
    now = int(time.time())
    conn = None
    lockf = None
    try:
        if _filelock_supported:
            lockf = open(_IDEMP_LOCKFILE, "a+")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        conn = _sqlite_conn()
        conn.execute("DELETE FROM processed_msgs WHERE ts < ?", (now - _IDEMP_TTL_SECS,))
        conn.execute("INSERT INTO processed_msgs (msg_id, ts) VALUES (?, ?)", (msg_id, now))
        log.info("[IDEMP][sqlite] claimed message %s", msg_id)
        return True
    except sqlite3.IntegrityError:
        log.info("[IDEMP][sqlite] duplicate message %s -> skip", msg_id)
        return False
    except Exception as e:
        # If SQLite is broken, allow one path to continue (avoid outage).
        log.exception("[IDEMP][sqlite] error (best-effort proceed): %s", e)
        return True
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        if _filelock_supported and lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
            except Exception:
                pass


def claim_discord_message(msg_id: str) -> bool:
    """
    Returns True if *this process* is the first to handle this Discord message, else False.
    """
    if not msg_id:
        return True
    if msg_id in _PROCESSED_LOCAL:
        log.info("[IDEMP][proc] duplicate message %s -> skip", msg_id)
        return False

    # Cross-container first (Redis)
    r = _redis_claim_msg(msg_id)
    if r is True:
        _PROCESSED_LOCAL.add(msg_id)
        return True
    if r is False:
        return False

    # Fallback (same container) SQLite+file lock
    ok = _sqlite_claim_msg(msg_id)
    if ok:
        _PROCESSED_LOCAL.add(msg_id)
    return ok


# =========================
# Discord client
# =========================
class Listener(discord.Client):
    async def setup_hook(self) -> None:
        log.info("[BOOT][proc=%s] Starting Discord client…", _PROC_TAG)

    async def on_ready(self):
        watching = ",".join(str(x) for x in WATCH_CHANNEL_IDS) or "<all?>"
        log.info("[READY][proc=%s] Logged in as %s | watching=[%s]",
                 _PROC_TAG, f"{self.user}#{self.user.discriminator if hasattr(self.user,'discriminator') else ''}", watching)

    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot:
                return
            if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
                return

            msg_id = str(message.id)
            ch = message.channel.id
            author = getattr(message.author, "name", "unknown")
            content_len = len(message.content or "")
            log.info("[RX][proc=%s] ch=%s by=%s id=%s len=%s",
                     _PROC_TAG, ch, author, msg_id, content_len)

            # *** CRITICAL: claim the message BEFORE parsing/executing ***
            if not claim_discord_message(msg_id):
                log.info("[EXEC][proc=%s] SKIP: already processed message_id=%s", _PROC_TAG, msg_id)
                return

            # Parse signal (your existing parser)
            sig = parse_signal(message.content or "")
            if not sig:
                return

            # Attach a stable client_id so downstream can also dedupe (good backstop)
            try:
                setattr(sig, "client_id", f"discord:{msg_id}")
            except Exception:
                pass

            # Log the parsed signal similarly to your current style
            try:
                side = getattr(sig, "side", None)
                symbol = getattr(sig, "symbol", None)
                e_low = getattr(sig, "entry_low", None)
                e_high = getattr(sig, "entry_high", None)
                sl = getattr(sig, "stop_loss", None)
                lev = getattr(sig, "leverage", None)
                tif = getattr(sig, "tif", None)
                log.info("[PARSER][proc=%s] side=%s symbol=%s band=(%s, %s) sl=%s lev=%s tif=%s client_id=%s",
                         _PROC_TAG, side, symbol, e_low, e_high, sl, lev, tif, f"discord:{msg_id}")
            except Exception:
                pass

            # Execute (your existing executor -> Hyperliquid)
            execute_signal(sig)
            log.info("[EXEC][proc=%s] execute_signal returned: OK", _PROC_TAG)

        except Exception as e:
            log.exception("[ERR][proc=%s] on_message failed: %s", _PROC_TAG, e)


# =========================
# Entrypoint
# =========================
def main():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Set DISCORD_BOT_TOKEN")
    intents = discord.Intents.default()
    intents.message_content = True  # required if bot has the Message Content intent
    client = Listener(intents=intents)
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
