#!/bin/bash
# Wait for the substrate AWS emulator to be ready for testing.
#
# Replaces scripts/localstack-wait.sh (#125).
#
# The old script polled /_localstack/health and grepped the JSON for
# '"lambda": "available"', '"s3": "running"', and '"iam": "available"'. Those
# patterns are whitespace-exact and none of them match substrate, which emits
# compact JSON ('"s3":"available"'). That would not have failed the script
# either: both branches of the check ended in `exit 0`, the negative one after
# printing "Continuing anyway...". So the service check never gated anything.
#
# Substrate answers /health as soon as it can serve, so liveness is the whole
# check here. Service availability is asserted per-test by the suite, which is
# where a missing service should surface.
#
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

set -euo pipefail

SUBSTRATE_URL=${SUBSTRATE_URL:-${LOCALSTACK_URL:-http://localhost:4566}}
MAX_RETRIES=${MAX_RETRIES:-60}
RETRY_INTERVAL=${RETRY_INTERVAL:-2}

echo "Waiting for substrate at ${SUBSTRATE_URL}..."

for i in $(seq 1 "$MAX_RETRIES"); do
    if curl -fsS -m 5 "${SUBSTRATE_URL}/health" > /dev/null 2>&1; then
        echo "substrate is ready: $(curl -fsS -m 5 "${SUBSTRATE_URL}/health")"
        exit 0
    fi

    echo "Attempt $i/$MAX_RETRIES: substrate not ready yet, waiting ${RETRY_INTERVAL}s..."
    sleep "$RETRY_INTERVAL"
done

echo "ERROR: substrate failed to become ready after $MAX_RETRIES attempts"
echo "Check that the container runtime is up and the substrate container is healthy:"
echo "  make substrate-status"
exit 1
