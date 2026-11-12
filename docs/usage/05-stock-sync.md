# Stock sync

Design: [ADR-009](../decisions.md#adr-009--stock-sync-story-06).

Keeps the stock on each bol offer equal to what ERPNext can sell.

1. In Bol Settings (*Stock* section), pick the **Warehouse** bol orders ship from (not a group warehouse) and a
   **Stock Buffer** (default 1). Then tick **Sync Stock** (off by default; needs *Sync Offers*).
2. Per item, bol gets `actual qty − reserved for open Sales Orders − reserved stock − buffer` in that
   warehouse, rounded down, between 0 and 999. Incoming stock (purchase orders) doesn't count. An Item can set
   its own *bol Stock Buffer* (Sales tab, *bol.com*); 0 means the Settings value. Raise the buffer for items
   that sell fast on bol and on other sales channels: they all draw on the same warehouse stock.
3. Every minute the scheduler compares each bol item's available quantity with what bol last confirmed and
   pushes only the differences. Any stock movement (Delivery Note, Stock Entry, Purchase Receipt…), a new or
   cancelled Sales Order, or a buffer change reaches bol within about a minute. The scheduler and background
   workers must be running. Nothing is sent when nothing changed, so stock 0 is sent once.
4. bol keeps subtracting its own open orders (*corrected stock*) until our order import has booked them.
5. Failures (bol unreachable, rejected) are logged once per 6 hours and commented on the Item. The next minute
   tries again. If the offer is gone at bol, the offer sync recreates it with its stock.
6. **Nightly reconcile** (05:00) reads every offer at bol and corrects any stock that differs. Its result is an
   integration log entry with method `reconcile_stock`: `X checked, Y corrected, Z failed`. On a healthy system
   *corrected* is 0. A steady number of corrections means pushes are being missed; check the workers.
