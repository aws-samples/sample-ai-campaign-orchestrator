"""Seed the customer profiles table (and optionally the OpenSearch vector index) for the demo.

Usage:
    python scripts/seed_demo_data.py [--count 100]
    python scripts/seed_demo_data.py --whatsapp-recipient +911234567890

The first form writes synthetic profiles so the orchestrator has something to
segment. The second adds (or updates) one real customer in Mumbai with
preferred_channel=whatsapp so a campaign with --whatsapp-recipient will exercise
the WhatsApp path end-to-end against a phone you actually own.

When the deployed stack has EnableSemanticSearch=true (i.e. has a
VectorCollectionEndpoint output), the seeder also embeds each profile's natural
language description with Titan and indexes it into the collection so
``segment_customers`` can do k-NN search.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

import boto3

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

LOCATIONS = ["Mumbai", "Bangalore", "Delhi", "Chennai", "Hyderabad", "Pune"]
CATEGORIES = ["Electronics", "Travel", "Apparel", "Home"]
SKUS = {
    "Travel": ["SKU-ANDAMAN-001", "SKU-GOA-002", "SKU-LADAKH-003"],
    "Electronics": ["SKU-LAPTOP-001", "SKU-PHONE-002", "SKU-CAMERA-006"],
    "Apparel": ["SKU-SHIRT-010", "SKU-SHOE-011"],
    "Home": ["SKU-SOFA-020", "SKU-LAMP-021"],
}
CHANNELS = ["sms", "whatsapp", "email"]

# Lifestyle tags give each profile a semantic fingerprint that Titan embeddings
# can separate. Picked from a small pool so a k-NN query for "budget-conscious
# families" actually matches a recognizable subset.
LIFESTYLES = [
    "budget-conscious family, kid-friendly travel, weekend getaways",
    "luxury traveler, premium experiences, fine dining",
    "tech enthusiast, follows new gadget launches, early adopter",
    "frequent business traveler, hotel loyalty member, airport lounges",
    "beach vacation lover, coastal trips, water sports",
    "adventure seeker, trekking, mountain destinations",
    "wellness-focused, yoga retreats, mindfulness, spas",
    "remote-working professional, urban explorer, co-working spaces",
    "value shopper, deal hunter, abandoned-cart redeemer",
    "fashion-forward, seasonal collections, premium apparel",
]


def _parse_samconfig(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} not found. Deploy the stack first: ./scripts/deploy.sh")
    config: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*([a-z_]+)\s*=\s*"([^"]+)"\s*$', line)
        if match:
            config.setdefault(match.group(1), match.group(2))
    return config


def _get_stack_outputs(cfn, stack_name: str) -> dict:
    response = cfn.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in response["Stacks"][0]["Outputs"]}


def _profile_description(profile: dict) -> str:
    return (
        f"{profile['lifestyle']}. Based in {profile['location']}, age {profile['age']}, "
        f"{int(profile['total_purchases'])} prior purchases, prefers {profile['category']}."
    )


def _make_profile(i: int) -> dict:
    category = random.choice(CATEGORIES)
    return {
        "customer_id": f"cust-{i:05d}",
        "phone": f"+1555{random.randint(1000000, 9999999)}",
        "email": f"customer{i:05d}@example.com",
        "location": random.choice(LOCATIONS),
        "category": category,
        "preferred_sku": random.choice(SKUS[category]),
        "lifestyle": random.choice(LIFESTYLES),
        "age": random.randint(22, 65),
        "total_purchases": random.randint(0, 20),
        "cart_item_count": random.randint(0, 5),
        "preferred_channel": random.choice(CHANNELS),
        "sentiment_score": Decimal(str(round(random.uniform(-0.5, 0.9), 2))),
    }


def _make_whatsapp_recipient(phone: str, email: str | None) -> dict:
    if not phone.startswith("+"):
        raise SystemExit("--whatsapp-recipient must be in E.164 form (e.g. +911234567890)")
    return {
        "customer_id": "cust-whatsapp-demo",
        "phone": phone,
        "email": email or "demo@example.com",
        "location": "Mumbai",
        "category": "Travel",
        "preferred_sku": "SKU-ANDAMAN-001",
        "lifestyle": "budget-conscious family, kid-friendly travel, weekend getaways",
        "age": 32,
        "total_purchases": 3,
        "cart_item_count": 1,
        "preferred_channel": "whatsapp",
        "sentiment_score": Decimal("0.5"),
    }


def _build_opensearch_client(endpoint: str, region: str):
    try:
        from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
    except ImportError as e:
        raise SystemExit(
            "opensearch-py is required for semantic search seeding. "
            "Install: pip install opensearch-py>=2.4"
        ) from e

    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=60,
    )


def _embed(bedrock, model_id: str, text: str) -> list[float]:
    response = bedrock.invoke_model(modelId=model_id, body=json.dumps({"inputText": text}))
    return json.loads(response["body"].read())["embedding"]


def _retry(fn, *, attempts=6, wait=15, label="opensearch"):
    """OpenSearch Serverless NextGen returns 403 / read-timeout while OCUs warm up
    from idle. Retry briefly."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            logger.info("%s attempt %d/%d failed (%s); retrying in %ds", label, i + 1, attempts, type(e).__name__, wait)
            time.sleep(wait)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100, help="Number of synthetic profiles to seed")
    parser.add_argument("--whatsapp-recipient", help="E.164 phone for one Mumbai customer with channel=whatsapp")
    parser.add_argument("--recipient-email", help="Email for the WhatsApp recipient row")
    parser.add_argument("--no-synthetic", action="store_true", help="Skip the synthetic batch")
    parser.add_argument("--no-vectors", action="store_true",
                        help="Skip OpenSearch indexing even if the stack has it enabled")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    config = _parse_samconfig(repo_root / "samconfig.toml")
    stack_name = config.get("stack_name")
    region = config.get("region")
    if not stack_name or not region:
        raise SystemExit("samconfig.toml is missing stack_name or region")

    cfn = boto3.client("cloudformation", region_name=region)
    outputs = _get_stack_outputs(cfn, stack_name)
    table_name = outputs["CustomerProfilesTableName"]
    vector_endpoint = outputs.get("VectorCollectionEndpoint") if not args.no_vectors else None
    vector_index = outputs.get("VectorIndexName") if not args.no_vectors else None
    embedding_model = "amazon.titan-embed-text-v2:0"

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    profiles: list[dict] = []
    if not args.no_synthetic:
        profiles.extend(_make_profile(i) for i in range(args.count))
    if args.whatsapp_recipient:
        profiles.append(_make_whatsapp_recipient(args.whatsapp_recipient, args.recipient_email))

    logger.info("Writing %d profiles to DynamoDB %s (%s)", len(profiles), table_name, region)
    with table.batch_writer() as batch:
        for item in profiles:
            batch.put_item(Item=item)

    if vector_endpoint and vector_index:
        logger.info("Indexing %d profiles into OpenSearch %s ...", len(profiles), vector_index)
        client = _build_opensearch_client(vector_endpoint, region)
        bedrock = boto3.client("bedrock-runtime", region_name=region)

        # Index settings: enable k-NN; mapping defines a 1024-dim FAISS hnsw field.
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "customer_id": {"type": "keyword"},
                    "description": {"type": "text"},
                    "profile_vector": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {"name": "hnsw", "engine": "faiss"},
                    },
                }
            },
        }

        def _ensure_index():
            if not client.indices.exists(index=vector_index):
                client.indices.create(index=vector_index, body=mapping)

        _retry(_ensure_index, label="ensure index")

        for item in profiles:
            description = _profile_description(item)
            embedding = _embed(bedrock, embedding_model, description)
            doc = {"customer_id": item["customer_id"], "description": description, "profile_vector": embedding}
            _retry(lambda d=doc: client.index(index=vector_index, body=d), attempts=3, wait=5, label="index doc")

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
