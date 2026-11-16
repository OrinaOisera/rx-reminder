"""Dose times shared by the preview, the scheduler, and later the calendar file.

The list is fixed by the medicine and the settings. `now` does not remove
past doses, so the hash stays the same between preview and confirm.
Five and six doses a day are spread between wake and sleep. A fixed 23:00
slot would fall after a normal bedtime.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common.http import RequestError
from common.models import normalize_confirmed_medications
from common.palette import get_color
from common.patients import strip_controls

logger = logging.getLogger(__name__)

MAX_DOSES = 500
HASH_MISMATCH = "The schedule changed, please review it again"
_TABLE = {
    1: ("08:00",),
    2: ("08:00", "20:00"),
    3: ("08:00", "14:00", "20:00"),
    4: ("08:00", "12:00", "16:00", "20:00"),
}


def generate_dose_times(med, settings, now) -> list[datetime]:
    """Timezone-aware local datetimes, sorted. `now` is unused on purpose."""
    del now
    if med.get("as_needed"):
        return []
    slots = daily_slots(med, settings)
    if not slots or not med.get("duration_days"):
        return []
    zone = settings["zone"]
    start = settings["start"]
    end = _midnight(start) + timedelta(days=int(med["duration_days"]))
    doses = []
    day = start.date()
    last_day = (end - timedelta(seconds=1)).date()
    while day <= last_day:
        for minute in slots:
            local = datetime(
                day.year,
                day.month,
                day.day,
                minute // 60,
                minute % 60,
                tzinfo=zone,
            )
            if start <= local < end:
                doses.append(local)
        day += timedelta(days=1)
    doses.sort()
    return doses


def daily_slots(med, settings) -> list[int]:
    """Minutes from midnight. User slots win. As-needed medicines have none."""
    if med.get("as_needed"):
        return []
    wake = settings["wake"]
    sleep = settings["sleep"]
    chosen = med.get("slots")
    if chosen:
        return [_minutes(slot) for slot in chosen]
    if med.get("interval_hours"):
        return _interval_slots(wake, sleep, int(med["interval_hours"]), med["name"])
    per_day = med.get("times_per_day")
    hint = med.get("timing_hint")
    if hint and per_day in (None, 1):
        return [_hint_minute(hint, wake, sleep)]
    if per_day is None:
        raise RequestError(400, f"Enter how often to take {med['name']}")
    if per_day >= 5:
        return _spread(wake, sleep, per_day)
    clamped = []
    for slot in _TABLE[per_day]:
        minute = min(sleep, max(wake, _minutes(slot)))
        if minute not in clamped:
            clamped.append(minute)
    return clamped


def build_preview(prescription, patient, medications, settings, now) -> dict:
    """Pure calculation. No AWS calls and no writes."""
    try:
        medications = normalize_confirmed_medications(medications)
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    color = get_color(patient["colorKey"]) or {}
    notes = []
    warnings = []
    per_medication = []
    rows = []
    partial = False
    long_course = False
    prn_count = 0
    for med_idx, med in enumerate(medications):
        if med.get("as_needed"):
            prn_count += 1
            per_medication.append(_med_summary(med_idx, med, [], []))
            continue
        if not med.get("duration_days"):
            raise RequestError(400, f"Enter how many days {med['name']} should be taken")
        if int(med["duration_days"]) > 30:
            long_course = True
        if med.get("slots"):
            for slot in med["slots"]:
                minute = _minutes(slot)
                if minute < settings["wake"] or minute > settings["sleep"]:
                    warnings.append(f"{slot} for {med['name']} is outside wake and sleep times")
        try:
            slot_minutes = daily_slots(med, settings)
        except RequestError:
            raise
        if _first_day_drops(settings, slot_minutes):
            partial = True
        doses = generate_dose_times(med, settings, now)
        per_medication.append(_med_summary(med_idx, med, _hhmm_list(slot_minutes), doses))
        for dose in doses:
            rows.append(
                {
                    "medIdx": med_idx,
                    "name": med["name"],
                    "doseText": dose_text(med),
                    "at": _utc_iso(dose),
                    "local": dose,
                }
            )
    if len(rows) > MAX_DOSES:
        raise RequestError(
            400,
            f"That schedule has {len(rows)} reminders. The maximum is {MAX_DOSES}. "
            "Shorten the course or use fewer daily times.",
        )
    if prn_count:
        notes.append(f"As-needed medicines ({prn_count}) were not scheduled.")
        warnings.append("As-needed medicines were skipped.")
    if partial:
        warnings.append("The first day only includes doses at or after the start time.")
    if long_course:
        warnings.append("This course is longer than 30 days.")
    warnings.extend(_crowded(rows))
    patient_name = strip_controls(patient["name"]).strip()
    preview = {
        "patient": {
            "name": patient_name,
            "colorKey": patient["colorKey"],
            "hex": color.get("hex"),
            "emoji": color.get("emoji"),
        },
        "perMedication": per_medication,
        "days": _days(rows),
        "totalDoses": len(rows),
        "notes": notes,
        "warnings": _unique(warnings)[:20],
        "scheduleHash": schedule_hash(prescription["patientId"], settings["timezone"], rows),
    }
    return preview


def schedule_hash(patient_id: str, timezone_name: str, rows: list[dict]) -> str:
    doses = [
        {
            "at": row["at"],
            "doseText": row["doseText"],
            "medIdx": row["medIdx"],
            "name": row["name"],
        }
        for row in rows
    ]
    doses.sort(key=lambda item: (item["at"], item["medIdx"], item["name"], item["doseText"]))
    payload = {"doses": doses, "patientId": patient_id, "timezone": timezone_name}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_same_hash(submitted, actual: str) -> None:
    if not isinstance(submitted, str) or not hmac.compare_digest(submitted, actual):
        raise RequestError(409, HASH_MISMATCH)


def parse_settings(raw, now: datetime, *, with_channel: bool) -> dict:
    if not isinstance(raw, dict):
        raise RequestError(400, "Settings are required")
    timezone_name = raw.get("timezone") or "Africa/Nairobi"
    if not isinstance(timezone_name, str):
        raise RequestError(400, "Choose a valid timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise RequestError(400, "Choose a valid timezone") from exc
    start = _parse_start(raw.get("startDateTime"), zone)
    if start < now.astimezone(zone) - timedelta(days=1):
        raise RequestError(400, "The start time cannot be more than a day ago")
    wake_text = raw.get("wakeTime") or "07:00"
    sleep_text = raw.get("sleepTime") or "22:00"
    wake = _clock(wake_text, "Wake time")
    sleep = _clock(sleep_text, "Sleep time")
    if sleep <= wake:
        raise RequestError(400, "Sleep time must be after wake time")
    settings = {
        "timezone": timezone_name,
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "start": start,
        "wakeTime": wake_text if isinstance(wake_text, str) else "07:00",
        "sleepTime": sleep_text if isinstance(sleep_text, str) else "22:00",
        "wake": wake,
        "sleep": sleep,
        "zone": zone,
    }
    if isinstance(wake_text, str) and len(wake_text) == 5:
        settings["wakeTime"] = wake_text
    else:
        settings["wakeTime"] = _hhmm(wake)
    if isinstance(sleep_text, str) and len(sleep_text) == 5:
        settings["sleepTime"] = sleep_text
    else:
        settings["sleepTime"] = _hhmm(sleep)
    if with_channel:
        channel, destination = _channel(raw)
        settings["channel"] = channel
        settings["destination"] = destination
    return settings


def stored_settings(settings: dict, topic_arn: str | None) -> dict:
    saved = {
        "timezone": settings["timezone"],
        "startDateTime": settings["startDateTime"],
        "wakeTime": settings["wakeTime"],
        "sleepTime": settings["sleepTime"],
        "channel": settings["channel"],
        "destination": settings["destination"],
    }
    if topic_arn:
        saved["topicArn"] = topic_arn
    return saved


def dose_text(med: dict) -> str:
    amount = med.get("dose_amount")
    unit = med.get("dose_unit")
    if amount is not None and unit:
        if isinstance(amount, float) and amount.is_integer():
            amount_text = str(int(amount))
        else:
            amount_text = f"{amount:g}" if isinstance(amount, float) else str(amount)
        return f"{amount_text} {unit}"
    if med.get("strength"):
        return med["strength"]
    return ""


def cron_expression(hhmm: str) -> str:
    hour, minute = hhmm.split(":")
    return f"cron({int(minute)} {int(hour)} * * ? *)"


def schedule_name(prescription_id: str, med_idx: int, hhmm: str) -> str:
    return f"rx-{prescription_id}-{med_idx}-{hhmm.replace(':', '')}"


def schedule_bounds(doses: list[datetime]) -> tuple[datetime, datetime]:
    first = min(doses).astimezone(timezone.utc) - timedelta(seconds=1)
    last = max(doses).astimezone(timezone.utc) + timedelta(minutes=1)
    return first, last


def delete_prescription_schedules(client, prescription_id: str) -> None:
    """Remove leftovers from an earlier attempt for this prescription only."""
    prefix = f"rx-{prescription_id}-"
    token = None
    while True:
        kwargs = {"GroupName": "default", "NamePrefix": prefix}
        if token:
            kwargs["NextToken"] = token
        page = client.list_schedules(**kwargs)
        names = [item["Name"] for item in page.get("Schedules", []) if str(item.get("Name", "")).startswith(prefix)]
        delete_schedules(client, names)
        token = page.get("NextToken")
        if not token:
            return


def create_schedules(client, *, prescription_id, user_id, patient, medications, settings, now, role_arn, function_arn, channel, destination, topic_arn) -> list[str]:
    """One schedule per medicine per daily time. A failure deletes the ones just created."""
    created = []
    patient_name = strip_controls(patient["name"]).strip()
    try:
        for med_idx, med in enumerate(medications):
            groups: dict[str, list[datetime]] = {}
            for dose in generate_dose_times(med, settings, now):
                groups.setdefault(dose.strftime("%H:%M"), []).append(dose)
            for hhmm, doses in groups.items():
                name = schedule_name(prescription_id, med_idx, hhmm)
                start, end = schedule_bounds(doses)
                payload = {
                    "userId": user_id,
                    "prescriptionId": prescription_id,
                    "medIdx": med_idx,
                    "name": med["name"],
                    "doseText": dose_text(med),
                    "instructions": med.get("instructions") or "",
                    "patientName": patient_name,
                    "colorKey": patient["colorKey"],
                    "channel": channel,
                    "destination": destination,
                }
                if topic_arn:
                    payload["topicArn"] = topic_arn
                client.create_schedule(
                    Name=name,
                    GroupName="default",
                    ScheduleExpression=cron_expression(hhmm),
                    ScheduleExpressionTimezone=settings["timezone"],
                    StartDate=start,
                    EndDate=end,
                    FlexibleTimeWindow={"Mode": "OFF"},
                    ActionAfterCompletion="DELETE",
                    State="ENABLED",
                    Target={
                        "Arn": function_arn,
                        "RoleArn": role_arn,
                        "Input": json.dumps(payload, ensure_ascii=False),
                    },
                )
                created.append(name)
    except Exception:
        delete_schedules(client, created)
        raise
    logger.info("schedules created %s count=%s", prescription_id, len(created))
    return created


def delete_schedules(client, names: list[str]) -> None:
    for name in names:
        try:
            client.delete_schedule(Name=name, GroupName="default")
        except Exception as exc:
            code = ""
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = response.get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                logger.error("schedule delete failed %s", code or type(exc).__name__)


def course_expiry_epoch(settings: dict, medications: list[dict], now: datetime) -> int:
    latest = None
    for med in medications:
        for dose in generate_dose_times(med, settings, now):
            if latest is None or dose > latest:
                latest = dose
    anchor = latest or settings["start"]
    return int((anchor + timedelta(days=30)).timestamp())


def _med_summary(med_idx, med, slots, doses) -> dict:
    return {
        "medIdx": med_idx,
        "name": med["name"],
        "doseText": dose_text(med),
        "slots": slots,
        "doseCount": len(doses),
        "firstDose": _local_iso(doses[0]) if doses else None,
        "lastDose": _local_iso(doses[-1]) if doses else None,
    }


def _days(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list] = {}
    for row in sorted(rows, key=lambda item: (item["at"], item["medIdx"])):
        local = row["local"]
        date = local.date().isoformat()
        grouped.setdefault(date, []).append(
            {
                "time": local.strftime("%H:%M"),
                "medIdx": row["medIdx"],
                "name": row["name"],
                "doseText": row["doseText"],
            }
        )
    return [{"date": date, "doses": doses} for date, doses in grouped.items()]


def _crowded(rows: list[dict]) -> list[str]:
    buckets: dict[str, set[int]] = {}
    for row in rows:
        key = row["local"].strftime("%Y-%m-%d %H:%M")
        buckets.setdefault(key, set()).add(row["medIdx"])
    messages = []
    seen = set()
    for key, meds in buckets.items():
        clock = key[-5:]
        if len(meds) > 3 and clock not in seen:
            seen.add(clock)
            messages.append(f"More than 3 medicines are due at {clock}")
    return messages


def _first_day_drops(settings, slot_minutes) -> bool:
    start = settings["start"]
    zone = settings["zone"]
    for minute in slot_minutes:
        local = datetime(start.year, start.month, start.day, minute // 60, minute % 60, tzinfo=zone)
        if local < start:
            return True
    return False


def _interval_slots(wake: int, sleep: int, hours: int, name: str) -> list[int]:
    slots = []
    minute = wake
    while minute < sleep:
        slots.append(minute)
        minute += hours * 60
        if len(slots) > 6:
            raise RequestError(400, f"{name} would have more than 6 doses a day")
    return slots or [wake]


def _hint_minute(hint: str, wake: int, sleep: int) -> int:
    if hint == "morning":
        minute = wake + 60
    elif hint == "noon":
        minute = 12 * 60
    elif hint == "evening":
        minute = 18 * 60
    else:
        minute = sleep - 30
    return min(sleep, max(wake, minute))


def _spread(wake: int, sleep: int, count: int) -> list[int]:
    if count <= 1:
        return [wake]
    span = sleep - wake
    slots = []
    for index in range(count):
        minute = int(round(wake + index * span / (count - 1)))
        minute = min(sleep, max(wake, minute))
        if minute not in slots:
            slots.append(minute)
    return slots


def _channel(raw: dict) -> tuple[str, str]:
    channel = raw.get("channel") or "email"
    if channel not in {"email", "sms"}:
        raise RequestError(400, "Choose email or sms")
    destination = raw.get("destination")
    if not isinstance(destination, str):
        raise RequestError(400, "Enter where the reminders should be sent")
    destination = strip_controls(destination).strip()
    if channel == "email":
        destination = destination.lower()
        if not _email_ok(destination):
            raise RequestError(400, "Enter a valid email address")
    elif not _phone_ok(destination):
        raise RequestError(400, "Enter a phone number in international format, such as +254712345678")
    return channel, destination


def _email_ok(value: str) -> bool:
    if len(value) > 254 or " " in value or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _phone_ok(value: str) -> bool:
    if len(value) < 9 or len(value) > 16 or not value.startswith("+"):
        return False
    digits = value[1:]
    return digits.isdigit() and not digits.startswith("0")


def _parse_start(value, zone: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RequestError(400, "Choose a start date and time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RequestError(400, "startDateTime must be a local date and time") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _clock(value, label: str) -> int:
    if not isinstance(value, str):
        raise RequestError(400, f"{label} must look like 07:00")
    try:
        return _minutes(value)
    except ValueError as exc:
        raise RequestError(400, f"{label} must look like 07:00") from exc


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    hour_i = int(hour)
    minute_i = int(minute)
    if hour_i > 23 or minute_i > 59:
        raise ValueError("bad clock")
    return hour_i * 60 + minute_i


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _hhmm_list(minutes: list[int]) -> list[str]:
    return [_hhmm(minute) for minute in minutes]


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _local_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unique(items: list[str]) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
