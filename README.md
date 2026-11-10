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

**Async operations (Bol Process).** bol handles shipments, cancellations, returns, labels and
content uploads asynchronously. Each one is a **Bol Process** row (read-only), followed every minute
until bol reports a final state:

| Status | Meaning |
|---|---|
| `PENDING` | Waiting for bol. Checked again after 10 s, 30 s, 1 m, 2 m, 5 m, then every 10 m. |
| `SUCCESS` | bol processed it; the follow-up (e.g. marking the delivery shipped) ran. |
| `FAILURE` / `TIMEOUT` | bol rejected it or didn't finish within 24 h. *Error Message* says why. The related document gets a comment and the failure is in the integration log. Fix the cause and start the action again from that document. |
| `HANDLER_ERROR` | bol is done, but our follow-up code failed. The related document and the Bol Process have a comment, and the traceback is in the integration log. After the bug is fixed, click **Retry handler** (System Manager / Bol Manager). |

Polling only runs while *Enabled* is ticked in Bol Settings. Final rows are deleted after 90 days;
`HANDLER_ERROR` rows are kept until someone retries them.

**Catalog: which Items are sold on bol** (ADR-007)

1. **Economic operator per Brand.** Create the operators in the bol seller dashboard. On each Brand,
   click **Select bol economic operator** (System Manager / Bol Manager) and pick one. If bol hasn't
   marked the operator `VALID` yet, it's saved anyway, but offers linked to it won't be published.
2. **Delivery promise.** Set *Default Min/Max Days to Customer* in Bol Settings. An Item can override
   them in its *bol.com* section (Sales tab); 0 means "use the default".
3. **Mark Items.** Tick **Sell on bol** on the Item. It saves only when the Item is a stock item (not a
   template), has exactly one barcode of type *EAN* with a valid 13-digit GTIN, and its Brand has an
   economic operator. The message lists everything that's missing. While the box is ticked, these
   checks run on every save.
4. *bol Offer Status* (`For sale`, `Not for sale`, `On hold`) and *bol Status Detail* are read-only.
   bol writes them.

**Adopt existing offers** (one-off, at cutover). Offers that already exist at bol (created by the old
Shopify↔bol app) are linked to their Items, never created again.

1. Enable the integration, save, and click **Adopt existing offers** in Bol Settings. It runs in the
   background and only reads from bol.
2. Each offer is matched to an Item through its EAN in the Item's barcodes. UPC-A (12 digits) and
   GTIN-14 forms of the same number match too. A match is stored as an *Ecommerce Item* (integration
   *Bol*, *Integration Item Code* = offer ID, *SKU* = EAN). The Item gets *Sell on bol* ticked and
   the offer's status copied, without the checks from step 3 above: fix what's missing later.
3. When it's done you get a message with the counts. The full report is the newest *Ecommerce
   Integration Log* entry with method `adopt_existing_offers`:
   - **unmatched**: no Item has this EAN. Add the barcode to the right Item, or delete the offer at bol.
   - **ambiguous**: several Items have this EAN. Remove the duplicate barcode.
   - **conflict**: the offer is already linked to another Item, or the Item/EAN to another offer.
     Nothing was changed. Correct or delete the wrong *Ecommerce Item*.
   - **failed**: unexpected error. There is a separate error log with the traceback.
4. Run it again after fixing: already linked offers are left as they are. If bol couldn't be reached,
   or kept sending the same page, the job stops, logs how far it got, and a rerun is safe. An offer
   bol sends twice is counted once (`repeated` in the report says how often that happened).

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

Tracking an async bol call (ADR-006):

```python
from marketplace_integrations.bol import process_status

# at import time in your feature module (and add the module to process_status.HANDLER_MODULES)
process_status.register("shipment", on_shipment_confirmed, on_shipment_failed)

response = client.request("POST", "/retailer/shipments", version=10, json=body)  # 202
process_status.track(response, "shipment", delivery_note, payload={"order_item_ids": ids})
```

`on_success(doc, entity_id, payload)` and `on_failure(doc, message, payload)` must be idempotent: they
run again on **Retry handler**. They run inside a savepoint; if they raise, their work is rolled back.

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
