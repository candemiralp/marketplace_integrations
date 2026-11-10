# 14 — Product content upload — optional

**Goal:** publish catalog content (title, description, attributes, images, GPSR
data) from ERPNext for own-brand EANs that bol doesn't know yet.
**Depends on:** 03, 04

> Recommendation: for a catalog of a few dozen SKUs, create the content once in
> the seller dashboard (bulk upload) and skip this story until the assortment
> grows. Offers, stock and orders don't depend on it.

## bol API — Product Content v10 (verify)
- `GET /retailer/content/catalog-products/{ean}` → does the product exist in bol's catalog?
- Chunk recommendations → suggests the product classification (GPC "chunk" ID)
  from a title and description.
- `POST /retailer/content/products` `{language, attributes: [{id, values: [{value, unitId?}]}],
  assets: [{url, labels: []}]}` → 202 + process status (upload ID).
- `GET /retailer/content/upload-report/{upload-id}`: request it **no earlier than
  1 hour** after the upload. Element statuses are `PUBLISHED` / `DECLINED` with a
  sub-status. Reports expire after 28 days.
- bol's data model (attributes per chunk, enrichment level 0/1 = mandatory,
  asset labels) is downloaded from bol's docs. Store a pinned copy and refresh
  it manually.
- Language: `nl`, `nl-BE`, `fr`, `fr-BE`. Attribute IDs are English, values are
  in the chosen language.

## Data model — DocType `Bol Product Content` (one per Item, autoname = item)
| Field | Type |
|---|---|
| `item` | Link Item (unique) |
| `language` | Select `nl\nnl-BE\nfr\nfr-BE` |
| `gpc_chunk_id` | Data |
| `attributes` | Table `Bol Content Attribute` (`attribute_id`, `value`, `unit_id`, `status` read-only, `status_detail` read-only) |
| `assets` | Table `Bol Content Asset` (`url`, `labels` comma-separated, `status` read-only) |
| `upload_id` | Data, read-only |
| `content_status` | Select `\nDraft\nUploading\nAwaiting report\nPublished\nPartially declined\nFailed` |
| `report_due_at` | Datetime, read-only |

Title, description, manufacturer and the GPSR safety fields are ordinary
attributes, using the IDs from the data model.

## Behaviour (`bol/content.py`)
- Button **Suggest category** → chunk recommendations → the user picks a chunk ID.
- `validate()`: every mandatory attribute (level 0/1) for the chunk exists in the
  pinned data model; asset URLs are **public https** (bol fetches them, so
  private ERPNext files won't work; use public files or Shopify CDN URLs); labels
  are valid for the chunk; the description passes through an allow-list HTML
  sanitizer (only the tags bol accepts).
- Button **Upload to bol** (Bol Manager): build the payload with a pure mapper
  (EAN taken from the Item, as in story 04) → POST → `track(…, "content", doc)`
  → `Uploading`. `on_success` → store the upload ID, set
  `report_due_at = now + 1h`, status `Awaiting report`.
- Hourly job: for docs whose `report_due_at` has passed → GET the report → if it's
  `IN_PROGRESS`, push `report_due_at` forward by 1h (give up after 48h → `Failed`)
  → otherwise write each element's status back to the attribute and asset rows
  → `Published` if every element is published, otherwise `Partially declined`
  with the details.

## Error handling
- Declined elements are a normal outcome: show them per row, and don't treat
  them as a system error. A repeat upload sends the full content again.
- A report that has expired (more than 28 days) → `Failed` with "upload again".
- Data model missing or stale → validation error that names the file to refresh.

## Tests
- Mapper payload; mandatory-attribute validation; non-public or http asset URL
  rejected; sanitizer strips disallowed tags; report parsing (all published,
  partial, still in progress, expired).

## Acceptance criteria
- A new EAN uploaded from ERPNext appears in bol's catalog. Declined attributes
  are visible per row with the reason.
