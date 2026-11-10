# OAuth2 client-credentials token for the bol Retailer API, cached in Redis only.
# bol rate-limits login.bol.com far below the API and bans IPs that fetch a token per call,
# so the token is reused until shortly before it expires.

import hashlib

import frappe
import requests

from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
)
from marketplace_integrations.bol.log import redact

TOKEN_URL = "https://login.bol.com/token"
TOKEN_TIMEOUT = (5, 15)
EXPIRY_MARGIN = 30


def get_token(settings, session=None) -> str:
	key = _cache_key(settings)
	token = frappe.cache.get_value(key, expires=True)
	if token:
		return token

	token, expires_in = _fetch_token(settings, session or requests.Session())
	frappe.cache.set_value(key, token, expires_in_sec=max(expires_in - EXPIRY_MARGIN, 1))
	return token


def invalidate_token(settings):
	frappe.cache.delete_value(_cache_key(settings))


def _cache_key(settings) -> str:
	client_hash = hashlib.sha256((settings.client_id or "").encode()).hexdigest()[:12]
	return f"bol:token:{settings.environment}:{client_hash}"


def _fetch_token(settings, session) -> tuple[str, int]:
	details = {"method": "POST", "path": TOKEN_URL, "request": {"url": TOKEN_URL}}
	try:
		response = session.request(
			"POST",
			TOKEN_URL,
			data={"grant_type": "client_credentials"},
			auth=(settings.client_id, settings.get_password("client_secret")),
			headers={"Accept": "application/json"},
			timeout=TOKEN_TIMEOUT,
		)
	except requests.RequestException as e:
		raise BolTransientError(f"Could not reach bol login: {type(e).__name__}", details)

	status = response.status_code
	details["status"] = status
	if status != 200:
		# The body is kept out of the message on purpose; it's only in the (redacted) details.
		details["response"] = redact({"status": status, "body": response.text})
	if status in (400, 401):
		raise BolAuthError("Invalid bol credentials", details)
	if status == 429:
		raise BolRateLimitError(
			"bol login rate limit reached", details, retry_after=retry_after_seconds(response)
		)
	if status >= 500:
		raise BolTransientError(f"bol login unavailable (HTTP {status})", details)
	if status != 200:
		raise BolAuthError(f"Unexpected bol login response (HTTP {status})", details)

	try:
		body = response.json()
		return body["access_token"], int(body["expires_in"])
	except (ValueError, KeyError, TypeError):
		raise BolServerError("Unparseable bol login response", details)


def retry_after_seconds(response) -> int:
	try:
		return int(response.headers.get("Retry-After"))
	except (TypeError, ValueError):
		return 1
