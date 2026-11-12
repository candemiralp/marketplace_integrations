# Shipments

Design: [ADR-011](../decisions.md#adr-011--shipment-confirmation-story-08).

Submitting a Delivery Note for a bol order confirms the shipment at bol, with track & trace.

1. In Bol Settings (*Shipments* section) pick the **Default Transporter** (bol's code; PostNL is `TNT`) and leave
   **Require Track & Trace** ticked unless you send items without a code (e.g. letter post). Then tick **Push Shipments**
   (off by default). Workers and the scheduler must be running.
2. Make the Delivery Note from the bol Sales Order (*Create → Delivery Note*). One Delivery Note holds the items of **one**
   bol order only, all shipped by you (or all by bol: see [Verzenden via bol labels](08-vvb-labels.md)). Part of an order can be shipped: bol gets the quantities on
   the Delivery Note. Fill in **bol Track & Trace** (section *bol.com Shipment*, next to *Transporter Info*; *bol
   Transporter* is filled from the settings) and submit.
3. *bol Shipment Status* on the Delivery Note:

   | Status | Meaning |
   |---|---|
   | `Pending` | Sent to bol, which is processing it (usually a minute), or bol couldn't be reached and it's tried again automatically. |
   | `Confirmed` | bol has the shipment; *bol Shipment ID* is bol's id. The order items are shipped at bol. |
   | `Failed` | bol rejected it (e.g. more units than are still open), or the credentials failed. The timeline says why. Fix the cause and click **bol → Retry bol shipment** (System Manager / Bol Manager with write permission on the Delivery Note). |
   | `Unknown` | The connection broke while sending: bol may or may not have it. Within about 10 minutes bol is checked and the shipment confirmed or sent again; **Retry** does the same at once. |

   Before anything is sent, bol's shipments for the order are checked, so a shipment is never sent twice, also after a
   timeout or a retry. Failures are in the integration log (once per 6 hours for repeats) and on the timeline.
4. **Track & trace later.** Fill in *bol Track & Trace* on the submitted Delivery Note and *Update*: it's added to the
   shipment at bol. bol only accepts a code while the shipment has none; to change one bol already has, use the bol
   seller dashboard (the timeline says so).
5. A Delivery Note whose shipment is `Pending`, `Confirmed` or `Unknown` can't be cancelled: process a return instead
   ([Returns](10-returns.md)). A `Failed` one can.
6. On the demo environment a shipment stays `Pending`: bol's demo never finishes its processes.
