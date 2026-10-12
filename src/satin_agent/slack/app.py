"""Slack Botの組み立て（`satin slack`）。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from typing import Any

from ..cma.configmap import read_all
from ..cma.states import CONFIGMAP as STATES_CONFIGMAP
from ..config import Config, ConfigError, anthropic_api_key
from ..guards.bulk_files import check_bash
from ..guards.config import GuardsConfig
from ..guards.filters import InboundMessage, RuleFilter, UsersInfo
from ..guards.gemini import GeminiClient, InputGuard
from ..guards.model_armor import ModelArmorClient, OutputInspector
from ..hooks import HookDispatcher, HookEvent
from ..ports import Clock
from .core import ConversationCore, RegisterUser, SandboxState, SandboxStatus, Settings
from .ports import ChatChannel, ConvRef, Inbound, SessionsPort
from .progress import load_timezone
from .store import BotStore

log = logging.getLogger("satin.slack")


def build_core(
    cfg: Config,
    *,
    sessions: SessionsPort,
    store: BotStore,
    channel: ChatChannel,
    clock: Clock,
    users: UsersInfo,
    bot_user_id: str,
    gemini: GeminiClient | None = None,
    armor: ModelArmorClient | None = None,
    dispatcher: HookDispatcher | None = None,
    sandbox_state: SandboxState | None = None,
    register_user: RegisterUser | None = None,
) -> ConversationCore:
    """設定とport実装から会話の中核を組み立てる。ガードは設定で無効にできる。"""
    guards = GuardsConfig.from_dict(cfg.get("guards"))
    rule_filter = RuleFilter(guards.filters, users, clock, bot_user_id)

    async def rules(m: Inbound) -> tuple[bool, str | None]:
        r = await rule_filter.check(
            InboundMessage(
                team=m.team,
                channel=m.conv.channel,
                user=m.user,
                text=m.text,
                ts=m.ts,
                thread_ts=None if m.conv.thread_ts == "-" else m.conv.thread_ts,
                is_dm=m.is_dm,
                bot_id=m.bot_id,
                subtype=m.subtype,
            )
        )
        return r.allowed, r.reply

    input_guard = None
    if guards.input_guard.enabled:
        if gemini is None:
            raise ConfigError("guards.input_guard が有効ですが、Geminiのクライアントがありません")
        input_guard = InputGuard.from_config(gemini, guards.input_guard).check
    inspect = None
    if guards.output_inspection.enabled:
        if armor is None:
            raise ConfigError(
                "guards.output_inspection が有効ですが、Model Armorのクライアントがありません"
            )
        inspect = OutputInspector.from_config(armor, guards.output_inspection).inspect

    def emit_hook(kind: str, session_id: str | None, data: dict[str, Any]) -> None:
        if dispatcher is not None:
            dispatcher.emit(HookEvent(kind=kind, session_id=session_id, ts=clock.now(), data=data))

    s = cfg.get("slack", {}) or {}
    try:
        load_timezone(str(s.get("timezone", Settings.timezone)))  # 起動時に設定の誤りを検出する
    except Exception as e:
        raise ConfigError(f"slack.timezone が不正です: {e}") from e
    return ConversationCore(
        sessions=sessions,
        store=store,
        channel=channel,
        clock=clock,
        settings=Settings(
            stop_words=tuple(s.get("stop_words", Settings.stop_words)),
            heartbeat_seconds=float(s.get("heartbeat_seconds", 60)),
            timezone=str(s.get("timezone", Settings.timezone)),
        ),
        rules=rules,
        input_guard=input_guard,
        inspect=inspect,
        check_bash=check_bash,
        emit_hook=emit_hook,
        sandbox_state=sandbox_state,
        register_user=register_user,
    )


def sandbox_state_from_configmap(kube: Any) -> SandboxState:
    """オーケストレータがConfigMapに書いたSandboxの状態を読む。"""
    import json

    async def state(session_id: str) -> SandboxStatus | None:
        raw = (await read_all(kube, STATES_CONFIGMAP)).get(session_id)
        if not raw:
            return None
        d = json.loads(raw)
        return SandboxStatus(d.get("state", ""), float(d.get("at", 0.0)), d.get("error", ""))

    return state


async def run_bot(cfg: Config, env: Mapping[str, str] | None = None) -> int:
    from anthropic import AsyncAnthropic
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    from ..assets.cma import State
    from ..cma.users import UserDirectory
    from ..commands import _SystemClock
    from ..guards.adapters import GenAiGeminiClient, ModelArmorSdkClient
    from ..hooks import load_hooks
    from ..sandbox.config import SandboxConfig
    from ..sandbox.kube import KubeAsyncio
    from .channel import SlackChat, SlackEventRouter, SlackReceiver, SlackUserDirectory
    from .sessions import AnthropicSessions

    e = os.environ if env is None else env
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SATIN_HMAC_KEY"):
        if not e.get(name):
            raise ConfigError(f"環境変数 {name} が設定されていません")
    env_id = e.get("CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID") or ""
    if not env_id:
        raise ConfigError("環境変数 CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID が設定されていません")
    state_path = cfg.path("assets.state_file", ".satin-state.json")
    agent_id = cfg.get("agent.id") or (
        State(state_path).data.get("agent_id") if state_path else None
    )
    if not agent_id:
        raise ConfigError(
            "Agent IDがありません（設定の agent.id か、satin publish の状態ファイル）"
        )

    guards = GuardsConfig.from_dict(cfg.get("guards"))
    hooks = load_hooks(cfg.get("hooks", [{"factory": "json_lines"}]))  # 読み込めなければ起動しない
    dispatcher = HookDispatcher(hooks)
    await dispatcher.start()

    clock: Clock = _SystemClock()
    scfg = SandboxConfig.from_config(cfg)
    app = AsyncApp(token=e["SLACK_BOT_TOKEN"])
    auth = await app.client.auth_test()
    users = SlackUserDirectory(
        app.client,
        clock,
        workspace_team=auth.get("team_id", ""),
        workspace_enterprise=auth.get("enterprise_id", "") or "",
    )
    store = BotStore(str(cfg.path("slack.db_path", "satin-bot.db")))
    async with KubeAsyncio(scfg.namespace, cfg.get("kubernetes.context")) as kube:
        directory = UserDirectory(kube, scfg.namespace, e["SATIN_HMAC_KEY"].encode())
        core = build_core(
            cfg,
            sessions=AnthropicSessions(
                AsyncAnthropic(api_key=anthropic_api_key(e)),
                agent_id=agent_id,
                environment_id=env_id,
                vault_ids=[str(v) for v in (cfg.get("agent.vault_ids") or [])],
            ),
            store=store,
            channel=SlackChat(app.client),
            clock=clock,
            users=users,
            bot_user_id=str(auth["user_id"]),
            gemini=GenAiGeminiClient(guards.input_guard) if guards.input_guard.enabled else None,
            armor=ModelArmorSdkClient(guards.output_inspection)
            if guards.output_inspection.enabled
            else None,
            dispatcher=dispatcher,
            sandbox_state=sandbox_state_from_configmap(kube),
            register_user=directory.register,
        )
        router = SlackEventRouter(
            core.handle,
            bot_user_id=str(auth["user_id"]),
            is_participating=lambda c: store.get_session(c.key) is not None,
            users=users,
        )
        receiver = SlackReceiver(app, router)
        receiver.register()
        recovered = await core.recover()  # 前回のBotが実行中のまま残したturnを引き継ぐ
        log.info("recovered %d turn(s)", recovered)
        handler = AsyncSocketModeHandler(app, e["SLACK_APP_TOKEN"])
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await handler.connect_async()
        await stop.wait()
        await handler.close_async()
        await receiver.wait_idle()
        await core.shutdown()
        await dispatcher.aclose()
    store.close()
    return 0


__all__ = ["build_core", "run_bot", "sandbox_state_from_configmap", "ConvRef"]
