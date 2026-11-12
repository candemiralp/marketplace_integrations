# Offer sync

Design: [ADR-008](../decisions.md#adr-008--offer-sync-story-05).

Keeps each Item's bol offer in line with ERPNext: price, delivery promise,
economic operator and on-hold state. Stock follows in the stock sync.

1. In Bol Settings, pick a **Price List**: selling, in EUR, with prices **including VAT** (bol shows them
   as is). Set *Default Max Days to Customer*. Then tick **Sync Offers** (off by default).
2. Changes are pushed within a minute: saving an Item (*Sell on bol*, *Disabled*, delivery days, Brand), an
   Item Price in that price list, or a Brand's economic operator. Background workers must be running.
   - Ticked and enabled → the offer is created (or, if bol already has one for the EAN, linked) and put
     on sale. A new offer starts with stock 0 until the stock sync sets it.
   - Unticked or disabled → the offer is put **on hold**, never deleted. Re-enabling puts it back on sale.
3. *bol Offer Status* on the Item:

   | Status | Meaning |
   |---|---|
   | `For sale` | Live on bol. |
   | `Not for sale` | bol doesn't sell it; *bol Status Detail* lists bol's reasons per country. |
   | `On hold` | Taken off sale by us (item unticked or disabled). |
   | `Not synced` | We couldn't update bol: *bol Status Detail* says why (no Item Price, price outside 1–9999, no economic operator, bol rejected the data). A live offer keeps its last values. Fix the data and save the Item. |
   | `Deleted` | Deleted with the button below, or gone at bol. |

   Failures are logged once per 6 hours in the integration log and commented on the Item.
4. **Delete bol offer** (Item form, *bol* menu, System Manager / Bol Manager) deletes the offer at bol after
   a confirmation and unticks *Sell on bol*. Use it only when the item will never be sold on bol again.
5. **Daily reconcile** refreshes every Item's status and reasons from bol, and re-syncs any offer that differs
   from ERPNext, has vanished at bol, or was never created. **After adopting existing offers**, check the
   prices in the bol price list before switching on *Sync Offers*: the first reconcile brings every adopted
   offer in line with ERPNext (price, delivery promise, reference = item code).

The bol demo environment doesn't accept offer writes (PATCH gives HTTP 403), so on Demo the sync can only
be checked up to reading offers.
