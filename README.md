# Eureka Forbes — Social Chat Agent

An AI agent that answers Eureka Forbes customers on **WhatsApp**, **Facebook Messenger**
and **Instagram DMs**. Same architecture as the GoEd AI chatbot, rebuilt for EF:
one brand, EF's Dataverse, EF's model — and **no Redis, no Postgres, no MCP server**.

---

## Architecture

```
Meta (WhatsApp / Messenger / Instagram)
            │  webhook
            ▼
┌───────────────────────────────────────────────┐
│  chatbot/main.py    FastAPI, port 8000        │
│    verify → parse → dedupe → ack 200          │
│    agent runs in the background, replies      │
└───────────────┬───────────────────────────────┘
                │
        ┌───────▼────────┐        ┌──────────────────┐
        │ chatbot/agent  │───────▶│ chatbot/tools/   │
        │  per-channel   │        │  local LangChain │
        │  agent + 2     │        │  tools (no MCP)  │
        │  middleware    │        └────────┬─────────┘
        └───┬────────┬───┘                 │
            │        │                     ▼
            │        │            ┌──────────────────┐
            │        │            │  EF Dataverse    │
            │        │            │  (CRM, OData)    │
            │        │            └──────────────────┘
            │        ▼
            │   ┌─────────────────────┐
            │   │ prompts/<channel>.md│  hot-reloaded on edit
            │   └─────────────────────┘
            ▼
   ┌────────────────────────────┐
   │ SQLite (data/ef_chat.db)   │  sessions, messages, summaries,
   │ chatbot/client/store.py    │  kv+TTL, webhook dedupe
   └────────────────────────────┘
```

### What changed from the GoEd reference

| GoEd | Here | Why |
|---|---|---|
| Redis sessions | SQLite (`client/store.py`) | no infra to run; survives restarts |
| Postgres prompts + pgvector KB | `prompts/*.md` files | prompts are edited, not queried |
| MCP server on :8001 | local tools in `chatbot/tools/` | one process, no network hop |
| Per-college `api_config`, trial gating | env vars | single tenant |
| Archive worker, web widget | dropped | not in scope |

Everything else is deliberately the same: channel-scoped prompts and tool sets,
middleware-based context injection, the rolling-summary memory pattern, the
once-per-session tool guard, and background processing so Meta always gets a fast 200.

---

## Quick start

```bash
cp .env.example .env          # fill in AZURE_LLM_API_KEY, DATAVERSE_*, Meta tokens
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python chatbot/main.py            # serves on :8000
.venv/bin/python scripts/local_chat.py      # or talk to it in the terminal
```

Expose it to Meta with any tunnel (`ngrok http 8000`) and register the callback URLs:

| Channel | Callback URL | Verify token |
|---|---|---|
| WhatsApp | `https://<host>/whatsapp` | `WHATSAPP_VERIFY_TOKEN` |
| Messenger | `https://<host>/facebook` | `FACEBOOK_VERIFY_TOKEN` |
| Instagram | `https://<host>/instagram` | `INSTAGRAM_VERIFY_TOKEN` |

Each is also served at `/webhook/<channel>` — Meta does not always re-verify when you edit
the callback path, and a mismatch shows up as a silent 404-retry loop on live messages only.

---

## The two things you will edit

### 1. Prompts — `prompts/<channel>.md`

One file per channel, currently placeholders. Edit the file and the **next message
uses it** — the loader watches the mtime and rebuilds the agent. No restart.

Variables substituted per call: `{{brand_name}} {{agent_name}} {{support_number}}
{{customer_name}} {{customer_phone}} {{channel}} {{current_date}} {{current_time}}`.

Do not restate customer profile, date/time or history in the prompt — the middleware
appends all of that after the base prompt on every call.

### 2. Tools — `chatbot/tools/`

```python
@tool
async def book_service_visit(date: str, slot: str, session_id: str = None) -> str:
    """Book a technician visit. Use only after confirming the date with the customer."""
    ...
    return json.dumps({"status": "success", ...})
```

Then register and expose it:

```python
# chatbot/tools/__init__.py
REGISTRY = {t.name: t for t in [identify_customer, book_service_visit]}

# chatbot/client/channel_config.py
"whatsapp": ChannelConfig(
    prompt_id="whatsapp",
    tools=["identify_customer", "book_service_visit"],
    once_per_session=["book_service_visit"],   # hard-blocked on a second call
    ...
)
```

Conventions that the runtime relies on:

- `session_id` is **injected automatically** into any tool that declares it — the model never supplies it.
- Return JSON with a `status` key; raise nothing at the model.
- A result with `status: found|success` and `record_id` / `lead_id` / `customer_id` is cached on the
  session, so the agent stops re-identifying the customer.
- Names in `once_per_session` are blocked after their first successful run, and the block is
  stated in the system prompt too — the model is told and enforced.

`ef_tools.py` ships one worked example (`identify_customer`, phone → EF customer/lead/prospect).
It is registered but not enabled on any channel; add its name to a channel's `tools` list to turn it on.

---

## How a message flows

1. Meta POSTs the webhook → parse → drop duplicates/echoes → **200 immediately**.
2. Profile (name, phone, sender id) is written to the session; yesterday's summary is carried over.
3. Typing indicator on; the agent runs in a background task, serialised per user.
4. `InjectSessionContext` builds the system prompt: base prompt → date/time → profile + CRM record
   + tool guards + house rules → conversation history.
5. `HandleToolErrors` injects `session_id`, blocks repeat calls, converts exceptions into
   messages the model can recover from.
6. Tokens are accumulated (social channels send one complete message) and the reply goes out.
7. Every 5th user message, a background LLM call folds the buffer into a rolling summary,
   so the context stays small no matter how long the conversation runs.

---

## CRM behaviour

The agent's job is to end every conversation with the right record in Dataverse.
Entity-set names, option-set values, lookup navigation properties and date formats
all come from `ef_schema_reference.json` via [chatbot/client/ef_schema.py](chatbot/client/ef_schema.py) —
nothing is hardcoded, so a re-published solution does not silently break the writes.

### The flow

```
customer messages in
   → agent asks name (+ phone; on WhatsApp the number is already known)
   → identify_customer  ── searches ef_customer → ef_lead → ef_prospect
        │
        ├── customer  → returns their 360 in the same call: assets, contracts,
        │               open cases, and any contract expiring within 6 months
        │               · agrees to renew → start_amc_renewal
        │                    creates an ef_lead "AMC renewal · <model>" (sales)
        │                    AND an ef_servicerequest type=AMCRequest (service)
        │               · reports a fault → raise_service_request
        │
        ├── lead/prospect → continue; never create a duplicate
        │                   (a prospect promoted to lead keeps the ef_Prospect link)
        │
        └── nobody    → create_lead once, then update_lead_details as more is learned
   → 2 minutes idle → one ef_interaction row for the whole conversation
                      + counters rolled forward on the parent record
```

### Phone matching

The org stores both `+91 98450 71284` and `9893984982`, so `ef_phone eq …` matches
almost nothing. Lookups filter server-side on `contains(ef_phone,'<last 5 digits>')`
— the only run of digits that survives every separator style in the data — then
confirm an exact match on normalised digits in Python. New records are written as
`+91 XXXXX XXXXX` to match the dominant convention.

### What gets written

| Table | When | Notable fields |
|---|---|---|
| `ef_lead` | no existing record, once name + phone are known | `ef_source` = WhatsApp/MetaDM, `ef_status` New→Working, `ef_productinterest`, `ef_qualificationscore` (0–1, computed) |
| `ef_lead` | customer agrees to an AMC renewal | `ef_productinterest` = "AMC renewal · <model>" |
| `ef_servicerequest` | renewal agreed, or a fault reported | `ef_requesttype` AMCRequest/Complaint/ServiceRequest, bound to customer + asset, `ef_visitdate` + `ef_visitstatus` once a slot is chosen |
| `ef_interaction` | 2 min after the last message | channel, direction, disposition, intent, sentiment, summary (≤2000), `ef_transcriptref` = session id, `ef_handledbytype` = AIAgent |
| `ef_customer` | after an interaction | `ef_totalinteractions`, `ef_inboundcount`, `ef_lastinteractiondate`, `ef_lastinbounddate`, running `ef_avgsentiment`, `ef_consecutivenonresponses` = 0 |

`ef_disposition` is decided by what actually happened before the model gets a say:
a renewal started → `ConvertedRenewal`, a case logged → `Resolved`, an escalation →
`Escalated`. Only when no rule applies does the classifier choose between
`Qualified` / `Interested` / `Engaged` / `Declined` / `NoResponse`.

### Service visits

`ef_servicerequest` holds one `ef_visitdate` timestamp and no notes field, so a slot
is stored as its **start time** and the window is what the agent says out loud. The
booked window is also written into the interaction summary so it survives in the CRM.

| Window | Stored `ef_visitdate` |
|---|---|
| morning — 10 AM – 1 PM | 10:00 IST (04:30 UTC) |
| afternoon — 1 PM – 4 PM | 13:00 IST (07:30 UTC) |
| evening — 4 PM – 7 PM | 16:00 IST (10:30 UTC) |

Rules enforced in code, not by the model: Monday–Saturday only, no past dates,
nothing more than 30 days ahead, and no same-day booking after 3 PM. A slot that
breaks a rule is refused with the next real alternatives attached, so the agent
offers something valid instead of confirming a time that was never written.

The agent may pass a slot straight to `raise_service_request` when the customer
volunteers one, or call `book_service_visit` afterwards; calling it again reschedules.
`ef_visitstatus` is set to `Scheduled`, and the case binds to the specific product
when the customer names it (an ambiguous mention binds nothing rather than the wrong
asset). Booking sets the interaction disposition to `ConvertedVisit`.

To change the windows or the rules, edit `SLOTS`, `CLOSED_WEEKDAYS` and
`SAME_DAY_CUTOFF_HOUR` at the top of the visit section in
[chatbot/tools/ef_crm.py](chatbot/tools/ef_crm.py).

### Interaction logging

One row per conversation, written `INTERACTION_IDLE_SECONDS` (default 120) after the
last message. A watermark per session records how far the CRM has been told about, so
a customer who returns later the same day produces a second row covering only the new
messages — never a duplicate, never a gap.

If nobody has been identified yet, the conversation is **held rather than logged**, so
it can land on the right record once they give a number. After
`INTERACTION_ORPHAN_HOURS` (default 24) it is logged unlinked rather than lost.

Force a flush instead of waiting:

```bash
curl -X POST -H "X-Admin-API-Key: $ADMIN_API_KEY" \
  "localhost:8000/admin/flush-interactions?idle_seconds=0"
```

`ef_costamount` is populated from token usage only if `COST_PER_1K_INPUT` /
`COST_PER_1K_OUTPUT` are set; otherwise it is written as 0. `ef_campaign` is left
null — inbound social conversations are not part of a campaign run.

### Verifying against the org

```bash
EF_LIVE=1 ./tests/run_all.sh          # read-only: identity, 360, renewal detection
EF_LIVE_WRITE=1 ./tests/run_all.sh    # creates real records, verifies fields, deletes them
.venv/bin/python scripts/cleanup_test_records.py            # dry run: what is test data
.venv/bin/python scripts/cleanup_test_records.py --delete   # remove ONLY the test records
```

The cleanup script identifies test data by marker (a name containing "TEST", a
9000000000-range phone, a "TEST …" case category, a `test:`/`:e2e` transcript ref) and
never touches anything else — once the bot is live, "created today" includes real
customers.

The write tests create a lead, an interaction, a renewal (lead + case) and a complaint,
assert every field and lookup landed correctly, then delete them and restore any
counters they touched.

---

## Layout

```
chatbot/
  main.py                  FastAPI app + the three webhooks + admin endpoints
  agent.py                 agent factory, middleware, streaming query loop
  state.py                 AgentContext passed to middleware
  client/
    config.py              env + LLM factory (azure_openai | anthropic | gemini)
    channel_config.py      per-channel prompt, tools, limits   ← edit this
    store.py               SQLite session store (replaces Redis)
    prompt_loader.py       prompts/*.md with mtime hot-reload
    dataverse_client.py    async EF Dataverse OData client
    base_connection.py     session ids, verification, truncation
    whatsapp_connection.py / facebook_connection.py / instagram_connection.py
    session_logger.py      verbose per-session tracing (APP_ENV=development)
    ef_schema.py           schema-driven names, choices, binds, date formats
    interaction_logger.py  idle-triggered ef_interaction writer
  tools/
    __init__.py            tool registry
    ef_tools.py            the 5 tools the agent can call      ← edit this
    ef_crm.py              EF business logic over Dataverse    ← edit this
prompts/                   whatsapp.md, instagram.md, facebook.md   ← edit these
ef_schema_reference.json   the EF Dataverse model (drives ef_schema.py)
scripts/local_chat.py      terminal chat, no webhooks needed
tests/                     store, agent and webhook tests (no API key needed)
data/ef_chat.db            SQLite state (gitignored)
```

---

## Deploying

`.github/workflows/deploy.yml` builds the image, pushes it to GHCR and repoints the
Azure App Service on every push to `main`. Full one-time setup — plan, app settings,
Always On, publish profile — is in [DEPLOY.md](DEPLOY.md).

Two non-negotiables there: the SQLite file must live on `/home` (persistent) and the
app must run on **one instance only**. Both are explained in that file.

---

## Operating it

```bash
./tests/run_all.sh                                   # all tests, offline
curl localhost:8000/health
curl -H "X-Admin-API-Key: $ADMIN_API_KEY" localhost:8000/admin/diagnostics
curl -H "X-Admin-API-Key: $ADMIN_API_KEY" localhost:8000/session/<id>/transcript/formatted
curl -X DELETE -H "X-Admin-API-Key: $ADMIN_API_KEY" localhost:8000/session/<id>
APP_ENV=development python chatbot/main.py           # writes logs/sessions/<id>.log
```

Session ids are `<channel>:<identifier>:<YYYY-MM-DD>` — a new day starts a fresh session and
carries over the previous day's summary. Sessions idle past `SESSION_TTL_DAYS` are purged hourly.

**One replica.** State is a local SQLite file, so run a single instance with a mounted volume
at `data/`. Horizontal scale means swapping `client/store.py` for Redis — nothing else changes.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Prompt 'x' is empty or missing` | no `prompts/x.md`, or the channel's `prompt_id` is wrong |
| `Unknown tool 'x' — not in the registry` | listed in `channel_config.py` but not in `REGISTRY` |
| 401 from the LLM | `AZURE_LLM_API_KEY` unset or wrong |
| 404 `DeploymentNotFound` / `Resource not found` | `AZURE_LLM_ENDPOINT` must be the **base** URL ending in `/openai/v1` — not the `/openai/responses?api-version=…` URL — and `AZURE_LLM_MODEL` must name a real deployment (`gpt-5.6-sol`, `gpt-5.4-mini`) |
| Webhook verifies, then live messages 404 | Meta's callback path differs from the verified one; both `/facebook` and `/webhook/facebook` are served, so restart the app if you added the alias after starting |
| Model rejects `temperature` | gpt-5.x only accepts the default — leave `LLM_TEMPERATURE` empty |
| Webhook verification fails | the token Meta sends must equal `*_VERIFY_TOKEN` in `.env` |
| Replies never arrive | check `is_configured()` in `/health`; a missing page token fails silently by design |
| `Dataverse is not configured` | set `DATAVERSE_URL` / `TENANT_ID` / `CLIENT_ID` / `CLIENT_SECRET` |
| `undeclared property 'ef_lead'` | a lookup was bound by attribute name; use `S.bind_key(table, attribute)`, which resolves the PascalCase navigation property |
| `Cannot convert ... to Edm.Date` | a DateOnly column got a full timestamp; use `S.datetime_value(table, column, dt)` |
| Interactions never appear in the CRM | nobody was identified, so they are held for `INTERACTION_ORPHAN_HOURS`; check `/admin/diagnostics` |
# EF-ChatAgent

---

## AMC renewal reminders (proactive WhatsApp)

WhatsApp only lets you send free-form text inside 24 hours of the customer's last
message. A renewal reminder is by definition outside that window, so it has to go out
as a **pre-approved template** — the model cannot write these. Once the customer
replies, a fresh 24-hour window opens and the normal agent takes over.

```
12:00 IST daily  →  find_due()  →  AMC contracts landing exactly on a milestone
                                    │
                    send approved template for that milestone
                                    │
                    customer replies  →  24h window opens  →  agent + prompt
                                                              start_amc_renewal
```

### The ladder

Each customer is messaged on six days, not daily. `chatbot/client/amc_templates.py`:

| Day | Template | Category |
|---:|---|---|
| −30 | `amc_expiry_30d` | UTILITY |
| −15 | `amc_expiry_15d` | UTILITY |
| −7 | `amc_renewal_offer_7d` | MARKETING — 10% off |
| −1 | `amc_expiry_tomorrow` | UTILITY |
| +3 | `amc_lapsed_offer` | MARKETING — 10% off |
| +15 | `amc_lapsed_final` | MARKETING — 10% off |

UTILITY is about a service the customer already holds: cheaper, no marketing opt-in,
and outside Meta's per-user marketing cap. Copy that promotes the discount is
MARKETING and carries all three costs, which is why only three of the six do.

### Safety rails

Four things stop this becoming a way to lose the number:

- **Off by default.** `AMC_REMINDERS_ENABLED=false` unless set otherwise.
- **Allowlist or nothing.** With `AMC_REMINDER_ALLOWLIST` empty the job refuses to
  send at all; reaching the whole CRM needs `AMC_REMINDER_ALLOW_FULL_CRM=true`. The
  demo org is full of real-looking mobile numbers.
- **Send-once.** Every (contract, milestone) pair is recorded *before* the send, so a
  crash or a restart cannot duplicate it. Duplicate contract records — this org has
  them — are collapsed per (number, template) within a run.
- **STOP is honoured before the agent runs.** `main.py` intercepts opt-outs and
  replies with one fixed line rather than letting the model improvise.

### Running it

```bash
# submit the six templates to Meta for approval (needs WHATSAPP_WABA_ID)
.venv/bin/python scripts/submit_whatsapp_templates.py            # status
.venv/bin/python scripts/submit_whatsapp_templates.py --submit   # create

# who would be messaged, sending nothing. as_of shifts the run date, which is the
# only practical way to test — real milestones land on a handful of days a year.
curl -H "X-Admin-API-Key: $KEY" \
  "localhost:8000/admin/amc-reminders/preview?as_of=2026-08-25"

# send now, off-schedule
curl -X POST -H "X-Admin-API-Key: $KEY" localhost:8000/admin/amc-reminders/run
```

### The renewal conversation

The reminder only opens the door. Everything after it is a normal conversation the
agent drives with short follow-up questions — no buttons, no menus.

```
reminder template  →  customer replies  →  24h window opens
                                             │
                        get_renewal_plans ───┤  what is available for THEIR appliance
                        objection playbook ──┤  price, local technician, not using it
                        troubleshooting ─────┤  filter due, no power, leak, alarm
                                             │
                 start_amc_renewal  ·  escalate_to_human  ·  log_renewal_outcome
```

**Prices come from one place.** `chatbot/client/amc_plans.py` is the only source of a
rupee figure, and `get_renewal_plans` is the only way the agent can reach it. The
model never does arithmetic on a price — the discount is applied in code.

The shipped figures are placeholders from a campaign mockup, so `AMC_PRICES_CONFIRMED`
defaults to `false`. While it is false the agent names the plans and says what each
covers, states the discount as a percentage, and refuses to quote an amount:

```
false → "1-year or 2-year AMC for service visits and labour, or a 2-year CMC that
         also covers parts and filters. All have 10% off. Exact prices aren't
         confirmed here; our team will confirm the amount."

true  → "1-year AMC: ₹2,249 after 10% off. 2-year AMC: ₹4,049. 2-year CMC: ₹6,299,
         including spare parts and filter cartridges."
```

Replace the figures with EF's real card rates before flipping the flag.

**Payment happens through one link, or not at all.** `STRIPE_PAYMENT_LINK` is injected
into the WhatsApp prompt as `{{payment_link}}`; unset, the agent is told there is no
link and instructed not to invent one, which is the pre-existing behaviour. The agent
may never ask for card/UPI details itself, and may never say a renewal is active on
the strength of having sent the link — it is active when the payment clears.

**Escalation is not a failure.** `escalate_to_human` sets the Escalated disposition
and records the reason, tells the customer someone will call, and offers the support
number. A customer who wants a person and is made to argue with a bot is one we lose.

### Campaign offers

`chatbot/client/offers.py` holds the live campaign — discount, end date, terms. It is
injected into every prompt as `{{current_offer}}` rather than fetched by a tool, so
the agent always knows it, and **it expires on its own end date**: past `OFFER_ENDS_ON`
the agent is told there is no offer and instructed not to invent one. A bot still
promoting a dead campaign is a promise you have to honour or explain.

The offer deliberately applies to everything, and the prompt forbids asking which
product someone wants before naming it — making people choose first loses whoever has
not decided yet.

**Never hardcode a discount in template copy.** Deleting a template to fix wrong copy
reserves its name for **four weeks** (`error_subcode 2388023`), so the offer templates
take the discount and end date as `{{n}}` parameters and survive the next campaign
without a review cycle. Anything else means new names — see the `_v2` suffixes.

### New callers become customers

`ef_servicerequest` binds only to `ef_customer`; there is no lead lookup. So a first-time
caller with a broken appliance could not have a case raised at all. `ef_crm.ensure_customer`
creates the record (idempotent on phone) and marks the lead Converted, which is what makes
this work:

```
>>> hi my water purifier is not giving water properly
<<< Hi! I'll help you with the water flow issue. May I know your name?
>>> Meera
<<< Reduced water flow is often due to the cartridge. When was it last changed?
>>> i changed filter maybe 8 months back
<<< I've logged a technician check under CASE-001036.
    I can book today 2–5 PM, today 5–7 PM, or Friday 8–11 AM. Which suits you?
>>> friday morning works
<<< Booked for Friday, 28 August, 8–11 AM. The technician will call before arriving.
```

Service hours are **Monday–Friday, 8 AM–7 PM**, in four windows (8–11, 11–2, 2–5, 5–7).

### Purchase interest

`register_purchase_interest` logs buying intent and the agent replies "I've notified our
team and they will call you shortly." A D365 Service Activity (`serviceappointment`) is
**not** used: its `regardingobjectid` cannot target the custom EF tables, and records do
not persist in this org because Service Scheduling is not provisioned. The work item goes
on tables that do link — a `Query` service request for a known customer, the lead record
otherwise.

### Brochure

`send_offer_brochure` uploads `eureka-forbes-offer-brochure.pdf` once, caches the media id
for 25 days (Meta expires them at 30), and sends it as a document. The cache key includes
the sending number, because a media id is scoped to the number that uploaded it. The prompt
forbids describing the contents — the agent has not read it.

### Demoing it

`scripts/demo.py` runs the flows in a terminal against the real agent, real prompt,
real tools and real Dataverse — everything except the WhatsApp transport. An expired
token, a dead tunnel or a template still in review cannot spoil it.

```bash
.venv/bin/python scripts/demo.py --list
.venv/bin/python scripts/demo.py renewal --slow     # types at reading pace
.venv/bin/python scripts/demo.py --all --slow
```

Each scenario prints the conversation and then what reached the CRM. It creates real
records — clean up with `scripts/cleanup_test_records.py` afterwards.

Live WhatsApp demos additionally need: a **System User** token (the API Setup token
expires in ~24h and dies mid-demo), a tunnel URL that matches what is saved in Meta,
and — for proactive reminders — approved templates.
