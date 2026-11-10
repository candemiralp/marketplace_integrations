# 02 — Settings, authentication, HTTP client, logging

**Goal:** one secure, resilient way to call bol, used by every later story.
**Depends on:** 01

## bol API (verify)
- Token: `POST https://login.bol.com/token?grant_type=client_credentials`,
  header `Authorization: Basic base64(client_id:client_secret)`,
  `Accept: application/json`. Response has `access_token` and `expires_in` (seconds).
- Base URLs: production `https://api.bol.com/retailer`, demo
  `https://api.bol.com/retailer-demo` (**check the demo path in the docs**).
- Errors come back as `application/vnd.problem+json`: `type`, `title`, `status`,
  `detail`, `instance`, `violations[{name, reason}]`.
- Rate limiting: 429 with a `Retry-After` header.

## Data model — Single DocType `Bol Settings`
| Field | Type | Notes |
|---|---|---|
| `enabled` | Check | master switch |
| `environment` | Select `Demo\nProduction` | default Demo |
| `client_id` | Data | required if enabled |
| `client_secret` | Password | required if enabled |
| `company` | Link Company | required if enabled |
| `debug_logging` | Check | also log successful calls |

Later stories add their own fields in their own sections. Permissions: System
Manager and Bol Manager (read/write). Add a **Test connection** button.

## Behaviour

### `bol/api/auth.py`
- `get_token(settings) -> str`. Reads from the cache key
  `bol:token:{environment}:{sha256(client_id)[:12]}`. On a cache miss it fetches
  a token and stores it with TTL `expires_in - 30`.
- `invalidate_token(settings)`.
- Token request: timeout (5, 15). On 400/401 raise `BolAuthError("Invalid bol
  credentials")` with no response body in the message. On a network error or
  5xx raise `BolTransientError`.

### `bol/api/client.py`
```python
class BolClient:
    def __init__(self, settings=None, session=None): ...
    def request(self, method, path, *, version, json=None, params=None,
                accept=None, reference=None) -> dict | bytes | None
```
- Headers: `Accept`/`Content-Type` = `application/vnd.retailer.v{version}+json`
  unless `accept` overrides it (for example the PDF in story 09). `User-Agent` =
  `marketplace_integrations/<version>`.
- Response: JSON → dict; binary `accept` → bytes; 204 → `None`.
- Status mapping and retries follow overview §4.1 exactly:
  - 401: invalidate the token, refresh once, retry once. A second 401 →
    `BolAuthError`.
  - 403 → `BolAuthError` (the message includes `detail`, for example "not
    registered for VVB").
  - 400/422 → `BolValidationError(title, detail, violations)`.
  - 404 → `BolNotFoundError`.
  - 429 → sleep `min(Retry-After, 60)` and retry. After 3 attempts →
    `BolRateLimitError`.
  - 502/503/504, connect errors → backoff 1/2/4s + jitter, max 3 →
    `BolTransientError`.
  - Read timeout on POST → no retry; raise `BolTransientError(ambiguous=True)`.
  - Other 5xx → `BolServerError` (retried like transient errors).
  - Malformed JSON on a 2xx → `BolServerError("Unparseable response")`.
- `get_client()` builds the client from cached settings. If integration is
  disabled it raises `BolError("bol integration disabled")`.

### `bol/api/errors.py`
The taxonomy from overview §4.1. `BolError.__str__` gives a short human message;
`.details` gives a dict for logs.

### `bol/log.py`
- `log_call(method, path, status, request=None, response=None, error=None,
  reference=None)`. It goes through `ecommerce_integrations`' `create_log`
  (signature recorded in story 01) with `integration="Bol"`.
- Writes when a call fails, and on success only for non-GET calls or when
  `debug_logging` is on.
- `redact(obj)`: recursively masks the keys `authorization`, `access_token`,
  `client_secret` (case-insensitive). Applied to headers and bodies.
- `log_once(kind, key, **kwargs)`: logs only if `bol:failed:{kind}:{key}` isn't
  already in the cache, then sets it with a 6h TTL. Used for recurring failures.

### Settings validation
- `validate()`: if `enabled`, the required fields must be set.
- `test_connection()`: whitelisted; Bol Manager / System Manager only. Gets a
  token and calls one cheap GET, for example the orders list with `page=1`.
  Returns `{"ok": true}` or raises a translated `ValidationError`.

## Error handling
Covered by the client mapping above. Saving the settings must never call bol;
only the button does.

## Security
- The secret is read only through `get_password`. A test asserts that a full
  request/response log never contains the secret or the token.
- No `verify=False` anywhere. Timeouts are set on every call.

## Tests (unit, mocked session)
- Token: cached, reused, refreshed after TTL; invalid credentials → `BolAuthError`
  without the body in the message.
- 401 → refresh → success; 401 twice → `BolAuthError`.
- 429 with `Retry-After: 2` → sleeps 2s (patch `time.sleep`) → success; 4×429 →
  `BolRateLimitError`.
- 503 ×2 then 200 → success; 503 ×4 → `BolTransientError`.
- Read timeout on POST → no retry, `ambiguous=True`; read timeout on GET → retried.
- 400 problem+json → `BolValidationError.violations` parsed.
- 404 → `BolNotFoundError`; 204 → `None`; binary accept → bytes.
- `redact` handles nested dicts and lists.
- Disabled settings → `get_client()` raises.

## Acceptance criteria
- **Test connection** succeeds against the demo environment with valid credentials
  and shows a clear error with invalid ones.
- No secrets appear in the logs (test enforced).
