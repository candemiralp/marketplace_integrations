# bol offer stock (v11) <-> ERPNext quantities. Pure: no frappe imports.

import math

# Offers v11 documents no maximum; the v10 stock requests cap the amount at 999 (ADR-009).
BOL_MAX_STOCK = 999


def available_to_sell(actual_qty, reserved_qty, reserved_stock, buffer) -> int:
	"""What bol may sell: stock on hand minus open Sales Orders, stock reservations and the buffer.

	Incoming stock (projected qty) doesn't count: it can't be shipped yet. Missing values count as 0.
	"""
	free = (actual_qty or 0) - (reserved_qty or 0) - (reserved_stock or 0) - (buffer or 0)
	# Bin quantities are floats: 2.9999999 must stay 3, not drop a unit.
	return min(max(math.floor(round(free, 6)), 0), BOL_MAX_STOCK)


def stock_body(amount: int) -> dict:
	# managedByRetailer false: bol also subtracts its own open orders until the order import books them (ADR-009).
	return {"amount": amount, "managedByRetailer": False}


def stock_matches(offer: dict, amount: int) -> bool:
	stock = offer.get("stock") or {}
	return stock.get("amount") == amount and stock.get("managedByRetailer") is False
