"""Sandboxの設定。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config


@dataclass(frozen=True)
class SandboxConfig:
    namespace: str = "satin-sandboxes"
    image: str = "satin-runtime:dev"
    image_pull_policy: str = "IfNotPresent"
    runtime_class: str = "gvisor"
    service_account: str = "satin-sandbox"
    cpu_request: str = "250m"
    memory_request: str = "512Mi"
    cpu_limit: str = "1"
    memory_limit: str = "2Gi"
    storage: str = "10Gi"
    storage_class: str = "standard-rwo"
    ready_timeout: float = 180.0
    # bashツールのタイムアウト。SDKの既定（120秒）と、1回のツール呼び出しの上限（150秒）は短いので、
    # Sandboxの起動時に上書きする。`timeout_ms`未指定なら default、指定されても max を超えない。
    bash_default_timeout_seconds: float = 3600.0
    bash_max_timeout_seconds: float = 7200.0
    retention_seconds: float = 7 * 24 * 3600.0
    assets_url: str = ""
    # NetworkPolicy: 許可するFQDN（DNS・Anthropic API・GCSは常に許可）
    allowed_fqdns: tuple[str, ...] = field(default_factory=tuple)
    extra_env: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Secretの値を環境変数に渡す（名前, Secret名, キー）。Secretはsandbox namespaceに置く。
    secret_env: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    # 送信を許可するクラスタ内のPod: {"namespace", "labels", "port"}（FQDNでは指定できない宛先）
    allowed_pods: tuple[dict, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, cfg: Config) -> SandboxConfig:
        sb = cfg.get("sandbox", {}) or {}
        k = cfg.get("kubernetes", {}) or {}
        d = cls()
        return cls(
            namespace=k.get("sandbox_namespace", d.namespace),
            image=sb.get("image", d.image),
            image_pull_policy=sb.get("image_pull_policy", d.image_pull_policy),
            runtime_class=sb.get("runtime_class", d.runtime_class),
            service_account=sb.get("service_account", d.service_account),
            cpu_request=sb.get("cpu_request", d.cpu_request),
            memory_request=sb.get("memory_request", d.memory_request),
            cpu_limit=sb.get("cpu_limit", d.cpu_limit),
            memory_limit=sb.get("memory_limit", d.memory_limit),
            storage=sb.get("storage", d.storage),
            storage_class=sb.get("storage_class", d.storage_class),
            ready_timeout=float(sb.get("ready_timeout", d.ready_timeout)),
            bash_default_timeout_seconds=float(
                sb.get("bash_default_timeout_seconds", d.bash_default_timeout_seconds)
            ),
            bash_max_timeout_seconds=float(
                sb.get("bash_max_timeout_seconds", d.bash_max_timeout_seconds)
            ),
            retention_seconds=float(sb.get("retention_seconds", d.retention_seconds)),
            assets_url=cfg.get("assets.gcs_url", "") or "",
            allowed_fqdns=tuple(sb.get("allowed_fqdns", ())),
            extra_env=tuple((str(a), str(b)) for a, b in (sb.get("env", {}) or {}).items()),
            secret_env=tuple(
                (str(n), str(ref["secret"]), str(ref["key"]))
                for n, ref in (sb.get("secret_env", {}) or {}).items()
            ),
            allowed_pods=tuple(dict(p) for p in (sb.get("allowed_pods", []) or [])),
        )
