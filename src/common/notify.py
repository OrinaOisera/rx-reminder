"""Email and SMS reminder text.

Subjects and SMS are ASCII and single-line. The email body may use the
patient's name and colour emoji. Patient names are never written to logs.
"""

import hashlib
import re
import unicodedata

import boto3

from common.config import aws_region
from common.palette import get_color
from common.patients import strip_controls

_SMS_TAIL = " Aid only, follow your prescription."


def topic_name(user_id: str, email: str) -> str:
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:8]
    return f"rx-{user_id}-{digest}"


def ensure_email_topic(user_id: str, email: str) -> str:
    """One topic per destination address, so recipients do not share a feed."""
    sns = boto3.client("sns", region_name=aws_region())
    created = sns.create_topic(Name=topic_name(user_id, email))
    arn = created["TopicArn"]
    sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=email)
    return arn


def send_reminder(payload: dict) -> bool:
    """Missing optional fields are skipped. A failure is raised to the caller."""
    if not isinstance(payload, dict):
        return False
    channel = payload.get("channel") or "email"
    patient = _single_line(payload.get("patientName"))
    medicine = _single_line(payload.get("name")) or "medicine"
    dose = _single_line(payload.get("doseText"))
    instruction = _single_line(payload.get("instructions"))
    destination = _single_line(payload.get("destination"))
    sns = boto3.client("sns", region_name=aws_region())
    if channel == "sms":
        if not destination:
            return False
        sns.publish(
            PhoneNumber=destination,
            Message=sms_body(patient, medicine, dose, instruction),
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}
            },
        )
        return True
    topic = payload.get("topicArn")
    if not isinstance(topic, str) or not topic:
        return False
    color = get_color(payload.get("colorKey") or "") or {}
    sns.publish(
        TopicArn=topic,
        Subject=email_subject(patient, medicine),
        Message=email_body(patient or "Patient", color.get("emoji") or "", medicine, dose, instruction),
    )
    return True


def sms_body(patient: str, medicine: str, dose: str, instruction: str) -> str:
    who = to_ascii(patient) or "Patient"
    med = to_ascii(medicine) or "medicine"
    dose_ascii = to_ascii(dose)
    note = to_ascii(instruction)
    head = f"{who} - take {med}"
    if dose_ascii:
        head += f" {dose_ascii}"
    head += "."
    if len(head) + len(_SMS_TAIL) > 160:
        head = head[: 160 - len(_SMS_TAIL)].rstrip()
    room = 160 - len(head) - len(_SMS_TAIL)
    if note and room > 2:
        note = note[: room - 2].rstrip()
        return f"{head} {note}.{_SMS_TAIL}"
    return head + _SMS_TAIL


def email_subject(patient: str, medicine: str) -> str:
    text = f"Reminder for {to_ascii(patient) or 'Patient'}: {to_ascii(medicine) or 'medicine'}"
    text = " ".join(text.split())
    return text[:99]


def email_body(patient: str, emoji: str, medicine: str, dose: str, instruction: str) -> str:
    line = f"{emoji} {patient} - take {medicine}".strip()
    if dose:
        line += f" {dose}"
    line += "."
    chunks = [line]
    if instruction:
        chunks.append(instruction + ".")
    chunks.append("Reminder aid only - follow your prescription.")
    return " ".join(chunks)


def to_ascii(value) -> str:
    text = _single_line(value)
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r" {2,}", " ", folded).strip()


def _single_line(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r" {2,}", " ", strip_controls(value)).strip()
