# Putting the site live

Pick one. All three keep everything in one folder (`/data`): the database, uploaded photos and videos, and the daily backups.

## Option A: a small server (Docker + automatic HTTPS)

1. Get a cheap VPS (Hetzner, DigitalOcean, any Ubuntu server) and install Docker.
2. Point your domain's A record at the server.
3. Copy the repo to the server and make `.env` (see `.env.example`): `DOMAIN=yourdomain.co.uk`, `OWNER_PASSWORD`, `SESSION_SECRET` (a long random string), `ANTHROPIC_API_KEY`, and whichever of Meta, Stripe, SMTP you use.
4. `docker compose up -d`. Caddy gets the HTTPS certificate by itself. Open `https://yourdomain.co.uk/owner`.
5. Update later: `git pull && docker compose up -d --build`. The data volume is kept.

## Option B: Fly.io

See the comments at the top of `fly.toml` (about five commands). The `pashtoon_data` volume holds `/data`.

## Option C: Railway, Render or similar

Deploy the Dockerfile, add a **persistent disk mounted at `/data`**, set the environment variables, and use `/healthz` as the health check. Without a disk the data is lost on every deploy.

## Settings that matter

| Variable | What it does |
|---|---|
| `PASHTOON_BASE_URL` | The public address (https). Used in links, Stripe and Meta callbacks. |
| `OWNER_EMAIL` | The owner's login email. |
| `OWNER_PASSWORD` | Temporary first password; the owner must change it at first login. To apply or reset on a site that already has data: put both in `.env`, then run `docker compose exec web python -m pashtoon_site.owner_setup` (or `python -m pashtoon_site.owner_setup` after `set -a; . ./.env; set +a`). Remove the password from `.env` afterwards. |
| `OWNER_MUST_CHANGE` | `1` makes a first-start password temporary too. |
| `SESSION_SECRET` | Long random string so logins survive restarts. |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` + `AI_PROVIDER=openai` | The assistant. |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | Email alerts and the weekly emailed backup. |
| `WHATSAPP_*`, `META_*`, `INSTAGRAM_TOKEN` | Customer messaging and WhatsApp alerts. |
| `STRIPE_*` | Card, Apple Pay, Google Pay. |
| `PASHTOON_BACKUP_KEEP` | How many daily backups to keep (default 30). |

Keys go only in `.env` or the host's secret settings. `.env` is ignored by git.

## Health and backups

- `GET /healthz` returns `{"ok": true, "last_backup_hours": …}`. Docker, Fly and Railway use it to restart the site if it stops answering.
- A backup (database plus uploads, one zip) is made every day into `/data/backups`, newest 30 kept. Settings has Back up now and Download. Tick "email me the weekly backup" (needs SMTP) so a copy also leaves the server.
- **Restore:** stop the site, unzip the backup, put `pashtoon_site.db` in `/data` (replacing the old one) and `uploads/` beside it, start the site.
- Run **one** copy of the site: the background jobs (backups, held alerts, tiffin meals) run inside it.

## After it is live

1. Settings > Meta: set the callback URL and verify token (the page shows them), and the privacy URL `/privacy`.
2. Stripe: add the webhook URL from Settings.
3. Add the site to the phone: open `/owner` and use Add to Home Screen.
4. Send a test alert from Settings.
