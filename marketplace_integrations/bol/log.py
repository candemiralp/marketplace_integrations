# Thin adapter over ecommerce_integrations' Ecommerce Integration Log (ADR-005).
#
# Logs are written when the caller's transaction ends (commit *or* rollback), never in the middle
# of it: create_log commits on the spot, which would persist a half-processed record and drop its
# savepoint. Deferring also keeps a failure's log when the record itself is rolled back.

import json
import traceback

import frappe

INTEGRATION = "Bol"
LOG_DOCTYPE = "Ecommerce Integration Log"
REDACTED_KEYS = frozenset({"authorization", "access_token", "client_secret"})
MASK = "********"
FAILURE_MARKER_TTL = 6 * 60 * 60


def redact(obj):
	"""Copy of `obj` with secret-bearing keys masked, at any depth."""
	if isinstance(obj, dict):
		return {
			key: MASK if isinstance(key, str) and key.lower() in REDACTED_KEYS else redact(value)
			for key, value in obj.items()
		}
	if isinstance(obj, list | tuple):
		return [redact(value) for value in obj]
	return obj


def log_call(
	method=None,
	path=None,
	status=None,
	request=None,
	response=None,
	error=None,
	reference: tuple[str, str] | None = None,
	safe: bool = False,
):
	"""Log a bol call. Failures are always logged; successes only for writes or with debug logging.

	`safe` marks a call that changed nothing at bol even though it isn't a GET (a POST query).
	"""
	if error is None and not _log_success(method, safe):
		return

	reference_doctype, reference_name = reference or (None, None)
	_write_after_transaction(
		{
			"doctype": LOG_DOCTYPE,
			"integration": INTEGRATION,
			"status": "Error" if error else "Success",
			"method": " ".join(str(part) for part in (method, path) if part),
			"message": f"{type(error).__name__}: {error}" if error else f"{method} {path} → {status}",
			"request_data": _dump(request),
			"response_data": _dump(response),
			"traceback": "".join(traceback.format_exception(error)) if error else None,
			"bol_reference_doctype": reference_doctype,
			"bol_reference_name": reference_name,
		}
	)


def log_once(kind: str, key: str, **kwargs) -> bool:
	"""log_call, unless the same failure (kind, key) was already logged in the last 6 hours."""
	marker = f"bol:failed:{kind}:{key}"
	if frappe.cache.get_value(marker, expires=True):
		return False
	log_call(**kwargs)
	frappe.cache.set_value(marker, 1, expires_in_sec=FAILURE_MARKER_TTL)
	return True


def add_comment(reference: tuple[str, str] | None, text: str):
	"""Show a failure on the related document's timeline (in the caller's transaction)."""
	doctype, name = reference or (None, None)
	if not (doctype and name and frappe.db.exists(doctype, name)):
		return
	frappe.get_doc(doctype, name).add_comment("Comment", text)


def _log_success(method, safe) -> bool:
	return (method != "GET" and not safe) or bool(frappe.db.get_single_value("Bol Settings", "debug_logging"))


def _dump(data):
	if data is None or isinstance(data, str):
		return data
	return json.dumps(redact(data), sort_keys=True, indent=4, default=str)


def _write_after_transaction(log: dict):
	pending = getattr(frappe.local, "bol_pending_logs", None)
	if pending is None:
		pending = frappe.local.bol_pending_logs = []
		# commit() clears after_rollback and rollback() clears after_commit, so _flush runs once.
		frappe.db.after_commit.add(_flush)
		frappe.db.after_rollback.add(_flush)
	pending.append(log)


def _flush():
	pending = getattr(frappe.local, "bol_pending_logs", None)
	frappe.local.bol_pending_logs = None
	if not pending:
		return
	for log in pending:
		doc = frappe.get_doc(log)
		# The referenced document may have been rolled back; the log must be kept anyway.
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
	# The caller's transaction has already ended here; this commits only the logs.
	frappe.db.commit()  # nosemgrep
