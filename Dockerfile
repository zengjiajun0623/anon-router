# anon-router: payment-private inference proxy (router + deposit watcher)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

# Persist ecash state + mint master key on a mounted volume (Railway: mount at /data).
ENV STATE_DB_PATH=/data/state.db \
    MINT_MASTER_PATH=/data/mint_master.hex \
    WATCHER_CURSOR=/data/.watcher_cursor

# Safe production defaults (override in the platform env as needed).
ENV DEV_FAUCET=0 \
    CHANNEL_LANE_ENABLED=0 \
    DAILY_USD_CAP=25 \
    ACCOUNT_RATE_PER_MIN=120

CMD ["./start.sh"]
