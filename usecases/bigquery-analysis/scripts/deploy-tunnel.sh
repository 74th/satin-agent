#!/bin/sh
# Install or upgrade the MCP tunnel (Helm chart, external mode). Create the Secrets first
# (create-tunnel-secrets.sh) and deploy the Echo MCP (kubectl apply -k k8s/echo-mcp).
#   KUBE_CONTEXT=<context> TUNNEL_DOMAIN=<tunnel domain> usecases/bigquery-analysis/scripts/deploy-tunnel.sh
# HELM_ACTION=template renders only (no cluster access).
set -eu
cd "$(dirname "$0")/../../.."

CHART="oci://us-docker.pkg.dev/anthropic-public-registry/charts/mcp-tunnel"
VERSION=2.0.2
DOMAIN="${TUNNEL_DOMAIN:?set TUNNEL_DOMAIN to the tunnel domain shown in the Console}"
VALUES=usecases/bigquery-analysis/k8s/mcp-tunnel/values.yaml

if [ "${HELM_ACTION:-}" = "template" ]; then
  exec helm template mcp-tunnel "${CHART}" --version "${VERSION}" -n mcp-tunnel \
    -f "${VALUES}" --set "gateway.config.tunnel_domain=${DOMAIN}"
fi
CTX="${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
helm --kube-context "${CTX}" upgrade --install mcp-tunnel "${CHART}" --version "${VERSION}" \
  -n mcp-tunnel --create-namespace -f "${VALUES}" --set "gateway.config.tunnel_domain=${DOMAIN}"
kubectl --context "${CTX}" -n mcp-tunnel rollout status deploy/mcp-tunnel --timeout=180s
