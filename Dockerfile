FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# (Optional) nuke any prebaked hyperliquid module
RUN python - <<'PY'
import pkgutil, sys, subprocess
mods = {m.name for m in pkgutil.iter_modules()}
for bad in ("hyperliquid",):
    if bad in mods:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", bad], check=False)
PY

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-u", "discord_listener.py"]
