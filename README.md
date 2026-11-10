### Marketplace Integrations

Connects ERPNext directly to the bol.com Retailer API (Fulfilment by Retailer): offers, stock,
orders, shipments, labels, cancellations, returns and invoices. ERPNext is the hub. Shopify keeps
running on `ecommerce_integrations`, whose `Ecommerce Item` and `Ecommerce Integration Log` this
app reuses.

Design and engineering standards: [`docs/stories/00-overview.md`](docs/stories/00-overview.md).
Decisions and spec differences: [`docs/decisions.md`](docs/decisions.md).

### Requirements

Frappe / ERPNext v15 and `ecommerce_integrations` (branch `version-15`).

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app --branch version-15 https://github.com/frappe/erpnext
bench get-app --branch version-15 https://github.com/frappe/ecommerce_integrations
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site $SITE install-app marketplace_integrations
```

`required_apps` makes `install-app` install ERPNext and `ecommerce_integrations` first. If either
isn't in the bench, the install stops with `Could not find app "<name>"`.

Installing (and every `bench migrate`) creates the **Bol Manager** role and applies the app's custom
fields. Both steps are idempotent. Bol Manager has no permissions of its own: each feature's DocTypes
grant them.

### How to use / operate

**Connect to bol**

1. Create API credentials in the bol seller dashboard (*Instellingen → Diensten → API instellingen*).
   In this dev environment use **demo** credentials only.
2. Open **Bol Settings** (System Manager or Bol Manager). Fill in *Client ID*, *Client Secret*,
   *Company*. Choose *Environment* (*Demo* by default).
3. Save, then click **Test Connection**. It fetches a token and lists open orders. An error message
   says whether the credentials were rejected or bol couldn't be reached.
4. Tick **Enabled** to switch the integration on. Saving never calls bol.

**Logs.** Every failed bol call and every successful write (POST/PUT/PATCH/DELETE) is recorded in
**Ecommerce Integration Log** (integration *Bol*), with request, response, traceback and the related
document (*Reference DocType / Name*). Tick **Debug Logging** to log successful GETs too. Turn it
off again: logs hold buyer data. Authorization headers, tokens and the client secret are masked.
Only System Manager and Bol Manager can read the logs.

**Tokens** are cached in Redis only, until 30 s before they expire. Saving Bol Settings clears the
cached token.

**For developers** (details in `docs/decisions.md`, ADR-004/005):

```python
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError
from marketplace_integrations.bol.log import log_call, log_once

client = get_client()  # raises BolError when the integration is disabled
try:
    orders = client.request("GET", "/retailer/orders", version=10, params={"status": "OPEN"})
except BolError as err:
    frappe.db.rollback(save_point="record")  # undo your own work first
    log_once("order", order_id, **err.details, error=err, reference=("Sales Order", so_name))
```

- Pass the path exactly as bol's spec writes it; the client handles the demo prefix, retries
  (§4.1), token refresh and error mapping.
- `err.ambiguous` is `True` when a POST may have been processed. Check bol's state before trying
  again.
- Logs are written when the transaction commits or rolls back, never in the middle of it.

### Development

In the dev environment, bench runs inside a container. Use the `make` targets from the
environment repo:

```bash
make migrate                     # migrate dev.localhost and test.localhost
make test                        # all tests on test.localhost
make test ARGS="--module marketplace_integrations.tests.test_install"
make lint                        # pre-commit run --all-files
```

On a plain bench: `bench --site <test-site> run-tests --app marketplace_integrations`
(the site needs `allow_tests`).

- Custom fields on other apps' DocTypes go in `marketplace_integrations/setup/custom_fields.py`
  only. Field names start with `bol_`; bol identifiers are `read_only` and `no_copy`.
- bol API specs downloaded for a story go in `docs/bol-openapi/`.

### Linting

`pre-commit` runs ruff (lint + format), Frappe's semgrep rules, prettier and eslint:

```bash
cd apps/marketplace_integrations
pre-commit install
```

### CI

- **CI** (`.github/workflows/ci.yml`): sets up a v15 bench with ERPNext and
  `ecommerce_integrations`, installs the app and runs the tests on every push to `develop` and on PRs.
- **Linters** (`.github/workflows/linter.yml`): pre-commit, Frappe semgrep rules and pip-audit on PRs.

### License

MIT
