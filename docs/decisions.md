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

---

## ADR-010 — Order import (story 07)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Orders),
ERPNext 15.124.5 and the demo environment.

| Story says | Spec / ERPNext / demo says | Decision |
|---|---|---|
| `GET /retailer/orders?fulfilment-method=FBR&status=OPEN&page=N`, 50 per page, stop on an empty page | Confirmed (`ReducedOrders {orders[{orderId, orderPlacedDateTime, orderItems[]}]}`); the spec states no page size. **The demo only has page 1**: page 2 is a 400 "Requested demo environment resource is not available" | Page until a page is empty; an id seen on an earlier page counts once (pages can shift). In Demo, stop after page 1. |
| `GET /retailer/orders/{order-id}` fields | Confirmed. Also: `pickupPoint`, `shipmentDetails.pickupPointName`, `billingDetails.kvkNumber/orderReference`, line `totalPrice`, `discounts[{title, amount}]`, `additionalServices`, `fulfilment.exactDeliveryDate`. `orderPlacedDateTime` is nullable | See decisions below. A missing placed date counts as "now". |
| `rate = unitPrice` | `unitPrice` is before bol's promotion; `totalPrice = unitPrice × quantity − Σ discounts` (VAT incl.), so the acceptance criterion "SO total = bol's total" fails with a promotion | **Owner decision: per-line discount.** `price_list_rate = unitPrice`, `discount_amount = Σ discounts / quantity` (cents, half-up), `rate = price_list_rate − discount_amount`. A per-unit rate stays right when stories 10/11 cancel or return part of a line. When `rate × open qty` ≠ bol's `totalPrice` pro rata (the discount doesn't split into whole cents), the SO gets a comment. |
| `delivery_date` = minimum `latestDeliveryDate` | `latestDeliveryDate` is empty when the buyer chose an `exactDeliveryDate`. ERPNext's `validate_delivery_date` **overwrites the header with the latest line date**, and refuses a delivery date before the order date (the demo's orders have 2018 delivery dates on 2019 orders) | Per line `latestDeliveryDate or exactDeliveryDate` is stored in `bol_latest_delivery_date`. **Every line's `delivery_date` is the order's** (the earliest of them, never before the order date, else placed + 1), so the header keeps the minimum. |
| Total equal to bol's | ERPNext rounds the grand total to whole euros unless `disable_rounded_total` | `disable_rounded_total = 1` on the SO; Sales Invoices made from it (story 12) copy it. ERPNext's inclusive-tax rounding can leave `net_total + total_taxes_and_charges` a cent off `grand_total` (it corrects the tax rows on invoices); `grand_total` is bol's. |
| `DuplicateEntryError` on `bol_order_id` = already imported | Frappe v15 raises **`UniqueValidationError(doctype, name, db_error)`** for a unique non-PK column (`DuplicateEntryError` is only for the primary key). When the other import has already committed, **ERPNext's `validate_po`** ("one Sales Order per customer PO", `po_no = orderId`) fails first, with a plain `ValidationError` | After any failure the order is rolled back to its savepoint; it counts as already imported (no log) when the error is the `bol_order_id` unique key or the Sales Order now exists. |
| Poll job `job_id="bol_poll_orders"` with dedupe | Frappe v15's scheduler enqueues with `scheduled_job::<method>` and skips a run while the previous one is queued or running (ADR-006) | The cron (`*/5 * * * *`) calls `orders.poll_orders` directly. |
| — | Lines carry `fulfilment.method` | Non-FBR lines are skipped (FBB is out of scope). |
| `billingDetails.vatNumber` | Also `kvkNumber`. Address has no VAT field, and every order is booked on the one bol customer, so `Customer.tax_id` can't hold it | **Buyer fields on the order** (owner decision, see below). The billing address is created only when its address fields differ from shipping. |
| — | `pickupPointName` | Put first in `address_line2` (before `extraAddressInformation`): it's what the carrier looks for. |

**Decisions.**
- **Times.** `mappers/dates.to_system_datetime` is the one helper for bol timestamps (offset → naive system time
  zone). The cutover guard (`import_orders_placed_after`) and `transaction_date` use it.
- **Mapper** `mappers/order.to_sales_order` is pure and returns `None` when no FBR unit is left to ship (checked
  before the country, so a closed order from elsewhere is skipped, not an error). Mapping errors are
  `BolMappingError` with `reason` `unknown_ean | ambiguous_ean | country`; warnings are reason-coded dicts the
  flow words as translated comments.
- **Items** are found by EAN with `offers.items_by_ean` (all GTIN forms, ADR-007; renamed from `_items_by_ean`).
  The offer reference is compared with the item_code cut to 100 characters (ADR-008).
- **Taxes.** The template rows are copied with ERPNext's `get_taxes_and_charges`. Bol Settings refuses a template
  of another company, one without rows, or one with a row not *included in print rate*.
- **Warehouse.** The order lines use the existing *Warehouse* setting (story 06); it's required for Import Orders too.
- **Failures** roll back to the savepoint (addresses included), `log_once("order", order_id)` with the full order
  payload (or the bol call, for a GET failure), and the poll continues. A failing order list is logged once per 6 h
  (`bol:failed:orders:list`) and fails the job.
- **One customer, buyer on the order** (owner decision, after comparing with ecommerce_integrations' Shopify flow,
  which makes a Customer per Shopify customer). bol sends no customer id (the e-mail is a per-order relay address),
  so buyers can't be recognised reliably, and bol pays out one net amount for many orders, which a single receivable
  party matches. The invoice still has to name the buyer: Sales Order, Delivery Note and Sales Invoice get read-only
  `bol_buyer_name`, `bol_buyer_company`, `bol_buyer_vat_number`, `bol_buyer_kvk_number`, taken from `billingDetails`
  (the party that pays) or, without it, `shipmentDetails`. They aren't `no_copy`, so `make_delivery_note` and
  `make_sales_invoice` carry them over. They are buyer PII, for the cleanup in story 15. Printing them on the invoice
  instead of the customer name, and the VAT treatment of business buyers in BE, belong to story 12.
- `sync_existing_order_changes(ids)` receives the already imported open orders; it's a no-op until story 10.
- **Tests.** The test site has no Company: `tests/utils.make_order_masters` builds `_Test bol Company` (standard
  chart, NL, EUR), a default Address Template, two inclusive templates and a Customer once and commits them.
  `test_bol_settings.py` adds Customer and Sales Taxes and Charges Template to `test_ignore` (ADR-005).
- **Demo environment (checked live, GETs only).** The list returns 4 canned orders. Three have every unit shipped and
  are skipped. `1042831430` (BE, one open unit, EAN 8717418510749, `unitPrice` 10.30 with a 9.99 discount on
  3 units) imported as a submitted SO on `dev.localhost` with the BE template, the item's bol ids, the Brussels
  address (extension `B`) and a separate billing address. The demo's `totalPrice` doesn't match its unit price and
  discount, so that SO got the rounding comment, plus a reference comment (the demo's reference `MijnOffer1121`).

---

## ADR-011 — Shipment confirmation (story 08)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Shipments and
Transports), bol's functional page *Orders and Shipments* (v10), ERPNext 15.124.5 and the demo environment.

| Story says | Spec / docs / demo say | Decision |
|---|---|---|
| `POST /retailer/shipments {orderItems[{orderItemId, quantity}], shipmentReference, shippingLabelId?, transport?}` → 202 | Confirmed (`ShipmentRequest`): 1–100 order items, `quantity` > 0 (omitted = all open units), `shipmentReference` ≤ 90 chars, null or non-empty; `transport.transporterCode` required, `trackAndTrace` nullable | Quantities are summed per order item and always sent. `shipmentReference` = the Delivery Note name. An empty T&T is sent as `null`. |
| `transporterCode` enum ("check the enum") | **The spec has no enum** (a plain string). The functional page lists the codes; dashes and underscores are mixed (`TNT-EXTRA`, `TNT_BRIEF`, `DPD-NL`, `BPOST_BE`) and "could change between versions" | 13 codes in `mappers/shipment.TRANSPORTER_CODES`: TNT (PostNL), TNT-EXTRA, TNT_BRIEF, BRIEFPOST, DHL, DHLFORYOU, DPD-NL, DPD-BE, BPOST_BE, BPOST_BRIEF, GLS, UPS, OTHER. Delivery Note *bol Transporter* and Bol Settings *Default Transporter* use the same list (a test keeps them equal). |
| `entityId` = shipmentId | The spec's generic `ProcessStatus.entityId` text says "the id of the order item". The functional page: "corresponds to the shipment id … returned only if the status is SUCCESS" | The functional page is followed. Should `entityId` be missing on SUCCESS, the handler looks the shipment up (reconcile). |
| `GET /retailer/shipments?order-id=` | `ReducedShipment {shipmentId, shipmentReference, order, shipmentItems[{orderItemId, ean}], transport{transportId}}`: **no quantities**. `GET /retailer/shipments/{id}` has `shipmentItems[].quantity/quantityShipped` and `transport{transportId, transporterCode, trackAndTrace}` | Reconcile: a listed shipment with our reference is ours. Otherwise a shipment with exactly our order items (not recorded on another DN) is fetched and is ours when its reference is ours, or its quantities equal ours and its reference names no other DN. Only page 1 is read: one order's shipments fit on it. |
| `PUT /retailer/transports/{id}` "adds or corrects T&T later" | `ChangeTransportRequest {trackAndTrace (required), transporterCode?}` → 202. The functional page: it "only allows you to add the tracking information when the trackAndTrace code is missing", and advises against changing the code or the transporter with it | **Add only.** `update_transport` reads the shipment; it PUTs when bol has no T&T, does nothing when bol has the same one, and comments ("correct it in the seller dashboard") when bol has another. |
| — | Spec error responses: 400 only (plus 404 on the detail) | 400 → `BolValidationError` with violations → Failed (§ below). |

**Demo environment (checked live, `dev.localhost`).** `POST /retailer-demo/shipments` answers any body with a canned
`CREATE_SHIPMENT` ProcessStatus (id `019bbcbc-…fff`, no `entityId`); the bulk process-status call then answers `PENDING`
with `entityId 555551` forever, so on Demo a shipment stays *Pending*. `PUT /retailer-demo/transports/312778947` answers a
`CHANGE_TRANSPORT` ProcessStatus. The unfiltered list returns 4 canned shipments **without `shipmentReference`**; with
`order-id` the demo only knows `7616222250` (its list entry does carry the reference) and answers a 400 *"Requested demo
environment resource is not available"* for any other order. In Demo that 400 counts as "no shipments" (as with the order
pages, ADR-010). Details exist only for the canned ids. A Delivery Note couldn't be submitted on `dev.localhost`: its
company has no default inventory account (perpetual inventory), unrelated to this app. The flows are covered by tests
against an in-memory fake of the endpoints.

**Decisions.**
- **Statuses** (`bol_shipment_status`, all written with `db_set`, never by saving the DN): *Pending* (submitted; sent or
  about to be), *Confirmed*, *Failed* (a person has to act), *Unknown* (bol may have it).
- **Reconcile before every POST**, not only after an ambiguous one. It costs one GET per shipment and covers every way a
  shipment can exist without our knowing: an ambiguous POST, a crash between the POST and `track()`, a job run twice,
  a shipment made in the seller dashboard. bol itself also refuses to ship more than the open quantity, a second guard.
- **Failures in `confirm_shipment`** (inside a savepoint):
  - 400/422 → *Failed*, comment with bol's violations, log.
  - Auth error, our data (`BolMappingError`), any other non-transient bol error → *Failed*, comment, log.
  - `ambiguous=True`, a 202 without `processStatusId`, or any error after the POST → *Unknown*, logged and commented
    once per 6 h (`bol:failed:shipment_unknown:{dn}`).
  - Transient, rate limit, 5xx after the client's retries → stays *Pending*, logged and commented once per 6 h
    (`bol:failed:shipment:{dn}`).
  - Anything else (a bug) → *Failed*, log, comment, commit, re-raise (the job fails).
- **Sweep** (owner's decision): every 10 minutes `sweep_shipments` queues `confirm_shipment` for Delivery Notes *Unknown*
  or *Pending*, not modified for 10 minutes, without an unfinished Bol Process (PENDING or HANDLER_ERROR), 50 per run.
  That recovers lost jobs, bol outages and ambiguous timeouts without a click. *Failed* waits for **Retry bol shipment**.
- **Handlers.** `shipment.on_failure` only sets *Failed*: process_status has already logged bol's `errorMessage` and
  commented it on the DN (ADR-006). `shipment.on_success` also queues `update_transport` when the T&T was filled in while
  the shipment was *Pending*. `transport` comments on success; a failure is commented by the tracker.
- **Cancellation** is blocked for *Pending* and *Confirmed* (story) **and *Unknown*** (bol may have it; Retry settles it).
  *Failed* can be cancelled.
- **Validation runs only while *Push Shipments* is on**, except that `bol_order_id` is always filled from the Sales Order
  (informational). Return Delivery Notes are left alone (story 11). Without a distribution party a line counts as
  RETAILER. BOL lines need no transporter or T&T here; story 09 adds the label check.
- **Retry** needs System Manager/Bol Manager (`check_bol_role`, ADR-006) **and** Delivery Note write permission; only
  *Failed*/*Unknown*. An unexpected error becomes a `ValidationError` message.
- **Settings defaults on existing sites.** A Single's field defaults apply only while the Single has no stored values
  at all, so on a site whose Bol Settings were saved before, *Require Track & Trace* would read 0. Patch
  `set_shipment_defaults` stores the defaults of the two new fields when they have no value. (`stock_buffer`, story 06,
  has the same gap on such sites.)
- `tests/utils.make_bol_manager` (moved from `test_catalog.py`) gives a user with only the Bol Manager role.

## ADR-012 — Verzenden via bol labels (story 09)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Shipping Labels),
bol's demo page *v10 Shipping Labels* and the demo environment.

| Story says | Spec / docs / demo say | Decision |
|---|---|---|
| `POST /retailer/shipping-labels/delivery-options {orderItems[{orderItemId}]}` | Confirmed (`DeliveryOptionsRequest`); each item also takes an optional `quantity` (omitted = all open units). Errors: 400, 404 (403 isn't listed) | The Delivery Note's quantities are sent (summed per order item, as for shipments): a label for part of an order fits that part. The call only reads, so it's sent `safe` (retried like a GET). |
| `deliveryOptions[] {shippingLabelOfferId, validUntilDate, transporterCode, labelType, labelPrice{totalPrice}, packageRestrictions, handoverDetails, recommended?}` | `recommended` is **required**; `validUntilDate` is a **date** (nullable); `totalPrice` is **excl. VAT**; also `labelDisplayName` (required); `labelType` ∈ PARCEL, MAILBOX, MAILBOX_LIGHT | An option is expired when its date is before today (system date); no date = valid. Chooser: recommended → preferred transporter (case-insensitive) → any; the cheapest of that group. |
| `POST /retailer/shipping-labels {orderItems[{orderItemId, quantity}], shippingLabelOfferId}` → 202 | Confirmed (`ShippingLabelRequest`), 400 only. Demo: `CREATE_SHIPPING_LABEL` ProcessStatus | `entityId` is the shippingLabelId (functional docs). A SUCCESS without one → *Failed* (a new label can be bought). |
| `GET /retailer/shipping-labels/{id}`, Accept `application/vnd.retailer.v10+pdf` | Confirmed; also sends `X-Track-And-Trace-Code` / `X-Transporter-Code` headers (and HEAD for headers only) | The headers aren't read: `BolClient` returns the bytes, the transporter comes from the chosen option, and bol knows the label's T&T. |

**Demo environment (checked live, `dev.localhost`).** Delivery options answer only order item `2095052647` (two TNT
options, the recommended parcel at 4.41 and a TNT_BRIEF mailbox label at 2.88), with `validUntilDate` in **2020**; it is
`tests/fixtures/delivery_options.json`. Any other order item gets a 400. The label POST answers a canned
`CREATE_SHIPPING_LABEL` ProcessStatus, which (like every demo process) stays `PENDING`. The PDF GET answers only label
`c628ba4f-f31a-4fac-a6a0-062326d0dbbd`, with 141 bytes of plain text ("THIS IS A FAKE PDF FILE …").

**Decisions.**
- **Statuses** (`bol_label_status`, `db_set` only): *Requested* (bol is making it; with a *bol Shipping Label ID* it's
  made but not downloaded), *Ready* (PDF attached), *Failed* (bol refused; a new label can be bought). All three label
  fields are `no_copy`, `bol_label_transporter` too (the story didn't say), so an amended or duplicated DN starts clean.
- **Buying** is an explicit button, so bol is called in the request (overview §4.3). Bol Manager/System Manager **and**
  Delivery Note write permission (as Retry, ADR-011). The DN row is locked first: a double click waits, then finds
  *Requested* and is refused. A label is never bought while one is *Requested* or *Ready*.
- **Button failures** are logged (the log survives the rollback, ADR-005) and shown as a translated message; the DN is
  left as it was. 403 → "not registered for Verzenden via bol"; no option left → "No VVB option available"; 400 → bol's
  violations. **An ambiguous label POST** (or a 202 without `processStatusId`) leaves the DN unchanged and says to try
  again: there's no endpoint to find a label by order item, and bol charges only the label a shipment is confirmed with,
  so an orphan label costs nothing.
- **Download failures after SUCCESS** (bol error, or a body that isn't a PDF) are caught in the handler: the label id is
  kept, the status stays *Requested*, log + comment + message; **Download bol label again** only GETs. Checking the
  `%PDF-` signature first matters because Frappe's File check (`pdf_contains_js`) raises a raw pypdf error on anything
  else (as on the demo).
- **The PDF** is a private File `bol-label-{dn}.pdf` attached to the DN: readable by whoever may read the DN.
- **The user** who clicked gets a desk message (realtime `msgprint`) when the label is ready or failed; the DN is
  updated with `notify=True`, so an open form refreshes.
- **Submit** (while *Push Shipments* is on): items bol ships need a *Ready* label; any DN with a *Requested* label waits
  for it. A DN with a label skips story 08's transporter and T&T checks, and the shipment is confirmed with
  `shippingLabelId` (`build_shipment`, story 08).
- **Items are fixed once a label is bought**: changing lines or quantities of a DN with a *Requested*/*Ready* label is
  refused; delete the draft and make a new one. To make that possible, `hooks.ignore_links_on_delete` lists **Bol
  Process** and **Ecommerce Integration Log**: their Dynamic Links would otherwise block deleting any document they refer
  to. A handler for a deleted DN does nothing.
- **Use Verzenden via bol needs Push Shipments** (a label is used by confirming the shipment through the API).
  **bol Labels for Own Shipments** (`vvb_for_retailer_items`) offers the button on DNs of items you ship yourself; the
  person decides whether bol's price (in the timeline comment) beats their own contract. No contract-price setting.
- **Demo:** the expiry check is skipped (its options expired in 2020); its process never finishes and its "PDF" isn't
  one, so on the demo a label stays *Requested*. The flows are covered by tests against an in-memory fake.
- The form shows **Get bol label** only where it applies: the DN's `onload` hook sets `__onload.bol_label_allowed`.

---

## ADR-013 — Cancellations (story 10)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Orders),
ERPNext 15.124.5 and the demo environment.

| Story says | Spec / ERPNext / demo says | Decision |
|---|---|---|
| `PUT /retailer/orders/cancellation {orderItems: [{orderItemId, reasonCode}]}` → 202 | Confirmed (`CancellationRequest`, 202 ProcessStatus, 400 Problem), but **`orderItems` has `maxItems: 1`** | One PUT and one Bol Process per line. Payload `{so_detail, order_item_id}` instead of the story's lists. |
| "Check whether a cancellation covers the whole order item or a quantity" | No quantity field: the **whole order item** (all its open units) | Cancel on bol offers only lines with nothing delivered; on success the whole line qty is taken off. |
| `reasonCode` enum | The 12 codes, written in the spec as **one comma-joined string** (a spec bug) | Kept as `mappers/cancellation.REASON_CODES`; the form gets them through `__onload`. |
| Use the OPEN list's data; fetch the detail when it lacks the fields | `ReducedOrderItem` has `cancellationRequest`, `quantityCancelled`, `quantityShipped`, `quantity` (all required); the **demo list has them too** | `orders.open_orders` keeps the list entries; the detail is fetched only for an entry without the fields. |
| `apply_cancelled_qty(so, item_id, new_cancelled)` | A whole order cancelled line by line would first remove lines one at a time, then cancel | `apply_cancelled_qty(so_name, {order_item_id: quantityCancelled})`: one call per order, one `update_child_qty_rate` (or one cancel). |
| Reduce the line with `update_child_qty_rate` | It refuses qty 0 unless *Allow zero qty in Sales Order*; rows **left out** of `trans_items` are deleted (after ERPNext's own delivered/billed checks); a qty below `delivered_qty` is refused | **Owner decision:** a line bol cancels completely (other lines remaining) is **removed**, recorded in a comment. A line is never taken below its delivered qty: logged (`log_once`) and commented, the line left as it is, other lines still applied. |
| Cancel the SO when everything is cancelled | A draft (*Submit Orders* off) can't be cancelled | **Owner decision:** a draft is edited in place; a draft whose every line is cancelled is **deleted**, with a Success entry in the integration log (its timeline goes with it). |
| Seller cancel 400 → re-sync | The 400 can come on the PUT, or as a FAILURE process | Both re-sync: GET the order, apply it. A PUT 400 doesn't throw, so the re-sync commits; the dialog lists the failures. |

**Decisions.**
- **qty is net of bol's cancellations.** The import already leaves cancelled units out, so it now also stores
  `bol_qty_cancelled = quantityCancelled`: only what bol cancels later is taken off. The request flag isn't set at
  import; the next poll sets it and makes the ToDo.
- **Requests**: `bol_cancellation_requested` set on the line, a comment, and a ToDo for *Notify User* (new Bol Settings
  field), unless that user already has an open ToDo on the SO. Only for lines bol still has open. No user → comment only.
- **Savepoints**: requests and quantities each in their own savepoint per SO; a failure (e.g. `update_child_qty_rate`
  refusing because of a stock reservation) is rolled back, logged once per 6 h (`bol:failed:cancellation:{so}`) with a
  comment, and retried on the next poll. One commit per SO.
- **Daily check** (`sync_recent_orders`): SOs with `bol_order_id`, not cancelled, `per_delivered < 100`, not Closed,
  created in the last 30 days: GET each order and apply it.
- **Cancel on bol**: Bol Manager/System Manager **and** SO write permission (as Retry, ADR-011). The SO is locked first;
  a line with an unfinished cancellation process is refused. PUT is retried by the client on a transient error; a
  duplicate is refused by bol (400) and re-synced. Errors per line are collected: nothing is thrown once a line has
  been tracked (that would roll back its Bol Process). Only when nothing was sent and nothing re-synced is it a
  `ValidationError`.
- **Handlers**: success applies `bol_qty_cancelled + qty` for the line (a row already removed by the poll = done);
  failure re-syncs (process_status has logged and commented bol's message). A bol error in the re-sync is logged once,
  not raised.
- **Guard**: `Sales Order.before_cancel` refuses a bol SO with a line still open (`qty > delivered_qty`) while Import
  Orders is on; the integration's own cancel sets `flags.bol_cancelled`.
- **No new toggle**: everything runs under *Import Orders* (no speculative settings, overview §4.5).

**Demo environment (checked live, `dev.localhost`).** The OPEN list's items carry all cancellation fields (every
demo item is shipped already). The cancellation PUT answers a canned 202 `CANCEL_ORDER` ProcessStatus with
`entityId` = the order item id, **for any order item id**, and **always with the same `processStatusId`**. On the demo a
second line therefore shares the first line's Bol Process (`track` is idempotent per id), and, like every demo
process, it stays `PENDING`. Production ids are unique per request. The flows are covered by tests against an
in-memory fake.

---

## ADR-014 — Returns (story 11)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Returns),
ERPNext 15.124.5 and the demo environment.

| Story says | Spec / ERPNext / demo says | Decision |
|---|---|---|
| `GET /retailer/returns?handled=false&fulfilment-method=FBR&page=N` | Confirmed (`ReturnsResponse.returns[]` = `ReducedReturn`). `ReducedReturnItem` has `rmaId`, `orderId`, `ean`, `expectedQuantity`, `returnReason`, `handled` | The list is enough: the detail (`title`, `trackAndTrace`, `customerDetails`) isn't fetched on import. Paged until an empty page; only page 1 on the demo. |
| `GET /retailer/returns/{return-id}` | The path takes the **returnId**, not the rmaId | Return DNs also store `bol_return_id`, so the "already handled" check can GET the return. |
| `PUT /retailer/returns/{rma-id}` `{handlingResult, quantityReturned}` → 202 | Confirmed, media type v10; the six `handlingResult` values match the story. **`quantityReturned` has `minimum: 1`** | **Owner decision:** *Report to bol without stock return* sends `quantityReturned = expectedQuantity` (the units handled, which the customer keeps), not 0. |
| `returnReason {mainReason, detailedReason, customerComments}` | `detailedReason` nullable; the demo leaves it out and often repeats the main reason as the comment | `bol_return_reason` = main — detailed: comments, a repeated comment left out, truncated to 140 characters. |
| Poll with `job_id="bol_poll_returns"` | Frappe v15's scheduler enqueues `scheduled_job::<method>` and skips a run while the previous one is queued or running (ADR-006) | The cron (`*/30 * * * *`) calls `returns.poll_returns` directly. |
| `DuplicateEntryError` means it was created already | A unique custom field raises `UniqueValidationError` (as in story 07) | Both count as done, and so does a return DN found by `bol_rma_id` after the rollback. |
| Hidden field for the expected quantity | — | `bol_expected_qty` (Int, hidden). |

**Decisions.**
- **Finding the original DN**: submitted Sales Orders with `bol_order_id = orderId` → submitted, non-return DN lines
  of those SOs whose item has the return's EAN (`offers.items_by_ean`). What a line can still take back is
  `qty − returned_qty − units on draft bol returns of that line` (ERPNext only counts submitted returns). The oldest DN
  whose line covers `expectedQuantity` is used. None → `log_once` (`bol:failed:return_not_found:{rma}`) and a ToDo for
  *Notify User* with the return's details (referencing the SO if there is one); no DN.
- **The draft** comes from ERPNext's `make_sales_return(dn)`, reduced to the returned line with `qty = -expectedQuantity`.
  One ToDo per RMA for *Notify User* (none when unset). One savepoint and one commit per RMA. Any failure is rolled
  back, logged once per 6 h, and retried on the next poll.
- **Submit checks** (`before_submit`) run whenever `bol_rma_id` is set, even with *Handle Returns* off (no bol call):
  a handling result, whole units, `Σ|qty| ≤ bol_expected_qty`. Inspection may lower the qty; that's what's reported.
- **Reporting** (`report_handling`, background job `bol_return::{dn}`): PUT → Bol Process `return_handling`. Status
  stays *Open* until bol's process succeeds (→ *Reported*). A 400, or a FAILURE process, GETs the return: if bol has
  the RMA handled (a second PUT, or the seller dashboard), it's *Reported* with a comment, otherwise *Failed* with
  bol's message and a **Retry bol return report** button. Transient errors (connect, 429, 5xx) keep it *Open*, logged
  once per 6 h. The returns poll then re-queues submitted *Open* returns that are 10 minutes old and have no
  unfinished process (`sweep_reports`). That also reports returns submitted while *Handle Returns* was off.
- **Without stock return** (owner decision from the story): ERPNext can't submit a zero-qty return, so the draft has
  **Report to bol without stock return** (Bol Manager/System Manager, plus write and delete permission on the DN). It
  sends the PUT and, once bol accepts it (202), **deletes the draft**. Its Bol Process references the **original DN**,
  so bol's answer shows there. While that process is unfinished the poll skips the RMA (payload `rma_id`). If bol's
  process fails and the RMA is still open, the next poll makes a new draft. A bol error on the PUT keeps the draft
  and tells the user why. The delete is recorded in the integration log.
- The return DN copies `bol_order_id` from the original, so the *bol.com Shipment* section is hidden on returns
  (`depends_on … && !doc.is_return`); the *bol.com Return* section shows instead. Shipments and labels already skip
  returns.
- No financial documents: credit notes are story 12.
- **Toggle**: `Bol Settings.handle_returns` (default off), in a new *Returns* section. The ToDos go to the existing
  *Notify User*.

**Demo environment (checked live, `dev.localhost`, GET only).** The unhandled list has one return (`returnId`
15897410, rma 60283608, order 1044796550) **without `processingResults`** although the spec marks it required. Page 2
is a 400 ("resource not available"). The detail of 15897410 has two items, one handled. Its order isn't imported
here, so a demo poll (run once on `dev.localhost`) made one "no submitted Delivery Note" error log, no draft, and no
ToDo (*Notify User* isn't set there). The demo's answer to the
handling PUT wasn't checked (no writes were sent from this session). The flows are covered by tests against an
in-memory fake.

---

## ADR-015 — Accounting: sales invoices, credit notes, bol invoices, invoice requests (story 12)

**Context.** Checked on 2026-11-10 against `docs/bol-openapi/retailer-api-v10.json` (Retailer API v10, Invoices and
Shipments), bol's functional pages *Invoices* and *Customer invoices*, the demo pages `demo/v10-INVOICES.html` and
`demo/v10-SHIPMENTS.html`, ERPNext 15.124.5 and the demo environment.

| Story says | Spec / docs / ERPNext / demo say | Decision |
|---|---|---|
| `GET /retailer/invoices?period-start-date&period-end-date` (window ~31 days) | Confirmed; "no longer than 31 days" (the functional page says 32). **The spec has no response schema** (`string/byte`) for the list or the specification. The demo page has the shape: `invoiceListItems[{invoiceId, invoiceType (ALL_IN_ONE, ADVERTISING_VIA_BOL), issueDate, invoicePeriod, legalMonetaryTotal{lineExtensionAmount, payableAmount, taxExclusiveAmount, taxInclusiveAmount: {amount, currencyID}}, invoiceMediaTypes, specificationMediaTypes}]` | Periods of ≤ 31 days that share their boundary day (whether bol's end date is inclusive or not, no day is missed; invoices are de-duplicated by id). bol's totals = `taxExclusiveAmount` (net) and `taxInclusiveAmount − taxExclusiveAmount` (VAT). A 404 on the list (not in the spec) counts as "no invoices". |
| `GET …/{id}/specification` → line details | Paged with `page`, no schema. Demo: `invoiceSpecification[{id, invoiceLineRef, invoicedQuantity, item{Name = type, Description[], AdditionalItemProperty[]}, lineExtensionAmount{value}, price, taxTotal{TaxAmount{value}}}]` | Read until an empty page; a 404 past page 1, or a page identical to the previous one, ends it. Summed per `item.Name` (the line type), rounded per type in the currency precision. |
| `GET …/{id}` with Accept PDF | Confirmed (`application/vnd.retailer.v10+pdf`); the list says which media types each invoice has | The PDF is fetched only when listed; otherwise the remarks say so. Checked for `%PDF-` before Frappe's PDF check (as for labels, ADR-012). |
| Group lines by type, map to accounts | An ALL_IN_ONE invoice nets **`TURNOVER`** (the sales bol collected from buyers, VAT category O) against the fees, so `payableAmount` is usually negative | **Owner decision: TURNOVER and CORRECTION_TURNOVER stay off the Purchase Invoice.** Our Sales Invoices booked those sales on the bol receivable already; a PI line can't post to a Receivable account anyway (ERPNext needs a party on it). The remarks give their total; the account map refuses them. |
| Total check: our net/VAT vs the invoice | — | The draft's `net_total` / `total_taxes_and_charges` (as ERPNext computes them with the purchase template) are compared with bol's totals **minus the turnover**. More than 0.01 off → `MISMATCH: …` first in the remarks, `log_once("bol_invoice_mismatch", id)`. Never corrected. |
| Negative total → debit note; "if ERPNext requires `return_against`, log it" | ERPNext 15's `validate_return` checks `return_against` only when it's set: **a PI debit note doesn't need it** | Negative totals become draft PIs with `is_return = 1`, no `return_against`. A non-return PI refuses a negative qty but takes a negative rate: lines are qty 1 (−1 on a debit note) with a signed rate, so a type of the other sign still books correctly. |
| Lines without an item | A PI line needs `item_name`, `uom`, `conversion_factor` | `item_name` = the line type, description = bol's Dutch text + line count, uom = *Stock Settings → Default UOM*. A mapped item must be non-stock (ERPNext would book a stock item on Stock Received But Not Billed). |
| `make_sales_invoice(return_dn)` → credit note against the original SI | ERPNext's Delivery Note mapper maps `is_return` but **not `return_against`**, and leaves out no_copy fields | `return_against` = the submitted SI of the original Delivery Note, each row's `sales_invoice_item` found through the return row's `dn_detail`; `bol_order_id` and `debit_to` are set by hand. No original SI yet → `BolMappingError`, retried by the sweep. |
| (D) `GET /retailer/shipments/invoices/requests`, upload `POST /retailer/shipments/invoices/{shipment-id}` | Confirmed: requests `state` ∈ OPEN, UPLOAD_ERROR, ALL (the spec writes the enum as **one comma-joined string**, a spec bug), 50 per page; upload is `multipart/form-data`, field **`invoice`** → 202 `UPLOAD_INVOICE`. *Customer invoices* page: PDF only, ≤ 2 MB, KvK/VAT number of business buyers must be on it, one upload per shipment, re-upload after a virus result (UPLOAD_ERROR) | Implemented (owner decision). Requests in OPEN and UPLOAD_ERROR are uploaded. `BolClient.request(files=…)` sends multipart (requests sets the boundary; the log records file name and size only). |

**Decisions.**
- **A. Sales Invoices.** `shipments._confirmed` (both the success handler and the "bol already has it" path) and
  `returns._reported` call `invoices` (module imports both ways; attributes are only used at call time), which queues
  `invoice_delivery_note` (`job_id=bol_invoice::{dn}`, deduplicated, after commit) when *Invoice Shipments and
  Returns* is on. Posting date/time = the Delivery Note's ("the shipment date" in our books; bol's own
  `shipmentDateTime` would cost a GET). Credit notes are dated today (they must not predate the invoice).
- **Idempotency.** A Delivery Note counts as invoiced when **any Sales Invoice that isn't cancelled** references it
  (`Sales Invoice Item.delivery_note`), a draft made by hand included: someone is invoicing it, and a second invoice
  would bill the buyer twice. The story says "submitted SI"; this is stricter. The DN row is locked first.
- **Failures** roll back to a savepoint, `log_once("sales_invoice", dn)` and comment once on the DN. The form shows
  **Create bol Sales Invoice / credit note** while one is due and missing (`__onload.bol_invoice_missing`); it needs
  Bol Manager/System Manager **and** Sales Invoice create permission (plus DN read).
- **Sweep** (daily): Delivery Notes of the last 30 days that are due and have **no Sales Invoice at all** are queued.
  One with a cancelled invoice is left alone (cancelled on purpose). Shipments are queued before returns.
- **C. Window.** `bol_invoices_until` (read-only) = today after a run where every invoice imported. While any invoice
  fails, it **stays where it was**, so the 7-day overlap keeps covering the failed one until it imports (a failure is
  never dropped after a week). The first run starts 30 days back. A list failure is logged once per 6 h and raised.
- **One savepoint and commit per bol invoice**; a unique-key race (`bol_invoice_id`) counts as done. PIs are never
  submitted. Posting date = bill date = bol's issue date (the VAT period it belongs to); the reviewer can change it.
- **Dates.** bol's demo sends `issueDate` as epoch **seconds** and `invoicePeriod` as epoch **milliseconds**;
  `mappers/bol_invoice.issue_date` also takes ISO dates and timestamps, in the system time zone.
- **D. Uploads** (daily, *Upload Requested Invoices*, needs *Invoice Shipments and Returns*): shipment → DN
  (`bol_shipment_id`) → its submitted non-return SI → `frappe.get_print(…, "bol Sales Invoice", as_pdf=True)` →
  multipart POST → Bol Process `invoice_upload` (new option of Bol Process' *handler*) on the SI. Skipped while that
  process is unfinished. An ambiguous POST isn't repeated blindly: the next run sees whether bol still lists the
  request (bol refuses a second upload with a 400). No DN/SI → `log_once("invoice_request", shipment)` (+ DN comment).
  The buyer's billing details from the request aren't stored or logged (PII).
- **Notification** (owner request, after the story): one open ToDo per requested shipment for *Notify User*, on the
  SI (else the DN, else no reference), found again by a `[bol shipment {id}]` key in its description because bol
  lists the request every day until it has the invoice. The upload's success handler closes it, so open ToDos are
  the requests a person must handle. No user set → no ToDo (as for returns, ADR-014).
- **Print format "bol Sales Invoice"** (owner decision; standard, module Bol): the bol buyer (company, name, address,
  VAT and KvK numbers) is the invoice party, not the *bol.com* customer. Labels are **Dutch text**, not `_()`: bol
  requires the language of the buyer's country (NL, BE) whatever the system language. Prices are shown incl. VAT
  (bol's prices), with net, VAT rows and total.
- **PDF rendering** (wkhtmltopdf) fetches the print styles from the site's URL, so the background workers must be able
  to reach it (`host_name`), as for any Frappe PDF. In this dev container `test.localhost` doesn't resolve, so the
  tests render the print format as HTML and mock the PDF step.
- **Still open for the accountant:** VAT treatment of business buyers in BE (reverse charge vs OSS) — no VAT logic
  was changed; the order import's templates apply. Also whether credit notes should reduce the original invoice's
  outstanding (ERPNext default) or stand on their own.

**Demo environment (checked live, `dev.localhost`, GETs only).** The invoice list answers **only without a period**
(with `period-start-date`/`period-end-date` it's a 404) and returns one canned invoice (`4500022543921`, ALL_IN_ONE,
issue date 2018-05-01). The specification answers **only without `page`** (404 with one). The "PDF" is 141 bytes of
text ("THIS IS A FAKE PDF FILE …"), so an import on the demo ends in an error log, and its three documents' totals
don't agree with each other anyway. In Demo the list is read without a period and the specification once. Invoice
requests: `state=OPEN` answers two canned requests (shipments `987654`, `665578`, unknown here → logged);
`state=UPLOAD_ERROR` is a 400, so Demo reads OPEN only. No upload was sent from here. The flows are covered by tests
against an in-memory fake of the endpoints.
