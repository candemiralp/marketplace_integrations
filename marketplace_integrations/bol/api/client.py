# BolClient: the one way to call the bol API. Retries and status mapping follow overview §4.1.

import random
import time

import frappe
import requests
from urllib3.exceptions import NewConnectionError

from marketplace_integrations import __version__
from marketplace_integrations.bol.api.auth import get_token, invalidate_token, retry_after_seconds
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolError,
	BolNotFoundError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.log import log_call, redact

API_HOST = "https://api.bol.com"
TIMEOUT = (5, 30)
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60
USER_AGENT = f"marketplace_integrations/{__version__}"


def get_client() -> "BolClient":
	settings = frappe.get_cached_doc("Bol Settings")
	if not settings.enabled:
		raise BolError("bol integration disabled")
	return BolClient(settings)


class BolClient:
	def __init__(self, settings=None, session=None):
		self.settings = settings or frappe.get_cached_doc("Bol Settings")
		self.session = session or requests.Session()

	def request(
		self,
		method: str,
		path: str,
		*,
		version: int,
		json=None,
		params=None,
		accept: str | None = None,
		reference: tuple[str, str] | None = None,
		safe: bool = False,
	) -> dict | bytes | None:
		"""Call bol and return parsed JSON, bytes (non-JSON `accept`) or None (empty response).

		`path` is the path as written in bol's spec (`/retailer/orders`, `/shared/process-status/1`).
		`safe=True` marks a POST that only reads (bulk queries): it's retried and logged like a GET.
		Successful calls are logged here; failures raise a BolError and the caller logs them
		(`log_call(**err.details, error=err)` or `log_once`) once it has rolled back its own work.
		"""
		method = method.upper()
		# Only a write sent as POST can be duplicated by a retry.
		may_duplicate = method == "POST" and not safe
		url = self._url(path)
		headers = _headers(version, accept, has_body=json is not None)
		retries = 0
		refreshed = False

		while True:
			response = None
			try:
				response = self._send(method, url, headers, json, params, may_duplicate)
				if response.status_code == 401 and not refreshed:
					# The cached token may have been revoked or expired early: refresh once.
					refreshed = True
					invalidate_token(self.settings)
					continue
				result = _parse(response, accept, may_duplicate)
			except BolError as e:
				delay = _retry_delay(e, retries)
				if delay is None:
					e.details = e.details or _log_data(
						method, path, url, params, json, headers, response, accept
					)
					raise
				retries += 1
				time.sleep(delay)
				continue

			log_call(
				**_log_data(method, path, url, params, json, headers, response, accept),
				reference=reference,
				safe=safe,
			)
			return result

	def _url(self, path: str) -> str:
		path = path.lstrip("/")
		if self.settings.environment == "Demo":
			# Demo mirrors each API root with a -demo suffix: /retailer-demo/…, /shared-demo/…
			root, _, rest = path.partition("/")
			path = f"{root}-demo/{rest}"
		return f"{API_HOST}/{path}"

	def _send(self, method, url, headers, json, params, may_duplicate) -> requests.Response:
		token = get_token(self.settings, self.session)
		try:
			return self.session.request(
				method,
				url,
				headers={**headers, "Authorization": f"Bearer {token}"},
				json=json,
				params=params,
				timeout=TIMEOUT,
			)
		except (requests.ConnectionError, requests.Timeout) as e:
			# A POST that may have reached bol must not be retried blindly (it could duplicate).
			raise BolTransientError(
				f"bol request failed: {type(e).__name__}",
				ambiguous=may_duplicate and not _never_sent(e),
			)


def _headers(version: int, accept: str | None, has_body: bool) -> dict:
	media_type = f"application/vnd.retailer.v{version}+json"
	headers = {"Accept": accept or media_type, "User-Agent": USER_AGENT}
	if has_body:
		headers["Content-Type"] = accept if accept and _is_json(accept) else media_type
	return headers


def _is_json(media_type: str) -> bool:
	return media_type.endswith("json")


def _never_sent(error: requests.RequestException) -> bool:
	"""True when the request certainly never reached bol (connect timeout, refused, DNS)."""
	if isinstance(error, requests.ConnectTimeout):
		return True
	reason = getattr(error.args[0], "reason", None) if error.args else None
	return isinstance(reason, NewConnectionError)


def _parse(response: requests.Response, accept: str | None, may_duplicate: bool):
	status = response.status_code

	if 200 <= status < 300:
		if status == 204 or not response.content:
			return None
		if accept and not _is_json(accept):
			return response.content
		try:
			return response.json()
		except ValueError:
			raise BolServerError("Unparseable response", ambiguous=may_duplicate)

	problem = _problem(response)
	detail = problem.get("detail")
	if status in (401, 403):
		raise BolAuthError(f"bol denied access (HTTP {status})" + (f": {detail}" if detail else ""))
	if status in (400, 422):
		raise BolValidationError(problem.get("title"), detail, problem.get("violations"))
	if status == 404:
		raise BolNotFoundError(detail or "Not found")
	if status == 429:
		raise BolRateLimitError("bol rate limit reached", retry_after=retry_after_seconds(response))
	if status in (502, 503, 504):
		# 503 means bol didn't process the request; a 502/504 gateway may have forwarded it.
		raise BolTransientError(f"bol unavailable (HTTP {status})", ambiguous=may_duplicate and status != 503)
	if status >= 500:
		raise BolServerError(f"bol server error (HTTP {status})", ambiguous=may_duplicate)
	raise BolError(f"Unexpected bol response (HTTP {status})" + (f": {detail}" if detail else ""))


def _problem(response: requests.Response) -> dict:
	# bol sends problems with the resource's media type, not application/problem+json.
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def _retry_delay(error: BolError, retries: int) -> float | None:
	if retries >= MAX_RETRIES or error.ambiguous:
		return None
	if isinstance(error, BolRateLimitError):
		return min(error.retry_after, MAX_RETRY_AFTER)
	if isinstance(error, BolTransientError | BolServerError):
		return 2**retries + random.uniform(0, 1)
	return None


def _log_data(method, path, url, params, json, headers, response, accept) -> dict:
	return redact(
		{
			"method": method,
			"path": path,
			"status": response.status_code if response is not None else None,
			"request": {"url": url, "params": params, "headers": headers, "json": json},
			"response": _response_data(response, accept),
		}
	)


def _response_data(response: requests.Response | None, accept: str | None) -> dict | None:
	if response is None:
		return None
	if response.ok and accept and not _is_json(accept):
		body = f"<{len(response.content)} bytes>"
	else:
		try:
			body = response.json()
		except ValueError:
			body = response.text
	return {"status": response.status_code, "headers": dict(response.headers), "body": body}
