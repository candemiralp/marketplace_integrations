# Monitoring and privacy

Design: [ADR-017](../decisions.md#adr-017--cutover-monitoring-hardening-story-15).

You don't have to watch the logs: every failure in the integration ends up in the workspace counts and in a
morning e-mail.

## Workspace "Bol"

Open **Bol** in the desk sidebar (System Manager and Bol Manager). Five counts; click one to open the matching list:

| Card | Counts |
|---|---|
| bol Errors Last 24 Hours | Integration log entries with status Error from the last 24 hours |
| bol Stuck Processes | bol processes still `PENDING` after an hour, or with `HANDLER_ERROR` ([Async operations](02-processes.md)) |
| bol Shipments to Check | Submitted Delivery Notes whose *bol Shipment Status* is `Failed` or `Unknown` ([Shipments](07-shipments.md)) |
| bol Offers Not For Sale | Items sold on bol whose offer isn't *For sale* (also *On hold* and items without an offer yet) |
| bol Open Returns | bol return Delivery Notes not yet *Reported* ([Returns](10-returns.md)) |

Shortcuts: Bol Settings, Bol Process, the integration log filtered on `integration = Bol`, and the bulk upload export.

## Daily digest e-mail

At **07:30** (in the system time zone, which must be *Europe/Amsterdam* in System Settings) every enabled user with the
**Bol Manager** role gets one e-mail, **only when there's something to check**:

- the five counts above, with links;
- **orders not imported for more than 24 hours** (the import keeps failing; the link opens the latest error);
- shipments to check, with their Delivery Note;
- offers not for sale, with bol's reasons.

The e-mail names documents and links to them. It never contains buyer names, addresses or e-mail addresses: open
the link to see those. No Bol Manager user? Then *Notify User* gets it. With neither, an error is logged, which
shows on the errors card. The integration has to be **Enabled**, and the site needs a working outgoing Email Account.

## Health on Bol Settings

The *Health* section shows when these jobs last finished their run: **Last Order Poll** (every 5 minutes),
**Last Stock Reconcile** (nightly) and **Last Invoice Import** (daily). A time far in the past means the job
isn't running (check the scheduler: `bench doctor`) or can't read bol's list (check the errors card). Single
records that fail don't hold these back; they're in the error count.

## Removing buyer addresses (off by default)

bol buyers' addresses are created by the order import (*bol Buyer* ticked). In the *Privacy* section,
**Anonymize Buyer Addresses After (Days)** (0 = off) makes a daily job do this for each such address once
**every** Sales Order, Delivery Note and Sales Invoice using it is older than that and finished (Sales Order
*Completed*/*Closed*/*Cancelled*, Delivery Note fully invoiced, nothing in draft):

- name, street, city, region become `[removed]`; e-mail, phone and fax are emptied; **country and postcode stay**;
- the address is renamed to `bol-buyer-#####` (its old name was the buyer's name), and the links on the documents
  follow; its change history and comments are deleted;
- the **printed address on Sales Invoices isn't touched**: it's part of the invoice, which must be kept for 7 years.

Before switching it on, agree the number of days with your accountant and check bol's partner terms. Pick a period
well beyond bol's return window: a return Delivery Note made later from an anonymized address prints `[removed]`.
At most 200 addresses are done per day. A failure is logged and retried the next day.
