# 15 — Cutover, monitoring, hardening

**Goal:** switch bol.com from the Shopify↔bol app to the direct ERPNext
integration with no lost or duplicated orders. Make sure failures get noticed
without anyone having to watch the logs.
**Depends on:** all required stories

## A. Demo smoke test (`bol/smoke.py`)
`bench --site X execute marketplace_integrations.bol.smoke.run` against
`environment = Demo`. It exercises: token, offers list, orders list + detail,
shipment POST → process status, label delivery options, returns list, invoices
list. It prints PASS/FAIL per step and doesn't write to ERPNext. Run it before
every release. It's not part of the unit test suite.

## B. Monitoring
- **Workspace "Bol"** (JSON in the module): shortcuts to Bol Settings, Bol
  Process, the logs filtered to `integration = Bol`. Number cards:
  - integration log errors in the last 24h;
  - `Bol Process` rows with status `PENDING` older than 1h, or `HANDLER_ERROR`;
  - Delivery Notes with `bol_shipment_status` in (`Failed`, `Unknown`);
  - Items with `bol_offer_status` not equal to `For sale`;
  - open return DNs with `bol_rma_id`.
- **Daily digest email** (scheduler, 07:30 Amsterdam) to Bol Manager users, sent
  only if there's something to report: the counts above plus the lists of
  stuck orders (import failing for more than 24h), failed shipments, offers not
  for sale with their reasons, and webhook silence (13). Plain, short email with
  links. No PII in the email body; link to the documents instead.
- Health indicator on Bol Settings: last successful order poll, stock reconcile
  and invoice import (read-only datetimes written by those jobs).

## C. PII retention (configurable, **default off**)
- Settings: `anonymize_buyer_addresses_after_days` (Int, 0 = off).
- Daily job: Addresses with `bol_buyer = 1` linked only to documents older than
  N days → replace the personal fields with `"[removed]"`, keeping country and postcode.
- Dutch fiscal rules require keeping invoices for 7 years, and the printed address
  on a submitted SI (`address_display`) is part of that record and isn't touched.
  Confirm N and the scope with your accountant and check bol's partner terms
  before turning this on. Record the decision in `docs/decisions.md`.

## D. Cutover runbook (write it into `docs/cutover.md`)
1. **T-7d:** run story 04 **Adopt existing offers** on production with
   `sync_offers` / `sync_stock` off. Resolve every unmatched or ambiguous EAN.
   Link economic operators. Set the price list so bol prices equal the current bol prices.
2. **T-1d:** turn on `sync_offers` + `sync_stock`, but first disable stock and
   price pushes in the Shopify↔bol app. Watch the nightly reconcile: the target is
   0 corrections.
3. **T-0 (quiet hour):**
   a. In the Shopify↔bol app: turn off order import into Shopify.
   b. Set `import_orders_placed_after = now` and turn on `import_orders`,
      `push_shipments`, `use_vvb`, `handle_returns`, `auto_invoice`.
   c. Orders already in Shopify/ERPNext from the old flow are finished in the
      old flow: ship them from Shopify as before.
   d. Watch the first poll; place a real low-value test order if possible.
4. **T+7d:** if nothing is open in the old flow, uninstall the Shopify↔bol app
   and the Shopify PostNL app for bol orders. Turn on `import_bol_invoices`.
5. **Rollback:** turn off the toggles from 3b, turn the Shopify↔bol app's order
   import back on. Orders imported in the meantime stay in ERPNext and are shipped from there.

## E. Hardening checklist
- [ ] Every scheduler job deduplicated (`job_id`) and guarded by toggles
- [ ] Rate-limit sanity: count the calls per poll cycle at peak volume; stay well below bol's limits
- [ ] Permissions review: Bol Manager can't read anything beyond what it needs; integration logs are restricted
- [ ] `bench migrate` twice is idempotent; uninstalling the app leaves no broken hooks
- [ ] Secrets rotated after go-live; demo and production credentials kept separate
- [ ] Backup + restore test of the site before T-0

## Tests
- Digest builder: empty → no email; content without PII.
- Retention job: only old, bol-only addresses; submitted invoices untouched; off by default.

## Acceptance criteria
- Cutover completes with zero duplicate and zero lost orders (checked by comparing
  bol's order list with ERPNext SOs for the first 7 days; a script for this is part of `smoke.py`).
- Any failure in the integration reaches a human by email within 24h.
