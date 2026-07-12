#!/usr/bin/env bash
# Tear down all AWS resources created by deploy.sh.
#
# Empties the campaign assets S3 bucket (versions + delete markers) before
# stack deletion so CloudFormation can drop the bucket cleanly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

if [[ ! -f samconfig.toml ]]; then
  echo "ERROR: samconfig.toml not found. Did you run ./scripts/deploy.sh first?" >&2
  exit 1
fi

STACK_NAME="$(awk -F'=' '/stack_name/ {gsub(/[" ]/,"",$2); print $2; exit}' samconfig.toml)"
REGION="$(awk -F'=' '/^region/ {gsub(/[" ]/,"",$2); print $2; exit}' samconfig.toml)"

if [[ -z "${STACK_NAME}" || -z "${REGION}" ]]; then
  echo "ERROR: Could not parse stack_name/region from samconfig.toml" >&2
  exit 1
fi

echo "==> Stack to delete: ${STACK_NAME} (${REGION})"
read -r -p "Type the stack name to confirm deletion: " confirm
if [[ "${confirm}" != "${STACK_NAME}" ]]; then
  echo "Aborted." >&2
  exit 1
fi

BUCKET="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`CampaignAssetsBucketName`].OutputValue' \
  --output text 2>/dev/null || true)"

if [[ -n "${BUCKET}" && "${BUCKET}" != "None" ]]; then
  echo "==> Emptying S3 bucket ${BUCKET} (versions + delete markers)"
  aws s3api delete-objects --bucket "${BUCKET}" --region "${REGION}" \
    --delete "$(aws s3api list-object-versions --bucket "${BUCKET}" --region "${REGION}" \
      --query='{Objects: Versions[].{Key: Key, VersionId: VersionId}}' --output json 2>/dev/null || echo '{"Objects":[]}')" \
    >/dev/null 2>&1 || true
  aws s3api delete-objects --bucket "${BUCKET}" --region "${REGION}" \
    --delete "$(aws s3api list-object-versions --bucket "${BUCKET}" --region "${REGION}" \
      --query='{Objects: DeleteMarkers[].{Key: Key, VersionId: VersionId}}' --output json 2>/dev/null || echo '{"Objects":[]}')" \
    >/dev/null 2>&1 || true
fi

echo "==> sam delete"
sam delete --stack-name "${STACK_NAME}" --region "${REGION}" --no-prompts

echo "Done."
