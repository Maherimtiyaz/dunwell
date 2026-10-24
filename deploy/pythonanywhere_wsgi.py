"""PythonAnywhere WSGI configuration for the Dunwell service.

PythonAnywhere does not run your own socket server, so ``python -m
dunwell.serve`` is not how it works there. Instead it imports a WSGI callable.
Copy this file into the WSGI config it generates for you, normally:

    /var/www/<your-username>_pythonanywhere_com_wsgi.py

Reach it from the **Web** tab: the "WSGI configuration file" line is a link.
Then press **Reload**. Edits take effect only after a reload.

Before the first reload, on a **Bash console**:

    mkvirtualenv --python=/usr/bin/python3.11 dunwell
    git clone https://github.com/Maherimtiyaz/dunwell.git ~/dunwell

Free accounts get a persistent disk, which is what makes this a good home for
the waitlist: the SQLite file survives restarts and redeploys, unlike most
free tiers.
"""

import os
import sys

# --------------------------------------------------------------------- #
# 1. Where the repository lives
# --------------------------------------------------------------------- #

PROJECT = os.path.expanduser("~/dunwell")
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# --------------------------------------------------------------------- #
# 2. Configuration
#
# Set these BEFORE importing the application: it reads the environment once,
# when it is first built.
# --------------------------------------------------------------------- #

# The database. Keep it on the persistent disk, outside the checkout is fine
# too, but this path survives a `git pull`.
DATA_DIR = os.path.join(PROJECT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("DUNWELL_DB", os.path.join(DATA_DIR, "dunwell.db"))

# The landing page is hosted on GitHub Pages and posts sign-ups here, so this
# exact origin must be allowed. No trailing slash. Add a custom domain later
# with a comma, e.g. "https://dunwell.app,https://www.dunwell.app".
os.environ.setdefault(
    "DUNWELL_CORS_ORIGINS", "https://maherimtiyaz.github.io"
)

# Customer-visible copy. These strings appear in every drafted email, so they
# must be real before you send anything to a customer.
os.environ.setdefault("DUNWELL_COMPANY_NAME", "Northwind Studio")
os.environ.setdefault("DUNWELL_REPLY_TO", "billing@northwind.example")
os.environ.setdefault("DUNWELL_PAYMENT_LINK", "https://pay.northwind.example/inv")

# Leave mail in dry-run mode until real SMTP credentials exist. In dry-run
# nothing is sent; drafts are recorded and visible in the dashboard, which is
# the right default for a waitlist-only deployment.
# os.environ["DUNWELL_MAIL_MODE"] = "smtp"
# os.environ["SMTP_HOST"] = "smtp.example.com"
# os.environ["SMTP_PORT"] = "587"
# os.environ["SMTP_USER"] = "you@example.com"
# os.environ["SMTP_PASSWORD"] = "app-password-here"

# --------------------------------------------------------------------- #
# 3. The callable
# --------------------------------------------------------------------- #

from dunwell.wsgi import application  # noqa: E402
