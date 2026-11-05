"""Her real menu, typed from the printed menu (plans/menu.md, menu-design/prices.json). Loaded once, on the first
start, into an empty database; after that she edits it in the dashboard. No sample orders or customers are created."""

MENU = [
    # slug, name, category, description, image, notice hours, [(option, price)]
    ("dampukh", "Dam Pukh", "mains",
     "Traditional South Pukhtunkhwa dish: meat slow-cooked in a sealed pot with onions, potatoes, tomatoes and chilli, served with nan.",
     "dampukh.jpg", 24, [("Per person", 15), ("Family portion (1kg)", 40)]),
    ("painda", "Painda (Sohbat)", "mains",
     "Slow-cooked chicken in a rich, spiced shorba, served over thin chapati.",
     "painda.jpg", 24, [("Per person (1 chapati, 2 chicken pieces)", 12), ("Family portion (5 chapati, 10 chicken pieces)", 35)]),
    ("pulao", "Bannu Beef Pulao", "mains",
     "Pulao rice with a deep infusion of slow-cooked bone-in beef and the aroma of whole spices.",
     "pulao.jpg", 24, [("Per person", 12), ("Family portion (serves 5)", 35)]),
    ("namkeen", "Namkeen Gosht", "mains",
     "A timeless Pashtun favourite: lamb on the bone, slow-simmered with nothing more than salt and delicate spice. Inspired by the Namak Mandi of Peshawar.",
     "namkeen.jpg", 24, [("Half kg lamb", 22), ("1kg lamb", 40)]),
    ("kebab", "Peshawari Kebab", "mains",
     "Homemade Peshawari kebabs with rich spices, served with mint chutney.",
     "kebab.jpg", 24, [("4 chicken kebabs", 12), ("4 lamb kebabs", 12)]),
    ("karahi", "Peshawari Karahi", "mains",
     "Signature Shinwari karahi cooked with tomatoes, green chilli and black pepper, served with nan.",
     "karahi.jpg", 24, [("Chicken, per portion", 12), ("Chicken, 1kg", 18), ("Lamb, per portion", 15), ("Lamb, 1kg", 32)]),
    ("raan", "Peshawari Raan Pulao", "special",
     "Succulent whole lamb leg slow-cooked with spices and layered on steaming pulao rice. A feast inspired by Peshawar's culinary heritage. Serves 6 to 8 adults.",
     "raan.jpg", 48, [("Whole leg, serves 6 to 8", 110)]),
    ("malai", "Chicken Malai Boti", "also", "Creamy, mildly spiced chicken boti.", None, 24, [("Portion", 12)]),
    ("tikka", "Chicken Tikka", "also", "Chargrilled spiced chicken tikka.", None, 24, [("Portion", 12)]),
    ("nan", "Nan", "extras", "Fresh nan bread.", None, 24, [("Each", 2)]),
    ("chutney", "Fresh Chutney", "extras", "Homemade mint chutney.", None, 24, [("Pot", 1)]),
    ("salad", "Fresh Salad", "extras", "Fresh salad.", None, 24, [("Portion", 1)]),
    ("kheer", "Kheer", "dessert",
     "Creamy rice pudding inspired by the timeless flavours of Peshawar's Qissa Khwani Bazaar.",
     "kheer.jpg", 24, [("Per serving", 7.5)]),
    ("tiffin", "Tiffin Service", "tiffin",
     "Home-cooked meals delivered to your routine. Ask us about plans, days and prices.",
     None, 24, []),
]

# Dishes customers ask about rather than add to the basket (details come later from her, through the assistant).
ENQUIRY_ITEMS = {"tiffin"}

CATEGORIES = [("mains", "Mains"), ("special", "Special order · 48 hours' notice"), ("also", "Also available"),
              ("extras", "Extras"), ("dessert", "Dessert"), ("tiffin", "Tiffin service")]

# Delivery partners she can switch on later. Orders from them can also be posted to /api/partner-orders.
PARTNERS = [("ubereats", "Uber Eats"), ("deliveroo", "Deliveroo"), ("justeat", "Just Eat")]

DEFAULT_SETTINGS = {
    "shop_name": "The Pashtoon Flavours",
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
    "delivery_bands": [[3, 3.99, 0], [6, 5.99, 0], [None, 7.99, 50]],
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
    "announcement": "",
    "ai_enabled": True,
    "assistant_name": "Zara",
    "ai_model": "",                   # empty = CLAUDE_MODEL from .env (Opus by default, the strongest model)
    "promo_item_id": 0,               # dish whose video pops up once per visit (0 = off)
}
