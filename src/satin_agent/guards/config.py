"""ガード類の設定。Botの設定ファイルの`guards`セクション（dict）から読み込む。

```yaml
guards:
  filters:
    teams: [T1]
    channels: [C1]
    users: [U1]
    allow_direct_messages: true
    deny_guests: true
    deny_external_users: true
    max_chars: 8000
    rate_limit_per_minute: 20
  input_guard:            # Gemini
    enabled: true
    model: gemini-3.8-flash
    project: my-project
    location: global
    criteria_file: guards/input-guard.md
    fail_open: false      # trueならGemini失敗時にメッセージを通す
  output_inspection:      # Model Armor
    enabled: true
    template: projects/p/locations/l/templates/t
    location: us-central1
    ignored_filters: [pi_and_jailbreak]
```
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from satin_agent.config import ConfigError

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_IGNORED_FILTERS = ("pi_and_jailbreak",)


@dataclass(frozen=True)
class FilterConfig:
    """ルールのフィルタ。許可リストが空なら全て許可。"""

    teams: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    allow_direct_messages: bool = True
    deny_guests: bool = False
    deny_external_users: bool = False
    max_chars: int = 8000
    rate_limit_per_minute: int = 20
    users_cache_seconds: float = 600.0


@dataclass(frozen=True)
class GeminiConfig:
    enabled: bool = True
    model: str = DEFAULT_GEMINI_MODEL
    project: str = ""
    location: str = "global"
    # Noneなら同梱の既定の基準（guards/input_guard.md）を使う
    criteria_file: str | None = None
    history_size: int = 5
    # Geminiの呼び出し失敗時に通すか。既定はブロック
    fail_open: bool = False


@dataclass(frozen=True)
class ModelArmorConfig:
    enabled: bool = True
    # projects/<p>/locations/<l>/templates/<t>
    template: str = ""
    location: str = "us-central1"
    endpoint: str | None = None
    ignored_filters: tuple[str, ...] = DEFAULT_IGNORED_FILTERS
    on_sdp_match: str = "mask"  # mask | block
    chunk_chars: int = 8000


@dataclass(frozen=True)
class GuardsConfig:
    filters: FilterConfig = field(default_factory=FilterConfig)
    input_guard: GeminiConfig = field(default_factory=GeminiConfig)
    output_inspection: ModelArmorConfig = field(default_factory=ModelArmorConfig)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> GuardsConfig:
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ConfigError("guardsはマッピングにしてください")
        unknown = set(raw) - {"filters", "input_guard", "output_inspection"}
        if unknown:
            raise ConfigError(f"guardsに不明なキーがあります: {sorted(unknown)}")
        return cls(
            filters=_build(FilterConfig, raw.get("filters"), "guards.filters"),
            input_guard=_build(GeminiConfig, raw.get("input_guard"), "guards.input_guard"),
            output_inspection=_build(
                ModelArmorConfig, raw.get("output_inspection"), "guards.output_inspection"
            ),
        ).validated()

    def validated(self) -> GuardsConfig:
        if self.output_inspection.enabled and not self.output_inspection.template:
            raise ConfigError("guards.output_inspection.templateを指定してください")
        if self.output_inspection.on_sdp_match not in ("mask", "block"):
            raise ConfigError("guards.output_inspection.on_sdp_matchはmaskかblockです")
        if self.filters.max_chars <= 0:
            raise ConfigError("guards.filters.max_charsは正の数にしてください")
        return self


def _build[T](cls: type[T], raw: Any, where: str) -> T:
    if raw is None:
        return cls()
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}はマッピングにしてください")
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    unknown = set(raw) - set(fields)
    if unknown:
        raise ConfigError(f"{where}に不明なキーがあります: {sorted(unknown)}")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        default = fields[key].default
        if isinstance(default, tuple):
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise ConfigError(f"{where}.{key}はリストにしてください")
            value = tuple(str(v) for v in value)
        values[key] = value
    return cls(**values)
