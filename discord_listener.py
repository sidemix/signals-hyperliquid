import os, sys, time, logging, sqlite3
from typing import Optional
import discord

log = logging.getLogger("discord_listener")
log.setLevel(logging.INFO)
h = logging.StreamHandler(sys.stdout); h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
if not log.handlers: log.addHandler(h)
log.propagate = False

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
WATCH_CHANNEL_IDS = [int(x) for x in [s.strip() for s in (os.getenv("WATCH_CHANNEL_IDS","") or "").split(",") if s.strip()] if str(x).isdigit()]

_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS","604800"))
_IDEMP_DB_PATH  = os.getenv("IDEMP_DB_PATH","/tmp/discord_idemp.db")
_IDEMP_LOCKFILE = os.getenv("IDEMP_LOCKFILE","/tmp/discord_idemp.lock")

_PROC_TAG = os.getenv("DYNO") or os.getenv("RENDER_INSTANCE_ID") or hex(abs(hash(os.getpid())) & 0xFFFFFFFF)[2:]
_PROCESSED_LOCAL: set[str] = set()

_IDEMP_REDIS_URL = os.getenv("IDEMP_REDIS_URL","").strip()
_redis = None; _REDIS_REQUIRED = bool(_IDEMP_REDIS_URL); _REDIS_OK = False
if _IDEMP_REDIS_URL:
    try:
        import redis
        _redis = redis.Redis.from_url(_IDEMP_REDIS_URL, decode_responses=True)
        _redis.ping(); _REDIS_OK = True
        log.info("[IDEMP] Redis connected ✓")
    except Exception as e:
        _REDIS_OK = False; log.exception("[IDEMP] Redis connection failed — trades will be SKIPPED until fixed: %s", e)

_filelock_supported = True
try: import fcntl
except Exception: _filelock_supported = False

from parser import parse_signal
from execution import execute_signal

def _redis_claim_msg(msg_id:str)->Optional[bool]:
    if not (_redis and _REDIS_OK): return None
    try:
        ok = _redis.set(f"discord:msg:{msg_id}", "1", nx=True, ex=_IDEMP_TTL_SECS)
        if ok: log.info("[IDEMP][redis] claimed message %s", msg_id); return True
        log.info("[IDEMP][redis] duplicate message %s -> skip", msg_id); return False
    except Exception as e:
        log.exception("[IDEMP][redis] error: %s", e); return None

def _sqlite_conn():
    conn = sqlite3.connect(_IDEMP_DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;"); conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS processed_msgs (msg_id TEXT PRIMARY KEY, ts INTEGER NOT NULL)")
    return conn

def _sqlite_claim_msg(msg_id:str)->bool:
    now = int(time.time()); conn=None; lockf=None
    try:
        if _filelock_supported:
            lockf=open(_IDEMP_LOCKFILE,"a+"); fcntl.flock(lockf, fcntl.LOCK_EX)
        conn=_sqlite_conn()
        conn.execute("DELETE FROM processed_msgs WHERE ts<?",(now-_IDEMP_TTL_SECS,))
        conn.execute("INSERT INTO processed_msgs (msg_id,ts) VALUES (?,?)",(msg_id,now))
        log.info("[IDEMP][sqlite] claimed message %s", msg_id); return True
    except sqlite3.IntegrityError:
        log.info("[IDEMP][sqlite] duplicate message %s -> skip", msg_id); return False
    except Exception as e:
        log.exception("[IDEMP][sqlite] error (best-effort proceed): %s", e); return True
    finally:
        try: conn and conn.close()
        except: pass
        if _filelock_supported and lockf:
            try: fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()
            except: pass

def claim_discord_message(msg_id:str)->bool:
    if not msg_id: return True
    if msg_id in _PROCESSED_LOCAL:
        log.info("[IDEMP][proc] duplicate message %s -> skip", msg_id); return False
    if _REDIS_REQUIRED:
        r=_redis_claim_msg(msg_id)
        if r is True: _PROCESSED_LOCAL.add(msg_id); return True
        if r is False: return False
        log.warning("[IDEMP] Redis configured but unavailable in this pod; SKIPPING message %s", msg_id); return False
    ok=_sqlite_claim_msg(msg_id)
    if ok: _PROCESSED_LOCAL.add(msg_id)
    return ok

class Listener(discord.Client):
    async def setup_hook(self): log.info("[BOOT][proc=%s] Starting Discord client…", _PROC_TAG)
    async def on_ready(self):
        watching=",".join(str(x) for x in WATCH_CHANNEL_IDS) or "<all?>"
        tag=f"{self.user}"
        try:
            disc=getattr(self.user,"discriminator",None)
            if disc and disc!="0": tag=f"{self.user}#{disc}"
        except: pass
        log.info("[READY][proc=%s] Logged in as %s | watching=[%s]", _PROC_TAG, tag, watching)

    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot: return
            if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS: return
            msg_id=str(message.id)
            log.info("[RX][proc=%s] ch=%s by=%s id=%s len=%s", _PROC_TAG, message.channel.id, getattr(message.author,"name","?"), msg_id, len(message.content or ""))
            if not claim_discord_message(msg_id):
                log.info("[EXEC][proc=%s] SKIP: already processed (or Redis unavailable) message_id=%s", _PROC_TAG, msg_id); return
            if not claim_discord_message(msg_id):
            log.info("[EXEC][proc=%s] SKIP: already processed (or Redis unavailable) message_id=%s", _PROC_TAG, msg_id)
            return

            try: setattr(sig,"client_id",f"discord:{msg_id}")
            except: pass
            log.info("[PARSER][proc=%s] side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tif=%s client_id=%s",
                     _PROC_TAG, getattr(sig,"side",None), getattr(sig,"symbol",None),
                     getattr(sig,"entry_low",None), getattr(sig,"entry_high",None),
                     getattr(sig,"stop_loss",None), getattr(sig,"leverage",None),
                     getattr(sig,"tif",None), f"discord:{msg_id}")
            execute_signal(sig); log.info("[EXEC][proc=%s] execute_signal returned: OK", _PROC_TAG)
        except Exception as e:
            log.exception("[ERR][proc=%s] on_message failed: %s", _PROC_TAG, e)

def main():
    if not DISCORD_BOT_TOKEN: raise RuntimeError("Set DISCORD_BOT_TOKEN")
    intents=discord.Intents.default(); intents.message_content=True
    Listener(intents=intents).run(DISCORD_BOT_TOKEN)

if __name__=="__main__": main()
