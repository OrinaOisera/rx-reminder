"""Medicine JSON checks. Out-of-range numbers become null instead of a guessed dose."""

from common.models import normalize_confirmed_medications, normalize_extracted


def medicine(**overrides):
    body = {"name": "Amoxicillin", "confidence": "high"}
    body.update(overrides)
    return body


def test_enums_are_normalised_and_unknown_values_are_dropped():
    found = normalize_extracted(
        {"medications": [medicine(form="Capsule", dose_unit="ML", route="ORAL", timing_hint="Morning")]}
    )["medications"][0]
    assert found["form"] == "capsule"
    assert found["dose_unit"] == "ml"
    assert found["route"] == "oral"
    assert found["timing_hint"] == "morning"
    unknown = normalize_extracted({"medications": [medicine(form="potion", route="space")]})["medications"][0]
    assert unknown["form"] is None
    assert unknown["route"] is None


def test_out_of_range_numbers_become_null():
    found = normalize_extracted(
        {
            "medications": [
                medicine(times_per_day=9, interval_hours=0, duration_days=366, dose_amount=0),
            ]
        }
    )["medications"][0]
    assert found["times_per_day"] is None
    assert found["interval_hours"] is None
    assert found["duration_days"] is None
    assert found["dose_amount"] is None
    kept = normalize_extracted(
        {"medications": [medicine(times_per_day="6", duration_days=365, dose_amount="1.5")]}
    )["medications"][0]
    assert kept["times_per_day"] == 6
    assert kept["duration_days"] == 365
    assert kept["dose_amount"] == 1.5


def test_missing_confidence_is_low():
    found = normalize_extracted({"medications": [{"name": "Ibuprofen"}]})["medications"][0]
    assert found["confidence"] == "low"


def test_only_the_first_ten_medicines_are_kept():
    raw = {"medications": [medicine(name=f"Med {index}") for index in range(11)]}
    result = normalize_extracted(raw)
    assert len(result["medications"]) == 10
    assert result["medications"][-1]["name"] == "Med 9"
    assert any("first 10" in warning for warning in result["warnings"])


def test_a_medicine_without_a_name_is_dropped_on_extract_and_rejected_on_confirm():
    result = normalize_extracted({"medications": [{"dose_amount": 1}, medicine()]})
    assert len(result["medications"]) == 1
    assert any("without a name" in warning for warning in result["warnings"])
    try:
        normalize_confirmed_medications([{"dose_amount": 1}])
    except ValueError as exc:
        assert "name" in str(exc)
    else:
        raise AssertionError("a missing name should be rejected")
