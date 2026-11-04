"""End-to-end tests: no network and no API key needed (postcodes and Claude are faked)."""
import hashlib
import hmac
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pashtoon_site import agent, config, db, geo, shop, web

PASSWORD = "kitchen-secret-1"
POSTCODES = {"OL7 9TG": (53.4875, -2.1029), "OL6 6AA": (53.4930, -2.0920), "M1 1AE": (53.4794, -2.2453), "WA1 1AA": (53.3900, -2.5900)}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OWNER_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
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
            "date": day(), "time": "18:00", "payment": "bank", "agree": "1", "csrf": csrf(c, "/checkout")}
    data.update(kw)
    return c.post("/checkout", data=data)


# ---------------------------------------------------------------- menu and site

def test_real_menu_loaded_and_public_pages(client):
    names = [i["name"] for i in shop.menu()]
    for dish in ["Dam Pukh", "Painda (Sohbat)", "Bannu Beef Pulao", "Namkeen Gosht", "Peshawari Kebab", "Peshawari Karahi",
                 "Peshawari Raan Pulao", "Nan", "Kheer"]:
        assert dish in names
    assert shop.find_option("Dam Pukh", "family")["price"] == 40
    assert shop.find_option("Raan")["notice_hours"] == 48
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 0     # no demo orders
    for path in ["/", "/menu", "/checkout", "/owner/login"]:
        assert client.get(path).status_code == 200
    menu = client.get("/menu").text
    assert "Namkeen Gosht" in menu and "£110.00" in menu and "AI assistant" in menu


def test_install_is_idempotent(client):
    shop.update_price(opt("Kheer"), 8)
    shop.install()
    assert shop.find_option("Kheer")["price"] == 8
    assert db.q1("SELECT COUNT(*) n FROM menu_items")["n"] == 13


# ---------------------------------------------------------------- ordering rules

def test_quote_and_delivery_bands(client):
    lines = [{"option_id": opt("Dam Pukh", "per person"), "qty": 2}, {"option_id": opt("Nan"), "qty": 2}]
    q = shop.quote(lines, "collection", d=day(), t="18:00")
    assert q["food_total"] == 34 and q["total"] == 34
    near = shop.quote(lines, "delivery", "OL6 6AA", day(), "18:00")
    assert near["delivery_fee"] == 3.99 and near["total"] == 37.99
    mid = shop.quote(lines, "delivery", "M1 1AE", day(), "18:00")     # about 6 miles
    assert mid["delivery_fee"] == 5.99
    with pytest.raises(shop.ShopError, match="minimum order of £50"):   # over 6 miles needs £50 of food
        shop.quote(lines, "delivery", "WA1 1AA", day(), "18:00")
    far = shop.quote(lines * 2, "delivery", "WA1 1AA", day(), "18:00")
    assert far["delivery_fee"] == 7.99 and far["food_total"] == 68
    with pytest.raises(shop.ShopError, match="couldn't find"):
        shop.quote(lines, "delivery", "ZZ9 9ZZ", day(), "18:00")


def test_notice_closed_days_and_daily_limit(client):
    lines = [{"option_id": opt("Kheer"), "qty": 1}]
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
    r = checkout(client, [{"option_id": opt("Namkeen", "half"), "qty": 1}, {"option_id": opt("Nan"), "qty": 3}],
                 fulfilment="delivery", address="12 Park Road, Ashton", postcode="ol66aa", date=day(5))
    assert r.status_code == 303, r.text[:500]
    code = r.headers["location"].split("/o/")[1].split("?")[0]
    page = client.get(f"/o/{code}?new=1").text
    assert "Thank you" in page and "£31.99" in page and "Naan: yes, 3" in page and "cancellation policy" in page
    o = shop.get_order_by_code(code)
    assert o["postcode"] == "OL6 6AA" and o["delivery_fee"] == 3.99 and o["status"] == "confirmed"
    assert any("New order" in a["text"] for a in shop.alerts())
    client.post(f"/o/{code}/paid", data={"csrf": csrf(client, f"/o/{code}")})
    assert shop.get_order(o["id"])["payment_status"] == "reported"
    r = client.post(f"/o/{code}/cancel", data={"csrf": csrf(client, f"/o/{code}")})
    assert shop.get_order(o["id"])["status"] == "cancelled"


def test_checkout_errors_are_shown_not_crashes(client):
    r = checkout(client, [{"option_id": opt("Kheer"), "qty": 1}], phone="123")
    assert r.status_code == 200 and "mobile number" in r.text
    r = checkout(client, [], name="X")
    assert "basket is empty" in r.text
    r = checkout(client, [{"option_id": opt("Kheer"), "qty": 1}], agree="")
    assert "cancellation policy" in r.text


def test_late_cancel_blocked(client):
    o = shop.place_order([{"option_id": opt("Kheer"), "qty": 1}], "A", "07700900001", "collection", day(2), "18:00", "cash")
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
    o = shop.place_order([{"option_id": opt("Karahi", "lamb, 1kg"), "qty": 1}], "Bilal", "07700900003", "collection", day(2), "19:00", "cash")
    for path in ["/owner", "/owner/orders", "/owner/orders?view=unpaid", f"/owner/orders/{o['id']}", "/owner/orders/new", "/owner/inbox",
                 "/owner/calendar", "/owner/menu", f"/owner/menu/{o['items'][0]['option_id'] and shop.option(o['items'][0]['option_id'])['item_id']}",
                 "/owner/menu/new", "/owner/stock", "/owner/money", "/owner/settings", "/owner/assistant", "/kitchen"]:
        r = client.get(path)
        assert r.status_code == 200, path
    page = client.get(f"/owner/orders/{o['id']}").text
    assert "wa.me/447700900003" in page and "Thank you for your order with The Pashtoon Flavours" in page


def test_phone_order_status_and_kitchen(client):
    login(client)
    t = csrf(client, "/owner/orders/new")
    r = client.post("/owner/orders/new", data={"csrf": t, f"qty_{opt('Pulao', 'family')}": "1", "name": "Sana", "phone": "07700900004",
                                                "date": day(2), "time": "13:00", "fulfilment": "collection", "payment": "cash"})
    assert r.status_code == 303
    oid = int(r.headers["location"].split("/orders/")[1].split("?")[0])
    k = client.get(f"/api/kitchen?day={day(2)}").json()
    assert k["orders"][0]["id"] == oid and k["totals"] == [["Bannu Beef Pulao · Family portion (serves 5)", 1]]
    r = client.post(f"/api/kitchen/{oid}/status", json={"status": "cooking"}, headers={"X-CSRF": csrf(client, "/owner")})
    assert r.status_code == 200 and shop.get_order(oid)["status"] == "cooking"


def test_kitchen_tablet_link(client):
    r = client.get("/kitchen?key=" + db.get_setting("kitchen_token"))
    assert r.status_code == 303
    assert client.get("/api/kitchen").status_code == 200
    assert client.get("/owner").status_code == 303        # kitchen access is not owner access


def test_recipes_stock_shopping_writeoff_and_money(client):
    login(client)
    dp = opt("Dam Pukh", "per person")
    shop.set_recipe(dp, "250 g lamb\n1 pcs onion\n1 tbsp oil")
    shop.record_purchase("lamb", 1, "kg", 12)
    o = shop.place_order([{"option_id": dp, "qty": 6}], "Zainab", "07700900005", "collection", day(2), "18:00", "bank")
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
    assert m["sales"] == 90 and m["purchases"] == 36 and m["other"] == 9.5 and m["profit"] == 44.5 and m["waste"] == 2.4
    assert "Spoiled" in client.get("/owner/stock").text
    r = client.post("/owner/stock/writeoff", data={"csrf": csrf(client, "/owner/stock"), "ingredient": str(shop.ingredient("lamb")["id"]),
                                                    "qty": "0.1", "unit": "kg", "reason": "Leftover"})
    assert "Wrote off 100 g of lamb" in client.get(r.headers["location"]).text


def test_menu_edit_and_settings(client):
    login(client)
    it = shop.option(opt("Kheer"))
    r = client.post(f"/owner/menu/{it['item_id']}", data={"csrf": csrf(client, "/owner/menu"), "name": "Kheer", "category": "dessert",
                                                           "description": "Rice pudding", "notice_hours": "24", "available": "1",
                                                           "opt_id_0": str(it["id"]), "opt_label_0": "Per serving", "opt_price_0": "8.00",
                                                           "opt_label_1": "Family bowl", "opt_price_1": "30", f"recipe_{it['id']}": "100 ml milk\n20 g rice"})
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert shop.find_option("Kheer", "serving")["price"] == 8 and shop.find_option("Kheer", "family")["price"] == 30
    assert shop.recipe_text(it["id"]) == "100 ml milk\n20 g rice"
    r = client.post("/owner/settings", data={"csrf": csrf(client, "/owner/settings"), "accepting_orders": "1", "offers_delivery": "1",
                                              "offers_collection": "1", "slot_start": "13:00", "slot_end": "20:00", "slot_minutes": "30",
                                              "daily_order_limit": "8", "bank_details": "Sort 00-00-00", "band_miles_0": "3", "band_fee_0": "4.50",
                                              "band_fee_1": "8", "band_min_1": "60", "max_delivery_miles": "10", "shop_name": "The Pashtoon Flavours"})
    assert r.status_code == 303
    assert shop.setting("daily_order_limit") == 8 and shop.setting("delivery_bands") == [[3, 4.5, 0], [None, 8, 60]]
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
    items = [{"dish": "Painda", "portion": "per person", "qty": 2}]
    fake = FakeClaude([
        [{"type": "tool_use", "id": "t1", "name": "quote_order", "input": {"items": items, "fulfilment": "collection", "date": day(), "time": "18:00"}}],
        [{"type": "text", "text": "Hi, I'm Zara, the AI assistant. 2 x Painda per person, total £24.00. Shall I place it?"}],
        [{"type": "tool_use", "id": "t2", "name": "place_order", "input": {"items": items, "fulfilment": "collection", "date": day(), "time": "18:00",
                                                                             "name": "Maryam", "phone": "07700900007", "payment_method": "cash"}}],
        [{"type": "text", "text": "Done! Your order number is in the link above."}],
    ])
    agent.set_client(fake)
    t = csrf(client)
    r = client.post("/api/chat/send", json={"text": "2 painda for Friday 6pm collection"}, headers={"X-CSRF": t}).json()
    assert "AI assistant" in r["lines"][-1]["text"]
    assert "total £24.00" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    system = fake.calls[0]["system"]
    assert "You are an AI, and you say so" in system and "Never claim to be human" in system
    r = client.post("/api/chat/send", json={"text": "yes please"}, headers={"X-CSRF": t}).json()
    o = db.q1("SELECT * FROM orders")
    assert o and o["customer_name"] == "Maryam" and o["total"] == 24 and o["source"] == "chat"
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
    o = shop.place_order([{"option_id": opt("Kheer"), "qty": 1}], "A", "07700900008", "collection", day(), "18:00", "cash")
    ctx = agent.Ctx("customer", None)
    out, err = agent.run_tool(ctx, "order_status", {"order_id": o["id"], "phone": "07000000000"})
    assert err and "don't match" in out
    out, err = agent.run_tool(ctx, "order_status", {"order_id": o["id"], "phone": "+44 7700 900008"})
    assert not err and "Kheer" in out
    out, err = agent.run_tool(ctx, "update_price", {"option_id": 1, "price": 1})
    assert err                                                          # customers can't use owner tools


def test_owner_assistant(client):
    login(client)
    dp = opt("Painda", "per person")
    agent.set_client(FakeClaude([
        [{"type": "tool_use", "id": "u1", "name": "update_price", "input": {"option_id": dp, "price": 14}}],
        [{"type": "text", "text": "Done: Painda per person is now £14."}],
    ]))
    r = client.post("/api/owner/chat", json={"text": "Painda per person is now £14"}, headers={"X-CSRF": csrf(client, "/owner/assistant")}).json()
    assert "£14" in r["reply"] and shop.option(dp)["price"] == 14
    assert "Painda per person is now £14" in client.get("/owner/assistant").text


def test_whatsapp_webhook(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "WHATSAPP_VERIFY_TOKEN", "vt")
    assert client.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42").text == "42"
    body = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "447700900009", "profile": {"name": "Noor"}}],
                                                         "messages": [{"from": "447700900009", "id": "m1", "type": "text", "text": {"body": "menu?"}}]}}]}]}).encode()
    assert client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    conv = db.q1("SELECT * FROM conversations WHERE channel='whatsapp'")
    assert conv["name"] == "Noor" and conv["needs_owner"] == 1
