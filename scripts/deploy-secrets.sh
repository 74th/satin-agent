#!/bin/sh
# Create the Kubernetes Secrets from .env. Values are never printed or passed on a command line.
#   KUBE_CONTEXT=<context> scripts/deploy-secrets.sh
# Not copied to the cluster: JEV_API_TOKEN (tests only), SLACK_SIGNING_SECRET (Socket Mode).
set -eu
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:?set KUBE_CONTEXT to the target cluster context}"
NS="${SATIN_NAMESPACE:-satin-system}"
ENV_FILE="${ENV_FILE:-.env}"
K="kubectl --context ${CTX}"

[ -f "${ENV_FILE}" ] || { echo "error: ${ENV_FILE} not found" >&2; exit 1; }

umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# value of KEY from the env file (no output)
get() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1 | sed "s/^['\"]//; s/['\"]$//"; }

need() {
  v="$(get "$1")"
  [ -n "${v}" ] || { echo "error: $1 is not set in ${ENV_FILE}" >&2; exit 1; }
}
for k in CLAUDE_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN \
         CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID CLAUDE_MANAGED_AGENT_ENVIRONMENT_KEY \
         CLAUDE_CODE_SELF_HOSTED_ENVIRONMENT_KEY; do
  need "${k}"
done

# HMAC key for the session -> user mapping: keep the existing one, else generate.
if ${K} -n "${NS}" get secret satin-secrets >/dev/null 2>&1; then
  ${K} -n "${NS}" get secret satin-secrets -o jsonpath='{.data.SATIN_HMAC_KEY}' | base64 -d > "${TMP}/hmac" || true
fi
[ -s "${TMP}/hmac" ] || openssl rand -hex 32 > "${TMP}/hmac"

: > "${TMP}/satin-secrets.env"
for k in CLAUDE_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN \
         CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID CLAUDE_MANAGED_AGENT_ENVIRONMENT_KEY; do
  printf '%s=%s\n' "${k}" "$(get "${k}")" >> "${TMP}/satin-secrets.env"
done
printf 'SATIN_HMAC_KEY=%s\n' "$(cat "${TMP}/hmac")" >> "${TMP}/satin-secrets.env"
get CLAUDE_CODE_SELF_HOSTED_ENVIRONMENT_KEY | tr -d '\n' > "${TMP}/environment-secret"

${K} create namespace "${NS}" --dry-run=client -o yaml | ${K} apply -f - >/dev/null
${K} -n "${NS}" create secret generic satin-secrets \
  --from-env-file="${TMP}/satin-secrets.env" --dry-run=client -o yaml | ${K} apply -f - >/dev/null
${K} -n "${NS}" create secret generic satin-cc-environment-secret \
  --from-file=environment-secret="${TMP}/environment-secret" --dry-run=client -o yaml \
  | ${K} apply -f - >/dev/null

echo "secrets applied in ${NS}: satin-secrets, satin-cc-environment-secret"
