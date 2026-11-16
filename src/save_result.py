"""Write the structured medicines and move PROCESSING to PENDING_REVIEW.

The OCR text is not part of this input and is not stored. Unknown JSON fields
are dropped again here. The status change is conditional, so a retry cannot
overwrite a record that has already moved on.
"""

import logging
import re

from botocore.exceptions import ClientError

from common.config import UPLOAD_TTL_DAYS
from common.db import epoch_days_from_now, iso_now, table, to_dynamo, user_pk
from common.models import normalize_extracted

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ID = re.compile(r"^[0-9a-f]{12}$")


def handler(event, context):
    user_id = event.get("userId") if isinstance(event, dict) else None
    prescription_id = event.get("prescriptionId") if isinstance(event, dict) else None
    if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
        raise ValueError("missing user")
    if not isinstance(prescription_id, str) or not _ID.fullmatch(prescription_id):
        raise ValueError("missing prescription")
    extracted = normalize_extracted(event.get("extracted"))
    try:
        table().update_item(
            Key={"pk": user_pk(user_id), "sk": f"RX#{prescription_id}"},
            UpdateExpression=(
                "SET #s = :pending, extracted = :extracted, updatedAt = :now, expiresAt = :exp"
            ),
            ConditionExpression="attribute_exists(pk) AND #s = :processing",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":pending": "PENDING_REVIEW",
                ":processing": "PROCESSING",
                ":extracted": to_dynamo(extracted),
                ":now": iso_now(),
                ":exp": epoch_days_from_now(UPLOAD_TTL_DAYS),
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.error("save failed %s %s", prescription_id, code)
        raise
    logger.info("pending review %s medications=%s", prescription_id, len(extracted["medications"]))
    return {"status": "PENDING_REVIEW", "medicationCount": len(extracted["medications"])}
