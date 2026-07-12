"""Stage 2: resolve filters into a list of eligible customers.

Strategy:
- Always run the DynamoDB filter (location, age, category, etc.) against the
  customer profiles table.
- If the parser emitted a ``semantic_query`` AND an OpenSearch Serverless
  vector collection is configured, also embed the query with Titan and do a
  k-NN search. The final segment is the intersection of both sets when both
  run; otherwise whichever produced results.

Input event: parse_instruction output (with optional ``filters.semantic_query``).
Output: same fields plus ``eligible_users``.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = boto3.resource("dynamodb").Table(os.environ["CUSTOMER_PROFILES_TABLE"])
_region = os.environ.get("AWS_REGION", "us-east-1")
_vector_endpoint = os.environ.get("VECTOR_COLLECTION_ENDPOINT") or ""
_vector_index = os.environ.get("VECTOR_INDEX_NAME") or ""
_embedding_model = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

_bedrock = boto3.client("bedrock-runtime", region_name=_region) if _vector_endpoint else None
_opensearch_client = None


def _opensearch():
    global _opensearch_client
    if _opensearch_client is not None:
        return _opensearch_client
    from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

    host = _vector_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), _region, "aoss")
    _opensearch_client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    return _opensearch_client


def _build_filter(filters):
    expr = None

    def _and(part):
        nonlocal expr
        expr = part if expr is None else expr & part

    if filters.get("location"):
        _and(Attr("location").eq(filters["location"]))
    if filters.get("category"):
        _and(Attr("category").eq(filters["category"]))
    if filters.get("min_age") is not None:
        _and(Attr("age").gte(filters["min_age"]))
    if filters.get("max_age") is not None:
        _and(Attr("age").lte(filters["max_age"]))
    if filters.get("min_purchases") is not None:
        _and(Attr("total_purchases").gte(filters["min_purchases"]))
    if filters.get("has_cart_items") is True:
        _and(Attr("cart_item_count").gt(0))
    return expr


def _dynamodb_segment(filters):
    expr = _build_filter(filters)
    scan_kwargs = {}
    if expr is not None:
        scan_kwargs["FilterExpression"] = expr

    items = []
    response = _table.scan(**scan_kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = _table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
    return items


def _embed(text: str) -> list[float]:
    response = _bedrock.invoke_model(
        modelId=_embedding_model, body=json.dumps({"inputText": text})
    )
    return json.loads(response["body"].read())["embedding"]


def _vector_segment(semantic_query: str, top_k: int = 50) -> set[str]:
    embedding = _embed(semantic_query)
    body = {
        "size": top_k,
        "_source": ["customer_id"],
        "query": {"knn": {"profile_vector": {"vector": embedding, "k": top_k}}},
    }
    result = _opensearch().search(index=_vector_index, body=body)
    return {h["_source"]["customer_id"] for h in result.get("hits", {}).get("hits", [])}


def lambda_handler(event, context):
    filters = event.get("filters") or {}
    semantic_query = (filters.get("semantic_query") or "").strip()

    dynamo_items = _dynamodb_segment(filters)

    if semantic_query and _vector_endpoint and _vector_index and _bedrock:
        try:
            matched_ids = _vector_segment(semantic_query)
            logger.info("vector search matched %d customers for: %r", len(matched_ids), semantic_query)
            dynamo_items = [c for c in dynamo_items if c["customer_id"] in matched_ids]
        except Exception:
            logger.exception("vector search failed; falling back to DynamoDB-only segment")

    return {
        **event,
        "eligible_users": [
            {
                "customer_id": c["customer_id"],
                "phone": c.get("phone"),
                "email": c.get("email"),
                "preferred_channel": c.get("preferred_channel"),
            }
            for c in dynamo_items
        ],
    }
