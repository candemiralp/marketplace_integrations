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

**Offer sync** (ADR-008). Keeps each Item's bol offer in line with ERPNext: price, delivery promise,
economic operator and on-hold state. Stock follows in the stock sync.

1. In Bol Settings, pick a **Price List**: selling, in EUR, with prices **including VAT** (bol shows them
   as is). Set *Default Max Days to Customer*. Then tick **Sync Offers** (off by default).
2. Changes are pushed within a minute: saving an Item (*Sell on bol*, *Disabled*, delivery days, Brand), an
   Item Price in that price list, or a Brand's economic operator. Background workers must be running.
   - Ticked and enabled → the offer is created (or, if bol already has one for the EAN, linked) and put
     on sale. A new offer starts with stock 0 until the stock sync sets it.
   - Unticked or disabled → the offer is put **on hold**, never deleted. Re-enabling puts it back on sale.
3. *bol Offer Status* on the Item:

   | Status | Meaning |
   |---|---|
   | `For sale` | Live on bol. |
   | `Not for sale` | bol doesn't sell it; *bol Status Detail* lists bol's reasons per country. |
   | `On hold` | Taken off sale by us (item unticked or disabled). |
   | `Not synced` | We couldn't update bol: *bol Status Detail* says why (no Item Price, price outside 1–9999, no economic operator, bol rejected the data). A live offer keeps its last values. Fix the data and save the Item. |
   | `Deleted` | Deleted with the button below, or gone at bol. |

   Failures are logged once per 6 hours in the integration log and commented on the Item.
4. **Delete bol offer** (Item form, *bol* menu, System Manager / Bol Manager) deletes the offer at bol after
   a confirmation and unticks *Sell on bol*. Use it only when the item will never be sold on bol again.
5. **Daily reconcile** refreshes every Item's status and reasons from bol, and re-syncs any offer that differs
   from ERPNext, has vanished at bol, or was never created. **After adopting existing offers**, check the
   prices in the bol price list before switching on *Sync Offers*: the first reconcile brings every adopted
   offer in line with ERPNext (price, delivery promise, reference = item code).

The bol demo environment doesn't accept offer writes (PATCH gives HTTP 403), so on Demo the sync can only
be checked up to reading offers.

**Stock sync** (ADR-009). Keeps the stock on each bol offer equal to what ERPNext can sell.

1. In Bol Settings (*Stock* section), pick the **Warehouse** bol orders ship from (not a group warehouse) and a
   **Stock Buffer** (default 1). Then tick **Sync Stock** (off by default; needs *Sync Offers*).
2. Per item, bol gets `actual qty − reserved for open Sales Orders − reserved stock − buffer` in that
   warehouse, rounded down, between 0 and 999. Incoming stock (purchase orders) doesn't count. An Item can set
   its own *bol Stock Buffer* (Sales tab, *bol.com*); 0 means the Settings value. Raise the buffer for items
   that sell fast on both bol and Shopify: both channels draw on the same warehouse stock.
3. Every minute the scheduler compares each bol item's available quantity with what bol last confirmed and
   pushes only the differences. Any stock movement (Delivery Note, Stock Entry, Purchase Receipt…), a new or
   cancelled Sales Order, or a buffer change reaches bol within about a minute. The scheduler and background
   workers must be running. Nothing is sent when nothing changed, so stock 0 is sent once.
4. bol keeps subtracting its own open orders (*corrected stock*) until our order import has booked them.
5. Failures (bol unreachable, rejected) are logged once per 6 hours and commented on the Item. The next minute
   tries again. If the offer is gone at bol, the offer sync recreates it with its stock.
6. **Nightly reconcile** (05:00) reads every offer at bol and corrects any stock that differs. Its result is an
   integration log entry with method `reconcile_stock`: `X checked, Y corrected, Z failed`. On a healthy system
   *corrected* is 0. A steady number of corrections means pushes are being missed; check the workers.

**Order import** (ADR-010). Every new FBR order at bol becomes one Sales Order.

1. Create a Customer for bol (e.g. *bol.com*) and two Sales Taxes and Charges Templates for the bol Company,
   one for NL buyers and one for BE buyers (OSS). Tick *Is this Tax included in Basic Rate?* on **every** row:
   bol's prices include VAT.
2. In Bol Settings (*Orders* section) set **Customer**, **Tax Template NL**, **Tax Template BE** and the
   *Warehouse* (*Stock* section; order lines are booked on it). At cutover, set **Import Orders Placed After** to
   the moment the old Shopify↔bol app stopped, so orders it already brought in aren't imported twice. Leave
   **Submit Orders** ticked, or untick it to review new orders as drafts first. Then tick **Import Orders**
   (off by default).
3. Every 5 minutes the open FBR orders at bol are read. Each order without a Sales Order is imported:
   - *Customer* is the bol customer; *Customer's Purchase Order* and *bol Order ID* are bol's order number. The
     shipping address (and a billing address if it's different) is a new Address with *bol Buyer* ticked,
     linked to that customer. The e-mail on it is bol's relay address: use it only for the shipping label.
   - One line per open order item: the Item is found by its EAN barcode. The rate is what the buyer paid per unit
     (bol's price minus bol's discount, shown as *Discount Amount*). Each line has *bol Order Item ID*,
     *Distribution Party*, *Latest Delivery Date* and *Commission* (section *bol.com*). Units already shipped or
     cancelled at bol are left out; an order with nothing left to ship is skipped.
   - The tax template follows the shipping country (BE → BE, NL → NL). The grand total equals bol's total
     (rounded total is off).
   - *Delivery Date* is the earliest delivery date bol requires.
   - A comment on the Sales Order flags an offer reference that doesn't match the Item found by EAN (the EAN
     wins), or a discount that doesn't split into whole cents per unit (the total is then a cent or two off).
4. **Failures** (no Item with the EAN, several Items with it, a buyer outside NL/BE, a validation error) leave no
   Sales Order and no Address behind. They're in **Ecommerce Integration Log** with method `import_order`, the
   full bol order and the traceback, once per 6 hours per order. Fix the cause (e.g. add the barcode to the Item);
   the next poll imports the order. One failing order never holds up the others.
5. Polling again never creates a second Sales Order for the same bol order, even when two imports run at once.

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
