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
