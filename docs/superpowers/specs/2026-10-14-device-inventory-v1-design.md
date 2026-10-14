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
- A cron-driven agent that reports resources, collects software inventory, executes queued tasks, and spools reports while offline.
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
| Agent dependencies | Python standard library only | The installer then requires only `python3`, which Ubuntu 20.04+ and Debian 11+ always ship. No venv creation, no `pip install` on the client, no PyPI reachability during enrollment, no wheel to build. |
| Dashboard data path | Streamlit calls FastAPI; never touches PostgreSQL | Keeps §2's promise that the frontend is replaceable. Swapping in React later means writing a client against an API that already exists and is already exercised. |
| Distributions | Debian 11+ and Ubuntu 20.04+ only | One package-manager path. The collector interface makes `dnf` a new module later rather than a rewrite. |
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
* * * * * sentinel /opt/sentinel-agent/bin/sentinel-agent beat --quiet
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
| `/opt/sentinel-agent/` | 755 | root | script and modules |
| `/opt/sentinel-agent/bin/sentinel-agent` | 755 | root | CLI entrypoint |
| `/etc/sentinel-agent/config.json` | 600 | sentinel | server URL, device ID, API key |
| `/var/lib/sentinel-agent/state.db` | 600 | sentinel | SQLite: spool, `last_collect_at`, cached config, previous CPU sample |
| `/var/lib/sentinel-agent/agent.lock` | 600 | sentinel | flock target |
| `/var/log/sentinel-agent/agent.log` | 640 | sentinel | rotating log, 5 × 2 MB via stdlib `RotatingFileHandler` |
| `/etc/cron.d/sentinel-agent` | 644 | root | the entry above |

The agent runs as a dedicated unprivileged `sentinel` system user with `/usr/sbin/nologin` as its shell. `dpkg-query`, `/proc`, `npm ls -g` and system `pip` are all readable unprivileged, satisfying §13's least-privilege requirement. Only the installer needs root.

**Known limitation:** per-user pyenv installations and virtualenvs inside other users' home directories are not readable by the `sentinel` user and will not be reported. System-level interpreters are what gets inventoried. This is a consequence of running unprivileged and is accepted for v1.

### 4.4 Collectors

Each collector is a module exposing `collect() -> list[dict]` returning `{ecosystem, name, version, meta}`. Adding `dnf` or project-dependency scanning later is a new module, not a change to an existing one.

| Collector | Mechanism | Ecosystem tag |
|---|---|---|
| os | `dpkg-query -W -f='${Package}\t${Version}\t${Status}\n'`, keeping only rows whose status is `install ok installed` | `os` |
| python | For each `python3*` on `PATH`: `<interp> -m pip list --format=json`; `meta.interpreter` records which one | `python` |
| npm | `npm ls -g --depth=0 --json`, reading `.dependencies` | `npm` |
| java | Directories under `/usr/lib/jvm` plus `java -version` stderr parsing | `java` |
| host | `/etc/os-release`, `uname -r -m`, `/proc/cpuinfo`, `/proc/meminfo` | n/a — host facts |

Collector rules:

- Every subprocess call has a timeout (30 s default) and a missing binary is not an error — the collector returns an empty list and records a note in the report's `collector_status` map.
- One collector failing must not abort the report. The report is submitted with whatever succeeded, and `collector_status` says which collectors failed and why, so the dashboard can show partial inventory honestly rather than silently showing less.

### 4.5 Resource sampling

Standard library only:

- **CPU** — `/proc/stat` jiffy deltas against the previous sample stored in `state.db`. The first beat after enrollment reports `cpu_percent: null` because there is no prior sample; every subsequent beat covers exactly the interval since the last one.
- **Memory** — `/proc/meminfo`: `MemTotal`, `MemAvailable`.
- **Disk** — `os.statvfs` per mountpoint from `/proc/mounts`, filtered to real block-backed filesystems (`ext4`, `xfs`, `btrfs`, `zfs`, `vfat`), excluding `overlay`, `tmpfs`, `devtmpfs`, `squashfs`.
- **Load** — `os.getloadavg()`.
- **Uptime** — `/proc/uptime`.

### 4.6 Offline spool

Reports that fail to deliver are appended to a `spool` table in `state.db` with their original `collected_at` timestamp, so the server reconstructs history from timestamps, per §4 of the requirements.

- Size cap: **64 MB**, configurable via backend-delivered config.
- Overflow: oldest-first eviction, logged at WARNING.
- No age-based expiry in v1; the 30-day policy in §14 is a later tightening.
- Flush is oldest-first and stops on the first failure, so ordering is preserved and a down server does not cause a long retry storm each minute.

### 4.7 Task execution

v1 defines exactly one task type: `collect_inventory`. The agent executes only task types it has a registered handler for; an unknown type is reported `failed` with `error: "unsupported task type"` rather than ignored, so the dashboard shows the truth. No shell strings are accepted from the server — the task `type` selects a Python function, and `params` are validated against that handler's expected keys. This upholds §4 and §13's prohibition on arbitrary command execution.

---

## 5. Installer

`agent/install.sh`, a POSIX shell script the administrator hands to the operator of the target device. Run as root.

1. **Preflight** — confirm Debian or Ubuntu via `/etc/os-release`; confirm `python3` ≥ 3.8; confirm `cron` is installed and its service active; confirm `dpkg-query` exists. Any failure aborts with a specific message before anything is written.
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

FastAPI, all routes under `/api/v1`, Pydantic v2 request and response models. OpenAPI is the agent's contract document.

### 7.1 Agent routes

Authenticated by `Authorization: Bearer sk_dev_...`. A revoked key or a disabled device returns `403` with a machine-readable `code`, so the agent can log a clear reason rather than retrying blindly. Every agent response includes the resolved `config` so a device that missed a heartbeat still converges.

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
    sentinel_agent/{cli,config,http,state,spool,resources,tasks,collectors/}.py
    bin/sentinel-agent
    install.sh
    tests/
  sim/
    Dockerfile                     # Ubuntu 22.04 + cron + python3
  docs/superpowers/specs/
```

`server/` and `dashboard/` share one built image with different commands; `agent/` is never installed into a server container.

### 9.2 Compose

`db` (PostgreSQL 16, named volume, health check), `api` (migrations on start, then uvicorn), `dashboard`, and in the override file `sim-device-1..3` built from `sim/Dockerfile`. Each sim device runs `cron -f` in the foreground as PID 1 — no init system, no privileged flag, no cgroup mounts, which is the practical payoff of the cron runtime model.

Environment-specific values live in `.env`: database credentials, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `PUBLIC_API_URL`, `LOG_LEVEL`. `.env.example` documents every variable. Secrets are never committed.

All of §3's operations work unmodified: `docker compose up -d`, `down`, `restart`, `logs -f`, `up -d --build`.

### 9.3 TLS

Local development uses plain HTTP over the Compose network. The VPS deployment adds a Caddy service in front of `api` and `dashboard` for automatic TLS, enabled by a Compose profile so local development does not need certificates. The agent refuses a non-HTTPS server URL unless `--insecure` is passed at enrollment, which the sim devices use and which is logged loudly on every beat — making the insecure case visible rather than silent, and keeping §4's HTTPS requirement true in production.

---

## 10. Testing

Per §15, automated tests from the start.

**Unit (server)** — the diff engine against hand-built before/after sets, including the out-of-order and superseded cases; name normalization per ecosystem; status derivation at each threshold boundary; key generation, hashing and revocation; config resolution with and without a per-device override.

**Unit (agent)** — every collector parser against captured real command output held as fixtures, plus the degenerate cases: missing binary, empty output, malformed JSON, timeout. Resource parsers against captured `/proc` files, including the first-beat `cpu_percent: null` case. Spool ordering and eviction at the size cap. Lock contention — a second `beat` while the first holds the lock must exit 0 without acting.

**Integration** — the real agent script against the real API and a real PostgreSQL, asserting the full sequence: enroll, beat, collect, diff produces the expected change rows, and a report delivered while the API is unreachable spools and then flushes on the next beat. These tests are what pin the agent/API contract, since the agent has no shared types with the server.

**End-to-end** — `docker compose up`, then `install.sh` runs inside a sim container and the device appears in `GET /devices` as `online` with a non-zero package count within two minutes. A second E2E case installs a package inside the sim container, queues `collect_inventory`, and asserts the change log shows the addition.

Verification is evidence-based: a test run's output, not an assertion that it should pass.

---

## 11. Deviations from `project_schema.md`

Recorded so the parent document can be reconciled later.

1. **No systemd service (§4).** The agent is a cron-invoked script, per explicit user direction. Consequences: §4's restart-on-failure behaviour is inherent rather than configured, and logs go to a rotating file rather than journald. A systemd timer remains a drop-in alternative later for machines wanting catch-up after downtime via `Persistent=true`.
2. **No full inventory snapshots (§14).** Current state plus a change log, for the storage reasons in §6.3. The intent of §14 — reconstructing changes over time — is satisfied.
3. **Spool has no 30-day age policy (§4).** Size cap only in v1; the age policy is a later tightening.
4. **Resource history is not stored (§3, §14).** Latest value only.
5. **Only the Devices tab exists (§8).** The other eleven tabs are later specs.

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
