"""ワーカーのアイドル停止を、バックグラウンドのタスクの間だけ止める。

SDKの`_beta_session_runner._IdleClock`は、turnが終わってから`max_idle`秒でrunnerを止める。
確認待ちのツールのために`hold()`・`release()`があり、hold中は時計が進まない（最後の
`release()`で、改めて進み始める）。実行中のバックグラウンドのタスク1つにつき、これを1回かける。

runnerは`_IdleClock()`を自分で作るので、`timeouts.py`と同じく起動時に`__init__`を包んで、
作られた時計を登録する。SDKの内部に依存するため、時計が登録されない（SDKが変わった）場合は、
黙ってタスクが殺される状態にせず、タスクの起動を失敗にする。
"""

from __future__ import annotations

from typing import Any

_clock: Any = None
_installed = False


class IdleClockMissing(RuntimeError):
    pass


def install() -> None:
    """`_IdleClock`を包む（何度呼んでもよい）。内部が変わっていたら、ここで失敗する。"""
    global _installed
    from anthropic.lib.tools import _beta_session_runner as runner

    cls = runner._IdleClock
    for name in ("hold", "release", "arm", "disarm"):
        if not callable(getattr(cls, name, None)):
            raise IdleClockMissing(
                f"SDKの_IdleClockに{name}()がありません（SDKの内部が変わりました）"
            )
    if _installed and getattr(cls.__init__, "_satin_wrapped", False):
        return
    original = cls.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        global _clock
        original(self, *args, **kwargs)
        _clock = self  # 1つのSandboxは1つのセッションだけを処理する

    init._satin_wrapped = True  # type: ignore[attr-defined]
    init._satin_original = original  # type: ignore[attr-defined]
    cls.__init__ = init  # type: ignore[method-assign]
    _installed = True


def _require() -> Any:
    if _clock is None:
        raise IdleClockMissing("ワーカーのアイドルの時計を取得できないため、タスクを起動できません")
    return _clock


def hold() -> None:
    _require().hold()


def release() -> None:
    _require().release()


def reset() -> None:
    """テスト用: 登録を消し、`__init__`を元に戻す。"""
    global _clock, _installed
    from anthropic.lib.tools import _beta_session_runner as runner

    cls = runner._IdleClock
    original = getattr(cls.__init__, "_satin_original", None)
    if original is not None:
        cls.__init__ = original  # type: ignore[method-assign]
    _clock, _installed = None, False
