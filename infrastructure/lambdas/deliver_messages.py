"""Stage 6: dispatch messages via AWS End User Messaging and SES.

Each message is sent on its predicted channel. Delivery attempts are recorded
in the delivery tracking table; the AWS End User Messaging EventBridge events
update that record asynchronously via delivery_status_processor.

Idempotency
-----------
Step Functions retries this stage up to two extra times on `States.TaskFailed`
(three attempts total). To prevent double-sends on retry, we use the delivery
tracking table as a reservation ledger:

1. Before calling any provider API, we PutItem a "reservation" row keyed by
   ``send:{campaign_id}:{customer_id}:{channel}`` with a conditional expression
   that fails if the row already exists. This is atomic against concurrent
   attempts.
2. If the conditional PutItem fails, some earlier attempt already committed
   to sending to this recipient -- skip and move on.
3. If it succeeds, we call the provider API. On success we OVERWRITE the row
   with the provider-returned ``message_id`` and status ``SENT``. On failure
   we DELETE the reservation so future retries can try again.

The result: at most one send per (campaign, customer, channel), even across
Step Functions retries or overlapping Lambda invocations.
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError, ParamValidationError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sms_voice = boto3.client("pinpoint-sms-voice-v2")
_social = boto3.client("socialmessaging")
_ses = boto3.client("ses")
_tracking = boto3.resource("dynamodb").Table(os.environ["DELIVERY_TRACKING_TABLE"])
_rate_limits = boto3.resource("dynamodb").Table(os.environ["RATE_LIMITS_TABLE"])

_phone_pool_id = os.environ.get("PHONE_POOL_ID")
_sms_message_type = os.environ.get("SMS_MESSAGE_TYPE", "PROMOTIONAL")
_whatsapp_phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
_sender_email = os.environ["SENDER_EMAIL"]


def _send_sms(msg, campaign_id, whatsapp_template):
    if not _phone_pool_id:
        raise RuntimeError("SMS send requires PhonePoolId; redeploy with the parameter set.")
    response = _sms_voice.send_text_message(
        DestinationPhoneNumber=msg["phone"],
        OriginationIdentity=_phone_pool_id,
        MessageBody=msg["body"],
        MessageType=_sms_message_type,
    )
    return response["MessageId"]


def _send_whatsapp(msg, campaign_id, whatsapp_template):
    """Send a WhatsApp template message.

    Free-form text only works inside a 24-hour customer-service window. For
    business-initiated marketing we must reference an APPROVED template by
    name (and language) on the linked WABA. Caller passes whatsapp_template
    in the campaign instruction; the Bedrock-adapted body is wired into the
    template's BODY component as a single text parameter when the template
    expects one.
    """
    if not _whatsapp_phone_number_id:
        raise RuntimeError("WhatsApp send requires WhatsAppPhoneNumberId; redeploy with the parameter set.")
    if not whatsapp_template or not whatsapp_template.get("name"):
        raise RuntimeError("WhatsApp send requires whatsapp_template.name in the campaign payload")

    template_payload = {
        "name": whatsapp_template["name"],
        "language": {"code": whatsapp_template.get("language", "en_US")},
    }
    components = whatsapp_template.get("components")
    if components:
        template_payload["components"] = components

    body = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": msg["phone"],
            "type": "template",
            "template": template_payload,
        }
    )
    response = _social.send_whatsapp_message(
        originationPhoneNumberId=_whatsapp_phone_number_id,
        message=body.encode("utf-8"),
        metaApiVersion="v20.0",
    )
    return response["messageId"]


def _send_email(msg, campaign_id, whatsapp_template):
    response = _ses.send_email(
        Source=_sender_email,
        Destination={"ToAddresses": [msg["email"]]},
        Message={
            "Subject": {"Data": f"Campaign {campaign_id}"},
            "Body": {"Html": {"Data": msg["body"]}},
        },
    )
    return response["MessageId"]


def _reservation_id(campaign_id, customer_id, channel):
    return f"send:{campaign_id}:{customer_id}:{channel}"


def _reserve(campaign_id, customer_id, channel):
    """Try to atomically claim the (campaign, customer, channel) slot.

    Returns True if we won the reservation, False if a prior attempt already
    committed to sending to this recipient (skip case).
    """
    now = int(time.time())
    try:
        _tracking.put_item(
            Item={
                "message_id": _reservation_id(campaign_id, customer_id, channel),
                "customer_id": customer_id,
                "channel": channel,
                "campaign_id": campaign_id,
                "delivery_status": "RESERVED",
                "timestamp": now,
                "ttl": now + 90 * 24 * 60 * 60,
            },
            ConditionExpression="attribute_not_exists(message_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _finalize_send(message_id, customer_id, channel, campaign_id):
    """Overwrite the reservation row with the real provider message id and
    status SENT, then bump the customer's fatigue counter."""
    now = int(time.time())
    _tracking.put_item(
        Item={
            "message_id": _reservation_id(campaign_id, customer_id, channel),
            "provider_message_id": message_id,
            "customer_id": customer_id,
            "channel": channel,
            "campaign_id": campaign_id,
            "delivery_status": "SENT",
            "timestamp": now,
            "ttl": now + 90 * 24 * 60 * 60,
        }
    )
    _rate_limits.update_item(
        Key={"limiter_key": f"customer:{customer_id}"},
        UpdateExpression="SET recent_sends = list_append(if_not_exists(recent_sends, :empty), :ts), #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":empty": [],
            ":ts": [Decimal(now)],
            ":ttl": now + 30 * 24 * 60 * 60,
        },
    )


def _release_reservation(campaign_id, customer_id, channel):
    """Clear the reservation so a future retry can attempt this recipient."""
    try:
        _tracking.delete_item(
            Key={"message_id": _reservation_id(campaign_id, customer_id, channel)}
        )
    except ClientError:
        logger.exception("Failed to release reservation for %s / %s / %s",
                         campaign_id, customer_id, channel)


def lambda_handler(event, context):
    campaign_id = event["campaign_id"]
    whatsapp_template = event.get("whatsapp_template")
    summary = {"sent": 0, "failed": 0, "skipped_duplicate": 0, "by_channel": {"sms": {"sent": 0, "failed": 0}, "whatsapp": {"sent": 0, "failed": 0}, "email": {"sent": 0, "failed": 0}}}

    senders = {"sms": _send_sms, "whatsapp": _send_whatsapp, "email": _send_email}

    for msg in event["messages"]:
        channel = msg["channel"]
        customer_id = msg["customer_id"]
        sender = senders.get(channel)
        if not sender:
            logger.warning("Unknown channel %s for customer %s", channel, customer_id)
            continue

        # Idempotency: reserve BEFORE calling the provider API. On a Step
        # Functions retry, the reservation from the previous attempt is still
        # in the table, so we skip this recipient.
        if not _reserve(campaign_id, customer_id, channel):
            logger.info("Skipping already-sent %s / %s / %s", campaign_id, customer_id, channel)
            summary["skipped_duplicate"] += 1
            continue

        try:
            message_id = sender(msg, campaign_id, whatsapp_template)
            _finalize_send(message_id, customer_id, channel, campaign_id)
            summary["sent"] += 1
            summary["by_channel"][channel]["sent"] += 1
        except (ClientError, RuntimeError, ParamValidationError) as e:
            logger.exception("Send failed for %s on %s: %s", customer_id, channel, e)
            # Roll back the reservation so a subsequent attempt can retry.
            _release_reservation(campaign_id, customer_id, channel)
            summary["failed"] += 1
            summary["by_channel"][channel]["failed"] += 1

    return {**event, "delivery": summary}
