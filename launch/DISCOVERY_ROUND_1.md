# Dunwell — Product discovery, Round 1

Written from the two repos (`D:\dunwell`, `D:\dunwell-landing`) and the live
site, so you only answer what the code cannot tell me.

**How to use this:** read the "What I found" line under each question. Where it
says **[confirm]**, correct me if I am wrong. Where it says **[you]**, that is a
real gap — answer in one line, "not sure" is fine.

---

## 1. Which product are we launching?

**What I found — answered from the repo.**

| | |
| --- | --- |
| Name | **Dunwell** |
| Repo | `github.com/Maherimtiyaz/dunwell` (service) and `github.com/Maherimtiyaz/dunwell-landing` (marketing) |
| Live landing | `https://maherimtiyaz.github.io/dunwell-landing/` — up, HTTP 200 |
| Live app | not found. No public service URL is configured anywhere |
| One-liner | A receivables agent that reads a ledger, decides per invoice whether a follow-up is justified, and drafts it only from recorded facts |
| What it is not | not an LLM writer, not a mail-merge, not an accounting tool |

**The positioning question [you]:** the repo calls it an "AI receivables agent",
the landing sells "stop chasing invoices", the launch posts sell "it refused to
touch 9 of 13 invoices". Those are three different products to a buyer. Pick the
one sentence you want a stranger to repeat:

- A. "It writes my overdue-invoice emails." (writer — crowded, cheap)
- B. "It decides which invoices should not be chased." (judgment — defensible, harder to explain)
- C. "It gets my money in without burning the client." (outcome — best for India SMEs)

**Confirm:** is Dunwell the product we launch first, or is there another?

---

## 2. Who is it for?

**You said:** small business, startups, online business.

That is three markets, and the landing currently speaks to a fourth (US/EU
agencies on $49–$399/month). Answer these three:

1. **Pick one for launch.** Which single group has the pain most weeks?
   - Indian agencies / studios / consultancies (5–50 invoices a month)
   - Indian SaaS startups billing other businesses
   - Solo freelancers and creators
   - Online/D2C sellers collecting from distributors
2. **Name three real people or companies** you could call this week in that
   group. Not a persona — names. If you cannot name three, the segment is a
   guess, and Round 2 changes shape.
3. **The main task, in their words.** One line: "Every month I ______ and it
   takes me ______ hours."

**Why this matters now:** "small business" decides the price, the channel, and
the copy. A ₹499/month solo freelancer tool and a ₹15,000/month agency tool are
different products with the same code.

---

## 3. What is the current state?

**What I found — this is further along than "prototype".**

| Piece | State |
| --- | --- |
| Engine (decision policy + grounded drafts) | working, deterministic |
| CLI (`python -m dunwell`) | working, returns report + decisions CSV |
| Dashboard + API + SQLite | working locally |
| Auth, sessions, rate limits, CORS, health | implemented |
| Tests | **135 passing** in 22s, CI on Python 3.11–3.13 |
| Landing page | **live on GitHub Pages** |
| Waitlist on the live page | **broken** — `DUNWELL_API_URL` is empty, so the form posts to `github.io/api/waitlist` and gets 404 |
| Public app deployment | **not done** |
| Email delivery | dry-run only; SMTP unconfigured |
| Connectors | Stripe / QuickBooks / Xero read-only, fixture-tested, no pagination, no OAuth, no write-back |
| Real users | 0. Live waitlist: 0. Local DB has 6 test accounts and 2 test nudges |

**So the honest answer for Round 1 is:** "works locally, marketing site is live,
sign-up is not connected, nothing is deployed that a customer can use."

**The blocker [you]:** which of these do you want true before launch day?

- [ ] Waitlist collecting emails (needs the service deployed, ~1 day)
- [ ] 5 people using it on their own ledger (concierge, no self-serve)
- [ ] Anyone can sign up and use it unattended
- [ ] It can actually send email

They are four different launch projects. Doing all four is a month, not a week.

---

## 4. Traction and business model

**What I found — answered.**

- Users: 0
- Waitlist: 0 (the form has never successfully stored a signup)
- Revenue: 0
- Audience: the repos are public; no evidence of distribution yet
- Customer conversations: none recorded in the repo
- Model: **not decided in code**, but the landing already commits to
  **$49 / $149 / $399 per month**, with "first 10 customers 50% off for life"

**The model question [you]:** pick one for the first 90 days.

| Option | What it means for Dunwell |
| --- | --- |
| Concierge paid pilot | 3–5 businesses, ₹2,000–5,000/month, you run it for them |
| Freemium | free CSV import + paid sending/connectors; needs self-serve, which is closed today |
| Paid subscription | the landing's current $49/149/399, needs real email + connectors |
| Free beta | fastest learning, no revenue signal, hardest to convert later |

**Two things to decide while you are here:**

1. **Currency and price.** The landing sells in USD to a US/EU buyer. If the
   target is India, $49 ≈ ₹4,300/month — above what most Indian small
   businesses pay for a collections tool. ₹999 / ₹2,499 / ₹5,999 is a different
   page, not a different paragraph.
2. **The 50%-for-life promise.** It is on the live page today. Whatever you
   choose, honour it for the first 10 or remove it before the launch posts go
   out.

---

## 5. Resources

| Resource | Answer |
| --- | --- |
| Geography | **India + international — confirmed.** The USD pricing ($49/149/399) now makes sense for the international half; India needs a separate INR or PPP price |
| Deploy target | **Vercel — confirmed.** Perfect for the landing, one real constraint for the service (below) |
| Budget | **₹0 [confirm]** — enough for Vercel free + a free database; not enough for a domain plus a paid email API plus a paid host at once |
| Time per week | **[you]** |
| Team | **[you]** — solo, or co-founder? |
| Stack already used | Python 3.11+, standard library only, SQLite, GitHub Actions, GitHub Pages, no frontend framework |

### Vercel: what fits, and the one thing that does not

Vercel's Python runtime speaks WSGI, and `dunwell/wsgi.py` already exports
`application`, so the code is deployable with one config line:

```toml
[tool.vercel]
entrypoint = "dunwell.wsgi:application"
```

**But Vercel Functions have no persistent disk.** Only `/tmp` is writable, it is
per-instance, and it is wiped on cold start. Dunwell keeps accounts, sessions,
ledgers, and nudge history in one SQLite file. Deployed as-is on Vercel:

- the waitlist count resets to 0 between cold starts,
- signups and sessions disappear,
- an imported ledger vanishes before the first draft is approved.

So "deploy on Vercel" splits into two decisions:

| Path | What it means | Cost |
| --- | --- | --- |
| **A. Vercel landing, service elsewhere** | Landing on Vercel (static, fast, custom domain). Service on PythonAnywhere or Railway with a real disk. One `DUNWELL_API_URL` connects them. | ₹0, no code change |
| **B. Everything on Vercel** | Move `store.py` off SQLite to a hosted Postgres (Neon/Vercel Postgres) or Turso. Touches auth, waitlist, ledger, nudges. | ₹0 tier, ~2–4 days of work |

Path A is a launch this week. Path B is a launch next week with a database
migration that has nothing to do with product-market fit. For 0 users, A is the
correct order — and B can follow once there is a reason to scale.

Also true on either path: Vercel is serverless, so the in-memory rate limiters
are per-instance and weaker than they are locally. Fine at launch scale, worth
knowing.

**What ₹0 actually buys, given this codebase:**

- Landing: Vercel (free, custom domain, CDN) — or the current GitHub Pages site
- App: PythonAnywhere free (persistent disk, no custom domain, 100 CPU-s/day)
  or Railway with a 500 MB volume
- Email: dry-run until you verify a domain; then Resend/Brevo free tier over
  HTTPS (free PaaS tiers block SMTP 587)
- Domain: the only genuinely useful spend, ~₹900/year

**The constraint that hurts [you]:** with under 5 hours a week, a public launch
plus support is not realistic. Concierge onboarding of 3–5 businesses is. Which
one are you actually signing up for?

---

## Launch blockers I found (not questions — just facts)

These will bite during launch week, in this order:

1. **The live waitlist form is dead.** The deployed `config.js` has
   `DUNWELL_API_URL = ""`, and GitHub Pages cannot answer `/api/waitlist`.
   Every visitor who tries sees "The waitlist is not connected yet" — honest,
   but it converts nobody.
2. **The landing promises features the code does not have.** Specifically:
   "reconciles the payment the moment it lands", "payment reconciliation &
   auto-stop", "QuickBooks & Xero connectors" on the paid Growth plan,
   "auto-send can be enabled per account", "relationship memory". The README is
   honest; the landing is not. Launch posts that link to it will get caught.
3. **No public app URL.** The demo recording script assumes localhost, which is
   right for recording and wrong for a launch post that says "try it".
4. **No email delivery.** Approve-and-send returns `dry_…` ids. Fine for a
   demo, not fine for "gets you paid".
5. **No license file** in either repo, though both are public. Decide: open
   source, source-available, or proprietary.
6. **No demo video.** The landing hides the button until `DUNWELL_DEMO_URL` is
   set, which is the right behaviour — but the launch posts lead with the video.

---

## The 10 answers I need to start Round 2

Copy this block and fill it in:

```text
1. Product we launch first:
2. One customer segment (one line):
3. Three real prospects I could contact:
4. The task in their words:
5. State we launch from: waitlist / concierge pilot / public beta
6. Business model for 90 days:
7. Price + currency:
8. Target geography (India only / India + international):
9. Budget for launch month:
10. Hours per week + team:
```

Round 2, once these are in: positioning and one-liner, the launch sequence
(what ships in week 1 vs deferred), a waitlist/onboarding flow that works with
signup closed, a pricing page that matches the code, and a 30-day content and
outreach calendar for the segment you pick.

**Round 2 is written:** [ROUND_2_PLAN.md](ROUND_2_PLAN.md).
