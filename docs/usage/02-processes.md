# Async operations (Bol Process)

Design: [ADR-006](../decisions.md#adr-006--process-status-tracker-story-03).

bol handles shipments, cancellations, returns, labels and
content uploads asynchronously. Each one is a **Bol Process** row (read-only), followed every minute
until bol reports a final state:

| Status | Meaning |
|---|---|
| `PENDING` | Waiting for bol. Checked again after 10 s, 30 s, 1 m, 2 m, 5 m, then every 10 m. |
| `SUCCESS` | bol processed it; the follow-up (e.g. marking the delivery shipped) ran. |
| `FAILURE` / `TIMEOUT` | bol rejected it or didn't finish within 24 h. *Error Message* says why. The related document gets a comment and the failure is in the integration log. Fix the cause and start the action again from that document. |
| `HANDLER_ERROR` | bol is done, but our follow-up code failed. The related document and the Bol Process have a comment, and the traceback is in the integration log. After the bug is fixed, click **Retry handler** (System Manager / Bol Manager). |

Polling only runs while *Enabled* is ticked in Bol Settings. Final rows are deleted after 90 days;
`HANDLER_ERROR` rows are kept until someone retries them.
