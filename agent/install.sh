#!/usr/bin/env bash
#
# Sentinel agent installer.
#
# Self-contained: the agent source is embedded below as a base64 tar.gz, so the
# only network call made here is enrollment itself. Nothing is fetched from apt
# or PyPI.
#
#   sudo ./sentinel-install.sh
#   sudo ./sentinel-install.sh --server https://sentinel.example --key sk_dev_...
#   sudo ./sentinel-install.sh --uninstall
#
set -euo pipefail

# Values baked in by the dashboard download; empty in the generic installer,
# which prompts instead.
SERVER_URL='@@SERVER_URL@@'
API_KEY='@@API_KEY@@'
INSECURE='@@INSECURE@@'
AGENT_BUNDLE_B64='@@BUNDLE@@'

# When this script is run straight from the repository the placeholders above
# are still literal. Treat them as unset so --from-source development works.
case "$SERVER_URL" in '@@'*) SERVER_URL="" ;; esac
case "$API_KEY" in '@@'*) API_KEY="" ;; esac
case "$INSECURE" in '@@'*) INSECURE="" ;; esac
case "$AGENT_BUNDLE_B64" in '@@'*) AGENT_BUNDLE_B64="" ;; esac

AGENT_USER="sentinel"
INSTALL_DIR="/opt/sentinel-agent"
CONFIG_DIR="/etc/sentinel-agent"
STATE_DIR="/var/lib/sentinel-agent"
LOG_DIR="/var/log/sentinel-agent"
CRON_FILE="/etc/cron.d/sentinel-agent"
SYMLINK="/usr/local/bin/sentinel-agent"

FROM_SOURCE=""
FORCE_REENROLL=0
DO_UNINSTALL=0
SKIP_ENROLL=0

# Tracks whether this run created the install, so a failed enrollment can clean
# up after itself rather than leaving a half-configured device.
CREATED_INSTALL=0

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
if [ ! -t 1 ]; then RED=""; GREEN=""; YELLOW=""; BOLD=""; RESET=""; fi

info()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()    { printf '%s  ok%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()   { printf '%sfail%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Sentinel agent installer.

Self-contained: the agent source is embedded as a base64 bundle, so the only
network call made here is enrollment itself.

  sudo ./sentinel-install.sh
  sudo ./sentinel-install.sh --server https://sentinel.example --key sk_dev_...
  sudo ./sentinel-install.sh --uninstall

Options:
  --server URL        Platform base URL
  --key KEY           Device API key from the dashboard
  --insecure          Allow a plain-HTTP server URL (local testing only)
  --from-source DIR   Install from a local agent directory instead of the
                      embedded bundle (development)
  --force-reenroll    Re-enroll even if this device is already enrolled
  --uninstall         Remove the agent, leaving the device record on the server
  -h, --help          Show this help
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --server)         SERVER_URL="${2:-}"; shift 2 ;;
            --server=*)       SERVER_URL="${1#*=}"; shift ;;
            --key)            API_KEY="${2:-}"; shift 2 ;;
            --key=*)          API_KEY="${1#*=}"; shift ;;
            --insecure)       INSECURE=1; shift ;;
            --from-source)    FROM_SOURCE="${2:-}"; shift 2 ;;
            --from-source=*)  FROM_SOURCE="${1#*=}"; shift ;;
            --force-reenroll) FORCE_REENROLL=1; shift ;;
            --uninstall)      DO_UNINSTALL=1; shift ;;
            -h|--help)        usage; exit 0 ;;
            *)                die "unknown option: $1 (try --help)" ;;
        esac
    done
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root; try: sudo $0 $*"
}

preflight() {
    info "Checking prerequisites"

    [ -r /etc/os-release ] || die "/etc/os-release is missing; this does not look like Ubuntu"
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" != "ubuntu" ]; then
        die "this installer supports Ubuntu only (found ID=${ID:-unknown})"
    fi
    case "${VERSION_ID:-0}" in
        20.*|21.*|22.*|23.*|24.*|25.*|26.*) : ;;
        *) die "Ubuntu 20.04 or newer is required (found ${VERSION_ID:-unknown})" ;;
    esac
    ok "Ubuntu ${VERSION_ID}"

    command -v python3 >/dev/null 2>&1 \
        || die "python3 not found. Install it with: apt-get install -y python3"
    if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
        die "python3 >= 3.8 is required (found $(python3 --version 2>&1))"
    fi
    ok "$(python3 --version 2>&1)"

    command -v dpkg-query >/dev/null 2>&1 || die "dpkg-query not found"
    ok "dpkg-query present"

    if ! command -v crontab >/dev/null 2>&1 && [ ! -d /etc/cron.d ]; then
        die "cron not found. Install it with: apt-get install -y cron"
    fi
    # Checked in order of reliability. systemctl is authoritative on a normal
    # host, but the binary exists on images where systemd is not PID 1, where
    # it always reports failure -- so a running cron process is accepted as
    # proof regardless, rather than warning that a working install is broken.
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet cron 2>/dev/null; then
        ok "cron is active"
    elif pgrep -x cron >/dev/null 2>&1 || pgrep -x crond >/dev/null 2>&1; then
        ok "cron is running"
    else
        warn "cron does not appear to be running; the agent will not run until it is"
        warn "start it with: systemctl enable --now cron"
    fi

    command -v base64 >/dev/null 2>&1 || die "base64 not found (coreutils)"
    command -v tar >/dev/null 2>&1 || die "tar not found"
}

prompt_credentials() {
    if [ -z "$SERVER_URL" ]; then
        printf 'Platform URL (e.g. https://sentinel.example): '
        read -r SERVER_URL
    fi
    [ -n "$SERVER_URL" ] || die "a platform URL is required"

    case "$SERVER_URL" in
        https://*) : ;;
        http://*)
            if [ -z "$INSECURE" ] || [ "$INSECURE" = "0" ]; then
                die "refusing to send the API key over plain HTTP; pass --insecure for local testing"
            fi
            warn "using plain HTTP: the API key will cross the network unencrypted"
            ;;
        *) die "the platform URL must start with https:// or http://" ;;
    esac

    if [ -z "$API_KEY" ]; then
        printf 'Device API key: '
        # Echo disabled so the key does not persist in the terminal scrollback.
        stty -echo 2>/dev/null || true
        read -r API_KEY
        stty echo 2>/dev/null || true
        printf '\n'
    fi
    [ -n "$API_KEY" ] || die "a device API key is required"
}

create_user() {
    if id "$AGENT_USER" >/dev/null 2>&1; then
        ok "user '$AGENT_USER' exists"
        return
    fi
    info "Creating system user '$AGENT_USER'"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$AGENT_USER"
    ok "created '$AGENT_USER'"
}

install_files() {
    info "Installing agent to $INSTALL_DIR"
    [ -d "$INSTALL_DIR" ] || CREATED_INSTALL=1

    # Staged into a sibling directory and swapped in only once it is known
    # good. Unpacking over the live install would, on any failure, leave a cron
    # entry and a symlink pointing at a missing binary -- a device that stops
    # reporting with no obvious cause.
    local staging="${INSTALL_DIR}.new"
    rm -rf "$staging"
    mkdir -p "$staging"

    if [ -n "$FROM_SOURCE" ]; then
        if [ ! -d "$FROM_SOURCE/sentinel_agent" ] || [ ! -d "$FROM_SOURCE/bin" ]; then
            rm -rf "$staging"
            die "--from-source $FROM_SOURCE must contain sentinel_agent/ and bin/"
        fi
        cp -R "$FROM_SOURCE/sentinel_agent" "$FROM_SOURCE/bin" "$staging/"
    else
        if [ -z "$AGENT_BUNDLE_B64" ]; then
            rm -rf "$staging"
            die "this installer has no embedded agent bundle; pass --from-source DIR"
        fi
        if ! printf '%s' "$AGENT_BUNDLE_B64" | base64 -d | tar -xz -C "$staging"; then
            rm -rf "$staging"
            die "could not unpack the embedded agent bundle"
        fi
    fi

    if [ ! -f "$staging/bin/sentinel-agent" ]; then
        rm -rf "$staging"
        die "agent entrypoint missing after unpack; nothing was changed"
    fi

    find "$staging" -type d -exec chmod 755 {} +
    find "$staging" -type f -exec chmod 644 {} +
    chmod 755 "$staging/bin/sentinel-agent"
    chown -R root:root "$staging"

    # Swap: the old tree only goes away once the new one is in place.
    rm -rf "${INSTALL_DIR}.old"
    [ -d "$INSTALL_DIR" ] && mv "$INSTALL_DIR" "${INSTALL_DIR}.old"
    mv "$staging" "$INSTALL_DIR"
    rm -rf "${INSTALL_DIR}.old"

    ln -sfn "$INSTALL_DIR/bin/sentinel-agent" "$SYMLINK"
    ok "installed $(find "$INSTALL_DIR" -type f | wc -l | tr -d ' ') file(s)"

    mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
    chmod 750 "$CONFIG_DIR"
    chmod 700 "$STATE_DIR"
    chmod 750 "$LOG_DIR"
    chown "$AGENT_USER:$AGENT_USER" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
}

already_enrolled() {
    [ -f "$CONFIG_DIR/config.json" ] \
        && python3 -c "
import json,sys
try:
    sys.exit(0 if json.load(open('$CONFIG_DIR/config.json')).get('device_id') else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

run_as_agent() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$AGENT_USER" -- "$@"
    else
        su -s /bin/sh "$AGENT_USER" -c "$(printf '%q ' "$@")"
    fi
}

enroll() {
    if [ "$SKIP_ENROLL" -eq 1 ]; then
        ok "kept the existing device identity"
        ok "pass --force-reenroll to register against a different key"
        # Ownership is re-applied anyway: a reinstall recreates files as root.
        chown -R "$AGENT_USER:$AGENT_USER" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
        chmod 600 "$CONFIG_DIR/config.json"
        return
    fi

    info "Enrolling with $SERVER_URL"
    local args=(--server "$SERVER_URL" --key "$API_KEY")
    if [ -n "$INSECURE" ] && [ "$INSECURE" != "0" ]; then
        args+=(--insecure)
    fi

    # Enrollment runs as root so it can write into /etc, then ownership is
    # handed to the agent user below.
    if ! "$INSTALL_DIR/bin/sentinel-agent" --quiet enroll "${args[@]}"; then
        if [ "$CREATED_INSTALL" -eq 1 ]; then
            warn "enrollment failed; removing the partial install"
            remove_everything
        fi
        die "enrollment failed. Check the URL and that the key has not been used or revoked."
    fi

    chown -R "$AGENT_USER:$AGENT_USER" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
    chmod 600 "$CONFIG_DIR/config.json"
    ok "enrolled"
}

install_cron() {
    info "Scheduling the agent"
    # One entry: cron supplies the tick, the agent decides what is due. The
    # collection schedule therefore stays changeable from the dashboard.
    cat > "$CRON_FILE" <<EOF
# Sentinel agent. Managed by the installer; edits will be overwritten.
# The agent decides what work is due on each tick, so this entry never changes.
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * $AGENT_USER $INSTALL_DIR/bin/sentinel-agent --quiet beat
EOF
    chmod 644 "$CRON_FILE"
    chown root:root "$CRON_FILE"
    ok "installed $CRON_FILE"
}

verify() {
    info "Verifying"
    # Run as the agent user, which is what cron will do: this catches a
    # permissions mistake now rather than silently at the next tick.
    if run_as_agent "$INSTALL_DIR/bin/sentinel-agent" beat; then
        ok "first beat succeeded"
    else
        warn "the first beat did not succeed. The agent is installed and cron"
        warn "will retry every minute. Inspect: $LOG_DIR/agent.log"
        return 1
    fi

    local device_id
    device_id=$(run_as_agent "$INSTALL_DIR/bin/sentinel-agent" status 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("device_id") or "")' 2>/dev/null || true)

    printf '\n'
    ok "Sentinel agent installed"
    [ -n "$device_id" ] && printf '     device id: %s\n' "$device_id"
    printf '     logs:      %s/agent.log\n' "$LOG_DIR"
    printf '     status:    sentinel-agent status\n'
    printf '     collect:   sentinel-agent collect\n'
    printf '\n  The device appears in the dashboard within a minute.\n\n'
}

remove_everything() {
    rm -f "$CRON_FILE" "$SYMLINK"
    rm -rf "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
}

uninstall() {
    info "Removing the Sentinel agent"
    rm -rf "${INSTALL_DIR}.new" "${INSTALL_DIR}.old"
    remove_everything
    # Logs are deliberately kept: they are the record of what the agent did.
    ok "removed agent, config, state and cron entry"
    ok "logs left in place at $LOG_DIR"
    if id "$AGENT_USER" >/dev/null 2>&1; then
        userdel "$AGENT_USER" 2>/dev/null && ok "removed user '$AGENT_USER'" \
            || warn "could not remove user '$AGENT_USER'; remove it by hand if unused"
    fi
    printf '\n  The device record is still on the server. Delete it in the dashboard.\n\n'
}

main() {
    parse_args "$@"
    require_root "$@"

    if [ "$DO_UNINSTALL" -eq 1 ]; then
        uninstall
        exit 0
    fi

    preflight
    # The key is stored only as a hash, so it cannot be looked up again. An
    # already-enrolled device being reinstalled or upgraded must not be forced
    # into a needless key rotation just to satisfy a prompt.
    if already_enrolled && [ "$FORCE_REENROLL" -eq 0 ]; then
        info "Already enrolled; reinstalling the agent and keeping its identity"
        SKIP_ENROLL=1
    else
        prompt_credentials
    fi
    create_user
    install_files
    enroll
    install_cron
    verify || exit 1
}

main "$@"
