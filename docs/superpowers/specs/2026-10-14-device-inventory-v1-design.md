# Sentinel v1 — Device Inventory and Dashboard

**Design specification**
**Date:** 2026-10-14
**Status:** Approved, ready for implementation planning
**Parent requirements:** `project_schema.md`

---

## 1. Scope

`project_schema.md` describes roughly ten independent subsystems. This spec covers only the first slice: enrolling Linux devices and seeing them in a dashboard.

### In scope

- Docker Compose stack: PostgreSQL, FastAPI, Streamlit, simulated client devices.
- Admin authentication (single administrator, password only).
- Device registration producing a per-device API key.
- A terminal installer script that enrolls a device and installs a cron entry.
- A cron-driven bash agent that reports resources, collects software inventory, executes queued tasks, and spools reports while offline.
- Backend-delivered agent configuration.
- A Devices list and a device detail page.

### Out of scope for v1

Deferred entirely, each to its own spec cycle: software intelligence collection, vulnerability matching and findings, anomaly detection, predictive maintenance, risk prediction, the MCP server, and every dashboard tab other than Devices. No remediation or software updates, per `project_schema.md` §2.

### Non-goals that could be mistaken for scope

- No resource time-series or charts. Resources are latest-value only.
- No full inventory snapshot history. Current state plus a change log instead (§6.3).
- No version-range comparison logic. v1 only needs version string inequality to detect change; PEP 440 / semver / Maven comparison arrives with the matching engine.
- No HTTPS termination in local development. See §9.3.

---

## 2. Decisions and rationale

| Decision | Choice | Why |
|---|---|---|
| History depth | Latest resources; full package change log | Package history cannot be backfilled and every later module depends on it. Resource time-series is cost without a correlation target yet, and can be added later without touching existing tables. |
| Collector breadth | OS packages, host details, resources, language runtime versions, and system-level language packages | Everything comes from a package-manager query: bounded, fast, deterministic. No filesystem walking and no guessing which directory is a project. |
| Enrollment | Admin pre-creates the device; one-time key shown once | Matches `project_schema.md` §4.1. Every key is individually revocable and a leaked key compromises exactly one device. |
| Test target | Simulated Ubuntu containers in Compose | Matches §15. The cron runtime model (below) means these need only `cron`, not `/sbin/init` as PID 1 — so no privileged containers and no cgroup configuration. |
| Agent runtime | Single script invoked by cron; no daemon, no systemd unit | Explicit user requirement: a cron-jobable, manually triggerable background script. Also strictly more robust than a daemon — a failed run has no consequences because the next minute is a fresh process. |
| Agent implementation | Bash, using only `curl`, `awk`, `flock` and `dpkg-query` | All are present on a default Ubuntu Server install, so enrollment installs nothing and needs no access to apt or PyPI. Nothing is resident between cron runs, so there is no polling process to consume memory. |
| Agent wire protocol | Line-oriented text, not JSON | Follows from the bash decision: parsing a JSON response in shell would require `jq`, which Ubuntu does **not** ship by default, and generating a 1500-package JSON body in shell means hand-rolled escaping of values like `cpu_model`, which routinely contains quotes and parentheses. Tab-separated records avoid escaping entirely. Admin routes remain JSON. |
| Dashboard data path | Streamlit calls FastAPI; never touches PostgreSQL | Keeps §2's promise that the frontend is replaceable. Swapping in React later means writing a client against an API that already exists and is already exercised. |
| Distributions | Ubuntu 20.04+ only | One package-manager path and one guaranteed base toolchain. Debian is excluded because its minimal installs cannot be relied on to carry `curl`; the collector layout keeps `dnf` or Debian support a new module later rather than a rewrite. |
| Background workers | None | v1 has no server-side scheduled processing. Device status is derived at read time from `last_seen_at`. Retention pruning is a CLI command on a cron schedule inside the API container. No Celery, no Redis. |

---

## 3. Architecture

```text
Managed Linux device
  cron (* * * * *) --> sentinel-agent beat
                          |
                          |  outbound HTTPS, per-device API key
                          v
                      FastAPI  <---->  PostgreSQL
                          ^
                          |  HTTP, admin bearer token
                          |
                   Streamlit dashboard
```

Four containers: `db`, `api`, `dashboard`, and in development N× `sim-device`. All traffic is agent-initiated outbound; the server never connects to a device, per `project_schema.md` §3.

Service isolation matters more than image count: `api` and `dashboard` may share a built image and differ only in command.

---

## 4. The agent

### 4.1 Runtime model

One cron entry drives everything:

```cron
# /etc/cron.d/sentinel-agent
* * * * * sentinel /opt/sentinel-agent/sentinel-agent beat --quiet
```

Several cron entries — one per minute for heartbeat, one twice daily for inventory — were rejected because a schedule living in `/etc/cron.d` on each device cannot be changed from the dashboard, which contradicts the backend-delivered configuration requirement. Cron therefore controls only the tick; the script decides what is due.

A `beat` run, in order:

1. Acquire a non-blocking `flock` on `/var/lib/sentinel-agent/agent.lock`. If held, exit 0 immediately — a slow collection can never pile up invocations.
2. Sample resources and `POST /api/v1/agent/heartbeat`.
3. Apply the `config` from the response to local cache; record the pending `tasks`.
4. If `now - last_collect_at >= inventory_interval_seconds`, run collection and report it.
5. Execute pending tasks, posting each result.
6. Flush the spool: retry any report queued by an earlier failed run, oldest first.
7. Release the lock.

Cron's one-minute floor matches the 60-second heartbeat default in §16 exactly.

### 4.2 CLI

| Command | Purpose |
|---|---|
| `sentinel-agent enroll --server URL --key KEY [--insecure]` | Used by the installer. Writes config, registers, stores the returned device ID. `--insecure` permits a plain-HTTP server URL and is required by the sim devices. |
| `sentinel-agent beat [-v\|--quiet]` | One full pass as described above. The cron invocation. |
| `sentinel-agent collect` | Force inventory collection and report now, ignoring the interval. |
| `sentinel-agent status` | Print enrollment state, server URL, last successful contact, spool depth, cached config. Exit non-zero if never enrolled. |

`collect` and `beat -v` are the manual trigger path; both take the same lock, so a manual run and a cron run cannot overlap.

### 4.3 On-disk layout

| Path | Mode | Owner | Contents |
|---|---|---|---|
| `/opt/sentinel-agent/sentinel-agent` | 755 | root | CLI entrypoint, `#!/usr/bin/env bash` |
| `/opt/sentinel-agent/lib/*.sh` | 644 | root | sourced modules: `config`, `http`, `state`, `spool`, `resources`, `tasks`, `log`, `collect_*` |
| `/usr/local/bin/sentinel-agent` | symlink | root | convenience symlink so the manual commands in §4.2 work from any shell |
| `/etc/sentinel-agent/config.env` | 600 | sentinel | shell-sourceable: `SERVER_URL`, `DEVICE_ID`, `API_KEY`, `INSECURE` |
| `/var/lib/sentinel-agent/state/` | 700 | sentinel | one small file per value — `last_collect_at`, `cpu_sample`, `cached_config.env`, `last_contact_at` |
| `/var/lib/sentinel-agent/spool/` | 700 | sentinel | one file per undelivered report, named `<epoch_ms>-<kind>.txt` |
| `/var/lib/sentinel-agent/agent.lock` | 600 | sentinel | flock target |
| `/var/log/sentinel-agent/agent.log` | 640 | sentinel | appended; rotated by the script itself at 2 MB, keeping 5 generations |
| `/etc/cron.d/sentinel-agent` | 644 | root | the entry above |

State is flat files rather than a database: bash has no SQLite binding without installing one, and every value the agent persists is a scalar or a small key-value set, so files are both sufficient and directly inspectable during debugging. Each write is atomic — write to a temporary file in the same directory, then `mv` — so a run killed mid-write cannot leave a half-written state file.

The agent runs as a dedicated unprivileged `sentinel` system user with `/usr/sbin/nologin` as its shell. `dpkg-query`, `/proc`, `npm ls -g` and system `pip` are all readable unprivileged, satisfying §13's least-privilege requirement. Only the installer needs root.

**Known limitation:** per-user pyenv installations and virtualenvs inside other users' home directories are not readable by the `sentinel` user and will not be reported. System-level interpreters are what gets inventoried. This is a consequence of running unprivileged and is accepted for v1.

### 4.4 Collectors

Each collector is a sourced function `collect_<name>` writing tab-separated records — `ecosystem`, `scope`, `name`, `version` — to stdout, one per line. Adding Debian, `dnf` or project-dependency scanning later means a new file in `lib/`, not a change to an existing one.

Every command is chosen so its output needs no JSON parsing:

| Collector | Mechanism | Ecosystem tag |
|---|---|---|
| os | `dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\t${Version}\n'`, keeping rows whose status abbreviation is `ii` | `os` |
| python | For each `python3*` on `PATH`: `<interp> -m pip list --format=freeze`, which emits `name==version` and needs no parser. `scope` is the interpreter's resolved path | `python` |
| npm | `npm ls -g --depth=0 --parseable`, giving one install path per line; the package name is the final path segment and the version comes from the matching `package.json` via a single `awk` pass | `npm` |
| java | Directory names under `/usr/lib/jvm`, plus `java -version` stderr for the default JDK | `java` |
| host | `/etc/os-release`, `uname -r -m`, `/proc/cpuinfo`, `/proc/meminfo` | n/a — host facts |

`pip list --format=freeze` is used in place of `--format=json` specifically so the agent needs no JSON reader. It omits editable-install paths, which v1 does not report anyway.

Collector rules:

- Every command runs under `timeout 30`, and a missing binary is not an error: the collector emits nothing and the run records `collector=<name>:skipped:not installed`.
- A failing collector must never abort the report. Whatever succeeded is submitted, and the `collector=` header lines say which collectors failed and why, so the dashboard shows partial inventory honestly rather than silently showing less.
- Values are passed through `tr -d '\t\n'` before emission. Tabs and newlines are the only characters that could corrupt the wire format, and no real package name, version or CPU model contains either — so this is the complete escaping story, which is the main thing the line protocol buys over JSON.

### 4.5 Resource sampling

All from `/proc` via `awk`, with no external tools:

- **CPU** — `/proc/stat` jiffy deltas against the previous sample in `state/cpu_sample`. The first beat after enrollment reports `cpu_percent=` (empty, meaning unknown) because there is no prior sample; every later beat covers exactly the interval since the last one.
- **Memory** — `/proc/meminfo`: `MemTotal`, `MemAvailable`.
- **Disk** — `df -P -B1` filtered against `/proc/mounts` to real block-backed filesystems (`ext4`, `xfs`, `btrfs`, `zfs`, `vfat`), excluding `overlay`, `tmpfs`, `devtmpfs`, `squashfs`. `df` is coreutils, so it is always present.
- **Load** — `/proc/loadavg`.
- **Uptime** — `/proc/uptime`.

### 4.6 Offline spool

A report that fails to deliver is written to `spool/<epoch_ms>-<kind>.txt` with its original `collected_at` header intact, so the server reconstructs history from the agent's timestamps per §4 of the requirements. A directory of files is a natural queue in shell: `ls` gives ordering because the names are zero-padded epoch milliseconds, and `du -sb` gives the total size.

- Size cap: **64 MB**, configurable via backend-delivered config.
- Overflow: oldest-first eviction, logged at WARN.
- No age-based expiry in v1; the 30-day policy in §14 is a later tightening.
- Flush is oldest-first and stops on the first failure, so ordering is preserved and an unreachable server does not cause a long retry storm every minute.
- A spooled file is removed only after the server returns 2xx, so a crash mid-flush retries rather than loses the report. The server therefore must tolerate a duplicate report, which it does: an identical `collected_at` for the same device is accepted and recorded, and §6.3's superseded rule means it produces no change rows.

### 4.7 Task execution

v1 defines exactly one task type: `collect_inventory`. The agent dispatches via a `case` statement over the task type, which selects a shell function defined in the agent itself; an unrecognized type is reported `failed` with `error=unsupported task type` rather than ignored, so the dashboard shows the truth.

**No value received from the server is ever evaluated as shell.** Task types are matched against literal `case` patterns, never interpolated into a command, and task parameters are read into named variables and validated against the handler's expected keys before use. This is the central security requirement of a shell agent and upholds §4 and §13's prohibition on arbitrary command execution. The agent script runs under `set -euo pipefail` with `IFS` set explicitly, and all variable expansions are quoted.

---

## 5. Installer

`agent/install.sh`, a POSIX shell script the administrator hands to the operator of the target device. Run as root.

1. **Preflight** — confirm Ubuntu 20.04+ via `/etc/os-release`; confirm `bash` ≥ 4, `curl`, `awk`, `flock`, `df` and `dpkg-query` are present; confirm `cron` is installed and its service active. Any failure aborts naming the missing tool and the `apt install` line that fixes it, before anything is written. `curl` is the only one plausibly absent, and only on a minimized cloud image.
2. **Prompt** — ask interactively for the server URL and the API key. The key prompt disables echo. Both are also accepted as `--server` / `--key` flags for scripted installs, alongside `--insecure`, which is passed through to `enroll`.
3. **Create** the `sentinel` system user and group if absent.
4. **Install** the agent files to `/opt/sentinel-agent`, and create `/etc/sentinel-agent`, `/var/lib/sentinel-agent` and `/var/log/sentinel-agent` with the ownership and modes in §4.3.
5. **Enroll** — run `sentinel-agent enroll`. A rejected key aborts the install and removes what was written, leaving no half-configured device.
6. **Schedule** — write `/etc/cron.d/sentinel-agent`.
7. **Verify** — run one `sentinel-agent beat -v` and report the outcome, then print the device ID and a reminder that the device will appear in the dashboard within a minute.

The script is idempotent: re-running it over an existing install updates the files and re-enrolls, preserving the existing device ID unless `--force-reenroll` is given. It also accepts `--uninstall`, which removes the cron entry, files and user but leaves the device record on the server for the administrator to delete.

---

## 6. Data model

PostgreSQL, SQLAlchemy 2.0 declarative models, Alembic migrations. All timestamps are `timestamptz` stored in UTC.

### 6.1 Identity and auth

**`admin_user`** — `id`, `username` (unique), `password_hash`, `created_at`, `last_login_at`.

Exactly one row in v1, per §13. Seeded on first API start from `ADMIN_USERNAME` / `ADMIN_PASSWORD` environment variables; if a row already exists the seed is skipped. Password recovery is manual on the VPS: a `reset-admin-password` CLI command in the API container.

Hashing is Argon2id via `argon2-cffi`.

**`admin_session`** — `token_hash` (PK), `admin_user_id`, `created_at`, `expires_at`, `last_used_at`.

Opaque random tokens rather than JWTs, because a JWT cannot be revoked without extra state and v1 has no need for stateless verification. 32 random bytes, base64url-encoded, stored as a SHA-256 hash. 12-hour expiry, sliding on use.

### 6.2 Devices

**`device`** — `id` (UUID PK, platform-generated), `name`, `created_at`, `enabled` (bool), `last_seen_at`, `agent_version`, and host facts: `hostname`, `os_name`, `os_version`, `kernel`, `arch`, `cpu_model`, `cpu_cores`, `total_memory_bytes`.

Host facts are metadata, never identity, per §4. Devices are never matched or merged on hostname.

**`device_key`** — `id`, `device_id`, `key_hash` (unique), `created_at`, `revoked_at`, `last_used_at`.

A device may have multiple key rows but at most one with `revoked_at IS NULL`, enforced by a partial unique index on `device_id WHERE revoked_at IS NULL` rather than by application logic alone. Rotation inserts a new row and revokes the old one in one transaction, preserving the history §4 requires. Keys are 32 random bytes rendered as `sk_dev_<base64url>` and stored as a SHA-256 hash — the plaintext is shown once at creation and is not recoverable. SHA-256 rather than a password hash is correct here because the key is full-entropy random, not a human-chosen secret, so it is not subject to dictionary attack.

**`device_resources`** — `device_id` (PK, 1:1 with `device`), `sampled_at`, `cpu_percent` (nullable), `memory_total_bytes`, `memory_available_bytes`, `load_1`, `load_5`, `load_15`, `uptime_seconds`, `disks` (JSONB array of `{mountpoint, filesystem, total_bytes, free_bytes}`).

Overwritten on every heartbeat. A separate table rather than columns on `device` so that the frequently-written row is distinct from the rarely-written one, and so adding a time-series table later is additive.

### 6.3 Inventory

A full snapshot per report was considered and rejected: two reports a day × 30 days × ~1500 packages is ~90,000 rows per device for information that is almost entirely unchanged between reports. Instead, current state plus a change log — history is reconstructable by replaying changes backwards from current state, which answers "when did this appear" and "what changed last week" at a fraction of the storage.

**`package`** — `id`, `ecosystem` (enum: `os`, `python`, `npm`, `java`), `name`, `normalized_name`, unique on `(ecosystem, normalized_name)`.

A shared catalogue so that a package installed on 50 devices is one row. Normalization is ecosystem-specific: PyPI names lowercase with `_`, `.` and runs of `-` collapsed to a single `-` per PEP 503; npm and OS names lowercase; Java artifacts keyed as `groupId:artifactId`.

**`device_package`** — `device_id`, `package_id`, `scope`, `version`, `first_seen_at`, `last_seen_at`, `meta` (JSONB), PK `(device_id, package_id, scope)`.

Current state. `scope` discriminates where on the device the package lives: `system` for OS and npm-global packages, and the interpreter path (`/usr/bin/python3.11`) for Python packages. It is part of the key because the same Python package can be installed at *different versions* under two interpreters on one device — without `scope` those two installations would collide on one row and each report would flap the version back and forth, manufacturing an endless stream of false `version_changed` entries.

**`package_change`** — `id`, `device_id`, `package_id`, `scope`, `change_type` (enum: `added`, `removed`, `version_changed`), `old_version`, `new_version`, `detected_at`, `report_id`.

**`inventory_report`** — `id`, `device_id`, `collected_at` (agent clock), `received_at` (server clock), `package_count`, `collector_status` (JSONB), `change_count`.

Both timestamps are kept because a spooled report arrives long after collection, and §4 requires the server to stitch history from the agent's timestamps.

**Diffing** happens server-side on report receipt, inside one transaction: load the device's current `device_package` rows, compare against the report, write `package_change` rows for the difference, then update `device_package`. Comparison is string inequality on the version — v1 needs no ordering, only change detection.

**Out-of-order reports:** a report whose `collected_at` is older than the device's newest `collected_at` is stored and counted, but does not alter `device_package` or produce change rows. Diffing a stale report against newer current state would manufacture false changes. The report row records `superseded: true` so the dashboard can show it was received without implying it changed anything.

### 6.4 Tasks and configuration

**`task`** — `id` (UUID), `device_id`, `type`, `params` (JSONB), `status` (enum: `pending`, `running`, `completed`, `failed`), `created_at`, `claimed_at`, `completed_at`, `result` (JSONB), `error`.

A task moves to `running` when delivered in a heartbeat response, satisfying §4's duplicate-execution concern. A task `running` for longer than 30 minutes is treated as abandoned and shown as such; v1 does not auto-retry.

**`agent_config`** — `id`, `device_id` (nullable — `NULL` is the global default row), `poll_interval_seconds` (default 60), `inventory_interval_seconds` (default 43200), `spool_max_bytes` (default 67108864), `updated_at`.

Resolution is per-device row if present, else the global row. Returned in every heartbeat response.

### 6.5 Retention

A `prune` subcommand of `server/cli.py`, run daily at 03:00 UTC by a cron entry baked into the API image (which therefore installs `cron` and runs it alongside uvicorn via a small entrypoint script), deletes `package_change` rows, `inventory_report` rows and `task` rows older than **30 days**, and expired `admin_session` rows. `device_package` is current state and is never pruned. This matches §14's 30-day operational retention.

---

## 7. API

FastAPI, all routes under `/api/v1`. **Admin routes use JSON with Pydantic v2 models. Agent routes use the line protocol below**, because the agent is shell and cannot parse JSON without `jq`. The two halves are deliberately different: the agent protocol optimizes for being generated and parsed by `awk`, the admin API optimizes for being consumed by Streamlit today and React later.

### 7.0 Agent line protocol

`Content-Type: text/plain; charset=utf-8` in both directions. A body is a block of `key=value` header lines, then optionally a blank line followed by tab-separated records. Keys may repeat, which is how lists are expressed. Values contain no tabs or newlines (§4.4), so parsing needs no escaping rules.

An inventory report body:

```text
collected_at=2026-10-14T06:00:03Z
agent_version=1.0.0
collector=os:ok
collector=python:ok
collector=npm:skipped:not installed
collector=java:ok

os	system	nginx	1.24.0-2ubuntu7
python	/usr/bin/python3.11	numpy	2.1.0
java	system	openjdk-17-jdk	17.0.12
```

A heartbeat body:

```text
agent_version=1.0.0
cpu_percent=12.4
memory_total_bytes=8203431936
memory_available_bytes=4120094720
load_1=0.15
load_5=0.22
load_15=0.18
uptime_seconds=98234
spool_depth=0
disk=/:ext4:50000000000:20311234560
disk=/boot:ext4:1000000000:600000000
```

A heartbeat response:

```text
config.poll_interval_seconds=60
config.inventory_interval_seconds=43200
config.spool_max_bytes=67108864
task=9f3c1e80-...:collect_inventory
task.9f3c1e80-....param.force=true
```

The `task.<id>.param.<key>=<value>` shape is used from the start so that future task types with parameters need no protocol change. An empty value means unknown — `cpu_percent=` on the first beat after enrollment.

Server-side, each agent route has a hand-written parser module with its own unit tests, since Pydantic is not doing this validation. Parsers reject unknown keys, enforce a maximum body size and a maximum record count, and return `400` with a `code` and the offending line number. The agent's own requests are the fixtures those parsers are tested against.

### 7.1 Agent routes

Authenticated by `Authorization: Bearer sk_dev_...`. A revoked key or a disabled device returns `403` with a machine-readable `code=` line, so the agent can log a clear reason rather than retrying blindly. Every agent response includes the resolved `config.*` lines so a device that missed a heartbeat still converges.

| Method | Path | Purpose |
|---|---|---|
| POST | `/agent/enroll` | Body: host facts, agent version. Returns `device_id` and config. Idempotent — re-enrolling with the same key returns the same `device_id` and refreshes host facts. |
| POST | `/agent/heartbeat` | Body: resource sample, agent version, spool depth. Updates `last_seen_at` and `device_resources`. Returns `config` and `tasks` (which transitions them to `running`). |
| POST | `/agent/inventory` | Body: `collected_at`, `collector_status`, packages. Performs the diff of §6.3. Returns `report_id` and `change_count`. |
| POST | `/agent/tasks/{task_id}/result` | Body: `status`, `result`, `error`. Rejects a task belonging to another device. |

### 7.2 Admin routes

Authenticated by `Authorization: Bearer <session token>`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Username and password → session token and expiry. |
| POST | `/auth/logout` | Revoke the current session. |
| GET | `/devices` | List with derived status, resources and package counts. Supports `status` and `q` filters. |
| POST | `/devices` | Create. **Returns the plaintext key once.** |
| GET | `/devices/{id}` | Detail: host facts, resources, counts, latest report metadata. |
| PATCH | `/devices/{id}` | Rename, enable or disable. |
| DELETE | `/devices/{id}` | Delete device and all its data. |
| POST | `/devices/{id}/rotate-key` | Revoke current key, issue a new one, return plaintext once. |
| GET | `/devices/{id}/packages` | Paginated, filterable by ecosystem, searchable by name. |
| GET | `/devices/{id}/changes` | Paginated change log, newest first. |
| GET | `/devices/{id}/reports` | Report history with collector status. |
| GET | `/devices/{id}/tasks` | Task history. |
| POST | `/devices/{id}/tasks` | Queue a task. v1 accepts `collect_inventory` only. |
| GET | `/config` / `PUT` `/config` | Read and update the global config row. |
| GET | `/healthz` | Unauthenticated liveness, for Compose health checks. |

### 7.3 Derived device status

Computed at read time from `last_seen_at` against the device's resolved `poll_interval_seconds` (P, default 60):

| Status | Condition |
|---|---|
| `online` | `last_seen_at` within 3 × P (180 s) |
| `stale` | within 15 × P (900 s) |
| `offline` | older than 15 × P, or never seen |
| `disabled` | `enabled = false`, regardless of `last_seen_at` |

Three missed heartbeats before `stale` satisfies §8's requirement that one missed beat must not mark a device offline. Expressing thresholds as multiples of the poll interval means changing the interval does not silently break status.

---

## 8. Dashboard

Streamlit, calling the API over HTTP with the session token held in `st.session_state`. No database driver is installed in the dashboard image — the separation is enforced by what is available, not by discipline.

A thin hand-written API client module wraps `requests`, attaches the token, and on `401` clears the session and shows the login form, so an expired session is a clean re-login rather than a traceback.

**Login** — username and password; nothing else renders until authenticated.

**Devices** — one row per device: name, status badge, last seen (relative), OS, CPU %, memory %, worst disk %, package count. Filter by status, search by name or hostname. An "Add device" dialog takes a name and then shows the plaintext key once, with an explicit warning that it cannot be retrieved again and the installer command to run.

**Device detail** — five sections:

1. Host and OS facts, agent version, enrollment date.
2. Resources, as of the last heartbeat, with the sample time shown so a stale reading is never mistaken for a current one.
3. Packages — searchable, filterable by ecosystem, paginated. A banner if the latest report's `collector_status` shows any collector failed, naming which.
4. Change log — newest first, with type, old and new version, and detection time.
5. Tasks — history with status and timing, plus a **Collect now** button that queues a `collect_inventory` task and notes that the device will pick it up within a minute.

Device management — rename, disable, rotate key, delete — lives on the detail page, with confirmation on the destructive actions.

---

## 9. Repository and infrastructure

### 9.1 Layout

```text
sentinel/
  docker-compose.yml
  docker-compose.override.yml      # dev only: sim devices, exposed ports
  .env.example
  Makefile
  server/
    app/{main,config,db,auth,api/,models/,schemas/,services/}.py
    alembic/
    cli.py                         # seed-admin, reset-admin-password, prune
    tests/{unit,integration}/
    Dockerfile
  dashboard/
    app.py
    pages/
    api_client.py
    tests/
  agent/
    sentinel-agent                 # bash entrypoint
    lib/{config,http,state,spool,resources,tasks,log}.sh
    lib/collect_{os,python,npm,java,host}.sh
    install.sh
    tests/                         # bats suites + captured command fixtures
  sim/
    Dockerfile                     # Ubuntu 22.04 + cron + curl + python3-pip + nodejs
  docs/superpowers/specs/
```

`server/` and `dashboard/` share one built image with different commands; `agent/` is never installed into a server container.

### 9.2 Compose

`db` (PostgreSQL 16, named volume, health check), `api` (migrations on start, then uvicorn), `dashboard`, and in the override file `sim-device-1..3` built from `sim/Dockerfile`. Each sim device runs `cron -f` in the foreground as PID 1 — no init system, no privileged flag, no cgroup mounts, which is the practical payoff of the cron runtime model.

The sim image installs `python3-pip` and `nodejs` so the Python and npm collectors have something real to inventory. That is a property of the *test fixture*, not a dependency of the agent — the agent itself needs neither.

Environment-specific values live in `.env`: database credentials, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `PUBLIC_API_URL`, `LOG_LEVEL`. `.env.example` documents every variable. Secrets are never committed.

All of §3's operations work unmodified: `docker compose up -d`, `down`, `restart`, `logs -f`, `up -d --build`.

### 9.3 TLS

Local development uses plain HTTP over the Compose network. The VPS deployment adds a Caddy service in front of `api` and `dashboard` for automatic TLS, enabled by a Compose profile so local development does not need certificates. The agent refuses a non-HTTPS server URL unless `--insecure` is passed at enrollment, which the sim devices use and which is logged loudly on every beat — making the insecure case visible rather than silent, and keeping §4's HTTPS requirement true in production.

---

## 10. Testing

Per §15, automated tests from the start.

**Unit (server, pytest)** — the line-protocol parsers against captured real agent bodies, plus malformed input: unknown keys, a record with the wrong field count, an oversized body, a missing required header. The diff engine against hand-built before/after sets, including the out-of-order and superseded cases and two interpreters holding the same package at different versions. Name normalization per ecosystem; status derivation at each threshold boundary; key generation, hashing and revocation; config resolution with and without a per-device override.

**Unit (agent, bats)** — every collector against captured real command output, with the fixture binaries injected by putting a stub directory first on `PATH`. Degenerate cases: missing binary, empty output, a package name containing a space, `timeout` firing. Resource parsers against captured `/proc` files, including the first-beat empty `cpu_percent`. Spool ordering and eviction at the size cap. Lock contention — a second `beat` while the first holds the lock must exit 0 without acting. `shellcheck` runs over every shell file in CI and is treated as a test failure, since it catches the unquoted-expansion class of bug that bash makes easy and silent.

**Integration** — the real agent script against the real API and a real PostgreSQL, asserting the full sequence: enroll, beat, collect, the diff producing the expected change rows, and a report submitted while the API is unreachable spooling and then flushing on the next beat. Because the agent and server share no types, **these tests are the only thing pinning the wire contract** — so they assert on exact bodies, not just on status codes.

**End-to-end** — `docker compose up`, then `install.sh` runs inside a sim container and the device appears in `GET /devices` as `online` with a non-zero package count within two minutes. A second E2E case installs a package inside the sim container, queues `collect_inventory`, and asserts the change log shows the addition.

Verification is evidence-based: a test run's output, not an assertion that it should pass.

---

## 11. Deviations from `project_schema.md`

Recorded so the parent document can be reconciled later.

1. **No systemd service (§4).** The agent is a cron-invoked script, per explicit user direction. Consequences: §4's restart-on-failure behaviour is inherent rather than configured, and logs go to a rotating file rather than journald. A systemd timer remains a drop-in alternative later for machines wanting catch-up after downtime via `Persistent=true`.
2. **The agent is bash, not Python (§2, §4).** Per explicit user direction, for the lightest possible footprint on managed devices. Consequences: agent routes use a line protocol rather than JSON, so those routes lose Pydantic validation in favour of hand-written parsers; agent tests are `bats` plus `shellcheck` rather than pytest; and adding the richer client-side logic the later roadmap implies — project-dependency scanning with exclusion rules, version-range comparison — will be materially harder in shell than it would have been in Python. The point at which that becomes the binding constraint is the point to reconsider.
3. **Ubuntu only, not Linux generally (§2).** Debian is excluded in v1 because minimal Debian installs cannot be relied on to carry `curl`, which the bash agent requires. The collector layout keeps other distributions additive.
4. **No full inventory snapshots (§14).** Current state plus a change log, for the storage reasons in §6.3. The intent of §14 — reconstructing changes over time — is satisfied.
5. **Spool has no 30-day age policy (§4).** Size cap only in v1; the age policy is a later tightening.
6. **Resource history is not stored (§3, §14).** Latest value only.
7. **Only the Devices tab exists (§8).** The other eleven tabs are later specs.

---

## 12. Settled implementation details

Items `project_schema.md` §17 left open that this spec now fixes, with values chosen to be changed from the dashboard rather than in code:

| Item | Value |
|---|---|
| Stale threshold | 3 × poll interval (180 s default) |
| Offline threshold | 15 × poll interval (900 s default) |
| Spool disk cap | 64 MB, oldest-first eviction |
| Default poll interval | 60 s |
| Default inventory interval | 43200 s (12 h) |
| Abandoned task threshold | 30 minutes in `running` |
| Admin session lifetime | 12 h, sliding |
| Password hashing | Argon2id |
| API key format | `sk_dev_` + 32 random bytes base64url, SHA-256 hashed at rest |
| Retention | 30 days for changes, reports, tasks and sessions |
