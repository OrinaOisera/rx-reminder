"""DynamoDB helpers. Floats are converted to Decimal because DynamoDB rejects them."""

from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from common.config import table_name


def table():
    return boto3.resource("dynamodb").Table(table_name())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    # Fixed-width fractional seconds so same-second writes still sort by time.
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def epoch_days_from_now(days: int) -> int:
    return int(utc_now().timestamp()) + days * 86400


def user_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def to_dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        converted = {}
        for key, item in value.items():
            nested = to_dynamo(item)
            if nested is not None:
                converted[key] = nested
        return converted
    if isinstance(value, list):
        return [to_dynamo(item) for item in value if item is not None]
    return value


def from_dynamo(value):
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: from_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_dynamo(item) for item in value]
    return value


def query_all(pk: str, sk_prefix: str) -> list[dict]:
    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix),
    }
    while True:
        resp = table().query(**kwargs)
        items.extend(from_dynamo(item) for item in resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
