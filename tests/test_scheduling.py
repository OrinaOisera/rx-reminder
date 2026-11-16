"""Dose times. No AWS calls."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from common.http import RequestError
from common.scheduling import (
    build_preview,
    cron_expression,
    daily_slots,
    generate_dose_times,
    parse_settings,
    schedule_bounds,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def settings(start="2026-11-20T08:00", zone_name="Africa/Nairobi"):
    return parse_settings(
        {
            "timezone": zone_name,
            "startDateTime": start,
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
        "times_per_day": 1,
        "duration_days": 1,
        "as_needed": False,
    }
    body.update(overrides)
    return body


def clocks(body, **overrides):
    item = med(**overrides)
    item.update(body)
    return [f"{minute // 60:02d}:{minute % 60:02d}" for minute in daily_slots(item, settings())]


def test_slot_table_for_one_to_four_per_day():
    assert clocks({}, times_per_day=1) == ["08:00"]
    assert clocks({}, times_per_day=2) == ["08:00", "20:00"]
    assert clocks({}, times_per_day=3) == ["08:00", "14:00", "20:00"]
    assert clocks({}, times_per_day=4) == ["08:00", "12:00", "16:00", "20:00"]


def test_five_and_six_doses_are_spread_from_wake_to_sleep():
    assert clocks({}, times_per_day=5) == ["07:00", "10:45", "14:30", "18:15", "22:00"]
    assert clocks({}, times_per_day=6) == ["07:00", "10:00", "13:00", "16:00", "19:00", "22:00"]


def test_interval_hours_start_at_wake_and_stop_before_sleep():
    assert clocks({"interval_hours": 8, "times_per_day": None}) == ["07:00", "15:00"]


def test_interval_over_six_doses_is_rejected():
    with pytest.raises(RequestError) as caught:
        clocks({"interval_hours": 2, "times_per_day": None})
    assert caught.value.status == 400


def test_timing_hint_is_used_only_for_a_single_daily_dose():
    assert clocks({"timing_hint": "bedtime", "times_per_day": None}) == ["21:30"]
    assert clocks({"timing_hint": "morning", "times_per_day": 1}) == ["08:00"]
    assert clocks({"timing_hint": "bedtime", "times_per_day": 3}) == ["08:00", "14:00", "20:00"]


def test_as_needed_has_no_doses():
    item = med(as_needed=True, times_per_day=None, duration_days=None)
    assert generate_dose_times(item, settings(), NOW) == []


def test_first_day_drops_doses_before_the_start_time():
    chosen = settings("2026-11-20T15:00")
    item = med(times_per_day=3, duration_days=1)
    doses = generate_dose_times(item, chosen, NOW)
    assert [dose.strftime("%H:%M") for dose in doses] == ["20:00"]


def test_duration_includes_the_start_day_and_stops_before_the_final_midnight():
    one_day = generate_dose_times(med(duration_days=1), settings(), NOW)
    two_days = generate_dose_times(med(duration_days=2), settings(), NOW)
    assert [dose.date().isoformat() for dose in one_day] == ["2026-11-20"]
    assert [dose.date().isoformat() for dose in two_days] == ["2026-11-20", "2026-11-21"]


def test_total_dose_cap():
    prescription = {"patientId": "abcd1234"}
    patient = {"name": "Amina", "colorKey": "blue"}
    with pytest.raises(RequestError) as caught:
        build_preview(
            prescription,
            patient,
            [med(times_per_day=2, duration_days=251)],
            settings(),
            NOW,
        )
    assert caught.value.status == 400
    assert "500" in caught.value.message


def test_nairobi_and_london_dst_conversion():
    nairobi = generate_dose_times(med(), settings(), NOW)[0]
    assert nairobi.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-11-20T05:00:00Z"

    london = settings("2027-03-27T08:00", "Europe/London")
    doses = generate_dose_times(med(duration_days=3), london, NOW)
    utc = [dose.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for dose in doses]
    assert utc == [
        "2027-03-27T08:00:00Z",
        "2027-03-28T07:00:00Z",
        "2027-03-29T07:00:00Z",
    ]


def test_scheduler_bounds_are_utc():
    dose = generate_dose_times(med(), settings(), NOW)
    start, end = schedule_bounds(dose)
    assert start == datetime(2026, 11, 20, 4, 59, 59, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 20, 5, 1, tzinfo=timezone.utc)
    assert end - start == timedelta(minutes=1, seconds=1)


def test_cron_expression_is_not_zero_padded():
    assert cron_expression("08:00") == "cron(0 8 * * ? *)"
    assert cron_expression("08:05") == "cron(5 8 * * ? *)"


def test_now_does_not_change_the_dose_list():
    item = med(duration_days=2)
    chosen = settings()
    later = NOW + timedelta(days=30)
    assert generate_dose_times(item, chosen, NOW) == generate_dose_times(item, chosen, later)
