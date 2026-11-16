"""Environment variables and constants. Nothing here is an account id or secret literal."""

import os

MAX_BODY_BYTES = 20 * 1024
MAX_PATIENTS = 8
PRESIGN_EXPIRES_SECONDS = 300
CALENDAR_URL_SECONDS = 3600
UPLOAD_TTL_DAYS = 7
NAME_MAX = 40
RELATIONSHIP_MAX = 30


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable {name}")
    return value


def table_name() -> str:
    return required("TABLE_NAME")


def bucket_name() -> str:
    return required("BUCKET_NAME")


def api_key() -> str:
    key = required("API_KEY")
    if len(key) < 16:
        raise RuntimeError("API_KEY must be at least 16 characters")
    return key


def aws_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def default_timezone() -> str:
    return os.environ.get("DEFAULT_TIMEZONE", "Africa/Nairobi")


def bedrock_model_id() -> str:
    return os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")


def scheduler_role_arn() -> str:
    return required("SCHEDULER_ROLE_ARN")


def reminder_function_arn() -> str:
    return required("REMINDER_FUNCTION_ARN")
