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

## Updating agents

Agents update themselves. Edit the agent code, deploy, and machines pick up the
new version on their next heartbeat — no installer, no SSH, nothing to upload.

```
edit agent code  →  deploy  →  machines update themselves within a minute
```

**The version is a hash of the agent source**, so any edit produces a new
version automatically. There is no number to bump and no way to forget. The
deployed code is the source of truth, which means reverting your checkout and
redeploying rolls the fleet back too — `git revert` is a working emergency
brake.

On each machine the code lives in versioned directories with a `current`
pointer:

```
/opt/sentinel-agent/
├── bin/sentinel-agent      stable launcher - updates never replace this
├── versions/<hash>/        one directory per version
└── current -> versions/<hash>
```

**Nothing privileged is involved.** The tree belongs to the agent's own user,
and swapping the `current` symlink is atomic.

### What stops a bad version taking the fleet down

1. **Checksum.** A truncated or corrupted download is rejected before anything
   is written.
2. **Self-test before the swap.** The new code runs its own `selftest` as a
   separate process. If it cannot even import, nothing is swapped and the
   running version is untouched.
3. **Probation.** After a swap, the new version has three minutes to complete
   one full beat. If it does not, the stable launcher puts the previous version
   back — this is the only reason that launcher exists.
4. **Quarantine.** The first machine to report an update failure causes the
   server to stop offering that version to anyone else, and the dashboard says
   so. One machine has a bad minute instead of the whole fleet going dark.

The previous version stays on disk until a newer one has proven itself, so
there is always something known-good to fall back to.

### Seeing where the fleet is

**Settings** shows the current version and how many machines have it. The
Devices list has an Agent column:

| Shown | Means |
|---|---|
| Up to date | running the current version |
| Update pending | on an older version, not picked up yet — this is what an offline machine looks like |
| Rolled back | took the version, it failed, reverted itself |
| Held back | the version is quarantined and is not being offered |
| Not managed | agent predates self-updating; run the installer once |

A quarantined version clears by itself when you deploy a new one, since
quarantine applies to one hash. There is also a "try this version again"
button in Settings for when a failure was a fluke.

### Bootstrapping

An agent installed before self-updating existed cannot update itself to the
version that adds it — it has no handler for the job. Those machines show
**Not managed** and need the installer run once; after that they update on
their own forever. Re-running the installer keeps the device's identity and
history, so this is not disruptive.

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
sentinel-agent status     # enrollment, versions on disk, last contact, spool depth
sentinel-agent collect    # collect and report now
sentinel-agent beat -v    # one full pass, verbose
sentinel-agent selftest   # check this build can run (used before an update applies)
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

### Behind a reverse proxy

Both services publish on loopback, so put something in front that terminates
TLS. Any reverse proxy works; a Tailscale funnel is a quick option that
provides a certificate automatically:

```sh
tailscale funnel --bg --https=443 http://127.0.0.1:8501
tailscale funnel --bg --https=443 --set-path=/api http://127.0.0.1:8000
```

Tailscale strips the mount prefix before proxying, so with that layout
`PUBLIC_API_URL` carries the `/api` prefix while the application still sees
clean `/api/v1/...` paths. Funnel permits only ports 443, 8443 and 10000.

Redeploy after changes:

```sh
rsync -az --delete --exclude .git --exclude .env ./ <host>:~/sentinel/
ssh <host> 'cd ~/sentinel && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build'
```

`--build` is not optional for agent changes. Production does not mount
`./agent` — only the development override does — so the agent source the
installer embeds is baked into the API image. Syncing the files alone updates
nothing that gets served.

**Anything you expose publicly is reachable by anyone who knows the URL.**
Admin auth is a password with no 2FA and no rate limiting; that is v1's stated
scope, not an oversight, but it is the thing to tighten first. Change the
password with:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.cli reset-admin-password
```
