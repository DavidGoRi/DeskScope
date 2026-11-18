# DeskScope in a container: one process serves the web app and runs the
# mail/notification worker. Data (database, secret key, sla.json,
# notifications.json) lives in the /data volume.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DESKSCOPE_DB=/data/deskscope.db

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin deskscope \
    && mkdir -p /data && chown deskscope:deskscope /data

USER deskscope
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# Put a TLS reverse proxy in front and add --secure-cookies (see docker-compose.yml).
CMD ["deskscope", "serve", "--host", "0.0.0.0", "--port", "8000", "--worker-every", "60"]
