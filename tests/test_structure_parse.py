"""Model-reply parsing. The sample OCR file is not JSON and must not be guessed."""

from pathlib import Path

import pytest

from structure import StructuringError, parse_model_json

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_ocr_text.txt"


def test_fenced_json():
    raw = '```json\n{"is_prescription": true, "medications": []}\n```'
    assert parse_model_json(raw)["is_prescription"] is True


def test_json_with_prose_around_it():
    raw = 'Here is the reading:\n{"medications": [{"name": "Amoxicillin"}]}\nDone.'
    assert parse_model_json(raw)["medications"][0]["name"] == "Amoxicillin"


def test_invalid_json_is_an_error():
    with pytest.raises(StructuringError):
        parse_model_json("this is not json")


def test_sample_ocr_fixture_is_not_treated_as_json():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "Amoxicillin" in text
    with pytest.raises(StructuringError):
        parse_model_json(text)
