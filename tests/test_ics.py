"""Calendar text. Counts real VEVENT lines, not escaped text inside a title."""

from datetime import datetime, timezone

from common.ics import build_ics
from common.scheduling import generate_dose_times, parse_settings

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def settings():
    return parse_settings(
        {
            "timezone": "Africa/Nairobi",
            "startDateTime": "2026-11-20T08:00",
            "wakeTime": "07:00",
            "sleepTime": "22:00",
        },
        NOW,
        with_channel=False,
    )


def med(**overrides):
    body = {
        "name": "Amoxicillin",
        "dose_amount": 1,
        "dose_unit": "capsule",
        "times_per_day": 3,
        "duration_days": 2,
        "instructions": "After food, with water; shake well",
    }
    body.update(overrides)
    return body


def calendar(patient_name="Amina", medications=None):
    return build_ics(
        prescription_id="abc123def456",
        patient_name=patient_name,
        color_key="blue",
        medications=medications or [med()],
        settings=settings(),
        now=NOW,
    )


def event_lines(text):
    return [line for line in text.split("\r\n") if line == "BEGIN:VEVENT"]


def test_crlf_folding_and_escaping():
    text = calendar()
    assert "\r\n" in text
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    for line in text.split("\r\n"):
        if line == "":
            continue
        assert len(line.encode("utf-8")) <= 75
    assert "After food\\, with water\\; shake well" in text
    folded = [line for line in text.split("\r\n") if line.startswith(" ")]
    assert folded
    assert all(len(line.encode("utf-8")) <= 75 for line in folded)


def test_event_count_matches_the_dose_generator_and_uids_are_unique():
    chosen = settings()
    item = med()
    text = calendar(medications=[item])
    assert len(event_lines(text)) == len(generate_dose_times(item, chosen, NOW))
    uids = [line for line in text.split("\r\n") if line.startswith("UID:")]
    assert len(uids) == len(set(uids)) == 6


def test_summary_carries_emoji_and_name_and_colour_properties():
    text = calendar()
    assert "SUMMARY:🟦 Amina: Take Amoxicillin 1 capsule" in text
    assert "COLOR:dodgerblue" in text
    assert "X-APPLE-CALENDAR-COLOR:#1E88E5" in text


def test_injected_calendar_markup_does_not_add_an_event():
    text = calendar(patient_name="Amina\r\nBEGIN:VEVENT")
    assert len(event_lines(text)) == 6
    assert "AminaBEGIN:VEVENT" in text


def test_as_needed_calendar_has_no_events():
    text = calendar(medications=[med(as_needed=True, times_per_day=None, duration_days=None)])
    assert event_lines(text) == []
    assert text.startswith("BEGIN:VCALENDAR\r\n")
