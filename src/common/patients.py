"""Patient profiles stored under one API key. These are not user accounts.

The name stored here is whatever the caregiver typed. It is never sent to
Textract or Bedrock. Control characters are removed so the name cannot break
an email subject, an SMS body, or a calendar file.
"""

import re
import unicodedata
import uuid

from botocore.exceptions import ClientError

from common.config import MAX_PATIENTS, NAME_MAX, RELATIONSHIP_MAX
from common.db import from_dynamo, iso_now, query_all, table, user_pk
from common.http import RequestError
from common.palette import first_unused, get_color

_PATIENT_ID = re.compile(r"^[0-9a-f]{8}$")


def strip_controls(value: str) -> str:
    """Drop control and format characters (CR, LF, bidi overrides, and the rest)."""
    return "".join(ch for ch in value if unicodedata.category(ch) not in {"Cc", "Cf"})


def validate_name(raw) -> str:
    if raw is None:
        raise RequestError(400, "Name is required")
    if not isinstance(raw, str):
        raise RequestError(400, "Name must be text")
    name = re.sub(r" {2,}", " ", strip_controls(raw).strip())
    if not 1 <= len(name) <= NAME_MAX:
        raise RequestError(400, "Name must be 1 to 40 characters")
    if not any(ch.isalpha() for ch in name):
        raise RequestError(400, "Name must include a letter")
    for ch in name:
        if ch in " .'-":
            continue
        if not ch.isalpha():
            raise RequestError(
                400,
                "Name may only use letters, spaces, hyphens, apostrophes and periods",
            )
    return name


def validate_relationship(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise RequestError(400, "Relationship must be text")
    relationship = re.sub(r" {2,}", " ", strip_controls(raw).strip())
    if not relationship:
        return None
    if len(relationship) > RELATIONSHIP_MAX:
        raise RequestError(400, "Relationship must be 30 characters or fewer")
    return relationship


def require_patient_id(patient_id: str) -> str:
    """Path ids that are not ours are hidden as not found."""
    if not isinstance(patient_id, str) or not _PATIENT_ID.fullmatch(patient_id):
        raise RequestError(404, "Patient not found")
    return patient_id


def require_body_patient_id(patient_id) -> str:
    if not isinstance(patient_id, str) or not patient_id:
        raise RequestError(400, "Choose a family member before uploading a prescription")
    if not _PATIENT_ID.fullmatch(patient_id):
        raise RequestError(400, "patientId is not valid")
    return patient_id


def public_patient(item: dict) -> dict:
    body = {
        "patientId": item["patientId"],
        "name": item["name"],
        "colorKey": item["colorKey"],
    }
    relationship = item.get("relationship")
    if relationship:
        body["relationship"] = relationship
    return body


def list_patients(user_id: str) -> list[dict]:
    items = query_all(user_pk(user_id), "PATIENT#")
    items.sort(key=lambda item: (item.get("createdAt", ""), item.get("patientId", "")))
    return items


def get_patient(user_id: str, patient_id: str) -> dict | None:
    require_patient_id(patient_id)
    resp = table().get_item(Key={"pk": user_pk(user_id), "sk": f"PATIENT#{patient_id}"})
    item = resp.get("Item")
    return from_dynamo(item) if item else None


def _used_colors(patients: list[dict]) -> set[str]:
    return {item["colorKey"] for item in patients}


def create_patient(user_id: str, body: dict) -> dict:
    name = validate_name(body.get("name"))
    relationship = validate_relationship(body.get("relationship"))
    existing = list_patients(user_id)
    if len(existing) >= MAX_PATIENTS:
        raise RequestError(409, "You can save at most 8 family members, one for each colour")
    used = _used_colors(existing)
    requested = body.get("colorKey")
    if requested is None or requested == "":
        color_key = first_unused(used)
        if color_key is None:
            raise RequestError(409, "All colours are already in use")
    else:
        color_key = _claim_color(requested, used, existing)
    patient_id = uuid.uuid4().hex[:8]
    now = iso_now()
    item = {
        "pk": user_pk(user_id),
        "sk": f"PATIENT#{patient_id}",
        "patientId": patient_id,
        "name": name,
        "colorKey": color_key,
        "createdAt": now,
        "updatedAt": now,
    }
    if relationship:
        item["relationship"] = relationship
    _write_new_item(item)
    return public_patient(item)


def update_patient(user_id: str, patient_id: str, body: dict) -> dict:
    require_patient_id(patient_id)
    current = get_patient(user_id, patient_id)
    if not current:
        raise RequestError(404, "Patient not found")
    if not any(key in body for key in ("name", "colorKey", "relationship")):
        raise RequestError(400, "Nothing to update")
    name = current["name"]
    color_key = current["colorKey"]
    relationship = current.get("relationship")
    if "name" in body:
        name = validate_name(body.get("name"))
    if "relationship" in body:
        relationship = validate_relationship(body.get("relationship"))
    if "colorKey" in body and body.get("colorKey") not in (None, ""):
        existing = list_patients(user_id)
        others = [item for item in existing if item["patientId"] != patient_id]
        color_key = _claim_color(body.get("colorKey"), _used_colors(others), others)
    now = iso_now()
    names = {"#n": "name"}
    values = {":n": name, ":c": color_key, ":u": now}
    update = "SET #n = :n, colorKey = :c, updatedAt = :u"
    if relationship:
        update += ", relationship = :r"
        values[":r"] = relationship
    else:
        update += " REMOVE relationship"
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"PATIENT#{patient_id}"},
            UpdateExpression=update,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as exc:
        _reraise_missing(exc)
    saved = {"patientId": patient_id, "name": name, "colorKey": color_key}
    if relationship:
        saved["relationship"] = relationship
    return saved


def delete_patient(user_id: str, patient_id: str) -> None:
    require_patient_id(patient_id)
    if not get_patient(user_id, patient_id):
        raise RequestError(404, "Patient not found")
    if _has_active_prescription(user_id, patient_id):
        raise RequestError(
            409,
            "Cancel this person's active prescriptions before removing them",
        )
    try:
        table().delete_item(
            Key={"pk": user_pk(user_id), "sk": f"PATIENT#{patient_id}"},
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as exc:
        _reraise_missing(exc)


def _claim_color(requested, used: set[str], existing: list[dict]) -> str:
    if not isinstance(requested, str) or get_color(requested) is None:
        raise RequestError(400, "Choose a colour from the palette")
    if requested in used:
        owner = next(item["name"] for item in existing if item["colorKey"] == requested)
        raise RequestError(409, f"That colour is already used by {owner}")
    return requested


def _has_active_prescription(user_id: str, patient_id: str) -> bool:
    # Filtered in process so the reserved word "status" never appears in an expression.
    # A demo user has few prescriptions; the UPLOADING/FAILED items expire after 7 days.
    for item in query_all(user_pk(user_id), "RX#"):
        if item.get("patientId") == patient_id and item.get("status") == "ACTIVE":
            return True
    return False


def _write_new_item(item: dict) -> None:
    try:
        table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise RequestError(500, "Something went wrong. Please try again.") from exc
        raise


def _reraise_missing(exc: ClientError) -> None:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        raise RequestError(404, "Patient not found") from exc
    raise
