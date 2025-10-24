FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .

# Guarantee a clean HL install: wipe any pre-baked copy from the base image
RUN python - <<'PY'
import pkgutil, sys, subprocess
mods = [m.name for m in pkgutil.iter_modules()]
for bad in ("hyperliquid_python_sdk", "hyperliquid_python", "hl_sdk"):
    if bad in mods:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", bad], check=False)
PY

RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "-u", "discord_listener.py"]
