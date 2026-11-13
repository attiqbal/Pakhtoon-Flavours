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
import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import accounts, agent, backup, config, db, gcal, llm, menu_data, menu_image, messages, meta, notify, payments, reports, shop, tasks, tiffin
from . import partners as partners_mod
from .shop import ShopError

log = logging.getLogger(__name__)
HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(money=shop.money, menu_categories=menu_data.CATEGORIES, statuses=shop.STATUSES, wa=shop.wa_number,
                              pay_label=payments.label, pretty=shop.pretty_date, image_url=lambda img: img if (img or '').startswith('/') else f'/static/img/{img}')


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


def path_is_private(path: str) -> bool:
    return path.startswith(("/owner", "/kitchen", "/checkout", "/o/"))


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=csrf_token(request), st=shop.settings(), owner=is_owner(request),
               path=request.url.path, base_url=config.BASE_URL)
    ctx["partners"] = shop.partners_on()
    pid = int(shop.setting("promo_item_id") or 0)
    ctx["promo"] = None
    if pid and not path_is_private(request.url.path):
        ctx["promo"] = db.q1("SELECT * FROM menu_items WHERE id=? AND available=1 AND video IS NOT NULL", (pid,))
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


IMAGE_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp4": "video/mp4", "webm": "video/webm"}
MAX_PHOTO = 8 * 1024 * 1024
MAX_VIDEO = 60 * 1024 * 1024


def sniff(data: bytes) -> tuple[str, str] | None:
    """(kind, extension) from the file's first bytes, so a renamed file can't sneak in. None if not an allowed type."""
    for magic, ext in IMAGE_TYPES.items():
        if data.startswith(magic) and (ext != "webp" or data[8:12] == b"WEBP"):
            return "image", ext
    if data[4:8] == b"ftyp":
        return "video", "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm"
    return None


async def save_media(upload, allow_video: bool = True, title: str = "") -> dict | None:
    """A photo or video from the owner: checked by its real content, kept in data/uploads and added to her media library.
    Returns {'id', 'kind', 'url', 'path', 'media_type', 'title'}."""
    if not upload or not getattr(upload, "filename", ""):
        return None
    head = await upload.read(64)
    found = sniff(head)
    if not found or (found[0] == "video" and not allow_video):
        raise ShopError("Please upload a JPEG, PNG or WebP photo" + (", or an MP4 or WebM video." if allow_video else "."))
    kind, ext = found
    limit = MAX_VIDEO if kind == "video" else MAX_PHOTO
    name = f"{'dish' if kind == 'image' else 'clip'}-{secrets.token_hex(8)}.{ext}"
    path = os.path.join(config.UPLOAD_DIR, name)
    size = len(head)
    with open(path, "wb") as fh:
        fh.write(head)
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                fh.close()
                os.remove(path)
                raise ShopError(f"That file is too big. Photos can be up to 8 MB and videos up to 60 MB.")
            fh.write(chunk)
    title = title or (upload.filename or "")[:80]
    mid = db.execute("INSERT INTO media(kind,url,title,created_at) VALUES(?,?,?,?)", (kind, f"/media/{name}", title, shop.iso_now()))
    return {"id": mid, "kind": kind, "url": f"/media/{name}", "path": path, "media_type": MEDIA_TYPES[ext], "title": title}


async def save_photo(upload) -> str | None:
    """A dish photo from the owner (kept for older callers)."""
    m = await save_media(upload, allow_video=False)
    return m["url"] if m else None


def wa_link(phone: str, text: str) -> str:
    return f"https://wa.me/{shop.wa_number(phone)}?text={urlquote(text)}"


def ensure_owner() -> None:
    """Make the owner account if it is missing. Never replaces an existing email or password."""
    if not db.get_setting("owner_login_email"):
        email = config.OWNER_EMAIL or menu_data.OWNER_EMAIL
        db.set_setting("owner_login_email", email)
        if not db.get_setting("owner_email"):
            db.set_setting("owner_email", email)        # order alerts go here until she picks another
    if not db.get_setting("owner_password_hash"):
        password = config.OWNER_PASSWORD or menu_data.STARTER_PASSWORD
        db.set_setting("owner_password_hash", bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())
        db.set_setting("owner_starter_password", password == menu_data.STARTER_PASSWORD)


# ---------------------------------------------------------------- app

def create_app(db_path: str | None = None) -> FastAPI:
    if db_path:
        db.use(db_path)
    db.init()
    shop.install()
    ensure_owner()

    tasks.start()
    app = FastAPI(title="Pakhtoon Flavours", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie="pf_session", same_site="lax",
                       https_only=config.BASE_URL.startswith("https"), max_age=60 * 60 * 24 * 30)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    app.mount("/media", StaticFiles(directory=config.UPLOAD_DIR), name="media")

    @app.get("/healthz")
    def healthz():
        """For the host's health check: is the database answering, and how old is the last backup."""
        try:
            db.q1("SELECT 1 AS ok")
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "last_backup_hours": backup.last_backup_age_hours()}

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
        featured = [i for i in items if i["image"] and i["category"] in ("mains", "special") and i["options"]]
        return render(request, "public/home.html", featured=featured, bands=shop.delivery_bands_text().splitlines(),
                      clips=[i for i in items if i["video"]], tiffin=next((i for i in items if i["category"] == "tiffin"), None),
                      tiffin_plans=tiffin.plans(),
                      earliest=shop.earliest_day(), pay_methods=payments.methods(),
                      from_price=min((o["price"] for i in featured for o in i["options"]), default=0))

    @app.get("/menu", response_class=HTMLResponse)
    def menu_page(request: Request):
        return render(request, "public/menu.html", sections=shop.menu_by_category(), tiffin_plans=tiffin.plans())

    @app.get("/checkout", response_class=HTMLResponse)
    def checkout_page(request: Request):
        d = shop.now().date()
        return render(request, "public/checkout.html", slots=shop.time_slots(), min_date=(d + timedelta(days=1)).isoformat(),
                      max_date=(d + timedelta(days=90)).isoformat(), bands=shop.delivery_bands_text().splitlines(),
                      pay_methods=payments.methods())

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
            qt = await run_in_threadpool(shop.quote, _cart(json.dumps(body.get("items", []))), body.get("fulfilment") or "collection",
                                         body.get("postcode", ""), body.get("date", ""), body.get("time", ""),
                                         check_time=bool(body.get("date") and body.get("time")))
            return {"ok": True, **qt}
        except ShopError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/checkout")
    async def checkout(request: Request, cart: str = Form(""), name: str = Form(""), phone: str = Form(""), email: str = Form(""),
                       fulfilment: str = Form("collection"), address: str = Form(""), postcode: str = Form(""),
                       date_: str = Form("", alias="date"), time_: str = Form("", alias="time"), payment: str = Form(""),
                       notes: str = Form(""), agree: str = Form("")):
        await check_csrf(request)
        form = dict(name=name, phone=phone, email=email, fulfilment=fulfilment, address=address, postcode=postcode,
                    date=date_, time=time_, payment=payment, notes=notes)
        d = shop.now().date()
        page = dict(slots=shop.time_slots(), min_date=(d + timedelta(days=1)).isoformat(),
                    max_date=(d + timedelta(days=90)).isoformat(), bands=shop.delivery_bands_text().splitlines(), form=form,
                    pay_methods=payments.methods())
        if not allow(f"checkout:{ip(request)}", 12, 3600):
            return render(request, "public/checkout.html", error="Too many orders from here in a short time. Please message us.", **page)
        try:
            if not agree:
                raise ShopError("Please tick to confirm you've read the cancellation policy.")
            o = await run_in_threadpool(shop.place_order, _cart(cart), name, phone, fulfilment, date_, time_, payment, address,
                                        postcode, email, notes)
        except ShopError as e:
            return render(request, "public/checkout.html", error=str(e), **page)
        request.session.setdefault("my_orders", [])
        request.session["my_orders"] = (request.session["my_orders"] + [o["code"]])[-10:]
        if o["payment_method"] == "card":
            try:
                return RedirectResponse(await run_in_threadpool(payments.create_checkout, o), status_code=303)
            except RuntimeError:
                log.exception("card checkout failed for order %s", o["id"])
                return back(f"/o/{o['code']}?new=1", error="Your order is placed, but the card payment page didn't open. Press Pay now to try again.")
        return RedirectResponse(f"/o/{o['code']}?new=1", status_code=303)

    @app.get("/o/{code}", response_class=HTMLResponse)
    def track(request: Request, code: str):
        o = shop.get_order_by_code(code)
        if not o:
            raise HTTPException(404, "We couldn't find that order.")
        sid = request.query_params.get("stripe_session")
        if sid and o["payment_status"] != "paid":
            if payments.paid_order_id(sid) == o["id"]:
                shop.mark_card_paid(o["id"])
                o = shop.get_order_by_code(code)
        return render(request, "public/order.html", o=o, new=request.query_params.get("new") == "1" or bool(sid),
                      card_ready=payments.card_enabled(), paid_now=bool(sid) and o["payment_status"] == "paid",
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

    @app.post("/o/{code}/pay")
    async def track_pay(request: Request, code: str):
        await check_csrf(request)
        o = shop.get_order_by_code(code)
        if not o:
            raise HTTPException(404, "We couldn't find that order.")
        if o["payment_status"] == "paid" or o["status"] == "cancelled" or not payments.card_enabled():
            return back(f"/o/{code}", error="This order can't be paid online.")
        if not allow(f"pay:{ip(request)}", 20, 3600):
            return back(f"/o/{code}", error="Please wait a little and try again.")
        try:
            return RedirectResponse(await run_in_threadpool(payments.create_checkout, o), status_code=303)
        except RuntimeError:
            return back(f"/o/{code}", error="The card payment page didn't open. Please try again in a minute, or pay cash.")

    @app.post("/webhook/stripe")
    async def stripe_webhook(request: Request):
        body = await request.body()
        if not payments.webhook_ok(body, request.headers.get("stripe-signature", "")):
            raise HTTPException(400, "Bad signature")
        ev = json.loads(body or b"{}")
        obj = (ev.get("data") or {}).get("object") or {}
        if ev.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and obj.get("payment_status") == "paid":
            try:
                shop.mark_card_paid(int((obj.get("metadata") or {}).get("order_id")))
            except (TypeError, ValueError, ShopError):
                log.warning("Stripe event for unknown order: %s", obj.get("id"))
        return {"ok": True}

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return render(request, "public/privacy.html")

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
        await run_in_threadpool(agent.customer_message, conv, text)     # the AI can take a while; don't hold up other visitors
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

    _seen_ids: deque = deque(maxlen=2000)

    def _handle_meta(items: list[dict]) -> None:
        for m in items:
            if m["channel"] == "whatsapp" and notify.owner_digits() and shop.wa_number(m["from"]) == notify.owner_digits():
                notify.note_owner_message()      # her own message to the business number: opens the 24 hour window, not a customer
                continue
            conv = db.q1("SELECT * FROM conversations WHERE channel=? AND contact=? ORDER BY id DESC LIMIT 1", (m["channel"], m["from"]))
            conv = conv or agent.new_conversation(m["channel"], m["from"], m["name"])
            if not allow(f"meta:{m['channel']}:{m['from']}", 40, 3600):
                continue
            reply = agent.customer_message(conv, m["text"])
            if reply and not meta.send_text(m["channel"], m["from"], reply):
                shop.alert("message", f"A reply on {meta.CHANNELS[m['channel']]} couldn't be sent. Check the Meta settings.", f"/owner/inbox/{conv['id']}")

    @app.get("/webhook/meta")
    @app.get("/webhook/whatsapp")
    def meta_verify(request: Request):
        p = request.query_params
        if config.META_VERIFY_TOKEN and p.get("hub.mode") == "subscribe" and \
                secrets.compare_digest(p.get("hub.verify_token", ""), config.META_VERIFY_TOKEN):
            return PlainTextResponse(p.get("hub.challenge", ""))
        raise HTTPException(403, "Forbidden")

    @app.post("/webhook/meta")
    @app.post("/webhook/whatsapp")
    async def meta_inbound(request: Request, tasks: BackgroundTasks):
        body = await request.body()
        if not meta.signature_ok(body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "Bad signature")
        try:
            items = meta.parse(json.loads(body or b"{}"))
        except ValueError:
            raise HTTPException(400, "Bad JSON") from None
        fresh = []
        for m in items:          # Meta retries deliveries; answer each message once
            if m["id"] and m["id"] in _seen_ids:
                continue
            _seen_ids.append(m["id"])
            fresh.append(m)
        tasks.add_task(_handle_meta, fresh)      # reply after answering Meta, so it doesn't time out and retry
        return {"ok": True}


    # ------------------------------------------------------------ one-page menu, QR code and menu image

    def _file(data: bytes, media_type: str, name: str, request: Request) -> Response:
        headers = {"Cache-Control": "no-store"}
        if request.query_params.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return Response(data, media_type=media_type, headers=headers)

    @app.get("/m", response_class=HTMLResponse)
    def one_page_menu(request: Request):
        """The page the QR code points to: the whole menu on one page, with ordering."""
        return render(request, "public/qrmenu.html", sections=shop.menu_by_category(), tiffin_plans=tiffin.plans(),
                      min_delivery=shop.delivery_minimum())

    def _limited(request: Request) -> None:
        if not allow(f"img:{ip(request)}", 40, 3600):
            raise HTTPException(429, "Too many requests. Please try again later.")

    @app.get("/m/qr.png")
    async def qr_png_file(request: Request):
        _limited(request)
        return _file(await run_in_threadpool(menu_image.qr_png), "image/png", "pakhtoon-flavours-menu-qr.png", request)

    @app.get("/m/qr.svg")
    async def qr_svg_file(request: Request):
        _limited(request)
        return _file(await run_in_threadpool(menu_image.qr_svg), "image/svg+xml", "pakhtoon-flavours-menu-qr.svg", request)

    @app.get("/menu.png")
    async def menu_png_file(request: Request):
        """The current menu as a shareable picture, drawn fresh each time so it always has the latest dishes and prices."""
        _limited(request)
        return _file(await run_in_threadpool(menu_image.render_png), "image/png", "pakhtoon-flavours-menu.png", request)

    @app.get("/menu.pdf")
    async def menu_pdf_file(request: Request):
        _limited(request)
        return _file(await run_in_threadpool(menu_image.render_pdf), "application/pdf", "pakhtoon-flavours-menu.pdf", request)

    @app.get("/owner/qr-poster.pdf")
    async def qr_poster(request: Request):
        need_owner(request)
        return _file(await run_in_threadpool(menu_image.poster_pdf), "application/pdf", "pakhtoon-flavours-qr-poster.pdf", request)

    @app.get("/owner/share", response_class=HTMLResponse)
    def share_page(request: Request):
        need_owner(request)
        url = menu_image.public_url()
        return render(request, "owner/share.html", menu_url=url, local=any(h in url for h in ("localhost", "127.0.0.1")),
                      stamp=int(time.time()))

    # ------------------------------------------------------------ owner: login

    @app.get("/owner/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "owner/login.html", has_password=bool(db.get_setting("owner_password_hash")))

    @app.post("/owner/login")
    async def login(request: Request, password: str = Form(""), email: str = Form(""), next_: str = Form("/owner", alias="next")):
        await check_csrf(request)
        stored = db.get_setting("owner_password_hash")
        if not allow(f"login:{ip(request)}", 10, 900):
            return render(request, "owner/login.html", has_password=True, error="Too many tries. Wait 15 minutes.")
        owner_email = (db.get_setting("owner_login_email") or "").strip().lower()
        email_ok = not owner_email or secrets.compare_digest(email.strip().lower(), owner_email)
        pw_ok = bool(stored) and bcrypt.checkpw(password.encode(), stored.encode())
        if not (pw_ok and email_ok):
            return render(request, "owner/login.html", has_password=bool(stored), error="That email or password isn't right.")
        request.session.clear()
        request.session["owner"] = True
        return RedirectResponse(next_ if next_.startswith("/owner") else "/owner", status_code=303)

    @app.get("/owner/password", response_class=HTMLResponse)
    def password_page(request: Request):
        need_owner(request)
        return render(request, "owner/password.html")

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
        return render(request, "owner/menu_edit.html", it=it, library=db.q("SELECT * FROM media ORDER BY id DESC LIMIT 60"))

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
                                    f.get("description", ""), int(f.get("notice_hours") or 24), opts, bool(f.get("available")),
                                    enquiry=bool(f.get("enquiry")))
            for o in shop.menu(include_hidden=True):
                if o["id"] == new_id:
                    for opt in o["options"]:
                        txt = f.get(f"recipe_{opt['id']}")
                        if txt is not None and txt.strip() != shop.recipe_text(opt["id"]).strip():
                            shop.set_recipe(opt["id"], txt)
            photo = await save_media(f.get("photo"), allow_video=False)
            if photo:
                db.execute("UPDATE menu_items SET image=? WHERE id=?", (photo["url"], new_id))
            elif f.get("remove_photo"):
                db.execute("UPDATE menu_items SET image=NULL WHERE id=?", (new_id,))
            clip = await save_media(f.get("video"))
            if clip and clip["kind"] != "video":
                raise ShopError("That file is a photo. Use the photo box for photos.")
            if clip:
                db.execute("UPDATE menu_items SET video=? WHERE id=?", (clip["url"], new_id))
            elif f.get("remove_video"):
                db.execute("UPDATE menu_items SET video=NULL WHERE id=?", (new_id,))
            elif f.get("library_media"):
                m = db.q1("SELECT * FROM media WHERE id=?", (int(f.get("library_media")),))
                if m:
                    db.execute(f"UPDATE menu_items SET {'video' if m['kind'] == 'video' else 'image'}=? WHERE id=?", (m["url"], new_id))
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
            sl = {"orders": 0, "rows": [], "no_recipe": [], "total": 0, "check": []}
        return render(request, "owner/stock.html", items=shop.ingredients(), untracked=shop.ingredients(tracked=False),
                      paid_from=shop.PAID_FROM, history=shop.stock_history(),
                      sl=sl, start=start, end=end, reasons=shop.WRITE_OFF_REASONS, today=shop.today())

    @app.post("/owner/stock/{action}")
    async def stock_action(request: Request, action: str):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            if action == "purchase":
                msg = shop.record_purchase(f.get("name", ""), _float(f.get("qty"), 0), f.get("unit", "g"), _float(f.get("cost"), -1), f.get("date") or "",
                                           f.get("paid_from") or "1110")
            elif action == "tracked":
                msg = shop.set_tracked(f.get("ingredient", ""), bool(f.get("tracked")))
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
                      categories=shop.EXPENSE_CATEGORIES, paid_from=shop.PAID_FROM, today=shop.today())

    @app.post("/owner/money/expense")
    async def money_expense(request: Request, date_: str = Form("", alias="date"), category: str = Form("Other"),
                            description: str = Form(""), amount: str = Form(""), paid_from: str = Form("1110")):
        need_owner(request)
        await check_csrf(request)
        try:
            msg = shop.add_expense(date_ or shop.today(), category, description, _float(amount, 0), paid_from)
        except ShopError as e:
            return back("/owner/money", error=str(e))
        return back("/owner/money", msg)

    # ------------------------------------------------------------ settings

    @app.get("/owner/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        need_owner(request)
        return render(request, "owner/settings.html", ai_on=agent.configured(), meta_status=meta.status(), meta_channels=meta.CHANNELS,
                      models=config.MODELS, provider=llm.provider(), model=llm.model(), partners=menu_data.PARTNERS,
                      partner_key=db.get_setting("partner_key"), partner_url=f"{config.BASE_URL}/api/partner-orders",
                      partner_email_url=f"{config.BASE_URL}/api/partner-orders/email?key={db.get_setting('partner_key')}",
                      alert_kinds=notify.KINDS, alert_log=notify.recent_log(), wa_on=meta.enabled("whatsapp"), smtp_on=notify.smtp_ready(),
                      wa_template=bool(config.WHATSAPP_OWNER_TEMPLATE), wa_window=notify.window_open(), ntfy_server=config.NTFY_SERVER,
                      backups=backup.list_backups(), last_backup=db.get_setting("last_backup"),
                      dishes=shop.menu(), openai_on=bool(config.OPENAI_API_KEY), claude_on=bool(config.ANTHROPIC_API_KEY),
                      stripe_on=payments.configured(), stripe_webhook=bool(config.STRIPE_WEBHOOK_SECRET),
                      meta_callback=f"{config.BASE_URL}/webhook/meta", stripe_callback=f"{config.BASE_URL}/webhook/stripe",
                      kitchen_link=f"{config.BASE_URL}/kitchen?key={db.get_setting('kitchen_token')}",
                      feed=f"{config.BASE_URL}/calendar/{db.get_setting('calendar_token')}.ics", google=gcal.status())

    @app.post("/owner/google/connect")
    async def google_connect(request: Request):
        need_owner(request)
        await check_csrf(request)
        if not gcal.configured():
            return back("/owner/settings", error="Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env first (docs/google-calendar.md).")
        request.session["google_state"] = secrets.token_urlsafe(24)
        return RedirectResponse(gcal.auth_url(request.session["google_state"]), status_code=303)

    @app.get("/owner/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        need_owner(request)
        expected = request.session.pop("google_state", "")
        if error:
            return back("/owner/settings", error="Google Calendar was not connected: " + error[:100])
        if not code or not expected or not secrets.compare_digest(state, expected):
            return back("/owner/settings", error="That Google sign-in expired. Please try Connect again.")
        try:
            await run_in_threadpool(gcal.finish_connect, code, shop.setting("shop_name") or "Shop")
        except (gcal.GoogleError, httpx.HTTPError) as e:
            return back("/owner/settings", error=f"Google Calendar was not connected: {str(e)[:150]}")
        return back("/owner/settings", "Google Calendar connected. Your orders are being added now.")

    @app.post("/owner/google/disconnect")
    async def google_disconnect(request: Request):
        need_owner(request)
        await check_csrf(request)
        await run_in_threadpool(gcal.disconnect)
        return back("/owner/settings", "Google Calendar disconnected. Events already in Google stay there.")

    @app.post("/owner/google/sync")
    async def google_sync(request: Request):
        need_owner(request)
        await check_csrf(request)
        n = await run_in_threadpool(gcal.sync_pending)
        err = db.get_setting("google_error", "")
        return back("/owner/settings", error=err) if err else back("/owner/settings", f"Calendar up to date ({n} sent).")

    @app.post("/owner/settings")
    async def settings_save(request: Request):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            text_keys = ("shop_name", "tagline", "kitchen_postcode", "area", "whatsapp", "instagram", "tiktok", "bank_details",
                         "review_link", "announcement", "assistant_name", "hygiene_link")
            if f.get("hygiene_link", "").strip() and not f.get("hygiene_link", "").strip().startswith("https://"):
                raise ShopError("The hygiene rating link should start with https://, for example your page on ratings.food.gov.uk.")
            for k in text_keys:
                if k in f:
                    db.set_setting(k, f.get(k, "").strip()[:600])
            if "hygiene_rating" in f:
                rating = f.get("hygiene_rating", "").strip()
                db.set_setting("hygiene_rating", rating if rating in ("", "0", "1", "2", "3", "4", "5") else "")
            if not (f.get("pay_cash") or f.get("pay_bank") or f.get("pay_card")):
                raise ShopError("Keep at least one way to pay switched on.")
            for k in ("accepting_orders", "offers_delivery", "offers_collection", "ai_enabled", "pay_cash", "pay_bank", "pay_card"):
                db.set_setting(k, bool(f.get(k)))
            partners = {}
            for key, _ in menu_data.PARTNERS:
                partners[key] = {"on": bool(f.get(f"partner_{key}")), "url": f.get(f"partner_url_{key}", "").strip()[:300],
                                 "commission": min(max(_float(f.get(f"partner_fee_{key}"), 0) or 0, 0), 100)}
            db.set_setting("partners", partners)
            phone = f.get("owner_phone", "").strip()[:30]
            if phone and len(shop.wa_number(phone)) < 10:
                raise ShopError("Enter your own WhatsApp number with the country code, like 07700 900123.")
            db.set_setting("owner_phone", phone)
            db.set_setting("owner_email", f.get("owner_email", "").strip()[:120])
            db.set_setting("alert_kinds", {k: bool(f.get(f"alert_{k}")) for k in notify.KINDS})
            db.set_setting("alert_whatsapp", bool(f.get("alert_whatsapp")))
            ntfy_on = bool(f.get("alert_ntfy"))
            db.set_setting("alert_ntfy", ntfy_on)
            if ntfy_on and not db.get_setting("ntfy_topic"):
                db.set_setting("ntfy_topic", "pashtoon-" + secrets.token_urlsafe(12).lower().replace("_", "x").replace("-", "y"))
            db.set_setting("alert_email", f.get("alert_email") if f.get("alert_email") in ("off", "fallback", "always") else "fallback")
            qa, qb = f.get("quiet_from", ""), f.get("quiet_to", "")
            db.set_setting("quiet_from", shop.parse_time(qa) if qa else "")
            db.set_setting("quiet_to", shop.parse_time(qb) if qb else "")
            db.set_setting("quiet_urgent", bool(f.get("quiet_urgent")))
            db.set_setting("backup_email", bool(f.get("backup_email")))
            if f.get("ai_provider") in ("anthropic", "openai"):
                db.set_setting("ai_provider", f.get("ai_provider"))
            db.set_setting("ai_model", f.get("ai_model", "").strip()[:80])
            db.set_setting("promo_item_id", int(f.get("promo_item_id") or 0))
            if not (f.get("offers_collection") or f.get("offers_delivery")):
                raise ShopError("Keep collection or delivery switched on.")
            db.set_setting("slot_start", shop.parse_time(f.get("slot_start", "12:00")))
            db.set_setting("slot_end", shop.parse_time(f.get("slot_end", "21:00")))
            db.set_setting("slot_minutes", max(5, int(f.get("slot_minutes") or 30)))
            db.set_setting("daily_order_limit", max(0, int(f.get("daily_order_limit") or 0)))
            db.set_setting("max_delivery_miles", max(0.0, _float(f.get("max_delivery_miles"), 0)))
            db.set_setting("delivery_minimum", max(0.0, _float(f.get("delivery_minimum"), 0) or 0))
            bands = []
            for i in range(3):
                fee = _float(f.get(f"band_fee_{i}"))
                if fee is None:
                    continue
                upto = _float(f.get(f"band_miles_{i}"))
                bands.append([upto, fee, 0])
            if bands:
                bands.sort(key=lambda b: (b[0] is None, b[0] or 0))
                db.set_setting("delivery_bands", bands)
        except (ShopError, ValueError) as e:
            return back("/owner/settings", error=str(e))
        return back("/owner/settings", "Settings saved.")

    @app.post("/owner/settings/password")
    async def change_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(None),
                              from_: str = Form("/owner/settings", alias="from")):
        need_owner(request)
        await check_csrf(request)
        page = "/owner/password" if from_ == "/owner/password" else "/owner/settings"
        stored = db.get_setting("owner_password_hash")
        if not allow(f"pw:{ip(request)}", 10, 900):
            return back(page, error="Too many tries. Wait 15 minutes.")
        if stored and not bcrypt.checkpw(current.encode(), stored.encode()):
            return back(page, error="Your current password isn't right.")
        if len(new) < 8:
            return back(page, error="Use at least 8 characters for the new password.")
        if confirm is not None and confirm != new:
            return back(page, error="The two new passwords don't match.")
        if new == current:
            return back(page, error="Choose a password different from the current one.")
        db.set_setting("owner_password_hash", bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode())
        db.set_setting("owner_starter_password", False)
        return back(page, "Password changed.")

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
        """The owner's assistant. Takes JSON {text}, or a form with text and photos/videos to add to her media library."""
        need_owner(request)
        await check_csrf(request)
        if not allow("ownerchat", 60, 600):
            return {"reply": "Slow down a little, please try again in a minute."}
        attachments, media = [], []
        if "multipart" in request.headers.get("content-type", ""):
            f = await request.form()
            text = f.get("text", "")
            try:
                for up in f.getlist("files")[:6]:
                    m = await save_media(up)
                    if m:
                        attachments.append(m)
                        media.append({"id": m["id"], "kind": m["kind"], "url": m["url"]})
            except ShopError as e:
                return {"reply": str(e)}
        else:
            text = (await request.json()).get("text", "")
        if attachments and not text.strip():
            text = "I've attached " + ("a file" if len(attachments) == 1 else "some files") + "."
        return {"reply": await run_in_threadpool(agent.owner_chat, text, attachments), "media": media}

    @app.get("/api/owner/history")
    def assistant_history(request: Request):
        need_owner(request)
        return {"history": agent.owner_history()[-30:]}

    # ------------------------------------------------------------ tiffin

    @app.get("/tiffin", response_class=HTMLResponse)
    def tiffin_page(request: Request):
        return render(request, "public/tiffin.html", plans=tiffin.plans(), days=tiffin.DAYS, slots=shop.time_slots(),
                      earliest=(shop.now().date() + timedelta(days=2)).isoformat(), f={})

    @app.post("/tiffin")
    async def tiffin_signup(request: Request):
        await check_csrf(request)
        f = await request.form()
        if not allow("tiffin:" + ip(request), 10, 3600):
            raise HTTPException(429, "Too many sign-ups. Please try again later.")
        try:
            sub = await run_in_threadpool(
                tiffin.subscribe, int(f.get("plan_id") or 0), f.get("name", ""), f.get("phone", ""), f.getlist("days"), f.get("time", ""),
                f.get("start", ""), int(f.get("weeks") or 0), f.get("fulfilment", "collection"), f.get("address", ""), f.get("postcode", ""),
                f.get("payment", "cash"), f.get("email", ""), f.get("notes", ""))
        except (ShopError, ValueError) as e:
            return render(request, "public/tiffin.html", plans=tiffin.plans(), days=tiffin.DAYS, slots=shop.time_slots(),
                          earliest=(shop.now().date() + timedelta(days=2)).isoformat(), f=dict(f), error=str(e))
        return RedirectResponse(f"/tiffin/{sub['code']}?new=1", status_code=303)

    @app.get("/tiffin/{code}", response_class=HTMLResponse)
    def tiffin_track(request: Request, code: str):
        sub = tiffin.get_by_code(code)
        if not sub:
            raise HTTPException(404, "We couldn't find that tiffin plan.")
        return render(request, "public/tiffin_sub.html", sub=sub, meals=tiffin.meals(sub["id"]), new=request.query_params.get("new"))

    @app.post("/tiffin/{code}/cancel")
    async def tiffin_cancel(request: Request, code: str):
        await check_csrf(request)
        sub = tiffin.get_by_code(code)
        if not sub:
            raise HTTPException(404, "We couldn't find that tiffin plan.")
        msg = tiffin.set_status(sub["id"], "cancelled", by_customer=True)
        shop.alert("cancel", f"{sub['customer_name']} cancelled their tiffin plan ({sub['plan_name']}).", "/owner/tiffin")
        return back(f"/tiffin/{code}", msg)

    @app.get("/owner/tiffin", response_class=HTMLResponse)
    def tiffin_admin(request: Request):
        need_owner(request)
        subs = tiffin.subscriptions()
        for s_ in subs:
            s_["upcoming"] = len(tiffin.meals(s_["id"]))
        return render(request, "owner/tiffin.html", plans=tiffin.plans(active_only=False), subs=subs)

    @app.post("/owner/tiffin/plan")
    async def tiffin_plan_save(request: Request):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            pid = f.get("plan_id")
            tiffin.save_plan(int(pid) if pid and pid.isdigit() else None, f.get("name", ""), f.get("description", ""),
                             int(f.get("max_days") or 5), _float(f.get("price_week"), 0), bool(f.get("active")))
        except (ShopError, ValueError) as e:
            return back("/owner/tiffin", error=str(e))
        return back("/owner/tiffin", "Plan saved.")

    @app.post("/owner/tiffin/{sub_id}/{status}")
    async def tiffin_sub_status(request: Request, sub_id: int, status: str):
        need_owner(request)
        await check_csrf(request)
        try:
            msg = tiffin.set_status(sub_id, {"pause": "paused", "resume": "active", "cancel": "cancelled"}.get(status, status))
        except ShopError as e:
            return back("/owner/tiffin", error=str(e))
        return back("/owner/tiffin", msg)

    # ------------------------------------------------------------ alerts and backups (Settings)

    @app.post("/owner/settings/test-alert")
    async def test_alert(request: Request):
        need_owner(request)
        await check_csrf(request)
        results = await run_in_threadpool(notify.send_now, "Test alert: this is how new orders will reach you.", "Test alert")
        if not results:
            return back("/owner/settings", error="No alert channel is switched on yet. Add your WhatsApp number, ntfy topic or email, tick it and save first.")
        return back("/owner/settings", "Test sent: " + "; ".join(f"{c} {'ok' if ok else 'FAILED (' + d + ')'}" for c, ok, d in results))

    @app.post("/owner/settings/backup")
    async def backup_now(request: Request):
        need_owner(request)
        await check_csrf(request)
        name = await run_in_threadpool(backup.run)
        return back("/owner/settings", f"Backup made: {name}")

    @app.get("/owner/backup/{name}")
    def backup_download(request: Request, name: str):
        need_owner(request)
        if name == "now":
            data, fname = backup.make_zip(), f"pashtoon-backup-{shop.today()}.zip"
        else:
            if name not in [b["name"] for b in backup.list_backups()]:
                raise HTTPException(404, "No such backup.")
            data, fname = open(os.path.join(config.BACKUP_DIR, name), "rb").read(), name
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ------------------------------------------------------------ delivery apps

    def _partner_auth(request: Request) -> JSONResponse | None:
        """The secret from Settings, in the X-Partner-Key header or (for email services that can't set headers) ?key=."""
        key = db.get_setting("partner_key") or ""
        given = request.headers.get("x-partner-key", "") or request.query_params.get("key", "")
        if not key or not secrets.compare_digest(given, key):
            return JSONResponse({"error": "Wrong partner key."}, status_code=401)
        if not allow("partner:" + ip(request), 120, 600):
            return JSONResponse({"error": "Too many requests."}, status_code=429)
        return None

    @app.post("/api/partner-orders")
    async def partner_order(request: Request):
        """Orders from Uber Eats, Deliveroo or Just Eat (through Zapier or another connector). JSON: partner, items
        [{dish, portion, qty}], name, date, time, total, reference, notes, phone."""
        if (bad := _partner_auth(request)):
            return bad
        try:
            b = await request.json()
            lines = [{"option_id": shop.find_option(i.get("dish", ""), i.get("portion", ""))["id"], "qty": int(i.get("qty") or 1)}
                     for i in b.get("items", [])]
            o = shop.place_partner_order(b.get("partner", ""), lines, b.get("name", ""), b.get("date") or shop.today(),
                                         b.get("time") or "12:00", _float(b.get("total")), str(b.get("reference", ""))[:60],
                                         str(b.get("notes", "")), str(b.get("phone", "")))
        except (ShopError, ValueError, AttributeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "order_id": o["id"], "total": o["total"], "commission": o["partner_fee"], "duplicate": bool(o.get("duplicate"))}

    @app.post("/api/partner-orders/email")
    async def partner_order_email(request: Request):
        """A delivery app's order email, forwarded by an inbound-email service. Accepts JSON or a form with subject, text
        (or plain / body-plain / TextBody), from, and optionally partner."""
        if (bad := _partner_auth(request)):
            return bad
        try:
            if "json" in request.headers.get("content-type", ""):
                f = await request.json()
            else:
                f = dict((await request.form()).items())
            text = str(f.get("text") or f.get("plain") or f.get("body-plain") or f.get("TextBody") or f.get("body") or "")[:20000]
            o = partners_mod.import_email(text, str(f.get("subject") or f.get("Subject") or ""), str(f.get("from") or f.get("From") or f.get("sender") or ""),
                                          str(f.get("partner") or request.query_params.get("partner") or ""))
        except (ShopError, ValueError, AttributeError) as e:
            shop.alert("question", f"A delivery-app email couldn't be read: {e} Check it by hand.", "/owner/orders")
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "order_id": o["id"], "total": o["total"], "duplicate": bool(o.get("duplicate"))}

    # ------------------------------------------------------------ accounts

    def _range(period: str, start: str, end: str) -> tuple[str, str]:
        try:
            if start and end:
                return shop.parse_date(start).isoformat(), shop.parse_date(end).isoformat()
        except ShopError:
            pass
        if period == "tax_year":
            return accounts.tax_year()
        if period == "all":
            first = db.q1("SELECT MIN(d) d FROM (SELECT MIN(date) d FROM orders UNION SELECT MIN(date) FROM expenses UNION SELECT MIN(date) FROM stock_moves UNION SELECT MIN(date) FROM journals)")
            return (first or {}).get("d") or shop.today(), shop.today()
        return shop.period(period if period in ("week", "month", "last_month", "year") else "month")

    @app.get("/owner/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, report: str = "income", period: str = "month", start: str = "", end: str = "", account: str = ""):
        need_owner(request)
        s_, e_ = _range(period, start, end)
        if report not in reports.REPORTS:
            report = "income"
        data = {}
        if report == "income":
            data = accounts.income_statement(s_, e_)
        elif report == "balance":
            data = accounts.balance_sheet(e_)
        elif report == "cashflow":
            data = accounts.cash_flow(s_, e_)
        elif report == "trial":
            data = accounts.trial_balance(e_)
        elif report == "tax":
            data = accounts.tax_summary()
        elif report == "journal":
            data = {"journals": accounts.journals(s_, e_)[::-1][:300]}
        else:
            data = {"chart": accounts.CHART}
        ledger = None
        if account in accounts.POSTING:
            ledger = accounts.ledger(account, s_, e_)
        return render(request, "owner/accounts.html", report=report, data=data, start=s_, end=e_, period=period, rep=reports.REPORTS,
                      acc=accounts, ledger=ledger, account=account, quick=accounts.QUICK, today=shop.today(),
                      manual=accounts.manual_journals(), postings=accounts.posting_accounts())

    @app.post("/owner/accounts/journal")
    async def accounts_journal(request: Request):
        need_owner(request)
        await check_csrf(request)
        f = await request.form()
        try:
            if f.get("kind") == "custom":
                lines = []
                for i in range(4):
                    if f.get(f"acct_{i}"):
                        lines.append((f.get(f"acct_{i}"), _float(f.get(f"dr_{i}"), 0) or 0, _float(f.get(f"cr_{i}"), 0) or 0))
                accounts.add_journal(f.get("date") or shop.today(), f.get("memo", ""), lines)
                msg = "Journal saved."
            else:
                msg = accounts.quick_journal(f.get("kind", ""), f.get("date") or shop.today(), _float(f.get("amount"), 0), f.get("memo", ""))
        except ShopError as e:
            return back("/owner/accounts?report=journal", error=str(e))
        return back("/owner/accounts?report=journal", msg)

    @app.post("/owner/accounts/journal/{jid}/delete")
    async def accounts_journal_delete(request: Request, jid: int):
        need_owner(request)
        await check_csrf(request)
        try:
            accounts.delete_journal(jid)
        except ShopError as e:
            return back("/owner/accounts?report=journal", error=str(e))
        return back("/owner/accounts?report=journal", "Journal deleted.")

    @app.get("/owner/accounts/export/{kind}.{fmt}")
    def accounts_export(request: Request, kind: str, fmt: str, period: str = "month", start: str = "", end: str = ""):
        need_owner(request)
        if kind != "all" and kind not in reports.REPORTS or fmt not in ("xlsx", "pdf"):
            raise HTTPException(404, "No such report.")
        s_, e_ = _range(period, start, end)
        data = reports.excel(kind, s_, e_) if fmt == "xlsx" else reports.pdf(kind, s_, e_)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "application/pdf"
        fname = f"pakhtoon-flavours-{'accounts' if kind == 'all' else kind}-{e_}.{fmt}"
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    return app
