# discord_listener.py
import os
import sys
import time
import logging
import asyncio
import sqlite3
from typing import Optional

import discord

# ---- Logging
log = logging.getLogger("discord_listener")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
if not log.handlers:
    log.addHandler(_handler)
log.propagate = False

# ---- Env
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
WATCH_CHANNEL_IDS = [s.strip() for s in (os.getenv("WATCH_CHANNEL_IDS", "") or "").split(",") if s.strip()]
WATCH_CHANNEL_IDS = [int(x) for x in WATCH_CHANNEL_IDS if x.isdigit()]

# ---- Idempotency config
_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", "604800"))  # 7 days
_IDEMP_DB_PATH = os.getenv("IDEMP_DB_PATH", "/tmp/discord_idemp.db")
_IDEMP_LOCKFILE = os.getenv("IDEMP_LOCKFILE", "/tmp/discord_idemp.lock")

_PROC_TAG = os.getenv("DYNO") or os.getenv("RENDER_INSTANCE_ID") or hex(abs(hash(os.getpid())) & 0xFFFFFFFF)[2:]
_PROCESSED_LOCAL: set[str] = set()

# ---- Optional Redis (RECOMMENDED ACROSS MULTIPLE CONTAINERS)
_IDEMP_REDIS_URL = os.getenv("IDEMP_REDIS_URL", "").strip()
_redis = None
_REDIS_REQUIRED = bool(_IDEMP_REDIS_URL)  # if set, we make Redis mandatory (no sqlite fallback)
_REDIS_OK = False

if _IDEMP_REDIS_URL:
    try:
        import redis  # ensure 'redis>=5.0.0' is in requirements.txt
        _redis = redis.Redis.from_url(_IDEMP_REDIS_URL, decode_responses=True)
        # health check
        _redis.ping()
        _REDIS_OK = True
        log.info("[IDEMP] Redis connected ✓")
    except Exception as e:
        _REDIS_OK = False
        log.exception("[IDEMP] Redis connection failed — trades will be SKIPPED until fixed: %s", e)

# ---- Optional file lock (Linux) for sqlite mode
_filelock_supported = True
try:
    import fcntl  # not available on Windows
except Exception:
    _filelock_supported = False

# ---- Your modules
from parser import parse_signal
from execution import execute_signal


# =========================
# Idempotency helpers
# =========================
def _redis_claim_msg(msg_id: str) -> Optional[bool]:
    """True=claimed, False=duplicate, None=redis unavailable/error."""
    if not (_redis and _REDIS_OK):
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
        log.exception("[IDEMP][redis] error: %s", e)
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
        log.exception("[IDEMP][sqlite] error (best-effort proceed): %s", e)
        return True
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass
        if _filelock_supported and lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()
            except Exception:
                pass


def claim_discord_message(msg_id: str) -> bool:
    """
    Returns True if this process is the first to handle this Discord message; else False.
    Behavior:
      - If IDEMP_REDIS_URL is set: Redis is MANDATORY. If Redis not reachable in this pod, SKIP.
      - If IDEMP_REDIS_URL is not set: use SQLite (single-container only).
    """
    if not msg_id:
        return True

    # Process-local fast path
    if msg_id in _PROCESSED_LOCAL:
        log.info("[IDEMP][proc] duplicate message %s -> skip", msg_id)
        return False

    # Redis mode (mandatory when configured)
    if _REDIS_REQUIRED:
        r = _redis_claim_msg(msg_id)
        if r is True:
            _PROCESSED_LOCAL.add(msg_id)
            return True
        if r is False:
            return False
        # r is None -> Redis unavailable here -> SKIP to avoid duplicates
        log.warning("[IDEMP] Redis configured but unavailable in this pod; SKIPPING message %s", msg_id)
        return False

    # SQLite mode (only when Redis not configured)
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
        tag = f"{self.user}"
        try:
            # discord.py v2 may not use discriminators
            disc = getattr(self.user, "discriminator", None)
            if disc and disc != "0":
                tag = f"{self.user}#{disc}"
        except Exception:
            pass
        log.info("[READY][proc=%s] Logged in as %s | watching=[%s]", _PROC_TAG, tag, watching)

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

            # *** Claim BEFORE parsing/executing ***
            if not claim_discord_message(msg_id):
                log.info("[EXEC][proc=%s] SKIP: already processed (or Redis unavailable) message_id=%s",
                         _PROC_TAG, msg_id)
                return

            # Parse signal
            sig = parse_signal(message.content or "")
            if not sig:
                return

            # Attach stable client_id for downstream HL module
            try:
                setattr(sig, "client_id", f"discord:{msg_id}")
            except Exception:
                pass

            # Pretty log
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

            # Execute
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
    intents.message_content = True
    client = Listener(intents=intents)
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
