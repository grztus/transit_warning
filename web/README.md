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
