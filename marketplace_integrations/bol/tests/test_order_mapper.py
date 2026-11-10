# Pure order mapper tests (story 07): no database.

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.dates import to_system_datetime
from marketplace_integrations.bol.mappers.order import money, open_quantity, to_sales_order

ORDER = json.loads((Path(__file__).parent / "fixtures" / "order.json").read_text())
SHIRT, BAG = "_Test bol order shirt", "_Test bol order bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
TZ = "Europe/Amsterdam"


def context(**overrides) -> dict:
	return {
		"customer": "bol.com",
		"company": "Magnatlas",
		"warehouse": "Stores - MG",
		"items": {SHIRT_EAN: [SHIRT], BAG_EAN: [BAG]},
		"countries": {"NL": "Netherlands", "BE": "Belgium", "DE": "Germany"},
		"tax_templates": {"NL": "VAT NL incl", "BE": "VAT BE incl"},
		"timezone": TZ,
		"now": datetime(2026, 11, 10, 12, 0),
		**overrides,
	}


def order(**overrides) -> dict:
	result = copy.deepcopy(ORDER)
	result.update(copy.deepcopy(overrides))
	return result


def line(index: int = 0, **values) -> dict:
	"""An order whose line `index` is updated with `values`."""
	result = order()
	result["orderItems"][index].update(values)
	return result


class TestSalesOrder(unittest.TestCase):
	def test_header(self):
		so = to_sales_order(order(), context())["sales_order"]

		self.assertEqual(so["customer"], "bol.com")
		self.assertEqual(so["company"], "Magnatlas")
		self.assertEqual(so["bol_order_id"], "A2K8290LP8")
		self.assertEqual(so["po_no"], "A2K8290LP8")
		self.assertEqual(so["transaction_date"], "2026-11-09")
		self.assertEqual(so["ignore_pricing_rule"], 1)
		self.assertEqual(so["disable_rounded_total"], 1)
		self.assertEqual(so["currency"], "EUR")
		self.assertEqual(so["set_warehouse"], "Stores - MG")

	def test_lines_carry_bol_ids_and_prices(self):
		so = to_sales_order(order(), context())["sales_order"]

		shirt, bag = so["items"]
		self.assertEqual(
			{key: shirt[key] for key in ("item_code", "qty", "rate", "price_list_rate", "discount_amount")},
			{"item_code": SHIRT, "qty": 2, "rate": 24.95, "price_list_rate": 24.95, "discount_amount": 0.0},
		)
		self.assertEqual(shirt["bol_order_item_id"], "2012345678")
		self.assertEqual(shirt["bol_distribution_party"], "RETAILER")
		self.assertEqual(shirt["bol_latest_delivery_date"], "2026-11-12")
		self.assertEqual(shirt["bol_commission"], 5.18)
		self.assertEqual(shirt["warehouse"], "Stores - MG")
		self.assertEqual((bag["item_code"], bag["qty"], bag["rate"]), (BAG, 1, 12.99))

	def test_delivery_date_is_the_earliest_line_date(self):
		so = to_sales_order(order(), context())["sales_order"]

		self.assertEqual(so["delivery_date"], "2026-11-11")
		self.assertEqual([item["delivery_date"] for item in so["items"]], ["2026-11-11"] * 2)
		self.assertEqual(
			[item["bol_latest_delivery_date"] for item in so["items"]], ["2026-11-12", "2026-11-11"]
		)

	def test_exact_delivery_date_when_there_is_no_latest(self):
		result = order()
		for item in result["orderItems"]:
			item["fulfilment"].update(latestDeliveryDate=None, exactDeliveryDate="2026-11-13")

		so = to_sales_order(result, context())["sales_order"]

		self.assertEqual(so["delivery_date"], "2026-11-13")
		self.assertEqual(so["items"][0]["bol_latest_delivery_date"], "2026-11-13")

	def test_delivery_date_is_never_before_the_order_date(self):
		result = order()
		for item in result["orderItems"]:
			item["fulfilment"]["latestDeliveryDate"] = "2026-11-01"

		so = to_sales_order(result, context())["sales_order"]

		self.assertEqual(so["delivery_date"], "2026-11-09")
		self.assertEqual(so["items"][0]["bol_latest_delivery_date"], "2026-11-01")

	def test_without_dates_delivery_is_the_day_after_the_order(self):
		result = order()
		for item in result["orderItems"]:
			item["fulfilment"] = {"method": "FBR"}

		so = to_sales_order(result, context())["sales_order"]

		self.assertEqual(so["delivery_date"], "2026-11-10")
		self.assertEqual([item["delivery_date"] for item in so["items"]], ["2026-11-10"] * 2)

	def test_netherlands_gets_the_nl_template(self):
		self.assertEqual(
			to_sales_order(order(), context())["sales_order"]["taxes_and_charges"], "VAT NL incl"
		)

	def test_belgium_gets_the_be_template(self):
		result = order()
		result["shipmentDetails"].update(countryCode="BE", zipCode="2000", city="Antwerpen")
		result["billingDetails"] = None

		mapped = to_sales_order(result, context())

		self.assertEqual(mapped["sales_order"]["taxes_and_charges"], "VAT BE incl")
		self.assertEqual(mapped["shipping_address"]["country"], "Belgium")

	def test_other_country_is_unsupported(self):
		result = order()
		result["shipmentDetails"]["countryCode"] = "DE"

		with self.assertRaises(BolMappingError) as ctx:
			to_sales_order(result, context())
		self.assertEqual(ctx.exception.reason, "country")


class TestQuantities(unittest.TestCase):
	def test_open_quantity(self):
		self.assertEqual(open_quantity({"quantity": 5, "quantityCancelled": 1, "quantityShipped": 2}), 2)

	def test_partly_cancelled_and_shipped_lines_book_what_is_open(self):
		result = line(0, quantity=5, quantityCancelled=1, quantityShipped=2, totalPrice=124.75)

		shirt = to_sales_order(result, context())["sales_order"]["items"][0]

		self.assertEqual(shirt["qty"], 2)

	def test_closed_line_is_skipped(self):
		result = line(0, quantityCancelled=2)

		items = to_sales_order(result, context())["sales_order"]["items"]

		self.assertEqual([item["item_code"] for item in items], [BAG])

	def test_all_lines_closed_gives_none(self):
		result = line(0, quantityShipped=2)
		result["orderItems"][1]["quantityCancelled"] = 1

		self.assertIsNone(to_sales_order(result, context()))

	def test_closed_order_from_an_unsupported_country_is_skipped_not_an_error(self):
		result = line(0, quantityShipped=2)
		result["orderItems"][1]["quantityCancelled"] = 1
		result["shipmentDetails"]["countryCode"] = "DE"

		self.assertIsNone(to_sales_order(result, context()))

	def test_fbb_line_is_skipped(self):
		result = order()
		result["orderItems"][0]["fulfilment"]["method"] = "FBB"

		items = to_sales_order(result, context())["sales_order"]["items"]

		self.assertEqual([item["item_code"] for item in items], [BAG])


class TestItems(unittest.TestCase):
	def test_unknown_ean_is_an_error(self):
		with self.assertRaises(BolMappingError) as ctx:
			to_sales_order(order(), context(items={SHIRT_EAN: [SHIRT]}))
		self.assertEqual(ctx.exception.reason, "unknown_ean")
		self.assertIn(BAG_EAN, str(ctx.exception))

	def test_ean_on_several_items_is_an_error(self):
		with self.assertRaises(BolMappingError) as ctx:
			to_sales_order(order(), context(items={SHIRT_EAN: [SHIRT, "other"], BAG_EAN: [BAG]}))
		self.assertEqual(ctx.exception.reason, "ambiguous_ean")

	def test_reference_mismatch_trusts_the_ean_and_warns(self):
		result = order()
		result["orderItems"][0]["offer"]["reference"] = "SOMETHING-ELSE"

		mapped = to_sales_order(result, context())

		self.assertEqual(mapped["sales_order"]["items"][0]["item_code"], SHIRT)
		self.assertEqual(
			mapped["warnings"],
			[
				{
					"reason": "reference_mismatch",
					"order_item_id": "2012345678",
					"reference": "SOMETHING-ELSE",
					"item_code": SHIRT,
					"ean": SHIRT_EAN,
				}
			],
		)

	def test_missing_reference_is_fine(self):
		result = order()
		result["orderItems"][0]["offer"] = None

		self.assertEqual(to_sales_order(result, context())["warnings"], [])

	def test_reference_cut_to_bols_limit_matches(self):
		long_code = "X" * 120
		result = order()
		result["orderItems"][0]["offer"]["reference"] = long_code[:100]

		mapped = to_sales_order(result, context(items={SHIRT_EAN: [long_code], BAG_EAN: [BAG]}))

		self.assertEqual(mapped["warnings"], [])


class TestDiscounts(unittest.TestCase):
	def test_line_discount_becomes_a_per_unit_discount(self):
		result = line(0, discounts=[{"title": "2 for 45", "amount": 4.9}], totalPrice=45.0)

		mapped = to_sales_order(result, context())
		shirt = mapped["sales_order"]["items"][0]

		self.assertEqual(
			(shirt["price_list_rate"], shirt["discount_amount"], shirt["rate"]), (24.95, 2.45, 22.5)
		)
		self.assertEqual(mapped["warnings"], [])

	def test_discount_that_does_not_split_into_cents_warns(self):
		# The spec's example: 10 × 12.99 − 10.99 = 118.91, which is not 10 × a whole-cent rate.
		result = line(
			0, quantity=10, unitPrice=12.99, discounts=[{"title": "x", "amount": 10.99}], totalPrice=118.91
		)

		mapped = to_sales_order(result, context())
		shirt = mapped["sales_order"]["items"][0]

		self.assertEqual((shirt["discount_amount"], shirt["rate"]), (1.1, 11.89))
		self.assertEqual(
			mapped["warnings"],
			[{"reason": "rounding", "order_item_id": "2012345678", "expected": 118.91, "actual": 118.9}],
		)

	def test_money_rounds_half_up(self):
		self.assertEqual(money(0.125), 0.13)
		self.assertEqual(money(None), 0.0)


class TestAddresses(unittest.TestCase):
	def test_nl_address_with_house_number_extension(self):
		address = to_sales_order(order(), context())["shipping_address"]

		self.assertEqual(
			address,
			{
				"doctype": "Address",
				"address_title": "Billie Jansen",
				"address_type": "Shipping",
				"address_line1": "Dorpstraat 1 B",
				"address_line2": "Apartment",
				"pincode": "1111ZZ",
				"city": "Utrecht",
				"country": "Netherlands",
				"email_id": "billie@verkopen.bol.com",
				"phone": "012123456",
				"bol_buyer": 1,
				"links": [{"link_doctype": "Customer", "link_name": "bol.com"}],
			},
		)

	def test_address_without_extension_or_extra_information(self):
		result = order()
		result["shipmentDetails"].update(houseNumberExtension=None, extraAddressInformation=None)

		address = to_sales_order(result, context())["shipping_address"]

		self.assertEqual(address["address_line1"], "Dorpstraat 1")
		self.assertIsNone(address["address_line2"])

	def test_company_buyer_is_the_title(self):
		result = order()
		result["shipmentDetails"]["company"] = "Borduurwerk B.V."

		self.assertEqual(
			to_sales_order(result, context())["shipping_address"]["address_title"], "Borduurwerk B.V."
		)

	def test_pick_up_point_name_comes_first_on_line_two(self):
		result = order(pickupPoint=True)
		result["shipmentDetails"]["pickupPointName"] = "Albert Heijn: UTRECHT"

		address = to_sales_order(result, context())["shipping_address"]

		self.assertEqual(address["address_line2"], "Albert Heijn: UTRECHT, Apartment")

	def test_same_billing_address_gives_no_separate_address(self):
		self.assertIsNone(to_sales_order(order(), context())["billing_address"])

	def test_missing_billing_details_give_no_separate_address(self):
		self.assertIsNone(to_sales_order(order(billingDetails=None), context())["billing_address"])

	def test_different_billing_address_is_separate(self):
		result = order()
		result["billingDetails"].update(
			streetName="Kerkstraat",
			houseNumber="12",
			houseNumberExtension=None,
			city="Amsterdam",
			zipCode="1017GA",
		)

		mapped = to_sales_order(result, context())

		billing = mapped["billing_address"]
		self.assertEqual(billing["address_type"], "Billing")
		self.assertEqual(billing["address_line1"], "Kerkstraat 12")
		self.assertEqual((billing["city"], billing["pincode"]), ("Amsterdam", "1017GA"))
		self.assertIsNone(billing["phone"])
		self.assertEqual(mapped["shipping_address"]["address_line1"], "Dorpstraat 1 B")

	def test_billing_in_an_unknown_country_is_an_error(self):
		result = order()
		result["billingDetails"]["countryCode"] = "XX"

		with self.assertRaises(BolMappingError) as ctx:
			to_sales_order(result, context())
		self.assertEqual(ctx.exception.reason, "country")


class TestBuyer(unittest.TestCase):
	def _buyer(self, result: dict) -> dict:
		so = to_sales_order(result, context())["sales_order"]
		return {key: value for key, value in so.items() if key.startswith("bol_buyer_")}

	def test_consumer(self):
		self.assertEqual(
			self._buyer(order()),
			{
				"bol_buyer_name": "Billie Jansen",
				"bol_buyer_company": None,
				"bol_buyer_vat_number": None,
				"bol_buyer_kvk_number": None,
			},
		)

	def test_business_buyer_with_vat_and_kvk_numbers(self):
		result = order()
		result["billingDetails"].update(
			company="Borduurwerk B.V.", vatNumber="NL999999999B99", kvkNumber="99887766"
		)

		self.assertEqual(
			self._buyer(result),
			{
				"bol_buyer_name": "Billie Jansen",
				"bol_buyer_company": "Borduurwerk B.V.",
				"bol_buyer_vat_number": "NL999999999B99",
				"bol_buyer_kvk_number": "99887766",
			},
		)

	def test_the_billing_party_is_the_buyer_not_the_recipient(self):
		# A gift: shipped to someone else, paid for by the billing party.
		result = order()
		result["shipmentDetails"].update(firstName="Sam", surname="de Vries")

		self.assertEqual(self._buyer(result)["bol_buyer_name"], "Billie Jansen")

	def test_without_billing_details_the_recipient_is_the_buyer(self):
		result = order(billingDetails=None)
		result["shipmentDetails"].update(firstName="Sam", surname="de Vries", company="Atelier Sam")

		buyer = self._buyer(result)

		self.assertEqual(
			(buyer["bol_buyer_name"], buyer["bol_buyer_company"]), ("Sam de Vries", "Atelier Sam")
		)
		self.assertIsNone(buyer["bol_buyer_vat_number"])


class TestTimes(unittest.TestCase):
	def test_winter_offset(self):
		self.assertEqual(to_system_datetime("2026-11-09T23:30:00+01:00", TZ), datetime(2026, 11, 9, 23, 30))

	def test_summer_offset(self):
		self.assertEqual(to_system_datetime("2026-07-01T10:00:00+02:00", TZ), datetime(2026, 7, 1, 10, 0))

	def test_utc_is_converted(self):
		self.assertEqual(to_system_datetime("2026-11-09T23:30:00Z", TZ), datetime(2026, 11, 10, 0, 30))

	def test_other_system_time_zone(self):
		self.assertEqual(
			to_system_datetime("2026-11-09T23:30:00+01:00", "Europe/London"), datetime(2026, 11, 9, 22, 30)
		)

	def test_order_date_follows_the_system_time_zone(self):
		# 23:30 in Amsterdam is still the 9th; in UTC+2 it's already the 10th.
		so = to_sales_order(order(), context(timezone="Europe/Helsinki"))["sales_order"]

		self.assertEqual(so["transaction_date"], "2026-11-10")

	def test_order_without_a_date_uses_now(self):
		so = to_sales_order(order(orderPlacedDateTime=None), context())["sales_order"]

		self.assertEqual(so["transaction_date"], "2026-11-10")
