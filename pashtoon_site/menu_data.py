"""Her real menu, typed from the printed menu (plans/menu.md, menu-design/prices.json). Loaded once, on the first
start, into an empty database; after that she edits it in the dashboard. No sample orders or customers are created."""

MENU = [
    # slug, name, category, description, image, notice hours, [(option, price)]
    # Source of truth: her flyer of 12 Nov 2026. Kheer was dropped (discontinued).
    ("dampukh", "Dam Pukh", "mains",
     "Traditional South Pukhtunkhwa dish: meat slow-cooked in a sealed container with onions, potatoes, tomatoes and chilli, served with nan bread.",
     "dampukh.jpg", 24, [("Family portion (1kg)", 40)]),
    ("painda", "Painda (Sohbat)", "mains",
     "Traditional South Pukhtunkhwa dish: rice gravy/broth (shorba) made with slow-cooked chicken blended in spices, served over thin chapati (nan bread).",
     "painda.jpg", 24, [("Family portion (4 chapati, 8 chicken pieces)", 40)]),
    ("pulao", "Bannu Beef Pulao", "mains",
     "Traditional South Pukhtunkhwa pulao rice made with a deep infusion of slow-cooked bone-in beef and the aroma of spices.",
     "pulao.jpg", 24, [("Family portion", 35)]),
    ("namkeen", "Namkeen Gosht", "mains",
     "A timeless Pashtun favourite: lamb on the bone, slow-simmered and seasoned with nothing more than salt and delicate spice, letting the natural richness speak for itself. Inspired by the legendary Namak Mandi of Peshawar.",
     "namkeen.jpg", 24, [("Half kg lamb", 24), ("1kg lamb", 40)]),
    ("kebab", "Peshawari Kebab", "mains",
     "Homemade traditional Peshawari kebabs with a choice of chicken or lamb, made with rich spices and served with mint chutney. Four kebabs.",
     "kebab.jpg", 24, [("4 chicken kebabs", 15), ("4 lamb kebabs", 15)]),
    ("karahi", "Peshawari Karahi", "mains",
     "Signature Peshawari Shinwari karahi cooked with tomatoes, green chilli and black pepper, served with nan bread.",
     "karahi.jpg", 24, [("Chicken (1 full)", 26), ("Lamb (1kg)", 40)]),
    ("raan", "Peshawari Raan Pulao", "special",
     "Succulent whole lamb leg slow-cooked with spices and layered on steaming pulao rice. Inspired by Peshawar's rich culinary heritage, this special feast embodies tradition, depth of flavour and unforgettable hospitality. Serves 8 to 10 adults.",
     "raan.jpg", 48, [("Whole leg, serves 8 to 10", 120)]),
    ("malai", "Chicken Malai Boti", "also", "Creamy, mildly spiced chicken boti.", None, 24, [("Portion", 14)]),
    ("tikka", "Chicken Tikka", "also", "Chargrilled spiced chicken tikka.", None, 24, [("Portion", 14)]),
    ("nan", "Tandoori Nan", "extras", "Fresh tandoori nan bread.", None, 24, [("Each", 3)]),
    ("chutney", "Fresh Chutney", "extras", "Homemade mint chutney.", None, 24, [("Pot", 2)]),
    ("salad", "Fresh Salad", "extras", "Fresh salad.", None, 24, [("Portion", 2)]),
    ("tiffin", "Tiffin Service", "tiffin",
     "Weekly tiffin service available. Home-cooked meals delivered to your routine. Ask us about plans, days and prices.",
     None, 24, []),
]

# For sites that already hold the earlier menu: which old portion each new portion continues, so its recipe,
# stock links and order history stay attached. Any old portion not listed here is switched off (kept for history).
MENU_V2_FROM = {
    ("pulao", "Family portion"): "Family portion (serves 5)",
    ("painda", "Family portion (4 chapati, 8 chicken pieces)"): "Family portion (5 chapati, 10 chicken pieces)",
    ("dampukh", "Family portion (1kg)"): "Family portion (1kg)",
    ("karahi", "Chicken (1 full)"): "Chicken, 1kg",
    ("karahi", "Lamb (1kg)"): "Lamb, 1kg",
    ("raan", "Whole leg, serves 8 to 10"): "Whole leg, serves 6 to 8",
}

# Dishes customers ask about rather than add to the basket (details come later from her, through the assistant).
ENQUIRY_ITEMS = {"tiffin"}

CATEGORIES = [("mains", "Mains"), ("special", "Special order · 48 hours' notice"), ("also", "Also available"),
              ("extras", "Extras"), ("dessert", "Dessert"), ("tiffin", "Tiffin service")]

# Delivery partners she can switch on later. Orders from them can also be posted to /api/partner-orders.
PARTNERS = [("ubereats", "Uber Eats"), ("deliveroo", "Deliveroo"), ("justeat", "Just Eat")]

DEFAULT_SETTINGS = {
    "shop_name": "Pakhtoon Flavours",
    "tagline": "Traditional Khyber Pukhtunkhwa cuisine, home-cooked to order",
    "kitchen_postcode": "OL7 9TG",
    "area": "Ashton-under-Lyne, Greater Manchester",
    "whatsapp": "07983828205",
    "instagram": "thepashtoonflavours",
    "tiktok": "thepashtoonflavours",
    "accepting_orders": True,
    "offers_delivery": False,         # collection only for now; switch on in Settings
    # Delivery partners: shown as "Also on ..." links when on; commission is used in the accounts.
    "partners": {k: {"on": False, "url": "", "commission": 30} for k, _ in PARTNERS},
    "partner_key": "",                # secret for /api/partner-orders (made on first start)
    "offers_collection": True,
    # miles up to (None = any distance), fee, minimum food order for that band
    "delivery_bands": [[3, 3.99, 0], [6, 5.99, 0], [None, 7.99, 0]],
    "delivery_minimum": 50,           # smallest food total for delivery; collection has no minimum
    "max_delivery_miles": 0,          # 0 = no limit
    "slot_start": "12:00",
    "slot_end": "21:00",
    "slot_minutes": 30,
    "daily_order_limit": 0,           # 0 = no limit
    "closed_dates": [],
    "bank_details": "",
    "pay_cash": True,                 # she takes cash for now
    "pay_bank": False,                # switch on when she takes bank transfers
    "pay_card": True,                 # only works once STRIPE_SECRET_KEY is set
    "review_link": "",
    # Food Standards Agency hygiene rating shown on the homepage ("" hides it) and the link to her FSA listing.
    "hygiene_rating": "5",
    "hygiene_link": "https://ratings.food.gov.uk",
    "announcement": "",
    "owner_login_email": "",           # the owner signs in with this email and her password
    "owner_must_change_password": False,
    "ai_enabled": True,
    "assistant_name": "Zara",
    "ai_model": "",                   # empty = CLAUDE_MODEL from .env (Opus by default, the strongest model)
    "promo_item_id": 0,
    # Alerts to her phone when the dashboard is closed
    "owner_phone": "",                # her personal WhatsApp number (not the business number)
    "owner_email": "",
    "alert_kinds": {"order": True, "question": True, "cancel": True, "payment": True, "message": True},
    "alert_whatsapp": True,
    "alert_ntfy": False,              # free phone push through the ntfy app
    "ntfy_topic": "",
    "alert_email": "fallback",        # off, fallback (only if the others fail) or always
    "quiet_from": "",                 # e.g. 22:00; empty = never quiet
    "quiet_to": "",
    "quiet_urgent": True,             # cancellations still come through in quiet hours
    "owner_wa_last_in": "",           # when she last messaged the business number (opens WhatsApp's 24 hour window)
    "backup_email": False,            # email her a copy of the weekly backup               # dish whose video pops up once per visit (0 = off)
}


# Photos and short muted clips (from her own videos) for the dishes that have footage. Shipped in static/media.
DISH_MEDIA = {slug: (f"/static/media/{slug}.jpg", f"/static/media/{slug}.mp4") for slug in ("dampukh", "pulao", "namkeen", "karahi", "raan")}
# More from the same footage, offered in the media library so she or the assistant can use them.
LIBRARY_MEDIA = [("image", "/static/media/karahi-lamb.jpg", "Peshawari karahi, lamb"), ("video", "/static/media/karahi-lamb.mp4", "Peshawari karahi, lamb")]
