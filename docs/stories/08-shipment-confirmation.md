# 08 — Shipment confirmation

**Goal:** submitting a Delivery Note for a bol order confirms the shipment on bol,
with track & trace, exactly once, and the result is visible on the Delivery Note.
**Depends on:** 03, 07

## bol API — Shipments / Transports v10 (verify)
- `POST /retailer/shipments` body:
  `{orderItems: [{orderItemId, quantity}], shipmentReference, shippingLabelId?,
  transport?: {transporterCode, trackAndTrace}}` → 202 + process status
  (`entityId` = shipmentId).
- `GET /retailer/shipments?order-id=…` and `GET /retailer/shipments/{id}`
  (contains `transport.transportId`).
- `PUT /retailer/transports/{transport-id}` `{transporterCode, trackAndTrace}`
  adds or corrects track & trace later (same day). 202 + process status.
- `transporterCode` is an enum (PostNL is `TNT` in bol's list; **check the
  enum in the spec**).

## Data model
Custom fields on **Delivery Note**:

| Field | Type | Notes |
|---|---|---|
| `bol_order_id` | Data | read-only, fetched from the SO at validate |
| `bol_transporter_code` | Select | options = the verified enum subset; default from Settings |
| `bol_track_and_trace` | Data | `allow_on_submit` |
| `bol_shipment_status` | Select `\nPending\nConfirmed\nFailed\nUnknown` | read-only, allow_on_submit, no_copy |
| `bol_shipment_id` | Data | read-only, allow_on_submit, no_copy |

New fields in `Bol Settings`: `push_shipments` (Check),
`default_transporter_code`, `require_track_and_trace` (Check, default 1).

## Behaviour (`bol/shipments.py`)

### Validation (`Delivery Note.validate` / `before_submit`)
- Items come from at most **one** bol order. Mixing bol with non-bol SOs, or two
  bol orders, → throw.
- All bol lines share one `bol_distribution_party`. A mix → throw ("create
  separate Delivery Notes").
- `RETAILER` lines with `require_track_and_trace` and an empty T&T → throw.
  `BOL` lines need a VVB label (story 09 adds that check).
- Every bol line has `so_detail`, which resolves to a `bol_order_item_id`.

### On submit
`Delivery Note.on_submit` → if `push_shipments`, set status `Pending` and enqueue
`confirm_shipment(dn_name)` (`job_id=f"bol_ship::{dn}"`, after commit).

### `confirm_shipment(dn_name)`
1. Skip if the status is `Confirmed`, or `Pending` with an open `Bol Process`
   (idempotency).
2. Build the payload with the pure mapper `bol/mappers/shipment.py`:
   aggregate quantity per `orderItemId`; `shipmentReference = dn.name`;
   `shippingLabelId` if it's set (09), otherwise `transport`.
3. POST → `process_status.track(resp, "shipment", dn, payload={orderItemIds})`.
4. `on_success`: set `bol_shipment_id = entity_id`, status `Confirmed`, comment.
   `on_failure`: status `Failed`, comment with bol's `errorMessage`, log.
5. `BolTransientError(ambiguous=True)` → status `Unknown`. Before any retry,
   `reconcile_shipment(dn)` calls `GET shipments?order-id=` and looks for a
   shipment whose `shipmentReference == dn.name`, or whose order items and
   quantities match. Found → `Confirmed`. Not found → POST again.

### Track & trace added after submit
`Delivery Note.on_update_after_submit`: if `bol_track_and_trace` changed and the
status is `Confirmed` → enqueue `update_transport(dn)`: GET the shipment →
`transportId` → PUT the transport → track with handler `transport`.

### Retry button
Whitelisted `retry_shipment(dn)` (needs Delivery Note write permission +
Bol Manager role). Allowed when the status is `Failed` or `Unknown`. It runs the
reconcile first, then confirms.

### Cancellation guard
`Delivery Note.before_cancel`: if the status is `Confirmed` or `Pending` → throw
("Shipment already confirmed on bol; process a return instead").

## Error handling
- A bol 400 (e.g. quantity exceeds open quantity) → `Failed` with the
  violations shown in the comment. The user fixes the cause and presses Retry.
- `BolAuthError` → `Failed` + log; the shipment is never assumed confirmed.
- Every state change goes through `db_set` on the submitted DN. Never re-save
  the whole document.

## Tests
- Mapper: aggregation over several DN lines for the same order item; transport
  vs label; reference.
- Validation: mixed orders, mixed distribution parties, missing T&T.
- Flow: submit → Pending → process SUCCESS → Confirmed with ID; FAILURE → Failed
  + comment; ambiguous POST → Unknown → reconcile finds it → Confirmed (no
  second POST); reconcile finds nothing → POST again; T&T update → transport
  PUT; cancel blocked; retry permission check.

## Acceptance criteria
- In the demo environment, submitting a DN marks the bol order item as shipped,
  with T&T, and the DN shows `Confirmed`.
- A failure is visible on the DN within minutes and can be retried with one click.
- No duplicate shipment is possible, including after timeouts.
