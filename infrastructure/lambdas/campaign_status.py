"""GET /campaigns/{campaign_id} — return the campaign summary from DynamoDB.

If the summary item doesn't exist yet, returns 202 (still running).
"""

import json
import logging
import os
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = boto3.resource("dynamodb").Table(os.environ["DELIVERY_TRACKING_TABLE"])


def _decimal_default(o):
    if isinstance(o, Decimal):
        return float(o) if o % 1 else int(o)
    raise TypeError


def lambda_handler(event, context):
    campaign_id = event["pathParameters"]["campaign_id"]
    response = _table.get_item(Key={"message_id": f"campaign-summary:{campaign_id}"})
    item = response.get("Item")
    if not item:
        return {
            "statusCode": 202,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"campaign_id": campaign_id, "status": "running"}),
        }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {"campaign_id": campaign_id, "status": "complete", "summary": item.get("summary")},
            default=_decimal_default,
        ),
    }
