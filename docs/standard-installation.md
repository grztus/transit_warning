# STANDARD Linux installation and service example

Start with the [public quick start](../README.md#quick-start). Linux/Debian is
supported with a modern Python interpreter; the validated deployment used Python
3.11.15. Older Debian releases may need a separately installed interpreter and
its matching venv/pip support. Do not replace `/usr/bin/python3` or assume `pip3`
installs into the interpreter used by the service.

## Deployment checklist

1. Choose a deployment directory and an unprivileged service account. Make the
   checkout, `.env` and geoid grid readable by that account. Only that account and
   authorized administrators should have access to private configuration/data.
2. Create a venv with the chosen Python, then install `requirements.txt` with
   that venv's `python -m pip`. Keep the same interpreter in `ExecStart`.
3. Copy `.env.example` to `.env` only for a new installation; preserve existing
   configuration during upgrades. Fill required fields and feed endpoints, enable
   the dashboard, and configure geometry/geoid data as described in the README.
4. With supported Node, run `cd web`, `npm ci`, `npm run build`. Deploy the resulting
   `web/dist/` with the checkout. Vite is development-only.
5. Confirm the service account can write the configured history, custom-location
   settings, diagnostic and recording directories. Default paths are relative to
   the project working directory. Do not discard these directories on update.
6. Run the application interactively with the chosen interpreter first. Verify
   configuration, receiver/provider access and geoid discovery; stop with Ctrl+C.
7. Enable the service only after adapting and reviewing the example below. Use
   the [release checklist](standard-release-smoke-test.md) for updates and checks.

## Optional systemd unit

This is a template, not a file installed by the repository. Replace
`YOUR_SERVICE_USER` and both `/opt/transit-warning` paths with your chosen account
and deployment directory. The account must already exist. Save the adapted unit
as `/etc/systemd/system/transit-warning.service` with administrator privileges.

```ini
[Unit]
Description=Transit Warning STANDARD
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=YOUR_SERVICE_USER
WorkingDirectory=/opt/transit-warning
ExecStart=/opt/transit-warning/.venv/bin/python -u /opt/transit-warning/transit_warning.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=300
UMask=0077

[Install]
WantedBy=multi-user.target
```

The application loads `.env` from its project directory. No credentials belong
in the unit example. `SIGINT` invokes the existing KeyboardInterrupt/finally
shutdown path; the application does not install a SIGTERM shutdown handler.
The five-minute timeout is an example: allow enough time for your recorder ZIP
finalization and normal shutdown, rather than assuming one universal duration.
Forced termination can leave incomplete archives and preserved loose streams.

After adapting the unit:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now transit-warning.service
sudo systemctl status transit-warning.service --no-pager
sudo journalctl -u transit-warning.service --since '10 minutes ago' --no-pager
```

Tmux is optional for an interactive terminal session, not required by the backend
or this service. No existing production service is changed by these instructions.
For phone GPS, expose the localhost-bound dashboard through an appropriate secure
origin; follow [MOBILE guidance](../README.md#mobile-needs-a-secure-browser-context).
