"""Automatic backups. The whole business is one SQLite file plus the uploaded photos, so a backup is a consistent
snapshot of the database and the uploads folder in one zip. Kept in data/backups (the newest 30), and optionally
emailed to her once a week. Download one any time from Settings."""
import io
import os
import sqlite3
import zipfile
from datetime import datetime

from . import config, db, notify
from .db import get_setting, set_setting


def _snapshot() -> bytes:
    """A consistent copy of the live database (SQLite's backup API is safe while the site is running)."""
    src = db.conn()
    tmp = os.path.join(config.BACKUP_DIR, ".snapshot.db")
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
    data = open(tmp, "rb").read()
    os.remove(tmp)
    return data


def make_zip() -> bytes:
    from . import shop
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pashtoon_site.db", _snapshot())
        z.writestr("README.txt", f"Backup of {shop.setting('shop_name')} made {shop.iso_now()}.\n"
                   "Restore: stop the site, put pashtoon_site.db in the data folder (replacing the old one) and unzip uploads/ beside it.\n")
        if os.path.isdir(config.UPLOAD_DIR):
            for fn in sorted(os.listdir(config.UPLOAD_DIR)):
                path = os.path.join(config.UPLOAD_DIR, fn)
                if os.path.isfile(path) and not fn.startswith("."):
                    z.write(path, f"uploads/{fn}")
    return buf.getvalue()


def list_backups() -> list[dict]:
    if not os.path.isdir(config.BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(config.BACKUP_DIR), reverse=True):
        if fn.startswith("pashtoon-") and fn.endswith(".zip"):
            out.append({"name": fn, "size": os.path.getsize(os.path.join(config.BACKUP_DIR, fn))})
    return out


def run(now: datetime | None = None) -> str:
    """Make a backup now, keep only the newest few, and return its file name."""
    from . import shop
    now = now or shop.now()
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    name = f"pashtoon-{now.strftime('%Y%m%d-%H%M%S')}.zip"
    with open(os.path.join(config.BACKUP_DIR, name), "wb") as fh:
        fh.write(make_zip())
    for old in list_backups()[config.BACKUP_KEEP:]:
        os.remove(os.path.join(config.BACKUP_DIR, old["name"]))
    set_setting("last_backup", now.isoformat(timespec="seconds"))
    return name


def last_backup_age_hours() -> float | None:
    from . import shop
    last = get_setting("last_backup")
    if not last:
        return None
    return (shop.now() - datetime.fromisoformat(last)).total_seconds() / 3600


def maybe_run() -> str | None:
    """Called every minute by the background loop: one backup a day, and a weekly email copy if she wants it."""
    age = last_backup_age_hours()
    if age is not None and age < 24:
        return None
    name = run()
    from . import shop
    if shop.setting("backup_email") and shop.now().weekday() == 6 and notify.smtp_ready() and shop.setting("owner_email"):
        path = os.path.join(config.BACKUP_DIR, name)
        ok, detail = notify.send_email(f"{shop.setting('shop_name')} weekly backup", "Your weekly backup is attached. Keep it somewhere safe.",
                                       attach=(name, open(path, "rb").read()))
        notify._log("email", ok, "backup: " + detail, "weekly backup")
    return name
