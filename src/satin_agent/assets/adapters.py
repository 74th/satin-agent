"""実サービス向けのport実装（GCS、Anthropic）。"""

from __future__ import annotations

from typing import Any

from ..ports import NotFoundError


class GcsStore:
    """`gs://bucket/prefix/`配下を、prefixからの相対パスで読み書きする。"""

    def __init__(self, url: str, project: str | None = None) -> None:
        from google.cloud import storage

        if not url.startswith("gs://"):
            raise ValueError(f"gs:// で始まるURLを指定してください: {url}")
        bucket, _, prefix = url[len("gs://") :].partition("/")
        self._bucket = storage.Client(project=project).bucket(bucket)
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _blob(self, key: str):
        return self._bucket.blob(self._prefix + key)

    def read(self, key: str) -> bytes:
        blob = self._blob(key)
        if not blob.exists():
            raise NotFoundError(key)
        return blob.download_as_bytes()

    def write(self, key: str, data: bytes) -> None:
        self._blob(key).upload_from_string(data)

    def delete(self, key: str) -> None:
        self._blob(key).delete()

    def list(self) -> list[str]:
        return sorted(
            b.name[len(self._prefix) :]
            for b in self._bucket.list_blobs(prefix=self._prefix)
            if not b.name.endswith("/")
        )


class AnthropicApi:
    """`anthropic.Anthropic`の薄いラッパー。"""

    def __init__(self, client: Any) -> None:
        self._c = client

    @staticmethod
    def _files(files: dict[str, bytes]) -> list[tuple[str, bytes, str]]:
        return [(name, data, "application/octet-stream") for name, data in files.items()]

    def create_skill(self, display_name: str, files: dict[str, bytes]) -> dict[str, Any]:
        s = self._c.skills.create(files=self._files(files), display_name=display_name)
        return {"id": s.id, "latest_version_id": s.latest_version_id}

    def add_skill_version(self, skill_id: str, files: dict[str, bytes]) -> dict[str, Any]:
        v = self._c.skills.versions.create(skill_id=skill_id, files=self._files(files))
        return {"id": v.id}

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        a = self._c.beta.agents.retrieve(agent_id=agent_id)
        return {"id": a.id, "version": a.version}

    def create_agent(self, config: dict[str, Any]) -> dict[str, Any]:
        a = self._c.beta.agents.create(**config)
        return {"id": a.id, "version": a.version}

    def update_agent(self, agent_id: str, version: int, config: dict[str, Any]) -> dict[str, Any]:
        a = self._c.beta.agents.update(agent_id=agent_id, version=version, **config)
        return {"id": a.id, "version": a.version}
