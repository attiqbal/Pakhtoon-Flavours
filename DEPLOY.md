# Putting the site live

Pick one. All three keep everything in one folder (`/data`): the database, uploaded photos and videos, and the daily backups.

## Option A: a small server (Docker + automatic HTTPS)

1. Get a cheap VPS (Hetzner, DigitalOcean, any Ubuntu server) and install Docker.
2. Point your domain's A record at the server.
3. Copy the repo to the server and make `.env` (see `.env.example`): `DOMAIN=yourdomain.co.uk`, `SESSION_SECRET` (a long random string), `ANTHROPIC_API_KEY`, and whichever of Meta, Stripe, SMTP you use.
4. `docker compose up -d`. Caddy gets the HTTPS certificate by itself. Open `https://yourdomain.co.uk/owner`.
5. Update later: `git pull && docker compose up -d --build`. The data volume is kept.

## Option B: Fly.io

See the comments at the top of `fly.toml` (about five commands). The `pashtoon_data` volume holds `/data`.

## Option C: Railway, Render or similar

Deploy the Dockerfile, add a **persistent disk mounted at `/data`**, set the environment variables, and use `/healthz` as the health check. Without a disk the data is lost on every deploy.

## The address: pashtoonflavours.uk, and the old pakhtoonflavours.uk

The business is **Pashtoon Flavours** and the address is **https://pashtoonflavours.uk**. The earlier **pakhtoonflavours.uk** must keep working (old links, printed QR codes) by redirecting to the new one, with the same page and query.

1. **Domain DNS** (Cloudflare or your registrar): for `pashtoonflavours.uk` and `www.pashtoonflavours.uk` add the same records the old domain uses (an A record to the server's IP, or a CNAME to the host's address if you use Fly.io or Railway). Keep the old `pakhtoonflavours.uk` and `www.pakhtoonflavours.uk` records pointing at the **same** server, so the redirect can run.
2. **HTTPS**: with the Docker option, the included `Caddyfile` gets certificates for all four names once their DNS points at the server. On Fly.io or Railway, add both domains in the host's settings (`fly certs add pashtoonflavours.uk`, and the same for the old one). If Cloudflare proxies the domain (orange cloud), set SSL/TLS to **Full (strict)**.
3. **Settings**: set `DOMAIN=pashtoonflavours.uk` in `.env` (Docker) or `PASHTOON_BASE_URL=https://pashtoonflavours.uk` (other hosts), then restart: `docker compose up -d --build`. The site then redirects `pakhtoonflavours.uk`, `www.pakhtoonflavours.uk` and `www.pashtoonflavours.uk` to `https://pashtoonflavours.uk` with a permanent redirect (301; 308 for form posts). `OLD_HOSTS` (comma separated) changes the list of old addresses; `OLD_HOSTS=none` turns it off. `/healthz` is never redirected.
4. **Optional, at Cloudflare instead of (or as well as) the app**: Rules > Redirect Rules > "Redirect from old domain": when hostname equals `pakhtoonflavours.uk` or `www.pakhtoonflavours.uk`, dynamic redirect to `concat("https://pashtoonflavours.uk", http.request.uri.path)` with status 301 and **Preserve query string** on. The domain must be proxied (orange cloud) for the rule to run.
5. **Check**: `curl -sI https://pakhtoonflavours.uk/m?x=1` should show `301` and `location: https://pashtoonflavours.uk/m?x=1`, and `https://pashtoonflavours.uk/` should open the site. Then open the sharing page in the owner dashboard and make a new QR sheet and menu picture: they use the new address. Old printed QR codes keep working through the redirect.
6. **After the switch**: ask WhatsApp to re-read the link preview by sharing the new link once (a link shared earlier may keep its old picture for a while), and update the callback addresses in Meta and Stripe to `https://pashtoonflavours.uk/webhook/...` (they are shown in Settings).

## Settings that matter

| Variable | What it does |
|---|---|
| `PASHTOON_BASE_URL` | The public address (https): `https://pashtoonflavours.uk`. Used in links, Stripe and Meta callbacks. |
| `OLD_HOSTS` | Old addresses that redirect to the public address. Default `pakhtoonflavours.uk,www.pakhtoonflavours.uk`; `none` turns it off. |
| `OWNER_EMAIL`, `OWNER_PASSWORD` | Optional. The owner login is created automatically on first start (see README); set these before the first start only if you want a different email or first password. |
| `RESET_OWNER_LOGIN` | Only if the owner cannot sign in (for example the site was first started with another password, which is kept on later starts). Set it to any new value, such as today's date, and restart: the login goes back to the owner email and the starter password, once. The same value on later restarts does nothing, so it never undoes a password she chose. Remove it afterwards if you like. |
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
