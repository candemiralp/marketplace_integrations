# EAN / GTIN rules. Pure: no frappe imports. Codes are strings: leading zeros are significant.

from marketplace_integrations.bol.api.errors import BolMappingError

EAN_BARCODE_TYPE = "EAN"
GTIN_LENGTHS = (12, 13, 14)


def is_valid_gtin13(code) -> bool:
	"""13 digits whose last digit is the GS1 check digit."""
	if not isinstance(code, str) or len(code) != 13 or not code.isascii() or not code.isdigit():
		return False
	return int(code[-1]) == _check_digit(code[:-1])


def get_item_ean(item) -> str:
	"""The Item's one EAN barcode (`barcode_type == "EAN"`), as a valid GTIN-13.

	Raises BolMappingError with `reason` "missing", "several" or "invalid" otherwise.
	"""
	eans = [row.barcode for row in item.barcodes or [] if row.barcode_type == EAN_BARCODE_TYPE]
	if not eans:
		raise BolMappingError(f"Item {item.name} has no EAN barcode", reason="missing")
	if len(eans) > 1:
		raise BolMappingError(f"Item {item.name} has several EAN barcodes: {', '.join(eans)}", reason="several")
	if not is_valid_gtin13(eans[0]):
		raise BolMappingError(f"EAN {eans[0]} of item {item.name} is not a valid GTIN-13", reason="invalid")
	return eans[0]


def gtin_forms(code: str) -> list[str]:
	"""`code` plus the zero-padded forms of the same GTIN (UPC-A 12, EAN-13, GTIN-14 digits).

	GS1 treats a GTIN-12 and the GTIN-13 with one more leading zero as the same number, so a
	UPC-A barcode in ERPNext matches bol's EAN-13.
	"""
	significant = code.lstrip("0")
	forms = {code} | {significant.zfill(length) for length in GTIN_LENGTHS if len(significant) <= length}
	return sorted(forms)


def _check_digit(digits: str) -> int:
	# Weights 3, 1, 3, … from the rightmost data digit.
	total = sum(int(digit) * (3 if i % 2 == 0 else 1) for i, digit in enumerate(reversed(digits)))
	return (10 - total % 10) % 10
