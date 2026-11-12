# New products on bol: export for the bulk upload

Design: [ADR-016](../decisions.md#adr-016--new-products-via-bols-excel-bulk-upload-not-the-product-content-api-replaces-story-14).

Products bol doesn't know yet (new EANs) are created
in the bol seller dashboard (*Items → Add items in bulk*, the option for items not yet on bol), from a file ERPNext
makes. There's no content upload through the API (story 14 was dropped).

1. Open the report **bol Bulk Upload Export** (System Manager or Bol Manager, with read access to Items). Filter on
   *Item Group*, *Brand* or one *Template* if you like. *Only items not on bol yet* (on by default) leaves out Items
   that already have a bol offer.
2. It lists every product that isn't a template: one row per variant, with **EAN**, **Title** (*Item Name*),
   **Description** (cleaned to the HTML bol accepts: `b br h3 li ol ul p strong`), **Brand**, **Internal Reference**
   (item code), **Family Key** (the template, so variants can be grouped), one column per variant attribute (*Size*,
   *Colour*, …) and **Item Group**. A variant without its own description or brand takes its template's. Images
   aren't in the file: add them in bol's tool. Items without a valid EAN barcode are left out and named above the table.
3. Click **Download for bol** (not the standard *Export*, which turns descriptions into flat text). The file has one
   tab and the column names in row 1, as bol requires.
4. **Translate** Title and Description into Dutch in the file: bol creates new items only from Dutch content. Then
   upload it in *Add items in bulk*, link EAN, title and description to bol's fields, check the suggested category per
   set of items and the attribute matches (skip *Item Group*, *Internal Reference* if bol offers no match), and fill
   what bol marks red.
5. Don't enter prices and stock there: once the item exists, tick **Sell on bol** on the Item and the [offer sync](04-offer-sync.md)
   creates the offer.
