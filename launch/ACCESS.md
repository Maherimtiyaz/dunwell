# How a customer actually gets the service

The landing page sells early access, not a product you can sign up for. That is
deliberate: `POST /api/signup` is closed on the public host, and every account
is created by you, by hand, on the server.

So the honest answer to "how do people get it" is:

> They leave an email. You reply, create their account, send them a link and a
> password, and sit with them for the first import.

That is a concierge onboarding, not a funnel. It is the right shape for the
first ten customers, because the product's value depends on their ledger
looking the way the importer expects, and because you want to hear the first
"this draft is wrong" complaint directly.

---

## The path, end to end

| # | Who | What happens | Where it lives |
| --- | --- | --- | --- |
| 1 | Visitor | submits the waitlist form on the landing page | `POST /api/waitlist` → `waitlist` table on PythonAnywhere |
| 2 | You | read the list, reply within a day | Bash console (below) |
| 3 | You | qualify: is there an overdue invoice, and a ledger export? | email or a 15-minute call |
| 4 | You | create the account with their company details | `python -m dunwell.admin` |
| 5 | You | send the handover email (template below) | your normal mail client |
| 6 | Customer | signs in, imports their ledger, approves the first draft | `/dashboard` |
| 7 | You | invoice them, keep signup closed | manual |

## 1. Read the waitlist

From a Bash console on PythonAnywhere:

```bash
python -c "import sqlite3,os;c=sqlite3.connect(os.path.expanduser('~/dunwell/data/dunwell.db'));print('\n'.join(f'{e}  {t[:10]}' for e,t in c.execute('select email, created_at from waitlist order by created_at')))"
```

The count is also public at `/api/waitlist/count` if you only want the number.

## 2. Qualify before you build anything

Three questions, one reply:

1. Roughly how much is overdue right now, and how many invoices?
2. Can you export customers and invoices as CSV from your accounting tool?
3. Who writes the awkward follow-up today - you, or nobody?

If the answer to (2) is no, the import is the whole onboarding. Do not promise
a connector: there is none.

## 3. Create the account

```bash
cd ~/dunwell
DUNWELL_DB=~/dunwell/data/dunwell.db \
python -m dunwell.admin \
  --email owner@theircompany.com \
  --company "Their Company" \
  --reply-to billing@theircompany.com \
  --payment-link "https://their-payment-link"
```

It prompts for a password twice. `--company`, `--reply-to`, and
`--payment-link` are customer-visible: they appear in every drafted email, so
get them right the first time.

Use a **random password** you generate, not one you reuse. There is no
change-password route yet (see "Before you charge" below), so treat this
credential as temporary and rotate it when that route exists.

## 4. The handover email

> Subject: your Dunwell account is ready
>
> Hi {name} - your early-access account is set up.
>
> Sign in here: https://mahekimtiyaz.pythonanywhere.com/dashboard
> Email: {their email}
> Temporary password: {the password you set}
>
> Two things worth knowing before you start:
>
> - Import takes two CSVs: customers (`id,name,email,tier,opted_out`) and
>   invoices (`id,number,customer_id,amount_cents,currency,issued_on,due_on,status`).
>   "Load demo" fills both so you can see the shape first.
> - Nothing sends without your approval. You will see each draft before it
>   goes anywhere.
>
> Reply to this email if the import complains - I would rather fix it with you
> than have you guess.

## 5. The first session

Do this with them, not to them:

1. Sign in. The dashboard opens on **Import your ledger**.
2. Click **Load demo** first, so they see a populated screen with real decision
   reasons before their own data is in play.
3. Paste their two CSVs and import. Watch for a row count mismatch - it means a
   `customer_id` in one file has no match in the other.
4. Open the drafts. Read one out loud together and check the facts against the
   invoice: amount, number, days late.
5. Approve one send - theirs, with their finger on the button.

That last step is the product. Everything before it is setup.

## 6. Money

Send an invoice manually; there is no billing integration. Keep
`DUNWELL_SIGNUP_MODE=closed` until you have a reason to open it, and re-read
"Before you charge" first.

---

## Before you charge: what is not finished

These are real, and each one is a reason a paying customer could be unhappy.

| Gap | Effect | Fix |
| --- | --- | --- |
| **PythonAnywhere blocks outbound SMTP** (ports 25/465/587) | `DUNWELL_MAIL_MODE=smtp` cannot work there. The mailer falls back to `dryrun`, so "approve & send" records the nudge but no email leaves. | Send over an HTTP email API (Resend, Postmark, SES) from the worker, or run the service somewhere with SMTP. **Do this before taking money.** |
| No password change or reset | You know the password you set. The customer cannot change it. | Add `POST /api/password` + a form on the dashboard. |
| No backups on the free tier | One `.db` file, no snapshots. | Download `~/dunwell/data/dunwell.db` on a schedule, or upgrade. |
| One worker, 512 MB | A slow request blocks the whole site. | Upgrade to Developer ($10/mo) before invoicing: second worker, custom domain. |
| `mahekimtiyaz.pythonanywhere.com` | Works, looks like a side project. | Custom domain needs the paid tier. |
| Billing is manual | No plan, no entitlement, no dunning. | Fine for ten customers. Not fine for a hundred. |

The first row is the one that matters. A receivables product that cannot send
an email is a report generator.

---

## When to open self-serve signup

Not until all of these are true:

- sending works without you in the loop;
- a customer can reset their own password;
- the import handles a ledger that is not shaped like the demo;
- you have a plan model, so "paid" means something to the app.

Until then, `DUNWELL_SIGNUP_MODE=closed` is not a limitation to apologise for.
It is the reason nobody can burn your sending reputation or your single worker.

## If someone asks "can I just sign up?"

Say: "Not yet - early access is set up by hand. Send me your email and I will
have you in today." Then do steps 3-5. That is a better first impression than a
signup form, and it is the truth.
