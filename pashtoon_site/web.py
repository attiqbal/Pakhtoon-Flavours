"""The website: public pages and ordering, the owner's dashboard, the kitchen screen and the chat endpoints."""
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from urllib.parse import quote as urlquote

import bcrypt
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import agent, config, db, menu_data, messages, shop, whatsapp
from .shop import ShopError

log = logging.getLogger(__name__)
HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(money=shop.money, menu_categories=menu_data.CATEGORIES, statuses=shop.STATUSES, wa=shop.wa_number)


# ---------------------------------------------------------------- small helpers

_hits: dict[str, deque] = defaultdict(deque)


def allow(key: str, limit: int, seconds: int) -> bool:
    now, dq = time.monotonic(), _hits[key]
    while dq and now - dq[0] > seconds:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


async def check_csrf(request: Request) -> None:
    sent = request.headers.get("x-csrf")
    if not sent:
        form = await request.form()
        sent = form.get("csrf")
    if not sent or not secrets.compare_digest(str(sent), request.session.get("csrf", "")):
        raise HTTPException(400, "This page has expired. Go back, refresh and try again.")


def is_owner(request: Request) -> bool:
    return bool(request.session.get("owner"))


def is_kitchen(request: Request) -> bool:
    return is_owner(request) or bool(request.session.get("kitchen"))


def need_owner(request: Request) -> None:
    if not is_owner(request):
        raise HTTPException(303, headers={"Location": "/owner/login?next=" + urlquote(request.url.path)})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=csrf_token(request), st=shop.settings(), owner=is_owner(request),
               path=request.url.path, base_url=config.BASE_URL)
    if is_owner(request):
        ctx.setdefault("alert_count", shop.unseen_alerts())
        ctx.setdefault("waiting_chats", db.q1("SELECT COUNT(*) n FROM conversations WHERE needs_owner=1")["n"])
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={urlquote(msg)}"
    elif error:
        url += f"{sep}error={urlquote(error)}"
    return RedirectResponse(url, status_code=303)


def _float(x, default=None):
    try:
        return float(str(x).replace("£", "").strip())
    except (TypeError, ValueError):
        return default


def wa_link(phone: str, text: str) -> str:
    return f"https://wa.me/{shop.wa_number(phone)}?text={urlquote(text)}"


# ---------------------------------------------------------------- app

def create_app(db_path: str | None = None) -> FastAPI:
    if db_path:
        db.use(db_path)
    db.init()
    shop.install()
    if config.OWNER_PASSWORD and not db.get_setting("owner_password_hash"):
        db.set_setting("owner_password_hash", bcrypt.hashpw(config.OWNER_PASSWORD.encode(), bcrypt.gensalt()).decode())

    app = FastAPI(title="The Pashtoon Flavours", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie="pf_session", same_site="lax",
                       https_only=config.BASE_URL.startswith("https"), max_age=60 * 60 * 24 * 30)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (301, 302, 303) and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        resp = render(request, "public/error.html", message=exc.detail, code=exc.status_code)
        resp.status_code = exc.status_code
        return resp

    # ------------------------------------------------------------ public site

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        items = shop.menu()
        featured = [i for i in items if i["image"] and i["category"] in ("mains", "special")]
        return render(request, "public/home.html", featured=featured, bands=shop.delivery_bands_text().splitlines())

    @app.get("/menu", response_class=HTMLResponse)
    def menu_page(request: Request):
        return render(request, "public/menu.html", sections=shop.menu_by_category())

    @app.get("/checkout", response_class=HTMLResponse)
    def checkout_page(request: Request):
        d = shop.now().date()
        return render(request, "public/checkout.html", slots=shop.time_slots(), min_date=(d + timedelta(days=1)).isoformat(),
                      max_date=(d + timedelta(days=90)).isoformat(), bands=shop.delivery_bands_text().splitlines())

    def _cart(raw: str) -> list[dict]:
        try:
            data = json.loads(raw or "[]")
            return [{"option_id": int(x["option_id"]), "qty": int(x["qty"])} for x in data][:40]
        except (ValueError, KeyError, TypeError):
            raise ShopError("Your basket couldn't be read. Please add the dishes again.") from None

    @app.post("/api/quote")
    async def api_quote(request: Request):
        body = await request.json()
        try:
            qt = shop.quote(_cart(json.dumps(body.get("items", []))), body.get("fulfilment") or "collection",
                            body.get("postcode", ""), body.get("date", ""), body.get("time", ""),
                            check_time=bool(body.get("date") and body.get("time")))
            return {"ok": True, **qt}
        except ShopError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/checkout")
    async def checkout(request: Request, cart: str = Form(""), name: str = Form(""), phone: str = Form(""), email: str = Form(""),
                       fulfilment: str = Form("collection"), address: str = Form(""), postcode: str = Form(""),
                       date_: str = Form("", alias="date"), time_: str = Form("", alias="time"), payment: str = Form("bank"),
                       notes: str = Form(""), agree: str = Form("")):
        await check_csrf(request)
        form = dict(name=name, phone=phone, email=email, fulfilment=fulfilment, address=address, postcode=postcode,
                    date=date_, time=time_, payment=payment, notes=notes)
        d = shop.now().date()
        page = dict(slots=shop.time_slots(), min_date=(d + timedelta(days=1)).isoformat(),
                    max_date=(d + timedelta(days=90)).isoformat(), bands=shop.delivery_bands_text().splitlines(), form=form)
        if not allow(f"checkout:{ip(request)}", 12, 3600):
            return render(request, "public/checkout.html", error="Too many orders from here in a short time. Please message us.", **page)
        try:
            if not agree:
                raise ShopError("Please tick to confirm you've read the cancellation policy.")
            o = shop.place_order(_cart(cart), name, phone, fulfilment, date_, time_, payment, address, postcode, email, notes)
        except ShopError as e:
            return render(request, "public/checkout.html", error=str(e), **page)
        request.session.setdefault("my_orders", [])
        request.session["my_orders"] = (request.session["my_orders"] + [o["code"]])[-10:]
        return RedirectResponse(f"/o/{o['code']}?new=1", status_code=303)

    @app.get("/o/{code}", response_class=HTMLResponse)
    def track(request: Request, code: str):
        o = shop.get_order_by_code(code)
        if not o:
            raise HTTPException(404, "We couldn't find that order.")
        return render(request, "public/order.html", o=o, new=request.query_params.get("new") == "1",
                      can_cancel=o["status"] == "confirmed" and shop.hours_until(o) >= 24,
                      confirmation=messages.order_confirmation(o), payment_msg=messages.payment_request(o),
                      policy=messages.cancellation_policy())

    @app.post("/o/{code}/paid")
    async def track_paid(request: Request, code: str):
        await check_csrf(request)
        o = shop.get_order_by_code(code)
        if not o:
            raise HTTPException(404, "We couldn't find that order.")
        shop.report_payment(o["id"])
        return back(f"/o/{code}", "Thank you! We'll check and confirm your payment.")

    @app.post("/o/{code}/cancel")
    async def track_cancel(request: Request, code: str):
        await check_csrf(request)
        o = shop.get_order_by_code(code)
        if not o:
            raise HTTPException(404, "We couldn't find that order.")
        try:
            shop.customer_cancel(o["id"])
        except ShopError as e:
            return back(f"/o/{code}", error=str(e))
        return back(f"/o/{code}", "Your order has been cancelled.")

    # ------------------------------------------------------------ customer chat

    @app.post("/api/chat/send")
    async def chat_send(request: Request):
        await check_csrf(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Type a message first."}
        conv = agent.conversation_by_token(request.session.get("chat"))
        if not conv:
            if not allow(f"chatstart:{ip(request)}", 10, 3600):
                return {"ok": False, "error": "Please try again a little later."}
            conv = agent.new_conversation("web", name=(body.get("name") or "").strip()[:60])
            request.session["chat"] = conv["token"]
        if not allow(f"chat:{conv['id']}", 25, 600) or not allow(f"chatip:{ip(request)}", 60, 600):
            return {"ok": False, "error": "You're sending messages very quickly. Please wait a moment."}
        agent.customer_message(conv, text)
        return {"ok": True, "lines": agent.chat_lines(conv["id"], int(body.get("after") or 0))}

    @app.get("/api/chat/poll")
    def chat_poll(request: Request, after: int = 0):
        conv = agent.conversation_by_token(request.session.get("chat"))
        return {"lines": agent.chat_lines(conv["id"], after) if conv else []}

    # ------------------------------------------------------------ calendar feed and WhatsApp

    @app.get("/calendar/{token}.ics")
    def calendar_feed(token: str):
        if not secrets.compare_digest(token, db.get_setting("calendar_token", "")):
            raise HTTPException(404, "Not found")
        return Response(shop.ics_feed(), media_type="text/calendar; charset=utf-8")

    @app.get("/webhook/whatsapp")
    def wa_verify(request: Request):
        p = request.query_params
        if config.WHATSAPP_VERIFY_TOKEN and p.get("hub.mode") == "subscribe" and \
                secrets.compare_digest(p.get("hub.verify_token", ""), config.WHATSAPP_VERIFY_TOKEN):
            return PlainTextResponse(p.get("hub.challenge", ""))
        raise HTTPException(403, "Forbidden")

    @app.post("/webhook/whatsapp")
    async def wa_inbound(request: Request):
        body = await request.body()
        if not whatsapp.signature_ok(body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "Bad signature")
        for m in whatsapp.parse(json.loads(body or b"{}")):
            conv = db.q1("SELECT * FROM conversations WHERE channel='whatsapp' AND contact=? ORDER BY id DESC LIMIT 1", (m["from"],))
            conv = conv or agent.new_conversation("whatsapp", m["from"], m["name"])
            if not allow(f"wa:{m['from']}", 40, 3600):
                continue
            reply = agent.customer_message(conv, m["text"])
            if reply:
                whatsapp.send_text(m["from"], reply)
        return {"ok": True}

    # ------------------------------------------------------------ owner: login

    @app.get("/owner/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "owner/login.html", has_password=bool(db.get_setting("owner_password_hash")))

    @app.post("/owner/login")
    async def login(request: Request, password: str = Form(""), next_: str = Form("/owner", alias="next")):
        await check_csrf(request)
        stored = db.get_setting("owner_password_hash")
        if not allow(f"login:{ip(request)}", 10, 900):
            return render(request, "owner/login.html", has_password=True, error="Too many tries. Wait 15 minutes.")
        if not stored or not bcrypt.checkpw(password.encode(), stored.encode()):
            return render(request, "owner/login.html", has_password=bool(stored), error="That password isn't right.")
        request.session.clear()
        request.session["owner"] = True
        return RedirectResponse(next_ if next_.startswith("/owner") else "/owner", status_code=303)

    @app.post("/owner/logout")
    async def logout(request: Request):
        await check_csrf(request)
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ owner: dashboard and orders

    @app.get("/owner", response_class=HTMLResponse)
    def dashboard(request: Request):
        need_owner(request)
        t = shop.today()
        tm = (shop.now().date() + timedelta(days=1)).isoformat()
        week_end = (shop.now().date() + timedelta(days=7)).isoformat()
        m = shop.summary(*shop.period("month"))
        alerts = shop.alerts(12)
        db.execute("UPDATE alerts SET seen=1 WHERE seen=0")
        chats = db.q("SELECT * FROM conversations WHERE needs_owner=1 ORDER BY updated_at DESC LIMIT 5")
        return render(request, "owner/dashboard.html", today_pretty=shop.pretty_date(t), today=shop.orders_on(t), tomorrow=shop.orders_on(tm), tomorrow_date=tm,
                      upcoming=len(shop.list_orders(t, week_end, "open")), month=m, alerts=alerts, chats=chats,
                      unpaid=shop.unpaid_orders(), low=[i for i in shop.ingredients() if i["low"]], alert_count=0)

    @app.get("/owner/orders", response_class=HTMLResponse)
    def orders_page(request: Request, view: str = "upcoming", search: str = ""):
        need_owner(request)
        t = shop.today()
        if search:
            rows = shop.list_orders(search=search.strip())
        elif view == "past":
            rows = list(reversed(shop.list_orders(None, (shop.now().date() - timedelta(days=1)).isoformat())))[:200]
        elif view == "unpaid":
            rows = shop.unpaid_orders()
        elif view == "cancelled":
            rows = shop.list_orders(status="cancelled")
        else:
            rows = shop.list_orders(t, None, "open")
        return render(request, "owner/orders.html", rows=rows, view=view, search=search)

    @app.get("/owner/orders/new", response_class=HTMLResponse)
    def order_new_page(request: Request):
        need_owner(request)
        return render(request, "owner/order_new.html", sections=shop.menu_by_category(), slots=shop.time_slots(), form={})

    @app.post("/owner/orders/new")
    async def order_new(request: Request):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        lines = [{"option_id": int(k[4:]), "qty": int(v)} for k, v in f.items() if k.startswith("qty_") and str(v).strip().isdigit() and int(v) > 0]
        try:
            o = shop.place_order(lines, f.get("name", ""), f.get("phone", ""), f.get("fulfilment", "collection"), f.get("date", ""),
                                 f.get("time", ""), f.get("payment", "cash"), f.get("address", ""), f.get("postcode", ""),
                                 notes=f.get("notes", ""), source="phone")
        except ShopError as e:
            return render(request, "owner/order_new.html", sections=shop.menu_by_category(), slots=shop.time_slots(),
                          form=dict(f), error=str(e))
        return back(f"/owner/orders/{o['id']}", f"Order #{o['id']} added.")

    @app.get("/owner/orders/{order_id}", response_class=HTMLResponse)
    def order_page(request: Request, order_id: int, reason: str = "high demand", new_time: str = ""):
        need_owner(request)
        try:
            o = shop.get_order(order_id)
        except ShopError as e:
            raise HTTPException(404, str(e)) from None
        msgs = [(k, label, messages.render(k, o, reason, new_time)) for k, label in messages.TEMPLATES]
        return render(request, "owner/order.html", o=o, msgs=[(k, label, txt, wa_link(o["phone"], txt)) for k, label, txt in msgs],
                      track_url=f"{config.BASE_URL}/o/{o['code']}")

    @app.post("/owner/orders/{order_id}/status")
    async def order_status(request: Request, order_id: int, status: str = Form(...)):
        need_owner(request)
        await check_csrf(request)
        try:
            shop.set_status(order_id, status)
        except ShopError as e:
            return back(f"/owner/orders/{order_id}", error=str(e))
        return back(f"/owner/orders/{order_id}", f"Order #{order_id} is now {status}.")

    @app.post("/owner/orders/{order_id}/payment")
    async def order_payment(request: Request, order_id: int, payment_status: str = Form(...)):
        need_owner(request)
        await check_csrf(request)
        try:
            shop.set_payment(order_id, payment_status)
        except ShopError as e:
            return back(f"/owner/orders/{order_id}", error=str(e))
        return back(f"/owner/orders/{order_id}", "Payment updated.")

    # ------------------------------------------------------------ kitchen screen

    @app.get("/kitchen", response_class=HTMLResponse)
    def kitchen(request: Request, key: str = ""):
        if key and secrets.compare_digest(key, db.get_setting("kitchen_token", "")):
            request.session["kitchen"] = True
            return RedirectResponse("/kitchen", status_code=303)
        if not is_kitchen(request):
            return RedirectResponse("/owner/login?next=/kitchen", status_code=303)
        return render(request, "owner/kitchen.html")

    @app.get("/api/kitchen")
    def kitchen_data(request: Request, day: str = ""):
        if not is_kitchen(request):
            raise HTTPException(403, "Log in first.")
        try:
            d = shop.parse_date(day).isoformat() if day else shop.today()
        except ShopError:
            d = shop.today()
        rows = shop.orders_on(d)
        need = defaultdict(int)
        for o in rows:
            if o["status"] in ("confirmed", "cooking"):
                for i in o["items"]:
                    need[f"{i['dish']} · {i['portion']}"] += i["qty"]
        pick = ("id", "customer_name", "time", "fulfilment", "status", "notes", "payment_status", "postcode", "items")
        return {"date": d, "pretty": shop.pretty_date(d), "orders": [{k: o[k] for k in pick} for o in rows],
                "totals": sorted(need.items()), "alerts": shop.unseen_alerts() if is_owner(request) else 0}

    @app.post("/api/kitchen/{order_id}/status")
    async def kitchen_status(request: Request, order_id: int):
        if not is_kitchen(request):
            raise HTTPException(403, "Log in first.")
        await check_csrf(request)
        body = await request.json()
        if body.get("status") not in ("cooking", "ready", "completed", "confirmed"):
            raise HTTPException(400, "Unknown status")
        try:
            shop.set_status(order_id, body["status"])
        except ShopError as e:
            raise HTTPException(404, str(e)) from None
        return {"ok": True}

    @app.get("/api/owner/pulse")
    def pulse(request: Request):
        if not is_owner(request):
            raise HTTPException(403, "Log in first.")
        latest = db.q1("SELECT id, text, link FROM alerts ORDER BY id DESC LIMIT 1")
        return {"alerts": shop.unseen_alerts(), "latest": latest,
                "chats": db.q1("SELECT COUNT(*) n FROM conversations WHERE needs_owner=1")["n"]}

    # ------------------------------------------------------------ inbox

    @app.get("/owner/inbox", response_class=HTMLResponse)
    @app.get("/owner/inbox/{conv_id}", response_class=HTMLResponse)
    def inbox(request: Request, conv_id: int | None = None):
        need_owner(request)
        convs = db.q("SELECT * FROM conversations ORDER BY needs_owner DESC, updated_at DESC LIMIT 100")
        for c in convs:
            c["last"] = db.q1("SELECT sender, text FROM chat_log WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (c["id"],)) or {}
        current, lines = None, []
        if conv_id:
            current = db.q1("SELECT * FROM conversations WHERE id=?", (conv_id,))
            if not current:
                raise HTTPException(404, "No such chat.")
            lines = agent.chat_lines(conv_id)
            db.execute("UPDATE conversations SET unread=0 WHERE id=?", (conv_id,))
        return render(request, "owner/inbox.html", convs=convs, current=current, lines=lines, ai_on=agent.configured())

    @app.post("/owner/inbox/{conv_id}/reply")
    async def inbox_reply(request: Request, conv_id: int, text: str = Form(""), pause: str = Form("")):
        need_owner(request)
        await check_csrf(request)
        try:
            agent.owner_reply(conv_id, text)
        except ShopError as e:
            return back(f"/owner/inbox/{conv_id}", error=str(e))
        if pause:
            db.execute("UPDATE conversations SET ai_paused=1 WHERE id=?", (conv_id,))
        return back(f"/owner/inbox/{conv_id}")

    @app.post("/owner/inbox/{conv_id}/ai")
    async def inbox_ai(request: Request, conv_id: int, on: str = Form("")):
        need_owner(request)
        await check_csrf(request)
        db.execute("UPDATE conversations SET ai_paused=?, needs_owner=CASE WHEN ? THEN 0 ELSE needs_owner END WHERE id=?",
                   (0 if on else 1, 1 if on else 0, conv_id))
        return back(f"/owner/inbox/{conv_id}", "The assistant will answer this chat again." if on else "You're handling this chat. The assistant is paused here.")

    @app.post("/owner/inbox/{conv_id}/done")
    async def inbox_done(request: Request, conv_id: int):
        need_owner(request)
        await check_csrf(request)
        db.execute("UPDATE conversations SET needs_owner=0, unread=0 WHERE id=?", (conv_id,))
        return back(f"/owner/inbox/{conv_id}", "Marked as handled.")

    # ------------------------------------------------------------ calendar

    @app.get("/owner/calendar", response_class=HTMLResponse)
    def calendar(request: Request, month: str = ""):
        need_owner(request)
        try:
            y, m = (int(x) for x in month.split("-")) if month else (shop.now().year, shop.now().month)
            date(y, m, 1)
        except ValueError:
            y, m = shop.now().year, shop.now().month
        prev = date(y, m, 1) - timedelta(days=1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        return render(request, "owner/calendar.html", weeks=shop.month_grid(y, m), title=date(y, m, 1).strftime("%B %Y"),
                      prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"),
                      feed=f"{config.BASE_URL}/calendar/{db.get_setting('calendar_token')}.ics")

    @app.post("/owner/calendar/close")
    async def calendar_close(request: Request, day: str = Form(...), month: str = Form("")):
        need_owner(request)
        await check_csrf(request)
        d = shop.parse_date(day).isoformat()
        closed = set(shop.setting("closed_dates") or [])
        closed.symmetric_difference_update({d})
        db.set_setting("closed_dates", sorted(closed))
        return back(f"/owner/calendar?month={month}", f"{shop.pretty_date(d)} is now {'closed' if d in closed else 'open'} for orders.")

    # ------------------------------------------------------------ menu

    @app.get("/owner/menu", response_class=HTMLResponse)
    def menu_admin(request: Request):
        need_owner(request)
        sections = shop.menu_by_category(include_hidden=True)
        for _, _, items in sections:
            for it in items:
                for o in it["options"]:
                    o["has_recipe"] = bool(shop.recipe(o["id"]))
                    o["cost"] = shop.recipe_cost(o["id"])
        return render(request, "owner/menu.html", sections=sections)

    @app.get("/owner/menu/{item_id}", response_class=HTMLResponse)
    def menu_edit_page(request: Request, item_id: str):
        need_owner(request)
        it = None
        if item_id != "new":
            it = next((i for i in shop.menu(include_hidden=True) if str(i["id"]) == item_id), None)
            if not it:
                raise HTTPException(404, "No such dish.")
            for o in it["options"]:
                o["recipe"] = shop.recipe_text(o["id"])
                o["cost"] = shop.recipe_cost(o["id"])
        return render(request, "owner/menu_edit.html", it=it)

    @app.post("/owner/menu/{item_id}")
    async def menu_edit(request: Request, item_id: str):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        opts = []
        for idx in range(12):
            label = f.get(f"opt_label_{idx}")
            if label is None:
                continue
            oid = f.get(f"opt_id_{idx}")
            opts.append((int(oid) if oid and oid.isdigit() else None, label, _float(f.get(f"opt_price_{idx}"))))
        try:
            new_id = shop.save_item(None if item_id == "new" else int(item_id), f.get("name", ""), f.get("category", "mains"),
                                    f.get("description", ""), int(f.get("notice_hours") or 24), opts, bool(f.get("available")))
            for o in shop.menu(include_hidden=True):
                if o["id"] == new_id:
                    for opt in o["options"]:
                        txt = f.get(f"recipe_{opt['id']}")
                        if txt is not None and txt.strip() != shop.recipe_text(opt["id"]).strip():
                            shop.set_recipe(opt["id"], txt)
        except (ShopError, ValueError) as e:
            return back(f"/owner/menu/{item_id}", error=str(e))
        return back(f"/owner/menu/{new_id}", "Saved.")

    @app.post("/owner/menu/{item_id}/toggle")
    async def menu_toggle(request: Request, item_id: int):
        need_owner(request)
        await check_csrf(request)
        it = db.q1("SELECT * FROM menu_items WHERE id=?", (item_id,))
        if not it:
            raise HTTPException(404, "No such dish.")
        return back("/owner/menu", shop.set_available(item_id, not it["available"]))

    # ------------------------------------------------------------ stock

    @app.get("/owner/stock", response_class=HTMLResponse)
    def stock(request: Request, start: str = "", end: str = ""):
        need_owner(request)
        t = shop.now().date()
        start = start or (t + timedelta(days=1)).isoformat()
        end = end or (t + timedelta(days=2)).isoformat()
        try:
            sl = shop.shopping_list(shop.parse_date(start).isoformat(), shop.parse_date(end).isoformat())
        except ShopError:
            sl = {"orders": 0, "rows": [], "no_recipe": [], "total": 0}
        return render(request, "owner/stock.html", items=shop.ingredients(), history=shop.stock_history(),
                      sl=sl, start=start, end=end, reasons=shop.WRITE_OFF_REASONS, today=shop.today())

    @app.post("/owner/stock/{action}")
    async def stock_action(request: Request, action: str):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            if action == "purchase":
                msg = shop.record_purchase(f.get("name", ""), _float(f.get("qty"), 0), f.get("unit", "g"), _float(f.get("cost"), -1), f.get("date") or "")
            elif action == "writeoff":
                reason = f.get("reason", "")
                if f.get("detail"):
                    reason = f"{reason}: {f.get('detail')}"
                msg = shop.write_off(f.get("ingredient", ""), _float(f.get("qty"), 0), f.get("unit", ""), reason)
            elif action == "count":
                msg = shop.count_stock(f.get("ingredient", ""), _float(f.get("qty"), -1), f.get("unit", ""))
            elif action == "low":
                i = shop.ingredient(f.get("ingredient", ""))
                shop.set_low_level(i["id"], shop.to_base(_float(f.get("qty"), 0), f.get("unit") or i["unit"])[0])
                msg = f"Low-stock warning for {i['name']} saved."
            else:
                raise HTTPException(404, "Unknown action")
        except ShopError as e:
            return back("/owner/stock", error=str(e))
        return back("/owner/stock", msg)

    # ------------------------------------------------------------ money

    @app.get("/owner/money", response_class=HTMLResponse)
    def money_page(request: Request, period: str = "month", start: str = "", end: str = ""):
        need_owner(request)
        try:
            s, e = (shop.parse_date(start).isoformat(), shop.parse_date(end).isoformat()) if start and end else shop.period(period)
        except ShopError:
            s, e = shop.period("month")
        return render(request, "owner/money.html", m=shop.summary(s, e), period=period, expenses=shop.expenses(s, e),
                      categories=shop.EXPENSE_CATEGORIES, today=shop.today())

    @app.post("/owner/money/expense")
    async def money_expense(request: Request, date_: str = Form("", alias="date"), category: str = Form("Other"),
                            description: str = Form(""), amount: str = Form("")):
        need_owner(request)
        await check_csrf(request)
        try:
            msg = shop.add_expense(date_ or shop.today(), category, description, _float(amount, 0))
        except ShopError as e:
            return back("/owner/money", error=str(e))
        return back("/owner/money", msg)

    # ------------------------------------------------------------ settings

    @app.get("/owner/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        need_owner(request)
        return render(request, "owner/settings.html", ai_on=agent.configured(), wa_on=whatsapp.enabled(),
                      kitchen_link=f"{config.BASE_URL}/kitchen?key={db.get_setting('kitchen_token')}",
                      feed=f"{config.BASE_URL}/calendar/{db.get_setting('calendar_token')}.ics")

    @app.post("/owner/settings")
    async def settings_save(request: Request):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            text_keys = ("shop_name", "tagline", "kitchen_postcode", "area", "whatsapp", "instagram", "tiktok", "bank_details",
                         "review_link", "announcement", "assistant_name")
            for k in text_keys:
                if k in f:
                    db.set_setting(k, f.get(k, "").strip()[:600])
            for k in ("accepting_orders", "offers_delivery", "offers_collection", "ai_enabled"):
                db.set_setting(k, bool(f.get(k)))
            db.set_setting("slot_start", shop.parse_time(f.get("slot_start", "12:00")))
            db.set_setting("slot_end", shop.parse_time(f.get("slot_end", "21:00")))
            db.set_setting("slot_minutes", max(5, int(f.get("slot_minutes") or 30)))
            db.set_setting("daily_order_limit", max(0, int(f.get("daily_order_limit") or 0)))
            db.set_setting("max_delivery_miles", max(0.0, _float(f.get("max_delivery_miles"), 0)))
            bands = []
            for i in range(3):
                fee = _float(f.get(f"band_fee_{i}"))
                if fee is None:
                    continue
                upto = _float(f.get(f"band_miles_{i}"))
                bands.append([upto, fee, _float(f.get(f"band_min_{i}"), 0) or 0])
            if bands:
                bands.sort(key=lambda b: (b[0] is None, b[0] or 0))
                db.set_setting("delivery_bands", bands)
        except (ShopError, ValueError) as e:
            return back("/owner/settings", error=str(e))
        return back("/owner/settings", "Settings saved.")

    @app.post("/owner/settings/password")
    async def change_password(request: Request, current: str = Form(""), new: str = Form("")):
        need_owner(request)
        await check_csrf(request)
        stored = db.get_setting("owner_password_hash")
        if stored and not bcrypt.checkpw(current.encode(), stored.encode()):
            return back("/owner/settings", error="Your current password isn't right.")
        if len(new) < 8:
            return back("/owner/settings", error="Use at least 8 characters for the new password.")
        db.set_setting("owner_password_hash", bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode())
        return back("/owner/settings", "Password changed.")

    @app.post("/owner/settings/new-links")
    async def new_links(request: Request):
        need_owner(request)
        await check_csrf(request)
        for key in ("calendar_token", "kitchen_token"):
            db.set_setting(key, secrets.token_urlsafe(18))
        return back("/owner/settings", "New kitchen and calendar links made. The old ones no longer work.")

    # ------------------------------------------------------------ owner assistant

    @app.get("/owner/assistant", response_class=HTMLResponse)
    def assistant_page(request: Request):
        need_owner(request)
        return render(request, "owner/assistant.html", history=agent.owner_history(), ai_on=agent.configured())

    @app.post("/api/owner/chat")
    async def assistant_chat(request: Request):
        need_owner(request)
        await check_csrf(request)
        body = await request.json()
        if not allow("ownerchat", 60, 600):
            return {"reply": "Slow down a little, please try again in a minute."}
        return {"reply": agent.owner_chat(body.get("text", ""))}

    return app
