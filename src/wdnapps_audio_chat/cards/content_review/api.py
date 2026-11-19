"""图文内容审查卡片的 REST API。

端点
====

* ``GET    /api/content-review/status``   — 状态（模型就绪没 / 卡位 / 上限）。
* ``POST   /api/content-review/enable``   — 启用（起决策模型实例）。
* ``POST   /api/content-review/disable``  — 禁用（停决策模型实例）。
* ``POST   /api/content-review/review``   — 审一次（图文 + 列表 → 概率）。

⚠️ **模型可切换**（2026-11-19）：0.8B 与 4B 同规格同协议，界面上拉选。
⇒ 每个端点都接受 ``model_id``（``status`` 是 query，其余在 body 里），
不传 = 默认模型。

错误码
======

* 400 —— 参数不合法 / 模型没装 / 模型未启用。
* 502 —— 推理本身失败。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from wdnapps_audio_chat.cards.content_review.service import (
    MAX_ITEMS,
    MAX_TEXT_CHARS,
    get_service,
)

LOG = logging.getLogger("apps.content_review_api")
router = APIRouter(tags=["content-review"])


# --------------------------------------------------------------------------- #
# Pydantic
# --------------------------------------------------------------------------- #
class ItemPayload(BaseModel):
    """列表里的一项（一个审查维度）。"""

    model_config = {"extra": "ignore"}
    #: 维度名（如「广告」）—— 它会**直接进模型的 prompt**，所以别写太长。
    name: str = Field(default="", max_length=60)
    #: 判定说明（可选）—— 告诉模型这一项具体指什么。
    description: str = Field(default="", max_length=200)


class EnablePayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 卡位；不传 = 默认第一张 1828 协卡（0 号）
    npu_index: Optional[int] = Field(default=None, ge=0, le=15)
    #: 用哪个决策模型；不传 = 默认（4B）。见 ``service.MODEL_IDS``。
    model_id: Optional[str] = Field(default=None, max_length=64)


class DisablePayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 停哪个决策模型；不传 = 默认（4B）。
    model_id: Optional[str] = Field(default=None, max_length=64)


class ReviewPayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 待审查的文字（state）。可以只有图、只有字，或两者都有。
    text: str = Field(default="", max_length=MAX_TEXT_CHARS)
    #: 可增删的审查维度列表（最多 16 个 —— 模型硬上限）。
    items: List[ItemPayload] = Field(default_factory=list)
    #: 图片（base64，可带 data URL 前缀）。空 = 纯文本审查。
    image_b64: str = Field(default="", max_length=16 * 1024 * 1024)
    #: 用哪个决策模型；不传 = 默认（4B）。
    model_id: Optional[str] = Field(default=None, max_length=64)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
# ⚠️ **端点是同步 ``def``，不是 ``async def``** —— 启停是**阻塞**操作：启用要
# 等模型加载完（约 40 秒），审查一次图文决策要 ~9 秒。
#
# FastAPI 只把 ``async def`` 端点跑在事件循环里，``def`` 端点丢线程池 ——
# 写成 ``async def`` 的话，这几十秒里**整个 Web UI 都没响应**（首页打不开、
# 算力页不刷新、WS 推送全停）。
#
@router.get("/status")
def get_content_review_status(model_id: Optional[str] = None) -> Dict[str, Any]:
    """当前状态快照；界面每次刷新拉一次。

    ``model_id`` = 看**哪个**模型的状态（界面上拉选中的那个）；不传 = 默认。
    响应里的 ``models`` 是全部候选模型，界面用它渲染下拉。
    """
    return get_service().status(model_id)


@router.post("/enable")
def post_content_review_enable(payload: EnablePayload) -> Dict[str, Any]:
    """启用：起决策模型的一个实例（默认第一张 1828 协卡）。

    这一步要等模型加载完（约 30~60 秒），所以是**慢端点** —— 界面上按钮会转圈。
    """
    try:
        return get_service().enable(payload.npu_index, payload.model_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/disable")
def post_content_review_disable(
        payload: Optional[DisablePayload] = None) -> Dict[str, Any]:
    """禁用：停掉决策模型实例。"""
    try:
        return get_service().disable(payload.model_id if payload else None)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/review")
def post_content_review_review(payload: ReviewPayload) -> Dict[str, Any]:
    """对图文按列表逐项做概率估计。

    图文决策本身是阻塞调用（实测 ~9 秒；首次还要加载视觉模型），所以同样
    是同步端点。
    """
    if len(payload.items) > MAX_ITEMS:
        raise HTTPException(
            status_code=400,
            detail="审查项最多 %d 个（模型上限），当前 %d 个"
                   % (MAX_ITEMS, len(payload.items)),
        )
    try:
        return get_service().review(
            text=payload.text,
            items=[i.model_dump() for i in payload.items],
            image_b64=payload.image_b64 or None,
            model_id=payload.model_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        LOG.exception("content_review: 审查失败")
        raise HTTPException(status_code=502, detail=f"审查失败: {e}") from e
