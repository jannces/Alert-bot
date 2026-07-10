FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The scanner is notification-only: it needs no exchange credentials.
# Provide WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID at runtime.
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

CMD ["python", "main.py", "--config", "config/config.yaml"]
