# Test doubles for bol HTTP calls. No test talks to bol.

import json

import frappe
import requests
from requests.structures import CaseInsensitiveDict

from marketplace_integrations.bol.api.auth import TOKEN_URL

SECRET = "s3cr3t-client-secret"
TOKEN = "tok-3f9a1c7e"


def make_settings(**overrides) -> frappe._dict:
	settings = frappe._dict(
		doctype="Bol Settings",
		name="Bol Settings",
		enabled=1,
		environment="Demo",
		client_id="test-client-id",
		debug_logging=0,
	)
	settings.update(overrides)
	settings.get_password = lambda fieldname: SECRET
	return settings


def make_response(status=200, json_body=None, headers=None, content=b"") -> requests.Response:
	response = requests.Response()
	response.status_code = status
	response.headers = CaseInsensitiveDict(headers or {})
	response.encoding = "utf-8"
	if json_body is not None:
		response._content = json.dumps(json_body).encode()
		response.headers.setdefault("Content-Type", "application/vnd.retailer.v10+json")
	else:
		response._content = content
	return response


def token_response(expires_in=299) -> requests.Response:
	return make_response(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": expires_in})


class FakeSession:
	"""Replays queued API responses (or raises queued exceptions) and answers token requests."""

	def __init__(self, *responses, token=None):
		self.responses = list(responses)
		self.token = token
		self.calls = []
		self.token_calls = []

	def request(self, method, url, **kwargs):
		if url == TOKEN_URL:
			self.token_calls.append(kwargs)
			result = self.token if self.token is not None else token_response()
		else:
			self.calls.append({"method": method, "url": url, **kwargs})
			result = self.responses.pop(0)
		if isinstance(result, Exception):
			raise result
		return result
