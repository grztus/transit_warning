# Transit Warning web frontend

The supported STANDARD frontend initializes through `/api/v1/bootstrap`, follows
revisioned live/settings updates through `/api/v1/stream`, and changes runtime
settings through `/api/v1/settings`. It includes LIVE and HISTORY.
During local development, Vite proxies `/api` to
`http://127.0.0.1:8765` by default.

## Toolchain

Use Node.js **22.22.2+ within 22.x** or **24.15.0+ within 24.x** for the
locked build/test stack. `package-lock.json` pins jsdom 30.0.1 with
`^22.22.2 || ^24.15.0 || >=26.0.0`, Vitest 5.0.0 with
`^22.12.0 || ^24.0.0 || >=26.0.0`, and Vite 8.2.2 with
`^20.19.0 || >=22.12.0`. The two documented Node lines satisfy the complete
locked stack; an older Vite-only minimum is insufficient for the tests.
This checkpoint was validated with Node 24.19.0 and npm 11.17.0.

Install locked dependencies and start the development server:

```powershell
npm.cmd ci
npm.cmd run dev
```

To test against a backend on another private LAN host, set the development
proxy target for the current PowerShell session before starting Vite:

```powershell
$env:VITE_BACKEND_TARGET = "http://<backend-host>:8765"
npm.cmd run dev
```

`VITE_BACKEND_TARGET` affects only the Vite development proxy. The browser
application continues to use same-origin `/api/` paths; production API paths and
backend configuration are unchanged.

## Production

Use Vite during development:

```sh
npm run dev
```

Install the locked dependencies and create the production build with:

```sh
npm ci
npm run build
```

The generated frontend is written to `web/dist`. The existing Python dashboard
server serves that directory on the configured dashboard port: `/` serves the
React index, `/assets/` serves its static assets, and the existing `/api/`
routes remain same-origin. Vite does not run in production. The legacy page can
expose internal MANUAL terminology and is not the React observer presentation.
This compatibility/debug interface remains available at `/legacy`; when
`web/dist/index.html` is absent, `/` also falls back to that legacy dashboard.

## STANDARD operational controls

The top navigation contains LIVE and HISTORY. Controls start compact, with
STATIC/MOBILE observer selection, LOCAL/INTERNET/AUTO aircraft-source selection,
and independent Telegram SUN and MOON toggles. Expand Controls for diagnostics
and the fixed-location editor. The same controls wrap and stack on narrow screens.

STATIC uses the configured default location. Under Controls, choose Change
location to enter fixed latitude, longitude and elevation AMSL, then Apply or
Cancel. Custom fixed locations still appear as STATIC; the existing backend
MANUAL value is an internal compatibility detail. Use default location restores
the configured STATIC origin. Applying custom coordinates never starts GPS.
The last complete custom fixed position is saved through the backend
MANUAL-compatible storage path (`DASHBOARD_SETTINGS_PATH`, normally
`recordings/dashboard_settings.json`) and restored after backend restart.
Frontend reload/restart does not clear it. Startup mode still follows
`OBSERVER_MODE`; source, Telegram and fallback settings restore their configured
startup values. Storage must be writable for durable custom-location changes.

Selecting MOBILE starts or reuses browser GPS for transit calculations. Waiting,
active and attention feedback distinguish acquisition, backend MOBILE_FRESH
status and errors. Start GPS retries acquisition; Stop GPS stops the browser
watch and clears the mobile fix without changing the requested observer mode.
Selecting STATIC also stops GPS. Old watch callbacks and update completions are
ignored after stopping or replacing the watch. Browser location is never shown
in the dashboard. Observer fallback remains an independent backend setting.

Aircraft-source selection uses the existing revisioned settings API. Requested
mode, effective mode, provider and provider health come from backend state;
unavailable fields are shown as unavailable. The UI does not infer source
ownership or privacy-blocked status. Telegram SUN and MOON remain independent.

Backend state starts compact and shows data health and generated time. Expand
it for transport, last successful refresh and aircraft-source diagnostics.
Realtime/reconnecting/polling status is distinct from stale backend data and
confirmed connection failure. Delayed HTTP responses cannot overwrite newer
live/settings state received while those requests were running. Revision resets
remain possible after a backend restart when no newer state arrived meanwhile.

STANDARD has no maps, map-location controls, centerline interactions, saved
location collections, or PATTERN navigation. Its GPS acquisition has no map consumer.

Compact Controls is a single full-width disclosure button showing Controls,
the authoritative observer summary (including MOBILE NO FIX or fallback when
applicable), and a chevron. No individual controls or diagnostic banners appear
until expanded. Tap the row to reveal all observer/GPS/fallback, source, Telegram,
location and diagnostic controls. Collapsing keeps drafts and the GPS watch
intact; it only hides the controls. This behavior is shared by phone and desktop.

Browser geolocation requires a secure context (normally HTTPS) and browser
permission. Plain LAN HTTP may make geolocation unavailable; this is distinct
from permission denial. The physical Galaxy S23 smoke test succeeded through
the Tailscale secure origin, with backend MOBILE_FRESH and GPS ACTIVE. A running
browser watcher alone does not establish a valid backend fix. This does not
introduce a cloud requirement or change networking or backend GPS semantics.
