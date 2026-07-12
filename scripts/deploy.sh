#!/usr/bin/env bash
# Deploy the AI-CPaaS campaign orchestrator end-to-end.
#
# Usage:
#   ./scripts/deploy.sh [--guided]
#
# First run: pass --guided so SAM prompts for stack name, region, and parameters
# and writes them to samconfig.toml. Subsequent runs reuse those values.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

if ! command -v sam >/dev/null 2>&1; then
  echo "ERROR: AWS SAM CLI not found. Install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html" >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: AWS CLI not found. Install: https://aws.amazon.com/cli/" >&2
  exit 1
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: AWS credentials are not configured. Run 'aws configure' or set env vars." >&2
  exit 1
fi

echo "==> sam build"
# Use --use-container if the local Python runtime doesn't match (3.13). Set
# SAM_USE_CONTAINER=1 to force container builds regardless.
if [[ "${SAM_USE_CONTAINER:-0}" == "1" ]] || ! command -v python3.13 >/dev/null 2>&1; then
  echo "  (using Docker container for runtime parity)"
  sam build --use-container
else
  sam build
fi

if [[ "${1:-}" == "--guided" ]] || [[ ! -f samconfig.toml ]]; then
  echo "==> sam deploy --guided"
  sam deploy --guided
else
  echo "==> sam deploy"
  sam deploy
fi

STACK_NAME="$(awk -F'=' '/stack_name/ {gsub(/[" ]/,"",$2); print $2; exit}' samconfig.toml || true)"
REGION="$(awk -F'=' '/^region/ {gsub(/[" ]/,"",$2); print $2; exit}' samconfig.toml || true)"

if [[ -n "${STACK_NAME}" && -n "${REGION}" ]]; then
  echo ""
  echo "==> Stack outputs"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table
  echo ""
  echo "Test it:"
  ENDPOINT="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`CampaignApiEndpoint`].OutputValue' --output text)"
  cat <<EOF
  curl -X POST "${ENDPOINT}" \\
    -H 'content-type: application/json' \\
    -d '{"instruction": "Send the Andaman package to Mumbai customers who haven\\u0027t booked in six months"}'
EOF
fi
