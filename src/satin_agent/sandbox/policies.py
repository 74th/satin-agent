"""ネットワーク隔離: 全拒否 → DNS → メタデータサーバ → FQDN の順に、必要な宛先だけ許可する。

NetworkPolicyの許可は加算になる。外向きは「kube-dns宛のDNS」と
FQDNNetworkPolicyの許可先だけで、他のSandboxのPodや任意の宛先へは出られない。
入力は全て拒否する（オーケストレータはKubernetes API経由でしか操作しない）。
"""

from __future__ import annotations

from typing import Any

from .config import SandboxConfig
from .naming import LABEL_SANDBOX

ALWAYS_ALLOWED_FQDNS = ("api.anthropic.com", "storage.googleapis.com")

# GKE Workload Identityのメタデータサーバ。GCSの資格情報の取得に必要（アセットの取得）。
# 169.254.169.254:80 はPodから見たメタデータのアドレス、169.254.169.252:988 と
# 127.0.0.1:988 はWorkload Identityのサーバ（Dataplane V1 / V2）。
METADATA_ENDPOINTS = (
    ("169.254.169.254/32", 80),
    ("169.254.169.252/32", 988),
    ("127.0.0.1/32", 988),
)


def _selector(sandbox: str) -> dict[str, Any]:
    return {"matchLabels": {LABEL_SANDBOX: sandbox}}


def render_policies(cfg: SandboxConfig, *, sandbox: str) -> list[dict[str, Any]]:
    labels = {LABEL_SANDBOX: sandbox, "app.kubernetes.io/managed-by": "satin-agent"}

    def meta(name: str) -> dict[str, Any]:
        return {
            "name": f"{sandbox}-{name}",
            "namespace": cfg.namespace,
            "labels": dict(labels),
        }

    fqdns = list(dict.fromkeys([*ALWAYS_ALLOWED_FQDNS, *cfg.allowed_fqdns]))
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("deny"),
            "spec": {"podSelector": _selector(sandbox), "policyTypes": ["Ingress", "Egress"]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("dns"),
            "spec": {
                "podSelector": _selector(sandbox),
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                                },
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")],
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("metadata"),
            "spec": {
                "podSelector": _selector(sandbox),
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": cidr}}],
                        "ports": [{"protocol": "TCP", "port": port}],
                    }
                    for cidr, port in METADATA_ENDPOINTS
                ],
            },
        },
        {
            "apiVersion": "networking.gke.io/v1alpha1",
            "kind": "FQDNNetworkPolicy",
            "metadata": meta("fqdn"),
            "spec": {
                "podSelector": _selector(sandbox),
                "egress": [
                    {
                        "matches": [{"name": f} for f in fqdns],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        },
    ]


POLICY_KINDS = {
    "NetworkPolicy": "networkpolicy",
    "FQDNNetworkPolicy": "fqdnnetworkpolicy",
}
