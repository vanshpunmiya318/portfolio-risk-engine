# Portfolio Risk Engine frontend

Independent React + TypeScript + Vite application. The Python, Streamlit and FastAPI applications remain unchanged.

## Development

Use a Node.js version supported by the installed Vite version. Run these commands from `frontend/`:

```sh
npm ci
npm run dev
npm run build
npm run preview
```

On Windows PowerShell with script execution restricted, use `npm.cmd` instead of `npm`.

The build runs strict TypeScript checking before bundling. Commit the lockfile; dependencies and build output are ignored.

## Structure

- `src/App.tsx`: shared shell and seven React Router routes.
- `src/api/`: typed summary client with runtime response validation.
- `src/lib/`: currency and calendar-date formatting.
- `src/pages/`: route content.
- `src/theme/`: persisted light/dark/system preference, including live OS theme changes.
- `src/styles/tokens.css`: semantic colors, spacing, typography and shape tokens.
- `src/styles/base.css`: global primitives and accessibility defaults.
- `src/styles/shell.css`: responsive shell and placeholder styles.

Overview fetches `/api/portfolio/summary` and displays actual API values, with loading, retryable error and empty states. It performs no financial calculations and includes no sample financial data. Other pages remain placeholders.

Start FastAPI separately on `127.0.0.1:8000`. The Vite development server proxies `/api` to that address. Run `npm.cmd run dev` and open the printed frontend URL.

Production hosting must serve `index.html` for client-side routes such as `/overview` (SPA fallback), and forward `/api` to FastAPI on the same origin. The development proxy is not a production deployment configuration. Authentication is deferred.
