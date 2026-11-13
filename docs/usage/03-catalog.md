# Catalog: which Items are sold on bol

Design: [ADR-007](../decisions.md#adr-007--catalog-mapping-economic-operators-adopting-offers-story-04).

1. **Economic operator per Brand.** Create the operators in the bol seller dashboard. On each Brand,
   click **Select bol economic operator** (System Manager / Bol Manager) and pick one. If bol hasn't
   marked the operator `VALID` yet, it's saved anyway, but offers linked to it won't be published.
2. **Delivery time.** Pick a *Default Delivery Time* in Bol Settings, the same choices as in the bol
   seller dashboard. With *Within 24 hours*, also pick *If Ordered Before* (12:00–23:00). An Item can
   override both in its *bol.com* section (Sales tab); empty means "use the default". See
   [ADR-018](../decisions.md#adr-018--delivery-times-bols-own-delivery-promise-fix-2026-11-13).
3. **Mark Items.** Tick **Sell on bol** on the Item. It saves only when the Item is a stock item (not a
   template), has exactly one barcode of type *EAN* with a valid 13-digit GTIN, and its Brand has an
   economic operator. The message lists everything that's missing. While the box is ticked, these
   checks run on every save.
4. *bol Offer Status* (`For sale`, `Not for sale`, `On hold`) and *bol Status Detail* are read-only.
   bol writes them.

## Adopt existing offers

One-off, at cutover. Offers that already exist at bol (created in the seller
dashboard or by a previous connector) are linked to their Items, never created again.

1. Enable the integration, save, and click **Adopt existing offers** in Bol Settings. It runs in the
   background and only reads from bol.
2. Each offer is matched to an Item through its EAN in the Item's barcodes. UPC-A (12 digits) and
   GTIN-14 forms of the same number match too. A match is stored as an *Ecommerce Item* (integration
   *Bol*, *Integration Item Code* = offer ID, *SKU* = EAN). The Item gets *Sell on bol* ticked and
   the offer's status copied, without the checks from *Mark Items* above: fix what's missing later.
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
