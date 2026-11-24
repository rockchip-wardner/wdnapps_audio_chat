"""流式同传的**译文**（实时路径 + 文件 / 视频路径）。

这条链路挂在 ASR 卡的「一句话结束」上：

* 实时路径 —— ASR 端点检测切出一句（``endpoint`` 帧）→ 本模块把这一句
  送去翻译 → 译文沿**同一条 WS** 推回浏览器；
* 文件 / 视频路径 —— 没有 endpoint，由 ``service.py`` 的
  :class:`_SentenceSplitter` 自己断句 → 切出完整句再送来这里。

为什么挂在 ASR 卡里而不是另开一张卡
----------------------------------
实时 ASR 流是**单例**的：所有 live 会话绑的是 registry 里**同一个** ASR
worker 的 session（``service.py::select_session``）。另开一张卡要么跨卡
HTTP 调这里的端点，要么自己开一条流（两条流会搅坏同一个增量解码器）。

为什么不写进 ``service.py``
---------------------------
``service.py`` 已经 2000 行，且它只管「音频 → 文本」。翻译是**另一段**
生命周期（有队列、有线程、有自己的开关与状态），单独成文件更清楚 ——
跟 ``board_mic.py`` 是同一个理由。

⚠️ **两条路径都能用**：

* **实时路径**（麦克风 / 板载麦克风）—— 挂在 ASR 卡的「一句话结束」
  （``endpoint``）帧上：端点检测命中就是一句结束 ⇒ 直接送去翻译。
* **文件 / 视频路径** —— 它**没有** endpoint（只有 ``round`` 帧的
  committed / unfixed，收尾只有一个 ``done``）⇒ 由
  ``service.py`` 的 :class:`_SentenceSplitter` **自己按标点断句**，
  切出完整句再送来这里。

两条路径的句序号**不在同一区间**（文件路径用
``请求号 × 100000 + 句序号``）⇒ 前端「按序号覆盖同一条译文」的逻辑
不用改，两条路径的译文也不会互相顶掉。

降级语义（一律**不**让翻译把识别带崩）
------------------------------------
=====================  ==========================================
情况                   前端看到什么
=====================  ==========================================
翻译成功               译文（status=done）
模型没启用             原文 + 「翻译模型未启用」（status=unavailable）
这句翻不出来           原文 + 「未翻译」（status=failed）
源语 == 目标语         原文（status=skipped，不浪费推理）
认不出源/目标语言      原文 + 「未翻译」（status=failed）
=====================  ==========================================
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("qwen3_asr.translation")

#: 译文队列长度（有界）。满了**丢最旧**的 —— 同传场景下「丢掉 10 秒前
#: 的译文」比「延迟 10 秒」可接受。
#:
#: ⚠️ **2026-11-20 从 32 放大到 256** —— 「丢最旧」这个策略在**批量补翻**
#: 场景下是致命的：用户「取消勾选 → 再勾选实时翻译」时，前端会把**整条时间
#: 轴**（一集 98 分钟的访谈 ≈ 900 行）的行**一次性**重新要一遍 ⇒ 队列瞬间爆
#: ⇒ **丢最旧的** ⇒ 丢掉的正是**播放头附近的那些行** ⇒ 画面字幕上一直**没
#: 有译文**（用户报：「再勾选实时翻译，字幕上没有实时翻译」）。
#:
#: ⇒ 前端已经改成「**播放头优先 + 限速**」（见 ``static/app.js`` 的
#:    ``TR_REQ_BATCH`` / ``TR_REQ_TICK_MS``）⇒ 正常不会再爆。
#: ⇒ 这里放大只是**余量**：万一有一波突发，也不至于把「最该翻的那几行」丢掉
#:    （队列里的东西会按提交的先后顺序翻译 ⇒ 队头 = 屏幕上那几行）。
QUEUE_MAX = 256

#: 翻译线程数。
#:
#: ⚠️ **并发单位是 MT 实例，不是线程** —— 一个 ``LLMRPCSession`` 的 RPC
#: 执行器是 ``ThreadPoolExecutor(max_workers=1)`` ⇒ 同一个 session 同时
#: 只能一个推理在飞。这里起 2 个线程是为了**两份实例**能并行；只有一份
#: 实例时它们会自动排队（不会出错，只是并发度是 1）。
WORKERS = 2

#: 单句翻译的超时（秒）。超时按「翻不出来」处理，不阻塞整条队列。
SENTENCE_TIMEOUT_S = 20.0


class _Item:
    """队列里的一项：一句待译的话。"""

    __slots__ = ("index", "text", "lang", "stream_id", "loop", "enqueued_at",
                 "gen", "start_sec", "end_sec", "seq")

    def __init__(self, index: int, text: str, lang: str, stream_id: str,
                 loop: Any, enqueued_at: float, gen: int = 0,
                 start_sec: Optional[float] = None,
                 end_sec: Optional[float] = None,
                 seq: Optional[int] = None):
        self.index = index
        self.text = text
        self.lang = lang
        self.stream_id = stream_id
        self.loop = loop
        self.enqueued_at = enqueued_at
        #: 入队时的**代数**（见 :attr:`TranslationService._gen`）。
        self.gen = gen
        #: 这句话的**音频时间范围**（秒，音轨相对时间）。
        #:
        #: ⚠️ 实时路径（麦克风）没有这个 —— 那边是端点切句，字幕不按时间
        #: 排。文件 / 视频路径有 ⇒ 前端靠它把译文挂到**字幕行**上（画面
        #: 字幕显示双语）。
        self.start_sec = start_sec
        self.end_sec = end_sec
        #: **行级**翻译时前端给这一行编的序号（原样带回，前端据此对回
        #: 是哪一行）。句子级翻译没有这个（``None``）。
        self.seq = seq


class TranslationService:
    """把 ASR 的「一句话完成」变成「一句译文」。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: 总开关（页面上那个「实时翻译」勾选）。
        self._enabled = False
        #: 目标语言（provider 的语言 key，如 ``en``）。
        self._target_lang = "en"
        self._queue: "queue.Queue[_Item]" = queue.Queue(maxsize=QUEUE_MAX)
        self._workers: List[threading.Thread] = []
        self._stopping = threading.Event()
        #: 统计（给界面看）
        self._submitted = 0
        self._translated = 0
        self._failed = 0
        self._skipped = 0
        self._dropped = 0
        self._latency_ms = 0.0
        #: **代数** —— 「清空」时 +1。
        #:
        #: 队列清空只能丢掉**还没开始**的句子；**已经在跑**的那一句
        #: （正在推理）清不掉 ⇒ 它的译文会在「清空」之后才推回来 ⇒ 页面上
        #: 又冒出一行（用户明明刚清空）。
        #:
        #: ⇒ 每句话记下入队时的代数；处理前对不上就**丢掉**。
        self._gen = 0

    # ------------------------------------------------------------------ #
    # 设置 / 状态
    # ------------------------------------------------------------------ #
    def settings(self) -> Dict[str, Any]:
        with self._lock:
            return {"enabled": self._enabled, "target_lang": self._target_lang}

    def configure(self, *, enabled: Optional[bool] = None,
                  target_lang: Optional[str] = None) -> Dict[str, Any]:
        """改设置（页面上那个开关 + 目标语言下拉）。"""
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
                if not self._enabled:
                    # 关掉 ⇒ 把排队中的句子丢掉（别让它们在后台接着烧 NPU）
                    self._drain()
            if target_lang is not None:
                tgt = (target_lang or "").strip()
                if tgt:
                    self._target_lang = tgt
            return self.settings()

    def status(self) -> Dict[str, Any]:
        """界面用的状态快照。"""
        with self._lock:
            base = {
                "enabled": self._enabled,
                "target_lang": self._target_lang,
                "queue_depth": self._queue.qsize(),
                "submitted": self._submitted,
                "translated": self._translated,
                "failed": self._failed,
                "skipped": self._skipped,
                "dropped": self._dropped,
                "avg_latency_ms": (round(self._latency_ms / self._translated, 1)
                                   if self._translated else 0.0),
            }
        # 模型状态走宿主模块（**不**在这里碰 registry —— 查询不该拉起它）
        try:
            from app.core.translate.npu_manager import (
                get_npu_translate_instances,
            )
            base["model"] = get_npu_translate_instances().status()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("translation: 读模型状态失败: %s", exc)
            base["model"] = {}
        return base

    # ------------------------------------------------------------------ #
    # 生产（ASR 的 endpoint 帧调它）
    # ------------------------------------------------------------------ #
    def queue_depth(self) -> int:
        """当前**排队待译**的句数。

        ⚠️ **轻量** —— 前端拿它做**背压**（队列太深就先别再塞）。
        别用 :meth:`status`：那个会去问一遍 NPU 实例的详细状态，重。

        ⚠️ 这是**入队之后**的深度 ⇒ 前端据此判断「我是不是塞太多了」。
        """
        try:
            return int(self._queue.qsize())
        except Exception:  # noqa: BLE001
            return 0

    def submit(self, *, index: int, text: str, lang: str, stream_id: str,
               loop: Any, start_sec: Optional[float] = None,
               end_sec: Optional[float] = None,
               seq: Optional[int] = None) -> None:
        """送来一句待译的话。

        ``start_sec`` / ``end_sec`` —— 这句话的**音频时间范围**（可选）。
        文件 / 视频路径会给 ⇒ 前端靠它把译文挂到对应的**字幕行**上
        （画面字幕显示双语，译文跟着字幕的节奏走）。

        ``seq`` —— **行级**翻译时前端给这一行编的序号（原样带回）。
        句子级翻译不传（``None``）。

        ⚠️ **绝不抛异常** —— 翻译出问题不许影响识别。
        """
        text = (text or "").strip()
        with self._lock:
            if not self._enabled or not text:
                return
            self._submitted += 1
            gen = self._gen
        item = _Item(index, text, lang, stream_id, loop, time.monotonic(),
                     gen, start_sec, end_sec, seq)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # 丢**最旧**的（同传场景下保实时性优先）
            try:
                old = self._queue.get_nowait()
                with self._lock:
                    self._dropped += 1
                LOG.warning("translation: 队列满，丢掉第 %s 句", old.index)
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                with self._lock:
                    self._dropped += 1
        self._ensure_workers()

    # ------------------------------------------------------------------ #
    # 消费
    # ------------------------------------------------------------------ #
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers and any(w.is_alive() for w in self._workers):
                return
            self._workers = []
        for i in range(WORKERS):
            th = threading.Thread(target=self._loop, name=f"asr-translate-{i}",
                                  daemon=True)
            th.start()
            with self._lock:
                self._workers.append(th)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(item)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("translation: 处理第 %s 句失败: %s", item.index, exc)
            finally:
                self._queue.task_done()

    def _handle(self, item: _Item) -> None:
        # ⚠️ **「清空」之后到的旧句子 ⇒ 丢掉**（见 :attr:`_gen`）。
        # 不丢的话，用户刚点完「清空」，页面上又冒出一行译文。
        with self._lock:
            if item.gen != self._gen:
                self._dropped += 1
                return
        t0 = time.monotonic()
        target = self.settings()["target_lang"]
        status = "failed"
        text = ""
        reason = ""

        # 源语 == 目标语 ⇒ 透传（不浪费一次推理）
        try:
            from app.core.translate.npu_provider import normalize_language
            src = normalize_language(item.lang)
            tgt = normalize_language(target)
        except Exception:  # noqa: BLE001
            src, tgt = item.lang, target

        if src and tgt and src == tgt:
            status = "skipped"
            text = item.text
            reason = "源语与目标语相同"
        else:
            try:
                from app.core.translate.npu_manager import (
                    get_npu_translate_instances,
                )
                mgr = get_npu_translate_instances()
                out, _perf = mgr.translate_sentence(item.text, item.lang, target)
                text = out
                status = "done"
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if name == "UnsupportedLanguageError":
                    status = "failed"
                    reason = str(exc)
                elif name == "NpuQualityError":
                    status = "failed"
                    reason = "这句没翻出来（显示原文）"
                elif isinstance(exc, RuntimeError):
                    status = "unavailable"
                    reason = "翻译模型未启用"
                else:
                    status = "failed"
                    reason = "翻译失败: %s" % exc
                text = item.text
                LOG.info("translation: 第 %s 句 %s（%s）", item.index, status,
                         reason or exc)

        latency_ms = int((time.monotonic() - t0) * 1000)
        # ⚠️ **推理期间可能又「清空」了** ⇒ 这条译文作废：不计数、不广播
        # （不查这一下的话，页面已经清空了、统计里却多了一句「已译」）。
        with self._lock:
            if item.gen != self._gen:
                self._dropped += 1
                return
        with self._lock:
            if status == "done":
                self._translated += 1
                self._latency_ms += latency_ms
            elif status == "skipped":
                self._skipped += 1
            else:
                self._failed += 1

        self._broadcast(item, status, text, reason, latency_ms)

    # ------------------------------------------------------------------ #
    # 广播
    # ------------------------------------------------------------------ #
    def _broadcast(self, item: _Item, status: str, text: str, reason: str,
                   latency_ms: int) -> None:
        """把译文推给浏览器（**与 ASR 的帧同一条 WS**）。

        ⚠️ ``asyncio.Queue.put_nowait`` **不是线程安全的** ⇒ 必须切回
        事件循环线程再广播（与 ASR 的 delta 回调同一个道理）。

        ⚠️ 「清空」期间的作废检查在 :meth:`_handle` 里做（推理前后各一次）
        —— 那里才是决定「这条还算不算数」的地方。
        """
        payload = {
            "type": "translation",
            "stream_id": item.stream_id,
            "index": item.index,
            "source": item.text,
            "text": text,
            "status": status,
            "reason": reason,
            "latency_ms": latency_ms,
        }
        # ⚠️ **时间**（可选）—— 文件 / 视频路径给 ⇒ 前端把译文挂到对应的
        # 字幕行上；实时路径没有 ⇒ 前端只把它列在译文面板里。
        if item.start_sec is not None:
            payload["start_sec"] = item.start_sec
        if item.end_sec is not None:
            payload["end_sec"] = item.end_sec
        # ⚠️ **行级**翻译 ⇒ 把前端的行序号原样带回（前端据此对回是哪一行）
        if item.seq is not None:
            payload["seq"] = item.seq

        def _emit() -> None:
            try:
                from wdnapps_audio_chat.cards.qwen3_asr.service import (
                    get_asr_service,
                )
                get_asr_service().broadcast(payload)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("translation: 广播失败: %s", exc)

        loop = item.loop
        if loop is None:
            _emit()
            return
        try:
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(_emit)
        except RuntimeError:
            # 循环已经关了（服务在退出）—— 丢掉这一帧
            pass

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #
    def _drain(self) -> None:
        """丢掉队列里排着的句子。"""
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                return

    def drain(self) -> None:
        """丢掉队列里排着的句子（**公开**接口）。

        识别停了就别在后台接着烧 NPU —— 调用方（ASR 的 stop / 离开页面）
        调它。⚠️ **不动开关**：用户下次还要用，只是别让旧句子接着翻。
        """
        self._drain()

    def clear(self) -> None:
        """清空统计与队列（页面上的「清空」）。"""
        with self._lock:
            #: 代数 +1 ⇒ **已经在跑**的那一句的译文回来时会被丢掉
            #: （否则用户刚点完「清空」，页面上又冒出一行）。
            self._gen += 1
            self._submitted = 0
            self._translated = 0
            self._failed = 0
            self._skipped = 0
            self._dropped = 0
            self._latency_ms = 0.0
        self._drain()

    def close(self) -> None:
        """停线程（服务退出时）。"""
        self._stopping.set()
        self._drain()
        with self._lock:
            workers = list(self._workers)
            self._workers = []
        for w in workers:
            try:
                w.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


#: 进程内单例。
_SERVICE: Optional[TranslationService] = None
_SERVICE_LOCK = threading.Lock()


def get_translation_service() -> TranslationService:
    """译文服务单例（懒创建）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = TranslationService()
        return _SERVICE
