# STANDARD Debian deployment smoke test

Run this checklist manually on the deployment after approving the release commit.
No production host was accessed during the local release checkpoint. Physical
phone/browser testing remains required. Use the existing private access transport;
the dashboard does not add authentication. Do not publish private logs or locations.

## 1. Select the deployment and verify Git

Replace each placeholder with the actual existing deployment value. The repository
does not define a systemd unit name or a universal virtualenv/data path. Set
`EXPECTED_COMMIT` to the approved documentation release commit from the final
report (or the explicitly approved promotion commit). Use `main` after promotion,
or `standard-sync` for an explicitly authorized test deployment.

```sh
REPO='/absolute/path/to/existing/deployment'
SERVICE='existing-service.service'
PYTHON='/absolute/path/to/deployment/venv/bin/python'
RELEASE_BRANCH='main'
EXPECTED_COMMIT='<approved-full-commit-hash>'
cd "$REPO"
git status --short
# Stop if unexpected local changes exist; preserve .env and recordings.
git fetch origin
git switch "$RELEASE_BRANCH"
git pull --ff-only origin "$RELEASE_BRANCH"
git rev-parse HEAD
test "$(git rev-parse HEAD)" = "$EXPECTED_COMMIT"
```

Stop on any failed command or unexpected commit. This checklist authorizes no
merge, force update, or replacement of local settings. Confirm the installed unit's
working directory, interpreter, private environment and graceful-shutdown timeout:

```sh
sudo systemctl cat "$SERVICE"
"$PYTHON" --version
"$PYTHON" -m pip install -r requirements.txt
node --version
npm --version
(cd web && npm ci && npm test && npm run typecheck && npm run build)
```

Use Node 22.22.2+ within 22.x or 24.15.0+ within 24.x. Verify the Debian EGM96
PGM exists and is readable by the service user. For authoritative TRUE_2D, confirm
`AUTHORITATIVE_PREDICTION_GEOMETRY=TRUE_2D`; missing datum data must not be
silently replaced with LEGACY. Keep existing `.env`, settings, history and recorder
paths. Vite is not the production server; Python serves the built `web/dist`.

## 2. Restart and inspect backend health (immediate)

```sh
sudo systemctl restart "$SERVICE"
sudo systemctl is-active "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
sudo journalctl -u "$SERVICE" --since '10 minutes ago' --no-pager
```

Confirm the old process shut down cleanly, workers and recorder closed, and the
new process starts without traceback. Check no repeated publication failures,
worker shutdown warnings or accumulating delay. Request `/api/v1/bootstrap`
through the existing private dashboard URL: it must return a schema-v1 snapshot,
with advancing generated time when the live main loop is running. An empty LIVE
list is valid without candidates. Repeated dirty generations must eventually be
published; deferred jobs must execute when eligible data arrive. Existing private
terminal diagnostics expose scheduler/publisher counters; there is no promised
new public metrics endpoint. Confirm worker liveness using those diagnostics
when available, and distinguish an idle worker from a failed one.

## 3. Sources (immediate with a permitted fixed observer)

Use the React controls and check requested mode, effective mode and provider
health after every transition: LOCAL -> INTERNET -> LOCAL -> AUTO -> INTERNET
-> AUTO -> LOCAL. LOCAL must keep receiver ingestion; INTERNET must use remote
predictions; AUTO must retain local ingest and field-level remote fallback.
Switching must clear incompatible old ownership/candidates without deleting a
newer replacement. Provider errors or missing eligible data must be explicit,
without frozen data being presented as fresh. Observe recovery when evidence
returns; do not induce network outages on production solely for this check.
Requested MOBILE must show provider privacy blocking, even with static fallback.
Replay isolation was tested synthetically; do not start replay inside the live
service or mix it with production inputs.

## 4. Observer and synchronization (immediate)

Open two clients. Select STATIC/default, then Change location with authorized test
coordinates and Apply; both clients must show STATIC/custom and the same
privacy-safe effective state. Use default location to restore the configured
observer. Confirm a browser reload retains the saved custom editor values;
backend restart restores saved coordinates but startup mode follows configuration.

Select MOBILE in a secure browser context, grant GPS permission, and check waiting,
fresh, stale/fallback/no-fix feedback as appropriate. Verify calculation elevation
remains configured AMSL; browser altitude is diagnostic only. Select STATIC and
confirm that client's GPS watch stops, its old callbacks cannot reactivate it,
and the other client converges to STATIC diagnostics. Reconnect/reload the browser;
transport recovery alone must not label stale generated data ACTIVE. Restore the
intended production observer and permissions after testing.

## 5. Telegram (toggles immediate; delivery candidate-dependent)

Toggle SUN independently of MOON, then MOON independently of SUN. Confirm both
clients converge, the other body remains unchanged, and settings match the intended
production state. Disabled delivery must not suppress prediction/history.

When an eligible candidate is available, verify LOCAL notification delivery, then
INTERNET/AUTO through the common pipeline. Where a direct source handoff can be
observed, check no duplicate alert for the inherited notification window. Record
these as pending if no candidate qualifies; no real transit is required. Do not
manufacture production alerts merely to satisfy this checklist. Queue acceptance
is not proof of successful Telegram network delivery.

## 6. UI and physical browser (immediate)

Review 320, 360, 390, 768 px and desktop widths, plus a physical phone/browser.
Check LIVE/HISTORY only; STATIC/MOBILE only; LOCAL/INTERNET/AUTO; independent
SUN/MOON; compact and expanded Controls/Backend state; fixed-location editor;
long callsigns/status text; no horizontal page overflow. Check HISTORY filters,
pagination and CSV. There must be no PATTERN tab, map controls, consent flow,
fullscreen map or device-location map marker. `/legacy` is compatibility/debug
UI, not the React presentation. Test temporary browser disconnection and recovery.

## 7. Recorder, history and finalization

With the existing recording policy enabled, confirm receiver-stream files advance
and archives close cleanly on controlled shutdown. INTERNET/AUTO snapshots must
not be converted to local receiver messages. Candidate capture and full-session
capture remain separate. Existing HISTORY must stay readable immediately; new
withdrawal/passed/finalization records and their provenance can be checked when
an appropriate candidate is available. No real transit is required for withdrawal
validation. Mark unavailable candidate-dependent checks as pending.

## 8. Performance (immediate baseline, then normal-load observation)

```sh
PID="$(systemctl show -p MainPID --value "$SERVICE")"
ps -p "$PID" -o pid,etime,%cpu,%mem,nlwp
ss -tinp
sudo journalctl -u "$SERVICE" --since '10 minutes ago' --no-pager
```

Compare delay, CPU and TCP Recv-Q to the deployment's normal baseline over several
minutes in each practical source mode. Review private scheduler pending/capacity,
expiry/failure and solve counters, plus publisher dirty/published generations,
coalescing/failure/duration and worker counters if exposed. Pending scheduler keys
must stay within 32 plus one running job; publisher work coalesces without a
snapshot queue. Local synthetic benchmark timings are not production limits.
Keep endpoint-bearing command output private. Record commit, interpreter/toolchain,
results and any pending candidate checks. Stop rollout on traceback, stale state
reported fresh, source contamination, missing workers or sustained new backlog.
