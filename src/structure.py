"""Turn OCR text into the medicine JSON with the Bedrock Converse API.

Logs the line count, medicine count, and latency only. The OCR text and the
model reply are never logged. A patient name is not added to the request.
"""

import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

from common.config import aws_region, bedrock_model_id
from common.models import normalize_extracted
from common.prompts import REPAIR_MESSAGE, SYSTEM_PROMPT

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ThrottlingError(Exception):
    """Retried by the state machine."""


class StructuringError(Exception):
    """The model did not return usable JSON. Not retried as throttling."""


class BedrockAccessError(Exception):
    """The configured model cannot be called from this account."""


def handler(event, context):
    started = time.perf_counter()
    ocr = event.get("ocr") if isinstance(event, dict) else None
    text = ocr.get("text") if isinstance(ocr, dict) else ""
    if not isinstance(text, str):
        text = ""
    line_count = ocr.get("lineCount") if isinstance(ocr, dict) else None
    extracted = structure_ocr(text)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "structured lineCount=%s medications=%s latencyMs=%s",
        line_count,
        len(extracted["medications"]),
        elapsed_ms,
    )
    return extracted


def structure_ocr(text: str) -> dict:
    client = boto3.client("bedrock-runtime", region_name=aws_region())

    def call_model(repair: bool, previous: str | None) -> str:
        messages = [{"role": "user", "content": [{"text": text}]}]
        if repair:
            messages.append({"role": "assistant", "content": [{"text": previous or ""}]})
            messages.append({"role": "user", "content": [{"text": REPAIR_MESSAGE}]})
        return converse(client, messages)

    return structure_with(call_model)


def structure_with(call_model) -> dict:
    """One repair attempt when the reply is not JSON. Throttling is not caught here."""
    first = call_model(False, None)
    try:
        return normalize_extracted(parse_model_json(first))
    except (StructuringError, json.JSONDecodeError, ValueError):
        second = call_model(True, first)
    try:
        return normalize_extracted(parse_model_json(second))
    except (StructuringError, json.JSONDecodeError, ValueError) as exc:
        raise StructuringError("invalid json") from exc


def converse(client, messages) -> str:
    try:
        response = client.converse(
            modelId=bedrock_model_id(),
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            inferenceConfig={"temperature": 0, "maxTokens": 1500},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.error("bedrock error %s", code)
        raise_for_bedrock(code, exc)
    parts = ((response.get("output") or {}).get("message") or {}).get("content") or []
    chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "\n".join(chunks)


def raise_for_bedrock(code: str, exc: Exception) -> None:
    if code in {"ThrottlingException", "TooManyRequestsException"}:
        raise ThrottlingError("bedrock throttled") from exc
    if code in {"AccessDeniedException", "ResourceNotFoundException", "ValidationException"}:
        raise BedrockAccessError("bedrock model is not available") from exc
    raise exc


def parse_model_json(raw: str) -> dict:
    if not isinstance(raw, str):
        raise StructuringError("invalid json")
    text = _strip_fences(raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _load_first_object(text)
    if not isinstance(data, dict):
        raise StructuringError("invalid json")
    return data


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    first_break = text.find("\n")
    if first_break == -1:
        return text
    body = text[first_break + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _load_first_object(text: str) -> dict:
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start < 0:
            raise StructuringError("invalid json")
        snippet = _balanced_object(text, start)
        if snippet is None:
            raise StructuringError("invalid json")
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError:
            search_from = start + 1
            continue
        if isinstance(data, dict):
            return data
        search_from = start + 1


def _balanced_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
