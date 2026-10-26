# Redline UI

React 19 + Vite + TypeScript + Tailwind single-page app. It is built into
`packages/redline-core/src/redline_core/ui/static/` and served by the framework adapters.

## Develop

```bash
npm install
npm run dev          # http://localhost:5173/?mock=1
```

All API calls are relative (`./api/...`), and routing is hash based (`#/requests`, `#/requests/:id`,
`#/endpoints`, `#/issues`), so the build works at any mount prefix.

## Mock backend (dev only)

Open the dev server with `?mock=1` (remembered for the browser session) and the UI talks to the
fixtures in `src/mocks/` instead of a server. In dev it also falls back to the mock automatically when
`./api/meta` is unreachable or does not return JSON. The fixtures cover an N+1 with a diff, a duplicate
query, a slow query, an unhandled error with redacted bodies, EXPLAIN results for PostgreSQL and
SQLite, and a speedscope profile.

The mock sits behind `import.meta.env.DEV` and a dynamic import, so Vite removes it from production
builds.

## Build

```bash
npm run build        # tsc -b && vite build -> ../packages/redline-core/src/redline_core/ui/static
# or from the repo root:
scripts/build_ui.sh
```

## Keyboard

`j` / `k` move in the request list, `Enter` opens, `/` focuses search, `Esc` closes drawers and dialogs.
