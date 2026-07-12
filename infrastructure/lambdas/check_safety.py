"""Stage 4: suppress sends to fatigued or negative-sentiment customers.

Suppresses a customer if either:
- their stored sentiment_score is negative below the threshold, or
- they've already received >= MAX_MESSAGES_PER_WINDOW messages in the lookback
  window (rate limits table).

Input event: predict_channels output.
Output: same fields plus approved + suppressed lists.
"""

import logging
import os
import time
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_profiles = boto3.resource("dynamodb").Table(os.environ["CUSTOMER_PROFILES_TABLE"])
_rate_limits = boto3.resource("dynamodb").Table(os.environ["RATE_LIMITS_TABLE"])

NEGATIVE_THRESHOLD = Decimal(os.environ.get("NEGATIVE_SENTIMENT_THRESHOLD", "-0.3"))
MAX_MESSAGES_PER_WINDOW = int(os.environ.get("MAX_MESSAGES_PER_WINDOW", "5"))
WINDOW_SECONDS = int(os.environ.get("FATIGUE_WINDOW_SECONDS", str(7 * 24 * 60 * 60)))


def _is_fatigued(customer_id):
    response = _rate_limits.get_item(Key={"limiter_key": f"customer:{customer_id}"})
    item = response.get("Item")
    if not item:
        return False
    cutoff = int(time.time()) - WINDOW_SECONDS
    recent = [t for t in item.get("recent_sends", []) if int(t) >= cutoff]
    return len(recent) >= MAX_MESSAGES_PER_WINDOW


def _get_sentiment(customer_id):
    response = _profiles.get_item(
        Key={"customer_id": customer_id}, ProjectionExpression="sentiment_score"
    )
    item = response.get("Item") or {}
    return item.get("sentiment_score")


def lambda_handler(event, context):
    approved = []
    suppressed = []
    by_id = {p["customer_id"]: p for p in event["predictions"]}

    for prediction in event["predictions"]:
        customer_id = prediction["customer_id"]
        if _is_fatigued(customer_id):
            suppressed.append({"customer_id": customer_id, "reason": "frequency_fatigue"})
            continue
        sentiment = _get_sentiment(customer_id)
        if sentiment is not None and sentiment < NEGATIVE_THRESHOLD:
            suppressed.append({"customer_id": customer_id, "reason": "negative_sentiment"})
            continue
        approved.append(by_id[customer_id])

    return {**event, "approved": approved, "suppressed": suppressed}
