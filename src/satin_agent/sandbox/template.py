"""Sandboxテンプレートの描画（`{$param: key}`を値に置き換える）。"""

from __future__ import annotations

import copy
from importlib import resources
from typing import Any

import yaml

from .config import SandboxConfig
from .naming import (
    ANN_ENV_HASH,
    ANN_GENERATION,
    ANN_SESSION_ID,
    LABEL_MODE,
    LABEL_SANDBOX,
    LABEL_SESSION,
    sandbox_name,
    secret_name,
    session_hash,
)


class TemplateError(Exception):
    pass


def load_template() -> dict[str, Any]:
    text = resources.files("satin_agent").joinpath("templates/sandbox.yaml").read_text()
    return yaml.safe_load(text)


def _subst(node: Any, params: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if set(node) == {"$param"}:
            key = node["$param"]
            if key not in params:
                raise TemplateError(f"テンプレートのパラメータがありません: {key}")
            return copy.deepcopy(params[key])
        return {k: _subst(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst(v, params) for v in node]
    return node


def env_list(env: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in sorted(env.items())]


def render_sandbox(
    cfg: SandboxConfig,
    *,
    session_id: str,
    generation: int,
    mode: str,
    env: dict[str, str],
    env_hash: str,
) -> dict[str, Any]:
    """Sandbox CRを描画する（`operatingMode`は呼び出し側が決める）。"""
    params = {
        "runtime_class": cfg.runtime_class,
        "service_account": cfg.service_account,
        "image": cfg.image,
        "image_pull_policy": cfg.image_pull_policy,
        "env": env_list(env)
        + [
            {"name": n, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
            for n, secret, key in cfg.secret_env
        ],
        "cpu_request": cfg.cpu_request,
        "memory_request": cfg.memory_request,
        "cpu_limit": cfg.cpu_limit,
        "memory_limit": cfg.memory_limit,
        "run_secret_name": secret_name(session_id, generation),
        "storage_class": cfg.storage_class,
        "storage": cfg.storage,
    }
    obj = _subst(load_template(), params)
    md = obj.setdefault("metadata", {})
    md["name"] = sandbox_name(session_id, generation)
    md["namespace"] = cfg.namespace
    md.setdefault("labels", {}).update({LABEL_SESSION: session_hash(session_id), LABEL_MODE: mode})
    pod_md = obj["spec"]["podTemplate"].setdefault("metadata", {})
    pod_md.setdefault("labels", {}).update(
        {LABEL_SANDBOX: md["name"], LABEL_SESSION: session_hash(session_id)}
    )
    md["annotations"] = {
        ANN_SESSION_ID: session_id,
        ANN_GENERATION: str(generation),
        ANN_ENV_HASH: env_hash,
    }
    return obj
