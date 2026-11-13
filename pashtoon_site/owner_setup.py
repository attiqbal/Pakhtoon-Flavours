"""Reset the owner's login from the command line, for when she has forgotten her password.

    python -m pashtoon_site.owner_setup

Puts the login back to the owner email and starter password in menu_data.py. Normally nothing needs running: the
owner account is made automatically the first time the site starts."""
import sys

from . import db, web


def main() -> int:
    db.init()
    for key in ("owner_login_email", "owner_password_hash"):
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    web.ensure_owner()
    print(f"Owner login reset: {db.get_setting('owner_login_email')} with the starter password. Change it in the dashboard under Password.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
