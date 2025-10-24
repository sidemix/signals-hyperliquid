# ---- Base image ----
FROM python:3.11-slim

# ---- Runtime env ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---- Workdir ----
WORKDIR /app

# ---- De-bake any preinstalled hyperliquid copies (optional) ----
# (This block must come AFTER FROM.)
RUN python - <<'PY'
import pkgutil, sys, subprocess
mods = {m.name for m in pkgutil.iter_modules()}
for bad in ("hyperliquid",):
    if bad in mods:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", bad], check=False)
PY

# ---- Dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- App code ----
COPY . .

# ---- Entrypoint ----
CMD ["python", "-u", "discord_listener.py"]
