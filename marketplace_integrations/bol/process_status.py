# Follows every async bol operation (HTTP 202 + processStatusId) to a final state, then runs the
# handler registered in code for it (story 03, ADR-006). One Bol Process row per processStatusId;
# its name *is* the processStatusId, an opaque string (never cast it to int).

import importlib
from collections.abc import Callable
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError
from marketplace_integrations.bol.log import add_comment, log_call, log_once

DOCTYPE = "Bol Process"
PATH = "/shared/process-status"
VERSION = 10
BATCH_SIZE = 100
BACKOFF = (10, 30, 60, 120, 300)
BACKOFF_MAX = 600
# bol keeps process statuses for at most one day, so there is nothing to wait for after that.
TIMEOUT_AFTER = timedelta(hours=24)
FINAL = ("SUCCESS", "FAILURE", "TIMEOUT")
RETENTION_DAYS = 90
ROW_SAVEPOINT = "bol_process_row"
HANDLER_SAVEPOINT = "bol_process_handler"

# name -> (on_success(doc, entity_id, payload), on_failure(doc, message, payload)).
# Feature modules fill it at import time with register(). Handlers must be idempotent: Retry handler
# may run them again.
HANDLERS: dict[str, tuple[Callable, Callable]] = {}

# Modules that call register(). A background job only imports this module, so they are imported
# before any handler lookup. Each story that adds a handler adds its module here.
HANDLER_MODULES: tuple[str, ...] = ("marketplace_integrations.bol.shipments",)


def register(name: str, on_success: Callable, on_failure: Callable):
	HANDLERS[name] = (on_success, on_failure)


def track(response: dict, handler: str, reference_doc, payload: dict | None = None) -> str:
	"""Follow the process in bol's 202 `response` until it is final. Returns the Bol Process name.

	Idempotent: tracking the same processStatusId again returns the existing row.
	"""
	if handler not in _handlers():
		raise ValueError(f"Unknown bol process handler: {handler}")
	process_status_id = (response or {}).get("processStatusId")
	if not process_status_id:
		raise BolError("bol response has no processStatusId")
	process_status_id = str(process_status_id)

	if frappe.db.exists(DOCTYPE, process_status_id):
		return process_status_id
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"process_status_id": process_status_id,
			"handler": handler,
			"reference_doctype": reference_doc.doctype,
			"reference_name": reference_doc.name,
			"payload": frappe.as_json(payload or {}),
			"status": "PENDING",
			"attempts": 0,
			"next_check_at": add_to_date(now_datetime(), seconds=backoff(0)),
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Tracked concurrently by another job: already done.
		return process_status_id
	return process_status_id


def poll_pending():
	"""Scheduler (every minute): check the due PENDING processes with one bulk call."""
	if not frappe.db.get_single_value("Bol Settings", "enabled"):
		return
	names = frappe.get_all(
		DOCTYPE,
		filters={"status": "PENDING", "next_check_at": ("<=", now_datetime())},
		order_by="creation asc",
		limit=BATCH_SIZE,
		pluck="name",
	)
	if names:
		_check(names)


def check_one(name: str):
	"""Check one process now, whatever its next_check_at."""
	_check([name])


def run_handler(doc) -> Exception | None:
	"""Run the handler for bol's final status (`bol_status`). Returns the handler's error, if any.

	On error the handler's work is rolled back and the row becomes HANDLER_ERROR. It isn't polled
	again because bol's state is final; the fix is a manual Retry handler once the bug is fixed.
	"""
	payload = frappe.parse_json(doc.payload) if doc.payload else {}
	frappe.db.savepoint(HANDLER_SAVEPOINT)
	try:
		on_success, on_failure = _handlers()[doc.handler]
		if doc.bol_status == "SUCCESS":
			on_success(doc, doc.entity_id, payload)
		else:
			on_failure(doc, failure_message(doc), payload)
	except Exception as e:
		frappe.db.rollback(save_point=HANDLER_SAVEPOINT)
		doc.status = "HANDLER_ERROR"
		doc.save(ignore_permissions=True)
		log_call(
			path=f"{PATH}/{doc.name}",
			request={"handler": doc.handler, "payload": payload},
			error=e,
			reference=_reference(doc),
		)
		message = _("bol {0} handler failed: {1}").format(_label(doc), str(e))
		add_comment(_reference(doc), message)
		doc.add_comment("Comment", message)
		return e

	doc.status = doc.bol_status
	doc.save(ignore_permissions=True)
	return None


def failure_message(doc) -> str:
	return doc.error_message or _("bol reported {0}").format(doc.bol_status)


def delete_old_processes():
	"""Daily: delete final rows older than 90 days. HANDLER_ERROR rows stay until someone acts."""
	if not frappe.db.get_single_value("Bol Settings", "enabled"):
		return
	cutoff = add_to_date(now_datetime(), days=-RETENTION_DAYS)
	names = frappe.get_all(
		DOCTYPE, filters={"status": ("in", FINAL), "modified": ("<", cutoff)}, pluck="name"
	)
	if not names:
		return
	frappe.db.delete("Comment", {"reference_doctype": DOCTYPE, "reference_name": ("in", names)})
	frappe.db.delete(DOCTYPE, {"name": ("in", names)})


def backoff(attempts: int) -> int:
	"""Seconds until the next check after `attempts` checks: 10s, 30s, 1m, 2m, 5m, then every 10m."""
	return BACKOFF[attempts] if attempts < len(BACKOFF) else BACKOFF_MAX


def timed_out(created, now: datetime) -> bool:
	return get_datetime(now) - get_datetime(created) > TIMEOUT_AFTER


def _check(names: list[str]):
	results = _fetch(names)
	for name in names:
		_check_row(name, results.get(name))
		# One record per transaction (overview §4.1 rule 4).
		frappe.db.commit()  # nosemgrep


def _fetch(ids: list[str]) -> dict[str, dict]:
	"""bol's process statuses by id. Ids bol didn't return (yet, or any more) are left out."""
	body = {"processStatusQueries": [{"processStatusId": process_id} for process_id in ids]}
	try:
		response = get_client().request("POST", PATH, version=VERSION, json=body, safe=True)
	except BolError as e:
		# Every row is treated as "no news" and backs off; bol is asked again on a later run.
		log_once("process_status", "fetch", **e.details, error=e)
		return {}
	statuses = (response or {}).get("processStatuses") or []
	return {status["processStatusId"]: status for status in statuses if status.get("processStatusId")}


def _check_row(name: str, result: dict | None):
	frappe.db.savepoint(ROW_SAVEPOINT)
	try:
		doc = frappe.get_doc(DOCTYPE, name, for_update=True)
		if doc.status != "PENDING":
			return
		doc.attempts = cint(doc.attempts) + 1
		bol_status = (result or {}).get("status")
		if bol_status in FINAL:
			_finish(doc, bol_status, result.get("entityId"), result.get("errorMessage"), result)
		elif timed_out(doc.creation, now_datetime()):
			_finish(doc, "TIMEOUT", None, _("bol reported no final status within 24 hours."), result)
		else:
			doc.next_check_at = add_to_date(now_datetime(), seconds=backoff(doc.attempts))
			doc.save(ignore_permissions=True)
	except Exception as e:
		# A problem with this row must not stop the others; it is checked again on the next run.
		frappe.db.rollback(save_point=ROW_SAVEPOINT)
		log_once("process_status", name, path=f"{PATH}/{name}", error=e, reference=(DOCTYPE, name))


def _finish(doc, bol_status: str, entity_id: str | None, error_message: str | None, response: dict | None):
	doc.bol_status = bol_status
	doc.entity_id = entity_id
	doc.error_message = error_message
	if bol_status != "SUCCESS":
		message = failure_message(doc)
		log_call(
			path=f"{PATH}/{doc.name}", response=response, error=BolError(message), reference=_reference(doc)
		)
		add_comment(_reference(doc), _("bol {0} failed: {1}").format(_label(doc), message))
	run_handler(doc)


def _handlers() -> dict[str, tuple[Callable, Callable]]:
	for module in HANDLER_MODULES:
		importlib.import_module(module)
	return HANDLERS


def _reference(doc) -> tuple[str, str]:
	return (doc.reference_doctype, doc.reference_name)


def _label(doc) -> str:
	return doc.handler.replace("_", " ")
