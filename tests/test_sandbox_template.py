from pathlib import Path

import pytest
import yaml

from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.template import TemplateError, _subst, render_sandbox

SNAP = Path(__file__).parent / "snapshots" / "sandbox.yaml"


def render(**kw):
    return render_sandbox(
        SandboxConfig(namespace="satin-sandboxes", image="img:1"),
        session_id="sesn_X",
        generation=1,
        mode="cma",
        env={"SATIN_MODE": "cma", "SATIN_SESSION_ID": "sesn_X", "AGENT_USER_EMAIL": "a@b.c"},
        env_hash="h",
        **kw,
    )


def test_snapshot():
    got = yaml.safe_dump(render(), sort_keys=True, allow_unicode=True)
    if not SNAP.exists():  # 初回のみ生成。以後は差分をレビューして更新する
        SNAP.write_text(got)
    assert got == SNAP.read_text()


def test_isolation_and_restart_policy():
    pod = render()["spec"]["podTemplate"]["spec"]
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    c = pod["containers"][0]
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_secret_mounted_as_file_not_env():
    obj = render()
    pod = obj["spec"]["podTemplate"]["spec"]
    assert pod["volumes"][0]["secret"]["secretName"].startswith("satin-run-")
    assert pod["volumes"][0]["secret"]["defaultMode"] == 0o444
    env_names = [e["name"] for e in pod["containers"][0]["env"]]
    assert "SATIN_MODE" in env_names and not any("SECRET" in n for n in env_names)
    assert obj["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "workspace"


def test_missing_param_errors():
    with pytest.raises(TemplateError):
        _subst({"a": {"$param": "nope"}}, {})
