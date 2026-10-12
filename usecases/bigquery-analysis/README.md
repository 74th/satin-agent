# BigQuery analysis use case

Verifies, end to end, in the satin-agent (A) Managed Agents + Slack Bot setup:

- a Skill with a script that calls a Google Cloud API from the Sandbox (BigQuery public data, Workload Identity through a Terraform-managed GSA),
- Managed Agents calling an authenticated MCP server (Echo) in the cluster through MCP tunnels,
- deployment to GKE.

The Skill runs a fixed query: Google Trends, Japan, rising terms of the last 2 days (Tokyo dates), at most 10 rows
(`assets/skills/bigquery-trends/query.sql`). (B) Claude Code can use the Skill too, but the MCP wiring covers (A) only.

This use case **replaces** the default deployment (`k8s/overlays/dev`) while it runs: same namespaces, Environment and Slack
app, but its own Agent, assets prefix, Sandbox image and KSA. Do not run both at the same time (one orchestrator per Environment).

```
usecases/bigquery-analysis/
  assets/            CLAUDE.md, mcp.json, startup.sh, skills/bigquery-trends/
  satin.yaml         publish config (own state file, GCS prefix, Agent name)
  runtime.Dockerfile satin-runtime + google-cloud-bigquery
  k8s/overlay/       Bot / orchestrators config + KSA satin-bq-sandbox
  k8s/echo-mcp/      Echo MCP (namespace satin-mcp)
  k8s/mcp-tunnel/    values for the MCP tunnels Helm chart
  echo_mcp/          Echo MCP server (FastMCP) with its own tests and Dockerfile
  terraform/         GSA + IAM (separate root and state)
  scripts/           build-images.sh, create-tunnel-secrets.sh, deploy-tunnel.sh, create-vault.py
```

All commands run from the repo root. Replace `nnyn-dev` / `us-central1` with your own where needed (`PROJECT_ID`, `REGION`
for the scripts; `usecases/bigquery-analysis/{satin.yaml,k8s/overlay/satin.yaml,k8s/overlay/kustomization.yaml}` hold the same values as the default deployment).

## Prerequisites

- The default deployment is already set up (`terraform/`, `scripts/build-images.sh`, `scripts/deploy-secrets.sh`, `.env`).
- **MCP tunnels** (research preview): request access, create a tunnel in the Console, and note the **tunnel token** and the
  **tunnel domain**. Put `TUNNEL_TOKEN=<token>` and `ECHO_MCP_API_KEY=<long random string>` in `.env`
  (`openssl rand -hex 32` makes a good key). Needs `helm` and `openssl`.
- **Server certificate** for the tunnel (SAN `*.<tunnel domain>`): generate a CA and certificate as in
  [Deploy MCP tunnels with Helm](https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/deploy-helm) ("Without programmatic access"),
  register the CA in the Console, and keep `tls.crt` / `tls.key` in `secrets/mcp-tunnel/` (the `secrets/` directory is git-ignored).
  You renew it yourself before it expires (90 days).
- **Billing**: queries run in `BIGQUERY_JOB_PROJECT` (`sandbox.env` in the overlay). `LIMIT 10` does not reduce the scanned bytes,
  so check the scan size once with a dry run before the first live run:
  `bq query --dry_run --use_legacy_sql=false --project_id=<job project> < usecases/bigquery-analysis/assets/skills/bigquery-trends/query.sql`.
  The script caps a job at 10 GiB (`BIGQUERY_MAX_BYTES_BILLED`, in bytes, to change it).

## 1. Terraform (GSA and IAM)

```
cd usecases/bigquery-analysis/terraform
terraform init
terraform plan -var project_id=<project> -var assets_bucket_name=<assets bucket>
terraform apply -var project_id=<project> -var assets_bucket_name=<assets bucket>
terraform output -raw gsa_email   # used as GSA_EMAIL in step 6
```

It creates the GSA, `roles/bigquery.jobUser` on the job project, `roles/storage.objectViewer` on the assets bucket, and
`roles/iam.workloadIdentityUser` for `satin-sandboxes/satin-bq-sandbox` only. The default `satin-sandbox` KSA gets no BigQuery access.

## 2. Images

```
usecases/bigquery-analysis/scripts/build-images.sh <tag>   # PUSH=0 to build only
```

Builds `satin-runtime-bq` (runtime + BigQuery client) and `echo-mcp`. Use the same `<tag>` for `scripts/deploy.sh` in step 6.
Also push the app image if it is not there yet (`scripts/build-images.sh <tag>`).

## 3. Echo MCP

```
KUBE_CONTEXT=<context> usecases/bigquery-analysis/scripts/create-tunnel-secrets.sh   # DRY_RUN=1 checks inputs only
kubectl --context <context> apply -k usecases/bigquery-analysis/k8s/echo-mcp
```

Edit `images:` in `k8s/echo-mcp/kustomization.yaml` (or `kustomize edit set image`) to the pushed tag first. The server refuses to start
without `ECHO_MCP_API_KEY`. Only the tunnel proxy pod can reach it (NetworkPolicy).

## 4. MCP tunnel

Confirm the cluster Service CIDR, and add it to `gateway.config.upstream.allowed_ips` in `k8s/mcp-tunnel/values.yaml` if it is not RFC 1918:

```
kubectl --context <context> get svc kubernetes -n default   # CLUSTER-IP is inside the Service CIDR
```

```
KUBE_CONTEXT=<context> TUNNEL_DOMAIN=<tunnel domain> usecases/bigquery-analysis/scripts/deploy-tunnel.sh
HELM_ACTION=template TUNNEL_DOMAIN=<tunnel domain> usecases/bigquery-analysis/scripts/deploy-tunnel.sh   # render only
```

The chart (version pinned in the script) also creates the proxy pod's NetworkPolicies: ingress denied, egress only to DNS,
the Cloudflare edge (7844) and the Echo MCP Service.

## 5. Vault, publish

Set the tunnel URL in `assets/mcp.json` (`https://echo.<tunnel domain>/mcp`), then:

```
uv run python usecases/bigquery-analysis/scripts/create-vault.py          # writes only vault_id to .satin-state-bigquery.json
uv run satin publish --config usecases/bigquery-analysis/satin.yaml --assets usecases/bigquery-analysis/assets
```

`create-vault.py` can be run again safely (no duplicate Vault). The Vault holds `ECHO_MCP_API_KEY` as a `static_bearer`
credential for the tunnel URL. If you change the key, create a new Vault credential in the Console (the script does not update an existing one).

## 6. Deploy (swap the default deployment out)

```
KUBE_CONTEXT=<context> OVERLAY=usecases/bigquery-analysis/k8s/overlay SATIN_STATE=.satin-state-bigquery.json \
  GSA_EMAIL=<terraform output gsa_email> scripts/deploy.sh <tag>
```

`RENDER_ONLY=1` prints the rendered manifests without touching the cluster. `deploy.sh` fails if a placeholder
(`__VAULT_ID__`, `__GSA_EMAIL__`, `__AGENT_ID__`) is left.

## 7. Verify

1. KSA and GSA: `kubectl -n satin-sandboxes get sa satin-bq-sandbox -o yaml` shows the `iam.gke.io/gcp-service-account` annotation equal to `terraform output gsa_email`.
2. Echo MCP from inside the cluster (direct): from a throwaway pod in the `mcp-tunnel` namespace with the gateway labels, an MCP call with the right key works;
   without a key or with a wrong key it returns 401; from any other pod the connection is refused. See `docs/verification.md` for the exact commands used.
3. Through the tunnel: a Managed Agents session with the Vault lists and calls `echo` (check which header arrived, `X_API_KEY` or Bearer; never log the value).
4. Slack: ask for the Japanese rising search terms (the agent runs `run_query.py` and answers with date, rank and term) and for an echo of a string.

## Return to the default deployment

```
KUBE_CONTEXT=<context> scripts/deploy.sh <tag>
uv run satin sandbox gc --all --config k8s/overlays/dev/satin.yaml    # remove leftover Sandboxes
```

## Clean up

In this order: `helm -n mcp-tunnel uninstall mcp-tunnel`; `kubectl delete -k usecases/bigquery-analysis/k8s/echo-mcp` and the
`mcp-tunnel` namespace (removes the Secrets); `kubectl -n satin-sandboxes delete sa satin-bq-sandbox`; archive the Vault and the Agent
(archiving an Agent cannot be undone, check first); `terraform plan -destroy` then `destroy` in `usecases/bigquery-analysis/terraform`.

## Tests

- Repo root: `uv run pytest` (use case config, SQL, script with a fake client, overlay rendering; Helm and kubectl tests skip when the binary is missing).
- Echo MCP: `cd usecases/bigquery-analysis/echo_mcp && uv sync && uv run pytest`.
