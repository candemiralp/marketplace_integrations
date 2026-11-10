# 03 — Async process-status tracker

**Goal:** a single mechanism that follows every async bol operation (HTTP 202 +
`processStatusId`) to a final state and then runs a handler defined in code.
**Depends on:** 02

Used by: shipments (08), labels (09), cancellations (10), return handling (11),
transports (08) and content (14). Offers v11 are synchronous and don't use it.

## bol API (verify)
- `GET /retailer/process-status/{process-status-id}` (v10; the old `/shared/…`
  path is gone).
  Response: `processStatusId`, `entityId`, `eventType`, `description`,
  `status` ∈ `PENDING | SUCCESS | FAILURE | TIMEOUT`, `errorMessage`,
  `createTimestamp`.
- The bulk variant `POST /retailer/process-status` (by IDs): use it if the spec
  confirms it, to poll up to 1000 at once.
- **`processStatusId` is an opaque string.** bol changed its format in 2025, so
  never cast it to int.

## Data model — DocType `Bol Process`
| Field | Type | Notes |
|---|---|---|
| `process_status_id` | Data | **unique**, read-only |
| `handler` | Select | keys from the in-code registry, e.g. `shipment`, `transport`, `shipping_label`, `cancellation`, `return_handling`, `content` |
| `reference_doctype` | Link DocType | |
| `reference_name` | Dynamic Link | |
| `payload` | JSON | context the handler needs (e.g. order item IDs) |
| `status` | Select `PENDING\nSUCCESS\nFAILURE\nTIMEOUT\nHANDLER_ERROR` | |
| `entity_id` | Data | from bol on success |
| `error_message` | Small Text | |
| `attempts` | Int | |
| `next_check_at` | Datetime | indexed |

Read-only in the UI. Permissions: Bol Manager, System Manager (read). Add a
**Retry handler** button for the `HANDLER_ERROR` state.

## Behaviour (`bol/process_status.py`)
```python
HANDLERS: dict[str, tuple[Callable, Callable]] = {}   # name -> (on_success, on_failure)
def register(name, on_success, on_failure): ...
def track(response: dict, handler: str, reference_doc, payload: dict | None = None) -> str
def poll_pending() -> None                             # scheduler, every minute
def check_one(name: str) -> None
```
- `track()` inserts a `Bol Process` with status `PENDING` and
  `next_check_at = now + 10s`. It's idempotent on `process_status_id`.
- `poll_pending()`: settings enabled → select `PENDING` rows with
  `next_check_at <= now` (limit 100, oldest first) → fetch the status (bulk if
  available) → for each row:
  - `PENDING`: `attempts += 1`; backoff `next_check_at`: 10s, 30s, 1m, 2m, 5m,
    then every 10m. If more than 24h have passed since creation, mark `TIMEOUT`
    and call `on_failure`.
  - `SUCCESS`: set `entity_id`, call `on_success(doc, entity_id, payload)`.
  - `FAILURE` / `TIMEOUT`: set `error_message`, call `on_failure(doc, message, payload)`.
  - If a handler raises: roll back to the savepoint, set `HANDLER_ERROR`, log, and
    leave a comment on the reference document. bol's state is final, so never
    poll again. The fix is a manual **Retry handler** after the bug is fixed.
- The registry is filled at import time in each feature module, e.g.
  `register("shipment", on_shipment_confirmed, on_shipment_failed)`. Unknown
  handler names raise an error at `track()`. **Never** import a handler from a
  string stored in the database.
- `hooks.py`: `scheduler_events.cron["* * * * *"]` → `poll_pending`, enqueued
  with `job_id="bol_poll_process_status"` and deduplication so runs never overlap.
- A row-level failure (e.g. `BolTransientError` while fetching) leaves the row
  `PENDING` with backoff; one row's failure must not stop the others.
- Housekeeping: delete final-state rows older than 90 days (daily job).

## Error handling
- Every final failure writes to the integration log and adds a comment on the
  reference document (e.g. "bol shipment failed: <errorMessage>").
- Handlers must be idempotent: they may run again after **Retry handler**.

## Security
- Only System Manager / Bol Manager can use Retry handler (permission check in
  the whitelisted method).

## Tests
- Unit: backoff schedule; timeout after 24h; handler registry rejects unknown names.
- Integration (client mocked): PENDING → SUCCESS calls `on_success` once with
  `entity_id`; FAILURE calls `on_failure`; a handler exception → `HANDLER_ERROR`,
  a comment on the reference document, other rows still processed; duplicate
  `track()` → one row; a transient fetch error → row stays PENDING with
  `attempts` incremented.

## Acceptance criteria
- An async request created by any later story ends in a final state with its
  handler run or a visible error. Nothing stays PENDING forever.
