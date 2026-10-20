FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

# cron runs the nightly prune; tini reaps the children it spawns. dpkg-dev
# provides dpkg-deb, which builds the watchdog's .deb on demand.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tini dpkg-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./

# The watchdog source is embedded into the installer this API serves, so it has to
# be present in the image. It is never executed here.
COPY watchdog/ /app/watchdog/
# Maintainer scripts and the control template for the .deb.
COPY packaging/ /app/packaging/

RUN printf '%s\n' \
    'SHELL=/bin/sh' \
    'PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/bin' \
    '0 3 * * * root cd /app && python -m app.cli prune >> /var/log/cron.log 2>&1' \
    > /etc/cron.d/sentinel-prune \
    && chmod 644 /etc/cron.d/sentinel-prune \
    && touch /var/log/cron.log

COPY server/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
