# For developers

Design: [ADR-004](../decisions.md#adr-004--bol-auth-base-paths-and-error-format-checked-against-the-spec-story-02), [ADR-005](../decisions.md#adr-005--logging-adapter-deferred-writes-callers-log-failures-story-02).

## Calling bol

```python
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError
from marketplace_integrations.bol.log import log_call, log_once

client = get_client()  # raises BolError when the integration is disabled
try:
    orders = client.request("GET", "/retailer/orders", version=10, params={"status": "OPEN"})
except BolError as err:
    frappe.db.rollback(save_point="record")  # undo your own work first
    log_once("order", order_id, **err.details, error=err, reference=("Sales Order", so_name))
```

- Pass the path exactly as bol's spec writes it; the client handles the demo prefix, retries
  (§4.1), token refresh and error mapping.
- `err.ambiguous` is `True` when a POST may have been processed. Check bol's state before trying
  again.
- Logs are written when the transaction commits or rolls back, never in the middle of it.

## Tracking an async bol call

See [ADR-006](../decisions.md#adr-006--process-status-tracker-story-03).

```python
from marketplace_integrations.bol import process_status

# at import time in your feature module (and add the module to process_status.HANDLER_MODULES)
process_status.register("shipment", on_shipment_confirmed, on_shipment_failed)

response = client.request("POST", "/retailer/shipments", version=10, json=body)  # 202
process_status.track(response, "shipment", delivery_note, payload={"order_item_ids": ids})
```

`on_success(doc, entity_id, payload)` and `on_failure(doc, message, payload)` must be idempotent: they
run again on **Retry handler**. They run inside a savepoint; if they raise, their work is rolled back.
