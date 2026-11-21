FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HOME=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install . \
    && useradd --uid 1000 --home-dir /data --no-create-home zmm \
    && mkdir -p /data \
    && chown zmm:zmm /data

USER zmm

# Garmin tokens, SQLite state and FIT backups.
VOLUME ["/data"]

EXPOSE 8126

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8126/health', timeout=4)" || exit 1

ENTRYPOINT ["zwift-map-merge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8126"]
