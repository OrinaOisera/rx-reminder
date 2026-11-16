"""Hand-written RFC 5545 calendar. No third-party library.

One event per dose, in UTC, from the same dose list the scheduler uses.
Apple Calendar and Outlook read the colour properties. Google Calendar does
not, so every title also starts with the patient's emoji and name.
"""

import re
import unicodedata
from datetime import timedelta, timezone

from common.palette import get_color
from common.patients import strip_controls
from common.scheduling import dose_text, generate_dose_times

PRODID = "-//Rx Reminder//EN"
_AID = "Reminder aid only - follow your prescription."


def calendar_key(user_id: str, prescription_id: str) -> str:
    return f"calendars/{user_id}/{prescription_id}.ics"


def calendar_filename(patient_name: str, prescription_id: str) -> str:
    return f"meds-{filename_slug(patient_name)}-{prescription_id}.ics"


def filename_slug(name: str) -> str:
    folded = unicodedata.normalize("NFKD", strip_controls(name))
    ascii_name = folded.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    return slug or "patient"


def build_ics(*, prescription_id, patient_name, color_key, medications, settings, now) -> str:
    """CRLF calendar text. Text values are escaped and lines are folded."""
    color = get_color(color_key) or {}
    emoji = color.get("emoji") or ""
    css_name = color.get("cssName") or "black"
    hex_color = color.get("hex") or "#000000"
    safe_name = " ".join(strip_controls(patient_name).split()) or "Patient"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape('Meds - ' + safe_name)}",
        f"COLOR:{css_name}",
        f"X-APPLE-CALENDAR-COLOR:{hex_color}",
    ]
    stamp = _stamp(now.astimezone(timezone.utc))
    for med_idx, med in enumerate(medications):
        taken = dose_text(med)
        summary = f"{emoji} {safe_name}: Take {med['name']}"
        if taken:
            summary = f"{summary} {taken}"
        description = _AID
        if med.get("instructions"):
            description = f"{med['instructions']}\n{_AID}"
        for dose in generate_dose_times(med, settings, now):
            start = dose.astimezone(timezone.utc).replace(microsecond=0)
            end = start + timedelta(minutes=15)
            uid_time = _stamp(start)
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{prescription_id}-{med_idx}-{uid_time}@rx-reminder",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART:{uid_time}",
                    f"DTEND:{_stamp(end)}",
                    f"SUMMARY:{_escape(summary)}",
                    f"CATEGORIES:{_escape(safe_name)}",
                    f"DESCRIPTION:{_escape(description)}",
                    f"COLOR:{css_name}",
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    "TRIGGER:-PT0M",
                    f"DESCRIPTION:{_escape('Take ' + med['name'])}",
                    "END:VALARM",
                    "END:VEVENT",
                ]
            )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _escape(value: str) -> str:
    text = strip_controls(value)
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _fold(line: str) -> str:
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts = []
    limit = 75
    while len(data) > limit:
        cut = limit
        while cut > 0 and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        if cut <= 0:
            cut = limit
        parts.append(data[:cut])
        data = data[cut:]
        limit = 74
    parts.append(data)
    return b"\r\n ".join(parts).decode("utf-8")


def _stamp(moment) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
