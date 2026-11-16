"""Patient rules against an in-memory table. No AWS calls."""

import pytest
from botocore.exceptions import ClientError

import common.patients as patients
from common.http import RequestError


class MemoryTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, ConditionExpression=None):
        key = (Key["pk"], Key["sk"])
        if key not in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
                "DeleteItem",
            )
        del self.items[key]

    def query_all(self, pk, prefix):
        return [
            dict(item)
            for (item_pk, sk), item in self.items.items()
            if item_pk == pk and sk.startswith(prefix)
        ]


@pytest.fixture
def store(monkeypatch):
    memory = MemoryTable()
    monkeypatch.setattr(patients, "table", lambda: memory)
    monkeypatch.setattr(patients, "query_all", memory.query_all)
    return memory


def test_name_strips_cr_lf_and_rejects_a_name_without_a_letter():
    assert patients.validate_name("Amina\r\n") == "Amina"
    assert patients.validate_name("  Mary   Jane\n") == "Mary Jane"
    with pytest.raises(RequestError) as caught:
        patients.validate_name("1234")
    assert caught.value.status == 400


def test_colour_is_assigned_automatically_and_duplicates_are_rejected(store):
    first = patients.create_patient("user", {"name": "Amina"})
    second = patients.create_patient("user", {"name": "Sam", "relationship": "Son"})
    assert first["colorKey"] == "red"
    assert second["colorKey"] == "orange"
    assert second["relationship"] == "Son"
    with pytest.raises(RequestError) as caught:
        patients.create_patient("user", {"name": "Jo", "colorKey": "red"})
    assert caught.value.status == 409
    assert caught.value.message == "That colour is already used by Amina"


def test_eighth_patient_is_the_last(store):
    for name in ("Ada", "Bea", "Cam", "Dee", "Eve", "Fay", "Gus", "Hal"):
        patients.create_patient("user", {"name": name})
    with pytest.raises(RequestError) as caught:
        patients.create_patient("user", {"name": "Ninth"})
    assert caught.value.status == 409
    assert "8" in caught.value.message


def test_delete_is_blocked_while_a_prescription_is_active(store):
    saved = patients.create_patient("user", {"name": "Amina"})
    store.items[("USER#user", "RX#abc123def456")] = {
        "pk": "USER#user",
        "sk": "RX#abc123def456",
        "patientId": saved["patientId"],
        "status": "ACTIVE",
    }
    with pytest.raises(RequestError) as caught:
        patients.delete_patient("user", saved["patientId"])
    assert caught.value.status == 409
    store.items[("USER#user", "RX#abc123def456")]["status"] = "CANCELLED"
    patients.delete_patient("user", saved["patientId"])
    assert ("USER#user", f"PATIENT#{saved['patientId']}") not in store.items
