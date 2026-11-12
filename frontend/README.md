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

- `src/App.tsx`: shared shell and seven React Router routes; Overview and future-page placeholders only.
- `src/pages/`: route content.
- `src/theme/`: persisted light/dark/system preference, including live OS theme changes.
- `src/styles/tokens.css`: semantic colors, spacing, typography and shape tokens.
- `src/styles/base.css`: global primitives and accessibility defaults.
- `src/styles/shell.css`: responsive shell and placeholder styles.

No backend calls, financial calculations or sample financial data are included. Future services should return calculated data from FastAPI rather than duplicating Python calculations here.

Production hosting must serve `index.html` for client-side routes such as `/overview` (SPA fallback). Vite handles this locally. API proxying and authentication are intentionally deferred until backend integration.
