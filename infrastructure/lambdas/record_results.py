"""Stage 7: persist a campaign summary to the delivery tracking table.

Input event: deliver_messages output (or check_safety output if no eligible
customers / approved sends).

Output: {"campaign_id": "...", "summary": {...}}.
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = boto3.resource("dynamodb").Table(os.environ["DELIVERY_TRACKING_TABLE"])


def lambda_handler(event, context):
    campaign_id = event["campaign_id"]
    now = int(time.time())
    summary = {
        "campaign_id": campaign_id,
        "eligible_count": len(event.get("eligible_users", [])),
        "approved_count": len(event.get("approved", [])),
        "suppressed_count": len(event.get("suppressed", [])),
        "delivery": event.get("delivery", {}),
    }
    _table.put_item(
        Item={
            "message_id": f"campaign-summary:{campaign_id}",
            "customer_id": "campaign-summary",
            "channel": "summary",
            "campaign_id": campaign_id,
            "delivery_status": "SUMMARY",
            "timestamp": now,
            "ttl": now + 90 * 24 * 60 * 60,
            "summary": json.loads(json.dumps(summary), parse_float=Decimal),
        }
    )
    return {"campaign_id": campaign_id, "summary": summary}
