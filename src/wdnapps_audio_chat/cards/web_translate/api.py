"""网页翻译卡片的 REST API。

端点
====

* ``GET    /api/web-translate/status``    — 当前状态（启用与否 / 卡位 / 错误）。
* ``POST   /api/web-translate/enable``    — 启用翻译服务（起模型 + 接进翻译链）。
* ``POST   /api/web-translate/disable``   — 禁用翻译服务（摘链 + 停模型）。
* ``POST   /api/web-translate/translate`` — 翻一批文案（界面上的「试一下」）。

错误码
======

* 400 —— 参数不合法 / 模型没装 / 卡位冲突 / 启动失败。
* 502 —— 翻译本身失败（provider 链里所有家都不行）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from wdnapps_audio_chat.cards.web_translate.service import get_service

LOG = logging.getLogger("apps.web_translate_api")
router = APIRouter(tags=["web-translate"])


# --------------------------------------------------------------------------- #
# Pydantic
# --------------------------------------------------------------------------- #
class EnablePayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 卡位；不传 = 默认第一张 1828 协卡（0 号）
    npu_index: Optional[int] = Field(default=None, ge=0, le=15)


class TranslatePayload(BaseModel):
    model_config = {"extra": "ignore"}
    segments: List[str] = Field(..., min_length=1, max_length=64)
    source: str = "zh-CN"
    target: str = "en"


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
#
# ⚠️ **端点是同步 ``def``，不是 ``async def``** —— 启停翻译服务是**阻塞**
# 操作：启用要等模型加载完（约 20~30s），禁用要等 unload + shutdown +
# SIGTERM 宽限（约 3~5s）。
#
# FastAPI 只把 ``async def`` 端点跑在事件循环里，``def`` 端点丢线程池 ——
# 写成 ``async def`` 的话，这 30 秒里**整个 Web UI 都没响应**（首页打不开、
# 算力页不刷新、WS 推送全停）。禁用端点同理。
#
@router.get("/status")
def get_web_translate_status() -> Dict[str, Any]:
    """当前状态快照；界面每次刷新拉一次。"""
    return get_service().status()


@router.post("/enable")
def post_web_translate_enable(payload: EnablePayload) -> Dict[str, Any]:
    """启用翻译服务：起模型实例（默认 0 号 1828 协卡）+ 把 NPU 接进翻译链。

    这一步要等模型加载完（约 20~30 秒），所以是**慢端点** —— 界面上
    按钮会转圈。
    """
    try:
        return get_service().enable(payload.npu_index)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/disable")
def post_web_translate_disable() -> Dict[str, Any]:
    """禁用翻译服务：把 NPU 摘出翻译链 + 停掉模型实例。"""
    try:
        return get_service().disable()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/translate")
def post_web_translate_translate(payload: TranslatePayload) -> Dict[str, Any]:
    """翻一批文案（走**和界面右上角完全同一条**路径）。

    NPU 翻译本身是阻塞调用（一次推理 0.1~0.5s，一批可能十几秒），
    所以同样是同步端点。
    """
    try:
        return get_service().translate(payload.segments, payload.source, payload.target)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
