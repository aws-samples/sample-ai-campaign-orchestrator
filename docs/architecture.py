"""Architecture diagram for the campaign orchestrator blog post.

Layout: two horizontal lanes that the reader scans top-to-bottom.

  Lane 1 (top):    request flow                                        delivery
                   marketer -> API GW -> Launcher -> [seven stages] -> customer

  Lane 2 (below):  the services each stage calls
                   Bedrock | DynamoDB profiles | rate limits | OpenSearch |
                   DynamoDB delivery tracking | End User Messaging + SES

Each stage drops a single short arrow straight down to the service it uses;
the pipeline itself stays a clean horizontal chain.

Run:
    pip install diagrams
    brew install graphviz                 # or apt-get install graphviz
    python docs/architecture.py
"""

from pathlib import Path

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.engagement import Pinpoint, SimpleEmailServiceSes
from diagrams.aws.general import User
from diagrams.aws.integration import StepFunctions
from diagrams.aws.ml import Bedrock
from diagrams.aws.mobile import APIGateway
from diagrams.aws.analytics import AmazonOpensearchService


OUT = Path(__file__).resolve().parent / "architecture"

GRAPH_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "22",
    "splines": "ortho",      # right-angle connectors keep lanes clean
    "pad": "0.6",
    "nodesep": "0.4",
    "ranksep": "1.0",
    "rankdir": "LR",
    "newrank": "true",
}
NODE_ATTR = {"fontname": "Helvetica", "fontsize": "14", "imagepos": "tc"}
EDGE_ATTR = {"fontname": "Helvetica", "fontsize": "12", "color": "#545B64", "penwidth": "1.4"}


with Diagram(
    "AI-powered campaign orchestrator",
    filename=str(OUT),
    show=False,
    direction="LR",
    outformat="png",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    # --- Top lane: request flow ---
    marketer = User("Marketer")
    api = APIGateway("API Gateway")
    launcher = Lambda("Launcher")

    with Cluster("AWS Step Functions  (state machine)"):
        parse = Lambda("Parse")
        segment = Lambda("Segment")
        predict = Lambda("Predict")
        safety = Lambda("Safety")
        adapt = Lambda("Adapt")
        deliver = Lambda("Deliver")
        record = Lambda("Record")
        parse >> segment >> predict >> safety >> adapt >> deliver >> record

    customer = User("Customer")

    # --- Bottom lane: services each stage calls ---
    with Cluster("AWS service backend"):
        bedrock = Bedrock("Amazon Bedrock\n(Claude + Titan)")
        profiles = Dynamodb("DynamoDB:\nProfiles")
        rate_limits = Dynamodb("DynamoDB:\nRate limits")
        opensearch = AmazonOpensearchService("OpenSearch\nServerless\n(optional)")
        tracking = Dynamodb("DynamoDB:\nDelivery\ntracking")
        eum = Pinpoint("End User\nMessaging")
        ses = SimpleEmailServiceSes("Amazon SES")

    # --- Request entry ---
    marketer >> Edge(label="POST /campaigns") >> api >> launcher >> Edge(label="StartExecution") >> parse

    # --- Each stage drops one short vertical arrow to its service ---
    parse >> Edge(style="dashed") >> bedrock
    segment >> Edge(style="dashed") >> profiles
    segment >> Edge(style="dashed", label="optional") >> opensearch
    safety >> Edge(style="dashed") >> rate_limits
    adapt >> Edge(style="dashed") >> bedrock
    deliver >> Edge(style="dashed") >> eum
    deliver >> Edge(style="dashed") >> ses
    record >> Edge(style="dashed") >> tracking

    # --- Delivery ---
    eum >> Edge(label="SMS / WhatsApp") >> customer
    ses >> Edge(label="email") >> customer
