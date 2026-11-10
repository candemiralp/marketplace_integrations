# Exception taxonomy for bol calls (overview §4.1).
# str(error) is a short human message; `details` holds the (redacted) call data for logging and
# uses log_call's keyword names, so callers can do `log_call(**err.details, error=err)`.


class BolError(Exception):
	def __init__(self, message: str, details: dict | None = None, *, ambiguous: bool = False):
		super().__init__(message)
		self.details = details or {}
		# True when a POST may have been processed by bol: never retry blindly, check bol's state first.
		self.ambiguous = ambiguous


class BolTransientError(BolError):
	"""Connect error, timeout, 502/503/504. Retryable."""


class BolRateLimitError(BolError):
	"""429. Retryable after `retry_after` seconds."""

	def __init__(self, message: str, details: dict | None = None, *, retry_after: int = 1):
		super().__init__(message, details)
		self.retry_after = retry_after


class BolAuthError(BolError):
	"""Token failure, 401 after one refresh, 403. Not retryable."""


class BolValidationError(BolError):
	"""400/422. Not retryable: the data needs fixing."""

	def __init__(
		self,
		title: str | None,
		detail: str | None,
		violations: list[dict] | None = None,
		details: dict | None = None,
	):
		super().__init__(
			": ".join(part for part in (title, detail) if part) or "Request rejected by bol", details
		)
		self.title = title
		self.detail = detail
		self.violations = violations or []


class BolNotFoundError(BolError):
	"""404. The caller decides what that means."""


class BolServerError(BolError):
	"""Other 5xx or an unparseable response. Retryable (limited)."""


class BolMappingError(BolError):
	"""Our data can't be mapped to bol (unknown EAN, missing price, …). Not retryable.

	`reason` is a short code for the case, so a caller can show its own translated message
	(mappers are pure and can't translate).
	"""

	def __init__(self, message: str, details: dict | None = None, *, reason: str | None = None):
		super().__init__(message, details)
		self.reason = reason
