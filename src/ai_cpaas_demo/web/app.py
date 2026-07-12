"""Local Flask UI for the AI-CPaaS campaign orchestrator.

Talks to the deployed AWS stack:
- POST /api/intelligent-query → starts a Step Functions execution
- GET  /api/campaign-status/<id> → reads the campaign summary from DynamoDB
- GET  /api/whatsapp-templates → lists APPROVED templates on the linked WABA
- GET  /api/environment → reports region / production flag (used by index.html)

Required env vars:
    AWS_REGION                      (e.g. ap-south-1)
    CAMPAIGN_STATE_MACHINE_ARN      from the SAM stack output
    DELIVERY_TRACKING_TABLE         from the SAM stack output
    WABA_ID                         (e.g. waba-xxxxxxxxxxxx), enables the
                                    WhatsApp template dropdown

Run via ``./start.sh`` (gunicorn on :8080) or ``python -m flask --app
ai_cpaas_demo.web.app run`` for development.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
STATE_MACHINE_ARN = os.getenv("CAMPAIGN_STATE_MACHINE_ARN")
DELIVERY_TRACKING_TABLE = os.getenv("DELIVERY_TRACKING_TABLE")
WABA_ID = os.getenv("WABA_ID")
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"

_sfn = boto3.client("stepfunctions", region_name=AWS_REGION) if STATE_MACHINE_ARN else None
_tracking_table = (
    boto3.resource("dynamodb", region_name=AWS_REGION).Table(DELIVERY_TRACKING_TABLE)
    if DELIVERY_TRACKING_TABLE
    else None
)
_social = boto3.client("socialmessaging", region_name=AWS_REGION) if WABA_ID else None

app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/environment", methods=["GET"])
def get_environment():
    return jsonify(
        {
            "region": AWS_REGION,
            "is_production": IS_PRODUCTION,
            "state_machine_configured": bool(STATE_MACHINE_ARN),
            "waba_configured": bool(WABA_ID),
        }
    )


@app.route("/api/intelligent-query", methods=["POST"])
def run_intelligent_query():
    """Launch a campaign by starting the Step Functions orchestrator."""
    if not _sfn or not STATE_MACHINE_ARN:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "CAMPAIGN_STATE_MACHINE_ARN is not set. Configure it from the SAM stack output.",
                }
            ),
            503,
        )

    body = request.get_json(silent=True) or {}
    instruction = (body.get("query") or body.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"success": False, "error": "query is required"}), 400

    campaign_id = body.get("campaign_id") or f"campaign-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    sfn_input = {"campaign_id": campaign_id, "instruction": instruction}
    if body.get("whatsapp_template"):
        sfn_input["whatsapp_template"] = body["whatsapp_template"]

    try:
        response = _sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=campaign_id,
            input=json.dumps(sfn_input),
        )
    except ClientError as e:
        logger.exception("start_execution failed")
        return jsonify({"success": False, "error": str(e)}), 500

    return (
        jsonify(
            {
                "success": True,
                "campaign_id": campaign_id,
                "execution_arn": response["executionArn"],
            }
        ),
        202,
    )


@app.route("/api/campaign-status/<campaign_id>", methods=["GET"])
def campaign_status(campaign_id):
    """Return the campaign summary, or 202 if the orchestrator is still running."""
    if not _tracking_table:
        return (
            jsonify({"success": False, "error": "DELIVERY_TRACKING_TABLE is not set"}),
            503,
        )

    response = _tracking_table.get_item(Key={"message_id": f"campaign-summary:{campaign_id}"})
    item = response.get("Item")
    if not item:
        return jsonify({"campaign_id": campaign_id, "status": "running"}), 202

    return jsonify(
        {
            "campaign_id": campaign_id,
            "status": "complete",
            "summary": json.loads(json.dumps(item.get("summary"), default=str)),
        }
    )


@app.route("/api/whatsapp-templates", methods=["GET"])
def get_whatsapp_templates():
    """Return APPROVED WhatsApp templates on the linked WABA."""
    if not _social or not WABA_ID:
        return (
            jsonify(
                {
                    "success": False,
                    "templates": [],
                    "error": "WABA_ID is not configured. Find one with: "
                    "aws socialmessaging list-linked-whatsapp-business-accounts",
                }
            ),
            400,
        )

    try:
        response = _social.list_whatsapp_message_templates(id=WABA_ID)
    except NoCredentialsError:
        return jsonify({"success": False, "templates": [], "error": "AWS credentials not configured"}), 401
    except ClientError as e:
        logger.exception("list_whatsapp_message_templates failed")
        return jsonify({"success": False, "templates": [], "error": str(e)}), 500

    approved = [
        {
            "template_name": t.get("templateName"),
            "category": t.get("templateCategory"),
            "language": t.get("templateLanguage", "en"),
            "meta_id": t.get("metaTemplateId"),
        }
        for t in response.get("templates", [])
        if t.get("templateStatus") == "APPROVED"
    ]
    return jsonify({"success": True, "templates": approved, "total": len(approved)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
