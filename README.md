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
   - Every bol order is booked on that one customer, so the buyer is on the order itself: *bol Buyer Name*,
     *Company*, *VAT Number* and *KvK Number* (tab *Address & Contact*, from bol's billing details). They are
     copied to the Delivery Note and Sales Invoice (section *Customer's Purchase Order*), for invoices to business
     buyers.
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

**Shipments** (ADR-011). Submitting a Delivery Note for a bol order confirms the shipment at bol, with track & trace.

1. In Bol Settings (*Shipments* section) pick the **Default Transporter** (bol's code; PostNL is `TNT`) and leave
   **Require Track & Trace** ticked unless you send items without a code (e.g. letter post). Then tick **Push Shipments**
   (off by default). Workers and the scheduler must be running.
2. Make the Delivery Note from the bol Sales Order (*Create → Delivery Note*). One Delivery Note holds the items of **one**
   bol order only, all shipped by you (or all by bol: see *Verzenden via bol labels* below). Part of an order can be shipped: bol gets the quantities on
   the Delivery Note. Fill in **bol Track & Trace** (section *bol.com Shipment*, next to *Transporter Info*; *bol
   Transporter* is filled from the settings) and submit.
3. *bol Shipment Status* on the Delivery Note:

   | Status | Meaning |
   |---|---|
   | `Pending` | Sent to bol, which is processing it (usually a minute), or bol couldn't be reached and it's tried again automatically. |
   | `Confirmed` | bol has the shipment; *bol Shipment ID* is bol's id. The order items are shipped at bol. |
   | `Failed` | bol rejected it (e.g. more units than are still open), or the credentials failed. The timeline says why. Fix the cause and click **bol → Retry bol shipment** (System Manager / Bol Manager with write permission on the Delivery Note). |
   | `Unknown` | The connection broke while sending: bol may or may not have it. Within about 10 minutes bol is checked and the shipment confirmed or sent again; **Retry** does the same at once. |

   Before anything is sent, bol's shipments for the order are checked, so a shipment is never sent twice, also after a
   timeout or a retry. Failures are in the integration log (once per 6 hours for repeats) and on the timeline.
4. **Track & trace later.** Fill in *bol Track & Trace* on the submitted Delivery Note and *Update*: it's added to the
   shipment at bol. bol only accepts a code while the shipment has none; to change one bol already has, use the bol
   seller dashboard (the timeline says so).
5. A Delivery Note whose shipment is `Pending`, `Confirmed` or `Unknown` can't be cancelled: process a return instead
   (story 11). A `Failed` one can.
6. On the demo environment a shipment stays `Pending`: bol's demo never finishes its processes.

**Verzenden via bol labels** (ADR-012). Items bol ships itself (*bol Distribution Party* `BOL` on the Sales Order line)
must go with a bol label; the shipment is then confirmed with that label. bol charges a label only when the shipment
using it is confirmed.

1. Register for Verzenden via bol in the bol seller dashboard (*Instellingen → Diensten*); the API can't do it. In Bol
   Settings (*Verzenden via bol* section) tick **Use Verzenden via bol** (off by default; needs *Push Shipments*).
   Optional: **Preferred Label Transporter** (bol's code, e.g. `TNT`) is picked when bol recommends no label; otherwise
   the cheapest label is bought. **bol Labels for Own Shipments** also offers bol labels for items you ship yourself.
2. Make and **save** the Delivery Note, then click **bol → Get bol label** (System Manager / Bol Manager with write
   permission on the Delivery Note). bol's recommended (or preferred, or cheapest) label is bought; the timeline shows
   which and its price excl. VAT. *bol Label Status* becomes `Requested`.
3. Within a minute or two the label is `Ready`: `bol-label-<Delivery Note>.pdf` is attached (private: only people who
   may read the Delivery Note can open it) and you get a message. Print it, then submit the Delivery Note; no track &
   trace or transporter is needed.

   | Status | Meaning |
   |---|---|
   | `Requested` | bol is making the label. With a *bol Shipping Label ID* it's made but couldn't be downloaded: click **bol → Download bol label again** (it never buys a second label). |
   | `Ready` | The PDF is attached. The Delivery Note can be submitted. |
   | `Failed` | bol couldn't make it; the timeline says why. Fix the cause and click **Get bol label** again. |

4. A Delivery Note for items bol ships can't be submitted without a `Ready` label, and none while a label is
   `Requested`. Once a label is bought the items and quantities can't change: delete the draft and make a new one (an
   unused label isn't charged).
5. Messages when buying fails: *not registered for Verzenden via bol* (step 1); *No VVB option available* (the package
   doesn't fit bol's restrictions, e.g. too heavy); bol's own reasons for a rejected request. If the connection broke
   while buying, just try again.
6. On the demo environment a label stays `Requested`: bol's demo never finishes its processes.

**Cancellations** (ADR-013). The Sales Order follows bol's cancellations, and you can cancel lines on bol from it.
It runs while **Import Orders** is on; there's no separate switch.

1. In Bol Settings (*Orders* section) set **Notify User**: the person who gets a ToDo when a buyer asks to cancel.
   Without one, the request is only a comment on the Sales Order.
2. **Cancelled at bol** (by bol, or by the buyer before you accepted the order). Every 5 minutes, with the order
   poll, the Sales Order is brought in line. A daily check covers bol Sales Orders of the last 30 days that aren't
   fully delivered and are no longer in bol's open list.
   - Part of a line → its qty is lowered (ERPNext's *Update Items*, so totals, reservations and billing stay right).
   - A whole line, while other lines remain → the line is removed.
   - The whole order with nothing delivered → the Sales Order is cancelled (a draft one is deleted; the integration
     log keeps a record).
   - *bol Qty Cancelled* on the line (section *bol.com*) shows what bol has cancelled; the timeline says what changed.
   - A line is never lowered below what's delivered: that's an error in the integration log and on the timeline.
     Check the order with bol.
3. **The buyer asks to cancel.** The line gets *bol Cancellation Requested*, the Sales Order gets a comment, and
   Notify User gets one ToDo per Sales Order. You decide:
   - not shipped yet → **bol → Cancel on bol** with reason `REQUESTED_BY_CUSTOMER`;
   - already shipped → do nothing: bol tells the buyer to return the item.
4. **Cancel on bol** (System Manager / Bol Manager with write permission on the Sales Order). Tick the lines and pick
   bol's reason. bol cancels each line **as a whole**, so lines with anything delivered can't be chosen. One request
   is sent per line. The Sales Order is adjusted once bol has processed it, usually within a minute (see Bol Process).
   If bol refuses (e.g. it cancelled the item already), the Sales Order is read again from bol and brought in line.
   If some lines went through and others failed, the dialog says which.
5. **Cancel** on a bol Sales Order with lines still open at bol is refused ("Use Cancel on bol"): cancelling in ERPNext
   alone wouldn't cancel anything at bol.
6. On the demo environment a cancellation stays pending: bol's demo never finishes its processes, and it answers every
   request with the same process id.

**Returns** (ADR-014). Every return a buyer registers at bol becomes a draft return Delivery Note in ERPNext. Submit
it once the parcel is in and inspected, and bol gets the handling result.

1. In Bol Settings tick **Handle Returns** (*Returns* section). **Notify User** (*Orders* section) gets a ToDo for
   every return to inspect. Without one there's no ToDo; look for drafts with *bol RMA ID* set.
2. Every 30 minutes, each unhandled FBR return becomes a **draft return** against the Delivery Note that shipped it.
   It holds only the returned item, with bol's expected quantity. The *bol.com Return* section shows the RMA, the
   buyer's reason and the *bol Return Status* (`Open`).
   - If no submitted Delivery Note of that bol order can take the item back (e.g. it was shipped before the cutover),
     nothing is created: there's an error in the integration log and a ToDo with the return's details. Handle it by
     hand and in the bol seller dashboard.
3. **Parcel received:** set **bol Handling Result** (usually `RETURN_RECEIVED`), lower the qty if fewer came back, and
   submit. Submitting needs a handling result, and can't take back more than bol expects. bol is told in the
   background; *bol Return Status* becomes `Reported` once bol has processed it (see Bol Process).
4. **Nothing comes back** (e.g. `CUSTOMER_KEEPS_PRODUCT_PAID`): ERPNext can't submit a return of 0. On the draft use
   **bol → Report to bol without stock return**, pick the result, confirm. bol is told the expected quantity was
   handled (bol needs at least 1), and the draft is deleted. bol's answer appears on the original Delivery Note. If
   bol fails it, the return comes back as a new draft on the next poll.
5. `Failed`: bol refused the report; the timeline says why. Fix it (or handle it in the seller dashboard) and use
   **bol → Retry bol return report**. If bol had handled the return already, it's marked `Reported` instead of
   failing. bol unreachable: the return stays `Open` and is sent again automatically after about 10 minutes.
6. The credit note comes from *Accounting* below.
7. On the demo environment the one demo return belongs to an order that isn't imported, so it ends in the
   "no Delivery Note" log (and ToDo, with Notify User set).

**Accounting** (ADR-015). bol sales are invoiced on a bol receivable, returns get credit notes, and bol's own invoices
come in as draft Purchase Invoices to review. Have your accountant confirm the accounts and VAT treatment first.

1. **Settings** (*Accounting* section of Bol Settings, every switch off by default):
   - **Invoice Shipments and Returns** + **Receivable Account** (e.g. a *Debtors - bol* account of type Receivable).
     Needs *Push Shipments*.
   - **Upload Requested Invoices**: when a business buyer asks bol for an invoice, it's uploaded for them.
   - **Import bol Invoices** + **Supplier** (e.g. *bol.com*), **Purchase Tax Template** (VAT on top of bol's amounts,
     which exclude it) and **Default Expense Account**. Optionally map bol's line types (`COMMISSION`,
     `SHIPMENT_LABEL`, `CORRECTION_COMMISSION`, …) to their own account in **bol Invoice Accounts**. A type without
     VAT (e.g. `COMPENSATION`): map it to a non-stock Item that has a 0 % Item Tax Template, else the 21 % template
     makes the totals mismatch.
2. **Sales Invoices.** Once bol has confirmed a shipment, its Delivery Note is invoiced and submitted in the
   background: ERPNext's own *Make Sales Invoice*, on the Receivable Account, dated on the Delivery Note, with the
   *bol Order ID*. A Delivery Note that already has an invoice (one made by hand too, even a draft) is left alone.
3. **Credit notes.** A bol return reported to bol as `RETURN_RECEIVED` or `EXCHANGE_PRODUCT` gets a submitted credit
   note against that Sales Invoice. Other results (and *Report to bol without stock return*) get none.
4. **Failures** (closed period, missing account, no Sales Invoice to credit yet, …): nothing is made; the Delivery
   Note's timeline and the integration log say why. Fix it and use **bol → Create bol Sales Invoice** (or *Create bol
   credit note*) on the Delivery Note. A daily sweep also retries Delivery Notes of the last 30 days that never had an
   invoice (one with a cancelled invoice is left to you).
5. **No Payment Entries.** bol pays out the sales minus its fees. Match each payout in **Bank Reconciliation** against
   the Receivable Account (the sales) and the bol supplier (the fees on the Purchase Invoices).
6. **bol invoices** (daily). Each bol invoice becomes a **draft** Purchase Invoice: *Supplier Invoice No* and *bol
   Invoice ID* = bol's invoice number, dated on bol's issue date, one line per line type, bol's PDF attached
   (private). It's never submitted automatically: review and submit it yourself.
   - `TURNOVER` / `CORRECTION_TURNOVER` (the sales bol collected for you) are **not** on it: the bol Sales Invoices
     booked them already. The remarks say how much they were.
   - Line types not in the map are booked on the default account and listed in the remarks.
   - **`MISMATCH:`** first in the remarks (and an error log) means the draft's net or VAT is more than 0.01 off bol's
     invoice (turnover left out). ERPNext's figures are kept; find out why before submitting.
   - A negative total (bol owes you, e.g. compensation) comes in as a **debit note** (*Is Return*).
   - The import reads from *bol Invoices Imported Up To* minus 7 days. An invoice that fails is logged and the date
     stays put, so the next run tries it again.
7. **Invoice requests** (daily): for each open request (and each upload bol rejected for a virus) the Sales Invoice is
   printed with the **bol Sales Invoice** print format (Dutch, with the buyer's name, company, address, VAT and KvK
   numbers) and uploaded. The upload is followed as a Bol Process; the Sales Invoice's timeline says when it's done.
   A request for a shipment without a Sales Invoice here is logged; upload that one in the bol seller dashboard.
   The PDF is made by wkhtmltopdf, which needs the workers to reach the site's URL (`host_name`).
   **Notify User** (*Orders* section) gets a ToDo for every incoming request, on the Sales Invoice (or the Delivery
   Note, or nothing when the shipment isn't known here). It **closes itself** once bol has the upload, so an open one
   needs you: the timeline and the integration log say why. Closed ones are the history of requests.
8. On the demo environment the invoice import ends in an error log (bol's demo "PDF" isn't a PDF), and the invoice
   requests belong to shipments that don't exist here.

**New products on bol: export for the bulk upload** (ADR-016). Products bol doesn't know yet (new EANs) are created
in the bol seller dashboard (*Items → Add items in bulk*, the option for items not yet on bol), from a file ERPNext
makes. There's no content upload through the API (story 14 was dropped).

1. Open the report **bol Bulk Upload Export** (System Manager or Bol Manager, with read access to Items). Filter on
   *Item Group*, *Brand* or one *Template* if you like. *Only items not on bol yet* (on by default) leaves out Items
   that already have a bol offer.
2. It lists every product that isn't a template: one row per variant, with **EAN**, **Title** (*Item Name*),
   **Description** (cleaned to the HTML bol accepts: `b br h3 li ol ul p strong`), **Brand**, **Internal Reference**
   (item code), **Family Key** (the template, so variants can be grouped), one column per variant attribute (*Size*,
   *Colour*, …), **Image URL** (Shopify's CDN URL) and **Item Group**. A variant without its own description, brand or
   image takes its template's. Items without a valid EAN barcode are left out and named above the table.
3. Click **Download for bol** (not the standard *Export*, which turns descriptions into flat text). The file has one
   tab and the column names in row 1, as bol requires.
4. **Translate** Title and Description into Dutch in the file: bol creates new items only from Dutch content. Then
   upload it in *Add items in bulk*, link EAN, title and description to bol's fields, check the suggested category per
   set of items and the attribute matches (skip *Item Group*, *Internal Reference* if bol offers no match), and fill
   what bol marks red.
5. Don't enter prices and stock there: once the item exists, tick **Sell on bol** on the Item and the offer sync
   (above) creates the offer.

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
