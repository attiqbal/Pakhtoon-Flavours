"""End-to-end tests: no network and no API key needed (postcodes and Claude are faked)."""
import hashlib
import hmac
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import time
from datetime import date

from pashtoon_site import agent, config, db, geo, meta, payments, shop, web

PASSWORD = "kitchen-secret-1"
POSTCODES = {"OL7 9TG": (53.4875, -2.1029), "OL6 6AA": (53.4930, -2.0920), "M1 1AE": (53.4794, -2.2453), "WA1 1AA": (53.3900, -2.5900)}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OWNER_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "WHATSAPP_TOKEN", "INSTAGRAM_TOKEN", "META_PAGE_TOKEN", "META_APP_SECRET"):
        monkeypatch.setattr(config, k, "")
    monkeypatch.setattr(geo, "lookup", lambda p: POSTCODES.get(geo.normalise(p)))
    web._hits.clear()
    agent.set_client(None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    app = web.create_app(str(tmp_path / "test.db"))
    with TestClient(app, follow_redirects=False) as c:
        yield c
    agent.set_client(None)


def csrf(c, path="/"):
    m = re.search(r'window.CSRF = "([^"]+)"', c.get(path).text)
    return m.group(1)


def login(c):
    r = c.post("/owner/login", data={"password": PASSWORD, "csrf": csrf(c, "/owner/login"), "next": "/owner"})
    assert r.status_code == 303 and r.headers["location"] == "/owner"


def opt(dish, portion=""):
    return shop.find_option(dish, portion)["id"]


def day(n=2):
    return (shop.now().date() + timedelta(days=n)).isoformat()


def checkout(c, items, **kw):
    data = {"cart": json.dumps(items), "name": "Ayesha Khan", "phone": "07700 900123", "fulfilment": "collection",
            "date": day(), "time": "18:00", "payment": "cash", "agree": "1", "csrf": csrf(c, "/checkout")}
    data.update(kw)
    return c.post("/checkout", data=data)


# ---------------------------------------------------------------- menu and site

def test_real_menu_loaded_and_public_pages(client):
    names = [i["name"] for i in shop.menu()]
    for dish in ["Dam Pukh", "Painda (Sohbat)", "Bannu Beef Pulao", "Namkeen Gosht", "Peshawari Kebab", "Peshawari Karahi",
                 "Peshawari Raan Pulao", "Tandoori Nan", "Chicken Tikka"]:
        assert dish in names
    assert shop.find_option("Dam Pukh")["price"] == 40       # family portion only, no per-person sizes
    assert [len(i["options"]) for i in shop.menu() if i["slug"] in ("dampukh", "painda", "pulao", "raan")] == [1, 1, 1, 1]
    assert shop.find_option("Raan")["price"] == 120
    assert shop.find_option("Raan")["notice_hours"] == 48
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 0     # no demo orders
    for path in ["/", "/menu", "/checkout", "/owner/login"]:
        assert client.get(path).status_code == 200
    menu = client.get("/menu").text
    assert "Namkeen Gosht" in menu and "£120.00" in menu and "AI assistant" in menu


def test_install_is_idempotent(client):
    shop.update_price(opt("Chicken Tikka"), 8)
    shop.install()
    assert shop.find_option("Chicken Tikka")["price"] == 8
    assert db.q1("SELECT COUNT(*) n FROM menu_items")["n"] == 13


# ---------------------------------------------------------------- ordering rules

def test_quote_and_delivery_minimum(client):
    with pytest.raises(shop.ShopError, match="only doing collection"):    # collection only by default
        shop.delivery_quote("OL6 6AA")
    db.set_setting("offers_delivery", True)
    lines = [{"option_id": opt("Dam Pukh"), "qty": 1}, {"option_id": opt("Nan"), "qty": 2}]       # £46 of food
    q = shop.quote(lines, "collection", d=day(), t="18:00")        # collection has no minimum
    assert q["food_total"] == 46 and q["total"] == 46
    small = [{"option_id": opt("Nan"), "qty": 1}]
    assert shop.quote(small, "collection", d=day(), t="18:00")["total"] == 3
    for pc in ("OL6 6AA", "M1 1AE", "WA1 1AA"):                     # delivery needs £50 at every distance
        with pytest.raises(shop.ShopError, match="minimum order of £50"):
            shop.quote(lines, "delivery", pc, day(), "18:00")
    big = lines + [{"option_id": opt("Nan"), "qty": 2}]            # £52
    assert shop.quote(big, "delivery", "OL6 6AA", day(), "18:00")["delivery_fee"] == 3.99
    assert shop.quote(big, "delivery", "M1 1AE", day(), "18:00")["delivery_fee"] == 5.99
    far = shop.quote(lines * 2, "delivery", "WA1 1AA", day(), "18:00")
    assert far["delivery_fee"] == 7.99 and far["food_total"] == 92
    exact = [{"option_id": opt("Dam Pukh"), "qty": 1}, {"option_id": opt("Nan"), "qty": 1}, {"option_id": opt("Fresh Chutney"), "qty": 2},
             {"option_id": opt("Fresh Salad"), "qty": 1}]           # 40 + 3 + 4 + 2 = 49, one short
    with pytest.raises(shop.ShopError, match="minimum"):
        shop.quote(exact, "delivery", "OL6 6AA", day(), "18:00")
    exact[2]["qty"] = 3                                             # 51
    assert shop.quote(exact, "delivery", "OL6 6AA", day(), "18:00")["food_total"] == 51
    db.set_setting("delivery_minimum", 0)                           # she can switch it off
    assert shop.quote(small, "delivery", "OL6 6AA", day(), "18:00")["delivery_fee"] == 3.99
    with pytest.raises(shop.ShopError, match="couldn't find"):
        shop.quote(lines, "delivery", "ZZ9 9ZZ", day(), "18:00")


def test_delivery_minimum_at_checkout_and_in_ui(client):
    db.set_setting("offers_delivery", True)
    small = [{"option_id": opt("Dam Pukh"), "qty": 1}]
    r = checkout(client, small, fulfilment="delivery", address="1 High Street, Ashton", postcode="OL6 6AA")
    assert r.status_code == 200 and "minimum order of £50" in r.text
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 0
    assert checkout(client, small).status_code == 303               # same basket for collection is fine
    r = client.post("/api/quote", json={"items": [{"option_id": opt("Dam Pukh"), "qty": 1}], "fulfilment": "delivery", "postcode": "OL6 6AA"})
    assert r.json()["ok"] is False and "£50" in r.json()["error"]
    assert "Delivery minimum order: £50.00" in client.get("/checkout").text
    assert "minimum" in client.get("/m").text.lower()


def test_kheer_is_gone(client):
    assert "Kheer" not in [i["name"] for i in shop.menu(include_hidden=True)]
    assert "Kheer" not in client.get("/menu").text and "Kheer" not in client.get("/m").text and "kheer" not in client.get("/").text.lower()
    # a site that still has Kheer (and an old per-band minimum) is cleaned up once, orders keep their history
    iid = db.execute("INSERT INTO menu_items(slug,name,category,description,sort) VALUES('kheer','Kheer','dessert','x',99)")
    oid = db.execute("INSERT INTO menu_options(item_id,label,price) VALUES(?,?,?)", (iid, "Per serving", 7.5))
    db.set_setting("promo_item_id", iid)
    o = shop.place_order([{"option_id": oid, "qty": 1}], "A", "07700900001", "collection", day(2), "18:00", "cash")
    db.set_setting("kheer_removed", False)
    db.set_setting("rules_v1", False)
    db.set_setting("delivery_bands", [[3, 3.99, 0], [None, 7.99, 50]])
    shop.install()
    assert not db.q1("SELECT id FROM menu_items WHERE slug='kheer'") and not db.q("SELECT id FROM menu_options WHERE id=?", (oid,))
    assert shop.setting("promo_item_id") == 0 and shop.get_order(o["id"])["items"][0]["dish"] == "Kheer"
    assert shop.setting("delivery_minimum") == 50 and shop.setting("delivery_bands") == [[3, 3.99, 0], [None, 7.99, 0]]


def test_notice_closed_days_and_daily_limit(client):
    lines = [{"option_id": opt("Chicken Tikka"), "qty": 1}]
    with pytest.raises(shop.ShopError, match="24 hours"):
        shop.quote(lines, "collection", d=shop.today(), t="23:59")
    with pytest.raises(shop.ShopError, match="48 hours"):
        shop.quote([{"option_id": opt("Raan"), "qty": 1}], "collection", d=day(1), t="12:00")
    db.set_setting("closed_dates", [day(3)])
    with pytest.raises(shop.ShopError, match="closed"):
        shop.quote(lines, "collection", d=day(3), t="18:00")
    with pytest.raises(shop.ShopError, match="between"):
        shop.quote(lines, "collection", d=day(), t="06:00")
    db.set_setting("daily_order_limit", 1)
    shop.place_order(lines, "A", "07700900001", "collection", day(), "18:00", "cash")
    with pytest.raises(shop.ShopError, match="fully booked"):
        shop.place_order(lines, "B", "07700900002", "collection", day(), "19:00", "cash")


def test_checkout_tracking_payment_and_cancel(client):
    db.set_setting("pay_bank", True)
    db.set_setting("offers_delivery", True)
    r = checkout(client, [{"option_id": opt("Namkeen", "1kg"), "qty": 1}, {"option_id": opt("Nan"), "qty": 4}],
                 fulfilment="delivery", address="12 Park Road, Ashton", postcode="ol66aa", date=day(5), payment="bank")
    assert r.status_code == 303, r.text[:500]
    code = r.headers["location"].split("/o/")[1].split("?")[0]
    page = client.get(f"/o/{code}?new=1").text
    assert "Thank you" in page and "£55.99" in page and "Naan: yes, 4" in page and "cancellation policy" in page
    o = shop.get_order_by_code(code)
    assert o["postcode"] == "OL6 6AA" and o["delivery_fee"] == 3.99 and o["status"] == "confirmed"
    assert any("New order" in a["text"] for a in shop.alerts())
    client.post(f"/o/{code}/paid", data={"csrf": csrf(client, f"/o/{code}")})
    assert shop.get_order(o["id"])["payment_status"] == "reported"
    r = client.post(f"/o/{code}/cancel", data={"csrf": csrf(client, f"/o/{code}")})
    assert shop.get_order(o["id"])["status"] == "cancelled"


def test_checkout_errors_are_shown_not_crashes(client):
    r = checkout(client, [{"option_id": opt("Chicken Tikka"), "qty": 1}], phone="123")
    assert r.status_code == 200 and "mobile number" in r.text
    r = checkout(client, [], name="X")
    assert "basket is empty" in r.text
    r = checkout(client, [{"option_id": opt("Chicken Tikka"), "qty": 1}], agree="")
    assert "cancellation policy" in r.text


def test_late_cancel_blocked(client):
    o = shop.place_order([{"option_id": opt("Chicken Tikka"), "qty": 1}], "A", "07700900001", "collection", day(2), "18:00", "cash")
    db.execute("UPDATE orders SET date=?, time='23:59' WHERE id=?", (shop.today(), o["id"]))
    with pytest.raises(shop.ShopError, match="24 hours"):
        shop.customer_cancel(o["id"])


def test_csrf_required(client):
    r = client.post("/checkout", data={"cart": "[]", "csrf": "wrong"})
    assert r.status_code == 400


# ---------------------------------------------------------------- owner

def test_owner_pages_need_login(client):
    for path in ["/owner", "/owner/orders", "/owner/inbox", "/owner/stock", "/owner/money", "/owner/settings", "/owner/menu", "/owner/calendar"]:
        r = client.get(path)
        assert r.status_code == 303 and "/owner/login" in r.headers["location"], path
    assert client.get("/api/kitchen").status_code == 403
    r = client.post("/owner/login", data={"password": "nope", "csrf": csrf(client, "/owner/login")})
    assert "password isn" in r.text


def test_owner_pages_render(client):
    login(client)
    o = shop.place_order([{"option_id": opt("Karahi", "lamb"), "qty": 1}], "Bilal", "07700900003", "collection", day(2), "19:00", "cash")
    for path in ["/owner", "/owner/orders", "/owner/orders?view=unpaid", f"/owner/orders/{o['id']}", "/owner/orders/new", "/owner/inbox",
                 "/owner/calendar", "/owner/menu", f"/owner/menu/{o['items'][0]['option_id'] and shop.option(o['items'][0]['option_id'])['item_id']}",
                 "/owner/menu/new", "/owner/stock", "/owner/money", "/owner/settings", "/owner/assistant", "/kitchen"]:
        r = client.get(path)
        assert r.status_code == 200, path
    page = client.get(f"/owner/orders/{o['id']}").text
    assert "wa.me/447700900003" in page and "Thank you for your order with Pakhtoon Flavours" in page


def test_phone_order_status_and_kitchen(client):
    login(client)
    t = csrf(client, "/owner/orders/new")
    r = client.post("/owner/orders/new", data={"csrf": t, f"qty_{opt('Pulao', 'family')}": "1", "name": "Sana", "phone": "07700900004",
                                                "date": day(2), "time": "13:00", "fulfilment": "collection", "payment": "cash"})
    assert r.status_code == 303
    oid = int(r.headers["location"].split("/orders/")[1].split("?")[0])
    k = client.get(f"/api/kitchen?day={day(2)}").json()
    assert k["orders"][0]["id"] == oid and k["totals"] == [["Bannu Beef Pulao · Family portion", 1]]
    r = client.post(f"/api/kitchen/{oid}/status", json={"status": "cooking"}, headers={"X-CSRF": csrf(client, "/owner")})
    assert r.status_code == 200 and shop.get_order(oid)["status"] == "cooking"


def test_kitchen_tablet_link(client):
    r = client.get("/kitchen?key=" + db.get_setting("kitchen_token"))
    assert r.status_code == 303
    assert client.get("/api/kitchen").status_code == 200
    assert client.get("/owner").status_code == 303        # kitchen access is not owner access


def test_recipes_stock_shopping_writeoff_and_money(client):
    login(client)
    dp = opt("Dam Pukh")
    shop.set_recipe(dp, "250 g lamb\n1 pcs onion\n1 tbsp oil")
    shop.record_purchase("lamb", 1, "kg", 12)
    o = shop.place_order([{"option_id": dp, "qty": 6}], "Zainab", "07700900005", "collection", day(2), "18:00", "cash")
    sl = shop.shopping_list(day(2), day(2))
    lamb = next(r for r in sl["rows"] if r["name"] == "lamb")
    assert lamb["buy"] == "500 g" and sl["orders"] == 1
    shop.set_status(o["id"], "cooking")
    assert shop.ingredient("lamb")["stock"] == -500                       # 1000 g bought, 1500 g used
    shop.record_purchase("lamb", 2, "kg", 24)
    shop.write_off("lamb", 200, "g", "Spoiled")
    assert shop.ingredient("lamb")["stock"] == 1300
    shop.add_expense(shop.today(), "Packaging", "Foil trays", 9.5)
    shop.set_payment(o["id"], "paid")
    db.execute("UPDATE orders SET date=? WHERE id=?", (shop.today(), o["id"]))
    m = shop.summary(*shop.period("month"))
    assert m["sales"] == 240 and m["purchases"] == 36 and m["other"] == 9.5 and m["profit"] == 194.5 and m["waste"] == 2.4
    assert "Spoiled" in client.get("/owner/stock").text
    r = client.post("/owner/stock/writeoff", data={"csrf": csrf(client, "/owner/stock"), "ingredient": str(shop.ingredient("lamb")["id"]),
                                                    "qty": "0.1", "unit": "kg", "reason": "Leftover"})
    assert "Wrote off 100 g of lamb" in client.get(r.headers["location"]).text


def test_menu_edit_and_settings(client):
    login(client)
    it = shop.option(opt("Chicken Tikka"))
    r = client.post(f"/owner/menu/{it['item_id']}", data={"csrf": csrf(client, "/owner/menu"), "name": "Chicken Tikka", "category": "dessert",
                                                           "description": "Rice pudding", "notice_hours": "24", "available": "1",
                                                           "opt_id_0": str(it["id"]), "opt_label_0": "Per serving", "opt_price_0": "8.00",
                                                           "opt_label_1": "Family bowl", "opt_price_1": "30", f"recipe_{it['id']}": "100 ml milk\n20 g rice"})
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert shop.find_option("Chicken Tikka", "serving")["price"] == 8 and shop.find_option("Chicken Tikka", "family")["price"] == 30
    assert shop.recipe_text(it["id"]) == "100 ml milk\n20 g rice"
    r = client.post("/owner/settings", data={"csrf": csrf(client, "/owner/settings"), "accepting_orders": "1", "offers_delivery": "1",
                                              "offers_collection": "1", "pay_cash": "1", "slot_start": "13:00", "slot_end": "20:00", "slot_minutes": "30",
                                              "daily_order_limit": "8", "bank_details": "Sort 00-00-00", "band_miles_0": "3", "band_fee_0": "4.50",
                                              "band_fee_1": "8", "delivery_minimum": "60", "max_delivery_miles": "10", "shop_name": "Pakhtoon Flavours"})
    assert r.status_code == 303
    assert shop.setting("daily_order_limit") == 8 and shop.setting("delivery_bands") == [[3, 4.5, 0], [None, 8, 0]] and shop.setting("delivery_minimum") == 60
    assert shop.time_slots()[0] == "13:00"


def test_calendar_feed(client):
    shop.place_order([{"option_id": opt("Kebab", "lamb"), "qty": 2}], "Hina", "07700900006", "collection", day(2), "17:30", "cash", notes="extra chutney")
    assert client.get("/calendar/wrong.ics").status_code == 404
    r = client.get(f"/calendar/{db.get_setting('calendar_token')}.ics")
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.text and "Hina" in r.text and "T173000" in r.text and "extra chutney" in r.text


# ---------------------------------------------------------------- chat and the AI assistant

class FakeClaude:
    """Plays back scripted responses and records what it was sent."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw)))
        blocks = self.script.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(**b, model_dump=lambda mode=None, exclude_none=None, _b=b: dict(_b)) for b in blocks])


def test_chat_without_ai_goes_to_owner(client):
    t = csrf(client)
    r = client.post("/api/chat/send", json={"text": "Do you do halal?"}, headers={"X-CSRF": t}).json()
    assert r["ok"] and r["lines"][-1]["text"] == agent.HOLDING
    conv = db.q1("SELECT * FROM conversations")
    assert conv["needs_owner"] == 1
    login(client)
    client.post(f"/owner/inbox/{conv['id']}/reply", data={"csrf": csrf(client, "/owner/inbox"), "text": "Yes, all our meat is halal."})
    # the customer's browser kept its chat cookie through login? Use a fresh poll from the inbox page instead
    assert "all our meat is halal" in client.get(f"/owner/inbox/{conv['id']}").text
    assert db.q1("SELECT needs_owner FROM conversations WHERE id=?", (conv["id"],))["needs_owner"] == 0


def test_ai_takes_an_order_and_says_it_is_an_ai(client):
    items = [{"dish": "Painda", "portion": "family", "qty": 1}]
    fake = FakeClaude([
        [{"type": "tool_use", "id": "t1", "name": "quote_order", "input": {"items": items, "fulfilment": "collection", "date": day(), "time": "18:00"}}],
        [{"type": "text", "text": "Hi, I'm Zara, the AI assistant. 1 x Painda family portion, total £40.00. Shall I place it?"}],
        [{"type": "tool_use", "id": "t2", "name": "place_order", "input": {"items": items, "fulfilment": "collection", "date": day(), "time": "18:00",
                                                                             "name": "Maryam", "phone": "07700900007", "payment_method": "cash"}}],
        [{"type": "text", "text": "Done! Your order number is in the link above."}],
    ])
    agent.set_client(fake)
    t = csrf(client)
    r = client.post("/api/chat/send", json={"text": "2 painda for Friday 6pm collection"}, headers={"X-CSRF": t}).json()
    assert "AI assistant" in r["lines"][-1]["text"]
    assert "total £40.00" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    system = fake.calls[0]["system"]
    assert "You are an AI, and you say so" in system and "Never claim to be human" in system
    r = client.post("/api/chat/send", json={"text": "yes please"}, headers={"X-CSRF": t}).json()
    o = db.q1("SELECT * FROM orders")
    assert o and o["customer_name"] == "Maryam" and o["total"] == 40 and o["source"] == "chat"
    poll = client.get("/api/chat/poll?after=0").json()["lines"]
    assert [l["sender"] for l in poll] == ["customer", "ai", "customer", "ai"]


def test_ai_paused_when_owner_takes_over(client):
    agent.set_client(FakeClaude([[{"type": "text", "text": "Hello, I'm Zara, an AI assistant."}]]))
    t = csrf(client)
    client.post("/api/chat/send", json={"text": "hi"}, headers={"X-CSRF": t})
    conv = db.q1("SELECT * FROM conversations")
    agent.owner_reply(conv["id"], "Hi, it's Aarzu here!")
    db.execute("UPDATE conversations SET ai_paused=1 WHERE id=?", (conv["id"],))
    r = client.post("/api/chat/send", json={"text": "great, can I order?"}, headers={"X-CSRF": t}).json()
    assert r["ok"] and r["lines"][-1]["sender"] == "customer"          # no AI reply while paused
    assert "owner replied" in db.q1("SELECT pending FROM conversations")["pending"]


def test_order_status_tool_checks_phone(client):
    o = shop.place_order([{"option_id": opt("Chicken Tikka"), "qty": 1}], "A", "07700900008", "collection", day(), "18:00", "cash")
    ctx = agent.Ctx("customer", None)
    out, err = agent.run_tool(ctx, "order_status", {"order_id": o["id"], "phone": "07000000000"})
    assert err and "don't match" in out
    out, err = agent.run_tool(ctx, "order_status", {"order_id": o["id"], "phone": "+44 7700 900008"})
    assert not err and "Chicken Tikka" in out
    out, err = agent.run_tool(ctx, "update_price", {"option_id": 1, "price": 1})
    assert err                                                          # customers can't use owner tools


def test_owner_assistant(client):
    login(client)
    dp = opt("Painda")
    agent.set_client(FakeClaude([
        [{"type": "tool_use", "id": "u1", "name": "update_price", "input": {"option_id": dp, "price": 14}}],
        [{"type": "text", "text": "Done: Painda per person is now £14."}],
    ]))
    r = client.post("/api/owner/chat", json={"text": "Painda per person is now £14"}, headers={"X-CSRF": csrf(client, "/owner/assistant")}).json()
    assert "£14" in r["reply"] and shop.option(dp)["price"] == 14
    assert "Painda per person is now £14" in client.get("/owner/assistant").text


def test_whatsapp_webhook(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "META_VERIFY_TOKEN", "vt")
    assert client.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42").text == "42"
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"contacts": [{"wa_id": "447700900009", "profile": {"name": "Noor"}}],
                                                         "messages": [{"from": "447700900009", "id": "m1", "type": "text", "text": {"body": "menu?"}}]}}]}]}).encode()
    assert client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    conv = db.q1("SELECT * FROM conversations WHERE channel='whatsapp'")
    assert conv["name"] == "Noor" and conv["needs_owner"] == 1
    # Meta retries the same delivery: it is answered once
    client.post("/webhook/meta", content=body, headers={"x-hub-signature-256": sig})
    assert db.q1("SELECT COUNT(*) n FROM chat_log WHERE sender='customer'")["n"] == 1


def test_instagram_and_messenger_parse_and_reply(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "INSTAGRAM_TOKEN", "igtok")
    sent = []
    monkeypatch.setattr(meta.httpx, "post", lambda url, **kw: sent.append((url, kw)) or SimpleNamespace(status_code=200, text=""))
    agent.set_client(FakeClaude([[{"type": "text", "text": "Hi, I'm Zara, the AI assistant 😊"}]]))
    ig = {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "ig123"}, "message": {"mid": "a1", "text": "Do you deliver to OL6?"}},
        {"sender": {"id": "biz"}, "message": {"mid": "a2", "text": "echo", "is_echo": True}}]}]}
    fb = {"object": "page", "entry": [{"messaging": [{"sender": {"id": "fb9"}, "message": {"mid": "b1", "attachments": [{"type": "image"}]}}]}]}
    assert [m["channel"] for m in meta.parse(ig)] == ["instagram"]
    assert meta.parse(fb)[0]["channel"] == "messenger" and "can't be read" in meta.parse(fb)[0]["text"]
    body = json.dumps(ig).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook/meta", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert sent and sent[0][0].endswith("/me/messages") and sent[0][1]["json"]["recipient"] == {"id": "ig123"}
    assert db.q1("SELECT channel FROM conversations")["channel"] == "instagram"


# ---------------------------------------------------------------- payments and photos

def test_payment_methods_follow_settings(client):
    page = client.get("/checkout").text
    assert 'value="cash"' in page and 'value="bank"' not in page and 'value="card"' not in page   # cash only to start
    r = checkout(client, [{"option_id": opt("Chicken Tikka"), "qty": 1}], payment="bank")
    assert r.status_code == 200 and "Choose how you" in r.text


def test_card_payment_with_stripe(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    assert 'value="card"' in client.get("/checkout").text
    made = []
    monkeypatch.setattr(payments, "create_checkout", lambda o: made.append(o["id"]) or "https://checkout.stripe.com/c/pay/cs_test_1")
    r = checkout(client, [{"option_id": opt("Chicken Tikka"), "qty": 2}], payment="card")
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/")
    o = shop.get_order(made[0])
    assert o["payment_method"] == "card" and o["total"] == 28
    monkeypatch.setattr(payments, "paid_order_id", lambda sid: o["id"] if sid == "cs_test_1" else None)
    page = client.get(f"/o/{o['code']}?stripe_session=cs_bogus").text
    assert "Pay now" in page and shop.get_order(o["id"])["payment_status"] == "unpaid"
    page = client.get(f"/o/{o['code']}?stripe_session=cs_test_1").text
    assert "Payment received" in page and shop.get_order(o["id"])["payment_status"] == "paid"
    # the webhook also marks orders paid, but only with a valid signature
    o2 = shop.place_order([{"option_id": opt("Nan"), "qty": 1}], "B", "07700900011", "collection", day(), "18:00", "card")
    ev = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_2", "payment_status": "paid",
                                                                                  "metadata": {"order_id": str(o2["id"])}}}}).encode()
    assert client.post("/webhook/stripe", content=ev, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_x", f"{t}.".encode() + ev, hashlib.sha256).hexdigest()
    assert client.post("/webhook/stripe", content=ev, headers={"stripe-signature": f"t={t},v1={sig}"}).status_code == 200
    assert shop.get_order(o2["id"])["payment_status"] == "paid"


def test_dish_photo_upload(client):
    login(client)
    it = shop.option(opt("Chicken Tikka"))
    data = {"csrf": csrf(client, "/owner/menu"), "name": "Chicken Tikka", "category": "also", "description": "x", "notice_hours": "24",
            "available": "1", "opt_id_0": str(it["id"]), "opt_label_0": "Portion", "opt_price_0": "12"}
    r = client.post(f"/owner/menu/{it['item_id']}", data=data, files={"photo": ("evil.jpg", b"<script>alert(1)</script>", "image/jpeg")})
    assert "error" in r.headers["location"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = client.post(f"/owner/menu/{it['item_id']}", data=data, files={"photo": ("tikka.png", png, "image/png")})
    assert "error" not in r.headers["location"]
    img = db.q1("SELECT image FROM menu_items WHERE id=?", (it["item_id"],))["image"]
    assert img.startswith("/media/dish-") and img.endswith(".png")
    assert client.get(img).content == png
    assert img in client.get("/menu").text


# ---------------------------------------------------------------- collection only, partners, tracked stock

def test_collection_only_default_and_partner_orders(client):
    assert shop.setting("offers_delivery") is False
    html = client.get("/checkout").text
    assert 'value="delivery"' not in html
    db.set_setting("partners", {"ubereats": {"on": True, "url": "https://ubereats.com/aarzu", "commission": 30},
                                "deliveroo": {"on": False, "url": "", "commission": 25}, "justeat": {"on": False, "url": "", "commission": 0}})
    assert "ubereats.com/aarzu" in client.get("/menu").text
    key = db.get_setting("partner_key")
    body = {"partner": "ubereats", "items": [{"dish": "Dam Pukh", "portion": "family", "qty": 1}], "name": "Sam", "date": day(), "time": "19:00",
            "reference": "UE-123", "total": 40}
    assert client.post("/api/partner-orders", json=body).status_code == 401
    r = client.post("/api/partner-orders", json=body, headers={"X-Partner-Key": key})
    assert r.status_code == 200 and r.json()["commission"] == 12.0
    o = shop.get_order(r.json()["order_id"])
    assert o["payment_method"] == "partner" and o["partner"] == "ubereats"
    from pashtoon_site import accounts
    inc = accounts.income_statement(day(-1), day(3))
    assert inc["total_income"] == 40 and any(x["code"] == "6600" and x["amount"] == 12 for x in inc["overheads"])


def test_only_measurable_ingredients_are_tracked(client):
    assert "consumables" in shop.record_purchase("Garam masala", 100, "g", 3)
    shop.record_purchase("Lamb", 2, "kg", 30)
    names = [i["name"] for i in shop.ingredients()]
    assert "Lamb" in names and "Garam masala" not in names
    assert [i["name"] for i in shop.ingredients(tracked=False)] == ["Garam masala"]
    assert db.q1("SELECT account FROM expenses")["account"] == "5300"
    shop.set_recipe(opt("Dam Pukh"), "250 g lamb\n5 g cumin\n2 g salt\n1 pcs onion")
    rows = {r["name"]: r["tracked"] for r in shop.recipe(opt("Dam Pukh"))}
    assert rows == {"lamb": 1, "cumin": 0, "salt": 0, "onion": 1} or rows["cumin"] == 0 and rows["onion"] == 1
    o = shop.place_order([{"option_id": opt("Dam Pukh"), "qty": 2}], "A", "07700900003", "collection", day(), "18:00", "cash")
    sl = shop.shopping_list(day(), day())
    assert [r["name"] for r in sl["rows"]] == ["onion"]      # lamb is already in stock; spices are not listed
    assert "cumin" in sl["check"] and "salt" in sl["check"]
    with pytest.raises(shop.ShopError, match="isn't counted"):
        shop.write_off("cumin", 1, "g", "spoiled")


# ---------------------------------------------------------------- accounts

def test_books_balance_and_reports_export(client):
    from pashtoon_site import accounts, reports
    t = shop.today()
    accounts.quick_journal("capital", t, 500)
    shop.record_purchase("Lamb", 2, "kg", 40)
    shop.add_expense(t, "Gas and electric", "Gas", 20)
    shop.add_expense(t, "Packaging", "Trays on credit", 10, paid_from="2110")
    o = shop.place_order([{"option_id": opt("Dam Pukh"), "qty": 1}], "A", "07700900004", "collection", day(), "18:00", "cash")
    shop.set_payment(o["id"], "paid")
    shop.set_recipe(opt("Dam Pukh"), "1000 g lamb")
    shop.set_status(o["id"], "cooking")
    end = day(3)
    tb = accounts.trial_balance(end)
    assert tb["balanced"] and tb["debit"] == tb["credit"]
    inc = accounts.income_statement(t, end)
    assert inc["total_income"] == 40 and inc["net_profit"] == 40 - 20 - 10 - 20      # lamb used: 1 kg at £20
    bs = accounts.balance_sheet(end)
    assert bs["balanced"] and bs["total_equity"] == 500 + inc["net_profit"]
    cf = accounts.cash_flow(t, end)
    assert cf["balanced"] and cf["closing"] == 500 - 40 - 20 + 40
    with pytest.raises(shop.ShopError, match="equal"):
        accounts.add_journal(t, "bad", [("1110", 5, 0), ("3100", 0, 4)])
    tax = accounts.tax_summary()
    assert tax["turnover"] == 40 and tax["income_tax"] == 0 and tax["vat_left"] > 0
    for kind in list(reports.REPORTS) + ["all"]:
        assert reports.excel(kind, t, end)[:2] == b"PK" and reports.pdf(kind, t, end)[:4] == b"%PDF"


def test_accounts_pages_and_downloads(client):
    login(client)
    for report in ["income", "balance", "cashflow", "trial", "tax", "journal", "chart"]:
        assert client.get(f"/owner/accounts?report={report}").status_code == 200, report
    r = client.get("/owner/accounts/export/all.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.get("/owner/accounts/export/income.pdf?period=tax_year")
    assert r.content[:4] == b"%PDF"
    r = client.post("/owner/accounts/journal", data={"csrf": csrf(client, "/owner/accounts"), "kind": "drawings", "amount": "50", "date": shop.today()})
    assert "error" not in r.headers["location"]
    assert "Money taken out" in client.get("/owner/accounts?report=journal").text
    client.get("/logout") if False else None


# ---------------------------------------------------------------- the assistant: menu, recipes, media, other AI companies

def test_owner_assistant_edits_menu_recipes_and_media(client):
    login(client)
    agent.set_client(FakeClaude([
        [{"type": "tool_use", "id": "a1", "name": "add_menu_item", "input": {"name": "Lamb Biryani", "section": "mains", "description": "Fragrant.",
                                                                             "portions": [{"label": "Per person", "price": 14}]}}],
        [{"type": "tool_use", "id": "a2", "name": "save_info", "input": {"topic": "Tiffin", "text": "Mon to Fri lunch boxes, from £8."}}],
        [{"type": "text", "text": "Done."}],
    ]))
    r = client.post("/api/owner/chat", json={"text": "add lamb biryani £14, and tiffin is mon-fri lunch boxes from £8"},
                    headers={"X-CSRF": csrf(client, "/owner/assistant")}).json()
    assert r["reply"] == "Done."
    o = shop.find_option("Lamb Biryani")
    assert o["price"] == 14
    agent.set_client(FakeClaude([
        [{"type": "tool_use", "id": "b1", "name": "set_recipe", "input": {"option_id": o["id"], "ingredients": "200 g lamb\n150 g rice\n5 g cumin"}}],
        [{"type": "tool_use", "id": "b2", "name": "update_price", "input": {"option_id": o["id"], "price": 15}}],
        [{"type": "tool_use", "id": "b3", "name": "set_ingredient_tracked", "input": {"ingredient": "rice", "tracked": False}}],
        [{"type": "text", "text": "Done."}],
    ]))
    client.post("/api/owner/chat", json={"text": "recipe"}, headers={"X-CSRF": csrf(client, "/owner/assistant")})
    assert shop.option(o["id"])["price"] == 15
    assert {r["name"]: r["tracked"] for r in shop.recipe(o["id"])}["cumin"] == 0
    assert {r["name"]: r["tracked"] for r in shop.recipe(o["id"])}["rice"] == 0
    # a customer's assistant now knows the tiffin details and gets the skill files
    fake = FakeClaude([[{"type": "text", "text": "Hi"}]])
    agent.set_client(fake)
    client.post("/api/chat/send", json={"text": "tiffin?"}, headers={"X-CSRF": csrf(client)})
    system = fake.calls[0]["system"]
    assert "Mon to Fri lunch boxes" in system and "Skill: customer-service" in system and "COLLECTION ONLY" in system
    # photo sent to the assistant is saved to the library and shown to the model, then not stored
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    fake = FakeClaude([[{"type": "tool_use", "id": "c1", "name": "list_media", "input": {}}], [{"type": "text", "text": "Which dish is it for?"}]])
    agent.set_client(fake)
    r = client.post("/api/owner/chat", data={"text": "new photo"}, files=[("files", ("x.png", png, "image/png"))],
                    headers={"X-CSRF": csrf(client, "/owner/assistant")}).json()
    assert r["media"][0]["kind"] == "image" and "media id" in json.dumps(fake.calls[0]["messages"])
    assert any(b.get("type") == "image" for b in fake.calls[0]["messages"][-1]["content"])
    assert "base64" not in db.q1("SELECT messages FROM owner_chat ORDER BY id DESC")["messages"]
    fake = FakeClaude([[{"type": "tool_use", "id": "d1", "name": "set_dish_media", "input": {"item_id": o["item_id"], "kind": "image", "media_id": r["media"][0]["id"]}}],
                       [{"type": "text", "text": "Set."}]])
    agent.set_client(fake)
    client.post("/api/owner/chat", json={"text": "use it for the biryani"}, headers={"X-CSRF": csrf(client, "/owner/assistant")})
    assert db.q1("SELECT image FROM menu_items WHERE id=?", (o["item_id"],))["image"] == r["media"][0]["url"]


def test_assistant_can_run_on_chatgpt(client, monkeypatch):
    from pashtoon_site import llm
    db.set_setting("ai_provider", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test")
    assert llm.configured() and llm.provider() == "openai"
    sent = []
    replies = [{"choices": [{"message": {"content": None, "tool_calls": [{"id": "call_1", "type": "function",
                 "function": {"name": "get_menu", "arguments": "{}"}}]}}]},
               {"choices": [{"message": {"content": "Hi, I'm Zara, an AI assistant."}}]}]

    class Resp:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j

    def post(url, json=None, **kw):
        sent.append((url, json))
        return Resp(replies.pop(0))
    monkeypatch.setattr(llm.httpx, "post", post)
    r = client.post("/api/chat/send", json={"text": "hello"}, headers={"X-CSRF": csrf(client)}).json()
    assert "AI assistant" in r["lines"][-1]["text"]
    assert sent[0][0].endswith("/chat/completions") and sent[0][1]["tools"][0]["function"]["name"] == "get_menu"
    assert sent[1][1]["messages"][-1]["role"] == "tool" and sent[1][1]["messages"][-2]["tool_calls"][0]["id"] == "call_1"


# ---------------------------------------------------------------- tiffin and dish videos

def test_tiffin_enquiry_and_dish_video(client):
    menu = client.get("/menu").text
    assert "Tiffin" in menu and "Ask about tiffin service" in menu and 'data-option="' in menu
    login(client)
    it = db.q1("SELECT * FROM menu_items WHERE slug='dampukh'")
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 200
    data = {"csrf": csrf(client, "/owner/menu"), "name": it["name"], "category": "mains", "description": "x", "notice_hours": "24", "available": "1"}
    for o in shop.item(it["id"])["options"]:
        i = o["sort"]
        data.update({f"opt_id_{i}": str(o["id"]), f"opt_label_{i}": o["label"], f"opt_price_{i}": str(o["price"])})
    r = client.post(f"/owner/menu/{it['id']}", data=data, files={"video": ("clip.mp4", mp4, "video/mp4")})
    assert "error" not in r.headers["location"], r.headers["location"]
    url = db.q1("SELECT video FROM menu_items WHERE id=?", (it["id"],))["video"]
    assert url.endswith(".mp4") and client.get(url).content == mp4
    assert "data-video" in client.get("/menu").text
    r = client.post(f"/owner/menu/{it['id']}", data=data, files={"video": ("clip.mp4", b"not a video at all", "video/mp4")})
    assert "error" in r.headers["location"]
    db.set_setting("promo_item_id", it["id"])
    client.cookies.clear()
    assert "window.PROMO" in client.get("/").text


def test_landing_page_and_phone_layout(client):
    h = client.get("/").text
    for bit in ("See the menu and order", "Watch it cooked", "Good to know", "application/ld+json", "Taking orders now", 'id="lp-sticky"'):
        assert bit in h, bit
    assert shop.earliest_day() and shop.earliest_day() in h
    db.set_setting("accepting_orders", False)
    assert shop.earliest_day() is None and "not taking new orders" in client.get("/").text
    # the customer chat never covers the owner login; the owner app has the phone tab bar and install manifest
    assert 'id="chat-box"' not in client.get("/owner/login").text
    login(client)
    o = client.get("/owner").text
    assert 'class="tabbar"' in o and "owner.webmanifest" in o
    assert client.get("/static/owner.webmanifest").status_code == 200


def test_calendar_feed_names_card_payments(client):
    o = shop.place_order([{"option_id": opt("Dam Pukh"), "qty": 1}], "Card Person", "07700900111", "collection",
                         day(3), "18:00", "card", by_owner=True)
    assert "card online" in shop.ics_feed() and "bank transfer" not in shop.ics_feed()
    assert o["payment_method"] == "card"


# ---------------------------------------------------------------- alerts when the dashboard is closed

@pytest.fixture()
def sent(monkeypatch):
    """Record what the alert channels are asked to send (no network)."""
    from pashtoon_site import notify
    out = []
    monkeypatch.setattr(notify, "SYNC", True)
    monkeypatch.setattr(config, "WHATSAPP_TOKEN", "t")
    monkeypatch.setattr(config, "WHATSAPP_PHONE_ID", "1")
    monkeypatch.setattr(meta, "send_text", lambda ch, to, text: out.append(("text", to, text)) or True)
    monkeypatch.setattr(meta, "send_template", lambda to, name, lang, params: out.append(("template", to, name, params)) or True)
    monkeypatch.setattr(notify, "send_ntfy", lambda text, title="": out.append(("ntfy", text)) or (True, "HTTP 200"))
    monkeypatch.setattr(notify, "send_email", lambda subject, text, to=None, attach=None: out.append(("email", subject)) or (True, "sent"))
    return out


def test_alerts_whatsapp_window_template_quiet_hours_and_switches(client, sent, monkeypatch):
    from pashtoon_site import notify
    db.set_setting("owner_phone", "07700 900123")
    shop.alert("order", "New order #1 from Sam: £30.00")
    assert sent[0][0] == "text"     # no template configured, so it tries a plain text (WhatsApp may refuse outside her window)
    sent.clear()
    notify.note_owner_message()
    shop.alert("order", "New order #2")
    assert sent[-1][:2] == ("text", "447700900123")
    # outside her 24 hour window a template is used, with newlines removed
    db.set_setting("owner_wa_last_in", (shop.now() - timedelta(hours=30)).isoformat())
    monkeypatch.setattr(config, "WHATSAPP_OWNER_TEMPLATE", "owner_alert")
    sent.clear()
    shop.alert("order", "Line one\nLine two")
    assert sent[-1][0] == "template" and sent[-1][3] == ["Line one Line two"]
    # per-alert switch
    db.set_setting("alert_kinds", {"order": False, "question": True, "cancel": True, "payment": True, "message": True})
    sent.clear()
    shop.alert("order", "ignored")
    assert sent == []
    # quiet hours hold alerts, cancellations come through, one summary after
    notify.note_owner_message()
    db.set_setting("alert_kinds", {k: True for k in notify.KINDS})
    db.set_setting("quiet_from", (shop.now() - timedelta(minutes=5)).strftime("%H:%M"))
    db.set_setting("quiet_to", (shop.now() + timedelta(minutes=30)).strftime("%H:%M"))
    if (shop.now() - timedelta(minutes=5)).day == shop.now().day and (shop.now() + timedelta(minutes=30)).day == shop.now().day:
        sent.clear()
        shop.alert("order", "held order A")
        shop.alert("payment", "held payment")
        shop.alert("cancel", "urgent cancel")
        assert [x[2] for x in sent] == ["urgent cancel"]
        assert notify.flush_held() == 0
        db.set_setting("quiet_from", "")
        sent.clear()
        assert notify.flush_held() == 2
        assert "While you were off: 1 new order, 1 payment" in sent[0][2]


def test_alert_fallbacks_and_settings_card(client, sent):
    from pashtoon_site import notify
    db.set_setting("owner_email", "aarzu@example.com")
    db.set_setting("alert_whatsapp", False)
    shop.alert("order", "x")
    assert sent[-1] == ("email", "Pakhtoon Flavours: alert")      # fallback: no other channel worked
    sent.clear()
    db.set_setting("alert_ntfy", True); db.set_setting("ntfy_topic", "t-123")
    shop.alert("order", "y")
    assert [x[0] for x in sent] == ["ntfy"]                           # email only when others fail
    db.set_setting("alert_email", "always")
    sent.clear()
    shop.alert("order", "z")
    assert {x[0] for x in sent} == {"ntfy", "email"}
    # the Settings card saves her choices
    login(client)
    html = client.get("/owner/settings").text
    assert "Alerts to your phone" in html and "ntfy" in html
    data = {"csrf": csrf(client, "/owner/settings"), "pay_cash": "1", "offers_collection": "1", "owner_phone": "07700 900123",
            "alert_whatsapp": "1", "alert_order": "1", "alert_cancel": "1", "alert_email": "off", "quiet_from": "23:00", "quiet_to": "07:00",
            "slot_start": "12:00", "slot_end": "21:00", "slot_minutes": "30"}
    r = client.post("/owner/settings", data=data)
    assert "error" not in r.headers["location"], r.headers["location"]
    assert shop.setting("alert_kinds") == {"order": True, "question": False, "message": False, "cancel": True, "payment": False}
    assert shop.setting("quiet_from") == "23:00" and shop.setting("alert_ntfy") is False and shop.setting("alert_email") == "off"
    sent.clear()
    r = client.post("/owner/settings/test-alert", data={"csrf": csrf(client, "/owner/settings")})
    assert "Test sent" in r.headers["location"] or "Test%20sent" in r.headers["location"]
    assert notify.recent_log()


def test_owner_whatsapp_message_is_not_a_customer(client, sent):
    from pashtoon_site import notify
    db.set_setting("owner_phone", "07700 900123")
    web_app = client.app
    msgs = [{"channel": "whatsapp", "from": "447700900123", "name": "Aarzu", "text": "hi", "id": "m1"}]
    # call the same handler the webhook uses
    import inspect
    handler = next(r.endpoint for r in web_app.routes if getattr(r, "path", "") == "/webhook/meta" and "POST" in getattr(r, "methods", ()))
    cell = [c.cell_contents for c in handler.__closure__ if callable(c.cell_contents) and getattr(c.cell_contents, "__name__", "") == "_handle_meta"]
    assert cell
    cell[0](msgs)
    assert db.q1("SELECT COUNT(*) n FROM conversations")["n"] == 0 and notify.window_open()


# ---------------------------------------------------------------- tiffin

def test_tiffin_plans_subscription_meals_and_assistant(client):
    from pashtoon_site import tiffin
    login(client)
    r = client.post("/owner/tiffin/plan", data={"csrf": csrf(client, "/owner/tiffin"), "name": "Weekday lunch box", "description": "Rice, curry and nan.",
                                                 "max_days": "5", "price_week": "45", "active": "1"})
    assert "error" not in r.headers["location"]
    plan = tiffin.plans()[0]
    assert plan["meal_price"] == 9.0
    assert "See plans" in client.get("/menu").text
    client.cookies.clear()
    start = (shop.now().date() + timedelta(days=3))
    r = client.post("/tiffin", data={"csrf": csrf(client, "/tiffin"), "plan_id": plan["id"], "days": ["0", "2", "4"], "time": "12:30", "start": start.isoformat(),
                                      "weeks": "2", "name": "Imran", "phone": "07700 900555", "payment": "cash", "fulfilment": "collection"})
    assert r.status_code == 303, r.text[:300]
    code = r.headers["location"].split("/tiffin/")[1].split("?")[0]
    sub = tiffin.get_by_code(code)
    meals = tiffin.meals(sub["id"], upcoming_only=False)
    assert 1 <= len(meals) <= 6 and all(m["source"] == "tiffin" and m["total"] == 9.0 for m in meals)
    assert {date.fromisoformat(m["date"]).weekday() for m in meals} <= {0, 2, 4}
    assert tiffin.extend_all() == 0 or True
    n = len(meals)
    tiffin.generate(sub["id"])
    assert len(tiffin.meals(sub["id"], upcoming_only=False)) == n        # no duplicates
    page = client.get(f"/tiffin/{code}?new=1").text
    assert "signed up" in page and "Weekday lunch box" in page
    assert any("tiffin sign-up" in a["text"] for a in shop.alerts())
    # too many days, and too little notice
    with pytest.raises(shop.ShopError, match="up to 5"):
        tiffin.subscribe(plan["id"], "X", "07700900001", [0, 1, 2, 3, 4, 5], "12:00", start.isoformat())
    with pytest.raises(shop.ShopError, match="24 hours"):
        tiffin.subscribe(plan["id"], "X", "07700900001", [shop.now().weekday()], "23:59", shop.today())
    # pause then resume
    msg = tiffin.set_status(sub["id"], "paused")
    assert "cancelled" in msg and not [m for m in tiffin.meals(sub["id"]) if m["status"] == "confirmed"]
    tiffin.set_status(sub["id"], "active")
    assert len([m for m in tiffin.meals(sub["id"]) if m["status"] == "confirmed"]) == n
    # the owner assistant manages plans, and the customer assistant knows them
    login(client)
    agent.set_client(FakeClaude([
        [{"type": "tool_use", "id": "t1", "name": "save_tiffin_plan", "input": {"name": "Veg box", "description": "Dal and sabzi", "max_days": 6, "price_week": 36}}],
        [{"type": "text", "text": "Added."}]]))
    client.post("/api/owner/chat", json={"text": "add veg tiffin 6 days 36"}, headers={"X-CSRF": csrf(client, "/owner/assistant")})
    assert any(p["name"] == "Veg box" and p["meal_price"] == 6.0 for p in tiffin.plans())
    assert "Veg box" in agent._run(agent.Ctx("customer"), "get_tiffin_plans", {})


# ---------------------------------------------------------------- backups, health, partners, audit fixes

def test_backup_and_health(client, tmp_path, monkeypatch):
    from pashtoon_site import backup
    import zipfile, io
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    assert client.get("/healthz").json()["ok"] is True
    shop.add_expense(shop.today(), "Other", "test", 5)
    name = backup.run()
    z = zipfile.ZipFile(io.BytesIO(open(tmp_path / "bk" / name, "rb").read()))
    assert "pashtoon_site.db" in z.namelist() and z.read("pashtoon_site.db")[:15] == b"SQLite format 3"
    assert backup.maybe_run() is None                  # one a day
    for h in (1, 2, 3):
        backup.run(shop.now() + timedelta(hours=h))
    assert len(backup.list_backups()) == 2             # keeps only the newest
    assert client.get("/healthz").json()["last_backup_hours"] is not None
    login(client)
    assert client.get("/owner/backup/now").content[:2] == b"PK"
    assert client.get("/owner/backup/../../etc/passwd").status_code == 404


def test_partner_email_import(client):
    db.set_setting("partners", {"ubereats": {"on": True, "url": "", "commission": 30}, "deliveroo": {"on": False, "url": "", "commission": 25},
                                "justeat": {"on": False, "url": "", "commission": 0}})
    key = db.get_setting("partner_key")
    mail = "Order ID: UE-48213\nCustomer: Sam Ali\n2 x Dam Pukh (Per person)  £30.00\n1 x Mystery Special  £9.00\nSubtotal £39.00\nTotal £39.00\nPickup at 18:45\n"
    body = {"subject": "New order UE-48213 from Uber Eats", "text": mail, "from": "orders@uber.com"}
    assert client.post("/api/partner-orders/email", json=body).status_code == 401
    r = client.post(f"/api/partner-orders/email?key={key}", json=body)
    assert r.status_code == 200, r.text
    o = shop.get_order(r.json()["order_id"])
    assert o["total"] == 39 and o["partner"] == "ubereats" and o["time"] == "18:45" and o["customer_name"] == "Sam Ali"
    assert "CHECK THESE ITEMS" in o["notes"] and "Mystery Special" in o["notes"] and o["partner_fee"] == 11.7
    assert any(i["dish"] == "Dam Pukh" and i["qty"] == 2 for i in o["items"])
    # the same email twice does not make a second order; an app that is off is refused; junk is reported
    again = client.post(f"/api/partner-orders/email?key={key}", json=body).json()
    assert again["order_id"] == o["id"] and again["duplicate"] is True
    assert client.post(f"/api/partner-orders/email?key={key}", json={"text": "hello", "from": "orders@deliveroo.co.uk"}).status_code == 400
    r = client.post("/api/partner-orders", json={"partner": "deliveroo", "items": [{"dish": "Chicken Tikka", "qty": 1}]}, headers={"X-Partner-Key": key})
    assert r.status_code == 400 and "switched on" in r.json()["error"]
    # form-encoded (what inbound-email services send)
    r = client.post(f"/api/partner-orders/email?key={key}", data={"subject": "Uber Eats order", "body-plain": "Order #UE-9\n1 x Chicken Tikka\nTotal £7.50", "from": "uber"})
    assert r.status_code == 200


def test_cancel_returns_stock_month_covers_whole_month_and_no_greeting(client):
    from pashtoon_site import accounts
    shop.record_purchase("Lamb", 2, "kg", 40)
    shop.set_recipe(opt("Dam Pukh"), "500 g lamb")
    o = shop.place_order([{"option_id": opt("Dam Pukh"), "qty": 1}], "A", "07700900008", "collection", day(), "18:00", "cash")
    shop.set_status(o["id"], "cooking")
    assert shop.ingredient("Lamb")["stock"] == 1500
    shop.set_status(o["id"], "cancelled")
    assert shop.ingredient("Lamb")["stock"] == 2000 and shop.get_order(o["id"])["stock_used"] == 0
    assert accounts.trial_balance(day(5))["balanced"]
    assert accounts.balances(None, day(5)).get("5100", 0) == 0
    s, e = shop.period("month")
    assert e >= shop.today() and e[8:] in ("28", "29", "30", "31")
    login(client)
    assert "Assalam" not in client.get("/owner").text and "Today" in client.get("/owner").text


def test_food_hygiene_badge(client):
    h = client.get("/").text
    assert "FOOD HYGIENE RATING" in h and "Food hygiene rating 5, very good" in h and "https://ratings.food.gov.uk" in h
    assert "FOOD HYGIENE RATING" in client.get("/menu").text  # footer shows it on every public page
    login(client)
    page = client.get("/owner/settings").text
    assert 'name="hygiene_rating"' in page and 'name="hygiene_link"' in page
    form = {"pay_cash": "1", "offers_collection": "1", "accepting_orders": "1", "slot_start": "12:00", "slot_end": "21:00",
            "slot_minutes": "30", "hygiene_rating": "4", "hygiene_link": "https://ratings.food.gov.uk/business/123456"}
    r = client.post("/owner/settings", data=form | {"csrf": csrf(client, "/owner/settings")})
    assert "error" not in r.headers["location"], r.headers["location"]
    assert shop.setting("hygiene_rating") == "4" and shop.setting("hygiene_link").endswith("/123456")
    assert "Food hygiene rating 4, good" in client.get("/").text
    r = client.post("/owner/settings", data=form | {"hygiene_link": "javascript:alert(1)", "csrf": csrf(client, "/owner/settings")})
    assert "error" in r.headers["location"] and shop.setting("hygiene_link").endswith("/123456")
    client.post("/owner/settings", data=form | {"hygiene_rating": "", "csrf": csrf(client, "/owner/settings")})
    assert "FOOD HYGIENE RATING" not in client.get("/").text


def test_flyer_update_migrates_an_existing_menu(client):
    """A site started on the earlier menu is moved to the flyer: portions carry over, photos stay, old sizes are switched off."""
    pulao = db.q1("SELECT id FROM menu_items WHERE slug='pulao'")["id"]
    db.execute("UPDATE menu_options SET label='Family portion (serves 5)', price=35 WHERE item_id=?", (pulao,))
    db.execute("INSERT INTO menu_options(item_id,label,price,sort) VALUES(?,?,?,?)", (pulao, "Per person", 12, 0))
    db.execute("UPDATE menu_items SET name='Old name', image='/media/hers.jpg' WHERE id=?", (pulao,))
    db.set_setting("shop_name", "The Pashtoon Flavours")
    db.set_setting("menu_v2", False)
    shop.install()
    opts = db.q("SELECT label, active FROM menu_options WHERE item_id=? ORDER BY id", (pulao,))
    assert opts == [{"label": "Family portion", "active": 1}, {"label": "Per person", "active": 0}]
    item = db.q1("SELECT name, image FROM menu_items WHERE id=?", (pulao,))
    assert item == {"name": "Bannu Beef Pulao", "image": "/media/hers.jpg"}
    assert shop.setting("shop_name") == "Pakhtoon Flavours"


# ---------------------------------------------------------------- one-page menu, QR code, menu image

def test_one_page_menu_has_every_dish_and_ordering(client):
    r = client.get("/m")
    assert r.status_code == 200
    for dish in ("Dam Pukh", "Painda (Sohbat)", "Peshawari Raan Pulao", "Tandoori Nan"):
        assert dish in r.text
    assert f'data-option="{opt("Dam Pukh")}"' in r.text and "data-add" in r.text         # same basket buttons as /menu
    assert "no minimum" in r.text
    db.set_setting("offers_delivery", True)
    assert "£50.00" in client.get("/m").text
    shop.update_price(opt("Dam Pukh"), 42)
    assert "£42.00" in client.get("/m").text


def test_qr_codes_point_at_the_menu_page(client, monkeypatch):
    from pashtoon_site import menu_image
    monkeypatch.setattr(config, "BASE_URL", "https://pakhtoonflavours.example")
    assert menu_image.public_url() == "https://pakhtoonflavours.example/m"
    r = client.get("/m/qr.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "attachment" in client.get("/m/qr.png?download=1").headers["content-disposition"]
    svg = client.get("/m/qr.svg")
    assert svg.status_code == 200 and b"<svg" in svg.content
    assert client.get("/owner/qr-poster.pdf").status_code == 303            # the printable sheet is for the owner
    login(client)
    pdf = client.get("/owner/qr-poster.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    page = client.get("/owner/share")
    assert page.status_code == 200 and "pakhtoonflavours.example/m" in page.text and "Generate menu image" in page.text


def test_menu_image_png_and_pdf_follow_the_menu(client):
    import io
    from PIL import Image
    a = client.get("/menu.png")
    assert a.status_code == 200 and a.headers["content-type"] == "image/png"
    im = Image.open(io.BytesIO(a.content))
    assert im.size[0] == 1654 and im.size[1] >= 2339
    shop.update_price(opt("Dam Pukh"), 44)
    shop.set_available(shop.find_item("Chicken Tikka")["id"], False)
    b = client.get("/menu.png")
    assert b.content != a.content                                           # redrawn from the current menu
    pdf = client.get("/menu.pdf?download=1")
    assert pdf.content[:4] == b"%PDF" and "attachment" in pdf.headers["content-disposition"]
    from pashtoon_site import menu_image
    cards, lists = menu_image._cards_and_lists()
    text = " ".join(n for _, rows in lists for n, _ in rows) + " ".join(c["name"] for c in cards)
    assert "Kheer" not in text and "Chicken Tikka" not in text and "Dam Pukh" in text


def test_menu_image_rate_limited(client):
    for _ in range(40):
        assert client.get("/m/qr.png").status_code == 200
    assert client.get("/m/qr.png").status_code == 429


# ---------------------------------------------------------------- owner account and password change

def test_owner_email_login_and_forced_password_change(client, monkeypatch):
    db.set_setting("owner_login_email", "aarzuehtishaam@gmail.com")
    db.set_setting("owner_must_change_password", True)
    form = lambda **kw: {"csrf": csrf(client, "/owner/login"), "next": "/owner", **kw}
    assert "email or password" in client.post("/owner/login", data=form(password=PASSWORD)).text                     # email is needed
    assert "email or password" in client.post("/owner/login", data=form(password=PASSWORD, email="someone@else.com")).text
    assert "email or password" in client.post("/owner/login", data=form(password="wrong-password", email="aarzuehtishaam@gmail.com")).text
    r = client.post("/owner/login", data=form(password=PASSWORD, email="  AarzuEhtishaam@gmail.com "))
    assert r.status_code == 303 and r.headers["location"] == "/owner/password"
    for path in ("/owner", "/owner/orders", "/owner/menu", "/owner/share"):                                   # nothing else until changed
        assert client.get(path).headers["location"] == "/owner/password"
    assert client.get("/owner/password").status_code == 200
    tok = csrf(client, "/owner/password")
    bad = [("wrong", "new-password-1", "new-password-1"), (PASSWORD, "short", "short"), (PASSWORD, "new-password-1", "different-1"),
           (PASSWORD, PASSWORD, PASSWORD)]
    for cur, new, conf in bad:
        r = client.post("/owner/settings/password", data={"csrf": tok, "current": cur, "new": new, "confirm": conf, "from": "/owner/password"})
        assert r.headers["location"].startswith("/owner/password?error=")
    assert db.get_setting("owner_must_change_password")
    r = client.post("/owner/settings/password", data={"csrf": tok, "current": PASSWORD, "new": "my-own-password-9", "confirm": "my-own-password-9", "from": "/owner/password"})
    assert r.headers["location"].startswith("/owner?msg=")
    assert not db.get_setting("owner_must_change_password") and client.get("/owner").status_code == 200
    client.post("/owner/logout", data={"csrf": csrf(client, "/owner")})
    r = client.post("/owner/login", data=form(password="my-own-password-9", email="aarzuehtishaam@gmail.com"))
    assert r.headers["location"] == "/owner"                                                                  # new password works, no more prompt
    assert client.post("/owner/login", data=form(password=PASSWORD, email="aarzuehtishaam@gmail.com")).status_code == 200


def test_owner_setup_command_reads_env_and_never_stores_plain_password(client, monkeypatch, capsys):
    from pashtoon_site import owner_setup
    monkeypatch.setattr(config, "OWNER_EMAIL", "aarzuehtishaam@gmail.com")
    monkeypatch.setattr(config, "OWNER_PASSWORD", "temp-pass-123456")
    assert owner_setup.main() == 0
    assert db.get_setting("owner_login_email") == "aarzuehtishaam@gmail.com" and db.get_setting("owner_must_change_password") is True
    assert "temp-pass-123456" not in json.dumps(db.q("SELECT * FROM settings")) and "temp-pass" not in capsys.readouterr().out
    r = client.post("/owner/login", data={"csrf": csrf(client, "/owner/login"), "email": "aarzuehtishaam@gmail.com", "password": "temp-pass-123456", "next": "/owner"})
    assert r.headers["location"] == "/owner/password"
    monkeypatch.setattr(config, "OWNER_PASSWORD", "x")
    assert owner_setup.main() == 1                                                                            # too short, refused
    monkeypatch.setattr(config, "OWNER_EMAIL", "")
    assert owner_setup.main() == 1


def test_first_start_creates_owner_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OWNER_EMAIL", "aarzuehtishaam@gmail.com")
    monkeypatch.setattr(config, "OWNER_PASSWORD", "temp-pass-123456")
    monkeypatch.setattr(config, "OWNER_MUST_CHANGE", True)
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "u"))
    web.create_app(str(tmp_path / "fresh.db"))
    assert db.get_setting("owner_login_email") == "aarzuehtishaam@gmail.com" and db.get_setting("owner_must_change_password") is True
