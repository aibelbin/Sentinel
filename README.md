# Sentinel

Device inventory and management for Ubuntu and Debian machines. A cron-driven watchdog reports
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

The normal way is the Debian package. It declares `python3` and `cron` as
dependencies, so apt installs whatever the machine is missing and nothing has
to be prepared by hand.

1. **Devices → Add a device**, give it a name, and copy the API key. It is
   stored only as a SHA-256 hash and cannot be shown again — rotate the key if
   you lose it.
2. Download the package (**Settings → Watchdog package**, or
   `GET /api/v1/watchdog-package`).
3. On the machine:

   ```sh
   sudo apt install ./sentinel-watchdog_1.0.0+<version>_all.deb
   sudo sentinel-watchdog enroll --server https://your-server --key sk_dev_...
   ```

Enrolment is a separate step because the package is identical for every
machine and a device key is not — a package cannot carry one. Until it is
enrolled the watchdog sits quietly; it does not fail or mail you.

From the server's own shell, one command does the registration and writes the
package:

```sh
docker compose exec -T api python -m app.cli create-device --name web-01 --deb
```

It prints the `apt install` and `enroll` lines with the key filled in.

### The self-contained script

For machines that cannot reach an apt mirror, the dashboard also offers a
single shell script with the watchdog embedded as a base64 bundle and the URL and
key already filled in:

```sh
sudo bash sentinel-install-<name>.sh
```

It installs nothing from the network. Because it bypasses apt it cannot resolve
dependencies, so it checks for `python3` and `cron` and tells you what is
missing; pass `--install-deps` to let it apt-install them itself. That is off
by default — installing packages on someone else's machine should be asked for,
not assumed.

Either way the device appears within a minute.

`PUBLIC_API_URL` in `.env` is the address baked into downloaded installers, so
it has to be reachable **from the devices** — not `localhost`, once you deploy.

## Liva, the assistant

The **Liva** tab is a chat assistant that can do what the UI does: ask about
any device, its packages, changes and task history, create a device (she shows
the key, the `.deb` and the enroll commands), rotate keys, edit the config. It
runs as its own container (`liva`) on Groq (`GROQ_API_KEY` in `.env`, free at
console.groq.com) and reaches Sentinel only through the API with your own
session, so it can do nothing you cannot.

- Reads and harmless writes run on their own. Deleting a device, rotating a
  key, changing config and clearing quarantine wait for an **Approve** click.
- The **Bypass approvals** toggle turns those prompts off for the current
  session, with a red banner while it is on.
- Device keys are shown to you on screen and never sent to Groq or stored.
- Chats and an audit log of every tool call are kept for 30 days in Liva's own
  SQLite volume. If Liva is down or unconfigured, the rest of the dashboard
  works as usual.

The tools are a real MCP server inside the `liva` container (localhost only).

## Updating watchdogs

Watchdogs update themselves. Edit the watchdog code, deploy, and machines pick up the
new version on their next heartbeat — no installer, no SSH, nothing to upload.

```
edit watchdog code  →  deploy  →  machines update themselves within a minute
```

**The version is a hash of the watchdog source**, so any edit under
`watchdog/sentinel_watchdog/` produces a new version automatically. There is no
number to bump and no way to forget.

The one exception is `watchdog/bin/sentinel-watchdog`, the launcher. It is
deliberately outside the update bundle — updates must never be able to replace
the piece that puts a broken version back — so **editing it does not produce a
new version and will not reach existing machines.** Changing the launcher means
re-running the installer, which is why it is kept small and boring.

The
deployed code is the source of truth, which means reverting your checkout and
redeploying rolls the fleet back too — `git revert` is a working emergency
brake.

On each machine the code lives in versioned directories with a `current`
pointer:

```
/opt/sentinel-watchdog/
├── bin/sentinel-watchdog      stable launcher - updates never replace this
├── versions/<hash>/        one directory per version
└── current -> versions/<hash>
```

**Nothing privileged is involved.** The tree belongs to the watchdog's own user,
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
Devices list has a Watchdog column:

| Shown | Means |
|---|---|
| Up to date | running the current version |
| Update pending | on an older version, not picked up yet — this is what an offline machine looks like |
| Rolled back | took the version, it failed, reverted itself |
| Held back | the version is quarantined and is not being offered |
| Not managed | watchdog predates self-updating; run the installer once |

A quarantined version clears by itself when you deploy a new one, since
quarantine applies to one hash. There is also a "try this version again"
button in Settings for when a failure was a fluke.

### Bootstrapping

A watchdog installed before self-updating existed cannot update itself to the
version that adds it — it has no handler for the job. Those machines show
**Not managed** and need the installer run once; after that they update on
their own forever. Re-running the installer keeps the device's identity and
history, so this is not disruptive.

## How the watchdog runs

One cron entry, every minute:

```cron
* * * * * sentinel /opt/sentinel-watchdog/bin/sentinel-watchdog --quiet beat
```

Cron only supplies the tick; the watchdog decides what is due, which is why the
collection schedule stays changeable from **Settings**. Each beat sends a
heartbeat, applies any config change, collects inventory if the interval has
elapsed, runs queued tasks, and flushes anything spooled while offline. Nothing
stays resident between runs, and a `flock` means a slow collection can never
pile up invocations.

Standard library only, so the installer needs nothing but `python3`.

Manual use on a device:

```sh
sentinel-watchdog status     # enrollment, versions on disk, last contact, spool depth
sentinel-watchdog collect    # collect and report now
sentinel-watchdog beat -v    # one full pass, verbose
sentinel-watchdog selftest   # check this build can run (used before an update applies)
```

| Path | Contents |
|---|---|
| `/opt/sentinel-watchdog/` | the watchdog, root-owned |
| `/etc/sentinel-watchdog/config.json` | server URL, device ID, API key — mode 600 |
| `/var/lib/sentinel-watchdog/state.db` | spool, last-collect time, cached config |
| `/var/log/sentinel-watchdog/watchdog.log` | rotating log |

The watchdog runs as an unprivileged `sentinel` user, never root.

## What gets collected

OS packages (`dpkg-query`), Python packages per system interpreter
(`pip list`), global npm packages, installed JDKs, host facts, and CPU, memory,
disk, load and uptime.

Only system-level packages. Virtualenvs and per-user pyenv installs under other
users' home directories are not readable by the unprivileged watchdog, and
filesystem scanning for project dependencies is out of scope for v1.

A collector that cannot run is not a failure: the report is submitted with
whatever succeeded, and the device page says which collectors were skipped and
why.

## Development

```sh
make up            # start everything, including two simulated devices
make sim-enroll    # create devices and run the real installer inside them
make sim-collect   # force inventory collection now
make test          # 131 tests: server (pytest + Postgres) and watchdog
make logs
make clean         # stop and DELETE the database volume
```

The simulated devices are plain Ubuntu containers running `cron -f`. No init
system and no privileged flag are needed, because the watchdog is a cron entry
rather than a systemd service.

`./watchdog` is mounted into the API container, so editing the watchdog changes the
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
to terminate TLS in front: the watchdog refuses to send its key over plain HTTP
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

`--build` is not optional for watchdog changes. Production does not mount
`./watchdog` — only the development override does — so the watchdog source the
installer embeds is baked into the API image. Syncing the files alone updates
nothing that gets served.

**Anything you expose publicly is reachable by anyone who knows the URL.**
Admin auth is a password with no 2FA and no rate limiting; that is v1's stated
scope, not an oversight, but it is the thing to tighten first. Change the
password with:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.cli reset-admin-password
```
