FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app liva \
    && mkdir -p /data && chown liva /data

WORKDIR /app

# A separate image on purpose: no psycopg and none of the server's code, so Liva
# can reach Sentinel only through its API.
COPY liva/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY liva/ ./

USER liva
VOLUME /data
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8100/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
