# Google Calendar sync

Every order becomes an event in a calendar called "<shop name> orders" in Aarzu's own Google account. New orders
appear, changes (time, status, payment) update the same event, and cancelled orders are marked CANCELLED. Each order
has a fixed event id, so there are never duplicates.

## One-time setup (about 10 minutes)

1. Go to https://console.cloud.google.com, create a project, and enable the **Google Calendar API**.
2. **OAuth consent screen**: user type External, add her Google account as a test user (no Google review needed while it is used by her alone).
3. **Credentials > Create credentials > OAuth client ID > Web application**. Under *Authorised redirect URIs* add
   `<your site address>/owner/google/callback` (shown in Owner > Settings > Google Calendar).
   For Codespaces use the forwarded address, for example `https://xxxx-8000.app.github.dev/owner/google/callback`.
4. Put the client id and secret in `.env` (never in chat or code):
   `GOOGLE_CLIENT_ID=...` and `GOOGLE_CLIENT_SECRET=...`, then restart the site.
5. Owner > Settings > **Connect Google Calendar**, sign in, allow access. Existing and future orders are added.

Notes
- The site only asks for `calendar.app.created`: it can see and change only the calendar it made, not the rest of her calendars.
- While the OAuth app is in "Testing" status Google expires the sign-in after 7 days; publish the app (or re-connect) for a permanent link.
- The refresh token is stored in the site's database (so it is inside backups). Treat backups as private.
- Syncing runs right after an order changes and again every minute, so a Google outage is retried automatically.
- Disconnect in Settings stops syncing; events already in Google stay.
