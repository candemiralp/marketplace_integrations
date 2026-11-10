# 01 — App scaffold & foundations

**Goal:** an installable, linted and tested Frappe app `marketplace_integrations` with
module `Bol`, a Bol Manager role and a custom-field mechanism that later
stories extend.
**Depends on:** —

## Scope
- Create the app with `bench new-app marketplace_integrations` (publisher Magnatlas).
- `hooks.py`: `required_apps = ["erpnext", "ecommerce_integrations"]`,
  `after_install`, `after_migrate`.
- `modules.txt`: `Bol`.
- `install.py`:
  - `ensure_role()` creates the role **Bol Manager** if it's missing (desk access).
  - `ensure_custom_fields()` calls `create_custom_fields(CUSTOM_FIELDS, update=True)`.
  - Both run from `after_install` and `after_migrate`.
- `setup/custom_fields.py`: `CUSTOM_FIELDS: dict[str, list[dict]] = {}`.
  It starts empty, and later stories add their fields here. Every field uses
  the `bol_` prefix, and identifier fields are `read_only` and `no_copy`.
- Tooling: `pyproject.toml` with ruff config (line length 110, rules
  `E,F,I,UP,B,SIM`), `.pre-commit-config.yaml` (ruff, ruff-format, Frappe
  semgrep rules), and the generated GitHub Actions CI, adjusted to also
  `get-app ecommerce_integrations`.
- `docs/decisions.md` (ADR log) and `docs/bol-openapi/` (`.gitkeep`).
- `README.md`: purpose, install, how to run the tests.

## Investigation task (record the results in `docs/decisions.md`)
Inspect the installed `ecommerce_integrations` app:
1. `Ecommerce Item` fields. Expected: `integration`, `integration_item_code`,
   `erpnext_item_code`, `sku`, `variant_id`, … What is the field type of
   `integration`? If it's a **Link to Module Def**, the module name `Bol` works
   as is. If it's a **Select**, add `Bol` to its options with a Property Setter
   in `install.py`.
2. `Ecommerce Integration Log` fields and the `create_log(...)` helper signature
   (path and arguments). Story 02 builds `bol/log.py` on top of what you find.
3. Whether `Ecommerce Item` has a unique constraint on
   `(integration, integration_item_code)`.

If either DocType turns out to be unsuitable, write down why and create a minimal
equivalent in `Bol` instead (`Bol Item Map` / `Bol Sync Log`). Keep the same
adapter API either way.

## Out of scope
Any bol API code.

## Error handling
- `install.py` functions are idempotent, so running them twice changes nothing.
- A failing `after_migrate` must raise. Don't swallow the error, or migrations
  will hide the problem.

## Security
- Bol Manager gets no permissions here. Each DocType grants its own.

## Tests
- `test_install.py`: running `ensure_role()` and `ensure_custom_fields()` twice
  causes no error and no duplicates.
- A smoke test proving the test runner works for this app.

## Acceptance criteria
- `bench --site X install-app marketplace_integrations` works on a site that has
  ERPNext and `ecommerce_integrations` installed, and fails with a clear message
  when they're missing.
- `bench migrate` twice in a row works.
- `pre-commit run --all-files` is clean, tests pass, and CI is green.
- `docs/decisions.md` contains the investigation results.
