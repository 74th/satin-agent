"""実サービスのアダプタ（Gemini on Agent Platform、Model Armor）。

認証はBot PodのKSAのWorkload Identity（ADC）。SDKのimportは、ユニットテストが
SDKなしで動くよう、使う時点まで遅らせる。
"""

from __future__ import annotations

import json
from typing import Any

from satin_agent.guards.config import GeminiConfig, ModelArmorConfig
from satin_agent.guards.gemini import RESPONSE_SCHEMA
from satin_agent.guards.model_armor import ArmorResult


class GenAiGeminiClient:
    """`google-genai`（Vertex AIモード）でJSONを生成する。"""

    def __init__(self, config: GeminiConfig, *, client: Any | None = None) -> None:
        self._model = config.model
        self._project = config.project
        self._location = config.location
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project or None, location=self._location
            )
        return self._client

    async def generate_json(self, prompt: str) -> dict:
        from google.genai import types

        response = await self._get_client().aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.0,
            ),
        )
        data = json.loads(response.text)
        if not isinstance(data, dict):
            raise ValueError("Gemini returned a non-object JSON")
        return data


class ModelArmorSdkClient:
    """`google-cloud-modelarmor`の`sanitize_model_response`を呼ぶ。"""

    def __init__(self, config: ModelArmorConfig, *, client: Any | None = None) -> None:
        self._template = config.template
        self._endpoint = config.endpoint or (
            f"modelarmor.{config.location}.rep.googleapis.com" if config.location else None
        )
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import modelarmor_v1

            options = ClientOptions(api_endpoint=self._endpoint) if self._endpoint else None
            self._client = modelarmor_v1.ModelArmorAsyncClient(client_options=options)
        return self._client

    async def sanitize(self, text: str) -> ArmorResult:
        from google.cloud import modelarmor_v1

        request = modelarmor_v1.SanitizeModelResponseRequest(
            name=self._template,
            model_response_data=modelarmor_v1.DataItem(text=text),
        )
        response = await self._get_client().sanitize_model_response(request=request)
        return to_armor_result(response.sanitization_result)


def to_armor_result(result: Any) -> ArmorResult:
    """SDKの`SanitizationResult`を`ArmorResult`にする。"""
    from google.cloud.modelarmor_v1 import FilterMatchState

    match = FilterMatchState.MATCH_FOUND
    if result.filter_match_state != match:
        return ArmorResult(False)
    matched: set[str] = set()
    sdp_matched = False
    masked: str | None = None
    for name, fr in result.filter_results.items():
        kind = type(fr).pb(fr).WhichOneof("filter_result")
        if kind is None:
            continue
        sub = getattr(fr, kind)
        if kind == "sdp_filter_result":
            inner_kind = type(sub).pb(sub).WhichOneof("result")
            if inner_kind is None:
                continue
            inner = getattr(sub, inner_kind)
            if inner.match_state == match:
                sdp_matched = True
                if inner_kind == "deidentify_result":
                    masked = inner.data.text or None
        elif sub.match_state == match:
            matched.add(name)
    return ArmorResult(True, frozenset(matched), sdp_matched, masked)
