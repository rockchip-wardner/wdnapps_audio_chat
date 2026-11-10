"""qwen3-asr card —— Qwen3-ASR 流式语音识别。

Manifest-driven backend entry point. The plugin loader
(:mod:`app.core.manifest`) imports this module and calls :func:`register_api`
to mount the router and :func:`register_shutdown` to register the cleanup
hook.

Backend layout (per card)::

    wdnapps_audio_chat/cards/qwen3_asr/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/qwen3-asr)
    ├── service.py         # business logic (singleton + WS broadcast)
    ├── board_mic.py       # 板载麦克风采集子进程管理
    ├── static/            # template.html + app.js + app.css + wizard.json
    │                      # + capture-worklet.js + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*`` (plus the SDK). No cross-card
imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from wdnapps_audio_chat.cards.qwen3_asr.api import router

LOG = __import__("logging").getLogger("apps.qwen3_asr")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/qwen3-asr``."""
    app.include_router(router, prefix="/api/qwen3-asr")
    LOG.info("qwen3_asr: API mounted at /api/qwen3-asr")


def register_shutdown() -> None:
    """Release the card's resources: service singleton + 板载采集子进程。

    Best-effort: each cleanup call is wrapped in try/except so a single
    failure does not break the rest of the shutdown sequence.
    """
    import importlib
    import traceback

    cleanup_targets = []
    for subname in ("service", "board_mic"):
        try:
            cleanup_targets.append(importlib.import_module(
                "wdnapps_audio_chat.cards.qwen3_asr." + subname))
        except Exception:
            continue

    for mod in cleanup_targets:
        for attr in ("close", "stop", "shutdown", "close_all"):
            fn = getattr(mod, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    LOG.warning("qwen3_asr: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())

    # 板载采集子进程由 ``board_mic`` 的模块级单例持有（``atexit`` 只是
    # 兜底）—— 宿主正常退出时 atexit 会跑，但 SIGTERM 直接杀进程时不会，
    # 采集进程会变孤儿。显式收一次。
    try:
        from wdnapps_audio_chat.cards.qwen3_asr.board_mic import get_board_mic
        get_board_mic().shutdown()
    except Exception:
        LOG.debug("qwen3_asr: board mic shutdown skipped", exc_info=True)
