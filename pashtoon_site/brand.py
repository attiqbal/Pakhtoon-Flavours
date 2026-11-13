"""The brand logo in every size the site needs: header mark, app icons, favicon and the link-preview picture
(the thumbnail WhatsApp and other apps show). The shipped logo lives in static/brand; when the owner uploads a new
one in Settings the same set is made again in the uploads folder and served from /brand/ instead."""
import os

from PIL import Image

from . import config

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static", "brand")
CREAM = (255, 244, 236)                                       # the site's linen background
FILES = {"logo.jpg": "image/jpeg", "logo-200.jpg": "image/jpeg", "og.jpg": "image/jpeg", "icon-512.png": "image/png",
         "icon-192.png": "image/png", "icon-180.png": "image/png", "favicon-32.png": "image/png"}


def custom_dir() -> str:
    return os.path.join(config.UPLOAD_DIR, "brand")


def path(name: str) -> str | None:
    """Where a brand file is: the owner's uploaded set if there is one, otherwise the shipped one."""
    if name not in FILES:
        return None
    p = os.path.join(custom_dir(), name)
    return p if os.path.exists(p) else os.path.join(STATIC_DIR, name)


def _square(im: Image.Image, size: int) -> Image.Image:
    """The logo fitted inside a cream square, so any shape of logo works as an icon."""
    canvas = Image.new("RGB", (size, size), CREAM)
    fit = im.copy()
    fit.thumbnail((size, size), Image.LANCZOS)
    canvas.paste(fit, ((size - fit.width) // 2, (size - fit.height) // 2))
    return canvas


def build(source: str, out_dir: str) -> None:
    """Make every brand file from one picture. Raises OSError or ValueError if it is not a usable image."""
    im = Image.open(source)
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, CREAM)
        flat.paste(rgba, mask=rgba.split()[-1])
        im = flat
    else:
        im = im.convert("RGB")
    if min(im.size) < 120:
        raise ValueError("That picture is too small for a logo. Use one at least 400 pixels wide.")
    os.makedirs(out_dir, exist_ok=True)
    _square(im, 1000).save(os.path.join(out_dir, "logo.jpg"), "JPEG", quality=84, optimize=True)
    _square(im, 200).save(os.path.join(out_dir, "logo-200.jpg"), "JPEG", quality=86, optimize=True)
    for size, name in ((512, "icon-512.png"), (192, "icon-192.png"), (180, "icon-180.png"), (32, "favicon-32.png")):
        _square(im, size).save(os.path.join(out_dir, name), "PNG", optimize=True)
    og = Image.new("RGB", (1200, 630), CREAM)                 # link preview: logo centred on cream, 1200 x 630
    mark = _square(im, 630)
    og.paste(mark, ((1200 - 630) // 2, 0))
    og.save(os.path.join(out_dir, "og.jpg"), "JPEG", quality=82, optimize=True)


def reset() -> None:
    """Back to the shipped logo."""
    d = custom_dir()
    for name in FILES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            os.remove(p)
