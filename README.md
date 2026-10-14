# Sentinel

Device inventory and management for Ubuntu machines. A cron-driven agent reports
host resources and installed software; a dashboard shows what is out there.

This is **v1**, covering enrollment, inventory and the Devices tab. The wider
scope in `project_schema.md` — vulnerability intelligence, matching, anomalies,
risk, predictive maintenance, MCP — is deliberately not built yet. The design
and its reasoning are in
[`docs/superpowers/specs/2026-10-14-device-inventory-v1-design.md`](docs/superpowers/specs/2026-10-14-device-inventory-v1-design.md).

## Running it

```sh
cp .env.example .env     # then edit ADMIN_PASSWORD
make up
```

- Dashboard: http://localhost:8501 (set `DASHBOARD_PORT` in `.env` if that port is taken; it binds to loopback only)
- API docs: http://localhost:8000/docs

Sign in with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`. They seed the
admin on first start only and are ignored afterwards; use
`docker compose exec api python -m app.cli reset-admin-password` to change it.

`make` on its own lists every target.

## Adding a device

1. **Devices → Add a device**, give it a name.
2. Copy the API key it shows. It is stored only as a SHA-256 hash and cannot be
   shown again — rotate the key if you lose it.
3. **Download installer for this device.** The script is self-contained: the
   agent source is embedded as a base64 bundle and the server URL and key are
   already filled in, so the device needs nothing from apt or PyPI.
4. On the machine:

   ```sh
   sudo bash sentinel-install-<name>.sh
   ```

The device appears within a minute. A generic installer that prompts for the URL
and key is also available on the same page.

`PUBLIC_API_URL` in `.env` is the address baked into downloaded installers, so
it has to be reachable **from the devices** — not `localhost`, once you deploy.

## How the agent runs

One cron entry, every minute:

```cron
* * * * * sentinel /opt/sentinel-agent/bin/sentinel-agent --quiet beat
```

Cron only supplies the tick; the agent decides what is due, which is why the
collection schedule stays changeable from **Settings**. Each beat sends a
heartbeat, applies any config change, collects inventory if the interval has
elapsed, runs queued tasks, and flushes anything spooled while offline. Nothing
stays resident between runs, and a `flock` means a slow collection can never
pile up invocations.

Standard library only, so the installer needs nothing but `python3`.

Manual use on a device:

```sh
sentinel-agent status     # enrollment, last contact, spool depth
sentinel-agent collect    # collect and report now
sentinel-agent beat -v    # one full pass, verbose
```

| Path | Contents |
|---|---|
| `/opt/sentinel-agent/` | the agent, root-owned |
| `/etc/sentinel-agent/config.json` | server URL, device ID, API key — mode 600 |
| `/var/lib/sentinel-agent/state.db` | spool, last-collect time, cached config |
| `/var/log/sentinel-agent/agent.log` | rotating log |

The agent runs as an unprivileged `sentinel` user, never root.

## What gets collected

OS packages (`dpkg-query`), Python packages per system interpreter
(`pip list`), global npm packages, installed JDKs, host facts, and CPU, memory,
disk, load and uptime.

Only system-level packages. Virtualenvs and per-user pyenv installs under other
users' home directories are not readable by the unprivileged agent, and
filesystem scanning for project dependencies is out of scope for v1.

A collector that cannot run is not a failure: the report is submitted with
whatever succeeded, and the device page says which collectors were skipped and
why.

## Development

```sh
make up            # start everything, including two simulated devices
make sim-enroll    # create devices and run the real installer inside them
make sim-collect   # force inventory collection now
make test          # 131 tests: server (pytest + Postgres) and agent
make logs
make clean         # stop and DELETE the database volume
```

The simulated devices are plain Ubuntu containers running `cron -f`. No init
system and no privileged flag are needed, because the agent is a cron entry
rather than a systemd service.

`./agent` is mounted into the API container, so editing the agent changes the
next downloaded installer without a rebuild.

## Deploying

`docker-compose.override.yml` is development-only — it adds the simulated
devices, mounts source over the image, and runs uvicorn with `--reload`. Deploy
with the base file plus the production override, which never loads it:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Set a real `ADMIN_PASSWORD` and `POSTGRES_PASSWORD`, and a `PUBLIC_API_URL`
devices can reach. Both services publish on **loopback only**, so something has
to terminate TLS in front: the agent refuses to send its key over plain HTTP
unless enrolled with `--insecure`, which is for local testing and is logged
loudly on every beat.

### Live deployment: fox

Running on the `fox` VPS, exposed publicly through a Tailscale funnel.

| | |
|---|---|
| Dashboard | `https://fox.tailcc00e1.ts.net:10000/` |
| API (for devices) | `https://fox.tailcc00e1.ts.net:10000/sentinel-api` |
| Project | `~/sentinel` |
| Host ports | `127.0.0.1:8511` dashboard, `127.0.0.1:8010` API |

Port 10000 because Funnel permits only 443, 8443 and 10000, and the first two
already serve other things on that host. Tailscale strips the mount prefix
before proxying, so the API sees `/api/v1/...` and `PUBLIC_API_URL` carries the
`/sentinel-api` prefix for agents.

The funnel was set up with:

```sh
sudo tailscale set --operator=$USER        # once, so funnel needs no sudo after
tailscale funnel --bg --https=10000 http://127.0.0.1:8511
tailscale funnel --bg --https=10000 --set-path=/sentinel-api http://127.0.0.1:8010
```

Inspect or remove it with `tailscale serve status` and
`tailscale funnel --https=10000 off`.

Redeploy after changes:

```sh
rsync -az --delete --exclude .git --exclude .env ./ fox:~/sentinel/
ssh fox 'cd ~/sentinel && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build'
```

**The funnel is open to the internet.** Anyone who knows the URL reaches the
login page, and admin auth is a password with no 2FA and no rate limiting —
that is v1's stated scope, not an oversight, but it is the thing to tighten
first if this holds anything sensitive. Change the password with:

```sh
ssh fox 'cd ~/sentinel && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.cli reset-admin-password'
```
