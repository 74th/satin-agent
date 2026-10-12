"""`KubeApi`の実クラスタ実装（kubernetes_asyncio）。1つのnamespaceを対象にする。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..ports import ConflictError, NotFoundError

CUSTOM = {
    "sandbox": ("agents.x-k8s.io", "v1beta1", "sandboxes"),
    "fqdnnetworkpolicy": ("networking.gke.io", "v1alpha1", "fqdnnetworkpolicies"),
}
CORE = {"secret", "configmap", "pod", "pvc"}


def _translate(exc: Exception) -> Exception:
    status = getattr(exc, "status", None)
    if status == 404:
        return NotFoundError(str(exc))
    if status == 409:
        return ConflictError(str(exc))
    return exc


class KubeAsyncio:
    def __init__(self, namespace: str, context: str | None = None) -> None:
        self.namespace = namespace
        self._context = context
        self._client: Any = None
        self._custom: Any = None
        self._core: Any = None
        self._net: Any = None

    async def __aenter__(self) -> KubeAsyncio:
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config(context=self._context)
        self._client = client.ApiClient()
        self._custom = client.CustomObjectsApi(self._client)
        self._core = client.CoreV1Api(self._client)
        self._net = client.NetworkingV1Api(self._client)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.close()

    def _ser(self, obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            return obj
        return self._client.sanitize_for_serialization(obj)

    # --- 呼び出しの振り分け ---
    def _ops(self, kind: str) -> dict[str, Any]:
        ns = self.namespace
        if kind in CUSTOM:
            g, v, p = CUSTOM[kind]
            c = self._custom
            return {
                "get": lambda n: c.get_namespaced_custom_object(g, v, ns, p, n),
                "list": lambda sel: c.list_namespaced_custom_object(
                    g, v, ns, p, label_selector=sel
                ),
                "create": lambda b: c.create_namespaced_custom_object(g, v, ns, p, b),
                "replace": lambda n, b: c.replace_namespaced_custom_object(g, v, ns, p, n, b),
                "patch": lambda n, b: c.patch_namespaced_custom_object(
                    g, v, ns, p, n, b, _content_type="application/merge-patch+json"
                ),
                "delete": lambda n: c.delete_namespaced_custom_object(g, v, ns, p, n),
                "watch_fn": c.list_namespaced_custom_object,
                "watch_args": {"group": g, "version": v, "namespace": ns, "plural": p},
            }
        if kind == "networkpolicy":
            n_ = self._net
            return {
                "get": lambda n: n_.read_namespaced_network_policy(n, ns),
                "list": lambda sel: n_.list_namespaced_network_policy(ns, label_selector=sel),
                "create": lambda b: n_.create_namespaced_network_policy(ns, b),
                "replace": lambda n, b: n_.replace_namespaced_network_policy(n, ns, b),
                "patch": lambda n, b: n_.patch_namespaced_network_policy(
                    n, ns, b, _content_type="application/merge-patch+json"
                ),
                "delete": lambda n: n_.delete_namespaced_network_policy(n, ns),
            }
        if kind in CORE:
            c = self._core
            word = {
                "secret": "secret",
                "configmap": "config_map",
                "pod": "pod",
                "pvc": "persistent_volume_claim",
            }[kind]
            return {
                "get": lambda n: getattr(c, f"read_namespaced_{word}")(n, ns),
                "list": lambda sel: getattr(c, f"list_namespaced_{word}")(ns, label_selector=sel),
                "create": lambda b: getattr(c, f"create_namespaced_{word}")(ns, b),
                "replace": lambda n, b: getattr(c, f"replace_namespaced_{word}")(n, ns, b),
                "patch": lambda n, b: getattr(c, f"patch_namespaced_{word}")(
                    n, ns, b, _content_type="application/merge-patch+json"
                ),
                "delete": lambda n: getattr(c, f"delete_namespaced_{word}")(n, ns),
                "watch_fn": getattr(c, f"list_namespaced_{word}"),
                "watch_args": {"namespace": ns},
            }
        raise ValueError(f"未対応のkind: {kind}")

    async def get(self, kind: str, name: str) -> dict[str, Any]:
        try:
            return self._ser(await self._ops(kind)["get"](name))
        except Exception as e:
            raise _translate(e) from e

    async def list(self, kind: str, label_selector: str | None = None) -> list[dict[str, Any]]:
        try:
            res = self._ser(await self._ops(kind)["list"](label_selector))
        except Exception as e:
            raise _translate(e) from e
        return list(res.get("items", []))

    async def create(self, kind: str, obj: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._ser(await self._ops(kind)["create"](obj))
        except Exception as e:
            raise _translate(e) from e

    async def replace(self, kind: str, obj: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._ser(await self._ops(kind)["replace"](obj["metadata"]["name"], obj))
        except Exception as e:
            raise _translate(e) from e

    async def patch(self, kind: str, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._ser(await self._ops(kind)["patch"](name, patch))
        except Exception as e:
            raise _translate(e) from e

    async def delete(self, kind: str, name: str) -> None:
        try:
            await self._ops(kind)["delete"](name)
        except Exception as e:
            raise _translate(e) from e

    async def watch(
        self, kind: str, label_selector: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        from kubernetes_asyncio import watch

        ops = self._ops(kind)
        w = watch.Watch()
        kwargs = dict(ops["watch_args"])
        if label_selector:
            kwargs["label_selector"] = label_selector
        try:
            async for ev in w.stream(ops["watch_fn"], timeout_seconds=300, **kwargs):
                yield {
                    "type": ev["type"],
                    "object": ev.get("raw_object") or self._ser(ev["object"]),
                }
        finally:
            await w.close()
