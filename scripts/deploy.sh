#!/bin/sh
# Apply k8s/overlays/dev. Build and push the images first (scripts/build-images.sh).
#   KUBE_CONTEXT=<context> scripts/deploy.sh [image-tag]
# Needs the Agent ID in .satin-state.json (run `satin publish --target cma` first).
set -eu
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
OVERLAY=k8s/overlays/dev
STATE="${SATIN_STATE:-.satin-state.json}"

AGENT_ID="$(python3 -c "import json,sys; print(json.load(open('${STATE}'))['agent_id'])")"
[ -n "${AGENT_ID}" ] || { echo "error: no agent_id in ${STATE}" >&2; exit 1; }

# Build into a temp copy so the committed overlay keeps its placeholders.
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cp -R k8s "${WORK}/k8s"
sed -i "s/__AGENT_ID__/${AGENT_ID}/" "${WORK}/${OVERLAY}/satin.yaml"
sed -i "s#satin-runtime:dev#satin-runtime:${TAG}#" "${WORK}/${OVERLAY}/satin.yaml"
( cd "${WORK}/${OVERLAY}" && kubectl kustomize . >/dev/null && \
  sed -i "s/newTag: dev/newTag: ${TAG}/" kustomization.yaml )

kubectl --context "${CTX}" apply -k "${WORK}/${OVERLAY}"
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-cma-orchestrator --timeout=180s
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-cc-orchestrator --timeout=180s
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-slack-bot --timeout=180s
