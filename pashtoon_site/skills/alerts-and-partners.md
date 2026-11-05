---
name: alerts-and-partners
for: owner
---
- Alerts: alert_settings changes how and when she is told about new orders when the dashboard is closed (WhatsApp, the free ntfy app, email), which kinds she wants, and quiet hours (held alerts arrive as one summary). Use test true after a change. WhatsApp alerts need the WhatsApp keys; email needs SMTP in the settings file. If a test fails, say what the log says and what is missing.
- Delivery apps: Uber Eats, Deliveroo and Just Eat orders reach the site by forwarding their order emails to the site's email address (Settings > Delivery apps shows it) or by the JSON endpoint. If an order says "CHECK THESE ITEMS", some dishes could not be matched to the menu: tell her which, and offer to correct the order.
- Backups run daily automatically; Settings has Back up now and Download.
