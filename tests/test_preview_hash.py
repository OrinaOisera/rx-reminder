"""Preview hash. The calculation does not call AWS."""

from datetime import datetime, timezone

import pytest

from common.http import RequestError
from common.scheduling import HASH_MISMATCH, build_preview, parse_settings, require_same_hash

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def preview(medications=None, patient_id="abcd1234", start="2026-11-20T08:00"):
    settings = parse_settings(
        {
            "timezone": "Africa/Nairobi",
            "startDateTime": start,
            "wakeTime": "07:00",
            "sleepTime": "22:00",
        },
        NOW,
        with_channel=False,
    )
    meds = medications or [
        {
            "name": "Amoxicillin",
            "dose_amount": 1,
            "dose_unit": "capsule",
            "times_per_day": 3,
            "duration_days": 5,
        }
    ]
    return build_preview(
        {"patientId": patient_id},
        {"name": "Amina", "colorKey": "blue"},
        meds,
        settings,
        NOW,
    )


def test_same_input_has_the_same_hash():
    first = preview()
    second = preview()
    assert first["scheduleHash"] == second["scheduleHash"]
    assert first["totalDoses"] == 15


def test_changing_a_slot_duration_or_patient_changes_the_hash():
    original = preview()["scheduleHash"]
    slotted = [
        {
            "name": "Amoxicillin",
            "dose_amount": 1,
            "dose_unit": "capsule",
            "times_per_day": 3,
            "duration_days": 5,
            "slots": ["09:00"],
        }
    ]
    assert preview(slotted)["scheduleHash"] != original
    shorter = [
        {
            "name": "Amoxicillin",
            "dose_amount": 1,
            "dose_unit": "capsule",
            "times_per_day": 3,
            "duration_days": 4,
        }
    ]
    assert preview(shorter)["scheduleHash"] != original
    assert preview(patient_id="ffff0000")["scheduleHash"] != original


def test_slots_override_the_daily_table():
    body = preview(
        [
            {
                "name": "Amoxicillin",
                "dose_amount": 1,
                "dose_unit": "capsule",
                "times_per_day": 3,
                "duration_days": 2,
                "slots": ["20:00", "09:00"],
            }
        ]
    )
    assert body["perMedication"][0]["slots"] == ["09:00", "20:00"]
    assert body["totalDoses"] == 4


def test_confirm_rejects_a_mismatched_hash():
    actual = preview()["scheduleHash"]
    require_same_hash(actual, actual)
    with pytest.raises(RequestError) as caught:
        require_same_hash("0" * 64, actual)
    assert caught.value.status == 409
    assert caught.value.message == HASH_MISMATCH


def test_first_day_partial_is_a_warning():
    body = preview(start="2026-11-20T15:00")
    assert any("first day" in line for line in body["warnings"])
