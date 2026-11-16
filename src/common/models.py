"""Hand-written checks for the medicine JSON. No pydantic.

Unknown choices and out-of-range numbers become null. That is deliberate:
clamping a dose or a duration would invent a value the photo did not state.
Fields the model adds, including a patient name, are discarded.
"""

import re
import unicodedata
from datetime import datetime

MAX_MEDICATIONS = 10
MAX_WARNINGS = 20

_FORMS = {"tablet", "capsule", "syrup", "injection", "cream", "drops", "inhaler", "other"}
_DOSE_UNITS = {"tablet", "capsule", "ml", "mg", "puff", "drop", "unit", "other"}
_ROUTES = {"oral", "topical", "inhaled", "injection", "eye", "ear", "other"}
_TIMING = {"morning", "noon", "evening", "bedtime"}
_WITH_FOOD = {"before", "with", "after"}
_CONFIDENCE = {"high", "medium", "low"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_extracted(raw) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    warnings = _warnings(raw.get("warnings"))
    medications = []
    dropped = 0
    incoming = raw.get("medications")
    if not isinstance(incoming, list):
        incoming = []
        if raw.get("medications") not in (None, []):
            warnings.append("Medicines could not be read.")
    for item in incoming:
        if len(medications) >= MAX_MEDICATIONS:
            warnings.append("Only the first 10 medicines were kept.")
            break
        medication = _medication(item)
        if medication is None:
            dropped += 1
            continue
        medications.append(medication)
    if dropped:
        warnings.append("A medicine without a name was dropped.")
    return {
        "is_prescription": raw.get("is_prescription") is True,
        "prescriber_or_facility": _text(raw.get("prescriber_or_facility"), 200),
        "prescription_date": _date(raw.get("prescription_date")),
        "medications": medications,
        "warnings": warnings[:MAX_WARNINGS],
    }


def _medication(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"), 100)
    if not name:
        return None
    confidence = _choice(raw.get("confidence"), _CONFIDENCE)
    return {
        "name": name,
        "strength": _text(raw.get("strength"), 100),
        "form": _choice(raw.get("form"), _FORMS),
        "dose_amount": _number(raw.get("dose_amount")),
        "dose_unit": _choice(raw.get("dose_unit"), _DOSE_UNITS),
        "route": _choice(raw.get("route"), _ROUTES),
        "times_per_day": _bounded_int(raw.get("times_per_day"), 1, 6),
        "interval_hours": _bounded_int(raw.get("interval_hours"), 1, 24),
        "timing_hint": _choice(raw.get("timing_hint"), _TIMING),
        "with_food": _choice(raw.get("with_food"), _WITH_FOOD),
        "duration_days": _bounded_int(raw.get("duration_days"), 1, 365),
        "as_needed": raw.get("as_needed") is True,
        "instructions": _text(raw.get("instructions"), 200),
        "confidence": confidence or "low",
        "source_text": _text(raw.get("source_text"), 500),
    }


def _warnings(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw:
        text = _text(item, 200)
        if text:
            cleaned.append(text)
    return cleaned


def _choice(value, allowed: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in allowed:
        return lowered
    return None


def _bounded_int(value, low: int, high: int) -> int | None:
    number = _whole(value)
    if number is None or number < low or number > high:
        return None
    return number


def _whole(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _number(value) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number <= 0 or number > 10000:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 3)


def _date(value) -> str | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def normalize_confirmed_medications(raw) -> list[dict]:
    """User-edited medicines. A missing name is rejected rather than dropped."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MEDICATIONS:
        raise ValueError("Send between 1 and 10 medicines")
    medications = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each medicine must be an object")
        medication = _medication(item)
        if medication is None:
            raise ValueError("Every medicine needs a name")
        medication.pop("source_text", None)
        if item.get("slots") is None:
            medication["slots"] = None
        else:
            medication["slots"] = parse_slots(item.get("slots"))
        medications.append(medication)
    return medications


def parse_slots(raw) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise ValueError("Use between 1 and 6 times a day")
    cleaned = []
    for item in raw:
        if not isinstance(item, str) or not _HHMM.fullmatch(item):
            raise ValueError("Times must look like 08:00")
        cleaned.append(item)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("Each time must be unique")
    return sorted(cleaned)


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    # Control characters become spaces so a newline cannot glue words together
    # or break a later email subject, SMS body, or calendar line.
    chars = []
    for char in value:
        if unicodedata.category(char) in {"Cc", "Cf"}:
            chars.append(" ")
        else:
            chars.append(char)
    cleaned = re.sub(r" {2,}", " ", "".join(chars)).strip()
    if not cleaned:
        return None
    return cleaned[:limit]
