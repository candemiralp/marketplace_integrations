# 04 — Catalog mapping, EAN validation, economic operator, adopt existing offers

**Goal:** ERPNext knows which Items are sold on bol, under which EAN, offer ID
and economic operator. Offers created earlier by the Shopify↔bol app are
**adopted, not duplicated**.
**Depends on:** 02

## bol API (verify)
- Economic Operators v1: search/list your economic operators and get one by ID
  (fields include `economicOperatorId`, `name`, `status` such as `VALID`).
  Creating operators is done **in the seller dashboard**, not in this story.
- Offers v11: `GET` offers list (filters `eans`, `reference`, `for-sale`,
  `page-size`, `cursor`) and `GET` a single offer.

## Data model
Custom fields (added to `setup/custom_fields.py`):

| DocType | Field | Type | Notes |
|---|---|---|---|
| Item | `bol_enabled` | Check | sell this item on bol |
| Item | `bol_min_days` / `bol_max_days` | Int | delivery promise (blank = Settings default) |
| Item | `bol_offer_status` | Data, read-only | e.g. `For sale`, `Not for sale`, `On hold` |
| Item | `bol_status_detail` | Small Text, read-only | not-for-sale reasons |
| Brand | `bol_economic_operator_id` | Data | picked via button |
| Ecommerce Item | `bol_last_stock_pushed` | Int, read-only | used in 06 |

New fields in `Bol Settings`: `default_min_days`, `default_max_days`.

Mapping record: `Ecommerce Item` with `integration="Bol"`,
`erpnext_item_code`, `integration_item_code = offerId`, `sku = EAN`
(field names as found in story 01).

## Behaviour

### EAN rules (`bol/mappers/ean.py`, pure)
- `is_valid_gtin13(code) -> bool`: 13 digits plus the GS1 check digit.
- `get_item_ean(item_doc) -> str`: exactly one `Item Barcode` row with
  `barcode_type == "EAN"` and a valid GTIN-13. Otherwise it raises
  `BolMappingError` with the reason.
- Item `validate` hook: if `bol_enabled`:
  - the item isn't a template (`has_variants == 0`), is a stock item, and has a valid EAN;
  - its Brand has `bol_economic_operator_id`.
  If any check fails, `frappe.throw` with a precise, translated message.

### Economic operator
- Whitelisted `list_economic_operators()` (Bol Manager) returns `id`, `name`,
  `status` from bol.
- Brand form button **Select bol economic operator** opens a dialog fed by the
  list above and saves the chosen ID. An operator whose status isn't `VALID` can
  be saved, but shows a warning, because offers linked to it won't be published.

### Adopt existing offers (`bol/offers.py::adopt_existing_offers`)
A whitelisted button on Bol Settings that enqueues a long job:
1. Page through all offers (v11 list, cursor-based).
2. For each offer, find the Item through its EAN in `Item Barcode`.
   - Match → upsert `Ecommerce Item` (offerId ↔ item), set `bol_enabled = 1`
     without running the full validation, and copy the status into
     `bol_offer_status`.
   - No match, or several Items with the same EAN → add to the report and don't
     create anything.
3. Result: a single `Ecommerce Integration Log` entry with counts and the list of
   unmatched or ambiguous EANs and offer IDs, plus a realtime message to the user
   who started it.
Running it again is safe (upsert keyed on offerId).

## Error handling
- Pagination errors: retry through the client; if retries run out, the job
  fails, the log shows how far it got, and a rerun is safe.
- Duplicate EAN across Items → never pick one automatically; report it.
- An offer ID already mapped to a different Item → report a conflict and leave
  it unchanged.

## Security
- Whitelisted methods: `frappe.only_for(["System Manager", "Bol Manager"])`.

## Tests
- GTIN-13: valid code, wrong check digit, wrong length, non-digits, leading zeros kept.
- Item validation: template rejected, missing EAN, two EANs, missing operator ID on Brand.
- Adopt: match, no match, duplicate EAN, conflict, idempotent rerun, multiple pages.

## Acceptance criteria
- Running **Adopt existing offers** on the live account maps every existing bol
  offer to an Item or lists it as an exception. It never creates offers.
- An Item can't be marked `bol_enabled` unless it's publishable.
