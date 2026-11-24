# NIVESHAM — Investment Intelligence Frontend

React + Vite + Tailwind CSS + Recharts. A public landing page at `/`, a sign-in page, and a top-navigation workspace for signed-in users.

## Backend connection

Set `VITE_API_BASE_URL` in `.env` (default: `http://localhost:8000/api/v1`).

Authentication uses the backend `POST /auth/token` form endpoint and `GET /auth/me`.

## Structure

- `/` public landing page (`src/pages/Home.jsx`)
- `/login` sign-in page (`src/pages/Login.jsx`)
- Signed-in workspace: top navigation with grouped menus (`src/components/Layout.jsx`)
- Brand mark and wordmark: `src/components/Brand.jsx`, `public/nivesham-logo.svg`
- Design tokens: `tailwind.config.js` and `src/index.css`

## Important: no mock data

The frontend contains no sample investment records, fake companies, fake news, fake signals, fake metrics, or placeholder business values. Empty backend responses render explicit empty states.

Some client-brief features cannot be made functional without backend endpoints. The UI therefore explains those gaps rather than fabricating data: company names for coverage (backend exposes company_id only), notification feed, password changes, active session tokens, OneDrive configuration, rate-limit management, and richer dashboard task/bookmark data.

## Run

```bash
npm install
npm run dev
```

Build:

```bash
npm run build
```

The app is intended to run beside the supplied FastAPI backend.

## Production container

The repository includes `Dockerfile` and `nginx.conf`. The root backend compose file builds the frontend as an nginx static site on port 3000.
