# 13 — Push notifications (webhooks) — optional

**Goal:** lower latency and fewer polling calls. bol pushes events to ERPNext,
and each event is treated **only as a hint** that triggers the same code the
pollers run. Polling stays in place as the safety net.
**Depends on:** 03, 05

## bol API — Subscriptions v10 (verify everything)
- Create, list, get, update and delete subscriptions:
  `{resources: [...], url, subscriptionType: "WEBHOOK"}`.
- Resource names to subscribe to (verify the enum): process status, shipment,
  and offer for-sale / not-for-sale events (the Offers v11 docs recommend
  `FOR_SALE` / `NOT_FOR_SALE`).
- Signature keys endpoint (public keys for push-notification signatures), and
  the header that carries the signature and key ID (verify the name and format).
- Test endpoint that sends a test notification for a subscription.

## Data model
New fields in `Bol Settings`: `use_webhooks` (Check), `subscription_id` (Data,
read-only), `webhook_last_received` (Datetime, read-only).

## Behaviour (`bol/webhooks.py`)

### Endpoint
```python
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60)
def receive(): ...
```
1. Reject immediately with 404 if `use_webhooks` is off; that way the endpoint
   reveals nothing when the feature is disabled.
2. Reject bodies over 64 KB.
3. **Verify the signature** over the raw body (`frappe.request.get_data()`) with
   the bol public key matching the key ID. Keys are cached for 24h; an unknown
   key ID triggers one refresh. An invalid or missing signature → 401 with an
   empty body, plus a log entry (kept low-volume with `log_once` per source IP).
4. Replay protection: if the payload has a timestamp, reject events older than
   10 minutes. Deduplicate by event ID (or a hash of the body) with a cache key
   that has a 1h TTL.
5. Parse the event → `frappe.enqueue(handle_event, event=..., queue="short")` →
   return 200 at once. **No DB writes or bol calls inside the request**, except the
   enqueue and `webhook_last_received` (written through the cache and flushed by a job).

### `handle_event(event)`
Only dispatches to existing code; there is no new business logic here.
- Process status event → `process_status.check_one(<Bol Process for that id>)`
  if a row exists; otherwise ignore it.
- Offer for-sale events → `offers.refresh_status(offer_id)`.
- Shipment events → nothing needed (we initiate shipments). Log at debug level.
- Unknown resource → debug log and ignore.

### Registration
Whitelisted `register_webhook()` (System Manager): url =
`frappe.utils.get_url("/api/method/marketplace_integrations.bol.webhooks.receive")`.
It must be `https`, otherwise throw. It creates or updates the subscription,
stores its ID, and calls the test endpoint.
Health: if `webhook_last_received` is older than 24h while bol activity
exists, the daily digest (15) warns about it.

## Security
- This is the **only** `allow_guest` endpoint in the app. Signature checks use
  the `cryptography` library, never hand-written crypto. Use constant-time
  comparisons where comparing digests.
- The payload is never trusted for state: handlers re-read from bol.
- Error responses never echo the input.

## Tests
- Valid signature → enqueued; invalid → 401; unknown key ID → one refresh then
  verify; missing header → 401; oversize → 413/400; replay/duplicate → ignored
  with 200; feature off → 404.
- `handle_event` dispatches correctly; unknown resource is ignored.
- Registration refuses an http URL.
Generate test keys with `cryptography` inside the test; don't commit private keys.

## Acceptance criteria
- With webhooks enabled, a demo shipment's process status is resolved within
  seconds instead of on the next poll.
- With webhooks disabled or failing, everything still works through polling.
