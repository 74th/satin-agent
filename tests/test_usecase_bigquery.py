"""usecases/bigquery-analysis の構成・SQL・スクリプトのテスト。"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from satin_agent.assets.load import load_assets
from satin_agent.config import load_config
from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.policies import render_policies
from satin_agent.sandbox.template import render_sandbox

ROOT = Path(__file__).resolve().parent.parent
UC = ROOT / "usecases" / "bigquery-analysis"
SKILL = UC / "assets" / "skills" / "bigquery-trends"


def _load_run_query():
    spec = importlib.util.spec_from_file_location("run_query", SKILL / "scripts" / "run_query.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rq = _load_run_query()


# --- 3.1 構成 ---------------------------------------------------------------------------


def test_assets_load_and_references_exist():
    a = load_assets(UC / "assets")
    assert list(a.skills) == ["bigquery-trends"]
    assert a.mcp_servers == []  # (A)のMCP（トンネル）は未検証のため登録しない
    assert a.startup_sh and a.claude_md
    files = a.skills["bigquery-trends"].files
    assert {"SKILL.md", "query.sql", "scripts/run_query.py"} <= set(files)
    # SKILL.md / CLAUDE.md が指すパスが存在する
    skill_md = files["SKILL.md"].decode()
    for rel in re.findall(r"/workspace/skills/bigquery-trends/(\S+)", skill_md):
        assert (SKILL / rel).is_file(), rel
    assert "bigquery-trends" in a.claude_md


def test_publish_config_is_dedicated():
    cfg = load_config(UC / "satin.yaml")
    default = load_config(ROOT / "satin.yaml")
    for key in ("assets.state_file", "assets.gcs_url", "agent.name"):
        assert cfg.get(key) and cfg.get(key) != default.get(key), key
    assert cfg.get("assets.gcs_url").endswith("/bigquery-analysis/")


# --- 3.2 SQL ----------------------------------------------------------------------------


def test_query_sql_matches_request():
    sql = (SKILL / "query.sql").read_text()
    flat = re.sub(r"\s+", " ", sql)
    assert "SELECT DISTINCT refresh_date, rank, term" in flat
    assert "`bigquery-public-data.google_trends.international_top_rising_terms`" in flat
    assert "country_name = 'Japan'" in flat
    assert 'refresh_date >= DATE_SUB(CURRENT_DATE("Asia/Tokyo"), INTERVAL 1 DAY)' in flat
    assert "ORDER BY refresh_date DESC, rank ASC" in flat
    assert flat.rstrip().endswith("LIMIT 10")


# --- 3.3 スクリプト ---------------------------------------------------------------------


class _Result:
    def __init__(self, rows, cols=("refresh_date", "rank", "term")):
        self.schema = [SimpleNamespace(name=c) for c in cols]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _Client:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.sql, self.config = rows, exc, None, None

    def query(self, sql, job_config=None):
        self.sql, self.config = sql, job_config
        if self.exc:
            raise self.exc
        return SimpleNamespace(result=lambda: _Result(self.rows))


def _run(client, env=None):
    env = {"BIGQUERY_JOB_PROJECT": "p"} if env is None else env
    out, err = io.StringIO(), io.StringIO()
    seen = {}

    def factory(project, max_bytes):
        seen["project"], seen["max"] = project, max_bytes
        return client, "CONFIG"

    code = rq.run(env, out, err, factory)
    return code, out.getvalue(), err.getvalue(), seen


def test_success_outputs_columns_and_rows():
    import datetime

    rows = [{"refresh_date": datetime.date(2026, 10, 12), "rank": 1, "term": "検索語"}]
    c = _Client(rows)
    code, out, err, seen = _run(c)
    assert code == 0 and err == ""
    data = json.loads(out)
    assert data["columns"] == ["refresh_date", "rank", "term"]
    assert data["rows"] == [{"refresh_date": "2026-10-12", "rank": 1, "term": "検索語"}]
    assert data["row_count"] == 1
    assert c.sql == (SKILL / "query.sql").read_text()  # 固定のSQL
    assert c.config == "CONFIG" and seen["project"] == "p" and seen["max"] == 10 * 1024**3


def test_zero_rows_is_success_with_no_rows():
    code, out, err, _ = _run(_Client([]))
    assert code == 0 and json.loads(out)["row_count"] == 0 and err == ""


@pytest.mark.parametrize(
    ("exc_factory", "category"),
    [
        (
            lambda: __import__("google.api_core.exceptions", fromlist=["x"]).Forbidden("denied"),
            "permission",
        ),
        (
            lambda: __import__("google.api_core.exceptions", fromlist=["x"]).Unauthorized("no"),
            "auth",
        ),
        (lambda: __import__("google.auth.exceptions", fromlist=["x"]).RefreshError("bad"), "auth"),
        (
            lambda: __import__("google.auth.exceptions", fromlist=["x"]).DefaultCredentialsError(
                "none"
            ),
            "auth",
        ),
        (
            lambda: __import__("google.auth.exceptions", fromlist=["x"]).TransportError("down"),
            "network",
        ),
        (
            lambda: __import__("google.api_core.exceptions", fromlist=["x"]).ServiceUnavailable(
                "x"
            ),
            "network",
        ),
        (lambda: ConnectionError("reset"), "network"),
        (
            lambda: __import__("google.api_core.exceptions", fromlist=["x"]).BadRequest("syntax"),
            "query",
        ),
    ],
)
def test_failures_are_classified_and_never_print_results(exc_factory, category):
    code, out, err, _ = _run(_Client(exc=exc_factory()))
    assert out == ""
    assert code == rq.EXIT_CODES[category] != 0
    assert json.loads(err)["error"]["category"] == category


def test_client_creation_failure_is_classified():
    def factory(project, max_bytes):
        from google.auth.exceptions import DefaultCredentialsError

        raise DefaultCredentialsError("no adc")

    out, err = io.StringIO(), io.StringIO()
    assert rq.run({"BIGQUERY_JOB_PROJECT": "p"}, out, err, factory) == rq.EXIT_CODES["auth"]
    assert out.getvalue() == ""


def test_missing_job_project_is_config_error():
    code, out, err, _ = _run(_Client([]), env={})
    assert code == rq.EXIT_CODES["config"] and out == ""
    assert json.loads(err)["error"]["category"] == "config"


def test_script_takes_no_sql_argument():
    r = subprocess.run(
        ["python3", str(SKILL / "scripts" / "run_query.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "引数はない" in r.stdout
    r = subprocess.run(
        ["python3", str(SKILL / "scripts" / "run_query.py"), "SELECT 1"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0


# --- 3.5 指示 ---------------------------------------------------------------------------


def test_instructions_cover_evidence_no_speculation_and_failure():
    skill = (SKILL / "SKILL.md").read_text()
    claude = (UC / "assets" / "CLAUDE.md").read_text()
    for text in (skill, claude):
        assert "refresh_date" in text or "日付" in text
        assert "事実として述べない" in text
        assert "作" in text  # 作り出さない / 作らない
    assert "条件" in skill and "条件" in claude
    assert "失敗" in skill and "失敗" in claude


# --- 2.2 / 2.3 overlay・Terraform ------------------------------------------------------

OVERLAY = UC / "k8s" / "overlay"


def _render(tmp_path, state, **env):
    if not shutil.which("kubectl"):
        pytest.skip("kubectl がありません")
    st = tmp_path / "state.json"
    st.write_text(json.dumps(state))
    import os

    e = {
        **os.environ,
        "KUBE_CONTEXT": "x",
        "OVERLAY": str(OVERLAY.relative_to(ROOT)),
        "SATIN_STATE": str(st),
        "RENDER_ONLY": "1",
        **env,
    }
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "deploy.sh"), "t1"], capture_output=True, text=True, env=e
    )


def _docs(text):
    return [d for d in yaml.safe_load_all(text) if d]


def test_overlay_renders_ksa_and_config(tmp_path):
    r = _render(
        tmp_path,
        {"agent_id": "agt_1", "vault_id": "vlt_1"},
        GSA_EMAIL="g@p.iam.gserviceaccount.com",
    )
    assert r.returncode == 0, r.stderr
    docs = _docs(r.stdout)
    ksa = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "satin-bq-sandbox"
    )
    assert ksa["metadata"]["namespace"] == "satin-sandboxes"
    assert (
        ksa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"]
        == "g@p.iam.gserviceaccount.com"
    )
    assert ksa["automountServiceAccountToken"] is False
    # Terraformのnamespace・KSAの名前と一致する
    tf = (UC / "terraform" / "variables.tf").read_text()
    assert re.search(r'variable "sandbox_ksa".*?default\s*=\s*"satin-bq-sandbox"', tf, re.S)
    assert re.search(r'variable "sandbox_namespace".*?default\s*=\s*"satin-sandboxes"', tf, re.S)
    # 既定のKSAは残るが、BigQuery用の注釈は付かない
    default_ksa = next(d for d in docs if d["metadata"]["name"] == "satin-sandbox")
    assert "annotations" not in default_ksa["metadata"]

    cm = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "satin-config"
    )
    cfg = yaml.safe_load(cm["data"]["satin.yaml"])
    assert cfg["agent"]["id"] == "agt_1" and cfg["agent"]["vault_ids"] == ["vlt_1"]
    assert cfg["sandbox"]["image"].endswith("satin-runtime-bq:t1")


def test_overlay_without_vault_fails_instead_of_shipping_placeholder(tmp_path):
    r = _render(tmp_path, {"agent_id": "agt_1"}, GSA_EMAIL="g@p.iam.gserviceaccount.com")
    assert r.returncode != 0 and "__VAULT_ID__" in r.stderr


def test_default_overlay_is_unaffected_by_vault(tmp_path):
    r = _render(tmp_path, {"agent_id": "agt_1"}, OVERLAY="k8s/overlays/dev")
    assert r.returncode == 0, r.stderr
    cm = next(
        d
        for d in _docs(r.stdout)
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "satin-config"
    )
    assert "vault_ids" not in yaml.safe_load(cm["data"]["satin.yaml"])["agent"]


def test_sandbox_uses_dedicated_ksa_and_allows_bigquery():
    cfg = load_config(OVERLAY / "satin.yaml")
    sc = SandboxConfig.from_config(cfg)
    assert sc.service_account == "satin-bq-sandbox"
    assert "bigquery.googleapis.com" in sc.allowed_fqdns
    assert ("BIGQUERY_JOB_PROJECT", cfg.get("sandbox.env.BIGQUERY_JOB_PROJECT")) in sc.extra_env
    default = SandboxConfig.from_config(
        load_config(ROOT / "k8s" / "overlays" / "dev" / "satin.yaml")
    )
    assert default.service_account == "satin-sandbox" and not default.allowed_fqdns


def test_rendered_sandbox_and_policies_use_ksa_and_fqdn():
    cfg = load_config(OVERLAY / "satin.yaml")
    sc = SandboxConfig.from_config(cfg)
    obj = render_sandbox(
        sc, session_id="s1", generation=1, mode="cma", env=dict(sc.extra_env), env_hash="h"
    )
    pod = obj["spec"]["podTemplate"]["spec"]
    assert pod["serviceAccountName"] == "satin-bq-sandbox"
    env = {e["name"]: e.get("value") for c in pod["containers"] for e in c.get("env", [])}
    assert env["BIGQUERY_JOB_PROJECT"]
    pols = render_policies(sc, sandbox="sb")
    assert "bigquery.googleapis.com" in json.dumps(pols)
    default = render_policies(SandboxConfig(), sandbox="sb")
    assert "bigquery.googleapis.com" not in json.dumps(default)


# --- 5.x トンネル・Secret・Vault -------------------------------------------------------

SCRIPTS = UC / "scripts"


def _load_create_vault():
    spec = importlib.util.spec_from_file_location("create_vault", SCRIPTS / "create-vault.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeVaults:
    def __init__(self):
        self.vaults = []
        self.creds = []
        self.beta = SimpleNamespace(
            vaults=SimpleNamespace(
                create=self._create,
                credentials=SimpleNamespace(create=self._cred_create, list=self._cred_list),
            )
        )

    def _create(self, display_name):
        self.vaults.append(display_name)
        return SimpleNamespace(id=f"vlt_{len(self.vaults)}")

    def _cred_create(self, vault_id, display_name=None, auth=None):
        self.creds.append((vault_id, auth))
        return SimpleNamespace(id="cred_1")

    def _cred_list(self, vault_id):
        return [
            SimpleNamespace(auth=SimpleNamespace(mcp_server_url=a["mcp_server_url"]))
            for v, a in self.creds
            if v == vault_id
        ]


def test_create_vault_is_idempotent_and_keeps_secret_out_of_state(tmp_path):
    cv = _load_create_vault()
    api, state = _FakeVaults(), tmp_path / "state.json"
    state.write_text(json.dumps({"agent_id": "agt_1"}))
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "mcp.json").write_text(
        json.dumps(
            {"mcp_servers": [{"type": "url", "name": "echo", "url": "https://e.t.example/mcp"}]}
        )
    )
    url = cv.echo_url(assets)
    secret = "sk-echo-SECRET-123"
    assert cv.ensure_vault(api, state, url, secret) == "vlt_1"
    assert cv.ensure_vault(api, state, url, secret) == "vlt_1"
    assert len(api.vaults) == 1 and len(api.creds) == 1
    assert api.creds[0][1] == {"type": "static_bearer", "mcp_server_url": url, "token": secret}
    text = state.read_text()
    assert secret not in text and json.loads(text) == {"agent_id": "agt_1", "vault_id": "vlt_1"}


def _secrets_env(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("TUNNEL_TOKEN=tok-SECRET\nECHO_MCP_API_KEY=key-SECRET\n")
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "tls.crt").write_text("c")
    (certs / "tls.key").write_text("k")
    import os

    return {
        **os.environ,
        "ENV_FILE": str(envf),
        "TUNNEL_CERT_DIR": str(certs),
        "DRY_RUN": "1",
    }


def test_tunnel_secrets_dry_run_names_keys_and_no_values(tmp_path):
    r = subprocess.run(
        ["sh", str(SCRIPTS / "create-tunnel-secrets.sh")],
        capture_output=True,
        text=True,
        env=_secrets_env(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "mcp-tunnel/mcp-tunnel-token: keys tunnel-token" in r.stdout
    assert "mcp-tunnel/mcp-tunnel-cert: keys tls.crt, tls.key" in r.stdout
    assert "satin-mcp/echo-mcp-api-key: keys api-key" in r.stdout
    assert "satin-sandboxes/echo-mcp-api-key: keys api-key" in r.stdout
    assert "SECRET" not in r.stdout + r.stderr


def test_tunnel_secrets_fail_without_inputs(tmp_path):
    env = _secrets_env(tmp_path)
    (tmp_path / ".env").write_text("TUNNEL_TOKEN=x\n")
    r = subprocess.run(
        ["sh", str(SCRIPTS / "create-tunnel-secrets.sh")], capture_output=True, text=True, env=env
    )
    assert r.returncode != 0 and "ECHO_MCP_API_KEY" in r.stderr


def _kustomize(path):
    if not shutil.which("kubectl"):
        pytest.skip("kubectl がありません")
    r = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return _docs(r.stdout)


def test_echo_mcp_manifests_wire_secret_and_restrict_ingress():
    docs = {(d["kind"]): d for d in _kustomize(UC / "k8s" / "echo-mcp")}
    dep = docs["Deployment"]
    assert dep["metadata"]["namespace"] == "satin-mcp"
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert env["name"] == "ECHO_MCP_API_KEY"
    assert env["valueFrom"]["secretKeyRef"] == {"name": "echo-mcp-api-key", "key": "api-key"}
    assert docs["Service"]["spec"]["type"] == "ClusterIP"
    np = docs["NetworkPolicy"]["spec"]
    assert np["podSelector"] == {"matchLabels": dep["spec"]["selector"]["matchLabels"]}
    (rule,) = np["ingress"]
    peer, sandbox_peer = rule["from"]  # proxyのPodと、Sandboxのpod（(B)の直接接続）だけ
    assert sandbox_peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "satin-sandboxes"
    }
    assert sandbox_peer["podSelector"]["matchExpressions"][0]["key"] == "satin.dev/sandbox"
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "mcp-tunnel"}
    assert peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"
    assert [p["port"] for p in rule["ports"]] == [8080]
    assert "Egress" in np["policyTypes"] and "egress" not in np  # 送信は全て拒否


def test_tunnel_chart_render_matches_echo_policy():
    if not shutil.which("helm"):
        pytest.skip("helm がありません")
    import os

    r = subprocess.run(
        ["sh", str(SCRIPTS / "deploy-tunnel.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "HELM_ACTION": "template", "TUNNEL_DOMAIN": "t.example.com"},
    )
    if r.returncode != 0:
        pytest.skip(f"chartを取得できません: {r.stderr[-200:]}")
    docs = _docs(r.stdout)
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "mcp-tunnel")
    proxy_cfg = yaml.safe_load(cm["data"]["config.yaml"])
    assert proxy_cfg["routes"] == {"echo": "http://echo-mcp.satin-mcp.svc.cluster.local:8080"}
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]
    secrets = json.dumps(pod["spec"]["volumes"]) + json.dumps(pod["spec"]["containers"])
    assert "mcp-tunnel-token" in secrets and "mcp-tunnel-cert" in secrets
    assert not any(d["kind"] == "Job" for d in docs)  # setup.enabled=false
    # Echo MCPのNetworkPolicyが許すproxyのPodのラベルは、実際のラベルと一致する
    echo_np = next(d for d in _kustomize(UC / "k8s" / "echo-mcp") if d["kind"] == "NetworkPolicy")
    allowed = echo_np["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    labels = pod["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in allowed.items())
    # proxy側のegress policyは、Echo MCPのPodと8080だけを許す
    eg = next(
        d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("egress")
    )
    echo_dep = next(d for d in _kustomize(UC / "k8s" / "echo-mcp") if d["kind"] == "Deployment")
    echo_pod_labels = echo_dep["spec"]["template"]["metadata"]["labels"]
    peers = [t for rule in eg["spec"]["egress"] for t in rule.get("to", []) if "podSelector" in t]
    assert peers[0]["podSelector"]["matchLabels"] == echo_pod_labels


def test_readme_referenced_paths_exist():
    text = (UC / "README.md").read_text()
    paths = set(
        re.findall(
            r"(?<![\w/.-])((?:usecases/bigquery-analysis|scripts|k8s|terraform)/[\w./-]+)", text
        )
    )
    missing = [
        p
        for p in sorted(paths)
        if not (ROOT / p.rstrip(".,)`")).exists() and not (UC / p.rstrip(".,)`")).exists()
    ]
    assert not missing, missing


def test_sandbox_gets_echo_key_from_secret_and_may_reach_only_echo_pods():
    cfg = load_config(OVERLAY / "satin.yaml")
    sc = SandboxConfig.from_config(cfg)
    assert sc.secret_env == (("ECHO_MCP_API_KEY", "echo-mcp-api-key", "api-key"),)
    obj = render_sandbox(sc, session_id="s1", generation=1, mode="cc", env={}, env_hash="h")
    env = obj["spec"]["podTemplate"]["spec"]["containers"][0]["env"]
    (key_env,) = [e for e in env if e["name"] == "ECHO_MCP_API_KEY"]
    assert key_env == {
        "name": "ECHO_MCP_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "echo-mcp-api-key", "key": "api-key"}},
    }
    pols = [p for p in render_policies(sc, sandbox="sb") if p["metadata"]["name"] == "sb-pods"]
    (egress,) = pols[0]["spec"]["egress"]
    assert egress["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "satin-mcp"
    }
    assert egress["to"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "echo-mcp"}
    assert egress["ports"] == [{"protocol": "TCP", "port": 8080}]
    # 既定の設定では、どちらも出ない
    d = SandboxConfig()
    assert not any(
        e.get("valueFrom")
        for e in render_sandbox(d, session_id="s1", generation=1, mode="cc", env={}, env_hash="h")[
            "spec"
        ]["podTemplate"]["spec"]["containers"][0]["env"]
    )
    assert all(p["metadata"]["name"] != "sb-pods" for p in render_policies(d, sandbox="sb"))


def test_startup_registers_echo_mcp_only_in_cc_mode_without_writing_the_key(tmp_path):
    import os

    def run(mode, key):
        env = {
            **os.environ,
            "SATIN_MODE": mode,
            "SATIN_HOST_CONFIG_DIR": str(tmp_path / f"{mode}-{bool(key)}"),
        }
        if key:
            env["ECHO_MCP_API_KEY"] = key
        subprocess.run(
            ["sh", str(UC / "assets" / "startup.sh")], env=env, check=True, capture_output=True
        )
        return tmp_path / f"{mode}-{bool(key)}" / ".claude.json"

    cc = run("cc", "SECRETKEY")
    cfg = json.loads(cc.read_text())
    echo = cfg["mcpServers"]["echo"]
    assert echo["url"] == "http://echo-mcp.satin-mcp.svc.cluster.local:8080/mcp"
    assert echo["headers"] == {"X_API_KEY": "${ECHO_MCP_API_KEY}"}  # 値ではなく環境変数の参照
    assert "SECRETKEY" not in cc.read_text()
    assert not run("cma", "SECRETKEY").exists()
    assert not run("cc", "").exists()
