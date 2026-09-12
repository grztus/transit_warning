# Transit Warning web frontend

The A2 frontend reads the versioned backend through `/api/v1/bootstrap`.
During local development, Vite proxies `/api` to
`http://127.0.0.1:8765` by default.

Install dependencies and start the development server:

```powershell
npm install
npm run dev
```

To test against a backend on another private LAN host, set the development
proxy target for the current PowerShell session before starting Vite:

```powershell
$env:VITE_BACKEND_TARGET = "http://<backend-host>:8765"
npm run dev
```

`VITE_BACKEND_TARGET` affects only the Vite development proxy. The browser
application continues to request `/api/v1/bootstrap`; production API paths and
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
routes remain same-origin. Vite does not run in production. The embedded legacy
dashboard remains temporarily available at `/legacy`; when `web/dist/index.html`
is absent, `/` also falls back to that legacy dashboard.

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
These runtime settings reset to configuration when the backend restarts.

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
locations, or PATTERN navigation. Its GPS acquisition has no map consumer.
