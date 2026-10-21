"""Keenetic SMS protocol and complete-message tracking (no logging of content)."""
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
import re
from urllib.parse import urlencode


class SmsError(Exception):
    """An SMS request failed or its result is unknown."""


def _count(value):
    if isinstance(value, bool):
        raise SmsError("Invalid SMS part count")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise SmsError("Invalid SMS part count") from None
    if str(result) != str(value) or result < 1:
        raise SmsError("Invalid SMS part count")
    return result


def normalize_messages(payload):
    """sms list already groups concatenated SMS; never expose partial text."""
    if not isinstance(payload, Mapping):
        raise SmsError("SMS list is unavailable")
    if "sms" in payload:
        group = payload["sms"]
        if not isinstance(group, Mapping):
            raise SmsError("SMS list is unavailable")
        payload = group.get("list", {})
    if not isinstance(payload, Mapping):
        raise SmsError("SMS list is unavailable")
    rows = payload.get("messages")
    if rows is None:
        if payload.get("messages-count") == 0:
            return []
        raise SmsError("SMS list is unavailable")
    if isinstance(rows, Mapping):
        if not all(isinstance(row, Mapping) for row in rows.values()):
            raise SmsError("Invalid SMS list")
        rows = [dict(row, id=key) for key, row in rows.items()]
    if not isinstance(rows, list):
        raise SmsError("Invalid SMS list")
    result = []
    ids = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise SmsError("Invalid SMS identifier")
        ids.add(row["id"])
        # Unknown part metadata must not accidentally publish a partial body.
        try:
            parts = _count(row.get("parts"))
            total = _count(row.get("total-parts", row.get("total_parts")))
        except SmsError:
            parts, total = None, None
        complete = parts is not None and parts == total and isinstance(row.get("text"), str)
        read = row.get("read")
        read = True if read in (True, "yes", "true") else False if read in (False, "no", "false") else None
        result.append({"id": row["id"], "from": row.get("from") if isinstance(row.get("from"), str) else None,
                       "timestamp": row.get("timestamp") if isinstance(row.get("timestamp"), str) else None,
                       "read": read, "parts": parts, "total_parts": total,
                       "complete": complete, "text": row["text"] if complete else None})
    return result


def timestamp_key(row):
    value = row.get("timestamp") or ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return float("-inf")
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


class SmsTracker:
    """Suppress startup history and emit a new concatenated message only once."""
    def __init__(self):
        self.initialized = False
        self.seen = OrderedDict()

    def update(self, messages):
        events = []
        for row in messages:
            key = (row["id"], row["from"], row["timestamp"])
            prior = self.seen.get(key)
            if self.initialized and row["complete"] and prior not in ("baseline", "complete"):
                events.append(row)
            self.seen[key] = "baseline" if not self.initialized or prior == "baseline" else "complete" if row["complete"] else "partial"
            self.seen.move_to_end(key)
        self.initialized = True
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)
        return sorted(events, key=timestamp_key)


def validate_recipient(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9]{3,15}", value):
        raise SmsError("Specify a phone number containing 3–15 digits, optionally starting with +")
    return value


def check_command_response(payload):
    """Reject HTTP objects, malformed replies and nested CLI errors."""
    if not isinstance(payload, (dict, list)) or not payload:
        raise SmsError("SMS command result is unknown; it was not retried")
    accepted = False
    def walk(value):
        nonlocal accepted
        if isinstance(value, dict):
            if value.get("status") in ("error", "critical", "warning") or value.get("error"):
                raise SmsError("Router rejected the SMS command")
            if value.get("status") in ("message", "success", "ok") or value.get("success") is True:
                accepted = True
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    if not accepted:
        raise SmsError("SMS command result is unknown; it was not retried")


class SmsGateway:
    def __init__(self, router):
        self.router = router

    async def list_messages(self, interface):
        payload = await self.router.api("GET", "/rci/sms/list?" + urlencode({"interface": interface}))
        return normalize_messages(payload)

    async def mark_read(self, interface, sms_id):
        if not isinstance(sms_id, str) or not sms_id or len(sms_id) > 128:
            raise SmsError("Specify a valid sms_id")
        result = await self.router.api("POST", "/rci/sms/read", {"interface": interface, "id": sms_id})
        check_command_response(result)

    async def send(self, interface, target, message):
        target = validate_recipient(target)
        if not isinstance(message, str) or not message.strip() or len(message) > 10000:
            raise SmsError("Specify a non-empty SMS message up to 10000 characters")
        try:
            result = await self.router.api("POST", "/rci/sms/send", {"interface": interface, "to": target, "message": message})
            check_command_response(result)
        except SmsError:
            raise
        except Exception:
            # A timeout may occur after the modem accepted the SMS. Never replay it.
            raise SmsError("SMS send result is unknown; it was not retried") from None
