# Cutover and release checks

Design: [ADR-017](../decisions.md#adr-017--cutover-monitoring-hardening-story-15).

## Before every release: demo smoke test

With Bol Settings on **Environment = Demo** and the **demo** credentials (never production ones here):

```bash
bench --site <site> execute marketplace_integrations.bol.smoke.run
```

It prints `PASS` or `FAIL` for each step: token, offers list, orders list + detail, shipment + process status,
label delivery options, returns list, invoices list. Every step runs even when an earlier one fails. It refuses to
run on Production. It creates no ERPNext documents; like any write to bol, the demo shipment gets an integration
log entry. *Enabled* doesn't have to be on. It isn't part of `run-tests`.

## Cutover (T-0)

1. **T-1 day: backup and restore test.** `bench --site <site> backup --with-files`, restore it on a scratch site
   (`bench --site <scratch> restore …`), and check that it opens and has today's documents. Never restore over the
   live site.
2. Set Bol Settings to **Production** with the production client id and secret (create them in the seller dashboard
   for this integration only; the demo credentials stay on the test system). **Test Connection**.
3. Set **Import Orders Placed After** to the cutover moment. Orders placed before it came in through Shopify and are
   never imported.
4. Switch off the bol ↔ Shopify app's order sync in Shopify, then tick the toggles in the order of the docs
   ([Connect](01-connect.md) … [Accounting](11-accounting.md)). Each feature is off until its toggle is on.
5. Make sure at least one user has the **Bol Manager** role and an outgoing Email Account works: that's where the
   [daily digest](14-monitoring.md) goes.
6. **After go-live: rotate the secret.** Generate a new client secret in the seller dashboard, save it in Bol
   Settings, **Test Connection**, then revoke the old one. Anyone who saw the secret during the cutover no longer has a
   working one.

## Days 1–7: zero lost, zero duplicate orders

Every day for the first week:

```bash
bench --site <site> execute marketplace_integrations.bol.smoke.compare_orders --kwargs "{'days': 7}"
```

It reads (never writes) bol's FBR orders placed in the 7 days from *Import Orders Placed After*, and compares them
with the Sales Orders:

| Line | Meaning | Target |
|---|---|---|
| imported | bol orders with a Sales Order | all of them |
| cancelled before import | every unit was cancelled before the import saw it (nothing to ship) | informational |
| missing | no Sales Order although something was left to ship | **0**; check the integration log for that order id |
| duplicates | more than one Sales Order (not cancelled) for one bol order | **0** |
| extra | a Sales Order in the window that bol doesn't list | **0** |

The comparison doesn't see Sales Orders the old Shopify app may have created for the same order (they have no bol
order id): if *missing* is 0 but stock looks off, look for bol orders in Shopify after the cutover moment.

## Hardening checklist (checked for story 15)

- [x] **Scheduler jobs deduplicated and guarded.** Frappe's scheduler never runs a job while its previous run is
  queued or running (ADR-006), and every job returns at once while its toggle (or *Enabled*) is off.
- [x] **Rate limits.** bol's [active limits](https://api.bol.com/retailer/public/ratelimits) against our calls per
  cycle at peak volume (assumed: 100 open orders, 20 new orders and 30 stock changes in 5 minutes, 50 shipments
  submitted at once):

  | Job | Calls per cycle at peak | bol limit |
  |---|---|---|
  | Order poll (5 min) | 2–3 list pages + 1 detail per new order (≈ 20) | list 25/min, detail 25/s |
  | Process status (1 min) | 1 bulk POST (≤ 100 ids) | 2/s |
  | Stock poll (1 min) | 1 PATCH per changed item (≈ 30) | 50/s |
  | Shipments (on submit) | 1 GET list + 1 POST per Delivery Note | GET **25/min**, POST 25/s |
  | Shipment sweep (10 min) | ≤ 20 confirmations (was 50) | GET 25/min |
  | Returns poll (30 min) | 1–2 list pages + ≤ 20 report PUTs (was 50) | 20/min each |
  | Invoices, invoice requests, offer and stock reconcile (daily) | a few list pages + per new invoice 1–2 specification pages + 1 PDF | 9–25/min |

  The tight one is reading shipments: **submitting more than about 25 bol Delivery Notes within one minute** makes
  bol answer 429. The client waits as long as bol asks (`Retry-After`, at most 60 s, 3 times); a confirmation that
  still can't get through stays *Pending* and the sweep sends it within 10 minutes. Nothing is lost, but expect a
  delay and a logged rate-limit error. Spread big batches over a few minutes.
- [x] **Permissions.** Bol Manager can: read/write Bol Settings (no delete, share or e-mail), read Bol Process, read
  the integration log, run the bulk upload export, and see the Bol workspace. Every button and card checks the role
  **and** the document permission. Integration logs (buyer data) are readable only by System Manager and Bol Manager.
- [x] **Migrate twice** is idempotent: custom fields, role and log permission are applied with "create if missing".
- [x] **Uninstall** (`bench --site <site> uninstall-app marketplace_integrations`) removes the app's DocTypes,
  workspace, number cards, report and print format, and Frappe also deletes the `Bol` integration log entries and
  Ecommerce Item rows (they link to the module). The custom fields (bol ids on Sales Orders, Delivery Notes, Items, …)
  and their data stay: no hook or script points at the app any more, so nothing breaks. Take a backup first.
- [ ] Secrets rotated after go-live; demo and production credentials kept apart (step 6 above).
- [ ] Backup + restore test before T-0 (step 1 above).
