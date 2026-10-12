"""ConfigMapの1キーずつの更新（楽観ロックでリトライ）。"""

from __future__ import annotations

from ..ports import ConflictError, KubeApi, NotFoundError


async def update_key(kube: KubeApi, name: str, namespace: str, key: str, value: str | None) -> None:
    """ConfigMap `name` の `data[key]` を設定する（Noneなら削除）。なければ作る。"""
    for _ in range(8):
        try:
            cm = await kube.get("configmap", name)
        except NotFoundError:
            if value is None:
                return
            try:
                await kube.create(
                    "configmap",
                    {"metadata": {"name": name, "namespace": namespace}, "data": {key: value}},
                )
                return
            except ConflictError:
                continue
        data = dict(cm.get("data") or {})
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        cm["data"] = data
        try:
            await kube.replace("configmap", cm)
            return
        except ConflictError:
            continue
    raise RuntimeError(f"ConfigMap {name} を更新できませんでした")


async def read_all(kube: KubeApi, name: str) -> dict[str, str]:
    try:
        return dict((await kube.get("configmap", name)).get("data") or {})
    except NotFoundError:
        return {}
