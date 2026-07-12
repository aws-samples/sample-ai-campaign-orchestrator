"""Stage 5: produce channel-specific content variants via Bedrock.

Generates one message per approved customer using the channel selected in the
predict stage. SMS variants are constrained to 160 characters, WhatsApp can be
longer with optional media markup, email is HTML.

Input event: check_safety output.
Output: same fields plus messages.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_bedrock = boto3.client("bedrock-runtime")
_model_id = os.environ["BEDROCK_MODEL_ID"]

_CHANNEL_PROMPTS = {
    "sms": (
        "Write a single SMS for the campaign brief below. Hard limit: 160 characters. "
        "No emojis, no links unless the brief explicitly includes one. Plain text only.\n\nBrief:\n{brief}"
    ),
    "whatsapp": (
        "Write a WhatsApp message for the campaign brief below. Up to 1024 characters. "
        "Friendly tone, optional emoji where natural.\n\nBrief:\n{brief}"
    ),
    "email": (
        "Write an HTML email body for the campaign brief below. Include a single <h1>, "
        "two short paragraphs, and a call-to-action link placeholder {{CTA_URL}}. No <html> "
        "or <body> wrappers.\n\nBrief:\n{brief}"
    ),
}


def _adapt(brief: str, channel: str) -> str:
    prompt = _CHANNEL_PROMPTS.get(channel, _CHANNEL_PROMPTS["sms"]).format(brief=brief)
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    response = _bedrock.invoke_model(modelId=_model_id, body=body)
    payload = json.loads(response["body"].read())
    text = payload["content"][0]["text"].strip()
    if channel == "sms" and len(text) > 160:
        text = text[:157] + "..."
    return text


def lambda_handler(event, context):
    brief = event.get("instruction", "")
    eligible_by_id = {u["customer_id"]: u for u in event["eligible_users"]}

    cache = {}
    messages = []
    for prediction in event["approved"]:
        channel = prediction["channel"]
        if channel not in cache:
            cache[channel] = _adapt(brief, channel)
        contact = eligible_by_id.get(prediction["customer_id"], {})
        messages.append(
            {
                "customer_id": prediction["customer_id"],
                "channel": channel,
                "body": cache[channel],
                "phone": contact.get("phone"),
                "email": contact.get("email"),
            }
        )

    return {**event, "messages": messages}
