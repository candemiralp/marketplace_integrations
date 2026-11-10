# 06 — Stock sync

**Goal:** the stock on each bol offer reflects ERPNext's available-to-sell (ATS)
quantity within a minute, without overselling and without useless API calls.
**Depends on:** 05

## bol API — Offers v11 (verify)
- PATCH the offer's `stock {amount, managedByRetailer}`.
- `correctedStock` is read-only (bol subtracts open orders) and can lag behind.
- bol caps the stock amount at a maximum; **check the limit in the spec** and
  clamp to it.

## Data model
New fields in `Bol Settings`: `sync_stock` (Check), `warehouse` (Link
Warehouse, required when `sync_stock`), `stock_buffer` (Int, default 1).
Optional Item field `bol_stock_buffer` (Int) overrides the buffer for one item.

## Behaviour (`bol/stock.py`)

### ATS (pure: `bol/mappers/stock.py::available_to_sell`)
```
ats = floor(actual_qty - reserved_qty - reserved_stock - buffer)
ats = clamp(ats, 0, BOL_MAX_STOCK)
```
`actual_qty`, `reserved_qty` and `reserved_stock` (the v15+ stock reservation
field, if it exists) come from `Bin` for `(item_code, settings.warehouse)`.
Don't count `projected_qty`; incoming purchase orders don't make stock sellable.

### `managedByRetailer`
Send `false`, so bol also subtracts its own open orders. This covers the gap
until the order poll (story 07) creates the Sales Order. Record the decision in
`docs/decisions.md`.

### `push_stock(item_code)` (job, `job_id=f"bol_stock::{item_code}"`, dedupe)
1. Return early unless `sync_stock` is on, the item is `bol_enabled`, and a
   mapping exists. If there's no mapping, `sync_offer` (05) creates the offer
   including its stock.
2. Compute ATS. If it equals `Ecommerce Item.bol_last_stock_pushed`, stop (no call).
3. PATCH the stock → on success store `bol_last_stock_pushed = ats`.
4. 404 → hand off to `sync_offer(item_code)` to recreate the offer.

### Triggers
- `Bin.on_update` (doc_events) where `warehouse == settings.warehouse` and the
  item is bol-enabled → enqueue `push_stock` with `enqueue_after_commit=True`.
  Deduplication merges bursts, e.g. many SO submits in one minute.
- Changing `stock_buffer`, or `bol_stock_buffer` on an Item → enqueue the affected items.

### Nightly reconcile (daily, early morning)
For every mapping: GET the offer, compare `stock.amount` with ATS, PATCH any
difference, and update `bol_last_stock_pushed`. This self-heals pushes that
were missed. Log a summary line: checked / corrected / failed.

## Interaction with Shopify
`ecommerce_integrations` keeps pushing stock to Shopify. Both channels draw on
the same Bin. The buffer and bol's `correctedStock` protect against selling the
same last unit twice. The cutover story documents that both channels use the
same warehouse.

## Error handling
- Transient errors / rate limit → the job fails, `bol_last_stock_pushed` stays
  unchanged, so the next trigger or the reconcile retries.
- Never write `bol_last_stock_pushed` unless bol confirmed the update.
- Multiple Bin updates during an outage are folded into one push with the latest
  value, because ATS is computed when the job runs, not when it's enqueued.

## Tests
- ATS: buffers, negative → 0, clamp to max, missing Bin → 0, item override buffer.
- Push: unchanged → no call; changed → PATCH + stored; failure → not stored;
  404 → offer resync triggered.
- Trigger: other warehouse ignored; non-bol item ignored; toggle off ignored.
- Reconcile corrects a difference and skips matches.

## Acceptance criteria
- Submitting a Delivery Note or Stock Entry updates the bol stock within a minute.
- Zero stock is pushed once, not on every Bin update.
- The nightly reconcile reports 0 corrections on a healthy system.
