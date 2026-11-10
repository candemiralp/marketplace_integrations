import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol.api import auth
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
)
from marketplace_integrations.bol.tests.utils import (
	SECRET,
	TOKEN,
	FakeSession,
	make_response,
	make_settings,
	token_response,
)


class TestAuth(FrappeTestCase):
	def setUp(self):
		self.settings = make_settings(client_id="auth-test-client")
		auth.invalidate_token(self.settings)
		self.addCleanup(auth.invalidate_token, self.settings)

	def test_token_is_cached_and_reused(self):
		session = FakeSession()

		self.assertEqual(auth.get_token(self.settings, session), TOKEN)
		self.assertEqual(auth.get_token(self.settings, session), TOKEN)

		self.assertEqual(len(session.token_calls), 1)

	def test_token_ttl_is_expires_in_minus_margin(self):
		auth.get_token(self.settings, FakeSession(token=token_response(expires_in=299)))

		ttl = frappe.cache.ttl(frappe.cache.make_key(auth._cache_key(self.settings)))
		self.assertTrue(260 <= ttl <= 269, ttl)

	def test_token_is_refetched_once_expired(self):
		session = FakeSession()
		auth.get_token(self.settings, session)
		# What Redis does when the TTL runs out.
		frappe.cache.delete_value(auth._cache_key(self.settings))

		auth.get_token(self.settings, session)

		self.assertEqual(len(session.token_calls), 2)

	def test_cache_key_is_per_environment_and_hides_client_id(self):
		key = auth._cache_key(self.settings)

		self.assertTrue(key.startswith("bol:token:Demo:"))
		self.assertNotIn("auth-test-client", key)
		self.assertNotEqual(
			key, auth._cache_key(make_settings(client_id="auth-test-client", environment="Production"))
		)

	def test_token_request_uses_basic_auth_and_form_body(self):
		session = FakeSession()

		auth.get_token(self.settings, session)

		call = session.token_calls[0]
		self.assertEqual(call["auth"], ("auth-test-client", SECRET))
		self.assertEqual(call["data"], {"grant_type": "client_credentials"})
		self.assertEqual(call["headers"], {"Accept": "application/json"})
		self.assertEqual(call["timeout"], (5, 15))

	def test_invalid_credentials_raise_auth_error_without_body(self):
		for status in (400, 401):
			body = {"error": "invalid_client", "error_description": "client unknown-body-marker"}
			session = FakeSession(token=make_response(status, body))

			with self.assertRaises(BolAuthError) as ctx:
				auth.get_token(self.settings, session)

			self.assertEqual(str(ctx.exception), "Invalid bol credentials")
			self.assertNotIn("unknown-body-marker", str(ctx.exception))
			self.assertNotIn(SECRET, str(ctx.exception.details))
			self.assertIsNone(frappe.cache.get_value(auth._cache_key(self.settings), expires=True))

	def test_network_error_is_transient(self):
		with self.assertRaises(BolTransientError):
			auth.get_token(self.settings, FakeSession(token=requests.ConnectionError("refused")))

	def test_server_error_is_transient(self):
		with self.assertRaises(BolTransientError):
			auth.get_token(self.settings, FakeSession(token=make_response(503)))

	def test_rate_limit_carries_retry_after(self):
		with self.assertRaises(BolRateLimitError) as ctx:
			auth.get_token(self.settings, FakeSession(token=make_response(429, headers={"Retry-After": "7"})))

		self.assertEqual(ctx.exception.retry_after, 7)

	def test_unusable_token_response(self):
		with self.assertRaises(BolServerError):
			auth.get_token(self.settings, FakeSession(token=make_response(200, {"token_type": "Bearer"})))

	def test_invalidate_token_removes_it(self):
		session = FakeSession()
		auth.get_token(self.settings, session)

		auth.invalidate_token(self.settings)
		auth.get_token(self.settings, session)

		self.assertEqual(len(session.token_calls), 2)
