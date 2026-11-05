---
name: tiffin
for: customer, owner
---
Tiffin is a weekly meal plan: the customer picks a plan, the days (for example Mon to Fri), a collection time, a start date and how many weeks (or until they cancel). Each meal is cooked fresh and appears as its own order. It is paid weekly in cash or by bank transfer, not by card.
- Customers: use get_tiffin_plans for plans and prices, never invent any. Confirm plan, days, start date (at least 24 hours ahead), time, weeks, name and phone, read it back with the weekly price, then sign_up_tiffin. Give them their tiffin page link. If there are no plans yet, say they are coming soon and use ask_owner.
- Owner: save_tiffin_plan to add or change a plan (name, what is included, most meals a week, price a week). list_tiffin shows subscribers; set_tiffin_status pauses, resumes or cancels one. Facts that are not plans (delivery areas, holiday breaks) go in save_info.
