"""content-review card（图文内容审查）。

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    cards/content_review/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/content-review)
    ├── service.py         # 决策模型启停 + 一次结构化决策
    ├── static/            # template.html + app.js + app.css + wizard.json
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from wdnapps_audio_chat.cards.content_review.api import router

LOG = __import__("logging").getLogger("apps.content_review")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/content-review``."""
    app.include_router(router, prefix="/api/content-review")
    LOG.info("content_review: API mounted at /api/content-review")


def register_shutdown() -> None:
    """Release the card's service singletons.

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import traceback
    cleanup_targets = []
    try:
        from wdnapps_audio_chat.cards.content_review import service as _service_mod
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
                    LOG.warning("content_review: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
