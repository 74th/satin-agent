#!/bin/sh
# Create the Secrets for the Echo MCP and the MCP tunnel. Values are never printed or put on a
# command line.
#   KUBE_CONTEXT=<context> usecases/bigquery-analysis/scripts/create-tunnel-secrets.sh
# From .env: TUNNEL_TOKEN (Console), ECHO_MCP_API_KEY (any long random string; also used by
# create-vault.py). From files: $TUNNEL_CERT_DIR/tls.crt and tls.key (default secrets/mcp-tunnel;
# the server certificate for *.<tunnel domain>, see the README).
# DRY_RUN=1 only checks the inputs and prints the Secret names and keys.
set -eu
cd "$(dirname "$0")/../../.."

ENV_FILE="${ENV_FILE:-.env}"
CERT_DIR="${TUNNEL_CERT_DIR:-secrets/mcp-tunnel}"
TUNNEL_NS="${TUNNEL_NAMESPACE:-mcp-tunnel}"
ECHO_NS="${ECHO_NAMESPACE:-satin-mcp}"
SANDBOX_NS="${SANDBOX_NAMESPACE:-satin-sandboxes}"   # (B) Sandboxes read the key from a Secret here

[ -f "${ENV_FILE}" ] || { echo "error: ${ENV_FILE} not found" >&2; exit 1; }
[ -s "${CERT_DIR}/tls.crt" ] && [ -s "${CERT_DIR}/tls.key" ] \
  || { echo "error: ${CERT_DIR}/tls.crt and tls.key are needed" >&2; exit 1; }

# value of KEY from the env file (no output)
get() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1 | sed "s/^['\"]//; s/['\"]$//"; }
for k in TUNNEL_TOKEN ECHO_MCP_API_KEY; do
  [ -n "$(get "${k}")" ] || { echo "error: ${k} is not set in ${ENV_FILE}" >&2; exit 1; }
done

if [ -n "${DRY_RUN:-}" ]; then
  echo "secret ${TUNNEL_NS}/mcp-tunnel-token: keys tunnel-token"
  echo "secret ${TUNNEL_NS}/mcp-tunnel-cert: keys tls.crt, tls.key"
  echo "secret ${ECHO_NS}/echo-mcp-api-key: keys api-key"
  echo "secret ${SANDBOX_NS}/echo-mcp-api-key: keys api-key"
  exit 0
fi

CTX="${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
K="kubectl --context ${CTX}"
umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
get TUNNEL_TOKEN | tr -d '\n' > "${TMP}/tunnel-token"
get ECHO_MCP_API_KEY | tr -d '\n' > "${TMP}/api-key"

for ns in "${TUNNEL_NS}" "${ECHO_NS}" "${SANDBOX_NS}"; do
  ${K} create namespace "${ns}" --dry-run=client -o yaml | ${K} apply -f - >/dev/null
done
${K} -n "${TUNNEL_NS}" create secret generic mcp-tunnel-token \
  --from-file=tunnel-token="${TMP}/tunnel-token" --dry-run=client -o yaml | ${K} apply -f - >/dev/null
${K} -n "${TUNNEL_NS}" create secret generic mcp-tunnel-cert \
  --from-file=tls.crt="${CERT_DIR}/tls.crt" --from-file=tls.key="${CERT_DIR}/tls.key" \
  --dry-run=client -o yaml | ${K} apply -f - >/dev/null
${K} -n "${ECHO_NS}" create secret generic echo-mcp-api-key \
  --from-file=api-key="${TMP}/api-key" --dry-run=client -o yaml | ${K} apply -f - >/dev/null
${K} -n "${SANDBOX_NS}" create secret generic echo-mcp-api-key \
  --from-file=api-key="${TMP}/api-key" --dry-run=client -o yaml | ${K} apply -f - >/dev/null

echo "secrets applied: ${TUNNEL_NS}/mcp-tunnel-token, ${TUNNEL_NS}/mcp-tunnel-cert," \
  "${ECHO_NS}/echo-mcp-api-key, ${SANDBOX_NS}/echo-mcp-api-key"
