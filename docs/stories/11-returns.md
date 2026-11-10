# 11 — Returns

**Goal:** every bol return registered by a customer becomes a draft Sales Return
in ERPNext. When it's received and inspected, submitting it reports the handling
result to bol.
**Depends on:** 03, 08

## bol API — Returns v10 (verify)
- `GET /retailer/returns?handled=false&fulfilment-method=FBR&page=N`.
- `GET /retailer/returns/{return-id}` → `returnId`, `registrationDateTime`,
  `returnItems[] {rmaId, orderId, ean, expectedQuantity, returnReason
  {mainReason, detailedReason, customerComments}, trackAndTrace, handled}`.
- `PUT /retailer/returns/{rma-id}` `{handlingResult, quantityReturned}` → 202 +
  process status. `handlingResult` enum (verify): `RETURN_RECEIVED`,
  `EXCHANGE_PRODUCT`, `RETURN_DOES_NOT_MEET_CONDITIONS`, `REPAIR_PRODUCT`,
  `CUSTOMER_KEEPS_PRODUCT_PAID`, `STILL_APPROVED`.
- Out of scope: creating returns for unregistered packages (`POST /retailer/returns`).
  Do those in the dashboard.

## Data model
Delivery Note custom fields (used on return DNs):
`bol_rma_id` (Data, **unique**, no_copy, read-only),
`bol_return_reason` (Small Text, read-only),
`bol_handling_result` (Select = enum above),
`bol_return_status` (Select `\nOpen\nReported\nFailed`, read-only, allow_on_submit).
New fields in `Bol Settings`: `handle_returns` (Check).

## Behaviour (`bol/returns.py`)

### Poll (cron every 30 min, `job_id="bol_poll_returns"`)
For each unhandled `rmaId` without a return DN (bulk existence check):
1. Find the original submitted, non-return DN through
   `SO.bol_order_id == orderId` → DN items with a matching `item_code` (via EAN).
   If there are several DNs, pick the one whose shipped quantity covers
   `expectedQuantity`.
2. Create a **draft** return with ERPNext's `make_sales_return(dn)` and keep only
   the matching line, with `qty = -expectedQuantity`. Set `bol_rma_id`, the
   reason text (main + detailed + comments, truncated), status `Open`. Insert it
   in a savepoint and commit.
3. ToDo for `notify_user`: "bol return {rmaId}: inspect and submit".
4. Original DN not found (e.g. shipped before the cutover) → `log_once` + ToDo
   with the return details. No return DN is created.

### Reporting the result
- `before_submit` on a return DN with `bol_rma_id`: `bol_handling_result` is
  required; `abs(qty)` must be ≤ `expectedQuantity`. Store the expected quantity
  in a hidden field at creation.
- `on_submit` → enqueue `report_handling(dn)`: PUT with
  `quantityReturned = abs(qty)` → track with handler `return_handling`.
  `on_success` → `Reported`. `on_failure` → `Failed` + comment; Retry button.
- Nothing physically received (e.g. `CUSTOMER_KEEPS_PRODUCT_PAID`): ERPNext
  can't submit a zero-qty return, so the draft has a button **Report to bol
  without stock return** (whitelisted, Bol Manager). It sends the PUT with
  `quantityReturned = 0` and the chosen result. After that request succeeds, it
  **deletes the draft**. Record this decision in `docs/decisions.md`.

## Error handling
- Idempotency: unique `bol_rma_id`; a `DuplicateEntryError` means it was already created.
- A PUT for an RMA that bol already marked as handled → 400 → GET the return; if
  it's handled, mark `Reported` with a comment instead of `Failed`.
- Credit notes are story 12. Never create financial documents here.

## Tests
- Mapping a return to its original DN (single, multiple DNs, not found).
- Draft return contains only the returned line, with the correct negative qty.
- Duplicate poll → one return. Submit without a handling result → blocked. Qty
  above expected → blocked.
- Report success/failure; already handled → Reported; the "without stock return" path.

## Acceptance criteria
- A demo return appears as a draft Sales Return linked to the original DN within
  30 minutes. Submitting it reports to bol, and the status is visible on the document.
