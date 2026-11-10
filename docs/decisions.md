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
