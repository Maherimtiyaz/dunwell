# Dunwell — Security Audit (pre-sale / pre-Vercel)

**Date:** 2026-11-02
**Scope:** `D:\dunwell` (service, engine, connectors, deploy config) and
`D:\dunwell-landing` (static site, local waitlist server, Vercel config).
**Method:** manual code review of every route and SQL statement, git-history
scan of both repos, live socket tests against the service and landing server,
and a browser run of the dashboard to check for CSP violations. Fixes were
applied where the fix was unambiguous; no findings were left as "known issue"
without a compensating control.

---

## 1. Verdict

**No P0 (ship-stopping) vulnerability was found.** There is no SQL injection,
no cross-tenant data access, no stored/reflected XSS sink, no secret in either
repo's history, and no unauthenticated route that mutates another account's
data.

**It is not yet safe to take money, for configuration reasons, not code
reasons.** Three deploy-time items must be completed first:

| # | Blocker | Why it matters |
| --- | --- | --- |
| B1 | `config.js` → `window.DUNWELL_API_URL` is empty | The waitlist form visibly fails: "The waitlist is not connected yet." |
| B2 | `DUNWELL_CORS_ORIGINS` on the service still names GitHub Pages | A form on Vercel will be blocked by CORS until this is the Vercel origin. |
| B3 | The service is not deployed | Vercel cannot host it (no persistent disk for SQLite); the app URL does not exist yet. |

Everything below is either **fixed in this pass** or **accepted with a
documented reason**.

---

## 2. Findings summary

Severity: **P1** = exploitable or directly enables account/data compromise;
**P2** = hardening or defence-in-depth; **P3** = informational.

| ID | Sev | Area | Finding | Status |
| --- | --- | --- | --- | --- |
| F1 | P1 | Error handling | 500 responses embedded `str(exc)`, leaking filesystem paths and SQLite/OS text. Mailer failures leaked SMTP transport detail. | **Fixed** |
| F2 | P1 | Headers | No CSP, `X-Frame-Options`, HSTS, or `Permissions-Policy`; clickjacking and framing were possible. | **Fixed** |
| F3 | P1 | Caching | JSON API responses (ledger, drafts, account) had no `Cache-Control`; shared-browser/proxy caching could expose one user's data to the next. | **Fixed** |
| F4 | P1 | CSRF | Cookie-bearing writes did not check `Origin`; a `text/plain` "simple request" could skip the CORS preflight entirely. | **Fixed** |
| F5 | P1 | Input validation | Profile fields (`company_name`, `signature`, …) were unbounded and accepted CR/LF, allowing header/body injection into outbound drafts and oversized stored records. | **Fixed** |
| F6 | P1 | Auth | Password input had no upper bound; a multi-megabyte password is pure scrypt CPU/memory burn. | **Fixed** |
| F7 | P1 | Database | SQLite file was created world-readable on POSIX; deleted rows survived in free pages and could be recovered from a re-imported ledger. | **Fixed** |
| F8 | P1 | CSV export | Customer names / invoice numbers beginning with `= + - @` executed as formulas when the export was opened in Excel/Sheets. | **Fixed** |
| F9 | P1 | Enumeration | Duplicate-signup returned the email address in the error, turning signup into an account-existence oracle. | **Fixed** |
| F10 | P2 | Transport | `Secure` on the session cookie depended solely on `X-Forwarded-Proto`; if a proxy omitted it, the cookie went out without `Secure`. | **Fixed** |
| F11 | P2 | Server | Chunked request bodies are not decoded by `http.server`; a chunked request could desync a keep-alive connection. Junk `Content-Length` could raise an uncaught error. | **Fixed** |
| F12 | P2 | Fingerprinting | `Server:` header disclosed Python version and app version. | **Fixed** |
| F13 | P2 | Static serving | The landing server's type map also served `.md`, `.txt`, and `.json`, so `README.md` and any repo `.txt`/`.json` in the web root were downloadable. (`server.py` was not: `.py` was never in the map.) | **Fixed** |
| F14 | P2 | Rate limiting | Landing limiter map grew without bound under a spray of unique IPs. | **Fixed** |
| F15 | P2 | CSP | `script-src` still needs `'unsafe-inline'` because the dashboard and landing are single-file inline scripts. | **Accepted** (no `innerHTML` sink; see §4) |
| F16 | P2 | Rate limiting | Limiters are in-process and per-worker; a multi-worker deploy multiplies the effective limit. | **Accepted** (single worker) |
| F17 | P2 | Account lifecycle | No password reset, no email verification, no MFA. | **Accepted** (pre-beta) |
| F18 | P3 | Connectors | `ConnectorError` includes the full request URL. Tokens are headers, not query parameters, so no credential is in that string. | **Accepted** |
| F19 | P3 | Waitlist | No CAPTCHA; a distributed botnet can still fill the CSV within the per-IP limit. | **Accepted** (low value target) |
| F20 | P3 | Backup | SQLite is a single file; backups/retention are operator responsibilities. | **Documented** |

---

## 3. P1 findings in detail

### F1 — Exception text leaked to clients

`Application.handle` caught `Exception` and returned:

```python
return error_response(500, f"internal error: {exc}", "server_error")
```

Any triggerable fault (malformed ledger, locked database, mail transport
failure) returned the exception string. That is an information-disclosure
primitive: absolute paths, SQL fragments, hostnames, and library internals.

**Fix** (`dunwell/service.py`): log the traceback server-side with
`LOG.exception(...)` and return the fixed string `"internal error"`. The mailer
call is wrapped separately and returns `502 send_failed` without the transport
message. Ledger sends are only recorded after a successful send, so a failed
send is retryable and never creates a phantom nudge.

**Regression test:** `test_internal_errors_do_not_echo_exception_text`.

### F2 — Missing browser security headers

Only `X-Content-Type-Options` and `Referrer-Policy` were sent.

**Fix:** added `X-Frame-Options: DENY`, `Permissions-Policy` (camera, mic,
geolocation, payment all denied), and a CSP with `frame-ancestors 'none'`,
`base-uri 'none'`, `form-action 'self'`, `object-src 'none'`. HSTS is added
only when the request arrived over HTTPS, so local HTTP development is
unaffected.

The landing equivalent is declared twice: in `dunwell-landing/server.py` (local
preview) and in `dunwell-landing/vercel.json` (production). Vercel does not run
`server.py`; the JSON file is the one that applies there.

**Verified over a real socket** — `GET /health` returned all six headers.

### F3 — Cacheable API responses

No `Cache-Control` was set on JSON. A ledger response is per-user data; a
shared machine or an intermediary cache could replay it to another session.

**Fix:** `json_response()` now sets `Cache-Control: no-store` by default.

**Regression test:** `test_api_responses_are_not_cached`.

### F4 — CSRF on cookie-bearing writes

`SameSite=Lax` blocks most cross-site POSTs, but it is a browser behaviour, not
an authorisation check. There was also a classic bypass: a cross-origin
`text/plain` POST is a "simple request", so the browser sends it **without** a
preflight, and `_json_body` would still parse it as JSON.

**Fix (two layers):**

1. Any `/api/` write with an `Origin` header must use
   `Content-Type: application/json` (else `415`). Cross-origin JSON forces a
   preflight, which the allowlist answers only for permitted origins.
2. Any cookie-bearing `POST`/`PUT`/`PATCH`/`DELETE` must have an `Origin` that
   is either the request's own host or an explicitly allowed CORS origin
   (else `403 forbidden_origin`).

Non-browser clients (curl, tests) send no `Origin` and are still allowed —
they cannot be tricked into carrying a victim's cookie.

**Regression tests:** `test_cross_origin_write_with_a_cookie_is_refused`,
`test_same_origin_write_with_a_cookie_is_allowed`,
`test_cross_origin_non_json_is_refused_before_parsing`.

### F5 — Unbounded / unsanitised profile fields

`company_name`, `signature`, `reply_to`, and `payment_link` were passed
straight from JSON into the database and into outbound draft headers/bodies.
`company_name: "Acme\r\nBcc: evil@x"` is a header-injection attempt; a 1 MB
string is a storage/CPU problem.

**Fix:**

- `_clean_text()` collapses CR/LF, strips control characters, trims, and caps
  length (company 120, signature 200, reply-to 254, payment link 500).
- `reply_to` must parse as an email address.
- `payment_link` must start with `http://` or `https://`.

**Regression tests:** `test_profile_fields_cannot_inject_a_header`,
`test_overlong_passwords_are_refused`.

### F6 — Unbounded password length

scrypt cost is driven by its parameters, but the password is still copied and
encoded, and `hashlib.scrypt` accepts the buffer. A 100 MB password is a cheap
way to pin a CPU core.

**Fix:** reject passwords over 1024 characters before hashing.
`_SCRYPT_N` was also raised from `2**14` to `2**15` (32 MiB working set) — a
deliberate middle ground for a 512 MiB host. Each hash stores its own
parameters, so old accounts still verify.

### F7 — Database permissions and deleted-row recovery

On POSIX, SQLite creates the file with the process umask, often `0644`. The
file contains password hashes, session digests, waitlist emails, and customer
ledgers.

**Fix** (`dunwell/store.py`):

- `chmod 0600` on open (POSIX only; skipped on Windows and `:memory:`).
- `PRAGMA secure_delete = ON` so deleted rows are overwritten rather than left
  in free pages.
- `PRAGMA busy_timeout = 5000` so a concurrent backup/CLI waits instead of
  failing.
- Duplicate-account errors no longer embed the address.

### F8 — CSV formula injection

`decisions.csv` is opened by the operator in a spreadsheet. A customer name of
`=HYPERLINK("http://evil","click")` becomes a live formula. The same applies to
the landing waitlist CSV, whose `email` field is attacker-supplied.

**Fix:** `_csv_safe()` prefixes any value starting with `= + - @`, tab, or CR
with a single quote, in both `dunwell/io.py` and `dunwell-landing/server.py`.
The landing reader reverses the guard (`_csv_unsafe`) before deduplicating, so
a quoted address is not re-added on the next signup.

**Regression test:** `test_csv_export_neutralises_formulas`.

### F9 — Signup account-existence oracle

`409 account already exists: victim@example.com` confirmed whether an address
was registered, and echoed it back into logs and error UIs.

**Fix:** the message is now `"an account with this email already exists"`
without the address, both in the service and in `Store.create_account`.

**Regression test:** `test_duplicate_signup_does_not_echo_the_address`.

### F10 — `Secure` cookie behind a proxy

`_is_secure()` returned true only for `X-Forwarded-Proto: https`. A proxy that
forgets that header silently downgrades the cookie to non-`Secure`.

**Fix:** fall back to the host name. `localhost`, `127.0.0.1`, `::1`, and an
empty host are treated as local HTTP; anything else is treated as public
HTTPS. A wrong guess against a LAN HTTP test only breaks that test; the
opposite mistake would leak the cookie.

---

## 4. Checked and clean

| Requested check | Result |
| --- | --- |
| **SQL injection** | Every user value is a bound parameter. The only dynamic SQL is `UPDATE accounts SET {cols}` where `cols` is built from a fixed allowlist (`company_name`, `reply_to`, `signature`, `payment_link`); unknown field names raise before the query. |
| **Tenant isolation** | Every ledger/nudge/session query is scoped by `account_id`. Tested end-to-end: account B sees `invoices: 0`, gets `400 empty_ledger` on report, and `404 unknown_invoice` when trying to send account A's draft. (`test_one_account_cannot_read_or_touch_another_ledger`.) |
| **XSS** | Dashboard renders exclusively through `textContent`; no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, or `new Function` in either repo. No `javascript:` URLs. The landing writes the error message with `textContent`. |
| **Authentication** | scrypt (N=2^15, r=8, p=1, 16-byte salt, 32-byte key), constant-time compare, per-hash parameters. Sessions are 256-bit `secrets.token_urlsafe`; only the SHA-256 digest is stored. Cookie is `HttpOnly; SameSite=Lax; Path=/`, `Secure` on public hosts, 14-day TTL. |
| **Admin routes** | There is no HTTP admin route. Account creation is `python -m dunwell.admin` on the host shell; the password is prompted with `getpass` by default. A `--password` flag exists for scripted provisioning, but it lands in shell history and the process list — use the prompt on a real host. |
| **Signup gate** | `DUNWELL_SIGNUP_MODE` defaults to `closed`; `/api/signup` returns `403` unless it is exactly `open`. |
| **Rate limiting** | Waitlist 10/min/IP. Authentication 10/min per IP **and** 10/min per email, with separate buckets for signup and login so creating an account does not lock the new user out. Service body cap 1,000,000 bytes (`413`). Landing 10/min/IP with a 16 KiB body cap, and its limiter now prunes stale keys at 10k entries. |
| **CORS** | Explicit allowlist only, exact match, no wildcard. `Access-Control-Allow-Origin` is echoed only for allowed origins, with `Vary: Origin`. Preflight restricted to `POST` on `/api/waitlist*`. Default is no cross-origin access. |
| **Security headers** | Service and landing both send CSP, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and HSTS over HTTPS. Dashboard load showed one console error, the expected `401` on `/api/me`; **no CSP violations**. |
| **Debug mode** | No framework, no debug flag, no traceback in responses. `LOG.exception` writes server-side only. |
| **Dependencies** | Standard library only for both apps (`requirements.txt` is intentionally empty). Nothing to pin, nothing to audit for CVEs, no supply-chain surface. |
| **Unused packages** | None installed. Connectors are opt-in and used by the CLI, not loaded by the HTTP service. |
| **Exposed files** | Landing: static allowlist (`.html`, `.css`, `.js`, `.svg`, `.png`, `.ico`), path resolved and checked with `is_relative_to`. `/server.py`, `/README.md`, `/data/waitlist.csv` all return `404`. Service: basename allowlist plus parent-directory check. |
| **Secrets in git history** | Scanned both repos (`git log -p`, all branches) for `sk_live`/`sk_test`, AWS keys, private-key headers, Stripe/Revolut/GitHub/Slack token shapes, and `password = "..."`. **No matches.** No `.env`, `*.db`, or `data/` was ever committed. `.gitignore` covers `.env`, `*.db`, `out/`, and the landing `data/`. |
| **Database** | All writes parameterised, foreign keys on, `secure_delete` on, `0600` on POSIX, one connection guarded by an `RLock`, `busy_timeout` set. |
| **Password hashing** | scrypt, not a fast hash; salted per account; algorithm parameters stored with the hash; constant-time verification. |

---

## 5. Residual risks (accepted)

These are not regressions; each has a reason and a compensating control.

1. **CSP allows inline script.** The dashboard and landing are single self-
   contained HTML files. Removing `'unsafe-inline'` requires extracting the
   scripts into files and adding nonces. The real XSS defence holds: there is
   no HTML sink, and all dynamic text goes through `textContent`. Revisit if a
   second script ever loads.
2. **Rate limiting is in-process.** On a single worker (the documented
   PythonAnywhere/Railway shape) it is exact. If the service is ever scaled to
   multiple processes, put the limiter in Redis or at the edge.
3. **No password reset / email verification / MFA.** Acceptable for a
   concierge beta with hand-created accounts. Required before self-serve scale:
   a reset flow needs single-use, expiring, hashed tokens and the same
   no-enumeration messaging as signup.
4. **`X-Forwarded-For` trust depends on `DUNWELL_TRUSTED_PROXY_HOPS`.** Set it
   to the real number of proxies in front of the app (`1` on PythonAnywhere /
   Railway). Too high and a client can spoof its address to bypass per-IP
   limits.
5. **Waitlist has no CAPTCHA.** A botnet can fill the CSV within the limit; the
   data is low-value and the operator reads it manually. Add hCaptcha/Turnstile
   only if it becomes a problem.
6. **scrypt memory cost.** N=2^15 uses 32 MiB per concurrent hash. On a 512 MiB
   host, several simultaneous logins can pressure memory. The 5/min auth limit
   bounds this.
7. **Connector errors include the URL.** Tokens are sent in headers, never in
   query strings, so the URL contains no credential. If a future connector puts
   a token in a query parameter, redact it before raising.
8. **SQLite backups are manual.** `secure_delete` protects deleted rows, but
   the live file plus its `-wal`/`-journal` must be copied atomically (or with
   `VACUUM INTO`) for a consistent backup. Set retention and deletion policy
   before onboarding real customer ledgers.
9. **Preview deployments are rejected by CORS.** Vercel previews get a random
   origin per build. This is intentional: only the production origin is
   allowed. Test the form on the production domain.

---

## 6. Production environment variables

Set these on the **service host** (PythonAnywhere/Railway), not on Vercel.
Vercel hosts only static files and needs no secrets.

| Variable | Required value | Secret? | Notes |
| --- | --- | --- | --- |
| `DUNWELL_DB` | `/home/<user>/dunwell.db` | No | Must be on the persistent disk. Never inside the repo. |
| `DUNWELL_SIGNUP_MODE` | `closed` or `open` | No | `closed` for concierge; `open` only when you accept public signups. |
| `DUNWELL_CORS_ORIGINS` | `https://<vercel-production-domain>` | No | Exact origin, no trailing slash. Add the custom domain later, comma-separated. Never `*`. |
| `DUNWELL_TRUSTED_PROXY_HOPS` | `1` | No | `1` for PythonAnywhere/Railway's single proxy. |
| `DUNWELL_COMPANY_NAME` | Your real trading name | No | Customer-visible in drafts. |
| `DUNWELL_REPLY_TO` | `billing@yourdomain.com` | No | Must be a real mailbox you monitor. |
| `DUNWELL_SIGNATURE` | Your real sign-off | No | Defaults to `<company> - Billing`. |
| `DUNWELL_PAYMENT_LINK` | `https://…` | No | Must be `http(s)`; it is embedded in drafts. |
| `DUNWELL_MAIL_MODE` | `smtp` | No | `dryrun` records without sending. |
| `SMTP_HOST` / `SMTP_PORT` | provider values | Host no / port no | Without `SMTP_HOST` the app falls back to dryrun. |
| `SMTP_USER` / `SMTP_PASSWORD` | provider credentials | **Yes** | Set only in the host's secret store. |
| `SMTP_TLS` | `1` | No | `0` only for a provider that requires plaintext on a private network. |
| `MAIL_FROM` | `billing@yourdomain.com` | No | Must be a verified sender or deliverability suffers. |
| `HOST` / `PORT` | `0.0.0.0` / platform port | No | Railway/Render inject `PORT`; bind `0.0.0.0`. |
| `STRIPE_API_KEY`, `QBO_*`, `XERO_*` | real tokens | **Yes** | Only for CLI connector runs; the HTTP service does not use them. |

**Rules:** no secret ever goes in `config.js`, `vercel.json`, a repo file, or a
`VITE_`/`NEXT_PUBLIC_`-style variable. `.env` is gitignored; `.env.example`
contains only empty placeholders. Rotate `SMTP_PASSWORD` immediately if it is
ever pasted into a chat, ticket, or screenshot.

---

## 7. Vercel checklist

Vercel runs **only the static landing**. It cannot run `server.py` and cannot
persist `data/waitlist.csv`; use the service for the waitlist API.

- [ ] Deploy `D:\dunwell-landing` as the Vercel project root (not `D:\dunwell`).
- [ ] Confirm `vercel.json` is picked up; `curl -sI https://<domain>/` shows
      CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy`,
      `Permissions-Policy`, and HSTS.
- [ ] Confirm `.vercelignore` excluded `server.py`, `README.md`, `data/`,
      `.github/`. `https://<domain>/server.py` and `/README.md` must return
      `404` (Vercel's own 404, not a file).
- [ ] Set `window.DUNWELL_API_URL` in `config.js` to the **service** URL
      (`https://<service-host>`), no trailing slash.
- [ ] Set `DUNWELL_CORS_ORIGINS` on the service to the exact Vercel production
      origin, then redeploy/reload the service.
- [ ] Submit the form from the production domain and confirm the service
      returns `{"ok": true, "count": N}`.
- [ ] Test a Vercel **preview** only for layout; its random origin is blocked
      by design.
- [ ] Do not add `includeSubDomains`/`preload` to HSTS until every subdomain is
      HTTPS.
- [ ] Keep `DUNWELL_SIGNUP_MODE=closed` until you are ready for public signups.

---

## 8. Verification performed

```text
# Service headers (real socket)
GET http://127.0.0.1:8912/health -> 200
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  Content-Security-Policy: default-src 'self'; ... frame-ancestors 'none'; ...
  Cache-Control: no-store
  Server: dunwell

# Landing headers + exposed files (real socket)
GET http://127.0.0.1:8914/            -> 200, same header set
GET /styles.css                       -> 200
GET /api/waitlist/count               -> 200 application/json
GET /server.py | /README.md | /data/waitlist.csv -> 404
POST /api/waitlist {"email":"nope"}   -> 422

# Behavioural spot checks (in-process)
duplicate signup        -> 409, no address echoed
cross-origin + cookie   -> 403 forbidden_origin
same-origin + cookie    -> 200
cross-origin text/plain -> 415 unsupported_media_type
reply_to "bad"          -> 422 invalid_email
2000-char password      -> 400 password is too long
public host cookie      -> Secure present
localhost cookie        -> Secure absent
landing CSV             -> "'=cmd@x.test,'=src,<ts>" (formula neutralised)

# Dashboard in Chromium
/dashboard -> 200, page renders; console: only expected 401 on /api/me,
no CSP violations.
```

**Test suite:** `python -m pytest tests -q` → **150 passed** (136 behavioural
+ 14 security regression tests).

---

## 9. How to re-run the checks

```powershell
cd D:\dunwell
python -m pytest tests -q

# service headers
python -m dunwell.serve --port 8912 --db $env:TEMP\audit.db
# then, in another shell:
#   curl.exe -sI http://127.0.0.1:8912/health

# landing headers and exposure
cd D:\dunwell-landing
python server.py --port 8914
# then:
#   curl.exe -sI http://127.0.0.1:8914/
#   curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8914/server.py

# secret scan (both repos)
git log --all -p | Select-String -Pattern 'sk_live|sk_test|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE|ghp_[A-Za-z0-9]{30,}'
git grep -n -I -E 'sk_live|sk_test|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE'
```

---

## 10. Files changed in this audit

| File | Change |
| --- | --- |
| `dunwell/service.py` | Headers, `no-store`, Origin + JSON content-type enforcement, sanitised errors, mailer error handling, profile sanitisation, chunked-body rejection, banner, socket timeout, secure-cookie fallback. |
| `dunwell/auth.py` | scrypt N=2^15, 1024-char password cap, control-character rejection in email. |
| `dunwell/store.py` | `0600` permissions, `secure_delete`, `busy_timeout`, non-enumerating duplicate error. |
| `dunwell/io.py` | CSV formula neutralisation, `StringIO` parsing. |
| `tests/test_security.py` | 14 regression tests (new file). |
| `deploy/pythonanywhere_wsgi.py` | CORS-origin documentation for the Vercel move. |
| `dunwell-landing/server.py` | Security headers, HSTS, static allowlist tightened, CSV guard + reverse guard, limiter pruning, chunked-body rejection, banner. |
| `dunwell-landing/vercel.json` | Security headers for production (new file). |
| `dunwell-landing/.vercelignore` | Excludes `server.py`, `README.md`, `data/`, `.github/` (new file). |

The audit changes are committed alongside this report. No secret, database,
`.env` file, or waitlist export is included in the commit.
