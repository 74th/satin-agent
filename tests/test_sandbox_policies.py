from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.naming import LABEL_SANDBOX
from satin_agent.sandbox.policies import render_policies


def test_deny_all_then_dns_then_fqdn():
    cfg = SandboxConfig(allowed_fqdns=("pypi.org",))
    pols = render_policies(cfg, sandbox="satin-x-g1")
    deny, dns, metadata, fqdn = pols
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"] and "egress" not in deny["spec"]  # 全拒否
    assert dns["spec"]["egress"][0]["ports"] == [
        {"protocol": "UDP", "port": 53},
        {"protocol": "TCP", "port": 53},
    ]
    names = [m["name"] for m in fqdn["spec"]["egress"][0]["matches"]]
    assert names == ["api.anthropic.com", "storage.googleapis.com", "pypi.org"]
    assert fqdn["kind"] == "FQDNNetworkPolicy"


def test_policies_select_only_this_sandbox():
    for p in render_policies(SandboxConfig(), sandbox="satin-x-g1"):
        assert p["spec"]["podSelector"] == {"matchLabels": {LABEL_SANDBOX: "satin-x-g1"}}
        assert p["metadata"]["namespace"] == "satin-sandboxes"


def test_only_metadata_server_is_allowed_by_ip():
    # 外向きの許可は、DNS（kube-dns宛）・メタデータサーバ・FQDN許可先だけ。0.0.0.0/0は使わない。
    import json

    pols = render_policies(SandboxConfig(), sandbox="s")
    text = json.dumps(pols)
    assert "0.0.0.0/0" not in text
    cidrs = [
        r["to"][0]["ipBlock"]["cidr"]
        for p in pols
        for r in p["spec"].get("egress", [])
        if r.get("to") and "ipBlock" in r["to"][0]
    ]
    assert cidrs == ["169.254.169.254/32", "169.254.169.252/32", "127.0.0.1/32"]
