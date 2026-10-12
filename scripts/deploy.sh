#!/bin/sh
# Apply an overlay (default k8s/overlays/dev). Build and push the images first (scripts/build-images.sh).
#   KUBE_CONTEXT=<context> scripts/deploy.sh [image-tag]
# Env:
#   OVERLAY       overlay directory (default k8s/overlays/dev)
#   SATIN_STATE   state file with agent_id (and optional vault_id) (default .satin-state.json)
#   RENDER_ONLY   if set, print the rendered manifests and exit without applying
#   GSA_EMAIL     fills __GSA_EMAIL__ in the overlay (Workload Identity annotation), if set
# Needs the Agent ID in the state file (run `satin publish` first).
set -eu
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
OVERLAY="${OVERLAY:-k8s/overlays/dev}"
STATE="${SATIN_STATE:-.satin-state.json}"

AGENT_ID="$(python3 -c "import json,sys; print(json.load(open('${STATE}'))['agent_id'])")"
[ -n "${AGENT_ID}" ] || { echo "error: no agent_id in ${STATE}" >&2; exit 1; }

VAULT_ID="$(python3 -c "import json; print(json.load(open('${STATE}')).get('vault_id', ''))")"

# Build into a temp copy so the committed overlay keeps its placeholders.
# The overlay keeps its repo-relative path so that its ../../base references still resolve.
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cp -R k8s "${WORK}/k8s"
if [ ! -d "${WORK}/${OVERLAY}" ]; then
  mkdir -p "${WORK}/${OVERLAY}"
  cp -R "${OVERLAY}/." "${WORK}/${OVERLAY}/"
fi
sed -i "s/__AGENT_ID__/${AGENT_ID}/" "${WORK}/${OVERLAY}/satin.yaml"
sed -i "s#\(satin-runtime[a-z-]*\):dev#\1:${TAG}#" "${WORK}/${OVERLAY}/satin.yaml"
if [ -n "${VAULT_ID}" ]; then
  sed -i "s/__VAULT_ID__/${VAULT_ID}/" "${WORK}/${OVERLAY}/satin.yaml"
fi
if [ -n "${GSA_EMAIL:-}" ]; then
  grep -rl "__GSA_EMAIL__" "${WORK}/${OVERLAY}" | xargs -r sed -i "s/__GSA_EMAIL__/${GSA_EMAIL}/"
fi
# Fail rather than ship a placeholder (e.g. __VAULT_ID__ without vault_id in the state file).
if grep -rn "__[A-Z_]*__" "${WORK}/${OVERLAY}" >&2; then
  echo "error: unresolved placeholder(s) in ${OVERLAY} (see above)" >&2
  exit 1
fi
( cd "${WORK}/${OVERLAY}" && kubectl kustomize . >/dev/null && \
  sed -i "s/newTag: dev/newTag: ${TAG}/" kustomization.yaml )

if [ -n "${RENDER_ONLY:-}" ]; then   # print the rendered manifests and stop (no cluster access)
  ( cd "${WORK}/${OVERLAY}" && kubectl kustomize . )
  exit 0
fi

kubectl --context "${CTX}" apply -k "${WORK}/${OVERLAY}"
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-cma-orchestrator --timeout=180s
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-cc-orchestrator --timeout=180s
kubectl --context "${CTX}" -n satin-system rollout status deploy/satin-slack-bot --timeout=180s
