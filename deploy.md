# Deploying Dunwell

Two independent deployables, per the Option A decision:

| Piece | What it is | Repo |
| --- | --- | --- |
| **Dunwell service** | Dashboard + JSON API + optional landing. Python, standard library only. | `D:/dunwell` |
| **Landing page** | Static marketing site + waitlist API. Python, standard library only. | `D:/dunwell-landing` |

They can be deployed together or apart. The service **auto-detects** a landing
directory next to itself, so the simplest arrangement is one process serving
both. If you deploy them separately, the landing gets its own hostname and the
service runs API-only.

---

## Table of contents

1. [What actually needs to change before deploying](#1-what-actually-needs-to-change-before-deploying)
2. [Free-tier options at a glance](#2-free-tier-options-at-a-glance)
3. [Option A — PythonAnywhere (recommended, both apps, free)](#3-option-a--pythonanywhere-recommended-both-apps-free)
4. [Option B — Render (service, free)](#4-option-b--render-service-free)
5. [Option C — Railway (service, free, persistent disk)](#5-option-c--railway-service-free-persistent-disk)
6. [Option D — Koyeb (service, free)](#6-option-d--koyeb-service-free)
7. [Option E — GitHub Pages + Pages Functions (landing only)](#7-option-e--github-pages--pages-functions-landing-only)
8. [Option F — Netlify / Cloudflare Pages (landing only)](#8-option-f--netlify--cloudflare-pages-landing-only)
9. [Domain and DNS](#9-domain-and-dns)
10. [Email delivery that actually sends](#10-email-delivery-that-actually-sends)
11. [Production hardening checklist](#11-production-hardening-checklist)
12. [Distribution — packaging and sharing](#12-distribution--packaging-and-sharing)
13. [Verification after every deploy](#13-verification-after-every-deploy)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What actually needs to change before deploying

Two items. Both are small, and neither is optional for a public deploy.

> **Status: both are implemented.** The code below is what landed; it is
> documented here so the behaviour is auditable. The third item (a health
> endpoint) landed too.

### 1a. The service must listen on `PORT` and `0.0.0.0` — done

Every PaaS sets a `PORT` environment variable and expects the app to bind
`0.0.0.0` (not `127.0.0.1`) or it will return 502. `dunwell/serve.py` now
reads both from the environment:

```python
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_port())
```

`_env_port()` falls back to `8906` when `PORT` is missing or unparseable, so
a typo in the platform's env panel cannot stop the service from booting. An
explicit `--host` / `--port` still wins, so nothing existing breaks.

Then deploy with `python -m dunwell.serve --host 0.0.0.0 --port $PORT` — or
just set `HOST=0.0.0.0` and `PORT` in the platform's environment panel and use
the bare command.

> **Note:** `0.0.0.0` binding is already verified working locally. `PORT=0`
> (ask the OS for a free port) also works — `serve.py` reads the real port
> back from `server.server_address[1]`.

### 1b. Add `Secure` to the session cookie under HTTPS — done

The session cookie now carries `Secure` when the request arrived over HTTPS:

```
dunwell_session=<token>; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=<n>
```

TLS is terminated by the platform, so the only signal available is the header
the proxy sets. `dunwell/service.py` reads it:

```python
    def _is_secure(self, headers: dict[str, str]) -> bool:
        proto = headers.get("x-forwarded-proto", "")
        return proto.split(",")[0].strip().lower() == "https"
```

The flag is passed into all three cookie call sites (`_signup`, `_login`, and
`_logout`). `_clear_cookie` carries the same flag so the delete matches the
set. Without the header — a plain HTTP local run — the cookie is unchanged
and the app still works.

### 1c. A health endpoint — done

```
GET /health  ->  {"ok": true, "status": "up"}
```

It is public and side-effect free, so it is safe to point a platform health
check at it.

> **Why this matters:** Render's free tier answers `/robots.txt` itself and
> those requests never reach your app, so a keep-alive pinger pointed at
> `/robots.txt` will *not* wake the service. Point probes at a real route.

### 1d. Company defaults — done, and now configurable

The placeholder `Northwind Studio` and `pay.northwind.example` used to ship
in every draft body. They are now read from the environment, so you set the
real values in the platform's env panel rather than editing code:

```
DUNWELL_COMPANY_NAME=Acme Studio
DUNWELL_REPLY_TO=billing@acme.test
DUNWELL_SIGNATURE=Acme Studio - Billing
DUNWELL_PAYMENT_LINK=https://pay.acme.test/inv
```

Accounts can still override all four individually at signup; these are only
the fallback.


---

## 2. Free-tier options at a glance

Verified against current platform documentation (October 2026).

| Platform | Persistent disk | Outbound SMTP 587 | Custom domain | Best for |
| --- | --- | --- | --- | --- |
| **PythonAnywhere** | ✅ 512 MiB home dir | ⚠️ Gmail only (free) | ❌ free tier | **Both apps, one place** |
| **Render** | ❌ ephemeral | ❌ blocked | ✅ | Landing only, or demo service |
| **Railway** | ✅ 500 MB volume | ❌ blocked | ✅ | Service, when data must persist |
| **Koyeb** | ❌ ephemeral (free) | ✅ allowed | ✅ | Service, no persistence needed |
| **GitHub Pages** | n/a (static) | ❌ | ✅ | Landing page |
| **Netlify / Cloudflare Pages** | n/a (static) | ❌ | ✅ | Landing page |

**The two constraints that decide everything:**

1. **Dunwell stores state in a SQLite file.** `dunwell/store.py:135` opens it
   with `isolation_level=None` (autocommit), so every write is durable
   immediately — but the *file* only survives if the platform gives you a
   persistent disk. On Render, Koyeb, and most container platforms the
   filesystem is wiped on every deploy and restart. **You will lose accounts,
   ledgers, and nudge history.**
2. **Most free PaaS tiers block outbound SMTP.** Railway blocks it entirely;
   Render blocks 25/465/587. Dunwell's `SmtpMailer` uses `smtplib` over
   port 587, so it needs either an allowed port or an HTTP email API.

**Recommended pairing:**

- **Service** → Railway (persistent volume) or PythonAnywhere (persistent home dir).
- **Landing** → GitHub Pages / Netlify / Cloudflare Pages (free, fast, static).
- **Email** → an HTTP email API (Resend, Brevo, Postmark) rather than raw SMTP.
  This sidesteps the port-587 blocks entirely. See [section 10](#10-email-delivery-that-actually-sends).

**Cost summary:** both can run at **$0**. The only paid step you may want is a
domain name (~$10/year) — and even that is optional, since every platform
above provides a free subdomain.

---

## 3. Option A — PythonAnywhere (recommended, both apps, free)

PythonAnywhere is the only free option that gives you **a persistent
filesystem**, which Dunwell needs. It also lets you run the landing and the
service as two web apps under one account.

**Free tier limits:** 1 web app, 1 worker, 512 MiB disk, 100 CPU-seconds/day,
custom domain ❌, outbound internet allowlisted (HTTP/S only, plus Gmail SMTP).

### 3a. Create the account and a web app

1. Sign up at pythonanywhere.com.
2. Open the **Web** tab → **Add a new web app** → choose **Manual configuration**.
3. Select **Python 3.12**.
4. Set the **Source code** directory to something like
   `/home/<username>/dunwell` and the **Working directory** the same.
5. Click **Create web app**.

### 3b. Upload the code

Free accounts have no SSH, so use the web file browser or `git` from a console.
Open **Consoles** → new Bash console:

```bash
cd ~
git clone https://github.com/<you>/dunwell.git
cd dunwell
```

If the repo is private, use a deploy key or a personal access token:

```bash
git clone https://<token>@github.com/<you>/dunwell.git
```

### 3c. The web app command

In the web app's **WSGI configuration file**, PythonAnywhere expects a WSGI
callable. Dunwell is a `http.server` app, not WSGI, so point the config at a
tiny adapter that runs the server in a background thread and proxies to it —
or, simpler and more robust, use the **Bash console to run the process
directly** and point the web app at it.

**Simplest working approach** — run it in the console as a daemon:

```bash
cd ~/dunwell
nohup python -m dunwell.serve --host 127.0.0.1 --port 8906 --db ~/dunwell/data/dunwell.db \
  >> ~/dunwell/serve.log 2>&1 &
```

Then in the web app's WSGI file, proxy to that port:

```python
# /var/www/<username>_pythonanywhere_com_wsgi.py
import os
os.environ['HTTP_PROXY'] = ''

from http.server import SimpleHTTPRequestHandler
import urllib.request

PORT = 8906

def application(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    ...
```

> **Honest recommendation:** PythonAnywhere's WSGI model does not fit a
> `http.server` app cleanly, and the proxy shim is fiddly. **Use it for the
> landing page** (which is a natural static-file fit) and use **Railway or
> Render for the service**, where you control the process directly. The two
> together are a cleaner result than forcing one platform to do both.

### 3d. If you must run both on PythonAnywhere

Run the **service** in a console as a daemon (as above), and configure the
**web app** to proxy to it. Run the **landing** on the second free slot using
the same technique with `--port 8905 --static ~/dunwell-landing`.

### 3e. Persistence — the whole point

`~/dunwell/data/dunwell.db` lives in your home directory, which is
**persistent and backed up by PythonAnywhere**. Survives console restarts and
web app reloads.

Watch your quota — 512 MiB is tight. Clean up periodically:

```bash
rm -rf /tmp/* /tmp/.* 2>/dev/null
rm -rf ~/.cache/* 2>/dev/null
du -sh ~/* | sort -h | tail
```

---

## 4. Option B — Render (service, free)

Good for a **demo** service and for the landing. **Not suitable for the real
service** — the filesystem is ephemeral and SMTP is blocked.

### 4a. Create the web service

1. Sign in at render.com.
2. **New → Web Service** → connect your GitHub repo.
3. Settings:

| Field | Value |
| --- | --- |
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m dunwell.serve --host 0.0.0.0 --port $PORT --db /tmp/dunwell.db` |
| Health check path | `/api/waitlist/count` |

`requirements.txt` is effectively empty (standard library only), so the build
step is a no-op — that is correct, not a mistake.

### 4b. Environment variables

Render's **Environment** tab:

```
PORT            = 10000            (set automatically)
DUNWELL_DB      = /tmp/dunwell.db
DUNWELL_MAIL_MODE = dryrun
DUNWELL_STATIC_DIR = /opt/render/project/src/dunwell-landing
```

### 4c. Free-tier behaviour you must plan for

- Spins down after **15 minutes** idle; cold start takes **~1 minute**.
- **750 instance hours/month** per workspace — roughly one always-on service.
- **Ephemeral filesystem** — `/tmp/dunwell.db` is wiped on every deploy and
  restart. Accounts, ledgers, and nudge history **will not persist**.
- **Outbound SMTP blocked** (25/465/587). `DUNWELL_MAIL_MODE=smtp` will fail.
- Free Postgres expires 30 days after creation — don't rely on it.
- Free tier has no custom-domain cost; add a domain under **Settings → Custom
  Domains** if you have one.

**If you use Render for the landing only**, it is a perfect fit: deploy
`D:/dunwell-landing` as a **Static Site** with the build command empty and the
publish path `.`, or as a Web Service running `python server.py --host 0.0.0.0 --port $PORT`.

---

## 5. Option C — Railway (service, free — persistent disk)

Best free option for the **service** because it gives you a **persistent
volume**.

### 5a. Create the project

1. Sign in at railway.app.
2. **New Project → Deploy from GitHub repo** → pick `dunwell`.
3. Railway detects Python and uses Nixpacks.

### 5b. Add a volume (required for persistence)

1. Select the service → **Volumes** tab → **Add Volume**.
2. Mount path: `/data`.
3. **The volume must be mounted at the same path on every deploy** — Railway
   will not attach a volume to a new deployment automatically otherwise.

### 5c. Configure

**Settings → Deploy**:

| Field | Value |
| --- | --- |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m dunwell.serve --host 0.0.0.0 --port $PORT --db /data/dunwell.db` |

**Settings → Variables**:

```
DUNWELL_DB        = /data/dunwell.db
DUNWELL_MAIL_MODE = dryrun
DUNWELL_STATIC_DIR = /app/dunwell-landing
```

### 5d. Free-tier reality

- Trial gives **$5 credit**; the free plan gives **$1/month credit**.
- 500 MB volume on the free plan, 5 GB on Hobby.
- **All outbound SMTP is blocked** (25/465/587) on Free, Trial, and Hobby.
  Only Pro ($20/mo) enables it, and you must redeploy after upgrading.
  Railway's own guidance: use an HTTPS email API (Resend, SendGrid, Mailgun).
- One replica on free. SQLite handles concurrency fine, but two replicas
  writing the same volume file would be a problem — keep it at one.

### 5e. Add a custom domain

**Settings → Networking → Generate Domain** gives a free `*.railway.app`
hostname. For your own domain: **Custom Domains → Add Domain**, add the CNAME
Railway shows you at your DNS provider, then Railway provisions the TLS
certificate automatically.

---

## 6. Option D — Koyeb (service, free)

Koyeb is the only free tier here that **allows outbound SMTP on 587**, which
makes it attractive for real email. The catch: free instances are **ephemeral**
and cannot use volumes.

### 6a. Deploy

1. Sign in at koyeb.com.
2. **Create → Web Service** → GitHub repo.
3. Settings:

| Field | Value |
| --- | --- |
| Builder | Python (buildpack) or Dockerfile |
| Run command | `python -m dunwell.serve --host 0.0.0.0 --port $PORT --db /home/dunwell/dunwell.db` |
| Instance size | Free (512 MB, 0.1 vCPU, 2 GB SSD) |
| Region | `was` or `fra` (only these support volumes) |

### 6b. Environment

```
DUNWELL_DB        = /home/dunwell/dunwell.db
DUNWELL_MAIL_MODE = smtp
SMTP_HOST         = smtp.yourprovider.com
SMTP_PORT         = 587
SMTP_TLS          = 1
MAIL_FROM         = billing@yourdomain.com
```

### 6c. Free-tier reality

- **1 free instance per organization.**
- Scales to zero after **1 hour** idle — this breaks in-memory state and
  background jobs.
- **Free instances cannot use volumes.** Local SSD is ephemeral and is wiped
  weekly on reschedule. Treat it as a demo.
- **Port 25 is blocked; port 587 is allowed.** This is the one free tier where
  `SmtpMailer` works unmodified.
- No static outbound IPs on free; some mail providers block shared PaaS IPs.
  If sends fail but connect fine, switch to an HTTP email API.
- Free tier needs no credit card; a card is required for volume-backed regions.

---

## 7. Option E — GitHub Pages + Pages Functions (landing only)

The cheapest way to publish the marketing site, and it gets you a free
`username.github.io` address. **The waitlist form needs a server** — static
Pages cannot accept a POST, so you need a Pages Function.

### 7a. Publish the static site

```bash
cd D:/dunwell-landing
git remote add origin https://github.com/<you>/dunwell-landing.git
git push -u origin main
```

Then on GitHub: **Settings → Pages → Source: Deploy from a branch** →
branch `main`, folder `/ (root)` → **Save**.

Your site is live at `https://<you>.github.io/dunwell-landing/` in about a
minute.

> **The path problem:** Pages serves from a subpath, so `href="styles.css"`
> resolves to `/dunwell-landing/styles.css` — correct. But absolute paths like
> `href="/styles.css"` break. The current `index.html` uses relative paths
> (`styles.css`, `privacy.html`), so it works as-is. Verify after deploy.

### 7b. Add a Pages Function for the waitlist

Create `functions/api/waitlist.js` in `D:/dunwell-landing`:

```javascript
export async function onRequestPost({ request, env }) {
  let email, source = "landing";
  try {
    const body = await request.json();
    email = String(body.email || "").trim().toLowerCase();
    source = String(body.source || "landing").trim().slice(0, 64);
  } catch {
    return json({ ok: false, error: { message: "Bad request", code: "bad_request" } }, 400);
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) {
    return json({ ok: false, error: { message: "Please enter a valid email address.", code: "invalid_email" } }, 422);
  }
  await env.WAITLIST.add(email);
  return json({ ok: true, count: Number(await env.WAITLIST.count()) });
}

export async function onRequestGet() {
  return json({ ok: true, count: Number(await env.WAITLIST.count()) });
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
```

Then create `functions/_middleware.js` to rate-limit, and bind a **KV
namespace** named `WAITLIST` (Settings → Functions → KV bindings). GitHub
Pages Functions support KV with 1000 writes/day free — plenty for a waitlist.

**Important:** the landing's `index.html` reads the error as a plain string
(`data.error`), but Dunwell's API returns `error: {message, code}`. If you
reuse the GitHub Function shape above, update the landing's JS to read
`data.error.message` so validation errors show properly. See
[section 11](#11-production-hardening-checklist).

### 7c. Add a CNAME for a custom domain

Create `CNAME` in `D:/dunwell-landing` containing your domain, then point a
CNAME at `username.github.io`. GitHub provisions TLS automatically.

---

## 8. Option F — Netlify / Cloudflare Pages (landing only)

Both host static sites free with global CDN and automatic TLS.

### 8a. Netlify

1. **Add new site → Import an existing project** → pick the repo.
2. Build settings: **Build command** empty, **Publish directory** `.`.
3. Netlify serves the static files. The waitlist form needs a **Netlify
   Function**:

```javascript
// netlify/functions/waitlist.js
exports.handler = async (event) => {
  const { email, source = "landing" } = JSON.parse(event.body || "{}");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return { statusCode: 422, body: JSON.stringify({ ok: false, error: { message: "Invalid email", code: "invalid_email" } }) };
  }
  // store in a Netlify Blobs / KV / Supabase bucket
  return { statusCode: 200, body: JSON.stringify({ ok: true, count: 1 }) };
};
```

Then point the form at `/.netlify/functions/waitlist` in `index.html`.

### 8b. Cloudflare Pages

Same shape: connect the repo, build command empty, output directory `/`.
For the waitlist, use a **Pages Function** with a KV binding — Cloudflare KV
is generous on the free tier and reads are unmetered.

---

## 9. Domain and DNS

If you buy a domain, you need DNS records pointing at each service. You can
host the landing and the app on **different subdomains**:

| Record | Points to | Serves |
| --- | --- | --- |
| `dunwell.app` | your DNS provider | Landing |
| `app.dunwell.app` | Railway / Render / Koyeb hostname | Dashboard + API |
| `www.dunwell.app` | same as apex | Landing |

**Recommended split:** keep the marketing site on GitHub Pages / Netlify
(fast, static, free, never goes down) and put the app on a subdomain that
points at wherever the service lives. If the service later moves, you update
one DNS record and the landing is untouched.

**TLS** is automatic on every platform listed — Render, Railway, Koyeb, and
GitHub Pages all provision Let's Encrypt certificates for you.

---

## 10. Email delivery that actually sends

This is where most free-tier deployments quietly fail. Dunwell's mailer has
two modes (`dunwell/mailer.py`):

| `DUNWELL_MAIL_MODE` | Behaviour |
| --- | --- |
| `dryrun` (default) | Records the message, returns `dry_<hex>`. **Nothing is sent.** |
| `smtp` | Real delivery over `smtplib` on port 587 with STARTTLS. |

`build_mailer()` **falls back to dryrun if `SMTP_HOST` is missing** — a
deliberate fail-safe so a misconfiguration never silently drops mail. So if
you set `DUNWELL_MAIL_MODE=smtp` and forget `SMTP_HOST`, the service starts
happily and records instead of sending. **Check the startup banner**: it
prints `mailer dryrun` when it has fallen back.

### 10a. Which transport to use

| Provider | Transport | Free tier | Port | Notes |
| --- | --- | --- | --- | --- |
| **Resend** | HTTP API | 3,000/mo, 100/day | 443 | Recommended. API key, no SMTP setup. |
| **Brevo (Sendinblue)** | HTTP API | 300/day | 443 | Generous free tier. |
| **Postmark** | HTTP API | 100/mo | 443 | Very good deliverability. |
| **Gmail / Google Workspace** | SMTP 587 | — | 587 | Needs an App Password. 500/day cap. |
| **Mailgun** | SMTP 587 | 5,000/mo | 587 | Sandbox mode for testing. |
| **Amazon SES** | SMTP 587 | 3,000/mo | 587 | Cheapest at volume. |

**Recommendation:** use an **HTTP API** (Resend or Brevo) wherever the platform
blocks SMTP — Railway, Render, and PythonAnywhere-free all block raw SMTP but
allow HTTPS to the provider's API. It also avoids the shared-PaS-IP
deliverability problems that plague SMTP from Render and Koyeb.

### 10b. Using SMTP (Koyeb, or a paid tier anywhere)

```bash
DUNWELL_MAIL_MODE=smtp
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_TLS=1
SMTP_USER=resend
SMTP_PASSWORD=re_xxxxxxxxxxxx
MAIL_FROM=billing@yourdomain.com
```

`MAIL_FROM` must be a domain you have **verified** with the provider and have
SPF/DKIM records for. Sending from an unverified domain lands in spam or is
rejected outright.

### 10c. Using an HTTP API (recommended)

`SmtpMailer` speaks raw SMTP only. To use an HTTP provider, add a small
`ApiMailer` next to it in `dunwell/mailer.py`:

```python
@dataclass
class ApiMailer:
    """Delivery over an HTTPS email API, for platforms that block SMTP."""

    mode: str = "api"
    from_address: str = "billing@dunwell.app"

    def send(self, message: MailMessage) -> str:
        import json
        import urllib.request

        payload = json.dumps({
            "from": self.from_address,
            "to": [message.to],
            "subject": message.subject,
            "text": message.body,
        }).encode("utf-8")
        request = urllib.request.Request(
            os.environ.get("MAIL_API_URL", "https://api.resend.com/emails"),
            data=payload,
            headers={
                "Authorization": f"Bearer {os.environ.get('MAIL_API_KEY', '')}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        return f"api_{body.get('id', uuid.uuid4().hex[:16])}"
```

Then extend `build_mailer()` to return it when `DUNWELL_MAIL_MODE=api`. The
`Mailer` protocol means nothing else in the codebase changes.

### 10d. Deliverability essentials

- **Verify your sending domain** with the provider (SPF, DKIM, and ideally
  DMARC).
- Set `MAIL_FROM` to a real address at that domain — `billing@yourdomain.com`.
- Each account can override `reply_to`, `company_name`, `signature`, and
  `payment_link` at signup; the defaults are the placeholder
  `Northwind Studio` and `pay.northwind.example`. **Change these before going
  live** — a real customer receiving "Northwind Studio - Billing" will not
  know what it is.

---

## 11. Production hardening checklist

Run through this before pointing a real customer at it.

### Must do

- [ ] **Set `DUNWELL_MAIL_MODE`** and verify the startup banner shows `smtp`
      (or `api`), not `dryrun`.
- [ ] **Set `MAIL_FROM`** to a verified sender on your own domain.
- [ ] **Set the company defaults** — the placeholder `Northwind Studio` and
      `pay.northwind.example` ship in every draft body. They are now
      environment variables (section 1d), so set `DUNWELL_COMPANY_NAME`,
      `DUNWELL_REPLY_TO`, and `DUNWELL_PAYMENT_LINK` in the platform's env
      panel. No code edit needed.
- [ ] **Confirm persistence.** Sign up, restart the service, sign in again. If
      your account is gone, the filesystem is ephemeral — move to Railway or
      PythonAnywhere, or accept data loss explicitly.
- [x] **Add `Secure` to the session cookie** (section 1b) if serving over
      HTTPS. — *done; the flag is set when `X-Forwarded-Proto: https` is
      present*
- [x] **Bind `0.0.0.0` and read `PORT`** (section 1a). — *done; `HOST` and
      `PORT` are read from the environment*
- [ ] **Set the database path** to a persistent location, e.g. `/data/dunwell.db`.
- [ ] **Back up the SQLite file.** It is the entire system of record:
      ```bash
      cp /data/dunwell.db /data/backup-$(date +%F).db
      ```
- [ ] **Put it behind HTTPS.** Every platform listed terminates TLS for you.

### Should do

- [x] **Add a `/health` route** (section 1c) and point your platform's health
      check at it. — *done; `GET /health` returns `{ok, status}`*
- [x] **Fix the landing's error-message shape.** The landing JS read
      `data.error` as a string, but the Dunwell API returns
      `error: {message, code}`. — *done; the landing now reads both shapes,
      and the landing's own `server.py` was changed to return the Dunwell
      contract, so the two backends are interchangeable*
- [ ] **Set a real `DUNWELL_STATIC_DIR`** if deploying the landing separately.
- [ ] **Add a link from the landing to the dashboard** so early-access users can
      reach the app.
- [ ] **Confirm rate limits suit you.** The waitlist limiter is 10 requests
      per IP per 60 seconds (`dunwell/service.py`).


### Know the limits (already documented in the README)

The connectors are honest stubs, not production integrations:

1. **No pagination** — one page per call; larger ledgers are silently truncated.
2. **QuickBooks and Xero have no dispute status** — both honour a `DISPUTED:`
   prefix convention instead.
3. **QuickBooks `paid_on` is `None`.**
4. **Nudge history is not read from the source** — `contacts` comes from
   Dunwell's own store, so `cooldown` and `max_nudges` only fire on data
   Dunwell has sent itself.
5. **No write-back** — `record_contact()` raises `UnsupportedOperation`.
6. **No OAuth flow** — QuickBooks and Xero expect an access token in the
   environment; token refresh is not implemented.
7. **Missing due dates fall back to the issue date** and record a warning.

For a live ledger, wire up a real connector and re-test the policy before
sending anything to a customer.

---

## 12. Distribution — packaging and sharing

### 12a. The two ways to distribute

**As a hosted service** (sections 3–8) — users get a URL. Best for a
SaaS product. The landing drives signups; the dashboard is the app.

**As a self-hosted package** — users run it themselves. This works because
Dunwell is **standard library only**: no pip install, no virtualenv, no
dependency conflicts.

### 12b. Building a self-contained archive

```bash
cd D:/dunwell
git clone https://github.com/<you>/dunwell.git /tmp/dunwell-release
cd /tmp/dunwell-release

# Tag the release
git tag -a v1.0.0 -m "First public release"
```

Then zip the repo, excluding anything local:

```bash
cd /tmp
zip -r dunwell-v1.0.0.zip dunwell-release \
  -x "*/.git/*" "*/__pycache__/*" "*/.pytest_cache/*" \
     "*/out/*" "*.db" "*/.env"
```

**Verify the archive is self-contained** before you ship it:

```bash
unzip dunwell-v1.0.0.zip -d /tmp/verify
cd /tmp/verify/dunwell-release
python -m pytest tests -q
python -m dunwell --ledger-dir demo --as-of 2026-10-16 --out out
python -m dunwell.serve --port 8906
```

A customer should be able to run those three commands with nothing but a
Python install.

### 12c. One-command install for the user

Add this to the README:

```bash
git clone https://github.com/<you>/dunwell.git
cd dunwell
python -m dunwell.serve --port 8906
```

Then: dashboard at `http://127.0.0.1:8906/dashboard`, API at `/api/**`.

For Windows users, `python` may need to be `py`:

```powershell
py -m dunwell.serve --port 8906
```

### 12d. PyPI packaging (optional)

The app is not a library, so PyPI is optional. If you want it:

```bash
pip install build
```

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "dunwell"
version = "1.0.0"
description = "Grounded AI receivables agent: deterministic invoice follow-up drafts"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "Proprietary" }
dependencies = []

[project.scripts]
dunwell = "dunwell.cli:main"
dunwell-serve = "dunwell.serve:main"

[tool.setuptools]
packages = ["dunwell", "dunwell.connectors"]

[tool.setuptools.package-data]
dunwell = ["static/*.html"]
```

Build and check:

```bash
python -m build
pip install dist/dunwell-1.0.0-py3-none-any.whl
dunwell-serve --port 8906
```

> `package-data` matters — without it the dashboard HTML is missing from the
> wheel and `/dashboard` returns 404.

### 12e. Docker

A `Dockerfile` for platforms that expect one:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Runtime is standard library only; this is here for the test suite.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir .

# Mount a volume here so the SQLite file survives redeploys.
VOLUME ["/data"]
ENV DUNWELL_DB=/data/dunwell.db
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000
CMD ["python", "-m", "dunwell.serve"]
```

Build and run:

```bash
docker build -t dunwell .
docker run -d --name dunwell -p 8906:8000 \
  -v dunwell-data:/data \
  -e DUNWELL_MAIL_MODE=smtp \
  -e SMTP_HOST=smtp.resend.com \
  -e SMTP_USER=resend \
  -e SMTP_PASSWORD=$SMTP_PASSWORD \
  -e MAIL_FROM=billing@yourdomain.com \
  dunwell
```

**The `-v dunwell-data:/data` volume is not optional** — without it, every
recreate wipes the database.

### 12f. Distributing to people who should not have the source

If you want to ship a self-hosted copy without your source visible, a
Docker image is the cleanest form:

```bash
docker build -t ghcr.io/<you>/dunwell:1.0.0 .
docker push ghcr.io/<you>/dunwell:1.0.0
```

Users then run:

```bash
docker run -d -p 8906:8000 -v dunwell-data:/data ghcr.io/<you>/dunwell:1.0.0
```

### 12g. Piping the engine into other systems

The engine is usable without the service:

```python
from dunwell.agent import run
from dunwell.core import Company
from dunwell.io import parse_customers_csv, parse_invoices_csv
from datetime import date

customers = parse_customers_csv(open("customers.csv").read())
invoices = parse_invoices_csv(open("invoices.csv").read())
report = run(invoices, customers, date.today(), Company(name="Acme", reply_to="billing@acme.com"))

for action in report.drafts:
    print(action.invoice.number, action.draft.subject, action.draft.body)
```

This is the integration path for a cron job that mails you a report, or for
wiring Dunwell into a bigger system.

---

## 13. Verification after every deploy

Run these against the deployed URL. If any line fails, the deploy is not done.

```bash
BASE=https://app.yourdomain.com

# 1. Landing or API responds
curl -s -o /dev/null -w "%{http_code}\n" $BASE/

# 2. Dashboard serves
curl -s -o /dev/null -w "%{http_code}\n" $BASE/dashboard

# 3. Waitlist API responds
curl -s $BASE/api/waitlist/count

# 4. Auth is enforced
curl -s -o /dev/null -w "%{http_code}\n" $BASE/api/me      # expect 401

# 5. Sign up and get a session
curl -s -c /tmp/c.txt -X POST $BASE/api/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"smoke@test.com","password":"testpass123"}'

# 6. Import the demo ledger
curl -s -b /tmp/c.txt -X POST $BASE/api/ledger \
  -H "Content-Type: application/json" -d '{"demo":true}'

# 7. Report renders
curl -s -b /tmp/c.txt $BASE/api/report | head -c 200

# 8. Persistence: restart the service, then confirm the session survives
curl -s -b /tmp/c.txt $BASE/api/me
```

**Step 8 is the one people skip.** Restart the service (or wait for a
spin-down), then re-run step 8. If you get `401`, your filesystem is ephemeral
and your data is not durable. Fix that before onboarding anyone.

To check the mailer actually sends, approve one draft and confirm the
delivery id is **not** prefixed `dry_`:

```
{"ok": true, "delivery_id": "smtp_a1b2c3d4e5f6g7h8", ...}
```

A `dry_` prefix means the mailer fell back to dryrun — `SMTP_HOST` is missing
or unreachable.

---

## 14. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `502 Bad Gateway` | App bound to `127.0.0.1` | Use `--host 0.0.0.0` and `$PORT` |
| App starts, then dies | `PORT` not read | Set `PORT` in the platform env, or pass `--port $PORT` |
| Accounts vanish on restart | Ephemeral filesystem | Move to a platform with a volume, or mount one |
| Drafts never send | Fell back to dryrun | Set `SMTP_HOST`; check the startup banner says `smtp` |
| `SMTPRecipientsRefused` / timeout | Platform blocks port 587 | Switch to an HTTP email API (section 10c) |
| Landing page 404 at `/` | No static dir found | Set `DUNWELL_STATIC_DIR` to the landing folder |
| Landing styles 404 on Pages | Absolute asset paths | Use relative paths (current HTML already does) |
| `demo ledger is not bundled` | `demo/` missing from deploy | Include it in the build; don't exclude it |
| `500 internal error` | Check the service log | The handler swallows tracebacks by design |
| Waiting ~1 min for first request | Cold start (Render/Koyeb free) | Expected; use a paid tier if it matters |
| Waitlist count always 0 | Landing and service store separately | Pick one authoritative store (section 10 note below) |
| `no ledger imported yet` (400) | Normal before import | Import via `POST /api/ledger` |
| `this invoice is held` (409) | Cooldown or policy hold | Expected — check `/api/report` for the reason |

### A note on the split waitlist

Under the Option A split, the landing's `server.py` writes signups to
`D:/dunwell-landing/data/waitlist.csv`, while the Dunwell service writes to the
SQLite `waitlist` table. **If you deploy both, signups go to whichever service
the form posts to.** Decide which is authoritative and point the other at it,
or export one into the other:

```bash
# Export the service's SQLite waitlist to CSV
python - <<'PY'
import csv, sqlite3
conn = sqlite3.connect("/data/dunwell.db")
rows = conn.execute("SELECT email, source, created_at FROM waitlist").fetchall()
with open("data/waitlist.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["email", "source", "ts"])
    w.writerows(rows)
print(f"exported {len(rows)} rows")
PY
```

---

## Quick start summary

**If you want the fastest path to a live URL:**

1. Service → **Railway** with a `/data` volume (persistent SQLite, free).
2. Landing → **GitHub Pages** (free, static, fast).
3. Email → **Resend HTTP API** (bypasses SMTP blocks).
4. Apply **section 1a** (read `PORT`, bind `0.0.0.0`) and **section 11**
   (real `MAIL_FROM`, real company name, confirm persistence) before going live.
5. Run **section 13** against the deployed URL.
