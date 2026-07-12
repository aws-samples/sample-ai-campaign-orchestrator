#!/bin/bash
# Start the local Flask UI for the campaign orchestrator.
# Requires AWS_REGION, CAMPAIGN_STATE_MACHINE_ARN, DELIVERY_TRACKING_TABLE,
# and optionally WABA_ID (for the template dropdown) in the environment.
set -e

export FLASK_ENV=production

exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    "src.ai_cpaas_demo.web.app:app"
