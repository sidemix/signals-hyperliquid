import sys, logging
root = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(h)
root.setLevel(logging.INFO)

log = logging.getLogger("execution")
from hyperliquid import submit_signal as hl_submit

def execute_signal(sig)->None:
    try:
        log.info("[EXEC] Dispatching to Hyperliquid: side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tif=%s client_id=%s",
                 getattr(sig,"side",None), getattr(sig,"symbol",None),
                 getattr(sig,"entry_low",None), getattr(sig,"entry_high",None),
                 getattr(sig,"stop_loss",None), getattr(sig,"leverage",None),
                 getattr(sig,"tif",None), getattr(sig,"client_id",None))
        hl_submit(sig)
    except Exception as e:
        log.exception("[EXEC] ERROR in execute_signal: %s", e); raise
