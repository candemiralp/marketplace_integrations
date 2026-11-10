# Decisions (ADR log)

Short records of decisions and of differences found between the stories and reality
(bol OpenAPI spec, installed apps). Newest at the bottom. Format: context → decision → consequences.

---

## ADR-001 — Reuse `Ecommerce Item` and `Ecommerce Integration Log` (story 01)

**Context.** Investigated on 2026-11-10 against the installed bench:
frappe 15.126.1, erpnext 15.124.5, ecommerce_integrations 1.20.3 (branch `version-15`), Python 3.11.18.
Test base class on this version: `frappe.tests.utils.FrappeTestCase`.

### Ecommerce Item

| Field | Type | Options | Notes |
|---|---|---|---|
| `erpnext_item_code` | Link | Item | reqd, read-only, indexed |
| `integration` | **Link** | **Module Def** | reqd, read-only, indexed |
| `integration_item_code` | Data | | reqd, read-only, indexed |
| `variant_id` | Data | | read-only |
| `has_variants` | Check | | read-only |
| `variant_of` | Link | Item | read-only |
| `sku` | Data | | read-only, indexed |
| `inventory_synced_on` | Datetime | | defaults to 1970-01-01 ("never synced") |
| `item_synced_on` | Datetime | | |

- Permissions: System Manager only. `track_changes` on.
- **`integration` is a Link to Module Def**, so our module name `Bol` works as is.
  **No Property Setter needed.** The `Bol` Module Def must exist on the site (see ADR-002).
- **No DB-level unique constraint.** All indexes are non-unique. `EcommerceItem.before_insert`
  checks for duplicates in the app and raises `frappe.DuplicateEntryError` when either of these exists:
  - (`integration`, `erpnext_item_code`, `integration_item_code`[, `variant_id`]), or
  - (`integration`, `sku`) when `sku` is set.

  (`integration`, `integration_item_code`) alone is **not** unique: the same code can point to
  two ERPNext items. The check is also exists-then-insert, so two concurrent inserts can both pass.
- Helpers in `ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item.ecommerce_item`:
  `is_synced`, `get_erpnext_item_code`, `get_erpnext_item`, `create_ecommerce_item`.
  (`create_ecommerce_item` also *creates an ERPNext Item*, which we don't want for bol: we map
  existing items.)

### Ecommerce Integration Log

| Field | Type | Notes |
|---|---|---|
| `title` | Data | set from `message`, else from the last part of `method`; truncated to 100 chars |
| `integration` | Link → Module Def | not read-only, not required |
| `status` | Data | free text (ecommerce_integrations uses "Queued", "Success", "Error") |
| `method` | Small Text | |
| `message`, `traceback`, `request_data`, `response_data` | Code | |

- Permissions: Administrator and System Manager only. `in_create` (no manual creation).
- **No reference-document fields** (no `reference_doctype` / `reference_name`).
- `EcommerceIntegrationLog.clear_old_logs(days=90)` deletes only `Success` logs.
- `resync`/`bulk_retry` re-enqueue only methods that start with `ecommerce_integrations.`, so they
  won't retry our jobs. That's harmless.

`create_log` lives at
`ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log.create_log`:

```python
def create_log(module_def=None, status="Queued", response_data=None, request_data=None,
               exception=None, rollback=False, method=None, message=None, make_new=False)
```

Behaviour that matters for the story-02 adapter (`bol/log.py`):
- It **always calls `frappe.db.commit()`**. Whatever is pending in the caller's transaction gets
  committed with the log.
- `rollback=True` calls `frappe.db.rollback()`, which rolls back the **whole** transaction, not a savepoint.
- Without `make_new=True`, when `frappe.flags.request_id` is set it **updates that existing log**
  instead of creating a new one.
- `traceback` is filled from `frappe.get_traceback()` (the exception being handled), and only if
  it's empty. Dicts passed as request/response data are JSON-dumped.

**Decision.** Use both DocTypes. Don't create `Bol Item Map` / `Bol Sync Log`.

**Consequences / open items for later stories.**
- Story 02 (`bol/log.py`): roll back to the record's savepoint *before* logging, never pass
  `rollback=True`, always pass `make_new=True`, and redact secrets before passing data.
  Two gaps in the overview's requirements need a solution in story 02. Reference document: add
  `bol_reference_doctype`/`bol_reference_name` custom fields, or rely on the related document's
  comment. Read access for Bol Manager: a Custom DocPerm on the log.
- Story 04: we can't rely on the database for Ecommerce Item uniqueness, and we don't add indexes
  to another app's table. Writes to Ecommerce Item for `Bol` must be serialised (single
  deduplicated job), and duplicates (`DuplicateEntryError`) are treated as "already mapped".

---

## ADR-002 — Module `Bol`; Module Defs are synced on migrate (story 01)

**Context.** `bench new-app` created the default module "Marketplace Integrations", and the app was
already installed on the dev and test sites. `install-app` creates Module Defs from `modules.txt`
(`frappe.installer.add_module_defs`), but **`bench migrate` doesn't**, so renaming the module to
`Bol` would leave existing sites without a `Bol` Module Def. Ecommerce Item and the integration log
link to it.

**Decision.** `modules.txt` contains only `Bol`. `install.after_migrate` calls
`add_module_defs("marketplace_integrations", ignore_if_duplicate=True)`, which is idempotent.
The orphaned "Marketplace Integrations" Module Def was deleted by hand on the two local sites.
No production site ever had it, so no patch is needed.

---

## ADR-003 — Lint tooling (story 01)

- Ruff rules are `E, F, I, UP, B, SIM` with line length 110, as the story asks. Frappe's generated
  ignore list is kept (`E501` among them: `ruff format` enforces the line length; long strings are left alone).
  Tabs for indentation (Frappe style).
- `frappe/semgrep-rules` ships no pre-commit hook. A local hook (`.github/helper/frappe-semgrep.sh`)
  clones the rules once, at a pinned commit, into `~/.cache/frappe-semgrep-rules/<sha>`, and runs
  `semgrep` (pinned in `additional_dependencies`). To update: bump `RULES_SHA` / the semgrep pin.
  The CI `linter.yml` also runs semgrep on the rules' HEAD plus `r/python.lang.correctness`.
- The dev container installs `pre-commit` via `uv tool install` (dev-env Dockerfile).
- CI is pinned to `version-15` for frappe, erpnext and ecommerce_integrations, with Python 3.11 and
  Node 20 to match the dev image.

---

## ADR-004 — bol auth, base paths and error format, checked against the spec (story 02)

**Context.** Checked on 2026-11-10 against the Retailer API v10 OpenAPI spec
(`docs/bol-openapi/retailer-api-v10.json`), the Authentication and Demo pages, and the
process-status v10, offers v11 and economic-operators v1 definitions.

| Story / overview says | Spec / docs say | Decision |
|---|---|---|
| `POST https://login.bol.com/token?grant_type=client_credentials` | `grant_type=client_credentials` as a **form body** parameter, `Content-Type: application/x-www-form-urlencoded`, Basic auth, `Accept: application/json` | Send it as form data. Bad credentials give **401** (confirmed live: `{"error":"invalid_client"}`). `expires_in` is 299 s. bol IP-bans clients that fetch a token per call, so the token is cached for `expires_in - 30`. |
| Base URLs `…/retailer` and `…/retailer-demo` | Demo mirrors **each API root**: `/retailer-demo/…`, `/shared-demo/…` | `BolClient.request` takes the full spec path (`/retailer/orders`) and, in Demo, appends `-demo` to the first segment. Code paths read exactly like the spec. |
| Process status at `/retailer/process-status/{id}` (overview §2) | **`/shared/process-status/{id}`** (process-status v10) | Story 03 uses `/shared/process-status/{id}`. |
| Media type `application/vnd.retailer.v{N}+json` | Economic operators use **`application/vnd.economic-operator.v1+json`** | Story 04 passes it as `accept`. When `accept` is a JSON type, Content-Type follows it. |
| Errors as `application/vnd.problem+json` | Error bodies use the resource's media type (`application/vnd.retailer.v10+json`). The Problem has `type, title, status, detail, host, instance, violations[{name, reason}]` | The client parses any JSON error body as a Problem. |
| — | The spec documents only 200/202/207/400/404/406/415 | 401/403/429/5xx handling follows the functional docs and overview §4.1. Any other 4xx (406, 415, …) raises a plain `BolError` and isn't retried. |
| Test connection: orders list with `page=1` | The demo only answers its hard-coded requests | Test connection sends `GET /retailer/orders?fulfilment-method=FBR&status=OPEN` (a demo example; page 1 is the default). |

---

## ADR-005 — Logging adapter: deferred writes, callers log failures (story 02)

**Context.** See ADR-001: `create_log` always commits, takes the traceback from the exception
currently being handled, and has no reference fields. A commit while a record is being processed
persists half-finished work and drops the record's savepoint (overview §4.1 rule 4). Logging every
failure inside the client would also defeat `log_once` (rule 6).

**Decision.**
- `bol/log.py` builds the `Ecommerce Integration Log` doc itself (same field semantics as
  `create_log`: JSON dumped with `sort_keys`, `indent=4`; status `Success`/`Error`) and **writes it
  when the caller's transaction ends**. It queues the entry on `frappe.local` and registers one
  flush on `frappe.db.after_commit` and `after_rollback`. Frappe v15's `commit()` clears
  `after_rollback` and `rollback()` clears `after_commit`, so the flush runs once. It inserts the
  logs (`ignore_links`, because the referenced document may have been rolled back) and commits only
  them. The result: logs survive a rollback, logging never commits the caller's work, and a
  savepoint rollback leaves queued logs in place. This is the one deviation from the story, which
  says to go through `create_log`.
- The traceback comes from the error object, not from `sys.exc_info()`.
- Reference document: custom fields `bol_reference_doctype` / `bol_reference_name` on Ecommerce
  Integration Log. `reference=(doctype, name)` everywhere.
- **The client logs successful calls** (non-GET always, GET only with *Debug Logging*). **Callers
  log failures** after rolling back: `log_call(**err.details, error=err, reference=…)`, or
  `log_once(kind, key, …)` for recurring failures. `BolError.details` uses `log_call`'s keyword
  names and is already redacted.
- Bol Manager read access to the log: `install.ensure_log_permission()` calls
  `frappe.permissions.add_permission`, which first copies the standard perms to Custom DocPerm.
  Consequence: later upstream changes to that DocType's standard perms no longer apply on our sites.
  Re-check after upgrading `ecommerce_integrations`.
- Test site: `test_bol_settings.py` sets `test_ignore = ["Company"]`. Without it, the runner builds
  ERPNext Company test records, which need ERPNext's full test setup (`Warehouse Type: Transit`).

---

## ADR-006 — Process-status tracker (story 03)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/process-status-v10.yaml` (Shared API
10.0.1) and the functional and demo pages for process status.

| Story says | Spec / docs say | Decision |
|---|---|---|
| `GET /retailer/process-status/{id}` | **`/shared/process-status/{process-status-id}`** (also ADR-004) | Use the spec path. |
| Bulk `POST /retailer/process-status` "if the spec confirms it" | `POST /shared/process-status`, body `{"processStatusQueries": [{"processStatusId": "…"}]}` (1–1000), response `{"processStatuses": [ProcessStatus…]}` | Every poll uses the bulk call (100 rows per run). |
| Response fields | Confirmed: `processStatusId` (string, nullable), `entityId`, `eventType`, `description`, `status` ∈ `PENDING, SUCCESS, FAILURE, TIMEOUT`, `errorMessage`, `createTimestamp`, `links` | Rows are matched by `processStatusId`, stored as a string. |
| — | Statuses are kept **for at most 1 day**. Afterwards the single GET returns 404 and the bulk call leaves the id out | An id missing from the bulk response counts as "no news": the row backs off, and the 24h timeout ends it. |

**Decisions.**
- **`BolClient.request(..., safe=True)`** for a POST that only reads. Its read timeouts and 502/504s
  aren't ambiguous (they are retried), and its successes are logged like a GET (only with Debug
  Logging). Without it the bulk poll would write a Success log every minute.
- **Extra field `bol_status`** keeps bol's final status. `status` becomes `HANDLER_ERROR` when a
  handler raises, and Retry handler needs to know whether to call `on_success` or `on_failure`.
- **The cron hook calls `poll_pending` directly; there's no wrapper with
  `enqueue(job_id="bol_poll_process_status", deduplicate=True)`.** Frappe v15's scheduler already
  enqueues every scheduled job with `job_id="scheduled_job::<method>"` and skips it while that job is
  queued or running (`ScheduledJobType.enqueue`), so runs can't overlap. A wrapper would only add a
  second job every minute.
- **Name = `processStatusId`** (`autoname: field:process_status_id`, also `unique`). Duplicate
  `track()` returns the existing row; a concurrent `DuplicateEntryError` counts as already tracked.
- **`HANDLER_MODULES`** in `process_status.py` lists the modules that call `register()`. A
  background poll only imports `process_status`, so these modules are imported before any handler
  lookup. It's a tuple in code, never read from the database. Each story that adds a handler adds its module.
- Failure visibility: a FAILURE/TIMEOUT is logged and commented on the reference document *before*
  `on_failure` runs. A handler exception is logged, commented on the reference document and on the
  Bol Process, and rolled back to a savepoint just before the handler.
- `retry_handler` checks roles with `frappe.get_roles()` instead of `frappe.only_for`, because
  `only_for` lets everyone through when `frappe.flags.in_test` is set, so the check couldn't be tested.
- Housekeeping (daily) deletes SUCCESS/FAILURE/TIMEOUT rows not modified for 90 days, and their
  Comments. It keeps `HANDLER_ERROR` rows because someone still has to act on them.
- There's no separate feature toggle: the tracker only does work for other features, and its jobs return
  early while `Bol Settings.enabled` is off.
- Comment helper `log.add_comment(reference, text)` for all stories. It does nothing if the document
  no longer exists.

---

## ADR-007 — Catalog mapping, economic operators, adopting offers (story 04)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/offers-v11.yaml` (Offers API 11.5.0) and
`docs/bol-openapi/economic-operators-v1.yaml` (Economic Operator API 1.1.1), both from
`https://api.bol.com/registry/api-definitions/…`, and against ERPNext 15.124.5.

| Story says | Spec / ERPNext says | Decision |
|---|---|---|
| Economic operators: list and get one | List is **`GET /retailer/economic-operators`** (`name`, `page`, `page-size` 25–100) → `{operators[], page{pageNumber, pageSize, numberOfPages, totalCount}}`. Get one is **`GET /retailer/economic-operator/{id}`** (singular) | Page through the list until `pageNumber >= numberOfPages`. The spec lists 404 for the list: treated as "no operators". |
| Fields `economicOperatorId`, `name`, `status` | The operator's id field is **`id`** (string UUID). `status` ∈ `PENDING, VALID, INVALID` | Return `{id, name, status}`. |
| — | Media type `application/vnd.economic-operator.v1+json` (ADR-004) | Passed as `accept`. |
| Offers v11 list filters `eans, reference, for-sale, page-size, cursor` | Also `offer-ids`, `last-modified-date-time`. `page-size` 1–100. Response `{offers[], page{pageSize, nextCursor}}` | Follow `page.nextCursor` until it's null or empty. |
| `bol_offer_status` "For sale / Not for sale / On hold" | v11 offers have **no status field**: only `onHoldByRetailer` and `countryAvailabilities[{countryCode, forSale}]` | `mappers/offer.offer_status`: on hold → `On hold`; for sale in any country → `For sale`; else `Not for sale`. |
| `bol_status_detail` = not-for-sale reasons | Reasons come from a separate `GET /retailer/offers/{id}/not-for-sale-reasons` | Adopting doesn't fetch them (one call per offer). Story 05 fills the detail when it refreshes the status. |
| Item `bol_min_days`/`bol_max_days`: blank = Settings default | Int custom fields are `NOT NULL DEFAULT 0` | **0 means "use the Settings default"**. A promise of 0 days can still be set in Bol Settings. |
| Duplicate EAN across Items | ERPNext's `Item Barcode.barcode` is **unique**, so the same string can't be on two Items | A "duplicate" is the same GTIN written differently: UPC-A (12 digits), EAN-13, GTIN-14 (GS1 treats leading zeros as insignificant). Lookup matches all zero-padded forms (`mappers/ean.gtin_forms`); more than one Item → reported as ambiguous. This also matches UPC-A barcodes from Shopify. |
| `frappe.only_for([...])` | `only_for` lets everyone through when `frappe.flags.in_test` is set (ADR-006) | `bol/permissions.check_bol_role()` checks `frappe.get_roles()`. Bol Process' Retry handler uses it too. |
| — | The offers spec has no response `example` for the list | Test fixtures are built from the schema's field examples. |

**Decisions.**
- **Item validation** (`catalog.validate_item`, Item `validate` hook) runs on every save while
  `bol_enabled` is ticked, not only when it's ticked. It lists every problem at once. Adopted items
  that aren't publishable yet (no operator on the Brand, a UPC-A barcode) can't be saved until they're
  fixed or unticked. That's deliberate: story 05 can't publish them either.
- `get_item_ean` is pure, so it can't translate. `BolMappingError` gets a `reason` code
  (`missing | several | invalid`) and the hook shows its own translated message for each.
- **Brand operator**: `bol_economic_operator_id` is read-only and set only through
  `set_brand_economic_operator`. It checks the id at bol (`GET` one operator) and rejects anything
  that isn't UUID-like before it reaches the URL path. It saves the Brand as a document
  (`save(ignore_permissions=True)`), so Brand hooks (story 05) see the change, and a Bol Manager
  without Brand write permission can set just this field. Brand read permission is still required.
  An operator that isn't `VALID` is saved and the form shows a warning.
- **Adopt existing offers** (`offers.adopt_existing_offers`), a long job, one at a time
  (`job_id="bol_adopt_existing_offers"`). With `enqueue_after_commit=True`, `frappe.enqueue` always
  returns `None`, so "already running" is checked with `is_job_enqueued` first.
  - It only sends GETs to bol. Each offer is processed inside a savepoint and committed on its own.
  - Upsert keyed on the offerId. An offer already mapped to another Item, or an Item/EAN already mapped
    to another offer, is a **conflict**: reported and left unchanged. `DuplicateEntryError` on insert
    counts as already mapped (ADR-001: Ecommerce Item has no DB unique constraint).
  - `bol_enabled` and `bol_offer_status` are written with `frappe.db.set_value(update_modified=False)`.
    That skips Item validation and doc_events on purpose, so adopting never triggers an offer push.
  - The result is **one** integration log entry (Success, or Error if paging failed) whose response
    holds the counts and the lists of unmatched, ambiguous, conflicting and failed offers. On a paging
    error it also holds the pages done, the last cursor and bol's response, and the job then fails. An
    offer that raises is rolled back, gets its own Error log and is counted as failed. The user who
    started the job gets a realtime `msgprint`.
  - `log_call` gained an optional `message` to replace its default one-line summary.
- `Brand.bol_economic_operator_id`, `Item.bol_offer_status`/`bol_status_detail`/`bol_enabled` and
  `Ecommerce Item.bol_last_stock_pushed` are `no_copy`. A duplicated Item must not inherit bol state.
- **Checked live on the demo environment** (read-only GETs): both list paths, the EO media type and
  the offer fields above are as the spec says. The demo ignores `page-size` (it always answers 25).
  It also returns the **same `nextCursor` on every page** and the same operator on all 4 "pages".
  (It is the spec's `Cursor` example, trailing comma included.) When bol repeats the cursor it was
  just given:
  - **Production**: the adopt job stops with an Error log. Following the cursor would loop forever,
    and stopping without an error could hide offers.
  - **Demo**: the repeat is treated as the last page, so adopting finishes normally there.
  Offers are de-duplicated by `offerId` within a run: one seen again is skipped and only counted in
  the report's `repeated`, so the outcome counts stay correct. Operators are de-duplicated by `id`.

---

## ADR-008 — Offer sync (story 05)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/offers-v11.yaml` (Offers API 11.5.0) and the
demo environment.

| Story says | Spec / demo says | Decision |
|---|---|---|
| Create returns `offerId` (201) | `POST /retailer/offers` → 201 `RetailerOffer {offerId}`. `CreateOfferRequest` requires `ean, condition, pricing, fulfilment`; `stock` (`CreateStock`) requires `amount, managedByRetailer` | Send `condition: {category: NEW}` and `stock: {amount: 0, managedByRetailer: false}` (story 06 sets the real stock, with `false` as story 06 decides). A create answer **without `offerId`** counts as ambiguous: the next run's EAN lookup finds the offer. |
| Update (PATCH) | `PATCH /retailer/offers/{id}` → 204. `PatchOfferRequest`: `economicOperatorId, onHoldByRetailer, reference, countryAvailabilities, pricing, fulfilment, stock, unknownProductTitle`. **`ean` and `condition` can't be patched** | `diff_offer` compares only `economicOperatorId, onHoldByRetailer, reference, pricing, fulfilment`. `stock` belongs to story 06. |
| Delivery schedule `MY_DELIVERY_PROMISE` / `BOL_DELIVERY_PROMISE` | `fulfilment.schedule` ∈ `BOL_DELIVERY_PROMISE, MY_DELIVERY_PROMISE, SHIPPING_VIA_BOL`; `deliveryPromise {minimumDaysToCustomer, maximumDaysToCustomer, ultimateOrderTime}`. The demo GET **echoes `schedule`** | Send `MY_DELIVERY_PROMISE` + min/max days. `schedule` is compared, so an offer on `BOL_DELIVERY_PROMISE` (e.g. adopted) is switched over. |
| `pricing.bundlePrices[]` 1–4, `unitPrice` 1–9999 | Confirmed; the unit price **includes VAT** | One bundle price (quantity 1), rounded to cents. The bol price list must hold VAT-inclusive EUR prices. |
| `reference` ≤ 100 chars, the item_code | Confirmed. ERPNext item codes can be 140 | Cut to 100 characters: it's informational only. |
| — | `countryAvailabilities` optional on create | Not sent: bol's default applies. |
| Delete → 204 | No 404 documented | A 404 on delete means "already gone": the mapping is removed anyway. |
| Not-for-sale reasons | `GET /retailer/offers/{id}/not-for-sale-reasons` → `{offerId, countries[{countryCode, reasons[{category, code, description, missingAttributes?}]}]}` | One line per reason in `bol_status_detail`: `NL: Price too high (4000-1)`. |
| Reconcile: "list `for-sale=false` first" | The list returns every offer's `onHoldByRetailer` and `countryAvailabilities` | **One unfiltered list pass** gives every status, at the same number of calls or fewer, and shows which mapped offers are gone at bol. Reasons are fetched only for `Not for sale` offers. |
| — | 409 documented on POST/PATCH | The client raises a plain `BolError` (no retry): logged, the job fails. |

**Demo environment (checked live).** GET (one offer, list, `eans=` filter, reasons) answers as the spec
says. **Writes can't be checked there**: every PATCH gets a bare HTTP 403 (empty body, same token that
POSTs fine), and POST and DELETE answer with a v10-style process-status example instead of the v11
response. On the demo a sync therefore ends in `Not synced` (create without `offerId`) or a 403 auth error
(PATCH). The flows are covered by tests against an in-memory fake of the endpoints.

**Decisions.**
- **Status.** `bol_offer_status` holds `For sale`, `Not for sale`, `On hold` (from bol), or ours: `Not synced`
  (with the reason in `bol_status_detail`, so the list view can filter on the status) and `Deleted`.
  Written with `db_set(update_modified=False)`. `db_set` runs `on_change`, never `on_update`, so writing
  the status never triggers another sync.
- **Hold needs nothing.** Desired state for an item that is unticked or disabled is `{"onHoldByRetailer": true}`
  only, so an item without a price or operator can still be taken off sale.
- **A live offer whose data broke is left alone** (owner's decision): no price, a price out of range or no
  operator gives `Not synced`, but the offer keeps its last price and stays on sale. Deleting and re-adding
  an Item Price never takes a listing offline in between. A missing price never sends 0.
- **Price** comes from ERPNext's `get_item_price` (stock UOM, no customer-specific rows, valid today).
- **Delivery days**: Item value, 0 = Settings default (ADR-007). Settings require *Default Max Days* when
  *Sync Offers* is on: without it items would promise delivery on the day of the order.
- **No duplicates.** Before any POST: an EAN already mapped to another item's offer → `Not synced`; then
  `GET ?eans=` adopts an existing offer (several offers for the EAN → `Not synced`). After a successful POST
  the mapping is **committed at once**, so nothing failing afterwards can lose it. An ambiguous POST isn't
  raised (no job retry); the next run's lookup settles it.
- **Transactions.** `sync_offer` works inside a savepoint. Handled failures roll back to it, write the
  status, log once and comment. Other bol errors roll back, log once, comment, commit (to keep the comment),
  then re-raise so the job fails.
- **Change detection** on Item and Brand treats `0`, `""` and `None` as equal. `has_value_changed` doesn't, so
  saves through the API (Int left `None`) would queue a sync on every save.
- **Deduplication race.** `enqueue(deduplicate=True)` skips a job that is queued *or running*. A price change
  committed while the job for that item is already running is skipped. The daily reconcile closes this
  gap: it builds each item's desired offer and compares it with the listed offer (`diff_offer`), queueing a
  sync for any difference, with no extra GETs. That also covers failed jobs (story: "the reconcile or the next
  trigger retries"), Item Prices that expire, and offers adopted in story 04 that differ from ERPNext.
  Mapped offers missing from the list and `bol_enabled` items without a mapping are queued too.
- **Delete** (`delete_offer`, POST only) needs System Manager or Bol Manager plus Item read permission (as
  with the Brand operator, ADR-007). It writes with `db_set`, so unticking *Sell on bol* doesn't queue a sync.

---

## ADR-009 — Stock sync (story 06)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/offers-v11.yaml` (Offers API 11.5.0),
`docs/bol-openapi/retailer-api-v10.json` and ERPNext 15.124.5.

| Story says | Spec / ERPNext says | Decision |
|---|---|---|
| PATCH the offer's `stock {amount, managedByRetailer}` | `PATCH /retailer/offers/{id}` takes `stock` (`UpdateStock`, both fields optional) → 204 | Send both: `{"stock": {"amount": n, "managedByRetailer": false}}`, nothing else. Stock and the offer sync's fields never share a PATCH. |
| Clamp to bol's maximum stock ("check the limit") | **v11 documents no maximum.** v10's `UpdateOfferStockRequest` and `StockCreate` have `minimum: 0, maximum: 999` | `BOL_MAX_STOCK = 999` (`mappers/stock.py`). Raise it if bol confirms a higher v11 limit. |
| Trigger: `Bin.on_update` (doc_events) | **ERPNext never saves Bin as a document on stock moves.** `stock_ledger.update_bin_data/update_bin`, `bin.update_qty` (SO reservations) and `Bin.db_set` write with `frappe.db.set_value`, so `on_update` doesn't fire on DN, Stock Entry or SO submit | **Minute poll** (`stock.poll_stock`, cron `* * * * *`, owner's decision). One query computes ATS for every bol-enabled mapped Item (Item ⟕ Ecommerce Item ⟕ Bin) and queues `push_stock` only where ATS ≠ `bol_last_stock_pushed`. No bol calls. It catches every source (stock ledger, SO and SRE reservations, buffer edits, switching the sync on) and has no timestamp races. There is no Bin doc_event and no separate buffer trigger. |
| Nightly reconcile: GET each offer | The offer **list** (`OfferResponse`) includes `stock {amount, correctedStock, managedByRetailer}` | One list pass (`offers.offer_pages`, as in ADR-008), daily at 05:00. A mismatch in `amount` **or** `managedByRetailer: true` (offers from the old Shopify app) is corrected. Mapped offers missing from the list are left to the offer reconcile, which recreates them. |
| `correctedStock` is read-only | Confirmed (only on the response) | Never compared or sent: bol lowers it by its open orders, so comparing it would "correct" every offer with an open order. |
| ATS = actual − `reserved_qty` − `reserved_stock` − buffer | `Bin.reserved_qty` counts **every** open SO line; `reserved_stock` (Stock Reservation Entries) reserves stock for those same lines | **Story formula kept** (owner). With Stock Reservation on (it's off by default and off on this bench), SRE quantities are subtracted twice: bol undersells, never oversells. |
| `managedByRetailer` | `false`: bol subtracts **all** open orders from our amount; `true`: only orders placed after our last update | `false` (story). bol orders not yet imported as Sales Orders (story 07) are still subtracted by bol. Once imported they're in `reserved_qty`, and bol only counts them until they're shipped, so there's a short overlap that can only undersell. |

**Decisions.**
- **"Pushed" marker.** `bol_last_stock_pushed` is an Int (NOT NULL, 0), so a new or adopted mapping looks like
  "0 pushed". `Ecommerce Item.inventory_synced_on` (1970-01-01 = never synced in ecommerce_integrations, which only
  reads its own integration's rows) is set together with it. A mapping counts as up to date only when both
  match. So an adopted offer and the first run after switching on *Sync Stock* are always pushed, even at 0.
  Both are written **only after bol confirmed** (204, or a reconcile that saw the same value).
- **Sync Stock requires Sync Offers** (owner). A 404 on push and an Item without an offer are handed to
  `sync_offer`, which recreates or creates the offer with its stock (`_adopt_or_create` sends the ATS and records
  it before the mapping is committed). With Sync Stock off a new offer still starts at 0.
- **Settings.** *Warehouse* must be a non-group warehouse. *Stock Buffer* defaults to 1. Item *bol Stock Buffer*:
  0 = Settings default (like the delivery days, ADR-007). An item can't override the buffer down to 0.
- **Failures.** A push that fails is logged and commented once per 6 h (`bol:failed:stock:{item}`). The job fails and
  nothing is stored, so the next poll (≤ 1 min) queues it again. A 404 isn't logged: the offer sync takes over.
  Repeated failures cost one call per item per minute (deduplicated by `job_id=bol_stock::{item}`).
- **Reconcile report.** One integration log line: `bol stock reconcile: X checked, Y corrected, Z failed.`
  If the list fails, an Error log carries the counts so far and the job fails.
- **Demo environment.** The demo rejects offer PATCHes with HTTP 403 (ADR-008), so pushes are covered by tests
  against the in-memory fake of the offer endpoints. The poll was checked on the dev site with a real Stock Entry.
