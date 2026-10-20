# A simulated managed device: plain Ubuntu with cron.
#
# No init system and no privileged flag needed, because the watchdog is a cron
# entry rather than a systemd service. This is the practical payoff of that
# design decision.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# python3 is what the watchdog needs. pip and nodejs are here only so the Python
# and npm collectors have something real to inventory -- they are properties of
# this test fixture, not dependencies of the watchdog.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cron \
        python3 \
        python3-pip \
        nodejs \
        npm \
        default-jdk-headless \
        ca-certificates \
        curl \
        procps \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# A couple of global packages so the collectors have interesting output, and so
# the end-to-end test has something to add and detect later.
# No --break-system-packages: Ubuntu 22.04 ships pip 22, which predates that
# flag, and has no PEP 668 marker that would require it.
RUN pip3 install --no-cache-dir requests==2.31.0 \
    && npm install -g --silent json@11.0.0

# Mounted read-only by Compose; the installer is run from here with
# --from-source during development.
RUN mkdir -p /opt/sentinel-src

CMD ["cron", "-f"]
