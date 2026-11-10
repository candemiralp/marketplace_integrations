# bol timestamps -> ERPNext datetimes. Pure: no frappe imports. The one place bol times are converted.

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def to_system_datetime(value: str, timezone: str) -> datetime:
	"""bol's ISO-8601 timestamp (with offset) as a naive datetime in `timezone` (the system time zone).

	ERPNext stores naive datetimes in the system time zone, so this is what can be compared and saved.
	"""
	moment = datetime.fromisoformat(value)
	if moment.tzinfo is None:
		# bol always sends an offset; a bare timestamp is read as UTC rather than as local time.
		moment = moment.replace(tzinfo=UTC)
	return moment.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
