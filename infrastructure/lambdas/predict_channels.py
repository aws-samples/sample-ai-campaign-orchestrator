"""Stage 3: predict the best channel per customer.

If a SageMaker endpoint name is set, invoke it; otherwise fall back to the
customer's stored preferred_channel (or SMS).

Input event: segment_customers output.
Output: same fields plus predictions.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sagemaker_endpoint = os.environ.get("SAGEMAKER_ENDPOINT_NAME")
_runtime = boto3.client("sagemaker-runtime") if _sagemaker_endpoint else None


def _predict_via_sagemaker(customer):
    response = _runtime.invoke_endpoint(
        EndpointName=_sagemaker_endpoint,
        ContentType="application/json",
        Body=json.dumps({"customer_id": customer["customer_id"]}),
    )
    body = json.loads(response["Body"].read())
    return body.get("channel")


def lambda_handler(event, context):
    predictions = []
    for user in event["eligible_users"]:
        channel = None
        if _runtime:
            try:
                channel = _predict_via_sagemaker(user)
            except Exception as e:
                logger.warning("SageMaker invocation failed, falling back: %s", e)
        if not channel:
            channel = user.get("preferred_channel") or "sms"
        predictions.append({"customer_id": user["customer_id"], "channel": channel})

    return {**event, "predictions": predictions}
