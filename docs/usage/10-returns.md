# Returns

Design: [ADR-014](../decisions.md#adr-014--returns-story-11).

Every return a buyer registers at bol becomes a draft return Delivery Note in ERPNext. Submit
it once the parcel is in and inspected, and bol gets the handling result.

1. In Bol Settings tick **Handle Returns** (*Returns* section). **Notify User** (*Orders* section) gets a ToDo for
   every return to inspect. Without one there's no ToDo; look for drafts with *bol RMA ID* set.
2. Every 30 minutes, each unhandled FBR return becomes a **draft return** against the Delivery Note that shipped it.
   It holds only the returned item, with bol's expected quantity. The *bol.com Return* section shows the RMA, the
   buyer's reason and the *bol Return Status* (`Open`).
   - If no submitted Delivery Note of that bol order can take the item back (e.g. it was shipped before the cutover),
     nothing is created: there's an error in the integration log and a ToDo with the return's details. Handle it by
     hand and in the bol seller dashboard.
3. **Parcel received:** set **bol Handling Result** (usually `RETURN_RECEIVED`), lower the qty if fewer came back, and
   submit. Submitting needs a handling result, and can't take back more than bol expects. bol is told in the
   background; *bol Return Status* becomes `Reported` once bol has processed it (see [Async operations](02-processes.md)).
4. **Nothing comes back** (e.g. `CUSTOMER_KEEPS_PRODUCT_PAID`): ERPNext can't submit a return of 0. On the draft use
   **bol → Report to bol without stock return**, pick the result, confirm. bol is told the expected quantity was
   handled (bol needs at least 1), and the draft is deleted. bol's answer appears on the original Delivery Note. If
   bol fails it, the return comes back as a new draft on the next poll.
5. `Failed`: bol refused the report; the timeline says why. Fix it (or handle it in the seller dashboard) and use
   **bol → Retry bol return report**. If bol had handled the return already, it's marked `Reported` instead of
   failing. bol unreachable: the return stays `Open` and is sent again automatically after about 10 minutes.
6. The credit note comes from [Accounting](11-accounting.md).
7. On the demo environment the one demo return belongs to an order that isn't imported, so it ends in the
   "no Delivery Note" log (and ToDo, with Notify User set).
