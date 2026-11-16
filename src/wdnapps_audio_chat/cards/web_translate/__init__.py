"""web-translate card（网页翻译）。

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    cards/web_translate/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/web-translate)
    ├── service.py         # 翻译服务的启停 + 状态
    ├── static/            # template.html + app.js + app.css + wizard.json
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from wdnapps_audio_chat.cards.web_translate.api import router

LOG = __import__("logging").getLogger("apps.web_translate")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/web-translate``."""
    app.include_router(router, prefix="/api/web-translate")
    LOG.info("web_translate: API mounted at /api/web-translate")


def register_shutdown() -> None:
    """Release the card's service singletons.

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import traceback
    cleanup_targets = []
    try:
        from wdnapps_audio_chat.cards.web_translate import service as _service_mod
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
                    LOG.warning("web_translate: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
