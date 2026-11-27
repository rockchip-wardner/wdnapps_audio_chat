"""Qwen3-ASR REST + WebSocket API。

端点
=====

* ``GET    /api/qwen3-asr/``            — 当前 snapshot。
* ``GET    /api/qwen3-asr/rounds``       — 本次识别的逐轮明细（刷新页面后恢复）。
* ``POST   /api/qwen3-asr/upload``       — 上传音频/视频到设备端（向导的「上传…」）。
* ``POST   /api/qwen3-asr/transcribe``   — 发起一次流式识别。
  来源二选一：multipart 的 ``file``（上传内容）**或** form 的 ``path``
  （设备端路径，向导「浏览…」选出来的）。音频与**含音轨的视频**都支持。
  400 / 409 / 502。
* ``GET    /api/qwen3-asr/media?path=`` — 把**设备端**的音视频文件交给浏览器
  播放（「含音轨的视频」来源要把画面播出来）。支持 Range → ``<video>``
  可拖进度条。⚠️ 本服务**没有鉴权** → 路径被三道闸限制（根目录白名单 /
  扩展名白名单 / resolve 后仍在根内），详见 :func:`_resolve_media_path`。
* ``POST   /api/qwen3-asr/stop``         — 立即停止正在跑的识别。
* ``POST   /api/qwen3-asr/reset``        — 清空本轮结果。
* ``WS     /api/qwen3-asr/ws``           — server-push 流式增量 + 状态变更。
  连接建立后立即推 ``snapshot``。

实时（麦克风）端点
==================

* ``POST   /api/qwen3-asr/live/start``   — 开一个实时识别会话。
* ``POST   /api/qwen3-asr/live/chunk``   — 提交一块 PCM（裸 s16le）。
* ``POST   /api/qwen3-asr/live/finish``  — 收尾，返回最终文本。
* ``DELETE /api/qwen3-asr/live/stop``    — 立即中止。

板载麦克风（设备端 ALSA 采集）端点：

* ``GET    /api/qwen3-asr/live/board/devices`` — 可用采集设备列表。
* ``POST   /api/qwen3-asr/live/board/start``   — 起采集进程。
* ``POST   /api/qwen3-asr/live/board/stop``    — 停采集进程。
* ``GET    /api/qwen3-asr/live/board/status``  — 采集进程状态。

这四个端点由 **feature flag** ``asr.live.enabled`` 门控 —— 关掉时一律
**404**，前端隐藏实时区块，**文件 / 视频路径完全不受影响**。

实时翻译（流式同传，实时路径 + 文件 / 视频路径）端点：

* ``GET    /api/qwen3-asr/translate/status``     — 开关 / 目标语言 / 模型状态 / 统计。
* ``GET    /api/qwen3-asr/translate/languages`` — 可选的目标语言。
* ``POST   /api/qwen3-asr/translate/settings``  — 改开关 / 目标语言。
* ``POST   /api/qwen3-asr/translate/enable``    — 起翻译模型（**慢端点**，~30 s）。
* ``POST   /api/qwen3-asr/translate/disable``   — 停翻译模型。

WS 帧类型
=========

* ``snapshot`` —— 连接建立后立即推一次。
* ``state``   —— 状态字段 diff（含完整 snapshot）。
* ``round``   —— 一轮增量：``{round, committed, unfixed, language, final}``。
  ``committed`` 是**累计**的已确认文本，``unfixed`` 是本轮还没确认的尾巴
  （下一轮可能被改写）。
* ``done``    —— 识别结束：``{language, text, elapsed_sec}``。
* ``error``   —— 推理异常或 capability 未启用。
* ``gap``     —— **新增**：音频有丢失，识别结果可能有缺口。
* ``endpoint``—— **新增**：一句话结束（端点检测命中）。
* ``live_done`` —— **新增**：实时会话结束。
* ``translation`` —— **新增**：一句译文：
  ``{stream_id, index, source, text, status, reason, latency_ms}``。
  ``status`` ∈ ``done`` / ``failed`` / ``skipped`` / ``unavailable``；
  失败时 ``text`` 是**原文**（前端显示原文 + 标「未翻译」）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import (APIRouter, Body, File, Form, Header, HTTPException,
                     Query, Request, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from wdnapps_audio_chat.cards.qwen3_asr.board_mic import (DEFAULT_DEVICE,
                                                          BoardMicError,
                                                          get_board_mic)
from wdnapps_audio_chat.cards.qwen3_asr.service import (_LIVE_ENDPOINT_CONFIG,
                                                        _LIVE_SESSION_TTL_SEC,
                                                        _LOOKBACK_SEC,
                                                        _TR_LINE_INDEX_BASE,
                                                        get_asr_service,
                                                        get_live_asr_service)
from app.core.audio.capture import (CAPTURE_N_CHANNELS, CAPTURE_SAMPLE_RATE,
                                    CAPTURE_SAMPLE_WIDTH, DEFAULT_CHUNK_MS,
                                    validate_capture_format)
from app.core.audio.decode import MEDIA_EXTS, VIDEO_EXTS, iter_media_pcm
from app.core.config import asr_live_enabled

_LOG = logging.getLogger("qwen3_asr_api")
router = APIRouter(tags=["qwen3-asr"])

#: 单次上传上限。60 秒 48 kHz s24 单声道 ≈ 8.6 MB，留足余量；
#: 含音轨的视频会大得多 —— 走「设备端路径」时不受这个限制。
_MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: 常见容器在 ``mimetypes`` 里查不到的 MIME（``mkv`` / ``avi`` 之类）。
#: 浏览器靠 Content-Type 决定用哪个解码器 —— 给错会直接播不出来。
_MEDIA_TYPES = {
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".opus": "audio/opus",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".wma": "audio/x-ms-wma",
}

#: 向导「上传…」落盘目录。与视频卡共用 ``<resource_dir>/uploads``，
#: 这样从任一应用上传的文件，另一个应用直接用同一绝对路径不会产生副本。
_UPLOAD_DIR: Optional[Path] = None


def _upload_dir() -> Path:
    global _UPLOAD_DIR
    if _UPLOAD_DIR is None:
        from app.core.config import default_resource_root
        d = default_resource_root() / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        _UPLOAD_DIR = d
    return _UPLOAD_DIR


# --------------------------------------------------------------------------- #
# 设备端媒体文件（给 <video> 播）
# --------------------------------------------------------------------------- #
def _resolve_media_path(raw: str) -> Path:
    """把用户给的路径解析成**允许根目录内**的媒体文件。

    ⚠️ 这个 webui **没有鉴权** —— 这个端点等于「任意文件读取」入口
    （``?path=/etc/shadow`` 能把 shadow 读出来）。所以必须过闸：

    1. **根目录白名单** —— 与设备端文件浏览器（``/api/fs``）共用
       :func:`app.core.fs_paths.resolve_within_root`（根 = ``base.resource_dir``）。
       页面上的路径**就是从那个浏览器里选出来的** ⇒ 两处口径必须一致，
       否则用户选得到、却播不了；
    2. **扩展名白名单** —— 只允许 :data:`MEDIA_EXTS`（音视频）。

    ``resolve_within_root`` 自带「resolve 之后仍在根内」这道闸
    （``..`` 与指向根外的软链接都会被规范化掉）。
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="缺少 path 参数")
    if not Path(raw).is_absolute():
        # 相对路径会被 resolve 到**服务进程的 CWD** ⇒ 报错里会冒出
        # 用户压根没写过的路径，很难看懂。
        raise HTTPException(
            status_code=400,
            detail="path 必须是设备端的绝对路径: {}".format(raw))

    from app.core.fs_paths import resolve_within_root
    resolved = resolve_within_root(raw)

    if resolved.suffix.lower() not in MEDIA_EXTS:
        raise HTTPException(
            status_code=400,
            detail="不是可播放的音视频文件（支持 {}）".format(
                ", ".join(sorted(MEDIA_EXTS))))

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return resolved


def _media_type(path: Path) -> str:
    """猜 MIME；``mimetypes`` 认不出的容器用内置表。"""
    import mimetypes

    ext = path.suffix.lower()
    if ext in _MEDIA_TYPES:
        return _MEDIA_TYPES[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _source_kind(name: str) -> str:
    """按**文件名后缀**判定来源类型：``video``（含音轨的视频）/ ``audio``。

    ⚠️ 只认后缀，**不做内容嗅探** —— 后缀是用户给的（上传的文件名 /
    设备端路径），填错了用户自己一眼看得见；而嗅探要解容器，慢，
    而且对「无音轨的视频」会判错（视频容器没音轨时按视频处理才对，
    因为它本来就不该出现在音频路径上）。

    这个值会**跟着轮次一起存到服务端**（见
    :meth:`service.AsrService.transcribe_stream` 的 ``source_info``）——
    页面刷新后前端靠它把轮次还原到**正确的来源页签**下。
    """
    ext = Path(name or "").suffix.lower()
    return "video" if ext in VIDEO_EXTS else "audio"


@router.get("/media")
async def get_media(path: str = Query(..., description="设备端媒体文件绝对路径")):
    """把设备端的音视频文件交给浏览器播放。

    用 ``FileResponse`` 而不是手写流式：Starlette 的 ``FileResponse``
    **支持 Range 请求** —— ``<video>`` 拖进度条、``<audio>`` 定位都靠它。
    没有 Range 的话浏览器只能从头播、拖不动。

    ⚠️ 这个端点**没有鉴权**，所以路径必须过
    :func:`_resolve_media_path` 的闸（绝对路径 / 根目录 / 扩展名 / 存在性）。
    """
    p = _resolve_media_path(path)
    return FileResponse(p, media_type=_media_type(p),
                        filename=p.name, content_disposition_type="inline")


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/")
async def get_asr_snapshot() -> dict:
    """完整 snapshot；UI 在进入页面时拉一次，后续靠 WS 推送增量。"""
    return get_asr_service().snapshot()


@router.get("/rounds")
async def get_asr_rounds() -> dict:
    """本次识别的逐轮明细（页面刷新后恢复）。

    响应形状：``{"rounds": [...], "source": {...}, "sentences": [...]}``。

    ``source`` —— 这批轮次**归属的来源**（``{"kind", "name", "path"}``）。
    ⚠️ 必须由**服务端**给，不能让前端自己猜：前端只知道「当前停在
    哪个页签」，刷新后那个页签是**默认值**（音频文件）→ 猜出来的
    归属会把**视频**的轮次画到音频页签下（或反之）= 串台。

    ``sentences`` —— 本次识别切出来的**句子**（原文 + 译文成对的那份原文）。
    ⚠️ 轮次能恢复、句子也得能恢复 ⇒ 否则刷新后结果框是空的。
    """
    svc = get_asr_service()
    return {"rounds": svc.rounds(), "source": svc.rounds_source(),
            "sentences": svc.sentences()}


@router.post("/upload")
async def post_asr_upload(file: UploadFile = File(...)) -> dict:
    """向导「上传…」→ 落到设备端 ``<resource_dir>/uploads``。

    返回 ``{path, name, size}`` —— 与向导框架的 upload 契约一致
    （``wizard.json`` 的 ``upload_endpoint``）。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not Path(safe_name).suffix:
        safe_name += ".bin"
    dest = _upload_dir() / safe_name
    try:
        # 同名覆盖：先 unlink 再 open("wb") —— 目标文件若由别的属主创建，
        # open("wb") 会因 inode 写权限失败，unlink 走的是目录级写权限。
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="write failed: {}".format(e))
    finally:
        try:
            file.file.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "path": str(dest),
        "name": file.filename,
        "size": dest.stat().st_size,
    }


@router.post("/transcribe")
async def post_asr_transcribe(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    chunk_seconds: Optional[float] = Form(None),
    max_new_tokens: Optional[int] = Form(None),
    rollback_tokens: Optional[int] = Form(None),
    rollback_rounds: Optional[int] = Form(None),
    start_sec: Optional[float] = Form(None),
    append: Optional[bool] = Form(None),
    translate_mode: Optional[str] = Form(None),
) -> dict:
    """发起一次流式识别。

    来源二选一：

    * ``file`` —— multipart 上传的音频 / 含音轨的视频；
    * ``path`` —— 设备端文件路径（向导「浏览…」选出来的）。

    ⚠️ **不再截断到 60 s** —— 那是 C demo 离线在线编码器的限制
    （``ONLINE_MAX_AUDIO_SECONDS``）。这条路径改走**增量会话**
    （每 1 秒一块喂给 SDK），长录音的代价是线性的 → 不该再砍尾巴。

    ⚠️ 解码（WAV 解析 / PyAV）是**阻塞 I/O** —— 解码器在 worker 线程
    里**边解边喂**，整个过程都不占事件循环（否则整个 Web 服务会卡住，
    与 model-download 清单端点同一个坑）。

    ``start_sec`` —— 从**容器的第几秒**开始识别（秒，0 = 从头）。
    「拖动进度条 → 字幕跟过去」用它：不用每次都从头解一遍 98 分钟的
    音轨（实测 PyAV seek 到 5000 s 只要 0.04 s，定位误差 ±20 ms）。
    轮次时间戳仍是**绝对**音频时间 → 前端的时间轴不用改。

    ``append`` —— **保留**已有的轮次（而不是清空）。拖动后重开识别时
    用它：前面识别过的部分要留在字幕时间轴上。

    ``translate_mode`` —— **翻译的单位**：

    * ``"sentence"``（默认）—— 后端按标点断句、逐句翻译（音频文件）。
    * ``"line"`` —— 后端**不**做句子级翻译；译文由前端逐行来要
      （``POST /translate/line``）。视频来源用这个：字幕的显示单位是
      「行」，译文必须逐行跟着。
    """
    if file is not None and file.filename:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="音频内容为空")
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail="音频过大（上限 {} MB）；大文件请用设备端路径".format(
                    _MAX_UPLOAD_BYTES // (1024 * 1024)),
            )
        source: Any = raw
        name = file.filename
    elif path and path.strip():
        source = path.strip()
        name = os.path.basename(source)
    else:
        raise HTTPException(status_code=400, detail="没有音频来源（file 或 path）")

    options = {}
    if chunk_seconds is not None:
        options["online_chunk_seconds"] = float(chunk_seconds)
    if max_new_tokens is not None:
        options["online_max_new_tokens"] = int(max_new_tokens)
    if rollback_tokens is not None:
        options["online_rollback_tokens"] = int(rollback_tokens)
    if rollback_rounds is not None:
        options["online_rollback_rounds"] = int(rollback_rounds)

    svc = get_asr_service()
    start_at = max(0.0, float(start_sec or 0.0))
    # ⚠️ **回看**：拖动进度条后重开识别时，新会话是**零上下文**的 ⇒
    #    模型会**重新判语种**（中文音频被判成 English / Russian）⇒
    #    头几轮的假设文本是外文（实测冒出过「Мыщение」）⇒ 那一段的
    #    译文也会跟着失败（源语种认不出来）。
    #
    # ⇒ **多解前面一段**当上下文（``_LOOKBACK_SEC`` 秒），那一段是
    # **纯上下文**（不参与停滞 / 停顿检测、句子级翻译也会跳过它）。
    #
    # ⚠️ 时间戳仍然是**绝对**音频时间 ⇒ 前端的时间轴不用改。
    decode_at = max(0.0, start_at - _LOOKBACK_SEC)
    lookback_sec = start_at - decode_at

    # 这批轮次**归属的来源** —— 服务端记着，页面刷新后前端靠它把轮次
    # 还原到正确的来源页签（见 ``GET /rounds`` 的 ``source``）。
    #
    # ⚠️ ``path`` 只有「设备端路径」时才有值：浏览器上传的 ``File``
    # 对象刷新后**拿不回来** → 路径留空，但轮次 / 文本照常恢复。
    source_info = {
        "kind": _source_kind(name),
        "name": name,
        "path": (source.strip() if isinstance(source, str) else ""),
    }

    def _factory():
        """惰性解码器 —— 真正的解码在 worker 线程里做（阻塞 I/O）。"""
        return iter_media_pcm(source, filename=name, chunk_seconds=1.0,
                              start_sec=decode_at)

    try:
        rid = svc.transcribe_stream(_factory, options=options,
                                     append=bool(append), start_sec=start_at,
                                     lookback_sec=lookback_sec,
                                     source_info=source_info,
                                     # ⚠️ **翻译的单位** —— ``"line"`` = 前端逐行
                                     # 来要译文（视频来源：字幕逐行推进，译文
                                     # 必须逐行跟着，见 ``post_translate_line``）。
                                     tr_mode=(translate_mode or "sentence"),
                                     # ⚠️ **主循环**：译文要广播给 WS 订阅者，
                                     # 而订阅队列属于主循环 ⇒ 必须把它传下去
                                     # （识别自己跑在另一个线程的另一个循环里，
                                     #  那个循环识别一结束就关掉）。
                                     loop=asyncio.get_running_loop())
    except ValueError as e:
        msg = str(e)
        if "上一条" in msg:
            return JSONResponse(
                status_code=409,
                content={"detail": msg, "snapshot": svc.snapshot()},
            )
        raise HTTPException(status_code=400, detail=msg) from e
    except RuntimeError as e:
        # 「功能未启用: swcap_audio_asr」 —— 模型没在算力页启动。
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "ok": True,
        "request_id": rid,
        "source_name": name,
        "snapshot": svc.snapshot(),
    }


@router.post("/stop")
async def post_asr_stop(
    reason: str = Query("user", description=(
        "谁喊的停：``user`` = 用户点了「停止」（画面也要停，字幕别再滚）；"
        "``internal`` = 前端为了「拖动进度条 → 重开识别」而停"
        "（画面**不能**停，用户正在看视频）。")),
) -> dict:
    """立即停止正在跑的识别。

    ``reason`` —— 「谁喊的停」。后端看这两种动作**一模一样**（都是停掉
    正在跑的识别），但对前端的含义相反：用户喊停 ⇒ 画面停；内部为了
    重开识别而停 ⇒ 画面继续（字幕还要跟着播放头走）。⇒ 由调用方说。
    """
    svc = get_asr_service()
    svc.stop(reason=reason)
    return {"ok": True, "snapshot": svc.snapshot()}


@router.post("/reset")
async def post_asr_reset() -> dict:
    """清空本轮结果。"""
    svc = get_asr_service()
    svc.reset()
    return {"ok": True, "snapshot": svc.snapshot()}


# --------------------------------------------------------------------------- #
# 实时（麦克风）会话
# --------------------------------------------------------------------------- #
def _require_live_enabled() -> None:
    """feature flag 门控。

    ``asr.live.enabled=false`` → 404（不是 403）：这个特性对外**不存在**，
    前端据此隐藏实时区块，文件 / 视频路径完全不受影响。
    """
    if not asr_live_enabled():
        raise HTTPException(status_code=404, detail="实时识别未启用")


@router.post("/live/start")
async def post_live_start(body: Optional[dict] = Body(None)) -> dict:
    """开一个实时识别会话。

    请求体（JSON）：

    ==============  =======  =======================================
    字段             默认      说明
    ==============  =======  =======================================
    ``source``       —         ``"browser"`` / ``"board"``
    ``sample_rate``  16000     **必须**是 16000
    ``n_channels``   1         只支持 1
    ``sample_width`` 2         只支持 2（s16le）
    ``chunk_ms``     500       采集端攒块粒度
    ==============  =======  =======================================
    """
    _require_live_enabled()
    data = body or {}
    source = str(data.get("source") or "browser").strip().lower()
    if source not in ("browser", "board"):
        raise HTTPException(status_code=400,
                            detail="source 必须是 browser 或 board")
    try:
        sample_rate = int(data.get("sample_rate") or CAPTURE_SAMPLE_RATE)
        n_channels = int(data.get("n_channels") or CAPTURE_N_CHANNELS)
        sample_width = int(data.get("sample_width") or CAPTURE_SAMPLE_WIDTH)
        chunk_ms = int(data.get("chunk_ms") or DEFAULT_CHUNK_MS)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="参数类型不对") from None
    try:
        validate_capture_format(sample_rate, n_channels, sample_width)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not (50 <= chunk_ms <= 5000):
        raise HTTPException(status_code=400,
                            detail="chunk_ms 必须在 50..5000 之间")

    mgr = get_live_asr_service()
    session = mgr.create(
        source=source, sample_rate=sample_rate, n_channels=n_channels,
        sample_width=sample_width, chunk_ms=chunk_ms)
    try:
        session.select_session()
    except RuntimeError as e:
        # 「功能未启用: swcap_audio_asr」 —— 模型没在算力页启动。
        mgr.drop(session.stream_id)
        raise HTTPException(status_code=502, detail=str(e)) from e
    await session.start()
    return {
        "ok": True,
        "stream_id": session.stream_id,
        "session_ttl_sec": _LIVE_SESSION_TTL_SEC,
        "chunk_seconds": 1.0,
        "endpoint": _LIVE_ENDPOINT_CONFIG.as_dict(),
    }


@router.post("/live/chunk")
async def post_live_chunk(
    request: Request,
    stream_id: str = Header(..., alias="X-Asr-Stream-Id"),
    seq: int = Header(..., alias="X-Asr-Seq"),
) -> dict:
    """提交一块 PCM（**裸 s16le**，无 WAV 头）。

    响应::

        {"ok": true, "seq": 12, "round": 7, "committed": "...",
         "unfixed": "...", "language": "English", "lag_ms": 143,
         "buffered_chunks": 0, "dropped_chunks": 0}

    ``round`` 为 0 表示这一块**没凑满 1 秒**（只缓冲，尚未推理）。
    """
    _require_live_enabled()
    mgr = get_live_asr_service()
    session = mgr.get(stream_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    pcm = await request.body()
    if not pcm:
        raise HTTPException(status_code=400, detail="音频块为空")
    try:
        result = await session.submit(pcm, seq)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if result.get("error"):
        # 熔断 / 会话出错 —— 409（冲突），不是 500。
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.post("/live/finish")
async def post_live_finish(
    stream_id: str = Header(..., alias="X-Asr-Stream-Id"),
) -> dict:
    """收尾：把尾巴喂给 SDK → 返回最终文本。"""
    _require_live_enabled()
    mgr = get_live_asr_service()
    session = mgr.get(stream_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    try:
        result = await session.finish()
    finally:
        mgr.drop(stream_id)
    return result


@router.delete("/live/stop")
async def delete_live_stop(
    stream_id: Optional[str] = Header(None, alias="X-Asr-Stream-Id"),
) -> dict:
    """立即中止。

    **不带** ``X-Asr-Stream-Id`` → 停掉**全部**实时会话（DemoGuard 的
    离开页面兜底走的就是这条路径 —— 它不知道会话 id）。
    """
    _require_live_enabled()
    mgr = get_live_asr_service()
    dropped = 0
    if stream_id:
        session = mgr.get(stream_id)
        if session is not None:
            result = await session.stop()
            dropped = int(result.get("dropped_chunks") or 0)
            mgr.drop(stream_id)
        return {"ok": True, "dropped_chunks": dropped}
    for s in mgr.sessions():
        try:
            result = await s.stop()
            dropped += int(result.get("dropped_chunks") or 0)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("live stop failed: %s", e)
        mgr.drop(s.stream_id)
    return {"ok": True, "dropped_chunks": dropped}


# --------------------------------------------------------------------------- #
# 板载麦克风（设备端 ALSA 采集）
# --------------------------------------------------------------------------- #
@router.get("/live/board/devices")
async def get_board_devices() -> dict:
    """本机可用的 ALSA 采集设备（UI 下拉用）。"""
    ctl = get_board_mic()
    return {"ok": True, "devices": ctl.devices(),
            "default_device": DEFAULT_DEVICE}


@router.post("/live/board/start")
async def post_board_start(device: Optional[str] = Body(None,
                                                        embed=True)) -> dict:
    """起板载麦克风采集（独立进程）。

    请求体 ``{"device": "hw:1,0"}``（省略则用默认设备）。

    ⚠️ 采集进程自己开实时会话、自己推流 —— 服务端不碰声卡。
    """
    _require_live_enabled()
    ctl = get_board_mic()
    try:
        st = ctl.start(device or "")
    except BoardMicError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, **st}


@router.post("/live/board/stop")
async def post_board_stop() -> dict:
    """停板载麦克风采集（SIGTERM → agent 优雅收尾）。"""
    ctl = get_board_mic()
    # ⚠️⚠️ **不能**直接调 ``ctl.stop()``。
    #
    # ``ctl.stop()`` 是**同步阻塞**的（等 agent 退出，最多 8 s）——
    # 写在 ``async def`` 端点里等于**把事件循环堵死**。而 agent 收到
    # SIGTERM 后正要 POST ``/live/finish`` 收尾，那个请求正好排在被
    # 堵死的事件循环后面 → **互相等**：
    #
    #   webui: ctl.stop() 等 agent 退出 ─┐
    #   agent: /live/finish 等 webui 处理 ─┘  → agent 被 SIGKILL，
    #   ``/live/finish`` 永远丢，live 会话残留到 600 s TTL。
    #
    # 实测症状：stop 端点耗时 5055 ms（= grace 秒数）、agent 日志停在
    # 「收到信号 15，准备收尾」、webui 日志在这 5 秒里**一条请求都没有**。
    #
    # 所以 ``stop`` 丢到线程里跑 —— 事件循环保持响应，``/live/finish``
    # 才处理得掉。
    result = await asyncio.to_thread(ctl.stop)
    # 安全网：agent 万一被 SIGKILL（没走完 finish）→ 把板载会话收掉，
    # 别让它占着 SDK 会话直到 600 s TTL。
    closed = await _close_board_live_sessions()
    return {**result, "closed_sessions": closed}


async def _close_board_live_sessions() -> int:
    """收掉所有**板载**来源的实时会话；返回收掉几个。

    只收 ``source == "board"`` 的 —— 浏览器采集的会话归浏览器自己管
    （``/live/stop``），不该被板载麦克风的停止顺手带走。
    """
    mgr = get_live_asr_service()
    closed = 0
    for s in mgr.sessions():
        if s.source != "board":
            continue
        try:
            await s.stop()
        except Exception as e:  # noqa: BLE001
            _LOG.debug("board live session stop failed: %s", e)
        mgr.drop(s.stream_id)
        closed += 1
    if closed:
        _LOG.info("board mic stop: 收掉 %s 个板载实时会话", closed)
    return closed


@router.get("/live/board/status")
async def get_board_status() -> dict:
    """板载麦克风采集进程的状态。"""
    ctl = get_board_mic()
    return {"ok": True, **ctl.status()}


# --------------------------------------------------------------------------- #
# 实时翻译（流式同传，实时路径 + 文件 / 视频路径）
# --------------------------------------------------------------------------- #
#
# ⚠️ **端点是同步 ``def``，不是 ``async def``** —— 起/停翻译模型是**阻塞**
# 操作：启用要等模型加载完（约 30 s），禁用要等 unload + shutdown +
# SIGTERM 宽限（约 3~5 s）。
#
# FastAPI 只把 ``async def`` 端点跑在事件循环里，``def`` 端点丢线程池 ——
# 写成 ``async def`` 的话，这 30 秒里**整个 Web UI 都没响应**。
#
class TranslateSettingsPayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 开关；不传 = 不改
    enabled: Optional[bool] = None
    #: 目标语言（provider 的语言 key，如 ``en``）；不传 = 不改
    target_lang: Optional[str] = None


class TranslateEnablePayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 卡位；不传 = 自动挑一张放得下的卡
    npu_index: Optional[int] = Field(default=None, ge=0, le=15)


class TranslateLinePayload(BaseModel):
    """翻**一整行字幕**的请求。

    ⚠️ **为什么要有这个端点**：字幕的**显示单位是「行」**，不是「句」——
    字幕逐行推进，译文必须**逐行**跟着（YouTube 的 cue 对 cue）。
    句子级翻译做不到：一句跨 3 行时，译文只挂得上最后那行 ⇒ 中间两行
    光秃秃，而且译文永远慢半拍（实测：字幕行 1.5 s 一行，译文 3~9 s 一句）。

    ⇒ 断句在**前端**（只有前端知道行是怎么排的）⇒ 翻译单位也得由前端
    驱动 ⇒ 这个端点就是前端来要「这一行的译文」。
    """

    model_config = {"extra": "ignore"}
    #: 这一行的原文（**整行**，不是碎片）
    text: str = Field(min_length=1, max_length=600)
    #: 这一行在音频里的时间范围（秒，音轨相对时间）
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    #: 源语（ASR 报的语言名，如 ``English``）
    lang: Optional[str] = None
    #: 前端给这一行编的序号 —— 回包原样带回，前端据此对回是哪一行
    seq: Optional[int] = None


@router.get("/translate/status")
def get_translate_status() -> dict:
    """译文状态：开关 / 目标语言 / 模型就绪情况 / 统计。"""
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        get_translation_service,
    )
    return get_translation_service().status()


@router.get("/translate/languages")
def get_translate_languages() -> dict:
    """可选的目标语言（HY-MT1.5 的词表）。

    ⚠️ ``zh-TW`` **不在列表里** —— 模型词表里简繁**共用一个码**，它根本
    分不出来（真机实测：转出来是「半繁体半简体」，比不转更糟）。
    """
    from app.core.translate.npu_provider import LANG_CODES, LANG_NAMES
    items = []
    for key in LANG_CODES:
        if key == "zh-TW":
            continue
        items.append({"id": key, "name": LANG_NAMES.get(key, key)})
    return {"items": items}


@router.post("/translate/settings")
def post_translate_settings(payload: TranslateSettingsPayload) -> dict:
    """改开关 / 目标语言。

    关掉开关会**丢掉排队待译的句子**（别让它们在后台接着烧 NPU）。
    """
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        get_translation_service,
    )
    svc = get_translation_service()
    svc.configure(enabled=payload.enabled, target_lang=payload.target_lang)
    return svc.status()


@router.post("/translate/line")
async def post_translate_line(payload: TranslateLinePayload) -> dict:
    """翻**一整行字幕**（视频来源的双语字幕用）。

    ⚠️ 译文**不在这条响应里** —— 它走**同一条 WS**（``translation`` 帧），
    和句子级翻译一模一样 ⇒ 前端只有一条通道要处理。

    ⚠️ 源语没给 ⇒ 用 ASR 当前的语种（跟句子级翻译一个口径）。
    """
    import asyncio

    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        get_translation_service,
    )

    lang = (payload.lang or "").strip()
    if not lang:
        from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
        lang = str(get_asr_service().snapshot().get("language") or "").strip()
    if not lang:
        raise HTTPException(status_code=400, detail="不知道源语，没法翻")

    seq = int(payload.seq) if payload.seq is not None else 0
    svc = get_translation_service()
    if not svc.status().get("enabled"):
        # ⚠️⚠️ **把服务端的权威开关值一起回给前端**（2026-11-26）——
        #
        # 拒收本身前端能看见，但它**不知道该拿哪个值当权威** ⇒ 它会一直
        # 以为「实时翻译是开着的」⇒ 每 5 秒重试一次、永远不停（实测
        # ~67 次/分 × 6 个标签页）。
        #
        # 典型的触发场景：**服务重启** ⇒ 服务端开关复位成关，而页面里的
        # 勾还是开的（页面没刷新）⇒ 前端与服务端**不一致**。
        #
        # ⇒ 明确回传 ``enabled: False`` ⇒ 前端把本地开关拉回来、立刻停手。
        return {
            "ok": False,
            "skipped": "翻译没开",
            "enabled": False,
        }
    # ⚠️ **批次号 = 当前识别请求号** —— 行级翻译（视频来源）也是
    # 「这次识别的一部分」⇒ 配音导出**复用**它 ⇒ 不用再识别一遍。
    _rid = 0
    try:
        from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
        _rid = int(get_asr_service().current_request_id() or 0)
    except Exception:  # noqa: BLE001 — 取不到就当没有批次
        _rid = 0
    svc.submit(
        index=_TR_LINE_INDEX_BASE + seq,
        text=payload.text,
        lang=lang,
        stream_id="line",
        loop=asyncio.get_running_loop(),
        batch_id=str(_rid or ""),
        start_sec=payload.start_sec,
        end_sec=payload.end_sec,
        seq=seq,
    )
    # ⚠️ **把队列深度回给前端** —— 前端拿它做**背压**（队列太深就先别
    #    再塞 ⇒ 服务端的队列永远碰不到 ``QUEUE_MAX`` ⇒ **不会丢最旧的**）。
    #
    # ⚠️ 服务端队列满了会**丢最旧的**（``translation.py::submit``）⇒ 丢的
    #    正是**先要的那几行**（= 播放头附近）⇒ 画面字幕上**永远只有中文**
    #    （用户报）。
    return {"ok": True, "seq": seq, "queue_depth": svc.queue_depth()}


@router.post("/translate/enable")
def post_translate_enable(payload: TranslateEnablePayload) -> dict:
    """起翻译模型（**慢端点**：要等模型加载完，约 30 s）。

    ⚠️ 起的是**本机** worker —— 「每块板子只管自己的算力」。
    """
    try:
        from app.core.translate.npu_manager import (
            get_npu_translate_instances,
        )
        return get_npu_translate_instances().ensure(payload.npu_index)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/translate/disable")
def post_translate_disable() -> dict:
    """停掉本板的翻译模型（**慢端点**：unload + shutdown，约 3~5 s）。"""
    try:
        from app.core.translate.npu_manager import (
            get_npu_translate_instances,
        )
        return get_npu_translate_instances().disable()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# --------------------------------------------------------------------------- #
# WebSocket —— push 流式增量 + 状态变更
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 同声传译（2026-11-21）
# --------------------------------------------------------------------------- #
#
# 同传是 ASR 卡里的一个**模式**（与「实时翻译」并列）⇒ 切句 → 翻译 → 播报。
# 播报走**宿主桥**（``app.core.audio.tts_bridge``）⇒ 卡片之间不直接 import
# （AGENTS 铁律：卡片只能 import ``app.core.*``）。

class InterpStartPayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 切句档位："fast"（快）/ "steady"（稳）
    mode: Optional[str] = None
    #: 播放时机："now"（不等播放）/ "follow"（跟播放头）
    gate: Optional[str] = None
    #: 目标语（provider 的语言 key，如 "zh-CN"）
    target_lang: Optional[str] = None
    #: **内置音色**（SDK 的 SPK_ID，如 serena / vivian）。
    speaker: Optional[str] = None
    #: **参考音色**（voice-clone，如 news_female / baby_girl）——
    #: 本板只有这类音色 ⇒ 面板选中参考音色时走这里。
    #:
    #: ⚠️ **和 ``speaker`` 是两个不同的 SDK 参数**，混用 ⇒ 音色不生效。
    ref_speaker: Optional[str] = None
    #: 音量 0.0–1.0
    volume: Optional[float] = None
    #: 来源标签："live" / "file" —— 只影响面板提示与滞后口径
    source: Optional[str] = None
    #: **标签页 id**（2026-11-25）—— 引擎是**全局单例** ⇒ 多个标签页会
    #: **共用一个播放头槽位** ⇒ 后上报的覆盖先上报的 ⇒ 实测那一轮有 **6 个
    #: 浏览器连接**同时上报 ⇒ 播放头来回跳 ⇒ 对齐失效 ⇒ 声音不跟随字幕。
    #: ⇒ 记住「这一轮是哪个标签页发起的」⇒ 只认它的播放头。
    #:
    #: ⚠️ 空 = 不限制（老客户端 / 老前端）。
    client_id: Optional[str] = None


class InterpSettingsPayload(BaseModel):
    model_config = {"extra": "ignore"}
    mode: Optional[str] = None
    gate: Optional[str] = None
    target_lang: Optional[str] = None
    speaker: Optional[str] = None
    #: 参考音色（voice-clone）—— 见 :class:`InterpStartPayload` 的说明。
    ref_speaker: Optional[str] = None
    volume: Optional[float] = None


class InterpPlayheadPayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 哪条流（文件流是 ``file-<rid>#<epoch>``；实时流没有播放头）
    stream_id: Optional[str] = None
    #: 播放头位置（秒，音频时间轴）
    playhead_sec: float = 0.0
    #: 播放器是不是在放（暂停 ⇒ 译文也该停）
    playing: bool = True
    #: **媒体已经播完**（``<audio>.ended``）⇒ 源**不会再往前走**
    #: ⇒ 剩下的译文**直接补播**（不然会被永久压住 ⇒「只播放了第一句」）。
    ended: bool = False
    #: **媒体加载/播放失败** ⇒ 同样源不会再往前走 ⇒ 直接补播。
    failed: bool = False
    #: **标签页 id**（2026-11-25）—— 只认**发起这一轮**的标签页的播放头
    #: （引擎是全局单例 ⇒ 多标签页会互相覆盖播放头 ⇒ 对齐失效）。
    client_id: Optional[str] = None


@router.get("/interp/languages")
async def get_interp_languages() -> dict:
    """同传**能出声**的目标语言（MT 能翻 ∩ TTS 能出声 = 10 种）。

    ⚠️ 为什么单独一个端点：字幕翻译能给 36 种目标语言，但 TTS 只会说
    11 种（含 auto）⇒ 同传的下拉**只列交集**，免得用户选了一个「播不出来」
    的语言还以为功能坏了。
    """
    from app.core.translate.npu_provider import LANG_CODES, LANG_NAMES
    from wdnapps_audio_chat.cards.qwen3_asr.interp import (
        _TTS_LANG_BY_PROVIDER,
    )

    items = []
    for key in LANG_CODES:
        if key == "zh-TW":
            continue
        tts_lang = _TTS_LANG_BY_PROVIDER.get(key)
        if not tts_lang:
            continue          # TTS 说不了 ⇒ 不列
        items.append({"id": key, "name": LANG_NAMES.get(key, key),
                      "tts": tts_lang})
    return {"items": items}


@router.post("/interp/start")
async def post_interp_start(payload: Optional[InterpStartPayload] = Body(None)) -> dict:
    """起同传。

    Body: ``{mode, gate, target_lang, speaker, volume, source}``
    ⇒ ``{ok, mode, gate, source, tts, ...}``

    ⚠️ **同传只对麦克风两种来源开放**（2026-11-26 用户要求）——
    「音频文件」/「含音轨的视频」改成**配音导出**（见 ``/dub/start``）⇒
    这两类来源的同传请求一律 **400**（前端也把那块藏了）。
    """
    import asyncio

    from wdnapps_audio_chat.cards.qwen3_asr.interp import get_interp_engine

    data = payload or InterpStartPayload()
    source = (data.source or "live").strip()
    if source in ("file", "video"):
        raise HTTPException(
            status_code=400,
            detail="文件类来源不做同传，请用「配音导出」")
    engine = get_interp_engine()
    # ⚠️ 广播要切回**主事件循环**（同传引擎是工作线程 ⇒ ``asyncio.Queue``
    # 不是线程安全的）⇒ 把当前循环记给引擎。
    engine.set_loop(asyncio.get_running_loop())
    status = engine.start(
        mode=data.mode or "steady",
        gate=data.gate or "now",
        target_lang=data.target_lang or "zh-CN",
        # ⚠️ **音色 / 音量原样透传**（可能是 None）——
        # None 的意思是「这次不改」⇒ 引擎**保留**面板上已经选好的值。
        # （之前写死 ``or ""`` ⇒ start 请求没带音色字段时会把用户选的
        # 音色抹掉 ⇒ 每句话音色都不一样。）
        speaker=data.speaker,
        ref_speaker=data.ref_speaker,
        volume=data.volume,
        source=data.source or "live",
        # ⚠️ **标签页 id**（2026-11-25）—— 只认这个标签页的播放头。
        client_id=str(data.client_id or ""),
    )
    return {"ok": True, **status}


@router.post("/interp/stop")
async def post_interp_stop() -> dict:
    """停同传 + 清 TTS 队列（**正在合成的那句跑完**）。"""
    from wdnapps_audio_chat.cards.qwen3_asr.interp import get_interp_engine

    status = get_interp_engine().stop()
    return {"ok": True, **status}


@router.post("/interp/ensure-tts")
async def post_interp_ensure_tts() -> dict:
    """把 **TTS 模型**拉起来（卡位由卡位策略决定 ⇒ 发声链独占一张卡）。

    ⚠️ **为什么需要这个端点**：向导的「模型加载进度条」是**按 model_id
    等实例出现**的 ⇒ 只把 model_id 报给框架**不会有人去起它** ⇒
    进度条会一直等到超时。

    ⚠️ **为什么走宿主桥**：卡片之间不能直接 import ⇒ TTS 卡在另一个插件包里
    ⇒ 由宿主（``app.core.audio.tts_bridge``）去动 registry。
    """
    try:
        from app.core.audio.tts_bridge import start_model, tts_model_id
    except Exception as exc:  # noqa: BLE001 — 宿主桥不可用 ⇒ 软失败
        return {"ok": False, "model_id": "", "npu_index": -1,
                "detail": "TTS 桥不可用: %s" % exc}
    model_id = tts_model_id()
    if not model_id:
        return {"ok": False, "model_id": "", "npu_index": -1,
                "detail": "本机没有 TTS 模型"}
    return start_model(model_id)


@router.get("/interp/status")
async def get_interp_status() -> dict:
    """同传指标（切句 / 翻译 / 播报 / 滞后 / 队列 / 变速）。

    ⚠️ **额外带一个 ``tts_model_id``** —— 向导的「模型加载进度条」是
    **按 model_id 找实例**的（框架轮询 ``GET /api/compute``，拿
    ``instances[]`` 里 model_id 对得上、status 变成 running 的那条判为
    就绪）⇒ 卡片得先知道**要等的那个 TTS 模型**是谁。
    ⇒ 这里从**宿主桥**取（按 capability 查，不写死 id ⇒ 换模型包不失效）。
    """
    import asyncio

    from wdnapps_audio_chat.cards.qwen3_asr.interp import get_interp_engine

    engine = get_interp_engine()
    if engine.loop_missing():
        engine.set_loop(asyncio.get_running_loop())
    out = engine.status()
    try:
        from app.core.audio.tts_bridge import tts_model_id
        out["tts_model_id"] = tts_model_id()
    except Exception:  # noqa: BLE001 — 宿主桥不可用 ⇒ 当作「没有 TTS」
        out["tts_model_id"] = ""
    return out


@router.post("/interp/settings")
async def post_interp_settings(payload: Optional[InterpSettingsPayload] = Body(None)) -> dict:
    """改档位 / 播放时机 / 目标语 / 音色 / 音量（**不重启引擎**）。"""
    from wdnapps_audio_chat.cards.qwen3_asr.interp import get_interp_engine

    data = payload or InterpSettingsPayload()
    status = get_interp_engine().settings(
        mode=data.mode,
        gate=data.gate,
        target_lang=data.target_lang,
        speaker=data.speaker,
        ref_speaker=data.ref_speaker,
        volume=data.volume,
    )
    return {"ok": True, **status}


@router.post("/interp/playhead")
async def post_interp_playhead(payload: Optional[InterpPlayheadPayload] = Body(None)) -> dict:
    """**前端上报播放头**（250 ms 一次）。

    ⚠️ 为什么走 REST 而不是 WS：ASR 卡的 ``/ws`` 是**服务端 → 浏览器**的
    单向广播；播放头是**浏览器 → 服务端** ⇒ 走 POST 最省事。
    """
    from wdnapps_audio_chat.cards.qwen3_asr.interp import get_interp_engine

    data = payload or InterpPlayheadPayload()
    return get_interp_engine().on_playhead(
        str(data.stream_id or ""), float(data.playhead_sec or 0.0),
        bool(data.playing), bool(data.ended), bool(data.failed),
        str(data.client_id or ""),
    )


# --------------------------------------------------------------------------- #
# 配音导出（2026-11-26）
# --------------------------------------------------------------------------- #
#
# 「音频文件」/「含音轨的视频」两个来源**不再做同声传译** ⇒ 改成导出：
#
# * 音频文件     → 目标语言的音频文件（ASR → 翻译 → TTS）；
# * 含音轨的视频  → ① 目标语言的音频文件 ② 双字幕视频 ③ 重新封装。
#
# 文件名 = **源文件名 + 目标语言 + 音色**，存在**源文件同级目录**。

class DubStartPayload(BaseModel):
    model_config = {"extra": "ignore"}
    #: 来源类型："audio"（音频文件）/ "video"（含音轨的视频）
    kind: str = "audio"
    #: 设备端源文件路径（**绝对路径**，须在允许的根目录内）
    path: str = ""
    #: 目标语言（provider 的语言 key，如 "zh-CN"）
    target_lang: str = ""
    #: **内置音色**（SDK 的 SPK_ID）。
    speaker: Optional[str] = None
    #: **参考音色**（voice-clone）—— 本板只有这类音色 ⇒ 面板选中参考
    #: 音色时走这里。
    #:
    #: ⚠️ **和 ``speaker`` 是两个不同的 SDK 参数**，混用 ⇒ 音色不生效。
    ref_speaker: Optional[str] = None


@router.post("/dub/start")
async def post_dub_start(payload: Optional[DubStartPayload] = Body(None)) -> dict:
    """起一次配音导出。

    Body: ``{kind, path, target_lang, speaker, ref_speaker}``

    ⇒ ``{ok, job_id, state, phase, ...}``

    ⚠️ **慢操作**（ASR + 翻译 + TTS + 烧字幕）⇒ **立即返回**，进度用
    ``GET /dub/status`` 轮询。
    """
    from wdnapps_audio_chat.cards.qwen3_asr.dub import get_dub_engine

    data = payload or DubStartPayload()
    raw = (data.path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="缺少源文件路径")
    # ⚠️ **路径要过闸** —— 与 ``/media`` 同一个口径（根目录白名单 +
    # 扩展名白名单）⇒ 不能让这个端点变成「任意路径写文件」的入口。
    try:
        src = _resolve_media_path(raw)
    except HTTPException:
        raise
    kind = "video" if _source_kind(src.name) == "video" else "audio"
    if (data.kind or "").strip() in ("video", "audio"):
        kind = data.kind.strip()
    target = (data.target_lang or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="缺少目标语言")

    engine = get_dub_engine()
    try:
        out = engine.start(
            kind=kind,
            source_path=str(src),
            target_lang=target,
            speaker=(data.speaker or ""),
            ref_speaker=(data.ref_speaker or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, **out}


@router.get("/dub/status")
async def get_dub_status() -> dict:
    """配音导出的进度 / 状态 / 产出的文件。"""
    from wdnapps_audio_chat.cards.qwen3_asr.dub import get_dub_engine

    return get_dub_engine().status()


@router.post("/dub/stop")
async def post_dub_stop() -> dict:
    """停掉正在跑的配音导出。"""
    from wdnapps_audio_chat.cards.qwen3_asr.dub import get_dub_engine

    return {"ok": True, **get_dub_engine().stop()}


@router.get("/dub/files")
async def get_dub_files(path: str = Query(..., description="设备端源文件绝对路径")) -> dict:
    """列出源文件同级目录里**已经生成**的配音文件（页面下拉用）。"""
    from wdnapps_audio_chat.cards.qwen3_asr.dub import get_dub_engine

    try:
        src = _resolve_media_path(path)
    except HTTPException:
        raise
    return {"items": get_dub_engine().list_outputs(str(src))}


@router.websocket("/ws")
async def asr_ws(ws: WebSocket) -> None:
    """WS handler：snapshot hydrate + 流式增量推送。"""
    await ws.accept()
    svc = get_asr_service()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    try:
        await ws.send_json({"type": "snapshot", "snapshot": svc.snapshot()})
    except Exception:
        return

    svc.register_subscriber(queue)

    async def _relay() -> None:
        try:
            while True:
                payload = await queue.get()
                try:
                    await ws.send_json(payload)
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    relay_task = asyncio.create_task(_relay(), name="qwen3-asr-ws-relay")

    try:
        # 客户端不发任何帧；这里循环 receive 仅用于检测断开
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        svc.unregister_subscriber(queue)
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass
        # Observable end of a subscriber connection — without it a WS that
        # never gets cleaned up is completely silent.
        _LOG.info("qwen3_asr ws closed")
