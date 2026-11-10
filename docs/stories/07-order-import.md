# 07 — Order import

**Goal:** every new FBR bol order becomes exactly one submitted ERPNext Sales
Order, with a delivery address, correct VAT treatment and order-item IDs for
later shipment and cancellation.
**Depends on:** 04

## bol API — Orders v10 (verify)
- `GET /retailer/orders?fulfilment-method=FBR&status=OPEN&page=N` (50 per page;
  stop on an empty page).
- `GET /retailer/orders/{order-id}` → `orderId`, `orderPlacedDateTime`,
  `shipmentDetails {salutation, firstName, surname, streetName, houseNumber,
  houseNumberExtension, extraAddressInformation, zipCode, city, countryCode,
  email, company, deliveryPhoneNumber, language}`, `billingDetails {…, vatNumber}`,
  `orderItems[] {orderItemId, cancellationRequest, fulfilment {method,
  distributionParty, latestDeliveryDate, expiryDate, timeFrameType},
  offer {offerId, reference}, product {ean, title}, quantity, quantityShipped,
  quantityCancelled, unitPrice, commission}`.
- `unitPrice` **includes VAT**. `email` is a bol relay address.
- bol is **not** an archive: store what you need.

## Data model
Custom fields:

| DocType | Field | Type | Notes |
|---|---|---|---|
| Sales Order | `bol_order_id` | Data | **unique**, no_copy, read-only, in standard filter |
| Sales Order Item | `bol_order_item_id` | Data | no_copy, read-only |
| Sales Order Item | `bol_distribution_party` | Select `RETAILER\nBOL` | read-only |
| Sales Order Item | `bol_latest_delivery_date` | Date | read-only |
| Sales Order Item | `bol_commission` | Currency | read-only, for reporting |
| Address | `bol_buyer` | Check | marks bol buyer PII (used in 15) |

New fields in `Bol Settings`: `import_orders` (Check), `customer` (Link Customer
"bol.com"), `tax_template_nl`, `tax_template_be` (Link Sales Taxes and Charges
Template; both must be "included in print rate"), `import_orders_placed_after`
(Datetime, cutover guard), `submit_orders` (Check, default 1).
`validate()` checks that the tax template rows are actually `included_in_print_rate`.

## Behaviour (`bol/orders.py`)

### Poll (cron `*/5 * * * *`, `job_id="bol_poll_orders"`, dedupe)
1. Page through the OPEN FBR orders. Collect the order IDs.
2. Remove the IDs that already exist (one `frappe.get_all` with `bol_order_id in […]`).
3. For each new ID: `import_order(order_id)` inside a savepoint, then commit
   that record.
4. Leave a hook for story 10: pass the IDs that already existed to
   `sync_existing_order_changes(ids)`. Until story 10 is done, that function is
   a no-op stub.

### `import_order(order_id)`
1. GET the order detail.
2. If `orderPlacedDateTime < import_orders_placed_after` → skip silently (debug log).
3. Pure mapper `bol/mappers/order.py::to_sales_order(order, ctx) -> dict`, where
   `ctx` holds the customer, company, warehouse, the `ean -> item_code` map,
   country names and tax templates. It returns the SO dict and address dicts.
   - Open quantity per line = `quantity - quantityCancelled - quantityShipped`.
     Lines with 0 are skipped. If every line is 0 → skip the order.
   - Item: resolved via EAN. Cross-check `offer.reference == item_code` when
     present. If they disagree, trust the EAN and add a warning.
   - `rate = unitPrice`, `ignore_pricing_rule = 1`, `po_no = orderId`,
     `transaction_date` = the placed date in the system timezone,
     `delivery_date` = the minimum `latestDeliveryDate`, or the placed date + 1
     if it's missing.
   - Taxes: `tax_template_be` if the shipment country is `BE`, otherwise
     `tax_template_nl`. Any other country → `BolMappingError` (unsupported).
   - Address: `address_line1 = f"{streetName} {houseNumber}{' ' + ext if ext}"`,
     `address_line2 = extraAddressInformation`, `pincode`, `city`, `country` (via
     the Country `code`), `email_id`, `phone`,
     `address_title = company or f"{firstName} {surname}"`, linked to the bol
     customer, `bol_buyer = 1`. Create a separate billing address only if it differs.
4. Insert the addresses and the SO (`flags.ignore_permissions = True`; this is a
   system job), then submit if `submit_orders`.
5. A `frappe.DuplicateEntryError` on `bol_order_id` → treat it as already
   imported (a concurrent run); roll back to the savepoint and continue.

## Error handling
- Unknown EAN, unsupported country, or a validation error on submit (no Item
  Price is needed — bol's `unitPrice` is used) → roll back the whole order (no orphan
  address) and `log_once(kind="order", key=order_id)` with the full payload. The
  next poll retries it automatically after the data is fixed.
- An order that fails for more than 24h → the daily digest (15) lists it.
- One failing order never blocks the rest of the batch.
- Times: convert with the single helper from overview §4.3. Never use naive
  `datetime.now()` comparisons against bol timestamps.

## Security & privacy
- Buyer PII lives only on Address/SO and in the integration logs (restricted).
- Never use the buyer's relay email for anything except the shipping label.

## Tests
- Mapper: NL address with extension; BE address → BE taxes; company buyer;
  separate billing; partial `quantityCancelled`/`quantityShipped`; all lines
  closed → None; unknown EAN → error; reference mismatch → warning; timezone conversion.
- Flow: new order → SO submitted with item-level IDs; the same order polled
  twice → one SO; simulated concurrent insert → no error, one SO; a failing
  order rolls back and doesn't stop the next; cutover guard skips old orders;
  pagination.

## Acceptance criteria
- A test order in the demo environment becomes a submitted SO within 5 minutes,
  with the correct address, VAT-inclusive total equal to bol's total, and
  `bol_order_item_id` on every line.
- Import is idempotent under repeated and concurrent polls.
