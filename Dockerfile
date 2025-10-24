FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .

# 1) Clean any leftover wheels from cache layers
# 2) Install the official SDK only
# 3) Sanity-check the SDK layout: must have wallet/Exchange/Info
RUN pip install --upgrade pip && \
    pip uninstall -y hyperliquid hyperliquid-python-sdk || true && \
    pip install --no-cache-dir -r requirements.txt && \
    python - <<'PY'
import pkgutil, importlib, sys
import hyperliquid as hl
print("HL base:", getattr(hl, "__file__", "?"))
subs = {m.name for m in pkgutil.iter_modules(hl.__path__)}
print("HL submodules:", sorted(subs))
assert "wallet" in subs, f"Bad HL install (no wallet): {sorted(subs)}"
# Import the modules we require. These MUST exist in the official SDK.
from hyperliquid.wallet import Wallet
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
print("HL import OK")
PY

COPY . .

# (your entrypoint)
CMD ["python", "-u", "main.py"]
