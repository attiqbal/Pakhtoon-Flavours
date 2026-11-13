"""Create or reset the website owner from the command line.

    OWNER_EMAIL=owner@example.com OWNER_PASSWORD='temporary-password' python -m pashtoon_site.owner_setup

Reads the email and temporary password from the environment (or .env, loaded by start.sh), never from the code. The
owner must choose a new password at the first login. Run it again any time to reset a forgotten password."""
import sys

import bcrypt

from . import config, db


def main() -> int:
    email, password = config.OWNER_EMAIL, config.OWNER_PASSWORD
    if not email or "@" not in email:
        print("Set OWNER_EMAIL to the owner's email address.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Set OWNER_PASSWORD to a temporary password of at least 8 characters.", file=sys.stderr)
        return 1
    db.init()
    db.set_setting("owner_login_email", email)
    if not db.get_setting("owner_email"):
        db.set_setting("owner_email", email)        # order alerts go here until she picks another
    db.set_setting("owner_password_hash", bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())
    db.set_setting("owner_must_change_password", True)
    print(f"Owner set to {email}. They will be asked to choose a new password at first login.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
