from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from urllib3.exceptions import MaxRetryError, NewConnectionError

from marketplace_integrations import __version__
from marketplace_integrations.bol.api import auth, client
from marketplace_integrations.bol.api.client import BolClient, get_client
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolError,
	BolNotFoundError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.tests.utils import TOKEN, FakeSession, make_response, make_settings

ORDERS = {"orders": [{"orderId": "1043946570"}]}
PROBLEM = {
	"type": "https://api.bol.com/problems",
	"title": "Error validating request. Consult the bol.com API documentation for more information.",
	"status": 400,
	"detail": "Bad request",
	"host": "Instance-001",
	"instance": "https://api.bol.com//retailer/resource",
	"violations": [{"name": "exampleValue", "reason": "Request contains invalid value(s): 'INVALID'"}],
}


class TestBolClient(FrappeTestCase):
	def setUp(self):
		self.settings = make_settings(client_id="client-test-client")
		auth.invalidate_token(self.settings)
		self.addCleanup(auth.invalidate_token, self.settings)
		self.sleep = self._patch("marketplace_integrations.bol.api.client.time.sleep")
		self.log_call = self._patch("marketplace_integrations.bol.api.client.log_call")

	def _patch(self, target):
		patcher = patch(target)
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _client(self, *responses, **settings):
		session = FakeSession(*responses)
		settings_doc = make_settings(client_id="client-test-client", **settings)
		return BolClient(settings_doc, session), session

	def test_get_returns_json(self):
		bol, session = self._client(make_response(200, ORDERS))

		result = bol.request("GET", "/retailer/orders", version=10, params={"status": "OPEN"})

		self.assertEqual(result, ORDERS)
		call = session.calls[0]
		self.assertEqual(call["url"], "https://api.bol.com/retailer-demo/orders")
		self.assertEqual(call["params"], {"status": "OPEN"})
		self.assertEqual(call["timeout"], (5, 30))
		self.assertEqual(call["headers"]["Accept"], "application/vnd.retailer.v10+json")
		self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
		self.assertEqual(call["headers"]["User-Agent"], f"marketplace_integrations/{__version__}")
		self.assertNotIn("Content-Type", call["headers"])
		self.assertNotIn("verify", call)

	def test_url_per_environment(self):
		demo, _ = self._client()
		production, _ = self._client(environment="Production")

		self.assertEqual(
			demo._url("/shared/process-status/1"), "https://api.bol.com/shared-demo/process-status/1"
		)
		self.assertEqual(production._url("/retailer/orders"), "https://api.bol.com/retailer/orders")

	def test_json_body_sets_content_type(self):
		bol, session = self._client(make_response(202, {"processStatusId": "1"}))

		bol.request("POST", "/retailer/offers", version=11, json={"ean": "0000007740404"})

		headers = session.calls[0]["headers"]
		self.assertEqual(headers["Content-Type"], "application/vnd.retailer.v11+json")
		self.assertEqual(session.calls[0]["json"], {"ean": "0000007740404"})

	def test_json_accept_override_applies_to_content_type(self):
		media_type = "application/vnd.economic-operator.v1+json"
		bol, session = self._client(make_response(200, {}))

		bol.request("POST", "/retailer/economic-operator", version=1, json={}, accept=media_type)

		self.assertEqual(session.calls[0]["headers"]["Accept"], media_type)
		self.assertEqual(session.calls[0]["headers"]["Content-Type"], media_type)

	def test_401_refreshes_token_once(self):
		bol, session = self._client(make_response(401), make_response(200, ORDERS))

		self.assertEqual(bol.request("GET", "/retailer/orders", version=10), ORDERS)
		self.assertEqual(len(session.token_calls), 2)

	def test_401_twice_raises_auth_error(self):
		bol, session = self._client(make_response(401), make_response(401))

		with self.assertRaises(BolAuthError):
			bol.request("GET", "/retailer/orders", version=10)
		self.assertEqual(len(session.calls), 2)

	def test_403_includes_detail(self):
		problem = {**PROBLEM, "status": 403, "detail": "Retailer is not registered for VVB"}
		bol, _ = self._client(make_response(403, problem))

		with self.assertRaises(BolAuthError) as ctx:
			bol.request("POST", "/retailer/shipping-labels", version=10, json={})
		self.assertIn("not registered for VVB", str(ctx.exception))

	def test_429_sleeps_retry_after_then_succeeds(self):
		bol, _ = self._client(make_response(429, headers={"Retry-After": "2"}), make_response(200, ORDERS))

		self.assertEqual(bol.request("GET", "/retailer/orders", version=10), ORDERS)
		self.sleep.assert_called_once_with(2)

	def test_retry_after_is_capped(self):
		bol, _ = self._client(make_response(429, headers={"Retry-After": "600"}), make_response(200, ORDERS))

		bol.request("GET", "/retailer/orders", version=10)
		self.sleep.assert_called_once_with(60)

	def test_429_four_times_raises_rate_limit(self):
		bol, session = self._client(*[make_response(429, headers={"Retry-After": "1"}) for _ in range(4)])

		with self.assertRaises(BolRateLimitError):
			bol.request("POST", "/retailer/offers", version=11, json={})
		self.assertEqual(len(session.calls), 4)

	def test_503_twice_then_success(self):
		bol, session = self._client(make_response(503), make_response(503), make_response(200, ORDERS))

		self.assertEqual(bol.request("GET", "/retailer/orders", version=10), ORDERS)
		self.assertEqual(len(session.calls), 3)
		delays = [c.args[0] for c in self.sleep.call_args_list]
		self.assertTrue(1 <= delays[0] < 2 and 2 <= delays[1] < 3, delays)

	def test_503_four_times_raises_transient(self):
		bol, session = self._client(*[make_response(503) for _ in range(4)])

		with self.assertRaises(BolTransientError) as ctx:
			bol.request("GET", "/retailer/orders", version=10)
		self.assertEqual(len(session.calls), 4)
		self.assertFalse(ctx.exception.ambiguous)

	def test_503_on_post_is_retried(self):
		bol, session = self._client(make_response(503), make_response(202, {"processStatusId": "1"}))

		bol.request("POST", "/retailer/offers", version=11, json={})
		self.assertEqual(len(session.calls), 2)

	def test_read_timeout_on_post_is_ambiguous_and_not_retried(self):
		bol, session = self._client(requests.ReadTimeout())

		with self.assertRaises(BolTransientError) as ctx:
			bol.request("POST", "/retailer/shipments", version=10, json={})
		self.assertTrue(ctx.exception.ambiguous)
		self.assertEqual(len(session.calls), 1)
		self.sleep.assert_not_called()

	def test_read_timeout_on_get_is_retried(self):
		bol, session = self._client(requests.ReadTimeout(), make_response(200, ORDERS))

		self.assertEqual(bol.request("GET", "/retailer/orders", version=10), ORDERS)
		self.assertEqual(len(session.calls), 2)

	def test_connection_refused_on_post_is_retried(self):
		refused = requests.ConnectionError(
			MaxRetryError(None, "/retailer/offers", NewConnectionError(None, "Connection refused"))
		)
		bol, session = self._client(refused, requests.ConnectTimeout(), make_response(202, {}))

		bol.request("POST", "/retailer/offers", version=11, json={})
		self.assertEqual(len(session.calls), 3)

	def test_dropped_connection_on_post_is_ambiguous(self):
		bol, session = self._client(requests.ConnectionError("Connection aborted"))

		with self.assertRaises(BolTransientError) as ctx:
			bol.request("POST", "/retailer/offers", version=11, json={})
		self.assertTrue(ctx.exception.ambiguous)
		self.assertEqual(len(session.calls), 1)

	def test_gateway_errors_on_post_are_ambiguous(self):
		for status in (502, 504):
			bol, session = self._client(make_response(status))

			with self.assertRaises(BolTransientError) as ctx:
				bol.request("POST", "/retailer/offers", version=11, json={})
			self.assertTrue(ctx.exception.ambiguous)
			self.assertEqual(len(session.calls), 1)

	def test_other_5xx_is_server_error_and_retried_for_get(self):
		bol, session = self._client(make_response(500), make_response(200, ORDERS))

		self.assertEqual(bol.request("GET", "/retailer/orders", version=10), ORDERS)
		self.assertEqual(len(session.calls), 2)

	def test_other_5xx_on_post_is_not_retried(self):
		bol, session = self._client(make_response(500))

		with self.assertRaises(BolServerError) as ctx:
			bol.request("POST", "/retailer/offers", version=11, json={})
		self.assertTrue(ctx.exception.ambiguous)
		self.assertEqual(len(session.calls), 1)

	def test_400_parses_violations(self):
		bol, session = self._client(make_response(400, PROBLEM))

		with self.assertRaises(BolValidationError) as ctx:
			bol.request("PUT", "/retailer/offers/1/price", version=11, json={})
		error = ctx.exception
		self.assertEqual(error.title, PROBLEM["title"])
		self.assertEqual(error.detail, "Bad request")
		self.assertEqual(error.violations, PROBLEM["violations"])
		self.assertEqual(len(session.calls), 1)

	def test_error_details_hold_the_call_for_logging(self):
		bol, _ = self._client(make_response(400, PROBLEM))

		with self.assertRaises(BolValidationError) as ctx:
			bol.request("PUT", "/retailer/offers/1/price", version=11, json={"pricing": {}})
		details = ctx.exception.details
		self.assertEqual(details["method"], "PUT")
		self.assertEqual(details["path"], "/retailer/offers/1/price")
		self.assertEqual(details["status"], 400)
		self.assertEqual(details["request"]["json"], {"pricing": {}})
		self.assertEqual(details["response"]["body"], PROBLEM)
		self.log_call.assert_not_called()

	def test_404(self):
		bol, _ = self._client(make_response(404, {**PROBLEM, "status": 404, "detail": "Order not found"}))

		with self.assertRaises(BolNotFoundError):
			bol.request("GET", "/retailer/orders/1", version=10)

	def test_unexpected_4xx(self):
		bol, session = self._client(make_response(406, {**PROBLEM, "status": 406}))

		with self.assertRaises(BolError):
			bol.request("GET", "/retailer/orders", version=10)
		self.assertEqual(len(session.calls), 1)

	def test_204_returns_none(self):
		bol, _ = self._client(make_response(204))

		self.assertIsNone(bol.request("DELETE", "/retailer/offers/1", version=11))

	def test_binary_accept_returns_bytes(self):
		pdf = b"%PDF-1.4 label"
		bol, session = self._client(
			make_response(200, content=pdf, headers={"Content-Type": "application/pdf"})
		)

		result = bol.request(
			"GET", "/retailer/shipping-labels/1", version=10, accept="application/vnd.retailer.v10+pdf"
		)

		self.assertEqual(result, pdf)
		self.assertEqual(session.calls[0]["headers"]["Accept"], "application/vnd.retailer.v10+pdf")

	def test_unparseable_json_is_server_error(self):
		bol, _ = self._client(*[make_response(200, content=b"<html>oops</html>") for _ in range(4)])

		with self.assertRaises(BolServerError) as ctx:
			bol.request("GET", "/retailer/orders", version=10)
		self.assertEqual(str(ctx.exception), "Unparseable response")

	def test_successful_call_is_handed_to_log_call(self):
		bol, _ = self._client(make_response(202, {"processStatusId": "1"}))

		bol.request("POST", "/retailer/offers", version=11, json={}, reference=("Item", "SKU-1"))

		kwargs = self.log_call.call_args.kwargs
		self.assertEqual(kwargs["method"], "POST")
		self.assertEqual(kwargs["status"], 202)
		self.assertEqual(kwargs["reference"], ("Item", "SKU-1"))
		self.assertNotIn("Authorization", kwargs["request"]["headers"])

	def test_get_client_raises_when_disabled(self):
		settings = frappe.get_single("Bol Settings")
		settings.enabled = 0
		with patch.object(client.frappe, "get_cached_doc", return_value=settings):
			self.assertRaisesRegex(BolError, "bol integration disabled", get_client)

	def test_get_client_when_enabled(self):
		with patch.object(client.frappe, "get_cached_doc", return_value=make_settings()):
			self.assertIsInstance(get_client(), BolClient)
