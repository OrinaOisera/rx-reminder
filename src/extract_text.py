"""Textract OCR for one prescription photo.

Returns the lines, how many there were, and the average confidence.
Textract Blocks are not returned: they can exceed the Step Functions limit.
The text is passed to the next state and is not written to CloudWatch.
"""

import logging
import re

import boto3

from common.config import aws_region, bucket_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_KEY = re.compile(r"^uploads/[0-9a-f]{12}/[0-9a-f]{12}\.(jpg|png)$")


def handler(event, context):
    bucket = event.get("bucket") if isinstance(event, dict) else None
    key = event.get("key") if isinstance(event, dict) else None
    if bucket != bucket_name() or not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError("unexpected image location")
    result = boto3.client("textract", region_name=aws_region()).detect_document_text(
        Document={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    summary = summarize_blocks(result.get("Blocks") or [])
    logger.info("ocr lines=%s avgConfidence=%s", summary["lineCount"], summary["avgConfidence"])
    return summary


def summarize_blocks(blocks) -> dict:
    """Keep LINE text only. WORD and PAGE blocks stay out of the state payload."""
    lines = []
    scores = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("BlockType") != "LINE":
            continue
        text = block.get("Text")
        lines.append(text if isinstance(text, str) else "")
        confidence = block.get("Confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            scores.append(float(confidence))
    average = round(sum(scores) / len(scores), 1) if scores else 0
    return {"text": "\n".join(lines), "lineCount": len(lines), "avgConfidence": average}
