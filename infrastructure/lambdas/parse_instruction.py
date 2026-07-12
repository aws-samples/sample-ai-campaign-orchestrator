"""Stage 1: parse the marketer's natural-language instruction via Bedrock.

Input event:
    {
        "campaign_id": "...",
        "instruction": "Send the Andaman package to Mumbai customers ..."
    }

Output: same fields plus structured filters parsed by the LLM.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_bedrock = boto3.client("bedrock-runtime")
_model_id = os.environ["BEDROCK_MODEL_ID"]

_PROMPT = """You parse marketing campaign instructions into structured fields.

Instruction:
{instruction}

Return JSON with these fields:
- "sku" (string or null): product SKU or package name
- "location" (string or null): city or region
- "category" (string or null): one of exactly "Electronics", "Travel", "Apparel", "Home" (case-sensitive), or null if no category is implied
- "min_age" (integer or null), "max_age" (integer or null)
- "min_purchases" (integer or null)
- "lookback_days" (integer or null): "haven't purchased in N days" -> N
- "has_cart_items" (bool or null)
- "semantic_query" (string or null): a short free-text description of who the
  campaign should reach when the instruction implies a vibe, lifestyle, or
  affinity that can't be expressed as a hard filter (e.g. "budget-conscious
  families interested in beach vacations", "frequent business travellers",
  "tech enthusiasts who follow new launches"). Null when the instruction is
  fully captured by the structured fields above.

Output ONLY the JSON object, no prose."""


def lambda_handler(event, context):
    instruction = event["instruction"]
    campaign_id = event["campaign_id"]

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": _PROMPT.format(instruction=instruction)}],
        }
    )
    response = _bedrock.invoke_model(modelId=_model_id, body=body)
    payload = json.loads(response["body"].read())
    text = payload["content"][0]["text"].strip()

    # Claude sometimes wraps JSON in ```json fences; strip them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        filters = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Bedrock returned non-JSON, falling back to empty filters: %s", text)
        filters = {}

    result = {
        "campaign_id": campaign_id,
        "instruction": instruction,
        "filters": filters,
    }
    if event.get("whatsapp_template"):
        result["whatsapp_template"] = event["whatsapp_template"]
    return result
