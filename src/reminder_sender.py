"""Invoked by EventBridge Scheduler at each dose time.

Logs the prescription id and the outcome only. A bad payload returns without
raising, because a crash would retry the same reminder and a duplicate send
is acceptable.
"""

import logging

from common.notify import send_reminder

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    prescription_id = event.get("prescriptionId") if isinstance(event, dict) else None
    med_idx = event.get("medIdx") if isinstance(event, dict) else None
    try:
        sent = send_reminder(event if isinstance(event, dict) else {})
    except Exception as exc:
        logger.error("reminder failed %s med=%s error=%s", prescription_id, med_idx, type(exc).__name__)
        return {"ok": False}
    logger.info("reminder %s %s med=%s", "sent" if sent else "skipped", prescription_id, med_idx)
    return {"ok": sent}
