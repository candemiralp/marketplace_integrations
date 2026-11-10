# 09 — Verzenden via bol (VVB) shipping labels

**Goal:** for order items that bol routes itself (`distributionParty = BOL`),
buy the bol label from the Delivery Note, attach the PDF, and confirm the
shipment with `shippingLabelId`. This is mandatory for those items; you can't
use your own PostNL label.
**Depends on:** 03, 08

## Preconditions
- The bol account is registered for VVB in the seller dashboard
  (Instellingen → Diensten). This can't be done through the API.
- A label is **charged when the shipment is confirmed**, not when it's created.

## bol API — Shipping Labels v10 (verify)
- `POST /retailer/shipping-labels/delivery-options` `{orderItems: [{orderItemId}]}`
  → `deliveryOptions[] {shippingLabelOfferId, validUntilDate, transporterCode,
  labelType, labelPrice{totalPrice}, packageRestrictions, handoverDetails,
  recommended?}`.
- `POST /retailer/shipping-labels` `{orderItems: [{orderItemId, quantity}],
  shippingLabelOfferId}` → 202 + process status (`entityId` = shippingLabelId).
- `GET /retailer/shipping-labels/{id}` with `Accept: application/vnd.retailer.v10+pdf` → PDF bytes.

## Data model
Delivery Note custom fields: `bol_shipping_label_id` (Data, read-only, no_copy),
`bol_label_status` (Select `\nRequested\nReady\nFailed`, read-only, no_copy),
`bol_label_transporter` (Data, read-only).
New fields in `Bol Settings`: `use_vvb` (Check), `vvb_preferred_transporter`
(Data, optional), `vvb_for_retailer_items` (Check, default 0: also buy bol labels
for RETAILER items when they're cheaper than your own contract).

## Behaviour (`bol/labels.py`)

### Button **Get bol label** (draft DN, bol order, `use_vvb`)
Whitelisted `request_label(dn_name)`. Needs DN write permission. The DN must be
saved and must not have a label yet (or its label must be `Failed`).
1. Delivery options for the DN's order items.
2. Pure chooser `bol/mappers/label.py::choose_option(options, preferred)`: a
   `recommended` option if present → else one with the preferred transporter →
   else the cheapest `totalPrice`. Expired `validUntilDate` options are excluded.
   No options left → `BolMappingError` ("No VVB option available — check the
   package restrictions").
3. POST the label → `track(resp, "shipping_label", dn)` → status `Requested`.
4. `on_success`: GET the PDF → attach it as a **private** file to the DN
   (`file_name=f"bol-label-{dn}.pdf"`) → set `bol_shipping_label_id`, status
   `Ready`, transporter → realtime message to the user.
   `on_failure`: `Failed` + comment.

### Integration with story 08
- `before_submit`: if any line is `BOL`, require `bol_label_status == "Ready"`.
- `confirm_shipment` sends `shippingLabelId` instead of `transport` (already
  supported by the mapper in 08).

### Label for draft DN cancelled or deleted
No API action is needed, because unconfirmed labels aren't billed. Clear the
fields if the DN is amended (`no_copy` takes care of that).

## Error handling
- 403 from delivery-options → translated message: "bol account not registered
  for Verzenden via bol".
- PDF download fails after SUCCESS → status stays `Requested`. Add a
  **Download label again** button that only does the GET. The label isn't
  bought a second time.
- Never buy a second label while one is `Requested` or `Ready` for the same DN.

## Security
- The PDF contains a buyer address, so it's stored as a private file and only
  people with DN permissions can read it.

## Tests
- Chooser: recommended wins; preferred transporter; cheapest; expired options
  excluded; empty → error.
- Flow: request → Requested → SUCCESS → PDF attached privately → Ready; failure
  → Failed; double click → no second label; submit blocked without a Ready
  label for BOL lines; the shipment payload uses `shippingLabelId`; 403 message.

## Acceptance criteria
- In the demo environment, a BOL-distributed order item gets a printable label
  PDF on its DN, and the shipment confirms with that label.
