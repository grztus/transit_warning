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
