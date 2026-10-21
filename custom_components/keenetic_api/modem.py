"""Read-only cellular modem data from the existing show/interface snapshot."""

from collections.abc import Mapping
import math

MODEM_TYPES = {"Mobile", "UsbModem", "UsbLte", "UsbQmi", "UsbMbim", "MwsMobile"}
MODEM_FIELDS = {
    "operator": ("operator", "spn"),
    "network": ("mobile",),
    "sim": ("sim",),
    "rssi": ("rssi",),
    "rsrp": ("rsrp",),
    "rsrq": ("rsrq",),
    "sinr": ("cinr", "sinr"),
    "band": ("band",),
    "temperature": ("temperature",),
    "roaming": ("roaming",),
}
STAT_FIELDS = {"rxbytes", "txbytes", "rxspeed", "txspeed"}
NUMERIC_FIELDS = {"rssi", "rsrp", "rsrq", "sinr", "temperature"} | STAT_FIELDS


def is_modem(interface):
    """Recognize physical modems and Wi-Fi-system mobile interfaces."""
    if not isinstance(interface, Mapping):
        return False
    traits = interface.get("traits", ())
    return interface.get("type") in MODEM_TYPES or (
        isinstance(traits, (list, tuple)) and "Mobile" in traits
    )


def modem_interfaces(interfaces):
    return {key: row for key, row in interfaces.items() if is_modem(row)}


def number(value):
    """Keep real zeroes; reject empty, boolean, malformed and non-finite values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def text(value):
    if value is None or isinstance(value, (bool, Mapping, list, tuple)):
        return None
    value = str(value).strip()
    return value if value and value.lower() not in {"unknown", "none", "n/a", "--", "-"} else None


def boolean(value):
    if value is True or value in ("yes", "true", "on", 1):
        return True
    if value is False or value in ("no", "false", "off", 0):
        return False
    return None


def modem_value(interface, stat, metric):
    """Normalize API values without inferring metrics absent on this modem."""
    if metric == "info":
        ati = interface.get("ati")
        ati = ati if isinstance(ati, Mapping) else {}
        return text(interface.get("product")) or text(ati.get("model")) or text(interface.get("model"))
    if metric == "connected":
        if boolean(interface.get("plugged")) is False:
            return False
        return boolean(interface.get("connected"))
    if metric in {"rxbytes", "txbytes"}:
        raw = stat.get(metric)
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, int) or (isinstance(raw, str) and raw.isdecimal()):
            return int(raw) if int(raw) >= 0 else None
        value = number(raw)
        return int(value) if value is not None and value >= 0 and value.is_integer() else None
    if metric in STAT_FIELDS:
        value = number(stat.get(metric))
        if value is None or value < 0:
            return None
        # Keenetic rxspeed/txspeed are bits/s. Traffic counters are bytes.
        return value / 1_000_000 if metric.endswith("speed") else int(value)
    for source in MODEM_FIELDS.get(metric, ()):
        raw = interface.get(source)
        value = number(raw) if metric in NUMERIC_FIELDS else boolean(raw) if metric == "roaming" else text(raw)
        if value is not None:
            return value
    return None


def modem_attributes(interface, interface_id):
    """Explicit allowlist: no SIM/account identifiers or phone numbers."""
    result = {"interface_id": interface_id, "interface_type": interface.get("type"),
              "remote": interface.get("type") == "MwsMobile"}
    for key in ("manufacturer", "model", "fw", "embedded", "connection-state", "signal-level"):
        if key in interface and interface[key] is not None:
            result[{"fw": "firmware", "connection-state": "connection_state", "signal-level": "signal_level"}.get(key, key)] = interface[key]
    return result


def discover_modem_metrics(interfaces, stats, seen, metrics, legacy_interfaces=()):
    """Discover newly attached modems and newly available optional metrics."""
    result = []
    for interface_id, row in modem_interfaces(interfaces).items():
        stat = stats.get(interface_id, {})
        for metric in metrics:
            if interface_id in legacy_interfaces and metric in STAT_FIELDS | {"connected"}:
                continue
            identity = (interface_id, metric)
            supported = metric in {"info", "connected"} or (
                metric in STAT_FIELDS and metric in stat
            ) or any(key in row for key in MODEM_FIELDS.get(metric, ()))
            if supported and identity not in seen:
                seen.add(identity)
                result.append(identity)
    return result


def duplicate_modem_entities(unique_id, interfaces, legacy_interfaces):
    """Exact b4 identities superseded by pre-existing interface entities."""
    for interface_id in modem_interfaces(interfaces):
        if interface_id not in legacy_interfaces:
            continue
        for metric in sorted(STAT_FIELDS | {"connected"}):
            domain = "binary_sensor" if metric == "connected" else "sensor"
            old_key = "connected_to_interface" if metric == "connected" else metric
            yield (domain, f"{unique_id}_{old_key}_{interface_id}",
                   f"{unique_id}_modem_{metric}_{interface_id}")
