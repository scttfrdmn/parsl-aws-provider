#!/bin/bash
# Wait for LocalStack to be ready for testing
#
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

set -e

LOCALSTACK_URL=${LOCALSTACK_URL:-http://localhost:4566}
MAX_RETRIES=${MAX_RETRIES:-60}
RETRY_INTERVAL=${RETRY_INTERVAL:-2}

echo "Waiting for LocalStack at ${LOCALSTACK_URL}..."

for i in $(seq 1 $MAX_RETRIES); do
    if curl -s "${LOCALSTACK_URL}/_localstack/health" > /dev/null 2>&1; then
        echo "LocalStack is ready!"

        # Check that required services are available
        health_check=$(curl -s "${LOCALSTACK_URL}/_localstack/health")
        echo "LocalStack health status: ${health_check}"

        # Verify critical services are running
        if echo "$health_check" | grep -q '"lambda": "available"' && \
           echo "$health_check" | grep -q '"s3": "running"' && \
           echo "$health_check" | grep -q '"iam": "available"'; then
            echo "All required AWS services are available in LocalStack"
            exit 0
        else
            echo "Warning: Some required services may not be fully available yet"
            echo "Continuing anyway..."
            exit 0
        fi
    fi

    echo "Attempt $i/$MAX_RETRIES: LocalStack not ready yet, waiting ${RETRY_INTERVAL}s..."
    sleep $RETRY_INTERVAL
done

echo "ERROR: LocalStack failed to become ready after $MAX_RETRIES attempts"
echo "Check that Docker is running and LocalStack container is healthy"
exit 1
