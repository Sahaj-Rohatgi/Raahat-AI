# Vendored frontend libraries

These files are served by the app itself (`/static/vendor/...`) instead of a public CDN, so the
frontend renders on a machine with no internet access — a venue network that blocks unpkg used to
mean a blank page.

| File | Version | Source |
|---|---|---|
| `react.production.min.js` | 18.3.1 | https://unpkg.com/react@18.3.1/umd/react.production.min.js |
| `react-dom.production.min.js` | 18.3.1 | https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js |
| `babel.min.js` | @babel/standalone 7 | https://unpkg.com/@babel/standalone@7/babel.min.js |
| `tailwind.js` | 3.4.5 (play CDN build) | https://cdn.tailwindcss.com/3.4.5 |

Re-download with the URLs above (`curl -sSL -o <file> <url>`) to upgrade a pinned version.
