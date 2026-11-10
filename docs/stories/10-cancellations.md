# 10 — Cancellations

**Goal:** cancellations in both directions keep the Sales Order and bol consistent:
customer requests, cancellations made on bol's side, and cancellations initiated
by the seller from ERPNext.
**Depends on:** 03, 07

## bol API — Orders v10 (verify)
- Order items carry `cancellationRequest` (bool) and `quantityCancelled`.
- `PUT /retailer/orders/cancellation`
  `{orderItems: [{orderItemId, reasonCode}]}` → 202 + process status.
  `reasonCode` enum (verify): `OUT_OF_STOCK`, `REQUESTED_BY_CUSTOMER`,
  `BAD_CONDITION`, `HIGHER_SHIPCOST`, `INCORRECT_PRICE`, `NOT_AVAIL_IN_TIME`,
  `NO_BOL_GUARANTEE`, `ORDERED_TWICE`, `RETAIN_ITEM`, `TECH_ISSUE`,
  `UNFINDABLE_ITEM`, `OTHER`.
- Check in the spec whether a cancellation covers the **whole order item** or a quantity.

## Data model
Sales Order Item custom fields: `bol_cancellation_requested` (Check, read-only),
`bol_qty_cancelled` (Float, read-only).
New field in `Bol Settings`: `notify_user` (Link User). It receives ToDos for
customer requests.

## Behaviour (`bol/cancellations.py`)

### A. Detect changes on existing orders
`sync_existing_order_changes(order_ids)` is called from the order poll (07) for
orders already in ERPNext. It uses the OPEN list data; fetch the detail only
when the list lacks these fields.
- New `cancellationRequest = true` on a line → set
  `bol_cancellation_requested`, add a comment on the SO, and create one ToDo for
  `notify_user` (deduplicated by the open ToDo on the same SO). The seller decides:
  - not shipped yet → cancel with reason `REQUESTED_BY_CUSTOMER` (C);
  - already shipped → ignore the request (bol tells the customer to return the item).
- `quantityCancelled` higher than `bol_qty_cancelled` → apply the local
  adjustment (B).
Orders that left the OPEN list (fully cancelled or shipped) also need checking:
a daily job runs over SOs that are not fully delivered, with a `bol_order_id`,
created in the last 30 days, and GETs each order.

### B. Apply a cancellation locally (`apply_cancelled_qty(so, item_id, new_cancelled)`)
- `delta = new_cancelled - bol_qty_cancelled`; if `delta <= 0` → no-op (idempotent).
- If every line would reach `qty - cancelled == 0` and nothing is delivered →
  cancel the SO.
- Otherwise reduce the line qty with ERPNext's `update_child_qty_rate`. This
  keeps reservations and billing consistent. It must never go below the
  delivered qty; if it would, log an error instead.
- Set `bol_qty_cancelled = new_cancelled` and add a comment.

### C. Seller-initiated cancellation
Whitelisted `cancel_bol_items(sales_order, so_details: list[str], reason_code)`.
Needs SO write permission plus the Bol Manager role. It validates the reason code
against the enum, and that the lines aren't delivered or already cancelled.
1. PUT the cancellation → `track(resp, "cancellation", so, payload={so_details, item_ids})`.
2. `on_success` → `apply_cancelled_qty` for each line (full line qty).
   `on_failure` → comment + log.
3. SO form: a **Cancel on bol** button opens a dialog with line checkboxes and a
   reason select.

## Error handling
- Every local adjustment runs in a savepoint. If `update_child_qty_rate` fails,
  roll back and `log_once`; it's retried on the next poll.
- A seller cancel for lines that bol already cancelled → bol returns 400 → the
  failure handler re-syncs from bol (GET order) instead of only failing.
- A user cancelling the SO directly in ERPNext does **not** cancel it on bol.
  Block `Sales Order.before_cancel` for bol orders with open lines on bol, with
  the message "Use Cancel on bol".

## Tests
- Request flag → ToDo once; repeated polls don't create a second ToDo.
- `apply_cancelled_qty`: full cancel → SO cancelled; partial → qty reduced; repeated
  call → no-op; below delivered → error logged.
- Seller cancel: validation (delivered line, bad reason); success applies the qty;
  failure comments; already cancelled → re-sync.
- `before_cancel` guard.

## Acceptance criteria
- A cancellation made on bol is reflected on the SO by the next poll.
- A seller cancel from the SO completes on bol and adjusts the SO automatically.
- The SO can never stay open for quantities bol has cancelled.
