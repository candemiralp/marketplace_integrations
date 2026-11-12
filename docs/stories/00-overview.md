# bol.com × ERPNext Integration — Project Overview

Read this file before every story. It holds the context, architecture and the
engineering standards that every story must meet. Stories (`01-…` to `15-…`)
are implemented one at a time, in order, each as its own branch/PR.

## 1. Context

- Magnatlas sells machine-embroidered apparel, bags and children's products.
- Today: Shopify storefront → `ecommerce_integrations` → ERPNext (stock,
  accounting, invoicing). PostNL labels are printed from a Shopify app. bol.com
  orders flow bol → paid Shopify app → Shopify → ERPNext.
- Target: **ERPNext is the hub.** bol.com connects directly to ERPNext. The
  Shopify↔bol app is retired. Shopify stays on `ecommerce_integrations`.
- Fulfilment: **FBR only** (Fulfilment by Retailer). FBB/LVB is out of scope.

```
Shopify ──(ecommerce_integrations)──┐
                                    ▼
bol.com ◄──(marketplace_integrations/bol)──► ERPNext ── Sales Order → Delivery Note → Sales Invoice
   ▲                                    │
   └──── VVB labels / shipment confirm ─┘
```

## 2. Technical baseline

| Item | Value |
|---|---|
| App | `marketplace_integrations` (new Frappe app), module `Bol` |
| Depends on | `erpnext`, `ecommerce_integrations` (reuse `Ecommerce Item`, `Ecommerce Integration Log`) |
| Frappe/ERPNext | v15+ — **run `bench version` first** and use the matching APIs (test base classes, `enqueue(deduplicate=…)`, etc.) |
| Python | the bench's version (3.11+) |
| HTTP | `requests` (already in Frappe env). No new runtime dependencies without justification. |
| Crypto | `cryptography` (already in Frappe env) for webhook signatures |

### bol API resources used

bol versions **per resource**. Media type pattern: `application/vnd.retailer.v{N}+json`.

| Resource | Version | Used in |
|---|---|---|
| Auth (OAuth2 client credentials) | — | 02 |
| Process status | v10 (`/retailer/process-status/{id}`) | 03 |
| Economic Operators | v1 (check media type in its docs) | 04 |
| **Offers** | **v11** (v10 is deprecated; removed 2027-02-01) | 04, 05, 06 |
| Orders, Shipments, Transports | v10 | 07, 08, 10 |
| Shipping Labels (VVB) | v10 | 09 |
| Returns | v10 | 11 |
| Invoices | v10 | 12 |
| Subscriptions (push) | v10 | 13 |
| Product Content | v10 | 14 |

Docs: <https://api.bol.com/retailer/public/Retailer-API/index.html>
(functional guides, Redoc, OpenAPI spec, Postman, demo environment).

**Rule for Claude Code:** the functional docs often omit exact paths and fields.
Before implementing a story, download the OpenAPI spec for the resource(s) it
uses into `docs/bol-openapi/` (once) and confirm every path, field name, enum
and media type against it. Where this spec says "verify", the spec wins. Write
down any differences you find in `docs/decisions.md`.

## 3. Code layout

```
marketplace_integrations/
  hooks.py
  install.py                 # after_install / after_migrate: role + custom fields
  setup/custom_fields.py     # single dict of all custom fields; each story extends it
  bol/                       # Frappe module "Bol"
    api/
      auth.py                # token fetch + cache
      client.py              # BolClient: one request() method, retries, errors
      errors.py              # exception taxonomy
    log.py                   # thin adapter over Ecommerce Integration Log
    process_status.py        # async process tracker (story 03)
    mappers/                 # PURE functions: bol JSON <-> plain dicts. No frappe imports.
    offers.py  stock.py  orders.py  shipments.py  labels.py
    cancellations.py  returns.py  invoices.py  webhooks.py  content.py
    doctype/…
    tests/
      fixtures/*.json        # payloads copied from Redoc examples
      test_*.py
```

## 4. Engineering standards (apply to every story)

### 4.1 Error handling — must be airtight

**Exception taxonomy** (`bol/api/errors.py`), all subclass `BolError`:

| Class | Cause | Retry? |
|---|---|---|
| `BolTransientError` | connect error, 502/503/504 | yes |
| `BolRateLimitError` | 429 | yes, after `Retry-After` |
| `BolAuthError` | token failure, 401 after one refresh, 403 | no |
| `BolValidationError` | 400/422; carries parsed `violations` | no — data fix needed |
| `BolNotFoundError` | 404 | no — caller decides |
| `BolServerError` | other 5xx | yes (limited) |
| `BolMappingError` | our data can't be mapped (unknown EAN, missing price…) | no |

Rules:
1. **Retry only when it's safe.** Retry `GET`/`PATCH`/`PUT`/`DELETE` on transient
   errors. Retry `POST` only when the request certainly wasn't processed (connect
   error, 429, 503). After a **read timeout on a POST** the outcome is unknown:
   don't retry. Raise `BolTransientError(ambiguous=True)` and let the caller
   check bol's state first (each story defines how).
2. In-call retries: max 3, exponential backoff with jitter (1s, 2s, 4s); a 429
   uses `Retry-After` (capped at 60s). When retries run out, the background job
   fails and the scheduler picks the record up again on its next run.
3. **Every inbound or outbound operation is idempotent.** Natural keys (bol order ID,
   order item ID, RMA ID, invoice ID, process status ID) are stored in **unique,
   `no_copy`** fields. Treat a `frappe.DuplicateEntryError` on insert as
   "already done", not as a failure.
4. **No partial documents.** Each record is processed inside a savepoint
   (`frappe.db.savepoint` / `rollback(save_point=…)`). Background jobs commit
   after each record so one bad record never blocks the others.
5. **No silent failures.** No bare `except`. Never `except Exception: pass`.
   Every failure is (a) written to `Ecommerce Integration Log` with request,
   response, traceback and a reference document, and (b) shown on the related
   document as a comment when there is one.
6. Don't spam the log with repeated failures: for a recurring failure on the
   same key, use a cache marker (`bol:failed:{kind}:{key}`, 6h TTL) so it's
   logged once while retries continue.
7. User-triggered actions (buttons) raise `frappe.ValidationError` with a clear,
   translated (`_()`) message. They never show raw tracebacks.
8. Async bol operations (HTTP 202 + process status) are never "fire and forget":
   they are tracked to a final state (story 03).

### 4.2 Security

- Secrets go only in `Password` fields, read with `settings.get_password()`.
  Never log, print, return or cache the client secret. The token lives only in
  Redis with a TTL.
- Logging redacts `Authorization`, `access_token`, `client_secret`. Integration
  logs contain buyer PII, so their read permission is restricted to System
  Manager and Bol Manager.
- TLS verification always on. Explicit timeouts on every call (connect 5s, read 30s).
- Whitelisted methods check permissions explicitly (`frappe.has_permission`/
  `frappe.only_for`) and validate input types. There is no `allow_guest`
  endpoint except the webhook in story 13, which is protected by signature
  verification.
- Never import code by a dotted path stored in the database. Callback registries
  are dicts defined in code.
- Queries use `frappe.get_all` / `frappe.qb`. No string-formatted SQL.
- Downloaded files (labels, invoices) are attached as **private** files.
- New role **Bol Manager** with least privilege. Settings and actions are restricted to it
  and to System Manager.

### 4.3 Frappe / ERPNext platform rules

- Custom fields are defined once in `setup/custom_fields.py` and applied with
  `create_custom_fields(…, update=True)` from `after_install` **and**
  `after_migrate` (idempotent). Don't edit DocType JSON files that belong to
  other apps. Don't use fixture exports for custom fields.
- Extend behaviour through `doc_events` and `scheduler_events` in `hooks.py`. Never
  monkey-patch ERPNext or `ecommerce_integrations`.
- Heavy work goes through `frappe.enqueue(…, enqueue_after_commit=True)` with a
  deterministic `job_id` plus deduplication. Never call bol inside a
  request/transaction that the user is waiting on, unless it's an explicit button.
- Scheduler jobs return early when `Bol Settings.enabled` or the specific
  feature toggle is off.
- Use ERPNext's own mapping functions (`make_delivery_note`, `make_sales_return`,
  `make_sales_invoice`, `update_child_qty_rate`) instead of building documents by hand.
- Money: `flt(…, precision)`. Datetimes: bol sends ISO-8601 with an offset; convert to
  the system timezone with a single helper.
- All user-facing strings wrapped in `_()`.
- Lint: `ruff` + `ruff format` + Frappe semgrep rules (`frappe/semgrep-rules`)
  in pre-commit.

### 4.4 Tests

- **Unit tests (no DB):** every mapper and the client. HTTP is mocked by
  injecting a fake session or patching `requests.Session.request`. Fixtures are
  real payloads taken from Redoc examples.
- **Integration tests (DB):** flows that create documents, with `BolClient`
  mocked. Use the bench's test base class (`FrappeTestCase` on v15,
  `IntegrationTestCase`/`UnitTestCase` on v16).
- Every error branch listed in a story's *Error handling* section has a test.
- No live API calls in tests. Live checks use the demo environment through the
  smoke script (story 15).
- Run: `bench --site <test-site> run-tests --app marketplace_integrations`.

### 4.5 Keep it lean

- No abstractions with a single implementation, no speculative settings, no
  "future-proof" hooks. Build what the story needs.
- Functions short and named after what they do. Comments explain *why*, not *what*.
- One client, one log adapter, one process tracker, reused everywhere.

## 5. Definition of Done (every story)

- [ ] Paths/fields verified against the OpenAPI spec; differences recorded in `docs/decisions.md`
- [ ] Acceptance criteria met
- [ ] Unit + integration tests written and passing; every listed error branch covered
- [ ] `ruff`, `ruff format`, semgrep clean
- [ ] `bench migrate` on an existing site is idempotent (run it twice)
- [ ] Feature toggle defaults to **off**; enabling it is the only thing needed to activate it
- [ ] Short "How to use / operate" page added to `docs/usage/` and linked from `README.md`

## 6. Story map

| # | Story | Depends on | Required? |
|---|---|---|---|
| 01 | App scaffold & foundations | — | yes |
| 02 | Settings, auth, HTTP client, logging | 01 | yes |
| 03 | Async process-status tracker | 02 | yes |
| 04 | Catalog mapping, EAN, economic operator, adopt existing offers | 02 | yes |
| 05 | Offer sync (create/update/price/hold/delete) | 04 | yes |
| 06 | Stock sync | 05 | yes |
| 07 | Order import | 04 | yes |
| 08 | Shipment confirmation | 03, 07 | yes |
| 09 | Verzenden via bol (VVB) labels | 03, 08 | if VVB is used |
| 10 | Cancellations | 03, 07 | yes |
| 11 | Returns | 03, 08 | yes |
| 12 | Accounting: sales invoices, bol invoices | 08, 11 | yes |
| 13 | Push notifications (webhooks) | 03, 05 | optional (polling is the baseline) |
| 14 | Product content upload | 03, 04 | optional (start with manual content) |
| 15 | Cutover, monitoring, hardening | all | yes |

## 7. Decisions to confirm before starting (owner: Magnatlas)

1. ERPNext/Frappe version (`bench version`).
2. Is the bol account registered for **Verzenden via bol**? (Registration is in
   the seller dashboard only.)
3. Warehouse for bol stock (same as Shopify?) and the stock buffer per SKU.
4. Tax templates for NL and BE buyers (OSS applies to BE); receivable account
   "Debtors – bol"; supplier "bol.com" for commission invoices.
5. Every sellable variant has a real GS1 EAN-13.
6. Economic operator(s) already exist in the bol seller dashboard.
