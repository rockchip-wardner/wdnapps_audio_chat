"""text-chat card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    cards/text_chat/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/text-chat)
    ├── service.py / *.py  # business logic
    ├── thinking_9b.py     # Qwen3.5-VL-9B thinking-output adapter
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from wdnapps_audio_chat.cards.text_chat.api import router

LOG = __import__("logging").getLogger("apps.text_chat")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/text-chat``."""
    app.include_router(router, prefix="/api/text-chat")
    LOG.info("text_chat: API mounted at /api/text-chat")


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import traceback
    cleanup_targets = []
    try:
        from wdnapps_audio_chat.cards.text_chat import service as _service_mod
        cleanup_targets.append(_service_mod)
    except Exception:
        pass
    for mod in cleanup_targets:
        for attr in ("close", "stop", "shutdown", "close_all"):
            fn = getattr(mod, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    LOG.warning("text_chat: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
