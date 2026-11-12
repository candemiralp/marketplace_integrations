# Cancellations

Design: [ADR-013](../decisions.md#adr-013--cancellations-story-10).

The Sales Order follows bol's cancellations, and you can cancel lines on bol from it.
It runs while **Import Orders** is on; there's no separate switch.

1. In Bol Settings (*Orders* section) set **Notify User**: the person who gets a ToDo when a buyer asks to cancel.
   Without one, the request is only a comment on the Sales Order.
2. **Cancelled at bol** (by bol, or by the buyer before you accepted the order). Every 5 minutes, with the order
   poll, the Sales Order is brought in line. A daily check covers bol Sales Orders of the last 30 days that aren't
   fully delivered and are no longer in bol's open list.
   - Part of a line → its qty is lowered (ERPNext's *Update Items*, so totals, reservations and billing stay right).
   - A whole line, while other lines remain → the line is removed.
   - The whole order with nothing delivered → the Sales Order is cancelled (a draft one is deleted; the integration
     log keeps a record).
   - *bol Qty Cancelled* on the line (section *bol.com*) shows what bol has cancelled; the timeline says what changed.
   - A line is never lowered below what's delivered: that's an error in the integration log and on the timeline.
     Check the order with bol.
3. **The buyer asks to cancel.** The line gets *bol Cancellation Requested*, the Sales Order gets a comment, and
   Notify User gets one ToDo per Sales Order. You decide:
   - not shipped yet → **bol → Cancel on bol** with reason `REQUESTED_BY_CUSTOMER`;
   - already shipped → do nothing: bol tells the buyer to return the item.
4. **Cancel on bol** (System Manager / Bol Manager with write permission on the Sales Order). Tick the lines and pick
   bol's reason. bol cancels each line **as a whole**, so lines with anything delivered can't be chosen. One request
   is sent per line. The Sales Order is adjusted once bol has processed it, usually within a minute (see [Async operations](02-processes.md)).
   If bol refuses (e.g. it cancelled the item already), the Sales Order is read again from bol and brought in line.
   If some lines went through and others failed, the dialog says which.
5. **Cancel** on a bol Sales Order with lines still open at bol is refused ("Use Cancel on bol"): cancelling in ERPNext
   alone wouldn't cancel anything at bol.
6. On the demo environment a cancellation stays pending: bol's demo never finishes its processes, and it answers every
   request with the same process id.
