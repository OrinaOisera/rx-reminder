"""HTTP helpers for the API Lambda. Responses never include stack traces."""

import base64
import hashlib
import hmac
import json

from common.config import MAX_BODY_BYTES, api_key


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_CORS = {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type,x-api-key",
    "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
}


def response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": dict(_CORS),
        "body": json.dumps(body, ensure_ascii=False),
    }


def error(status: int, message: str) -> dict:
    return response(status, {"error": message})


def header(event: dict, name: str) -> str | None:
    headers = event.get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value if isinstance(value, str) else None
    return None


def authorize(event: dict) -> dict | None:
    provided = header(event, "x-api-key")
    try:
        expected = api_key()
    except RuntimeError:
        return error(500, "Server is not configured")
    if not isinstance(provided, str) or not hmac.compare_digest(provided, expected):
        return error(401, "Unauthorized")
    return None


def current_user_id() -> str:
    """Demo auth: one API key is one user. Production would use Cognito."""
    return hashlib.sha256(api_key().encode("utf-8")).hexdigest()[:12]


def normalize_path(event: dict) -> str:
    path = event.get("rawPath") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if isinstance(stage, str) and stage and stage != "$default":
        prefix = "/" + stage
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix) :]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def parse_body(event: dict) -> dict:
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise RequestError(400, "Request body must be JSON")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestError(400, "Request body is not valid text") from exc
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise RequestError(413, "Request body is too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestError(400, "Request body must be JSON") from exc
    if not isinstance(data, dict):
        raise RequestError(400, "Request body must be a JSON object")
    return data
