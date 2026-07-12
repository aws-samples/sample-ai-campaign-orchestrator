"""API Gateway entry point: receive a campaign instruction and start the orchestrator.

POST /campaigns
{
  "instruction": "Send the Andaman package to Mumbai customers ..."
}

Returns the Step Functions execution ARN; clients poll DescribeExecution or read
the campaign-summary record from the delivery tracking table.
"""

import json
import os
import time
import uuid

import boto3

_sfn = boto3.client("stepfunctions")
_state_machine_arn = os.environ["STATE_MACHINE_ARN"]


def lambda_handler(event, context):
    body = event.get("body") or "{}"
    if isinstance(body, str):
        body = json.loads(body)

    instruction = body.get("instruction")
    if not instruction:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "instruction is required"}),
        }

    campaign_id = body.get("campaign_id") or f"campaign-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    sfn_input = {"instruction": instruction, "campaign_id": campaign_id}
    if body.get("whatsapp_template"):
        sfn_input["whatsapp_template"] = body["whatsapp_template"]

    response = _sfn.start_execution(
        stateMachineArn=_state_machine_arn,
        name=campaign_id,
        input=json.dumps(sfn_input),
    )

    return {
        "statusCode": 202,
        "body": json.dumps(
            {"campaign_id": campaign_id, "execution_arn": response["executionArn"]}
        ),
    }
