"""HTTP API router.

Safety: a photo moves from UPLOADING to PENDING_REVIEW only after OCR.
Reminders are created only by confirm, and only when the schedule hash matches
the timetable the person just reviewed. Do not log the request body, OCR text,
medicine details, destinations, or patient names.
Demo auth compares X-Api-Key with the API_KEY parameter. Real auth would be Cognito.
The user id is the first 12 hex characters of SHA-256 of that key.
"""

import logging
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common.config import (
    CALENDAR_URL_SECONDS,
    PRESIGN_EXPIRES_SECONDS,
    UPLOAD_TTL_DAYS,
    aws_region,
    bucket_name,
    reminder_function_arn,
    scheduler_role_arn,
)
from common.db import epoch_days_from_now, from_dynamo, iso_now, query_all, table, to_dynamo, user_pk
from common.models import normalize_confirmed_medications, normalize_extracted
from common.ics import build_ics, calendar_filename, calendar_key
from common.http import (
    RequestError,
    authorize,
    current_user_id,
    error,
    normalize_path,
    parse_body,
    response,
)
from common.notify import ensure_email_topic, send_reminder
from common.palette import palette_list
from common.patients import (
    create_patient,
    delete_patient,
    get_patient,
    list_patients,
    public_patient,
    require_body_patient_id,
    update_patient,
    validate_name,
)
from common.scheduling import (
    build_preview,
    course_expiry_epoch,
    create_schedules,
    delete_prescription_schedules,
    delete_schedules,
    dose_text,
    parse_settings,
    require_same_hash,
    stored_settings,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
}
_RX_ID = re.compile(r"^[0-9a-f]{12}$")
_PATIENT_QUERY = re.compile(r"^[0-9a-f]{8}$")


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    method = method.upper() if isinstance(method, str) else ""
    path = normalize_path(event)
    if method == "OPTIONS":
        return response(200, {})
    logger.info("request %s %s", method, path)
    denied = authorize(event)
    if denied:
        return denied
    try:
        user_id = current_user_id()
        return dispatch(method, path, event, user_id)
    except RequestError as exc:
        return error(exc.status, exc.message)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.error("request failed: %s", code or type(exc).__name__)
        return error(500, "Something went wrong. Please try again.")
    except Exception as exc:
        logger.error("request failed: %s", type(exc).__name__)
        return error(500, "Something went wrong. Please try again.")


def dispatch(method: str, path: str, event: dict, user_id: str) -> dict:
    parts = [part for part in path.split("/") if part]
    if parts == ["patients"] and method == "GET":
        patients = [public_patient(item) for item in list_patients(user_id)]
        return response(200, {"patients": patients, "palette": palette_list()})
    if parts == ["patients"] and method == "POST":
        created = create_patient(user_id, parse_body(event))
        return response(201, created)
    if len(parts) == 2 and parts[0] == "patients" and method == "PATCH":
        updated = update_patient(user_id, parts[1], parse_body(event))
        return response(200, updated)
    if len(parts) == 2 and parts[0] == "patients" and method == "DELETE":
        delete_patient(user_id, parts[1])
        return response(200, {"deleted": True})
    if parts == ["prescriptions"] and method == "POST":
        return create_prescription(user_id, parse_body(event))
    if parts == ["prescriptions"] and method == "GET":
        return list_prescriptions(user_id, event)
    if len(parts) == 2 and parts[0] == "prescriptions" and method == "GET":
        return get_prescription(user_id, parts[1])
    if len(parts) == 2 and parts[0] == "prescriptions" and method == "DELETE":
        return cancel_prescription(user_id, parts[1])
    if len(parts) == 3 and parts[0] == "prescriptions" and method == "GET" and parts[2] == "calendar":
        return get_calendar(user_id, parts[1])
    if len(parts) == 3 and parts[0] == "prescriptions" and method == "POST":
        if parts[2] == "preview":
            return preview_prescription(user_id, parts[1], parse_body(event))
        if parts[2] == "confirm":
            return confirm_prescription(user_id, parts[1], parse_body(event))
        if parts[2] == "test-reminder":
            return test_reminder(user_id, parts[1])
    return error(404, "Not found")


def create_prescription(user_id: str, body: dict) -> dict:
    patient_id = require_body_patient_id(body.get("patientId"))
    if not get_patient(user_id, patient_id):
        raise RequestError(404, "Patient not found")
    content_type = body.get("contentType")
    extension = _CONTENT_TYPES.get(content_type)
    if extension is None:
        raise RequestError(400, "contentType must be image/jpeg or image/png")
    prescription_id = uuid.uuid4().hex[:12]
    # Section 6: the key extension follows the content type. Section 5 shows .jpg only.
    image_key = f"uploads/{user_id}/{prescription_id}.{extension}"
    now = iso_now()
    try:
        table().put_item(
            Item={
                "pk": user_pk(user_id),
                "sk": f"RX#{prescription_id}",
                "prescriptionId": prescription_id,
                "status": "UPLOADING",
                "patientId": patient_id,
                "imageKey": image_key,
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": epoch_days_from_now(UPLOAD_TTL_DAYS),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ConditionalCheckFailedException":
            raise
        logger.error("prescription id collision %s", prescription_id)
        return error(500, "Something went wrong. Please try again.")
    upload_url = _presign_put(image_key, content_type)
    logger.info("prescription created %s", prescription_id)
    return response(
        201,
        {
            "prescriptionId": prescription_id,
            "uploadUrl": upload_url,
            "uploadHeaders": {"Content-Type": content_type},
            "expiresInSeconds": PRESIGN_EXPIRES_SECONDS,
        },
    )


def list_prescriptions(user_id: str, event: dict) -> dict:
    """Newest 20. Medicine names only, so the list stays small."""
    params = event.get("queryStringParameters") or {}
    wanted = params.get("patientId") if isinstance(params, dict) else None
    if wanted == "":
        wanted = None
    if wanted is not None and (not isinstance(wanted, str) or not _PATIENT_QUERY.fullmatch(wanted)):
        raise RequestError(400, "patientId is not valid")
    patients = {item["patientId"]: item for item in list_patients(user_id)}
    rows = []
    for item in query_all(user_pk(user_id), "RX#"):
        if wanted and item.get("patientId") != wanted:
            continue
        patient = patients.get(item.get("patientId"), {})
        rows.append(
            {
                "prescriptionId": item.get("prescriptionId"),
                "status": item.get("status"),
                "createdAt": item.get("createdAt"),
                "patientId": item.get("patientId"),
                "patientName": item.get("patientName") or patient.get("name"),
                "colorKey": item.get("colorKey") or patient.get("colorKey"),
                "medications": _medicine_names(item),
            }
        )
    rows.sort(key=lambda row: row.get("createdAt") or "", reverse=True)
    return response(200, {"prescriptions": rows[:20]})


def _medicine_names(item: dict) -> list[str]:
    confirmed = item.get("confirmed")
    meds = confirmed if isinstance(confirmed, list) else None
    if meds is None and isinstance(confirmed, dict):
        meds = confirmed.get("medications")
    if not isinstance(meds, list):
        extracted = item.get("extracted") if isinstance(item.get("extracted"), dict) else {}
        meds = extracted.get("medications")
    names = []
    if isinstance(meds, list):
        for med in meds:
            if isinstance(med, dict) and isinstance(med.get("name"), str) and med["name"]:
                names.append(med["name"])
    return names[:10]


def get_prescription(user_id: str, prescription_id: str) -> dict:
    if not _RX_ID.fullmatch(prescription_id):
        raise RequestError(404, "Prescription not found")
    resp = table().get_item(Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"})
    item = resp.get("Item")
    if not item:
        raise RequestError(404, "Prescription not found")
    return response(200, prescription_view(from_dynamo(item)))


def prescription_view(item: dict) -> dict:
    """Fields the poller needs. Raw OCR text is never stored and is not returned."""
    body = {
        "prescriptionId": item.get("prescriptionId"),
        "status": item.get("status"),
        "patientId": item.get("patientId"),
        "failureReason": item.get("failureReason"),
    }
    extracted = item.get("extracted")
    if isinstance(extracted, dict):
        body["extracted"] = normalize_extracted(extracted)
    for key in ("confirmed", "doseCount", "patientName", "colorKey"):
        if key in item:
            body[key] = item[key]
    settings = item.get("settings")
    if isinstance(settings, dict):
        visible = dict(settings)
        visible.pop("topicArn", None)
        body["settings"] = visible
    return body


def preview_prescription(user_id: str, prescription_id: str, body: dict) -> dict:
    item = _load_prescription(user_id, prescription_id)
    _require_pending(item)
    patient = _require_patient(user_id, item["patientId"])
    preview = _reviewed_schedule(item, patient, body, with_channel=False)
    return response(200, preview)


def confirm_prescription(user_id: str, prescription_id: str, body: dict) -> dict:
    if body.get("acknowledged") is not True:
        raise RequestError(400, "Confirm that you have checked this schedule")
    item = _load_prescription(user_id, prescription_id)
    _require_pending(item)
    patient = _require_patient(user_id, item["patientId"])
    patient_name = validate_name(patient["name"])
    settings, medications, preview = _reviewed_schedule_parts(item, patient, body, with_channel=True)
    require_same_hash(body.get("scheduleHash"), preview["scheduleHash"])
    token = _lock_confirm(user_id, prescription_id)
    client = _scheduler()
    created: list[str] = []
    uploaded = False
    try:
        topic_arn = None
        if settings["channel"] == "email":
            topic_arn = ensure_email_topic(user_id, settings["destination"])
        delete_prescription_schedules(client, prescription_id)
        created = create_schedules(
            client,
            prescription_id=prescription_id,
            user_id=user_id,
            patient=patient,
            medications=medications,
            settings=settings,
            now=datetime.now(timezone.utc),
            role_arn=scheduler_role_arn(),
            function_arn=reminder_function_arn(),
            channel=settings["channel"],
            destination=settings["destination"],
            topic_arn=topic_arn,
        )
        ics_key = calendar_key(user_id, prescription_id)
        _put_calendar(
            ics_key,
            build_ics(
                prescription_id=prescription_id,
                patient_name=patient_name,
                color_key=patient["colorKey"],
                medications=medications,
                settings=settings,
                now=datetime.now(timezone.utc),
            ),
        )
        uploaded = True
        _mark_active(
            user_id,
            prescription_id,
            token,
            patient_name,
            patient["colorKey"],
            medications,
            settings,
            topic_arn,
            created,
            preview["totalDoses"],
            ics_key,
        )
    except RequestError:
        if created:
            delete_schedules(client, created)
        if uploaded:
            _delete_calendar(calendar_key(user_id, prescription_id))
        _clear_lock(user_id, prescription_id, token)
        raise
    except Exception as exc:
        if created:
            delete_schedules(client, created)
        if uploaded:
            _delete_calendar(calendar_key(user_id, prescription_id))
        _clear_lock(user_id, prescription_id, token)
        logger.error("confirm failed %s %s", prescription_id, type(exc).__name__)
        raise RequestError(500, "Something went wrong. Please try again.") from exc
    notes = list(preview["notes"])
    if settings["channel"] == "email":
        notes.append("Check your inbox and confirm the subscription before reminders can arrive.")
    logger.info("confirmed %s doses=%s", prescription_id, preview["totalDoses"])
    return response(
        200,
        {
            "patient": preview["patient"],
            "doseCount": preview["totalDoses"],
            "firstDose": _overall_bound(preview, "firstDose", min),
            "lastDose": _overall_bound(preview, "lastDose", max),
            "icsUrl": _presign_calendar(calendar_key(user_id, prescription_id), patient_name, prescription_id),
            "notes": notes,
        },
    )


def get_calendar(user_id: str, prescription_id: str) -> dict:
    item = _load_prescription(user_id, prescription_id)
    if item.get("status") != "ACTIVE" or not item.get("icsKey"):
        raise RequestError(409, "The calendar is available after the schedule is confirmed")
    name = item.get("patientName") or "Patient"
    return response(
        200,
        {
            "icsUrl": _presign_calendar(item["icsKey"], name, prescription_id),
            "expiresInSeconds": CALENDAR_URL_SECONDS,
        },
    )


def cancel_prescription(user_id: str, prescription_id: str) -> dict:
    """Remove dose schedules and mark the prescription cancelled. Safe to repeat."""
    item = _load_prescription(user_id, prescription_id)
    names = item.get("scheduleNames") if isinstance(item.get("scheduleNames"), list) else []
    client = _scheduler()
    delete_schedules(client, [name for name in names if isinstance(name, str)])
    delete_prescription_schedules(client, prescription_id)
    if item.get("status") != "CANCELLED":
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression="SET #s = :cancelled, updatedAt = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":cancelled": "CANCELLED", ":now": iso_now()},
        )
    logger.info("cancelled %s", prescription_id)
    return response(200, {"status": "CANCELLED"})


def test_reminder(user_id: str, prescription_id: str) -> dict:
    item = _load_prescription(user_id, prescription_id)
    if item.get("status") != "ACTIVE":
        raise RequestError(409, "Reminders start only after the schedule is confirmed")
    settings = item.get("settings") or {}
    medications = item.get("confirmed") or []
    meds = medications.get("medications") if isinstance(medications, dict) else medications
    if not isinstance(meds, list) or not meds:
        raise RequestError(409, "This schedule has no medicines to remind you about")
    chosen = next(
        ((index, med) for index, med in enumerate(meds) if not med.get("as_needed")),
        (0, meds[0]),
    )
    med_idx, med = chosen
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression="ADD testReminderCount :one",
            ConditionExpression="attribute_not_exists(testReminderCount) OR testReminderCount < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": 5},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise RequestError(429, "This prescription has already sent 5 test reminders.") from exc
        raise
    try:
        send_reminder(
            {
                "userId": user_id,
                "prescriptionId": prescription_id,
                "medIdx": med_idx,
                "name": med.get("name"),
                "doseText": dose_text(med),
                "instructions": med.get("instructions") or "",
                "patientName": item.get("patientName") or "",
                "colorKey": item.get("colorKey") or "",
                "channel": settings.get("channel") or "email",
                "destination": settings.get("destination") or "",
                "topicArn": settings.get("topicArn"),
            }
        )
    except Exception as exc:
        logger.error("test reminder failed %s %s", prescription_id, type(exc).__name__)
        raise RequestError(500, "The test reminder could not be sent. Please try again.") from exc
    logger.info("test reminder %s", prescription_id)
    return response(200, {"sent": True})


def _reviewed_schedule(item, patient, body, *, with_channel: bool) -> dict:
    _, _, preview = _reviewed_schedule_parts(item, patient, body, with_channel=with_channel)
    return preview


def _reviewed_schedule_parts(item, patient, body, *, with_channel: bool):
    now = datetime.now(timezone.utc)
    settings = parse_settings(body.get("settings"), now, with_channel=with_channel)
    try:
        medications = normalize_confirmed_medications(body.get("medications"))
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    preview = build_preview(item, patient, medications, settings, now)
    return settings, medications, preview


def _load_prescription(user_id: str, prescription_id: str) -> dict:
    if not _RX_ID.fullmatch(prescription_id):
        raise RequestError(404, "Prescription not found")
    resp = table().get_item(Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"})
    item = resp.get("Item")
    if not item:
        raise RequestError(404, "Prescription not found")
    return from_dynamo(item)


def _require_pending(item: dict) -> None:
    if item.get("status") != "PENDING_REVIEW":
        raise RequestError(409, "This prescription is not waiting for review")


def _require_patient(user_id: str, patient_id: str) -> dict:
    patient = get_patient(user_id, patient_id)
    if not patient:
        raise RequestError(404, "Patient not found")
    return patient


def _lock_confirm(user_id: str, prescription_id: str) -> str:
    token = uuid.uuid4().hex
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression="SET confirmLock = :token, confirmLockAt = :at, updatedAt = :now",
            ConditionExpression=(
                "#s = :pending AND (attribute_not_exists(confirmLock) OR confirmLockAt < :stale)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":token": token,
                ":at": now_epoch,
                ":now": iso_now(),
                ":pending": "PENDING_REVIEW",
                ":stale": now_epoch - 120,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise RequestError(409, "This prescription is not waiting for review") from exc
        raise
    return token


def _clear_lock(user_id: str, prescription_id: str, token: str) -> None:
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression="REMOVE confirmLock, confirmLockAt",
            ConditionExpression="confirmLock = :token",
            ExpressionAttributeValues={":token": token},
        )
    except ClientError:
        return


def _mark_active(user_id, prescription_id, token, patient_name, color_key, medications, settings, topic_arn, names, dose_count, ics_key):
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression=(
                "SET #s = :active, confirmed = :confirmed, settings = :settings, "
                "scheduleNames = :names, doseCount = :count, patientName = :patientName, "
                "colorKey = :color, icsKey = :ics, expiresAt = :exp, updatedAt = :now "
                "REMOVE confirmLock, confirmLockAt"
            ),
            ConditionExpression="#s = :pending AND confirmLock = :token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":active": "ACTIVE",
                ":pending": "PENDING_REVIEW",
                ":token": token,
                ":confirmed": to_dynamo(medications),
                ":settings": to_dynamo(stored_settings(settings, topic_arn)),
                ":names": names,
                ":count": dose_count,
                ":patientName": patient_name,
                ":color": color_key,
                ":ics": ics_key,
                ":exp": course_expiry_epoch(settings, medications, datetime.now(timezone.utc)),
                ":now": iso_now(),
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise RequestError(409, "This prescription is not waiting for review") from exc
        raise


def _overall_bound(preview: dict, field: str, choose):
    values = [item[field] for item in preview["perMedication"] if item.get(field)]
    if not values:
        return None
    return choose(values)


def _scheduler():
    return boto3.client("scheduler", region_name=aws_region())


def _put_calendar(key: str, body: str) -> None:
    _s3_client().put_object(
        Bucket=bucket_name(),
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="text/calendar",
    )


def _delete_calendar(key: str) -> None:
    try:
        _s3_client().delete_object(Bucket=bucket_name(), Key=key)
    except ClientError:
        return


def _presign_calendar(key: str, patient_name: str, prescription_id: str) -> str:
    filename = calendar_filename(patient_name, prescription_id)
    return _s3_client().generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket_name(),
            "Key": key,
            "ResponseContentType": "text/calendar",
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=CALENDAR_URL_SECONDS,
    )


def _s3_client():
    """SigV4 against the regional endpoint so the browser is not redirected (CORS)."""
    region = aws_region()
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _presign_put(image_key: str, content_type: str) -> str:
    return _s3_client().generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket_name(),
            "Key": image_key,
            "ContentType": content_type,
        },
        ExpiresIn=PRESIGN_EXPIRES_SECONDS,
        HttpMethod="PUT",
    )
