# 05 — Offer sync (create, update, price, hold, delete)

**Goal:** an Item's bol offer always matches ERPNext: price, delivery promise,
economic operator and on-hold state. Stock is covered in story 06.
**Depends on:** 04

## bol API — Offers **v11** (verify every field)
- Create (POST) returns `offerId` **synchronously** (201).
- Get offer, list offers, **Update (PATCH)**, Delete (204).
- Get not-for-sale reasons.
- Key payload parts: `ean`, `reference` (≤100 chars, we send the item_code),
  `economicOperatorId`, `onHoldByRetailer`, `pricing.bundlePrices[]` (1–4 entries;
  `unitPrice` between 1 and 9999), `stock {amount, managedByRetailer}`,
  delivery schedule (`MY_DELIVERY_PROMISE` / `BOL_DELIVERY_PROMISE` + `deliveryPromise`).
- Validation and for-sale status are evaluated **after** creation (asynchronously
  on bol's side).

## Data model
New fields in `Bol Settings`: `sync_offers` (Check), `price_list` (Link Price
List, selling, required when `sync_offers`).

## Behaviour (`bol/offers.py`)

### Pure mapper `bol/mappers/offer.py`
`build_offer(item: dict, price: float, eo_id: str, min_days: int, max_days: int,
stock: int) -> dict`, plus `diff_offer(current: dict, desired: dict) -> dict`,
which returns only the changed top-level parts for the PATCH.
Pre-flight inside the mapper: the price must be within [1, 9999] and
`min_days <= max_days`. Anything else → `BolMappingError`.

### `sync_offer(item_code)` (background job, `job_id=f"bol_offer::{item_code}"`, dedupe)
1. Load Item, EAN, price (Item Price in `price_list`), the Brand's economic
   operator, and the delivery days (Item, falling back to Settings).
2. Determine the desired state:
   - `bol_enabled` and Item not disabled → `onHoldByRetailer = false`.
   - Otherwise → `onHoldByRetailer = true`. Prefer putting the offer on hold
     over deleting it, because it can be undone.
3. Mapping exists → GET the offer → `diff_offer` → PATCH if anything changed,
   otherwise do nothing.
   No mapping →
   - Not enabled → do nothing.
   - Enabled → **first** look up by EAN (`list offers?eans=`) to avoid creating a
     duplicate after an ambiguous earlier attempt. Found → adopt it. Not found →
     POST, then store the mapping.
   - POST gives `BolTransientError(ambiguous=True)` → don't retry now; the next
     run's EAN lookup settles it.
4. After writing, refresh the status: GET the offer; if it's not for sale, fetch
   the reasons. Write `bol_offer_status` and `bol_status_detail` with
   `db_set(update_modified=False)`.
5. GET returns 404 for a mapped offer (deleted on bol) → remove the mapping and,
   if enabled, recreate it in the same run.

### Triggers (`hooks.py` doc_events)
- `Item.on_update`: enqueue if any of `bol_enabled`, `disabled`, `bol_min_days`,
  `bol_max_days` changed, or `brand` changed (`doc.has_value_changed`).
- `Item Price.on_update` / `on_trash` where `price_list == settings.price_list`
  → enqueue for that item.
- `Brand.on_update` when `bol_economic_operator_id` changed → enqueue every
  bol-enabled item of that brand.
- All triggers use `enqueue_after_commit=True` and return early if
  `sync_offers` is off.

### Explicit delete
Whitelisted `delete_offer(item_code)` (Bol Manager): DELETE the offer, remove
the mapping, clear `bol_enabled`. A button on Item asks for confirmation first.

### Daily reconcile (scheduler, daily)
For every mapping: refresh `bol_offer_status` and the reasons. List offers with
`for-sale=false` first to keep the number of calls low.

## Error handling
- `BolMappingError` (no price, price out of range, no economic operator) → status
  `Not synced: <reason>` on the Item and `log_once(kind="offer", key=item_code)`.
- `BolValidationError` → put the violations into `bol_status_detail`, then log.
- Transient errors → the job fails; the reconcile or the next trigger retries.
- A missing Item Price must never create an offer with a price of 0.

## Security
- Only the explicit delete method is whitelisted; the rest is internal.

## Tests
- Mapper: payload shape; bundle price; out-of-range price; diff returns only
  changed parts; hold logic.
- Flow (client mocked): new item → lookup → create → mapping stored; existing
  offer adopted by EAN; unchanged → no PATCH; changed price → PATCH with the
  price only; disabled → hold; 404 → recreate; ambiguous POST → no retry and no
  duplicate on the next run; validation violations shown on the Item.

## Acceptance criteria
- Changing an Item Price in the bol price list updates the bol offer within a
  minute, with no duplicate offers.
- Disabling an item puts its offer on hold; enabling it puts it back on sale.
- Every Item shows its current bol status and any reasons it isn't for sale.
