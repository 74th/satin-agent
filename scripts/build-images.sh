#!/bin/sh
# Build and push the runtime and app images to Artifact Registry.
#   scripts/build-images.sh [tag]          (default tag: git short sha or "dev")
# Needs: gcloud auth configure-docker <region>-docker.pkg.dev
set -eu
cd "$(dirname "$0")/.."

PROJECT="${PROJECT_ID:-nnyn-dev}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-satin-agent}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
BASE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"

docker build -f docker/runtime.Dockerfile --build-arg "IMAGE_ID=satin-runtime-${TAG}" \
  -t "${BASE}/satin-runtime:${TAG}" .
docker build -f docker/app.Dockerfile -t "${BASE}/satin-app:${TAG}" .

docker push "${BASE}/satin-runtime:${TAG}"
docker push "${BASE}/satin-app:${TAG}"

echo "runtime: ${BASE}/satin-runtime:${TAG}"
echo "app:     ${BASE}/satin-app:${TAG}"
