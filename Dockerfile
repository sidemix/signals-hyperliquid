RUN python - <<'PY'
import pkgutil, sys, subprocess
mods = {m.name for m in pkgutil.iter_modules()}
for bad in ("hyperliquid",):
    if bad in mods:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", bad], check=False)
PY
