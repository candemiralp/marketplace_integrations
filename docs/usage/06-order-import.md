# Order import

Design: [ADR-010](../decisions.md#adr-010--order-import-story-07).

Every new FBR order at bol becomes one Sales Order.

1. Create a Customer for bol (e.g. *bol.com*) and two Sales Taxes and Charges Templates for the bol Company,
   one for NL buyers and one for BE buyers (OSS). Tick *Is this Tax included in Basic Rate?* on **every** row:
   bol's prices include VAT.
2. In Bol Settings (*Orders* section) set **Customer**, **Tax Template NL**, **Tax Template BE** and the
   *Warehouse* (*Stock* section; order lines are booked on it). At cutover, set **Import Orders Placed After** to
   the moment your previous connector stopped, so orders it already brought in aren't imported twice. Leave
   **Submit Orders** ticked, or untick it to review new orders as drafts first. Then tick **Import Orders**
   (off by default).
3. Every 5 minutes the open FBR orders at bol are read. Each order without a Sales Order is imported:
   - *Customer* is the bol customer; *Customer's Purchase Order* and *bol Order ID* are bol's order number. The
     shipping address (and a billing address if it's different) is a new Address with *bol Buyer* ticked,
     linked to that customer. The e-mail on it is bol's relay address: use it only for the shipping label.
   - Every bol order is booked on that one customer, so the buyer is on the order itself: *bol Buyer Name*,
     *Company*, *VAT Number* and *KvK Number* (tab *Address & Contact*, from bol's billing details). They are
     copied to the Delivery Note and Sales Invoice (section *Customer's Purchase Order*), for invoices to business
     buyers.
   - One line per open order item: the Item is found by its EAN barcode. The rate is what the buyer paid per unit
     (bol's price minus bol's discount, shown as *Discount Amount*). Each line has *bol Order Item ID*,
     *Distribution Party*, *Latest Delivery Date* and *Commission* (section *bol.com*). Units already shipped or
     cancelled at bol are left out; an order with nothing left to ship is skipped.
   - The tax template follows the shipping country (BE → BE, NL → NL). The grand total equals bol's total
     (rounded total is off).
   - *Delivery Date* is the earliest delivery date bol requires.
   - A comment on the Sales Order flags an offer reference that doesn't match the Item found by EAN (the EAN
     wins), or a discount that doesn't split into whole cents per unit (the total is then a cent or two off).
4. **Failures** (no Item with the EAN, several Items with it, a buyer outside NL/BE, a validation error) leave no
   Sales Order and no Address behind. They're in **Ecommerce Integration Log** with method `import_order`, the
   full bol order and the traceback, once per 6 hours per order. Fix the cause (e.g. add the barcode to the Item);
   the next poll imports the order. One failing order never holds up the others.
5. Polling again never creates a second Sales Order for the same bol order, even when two imports run at once.
