# Verzenden via bol labels

Design: [ADR-012](../decisions.md#adr-012--verzenden-via-bol-labels-story-09).

Items bol ships itself (*bol Distribution Party* `BOL` on the Sales Order line)
must go with a bol label; the shipment is then confirmed with that label. bol charges a label only when the shipment
using it is confirmed.

1. Register for Verzenden via bol in the bol seller dashboard (*Instellingen → Diensten*); the API can't do it. In Bol
   Settings (*Verzenden via bol* section) tick **Use Verzenden via bol** (off by default; needs *Push Shipments*).
   Optional: **Preferred Label Transporter** (bol's code, e.g. `TNT`) is picked when bol recommends no label; otherwise
   the cheapest label is bought. **bol Labels for Own Shipments** also offers bol labels for items you ship yourself.
2. Make and **save** the Delivery Note, then click **bol → Get bol label** (System Manager / Bol Manager with write
   permission on the Delivery Note). bol's recommended (or preferred, or cheapest) label is bought; the timeline shows
   which and its price excl. VAT. *bol Label Status* becomes `Requested`.
3. Within a minute or two the label is `Ready`: `bol-label-<Delivery Note>.pdf` is attached (private: only people who
   may read the Delivery Note can open it) and you get a message. Print it, then submit the Delivery Note; no track &
   trace or transporter is needed.

   | Status | Meaning |
   |---|---|
   | `Requested` | bol is making the label. With a *bol Shipping Label ID* it's made but couldn't be downloaded: click **bol → Download bol label again** (it never buys a second label). |
   | `Ready` | The PDF is attached. The Delivery Note can be submitted. |
   | `Failed` | bol couldn't make it; the timeline says why. Fix the cause and click **Get bol label** again. |

4. A Delivery Note for items bol ships can't be submitted without a `Ready` label, and none while a label is
   `Requested`. Once a label is bought the items and quantities can't change: delete the draft and make a new one (an
   unused label isn't charged).
5. Messages when buying fails: *not registered for Verzenden via bol* (step 1); *No VVB option available* (the package
   doesn't fit bol's restrictions, e.g. too heavy); bol's own reasons for a rejected request. If the connection broke
   while buying, just try again.
6. On the demo environment a label stays `Requested`: bol's demo never finishes its processes.
