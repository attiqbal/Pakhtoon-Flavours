"""Shareable menu image (PNG, PDF) and QR code, drawn from the live menu so a price change shows up the next time.

Style follows her printed menu: warm cream, dark headings, red prices, her dish photos. Fonts are the Bitstream Vera
fonts that ship with reportlab, so nothing extra needs installing on the server."""
import io
import os

import segno
from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab import __file__ as _rl_file
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from . import brand, config, shop

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.path.dirname(_rl_file), "fonts")
SITE_FONTS = os.path.join(HERE, "static", "fonts")
W = 1654                       # A4 width at 200 dpi
MIN_H = 2339                   # A4 height at 200 dpi
# The site's colours: warm cream type on a walnut-dark page, dashed cork lines, ember orange for prices and labels.
INK = (255, 237, 215)
RED = TEAL = ORANGE = (220, 80, 0)
MUTED = (216, 198, 176)
LINE = (108, 95, 81)
PAGE = (16, 9, 4)
LOGO = 300                     # logo size at the top of the menu picture
M = 100                        # side margin
PHOTO = (240, 200)
PRICE_W = 132


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """The site's font (Inter Tight, bundled, open licence): r = regular, b = semibold, d and i = medium for headings."""
    f = ImageFont.truetype(os.path.join(SITE_FONTS, "InterTight.ttf"), size)
    try:
        f.set_variation_by_axes([{"r": 400, "b": 600, "d": 500, "i": 500}[kind]])
    except OSError:                                           # an old Pillow without variable fonts: regular weight
        pass
    return f


def public_url() -> str:
    """The address the QR code and the menu image point at: the one-page menu."""
    return config.BASE_URL + "/m"


def photo_path(img: str | None) -> str | None:
    if not img:
        return None
    if img.startswith("/media/"):
        p = os.path.join(config.UPLOAD_DIR, os.path.basename(img))
    elif img.startswith("/static/"):
        p = os.path.join(HERE, "static", img[len("/static/"):])
    elif img.startswith("/"):
        return None
    else:
        p = os.path.join(HERE, "static", "img", img)
    return p if os.path.exists(p) else None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _price(x: float) -> str:
    return shop.money(x)


def qr_png(url: str | None = None, scale: int = 12, dark: str = "#100904") -> bytes:
    buf = io.BytesIO()
    segno.make(url or public_url(), error="m").save(buf, kind="png", scale=scale, border=3, dark=dark, light="white")
    return buf.getvalue()


def qr_svg(url: str | None = None) -> bytes:
    buf = io.BytesIO()
    segno.make(url or public_url(), error="m").save(buf, kind="svg", scale=10, border=3, dark="#100904", light="white", xmldecl=False)
    return buf.getvalue()


def _rounded_photo(path: str, size: tuple[int, int]) -> Image.Image:
    im = ImageOps.fit(Image.open(path).convert("RGB"), size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=18, fill=255)
    im.putalpha(mask)
    return im


def _cards_and_lists() -> tuple[list[dict], list[tuple[str, list[tuple[str, str]]]]]:
    """Dishes with a photo get a card; everything else is a compact list under its section name."""
    cards, lists = [], []
    for key, label, items in shop.menu_by_category():
        rows = []
        for it in items:
            if it["enquiry"]:
                rows.append(("Weekly plans", "ask us"))
            elif path := photo_path(it["image"]):
                cards.append({**it, "photo": path})
            else:
                for o in it["options"]:
                    nm = it["name"] if len(it["options"]) == 1 and o["label"] in ("Portion", "Each", "Pot") else f"{it['name']} · {o['label']}"
                    rows.append((nm, _price(o["price"])))
        if rows:
            lists.append((label.split(" · ")[0], rows))
    return cards, lists


def _card_height(draw, it, text_w) -> int:
    h = 0
    if it["notice_hours"] > 24:
        h += 36
    h += 8 + 54 * len(_wrap(draw, it["name"].upper(), _font("d", 46), text_w))
    h += sum(34 * len(_wrap(draw, o["label"], _font("b", 25), text_w - PRICE_W)) + 6 for o in it["options"])
    h += 34 * len(_wrap(draw, it["description"], _font("r", 24), text_w)) + 14
    return max(h, PHOTO[1])


def render_png() -> bytes:
    s = shop.settings()
    cards, lists = _cards_and_lists()
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    col_w = (W - 2 * M - 70) // 2
    text_w = col_w - PHOTO[0] - 24

    # two columns of cards, row by row
    heights = [_card_height(probe, c, text_w) for c in cards]
    rows_h = [max(heights[i:i + 2]) + 50 for i in range(0, len(heights), 2)]
    list_cols = [[], []]
    total_rows = sum(len(r) + 2 for _, r in lists)
    run = 0
    for head, rows in lists:
        list_cols[0 if run < total_rows / 2 else 1].append((head, rows))
        run += len(rows) + 2
    list_h = max((sum(48 + 44 * len(r) + 24 for _, r in col) for col in list_cols), default=0)

    info = [f"Pre-orders only, at least 24 hours in advance (48 hours for Raan Pulao)."]
    info.append(f"Collection: {s['kitchen_postcode']}" + (f" ({s['area']})" if s.get("area") else ""))
    if s["offers_delivery"]:
        info.append("Delivery: " + shop.delivery_bands_text().replace("\n", " · "))
    else:
        info.append("Collection only for now.")
    info.append(f"WhatsApp: {s['whatsapp']}")
    info_lines = []
    for t in info:
        info_lines += _wrap(probe, t, _font("r", 26), W // 2 - M - 20)

    head_h = 430 + LOGO + 10
    foot_h = 90 + 44 + 38 * len(info_lines) + 80
    foot_h = max(foot_h, 90 + 44 + 260 + 60)
    H = max(MIN_H, head_h + sum(rows_h) + (list_h + 40 if list_h else 0) + foot_h)

    im = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(im)
    px.rectangle((0, 0, W, H), fill=PAGE)
    px.rounded_rectangle((34, 34, W - 34, H - 34), radius=24, outline=LINE, width=2)
    top = 80
    logo_path = brand.path("logo.jpg")
    if logo_path and os.path.exists(logo_path):
        lg = Image.open(logo_path).convert("RGB").resize((LOGO, LOGO), Image.LANCZOS)
        mask = Image.new("L", (LOGO, LOGO), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, LOGO, LOGO), fill=255)
        im.paste(lg, ((W - LOGO) // 2, top), mask)
        px.ellipse(((W - LOGO) // 2, top, (W + LOGO) // 2, top + LOGO), outline=INK, width=3)
    off = LOGO + 10
    d = ImageDraw.Draw(im)

    def spaced(text, y, font, fill, gap):
        total = sum(d.textlength(ch, font=font) + gap for ch in text) - gap
        x = (W - total) / 2
        for ch in text:
            d.text((x, y), ch, font=font, fill=fill)
            x += d.textlength(ch, font=font) + gap

    spaced(s["tagline"].upper().replace(", HOME-COOKED TO ORDER", ""), 120 + off, _font("b", 26), TEAL, 9)
    title = s["shop_name"].upper()
    tf = _font("d", 132)
    while d.textlength(title, font=tf) > W - 2 * M and tf.size > 60:
        tf = _font("d", tf.size - 4)
    d.text(((W - d.textlength(title, font=tf)) / 2, 182 + off), title, font=tf, fill=INK)
    d.line([(M + 40, 360 + off), (W - M - 40, 360 + off)], fill=LINE, width=2)
    d.polygon([(W / 2, 342 + off), (W / 2 + 18, 360 + off), (W / 2, 378 + off), (W / 2 - 18, 360 + off)], fill=ORANGE)

    y = head_h - 30
    for r in range(0, len(cards), 2):
        for k, it in enumerate(cards[r:r + 2]):
            x0 = M + k * (col_w + 70)
            yy = y
            if it["notice_hours"] > 24:
                d.text((x0, yy), f"Special order · {it['notice_hours']} hours' notice", font=_font("b", 24), fill=RED)
                yy += 36
            nf = _font("d", 46)
            for ln in _wrap(d, it["name"].upper(), nf, text_w):
                d.text((x0, yy), ln, font=nf, fill=INK)
                yy += 54
            for o in it["options"]:
                d.text((x0, yy), _price(o["price"]), font=_font("b", 25), fill=RED)
                for ln in _wrap(d, o["label"], _font("b", 25), text_w - PRICE_W):
                    d.text((x0 + PRICE_W, yy), ln, font=_font("b", 25), fill=RED)
                    yy += 34
                yy += 6
            yy += 8
            for ln in _wrap(d, it["description"], _font("r", 24), text_w):
                d.text((x0, yy), ln, font=_font("r", 24), fill=MUTED)
                yy += 34
            ph = _rounded_photo(it["photo"], PHOTO)
            im.paste(ph, (x0 + col_w - PHOTO[0], y + 4), ph)
            d.rounded_rectangle((x0 + col_w - PHOTO[0], y + 4, x0 + col_w, y + 4 + PHOTO[1]), radius=18, outline=LINE, width=2)
        y += rows_h[r // 2]

    if lists:
        for ci, col in enumerate(list_cols):
            x0 = M + ci * (col_w + 70)
            yy = y
            for head, rows in col:
                d.text((x0, yy), head.upper(), font=_font("i", 34), fill=TEAL)
                yy += 60
                for name, price in rows:
                    lines = _wrap(d, name, _font("r", 28), col_w - 190)
                    for ln in lines:
                        d.text((x0 + 24, yy), ln, font=_font("r", 28), fill=INK)
                        yy += 36
                    yy -= 36 * len(lines)
                    d.text((x0 + col_w - d.textlength(price, font=_font("b", 28)), yy), price, font=_font("b", 28), fill=RED)
                    yy += 44 + 36 * (len(lines) - 1) - 6 + 6
                yy += 24
        y += list_h + 40

    fy = H - foot_h
    d.line([(M + 40, fy), (W - M - 40, fy)], fill=LINE, width=2)
    d.text((M, fy + 40), "ORDERS", font=_font("d", 40), fill=INK)
    yy = fy + 100
    for ln in info_lines:
        d.text((M, yy), ln, font=_font("r", 26), fill=INK)
        yy += 38
    rx = W // 2 + 70
    d.text((rx, fy + 40), "ORDER ONLINE", font=_font("d", 40), fill=INK)
    qr = Image.open(io.BytesIO(qr_png(scale=8))).convert("RGB").resize((230, 230), Image.NEAREST)
    im.paste(qr, (rx, fy + 100))
    d.text((rx + 260, fy + 110), "Scan to see the menu", font=_font("b", 26), fill=INK)
    d.text((rx + 260, fy + 148), "and order online", font=_font("b", 26), fill=INK)
    d.text((rx + 260, fy + 205), f"@{s['instagram']}", font=_font("b", 30), fill=RED)
    d.text((rx + 260, fy + 246), "Instagram & TikTok", font=_font("r", 24), fill=MUTED)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def render_pdf() -> bytes:
    """The same image on one page: A4 when it fits, otherwise a page of the same width and the right height."""
    png = render_png()
    w, h = Image.open(io.BytesIO(png)).size
    pw = A4[0]
    ph = pw * h / w
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(pw, max(ph, A4[1])), pageCompression=1)
    c.setTitle(f"{shop.setting('shop_name')} menu")
    c.drawImage(ImageReader(io.BytesIO(png)), 0, max(ph, A4[1]) - ph, width=pw, height=ph)
    c.showPage()
    c.save()
    return buf.getvalue()


def poster_pdf() -> bytes:
    """A4 'scan to order' sheet with a big QR code, to print and put on a counter, window or flyer."""
    s = shop.settings()
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{s['shop_name']} QR code")
    pw, ph = A4
    navy = (16 / 255, 9 / 255, 4 / 255)                       # walnut ink on cream paper: this sheet is for printing
    ember = (220 / 255, 80 / 255, 0)
    head = "Helvetica-Bold"
    try:
        pdfmetrics.registerFont(TTFont("InterTight", os.path.join(SITE_FONTS, "InterTight.ttf")))
        head = "InterTight"
    except Exception:                                         # the sheet still prints, in a plain font
        pass
    c.setFillColorRGB(1, 237 / 255, 215 / 255)
    c.rect(0, 0, pw, ph, fill=1, stroke=0)
    c.setStrokeColorRGB(*navy)
    c.setLineWidth(1.5)
    c.roundRect(24, 24, pw - 48, ph - 48, 22, fill=0, stroke=1)
    logo_path = brand.path("logo.jpg")
    if logo_path and os.path.exists(logo_path):
        c.saveState()                                         # the logo as a round badge, whatever its corners hold
        clip = c.beginPath()
        clip.circle(pw / 2, ph - 150, 92)
        c.clipPath(clip, stroke=0, fill=0)
        c.drawImage(ImageReader(logo_path), pw / 2 - 100, ph - 250, width=200, height=200)
        c.restoreState()
    c.setFillColorRGB(*navy)
    c.setFillColorRGB(*ember)
    c.setFont("Helvetica", 13)
    c.drawCentredString(pw / 2, ph - 278, s["tagline"].upper())
    c.setFillColorRGB(*navy)
    c.setFont(head, 30 if head != "Helvetica-Bold" else 26)
    c.drawCentredString(pw / 2, ph - 326, "SCAN TO SEE OUR MENU AND ORDER")
    qr = ImageReader(io.BytesIO(qr_png(scale=16)))
    c.drawImage(qr, pw / 2 - 170, ph - 690, width=340, height=340)
    c.setFont("Helvetica", 15)
    c.drawCentredString(pw / 2, ph - 715, public_url())
    c.setFont("Helvetica", 14)
    line = "Pre-orders, 24 hours' notice. " + (f"Delivery over {shop.money(shop.delivery_minimum())}; collection has no minimum." if s["offers_delivery"] and shop.delivery_minimum() else "Collection has no minimum order.")
    c.drawCentredString(pw / 2, ph - 740, line)
    c.drawCentredString(pw / 2, ph - 765, f"WhatsApp {s['whatsapp']} · Instagram @{s['instagram']}")
    c.showPage()
    c.save()
    return buf.getvalue()
