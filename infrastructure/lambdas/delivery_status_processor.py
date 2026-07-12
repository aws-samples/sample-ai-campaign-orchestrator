"""EventBridge -> Lambda: persist AWS End User Messaging delivery status events.

Triggered by SMS / WhatsApp delivery status events emitted by AWS End User
Messaging. Updates the delivery tracking table so downstream analytics can
correlate sends with their final outcomes.
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = boto3.resource("dynamodb").Table(os.environ["DELIVERY_TRACKING_TABLE"])


def lambda_handler(event, context):
    detail = event.get("detail", {})
    message_id = detail.get("messageId")
    status = detail.get("status")
    channel = detail.get("channel", "unknown")

    if not message_id:
        logger.info("Event missing messageId, skipping: %s", json.dumps(event))
        return {"statusCode": 200}

    _table.update_item(
        Key={"message_id": message_id},
        UpdateExpression="SET delivery_status = :s, updated_at = :t, channel = :c",
        ExpressionAttributeValues={
            ":s": status,
            ":t": int(time.time()),
            ":c": channel,
        },
    )
    logger.info("Updated delivery status: message_id=%s status=%s", message_id, status)
    return {"statusCode": 200}
