FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .

# hard-clean any cached/conflicting HL wheels, then install
RUN pip install --upgrade pip && \
    pip uninstall -y hyperliquid hyperliquid-python-sdk || true && \
    pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "-u", "main.py"]
