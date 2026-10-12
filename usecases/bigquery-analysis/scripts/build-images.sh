#!/bin/sh
# Build and push the use case images (runtime with BigQuery client, Echo MCP).
#   usecases/bigquery-analysis/scripts/build-images.sh [tag]     (default tag: git short sha or "dev")
# Needs: gcloud auth configure-docker <region>-docker.pkg.dev
# PUSH=0 builds only. The shared satin-runtime base is built by this script from docker/runtime.Dockerfile.
set -eu
cd "$(dirname "$0")/../../.."

PROJECT="${PROJECT_ID:-nnyn-dev}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-satin-agent}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
BASE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"
UC=usecases/bigquery-analysis

docker build -f docker/runtime.Dockerfile --build-arg "IMAGE_ID=satin-runtime-${TAG}" \
  -t "${BASE}/satin-runtime:${TAG}" .
docker build -f "${UC}/runtime.Dockerfile" --build-arg "BASE=${BASE}/satin-runtime:${TAG}" \
  -t "${BASE}/satin-runtime-bq:${TAG}" .
docker build -f "${UC}/echo_mcp/Dockerfile" -t "${BASE}/echo-mcp:${TAG}" "${UC}/echo_mcp"

if [ "${PUSH:-1}" = "1" ]; then
  docker push "${BASE}/satin-runtime-bq:${TAG}"
  docker push "${BASE}/echo-mcp:${TAG}"
fi
echo "runtime-bq: ${BASE}/satin-runtime-bq:${TAG}"
echo "echo-mcp:   ${BASE}/echo-mcp:${TAG}"
