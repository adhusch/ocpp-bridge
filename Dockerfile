FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8887

WORKDIR /opt/ocpp-bridge
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 1000 --home /opt/ocpp-bridge bridge \
 && mkdir -p /data && chown bridge /data

COPY app ./app

USER bridge
VOLUME ["/data"]
EXPOSE 8887

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8887\")}/health', timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
