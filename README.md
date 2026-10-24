# Dunwell

Grounded AI receivables agent. It reads a ledger, decides — deterministically —
which overdue invoices deserve a follow-up, and drafts that follow-up using only
recorded facts. Everything else is held for a human with a typed reason code.

The core invariant: **a draft is never invented.** If the facts do not support a
message, the invoice is held, not chased.

## Quickstart

**Engine (CLI)** — one deterministic run over a CSV ledger:

```bash
python -m dunwell --ledger-dir demo --as-of 2026-10-16 --out out
```

Writes `out/dunwell_report.md` and `out/decisions.csv`.

**Service** — dashboard + API in one process, no credentials required:

```bash
python -m dunwell.serve --port 8906
```

| URL | What it is |
| --- | --- |
| `http://127.0.0.1:8906/dashboard` | Import a ledger, review drafts, approve sends |
| `http://127.0.0.1:8906/` | The landing page, if `dunwell-landing` sits next door |
| `http://127.0.0.1:8906/api/**` | The JSON API the dashboard uses |

State lives in one SQLite file (`dunwell.db` by default). Drafts are recorded
but **not delivered** until you configure SMTP — see
[Email delivery](#email-delivery).

## Layout

```
dunwell/
  core.py             models, aging/DSO, decide() policy, grounded draft()
  agent.py            run() loop + RunReport metrics
  io.py               CSV ledger load, markdown + decisions CSV writers
  cli.py, __main__.py python -m dunwell
  store.py            SQLite persistence: accounts, ledger, nudge history
  auth.py             scrypt password hashing + session tokens
  mailer.py           Mailer protocol: DryRunMailer, SmtpMailer, build_mailer()
  service.py          HTTP routing: API, session auth, landing, dashboard
  serve.py            python -m dunwell.serve
  static/
    dashboard.html    operator UI (packaged with the code)
  connectors/         ledger sources -> domain models
    base.py           LedgerSnapshot, Transport, FixtureTransport, UrllibTransport
    csv_source.py     customers.csv + invoices.csv (offline default)
    stripe.py         Stripe API
    quickbooks.py     QuickBooks Online API
    xero.py           Xero API
demo/
  customers.csv, invoices.csv     13-invoice fixture covering every reason code
  fixtures/<source>/*.json        offline payloads for the three live connectors
tests/
  test_core.py        engine regression suite
  test_connectors.py  connector mapping, warnings, parity, offline behaviour
  test_cli.py         CLI source selection and failure paths
  test_service.py     store, auth, mailer, HTTP application
  test_serve.py       serve entry point, dashboard, end-to-end socket flow
```

## Service

### API

All responses are `{ "ok": true, ... }` or `{ "ok": false, "error": { "message",
"code" } }`. Protected routes return `401` with code `unauthorized`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | — | liveness probe: `{ok, status}` |
| `POST` | `/api/waitlist` | — | `{email, source}` → `{ok, count, already?}` |
| `GET` | `/api/waitlist/count` | — | `{ok, count}` |
| `POST` | `/api/signup` | — | `{email, password}` → sets session cookie |
| `POST` | `/api/login` | — | `{email, password}` → sets session cookie |
| `POST` | `/api/logout` | — | clears the session cookie |
| `GET` | `/api/me` | session | the signed-in account |
| `GET` | `/api/ledger` | session | invoice rows + counts |
| `POST` | `/api/ledger` | session | `{customers_csv, invoices_csv}` or `{demo: true}` |
| `GET` | `/api/report` | session | metrics, aging, decisions, rendered markdown |
| `GET` | `/api/drafts` | session | drafts pending approval |
| `POST` | `/api/drafts/<id>/send` | session | approve + deliver one draft |
| `GET` | `/api/nudges` | session | sending history |

Sending is deliberately explicit. **Nothing is delivered as a side effect of
reading a report** — every send is its own `POST`.

### Why nudge history is stored

`Invoice.contacts` is what drives `cooldown` and `max_nudges`. The connectors
return no history, so those two rules never fired on live data. The service
records each approved send in the `nudges` table and rehydrates `contacts` on
every run, so:

- a second send for the same invoice inside 7 days is refused (`409 held`),
- the tone ladder advances `courteous` → `firm` → `final`,
- the third nudge exhausts the cadence and escalates to a human.

The `nudges` primary key is a generated id, not
`(account_id, invoice_id, sent_at)`: two sends in the same clock tick must not
collide, and `sent_at` carries microseconds so ordering survives a burst.

### Email delivery

`build_mailer()` reads the environment:

| Variable | Effect |
| --- | --- |
| `DUNWELL_MAIL_MODE` | `dryrun` (default) or `smtp` |
| `SMTP_HOST`, `SMTP_PORT` | required for `smtp`; missing host falls back to dryrun |
| `SMTP_USER`, `SMTP_PASSWORD` | credentials, if the relay needs them |
| `SMTP_TLS` | `1` (default) upgrades with STARTTLS; `0` disables |
| `MAIL_FROM` | envelope sender — use a domain you have verified |

Dry-run mode records the message in memory and returns a `dry_<hex>` delivery
id, so the whole approve → send → history flow is testable offline. Nothing
leaves the machine until you ask it to.


## Decision policy

`decide()` returns a `Decision` with a typed `Reason` and `Stage`. Nothing is
scored by a model; the ordering is explicit and testable.

| Reason | Meaning | Outcome |
| --- | --- | --- |
| `ready` | Open, past due, contactable, inside the cadence | draft |
| `not_due` | Not past due yet | hold |
| `already_paid` | Settled | hold |
| `void` | Voided / written off | hold |
| `disputed` | Marked disputed | hold + escalate |
| `opted_out` | Customer asked not to be contacted | hold |
| `no_contact` | No email on record | hold + escalate |
| `cooldown` | Contacted recently | hold |
| `too_small` | Below the policy floor | hold |
| `max_nudges` | Cadence exhausted | hold |
| `over_escalation_age` | Too old to automate | hold + escalate |

Stages are `courteous` → `firm` → `final` → `escalate`.

## Connectors

Every source normalizes into one `LedgerSnapshot`, so the engine never knows
where the invoices came from. Read mapping is real; write-back is declared
honestly through `capabilities()`.

```python
from dunwell.connectors import build_connector

snapshot = build_connector("stripe", fixtures="demo/fixtures/stripe").fetch()
print(snapshot.source, len(snapshot.invoices), snapshot.warnings)
```

### Offline (fixtures)

`fixtures=` puts a connector in offline mode: no network, no credentials. That is
what the tests and demos use.

```bash
python -m dunwell --source stripe     --fixtures demo/fixtures/stripe     --as-of 2026-10-16 --out out/stripe
python -m dunwell --source quickbooks --fixtures demo/fixtures/quickbooks --as-of 2026-10-16 --out out/qbo
python -m dunwell --source xero       --fixtures demo/fixtures/xero       --as-of 2026-10-16 --out out/xero
```

### Live

Omit `--fixtures` and supply credentials through the environment.

| Source | Variables |
| --- | --- |
| Stripe | `STRIPE_API_KEY` |
| QuickBooks | `QBO_ACCESS_TOKEN`, `QBO_REALM_ID`, optional `QBO_SANDBOX=1` |
| Xero | `XERO_ACCESS_TOKEN`, `XERO_TENANT_ID` |

Live HTTP uses `urllib` from the standard library — no `requests` dependency.

### Field mapping

| Concept | Stripe | QuickBooks | Xero |
| --- | --- | --- | --- |
| Amount owed | `amount_due` (minor units) | `Balance` | `AmountDue` |
| Currency | `currency` | `CurrencyRef.value` | `CurrencyCode` |
| Issued | `created` | `TxnDate` | `DateString` |
| Due | `due_date` | `DueDate` | `DueDateString` |
| Paid | `status=paid` | `Balance <= 0` | `Status=PAID` |
| Void | `status=void` | — | `Status=VOIDED` |
| Disputed | `status=uncollectible` | `PrivateNote` starting `DISPUTED:` | `Reference` starting `DISPUTED:` |
| Skipped | `status=draft` | — | `Type != ACCREC`, `Status` draft/submitted |
| Contact | `customers.email` | `PrimaryEmailAddr.Address` | `Contact.EmailAddress` |

### Deliberate limitations

These are stubs with a clear seam, not finished integrations. Do not treat them
as production-ready without closing the list below.

1. **No pagination.** Each call asks for one page (`limit`/`page_size`); a ledger
   larger than that page is silently truncated.
2. **QuickBooks and Xero have no dispute status.** Rather than guess from free
   text, both honour an explicit `DISPUTED:` prefix. A real deployment should use
   a custom field and drop the convention.
3. **QuickBooks `paid_on` is `None`.** The paid date lives on the linked Payment
   document, which is not queried yet. Nothing in the policy needs it today.
4. **Nudge history is not read from the source.** Contact history belongs to
   Dunwell, so connectors return `contacts=()`. Until we own that store, every
   run looks like a first contact and `cooldown`/`max_nudges` never fire from
   live data.
5. **No write-back.** `record_contact()` raises `UnsupportedOperation` on every
   connector. Nothing is ever written to the customer's books.
6. **No OAuth flow.** QuickBooks and Xero expect an access token in the
   environment; token refresh is not implemented.
7. **Missing due dates fall back to the issue date** and record a warning on
   `LedgerSnapshot.warnings`. The CLI prints those warnings above the report.

## Tests

```bash
python -m pytest tests -q
```

Standard library only at runtime; `pytest` is the sole dev dependency. The
suite covers the engine, the three connectors, the CLI, and the service --
including a real-socket end-to-end flow (sign up, import, approve, send,
history) and a test proving sent nudges feed back into the cadence policy.

## CI

`.github/workflows/ci.yml` runs the suite on Python 3.11-3.13, then does a
packaging check on a clean checkout: the CLI runs against the demo ledger, the
bundled dashboard is present, and the service starts and answers on
`/dashboard`.

## Configuration

Every variable the project reads is listed in `.env.example`. Nothing is
required to run the CLI, the service, or the tests offline.

Two of them matter before a public deploy:

- `HOST` / `PORT` — the service reads both from the environment, so a PaaS
  platform that sets `PORT` works without extra flags. `--host` / `--port`
  still override them.
- `DUNWELL_COMPANY_NAME`, `DUNWELL_REPLY_TO`, `DUNWELL_PAYMENT_LINK` — these
  land in every draft body and are customer-visible. The shipped defaults are
  placeholders; set them or the customer receives "Northwind Studio - Billing".

One more if the landing page is hosted separately:

- `DUNWELL_CORS_ORIGINS` — a comma-separated allowlist of origins permitted to
  call the public waitlist routes from a browser. Only `POST /api/waitlist`
  and `GET /api/waitlist/count` honour it; the authenticated API is never
  reachable cross-origin. Empty (the default) disables cross-origin access.


## Deployment

`deploy.md` is the full guide: free-tier platform comparison, the two
deployables (service and landing), DNS, email delivery, and a verification
checklist to run after every deploy.

Short version: the **service** needs a persistent filesystem (it stores
accounts, ledgers, and nudge history in SQLite), and the **landing** is
static and can go on GitHub Pages. They are separate deployables and can run
on different hosts — see [deploy.md](deploy.md).

### Transports

The service runs two ways from one `Application` object:

| Transport | Entry point | Use it for |
| --- | --- | --- |
| HTTP server | `python -m dunwell.serve` | local development, any host that gives you a process |
| WSGI | `dunwell.wsgi:application` | PythonAnywhere, Gunicorn, uWSGI, most managed Python hosts |

```bash
gunicorn dunwell.wsgi:application        # any WSGI host
python -m dunwell.serve --port 8906      # local
```

PythonAnywhere imports a callable rather than running a process, so it uses
the WSGI path; `deploy/pythonanywhere_wsgi.py` is a ready-to-paste config.
Both transports share the same routes, the same CORS rules, and the same
tests, so a behaviour verified locally holds on the host.

