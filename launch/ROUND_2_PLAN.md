# Dunwell — Round 2: the launch plan

Built from your Round 1 answers:

| Decision | Answer |
| --- | --- |
| Launch shape | **Waitlist now → public self-serve beta after 5 hand-onboarded pilots** |
| Geography | India + international |
| Hosting | Vercel (landing) + a host with a real disk for the service |
| Model | Free trial, then a reasonable monthly price |
| Team | Solo |
| Hours per week | **assumed 8** — correct me if it is under 5, because then the beta moves to week 6 |

Two interpretations I had to make. Say the word and I will change them:

1. **"One time try then paid"** = a 14-day free trial with no card, then a
   monthly subscription. Not a one-time purchase.
2. **Segment** = you listed all four. Launching to all four is launching to
   none, so this plan picks **Indian agencies and studios** as primary, with
   freelancers as a secondary content channel. Reasoning below.

---

## 1. Who we actually launch to

**Primary: Indian agencies, studios, and consultancies — 5 to 50 open invoices
a month.**

Why this one and not the other three:

| Segment | Verdict |
| --- | --- |
| **Agencies / studios** | ✅ 10–50 invoices a month, 3–10 late at any time, relationships matter, CSV exports exist in Zoho Books / Tally / QuickBooks, and ₹999–2,499/month needs no approval |
| Solo freelancers | ⚠️ Highest pain, lowest willingness to pay. Use them for reach and content, not revenue |
| Indian SaaS startups | ⏳ Fewer, larger invoices; they buy DSO reporting, not reminders. Revisit after the connectors ship |
| Online / D2C sellers | ❌ Different workflow — distributor ledgers, marketplace settlements, no "client relationship" to protect. The product does not model this |

The demo ledger is already shaped like a studio (13 invoices, a disputed one, an
opted-out client, a strategic account). That is not a coincidence — sell what
the demo shows.

**Positioning, one line:**

> Dunwell chases the invoices worth chasing, and leaves the rest alone.

Supporting line for the landing hero: *"It refused to touch 9 of 13 overdue
invoices. That is the product."*

**Who it is not for** (say this out loud on the page): not for businesses
sending 500 invoices a month, not for B2C, not for anyone who wants fully
automatic sending on day one.

---

## 2. Pricing: trial, then paid

The current page sells $49 / $149 / $399. That is priced for a product with
payment reconciliation and QuickBooks/Xero connectors — neither exists. Cut to
**two plans**, priced for what the code does today.

| | Starter | Growth |
| --- | --- | --- |
| India (INR) | **₹999 / month** | **₹2,499 / month** |
| International (USD) | **$19 / month** | **$49 / month** |
| Open invoices | 25 | 200 |
| Draft stages | courteous + firm | full ladder incl. final notice |
| CSV import | ✅ | ✅ |
| Reason-coded holds | ✅ | ✅ |
| Report export (MD + CSV) | ✅ | ✅ |
| QuickBooks / Xero | roadmap | roadmap — **do not sell it yet** |
| First 10 customers, 50% for life | **₹499** | **₹1,249** |

Rules:

- **14-day trial, no card.** Full product, capped at 25 invoices. The cap is the
  upgrade trigger.
- **Monthly only at launch.** No annual plan, no seats, no usage billing. Add
  annual (2 months free) after the first 5 payments.
- **Payment link, manual activation.** Razorpay for INR, Stripe or Lemon
  Squeezy for USD. Do not build billing into the app for 10 customers.
- **Honour the 50%-for-life promise** already on the live page. Nobody has
  signed up, so you can also remove it — but if the page said it when someone
  joined, keep it.
- **Scale plan is deleted** until custom policies and API access exist.

The landing's pricing section needs a rewrite to match this table. That is a
one-hour change, not a redesign.

---

## 3. The launch sequence: three gates

Do not open the next gate until the previous one passes.

### Gate 1 — Waitlist collecting real emails (week 1)

The form is live but disconnected. Deploy, connect, and prove one real signup
lands in the service's database.

Pass condition: submit the form on the Vercel URL, then see
`GET /api/waitlist/count` increment on the service URL.

### Gate 2 — Five pilots on their own ledgers (weeks 1–2)

Not demo data. Their CSV, their clients, their invoices. You onboard each one
by hand, because CSV column mapping is the thing most likely to break and you
want to see it break with five people, not fifty.

Pass condition: 5 accounts created, 5 ledgers imported, at least 3 drafts
approved in week 2.

### Gate 3 — Public self-serve beta (week 3)

Open signup, trial cap on, waitlist gets first access for 48 hours, then the
launch posts go out.

Pass condition: a stranger can sign up, import a CSV, and see a decision without
you touching anything. If that is not true, the gate is not open.

**Why this order:** the public beta's whole promise is "try it yourself". Today
the import is the weakest part and email does not send. Pilots buy you the
objection list you need before strangers arrive.

---

## 4. Week 1 ship list (Oct 31 – Nov 6)

Ordered. Stop when the week ends; the rest moves to week 2.

1. **Deploy the landing to Vercel.** Import `dunwell-landing`, framework
   "Other", no build command, output `.`. Keep GitHub Pages live until the
   Vercel URL is verified, then redirect or delete.
2. **Deploy the service with a real disk.** PythonAnywhere free (WSGI, 512 MiB
   persistent home) is the ₹0 option and is already documented in `deploy.md`
   §3. Railway with a `/data` volume is the upgrade if PythonAnywhere's
   100 CPU-seconds/day is too tight.
3. **Set the service environment.** Without these, drafts go out as "Northwind
   Studio":
   ```
   DUNWELL_DB=/home/<user>/dunwell/data/dunwell.db
   DUNWELL_CORS_ORIGINS=https://<project>.vercel.app
   DUNWELL_SIGNUP_MODE=closed        # open at Gate 3
   DUNWELL_TRUSTED_PROXY_HOPS=1
   DUNWELL_COMPANY_NAME=<your studio name>
   DUNWELL_REPLY_TO=<your billing address>
   DUNWELL_PAYMENT_LINK=<your payment link or UPI page>
   DUNWELL_MAIL_MODE=dryrun
   ```
4. **Point the page at the service.** Set `window.DUNWELL_API_URL` in
   `config.js` to the service origin and commit it. The GitHub Actions workflow
   that used to inject it does not run on Vercel.
5. **Verify end to end.** Form → waitlist row → count. Then create one account
   with `python -m dunwell.admin`, import the demo ledger, approve one draft,
   and confirm the delivery id starts with `dry_` (expected — nothing sends yet).
6. **Add a "Copy draft" button** to the dashboard. Pilots will send from their
   own Gmail until there is a verified sending domain; without copy, the
   approve flow is a dead end. ~15 minutes of work.
7. **Fix the landing's claims** (Gate 3 blocker, but do it while you are in
   there): remove payment reconciliation, auto-stop, relationship memory, and
   "auto-send can be enabled"; label QuickBooks/Xero as roadmap; replace the
   three USD tiers with the two-plan table above.
8. **Record the 40-second demo.** `launch/DEMO_RECORDING.md` is already written
   for this. Upload unlisted, set `DUNWELL_DEMO_URL`.

Deliberately **not** in week 1: billing integration, trial-expiry code, email
delivery, OAuth connectors, a custom domain.

---

## 5. The pilot workflow (no domain, no email API, ₹0)

This is the part most plans get wrong. You do not need sending infrastructure to
run a pilot; you need the draft to be easy to move into the user's own inbox.

1. You create the account (`python -m dunwell.admin --email ...`), because
   signup is closed.
2. They export customers + invoices from Zoho Books / Tally / QuickBooks as CSV
   and send it to you. You import it on a call, fixing column names live.
3. They review decisions and drafts in the dashboard.
4. They hit **Copy draft**, paste into their own email client, send.
5. They hit **Approve** so the nudge is recorded and the cadence advances.

That gives you the full product loop with zero deliverability risk, and it
teaches you the exact CSV shapes Indian agencies actually export. When the first
payment lands, buy the domain (~₹900/year) and add the Resend HTTP mailer from
`deploy.md` §10c — then approve-and-send becomes one button.

**Do not** turn on SMTP with your personal Gmail. One bad send from a shared
address poisons the domain before you have one.

---

## 6. Thirty days, solo, ~8 hours a week

| Week | Focus (6–8 h) | Output |
| --- | --- | --- |
| **1** Oct 31–Nov 6 | Deploy, connect waitlist, copy button, demo video | Live URL, working form, video |
| **2** Nov 7–13 | 20 warm DMs to agencies; 5 onboarding calls; X thread | 5 pilots, first real ledgers |
| **3** Nov 14–20 | Open beta to waitlist 48h early; r/freelance post; fix top 3 objections | 15–25 signups, 3 active users |
| **4** Nov 21–27 | Ask pilots to pay; r/SaaS post; r/smallbusiness question | 2 paying, ₹2–5k MRR |

Outreach beats posting for the first ten customers. Twenty personal messages to
agency owners you can name will outperform a Reddit launch, and they do not
depend on account age or subreddit rules.

The posts in `launch/POSTS.md` are written for freelancers, SaaS, and small
business — not agencies. Reuse the r/freelance one as-is; write a new
agency-specific post for LinkedIn/X around "the client is friendly and 60 days
late" before week 2.

---

## 7. Metrics, and when to stop

Track five numbers weekly. Nothing else.

| Metric | Day 30 target |
| --- | --- |
| Waitlist emails | 25 |
| Pilots onboarded | 5 |
| Weekly active pilots (approved ≥1 draft) | 3 |
| Invoices recovered (₹, reported by pilots) | ₹50,000+ |
| Paying customers | 2 |

**Kill / change criteria:**

- 20 warm DMs → fewer than 5 replies: the offer or the segment is wrong. Do not
  build features; rewrite the first message.
- 5 pilots → fewer than 2 approve a draft in week 2: the import or the drafts
  are wrong. Watch an onboarding call end to end.
- 5 pilots, 30 days, 0 paid: the value is not worth ₹999 yet. Either the drafts
  are not good enough or the pain is not monthly. Interview the 5 before
  changing the price.

---

## 8. What I would cut from the launch

- The Scale plan and its "custom policies / API access" bullets.
- Every mention of reconciliation, auto-send, and connectors.
- The GitHub Pages deployment, once Vercel is verified.
- Self-serve billing. Payment links are enough for ten customers.
- The Stripe connector. Indian agencies use Zoho Books and Tally; Stripe
  invoices are a different buyer. CSV first.
- Any plan to support all four segments. One segment, one message, one price.

---

## 9. Open risks

1. **PythonAnywhere free is one worker and 100 CPU-seconds/day.** Five pilots
   with small ledgers fit; a public beta may not. Watch the CPU meter in week 3
   and move to Railway before it becomes an outage.
2. **CSV import is the fragile part.** Every accounting tool exports different
   column names. Budget two hours per pilot for the first five, and keep a note
   of every mapping you fix — that list becomes the import wizard.
3. **Vercel's serverless filesystem is not the service's home.** Keep the
   service on a disk-backed host until the store moves to Postgres. Do not let
   a "deploy everything to Vercel" urge delete the waitlist.
4. **The 50%-for-life promise is public.** Decide before Gate 3 whether it
   applies to the first 10 customers or is removed. Do not leave it ambiguous
   in the FAQ and pricing page at the same time.
