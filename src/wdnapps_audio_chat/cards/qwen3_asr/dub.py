"""配音导出（2026-11-26）。

「音频文件」/「含音轨的视频」两个来源**不再做同声传译** ⇒ 改成**导出**：

======================  ==================================================
来源                    产出
======================  ==================================================
音频文件                目标语言的**音频文件**（ASR → 翻译 → TTS）
含音轨的视频            ① 目标语言的**音频文件**
                        ② **双字幕**（原文 + 译文）烧进画面的视频文件
                        ③ ①+② 重新封装成的一个音视频文件
======================  ==================================================

文件名 = **源文件名称 + 目标语言 + 音色**，存在**源文件同级目录**：

.. code-block:: text

    会议录音.wav      →  会议录音_zh-CN_news_female.wav
    宣传片.mp4        →  宣传片_zh-CN_news_female.wav
                       宣传片_zh-CN_news_female.mp4

⚠️ **为什么「导出」而不是「同传」**
----------------------------------

同传是「边识别边播」—— 它跟**播放头**绑死（跟播放 / 不等播放），
适合麦克风这种**没有文件、没有播放头**的实时场景。而文件 / 视频源
本来就有一个**确定的成品**（整段文件）⇒ 用户要的是「处理好的文件」，
不是「边播边念」⇒ 导出更贴合需求，也更可控（可以重跑、可以挑文件播）。

⚠️ **管线与顺序**
----------------

.. code-block:: text

    音频文件:  解码 ──► ASR ──► 翻译 ──► TTS ──► 拼成一条音轨 ──► 落盘
    含音轨视频: 拆音轨 ──► ASR ──► 翻译 ──► TTS ──► 拼成一条音轨 ──► 落盘
                            └──────────── 字幕（原文 + 译文 + 时间） ──┐
    烧字幕 ──► 重新封装 ◄────────────────────────────────────────────┘

**字幕的节奏**靠的是 ASR 那一层本来就有的**逐句时间戳**
（``service.py::_tr_time_at``）—— 译文与原文是**同一句**、同一段时间
⇒ 烧出来的字幕天然跟原声对齐。

⚠️ **复用现成的机器，不另起一套**
--------------------------------

* **ASR** —— 走 ASR 卡的 :meth:`_AsrManager.transcribe_stream`（**同一套**
  增量解码 / 回看 / 断句逻辑）⇒ 页面上的「实时翻译 + 字幕实时显示」
  **照常工作**（这次识别就是那一次识别）。
* **翻译** —— 走 ASR 卡的 :class:`TranslationService`（同一套队列 / 降级 /
  质量闸）。本模块只**旁听**它的结果（:meth:`TranslationService.
  set_result_listener`），**不改动**它的行为。
* **TTS** —— 走宿主桥 :func:`app.core.audio.tts_bridge.synthesize_pcm`
  （卡片之间不直接 import）。
* **视频** —— 见 :mod:`cards.qwen3_asr.video_burn`。
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOG = logging.getLogger("qwen3_asr.dub")

#: 配音导出只对**文件类**来源有意义（麦克风没有文件可导）。
DUB_KINDS = ("audio", "video")

#: 一条字幕最短显示时长（秒）—— 太短会一闪而过。
_MIN_CUE_SEC = 0.6

#: **等页面上正在跑的识别**最多等多久（秒）。
#: ⚠️ 以前是**直接掐断**它 ⇒ 页面上的「实时结果」停在半截（用户实测）⇒
#:    现在改成等它跑完 ⇒ 页面上的结果是**完整的**。
#: ⇒ 超时了只能停掉（不然导出永远进不去）⇒ 那时在 ``warnings`` 里说清楚。
_DUB_WAIT_PAGE_ASR_SEC = 1800.0

#: **等本机的 TTS 实例就绪**最多等多久（秒）。
#: ⚠️ TTS 加载实测 ~22 s ⇒ 180 s 足够从零起一个。
#: ⇒ 超时了**不放弃**（继续合成，只是可能借对端的算力）⇒ 在 ``warnings``
#:    里说清楚。
_DUB_WAIT_LOCAL_TTS_SEC = 180.0

#: 文件名里不安全的字符（换掉，别让用户拿到一个建不出来的路径）。
_UNSAFE_NAME = re.compile(r"[^\w一-鿿.\-]+")


def sanitize_name(part: str, fallback: str = "x") -> str:
    """把一段文字变成能安全进文件名的片段。"""
    s = _UNSAFE_NAME.sub("_", (part or "").strip()).strip("._")
    return s or fallback


@dataclass
class DubSentence:
    """一句（原文 + 译文 + 时间）。"""

    orig: str
    trans: str
    start_sec: float
    end_sec: float
    status: str = "done"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "orig": self.orig,
            "trans": self.trans,
            "start_sec": round(float(self.start_sec), 3),
            "end_sec": round(float(self.end_sec), 3),
            "status": self.status,
        }


@dataclass
class DubJob:
    """一次配音导出的作业。"""

    id: str
    kind: str                       # "audio" / "video"
    source_path: str
    source_name: str
    target_lang: str
    speaker: str = ""
    ref_speaker: str = ""
    state: str = "pending"          # pending/running/done/error/stopped
    phase: str = ""                 # 人话状态（「识别中」…）
    progress: float = 0.0           # 0..1
    message: str = ""
    error: str = ""
    sentences: List[DubSentence] = field(default_factory=list)
    audio_path: str = ""
    video_path: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    warnings: List[str] = field(default_factory=list)
    #: **主事件循环**（API 层是 async ⇒ 起作业时就有）。
    #:
    #: ⚠️ 译文要广播给 WS 订阅者，而订阅队列属于主循环 ⇒ 识别线程必须
    #: 知道**主循环是哪个**。⇒ 在 :meth:`DubEngine.start`（跑在主循环上）
    #: 抓下来，识别线程直接用。
    loop: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_path": self.source_path,
            "source_name": self.source_name,
            "target_lang": self.target_lang,
            "speaker": self.speaker,
            "ref_speaker": self.ref_speaker,
            "state": self.state,
            "phase": self.phase,
            "progress": round(float(self.progress), 4),
            "message": self.message,
            "error": self.error,
            "sentences": [s.as_dict() for s in self.sentences],
            "audio_path": self.audio_path,
            "video_path": self.video_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "warnings": list(self.warnings),
        }


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> Path:
    """float32 单声道 PCM ⇒ 16-bit WAV 文件。"""
    import numpy as np

    arr = np.frombuffer(pcm, dtype="<f4")
    if arr.size == 0:
        raise ValueError("没有音频可写")
    clipped = np.clip(arr, -1.0, 1.0)
    s16 = (clipped * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(s16.tobytes())
    return path


def split_audio_track(video: Path, out_wav: Path) -> Tuple[float, float]:
    """把视频里的**音轨**抽出来落成 WAV。

    返回 ``(origin_sec, duration_sec)``：

    * ``origin_sec`` —— **音轨在容器里的起点**（秒）。字幕按**音轨相对
      时间**算 ⇒ 烧进画面时要加上它才是**容器时间**（否则音轨晚于画面
      起点的文件，字幕会整体提前）。
    * ``duration_sec`` —— 音轨时长。
    """
    import av
    import numpy as np

    container = av.open(str(video), timeout=(3.0, 10.0))
    try:
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("这个视频里没有音轨")
        stream = streams[0]
        rate = int(getattr(stream, "rate", 0) or 0) or 48000
        origin = 0.0
        st = getattr(stream, "start_time", None)
        tb = getattr(stream, "time_base", None)
        if st is not None and tb is not None:
            try:
                origin = float(st * tb)
            except Exception:  # noqa: BLE001
                origin = 0.0

        resampler = av.AudioResampler(format="fltp", layout="mono")
        chunks: List[Any] = []
        for frame in container.decode(audio=0):
            resampled = resampler.resample(frame)
            items = resampled if isinstance(resampled, list) else [resampled]
            for item in items:
                if item is None:
                    continue
                chunks.append(item.to_ndarray().reshape(-1))
        try:
            tail = resampler.resample(None)
            items = tail if isinstance(tail, list) else [tail]
            for item in items:
                if item is not None:
                    chunks.append(item.to_ndarray().reshape(-1))
        except Exception:  # noqa: BLE001
            pass
        if not chunks:
            raise ValueError("音轨解出来是空的")
        pcm = np.concatenate(chunks).astype("<f4")
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(
                (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        return origin, len(pcm) / float(max(1, rate))
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass


def _media_duration_sec(path: Path) -> float:
    """媒体的**时长**（秒）—— 只读容器头，不解码。

    ⚠️ 用它做「复用的结果覆盖了整段吗」的判据（见
    :meth:`DubEngine._reuse_page_result`）。
    """
    try:
        import av
        c = av.open(str(path), timeout=(5.0, 10.0))
        try:
            dur = float(c.duration or 0.0) / 1_000_000.0
            if dur > 0:
                return dur
            # 有些容器没有 duration ⇒ 用最长那条流
            best = 0.0
            for st in c.streams:
                if st.duration and st.time_base:
                    best = max(best, float(st.duration) * float(st.time_base))
            return best
        finally:
            c.close()
    except Exception:  # noqa: BLE001 — 读不到时长就当没有（不拦复用）
        return 0.0


def _trim_silence(arr, rate: int, thresh_db: float = -45.0,
                  margin_ms: int = 25):
    """裁掉一段 TTS 音频**首尾**的静音。

    ⚠️ **TTS 模型自己会在每段前面垫一段静音**（实测 ~0.45 s）⇒
    「按原句时间戳落位」之后，听感上配音会比字幕**慢半拍** ⇒
    裁掉首尾静音，首尾各留 ``margin_ms`` 的余量（别把气声/辅音削掉）。

    只裁**首尾**，中间的停顿保留（那是自然的呼吸）。
    """
    import numpy as np

    if arr is None or len(arr) == 0:
        return arr
    peak = float(np.abs(arr).max())
    if peak <= 1e-6:
        return arr
    thr = peak * (10.0 ** (thresh_db / 20.0))
    loud = np.abs(arr) >= thr
    nz = np.nonzero(loud)[0]
    if nz.size == 0:
        return arr
    margin = int(rate * margin_ms / 1000.0)
    start = max(0, int(nz[0]) - margin)
    end = min(len(arr), int(nz[-1]) + margin)
    return arr[start:end]


class DubEngine:
    """配音导出的作业管理器（单例）。

    ⚠️ **同一时刻只跑一个作业** —— ASR 是**全局单例**（一个实例只能跑
    一条流）⇒ 两个导出作业会互相踩。⇒ 新的 ``start`` 会**顶掉**旧的。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job: Optional[DubJob] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._seq = 0

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def start(self, *, kind: str, source_path: str, target_lang: str,
              speaker: str = "", ref_speaker: str = "") -> Dict[str, Any]:
        """起一次配音导出。

        ⚠️ **先停掉正在跑的识别** —— ASR 是单例，两个流会搅坏同一个增量
        解码器（``service.py`` 顶部写得很清楚）。
        """
        if kind not in DUB_KINDS:
            raise ValueError("配音导出只支持音频文件 / 含音轨的视频")
        src = Path(source_path)
        if not src.is_file():
            raise ValueError("源文件不存在: {}".format(source_path))

        # ⚠️ **主事件循环** —— API 层是 async ⇒ 这里能抓到。译文广播要靠它
        # 回到主循环（``asyncio.Queue`` 不是线程安全的）。
        try:
            import asyncio
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            main_loop = None

        with self._lock:
            self._cancel.set()
            self._job = None
            self._thread = None
            self._cancel = threading.Event()
            self._seq += 1
            job = DubJob(
                id="dub-{}".format(self._seq),
                kind=kind,
                source_path=str(src),
                source_name=src.name,
                target_lang=(target_lang or "").strip(),
                speaker=(speaker or "").strip(),
                ref_speaker=(ref_speaker or "").strip(),
                state="running",
                phase="准备中",
                started_at=time.time(),
                loop=main_loop,
            )
            self._job = job

        # ① **不要停掉页面上正在跑的识别** —— 见 :meth:`_wait_for_page_asr`。
        #    （以前这里是 ``svc.stop(reason="dub")`` ⇒ 用户自己的识别被**掐断**
        #    ⇒ 页面上的「实时结果」停在**半截** ⇒ 用户报「实时结果只显示了
        #    一部分」⇒ 现在改成**等**它跑完。）
        #
        # ⚠️ ASR 是**单例** ⇒ 导出自己那条流必须等它让出来 —— 等待放在
        #    作业线程里（`_wait_for_page_asr`），**不挡**这里（页面能立刻
        #    拿到作业号、看到「等页面上的识别跑完」这个阶段）。

        # ② 起翻译模型（**不动页面的开关和目标语言** ——
        #    见 :meth:`_ensure_translation`）
        try:
            self._ensure_translation(job)
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = "翻译模型起不来: {}".format(exc)
            job.finished_at = time.time()
            return job.as_dict()

        # ③ TTS 模型（后台加载，不挡）
        self._ensure_tts()

        self._thread = threading.Thread(
            target=self._run, args=(job,), name="qwen3-asr-dub", daemon=True)
        self._thread.start()
        return job.as_dict()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            job = self._job
        if job is None:
            return {"state": "idle", "phase": "", "progress": 0.0,
                    "message": "", "error": "", "sentences": [],
                    "audio_path": "", "video_path": "", "warnings": []}
        return job.as_dict()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            job = self._job
            self._cancel.set()
        if job is not None and job.state == "running":
            job.phase = "已取消"
            job.message = "用户取消了"
        return self.status()

    def list_outputs(self, source_path: str) -> List[Dict[str, Any]]:
        """列出源文件同级目录里**已经生成**的配音文件（供页面下拉选择）。

        ⚠️ **只认本作业生成的文件名规则**（``<源名>_<语言>_<音色>``）——
        不扫目录里的其它文件（用户目录里什么都有）。
        """
        src = Path(source_path or "")
        if not src.name:
            return []
        parent = src.parent if str(src.parent) else Path(".")
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for p in entries:
            if not p.is_file():
                continue
            if not p.name.startswith(src.stem + "_"):
                continue
            if p.suffix.lower() not in (".wav", ".mp4"):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append({
                "name": p.name,
                "path": str(p),
                "kind": "video" if p.suffix.lower() == ".mp4" else "audio",
                "size": size,
                "url": "/api/qwen3-asr/media?path=" + _urlquote(str(p)),
            })
        return out

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _ensure_translation(self, job: DubJob) -> None:
        """把翻译模型拉起来（**不动页面的开关和目标语言**）。

        ⚠️ ``ensure()`` 是**慢操作**（要等模型加载完，~30 s）⇒ 起作业前
        就做掉，别让作业跑起来才发现没翻译。

        ⚠️⚠️ **绝不能** ``configure(enabled=True, target_lang=...)`` ——
        翻译服务是**单例**，「开关」和「目标语言」都是**全局**的 ⇒ 那样会
        把页面上「实时翻译」的开关**顶开**、把「显示语言」改成导出要的那个
        （用户实测：点「生成配音版」⇒「实时翻译」自己开了、显示语言也变了）。

        ⇒ 导出那一批**自带目标语言**（``strict_target``）⇒ 它自己会翻
        ⇒ **不需要**动全局。
        """
        from app.core.translate.npu_manager import get_npu_translate_instances

        mgr = get_npu_translate_instances()
        status = mgr.status()
        if not status.get("running"):
            mgr.ensure()

    def _wait_for_page_asr(self, job: DubJob) -> None:
        """等**页面上正在跑的识别**跑完（**不打断它**）。

        ⚠️ **为什么不能直接停掉它**（2026-11-27 用户实测）：点「生成配音版」
        会把用户自己的识别**掐断** ⇒ 页面上「实时结果」就停在**半截**
        （用户报：「『实时结果』只显示了一部分」）⇒ 用户要的明明是**完整
        的全部结果**。

        ⚠️ ASR 是**单例** ⇒ 导出自己那条流**必须**等它让出来。

        ⇒ 这里**等**（有超时）⇒ 用户的识别跑完 ⇒ 导出**复用**它的结果
        ⇒ 页面上的结果完整、导出的文件也完整。

        ⚠️ **只等「文件路径」的识别**（``file_busy()``）—— 麦克风那种
        **实时**会话在**另一个服务**里（``get_live_asr_service``）⇒ 不占
        这个单例 ⇒ 不用等。
        """
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import (
                get_asr_service,
            )
            svc = get_asr_service()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("dub: 取 ASR 服务失败（best-effort）: %s", exc)
            return
        if not svc.file_busy():
            return
        self._set_phase(job, "等页面上的识别跑完", 0.02)
        LOG.info("dub: 页面上的识别还在跑 ⇒ 等它跑完（不打断它）")
        deadline = time.time() + _DUB_WAIT_PAGE_ASR_SEC
        while time.time() < deadline:
            if self._cancelled():
                raise RuntimeError("用户取消了")
            if not svc.file_busy():
                return
            time.sleep(0.5)
        # ⚠️ 超时 ⇒ 只能停掉它（否则导出永远进不去）⇒ 说清楚。
        LOG.warning("dub: 等页面上的识别超时（%.0f s）⇒ 停掉它",
                    _DUB_WAIT_PAGE_ASR_SEC)
        job.warnings.append(
            "页面上的识别等了 {:.0f} 秒还没跑完 ⇒ 已停掉它，"
            "导出自己重新识别了一遍".format(_DUB_WAIT_PAGE_ASR_SEC))
        try:
            svc.stop(reason="dub-timeout")
        except Exception as exc:  # noqa: BLE001
            LOG.debug("dub: 停旧识别失败（best-effort）: %s", exc)

    def _ensure_tts(self) -> None:
        """确保本机有一个跑着的 TTS 实例（**后台**，不挡）。

        ⇒ 复用同传引擎那套「本机优先 + 记 autostart」的逻辑（见
        ``interp.py::InterpEngine._ensure_tts_model``）—— 不想在两处
        抄一遍。
        """
        def _run() -> None:
            try:
                from wdnapps_audio_chat.cards.qwen3_asr.interp import (
                    get_interp_engine,
                )
                get_interp_engine().ensure_tts_model()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("dub: TTS 模型启动失败: %s", exc)

        threading.Thread(target=_run, name="dub-tts-ensure", daemon=True).start()

    def _wait_local_tts(self, job: DubJob) -> bool:
        """等**本板**的 TTS 实例就绪（合成之前**必须**等）。

        ⚠️ **为什么必须等**（实测 2026-11-27）：
        ``synthesize_pcm`` 走 ``get_session("swcap_text_to_speech")`` ⇒
        **本机优先，本机没有就退回 LAN peer**。而本机的 TTS 实例是
        ``_ensure_tts()`` 在**后台**拉起来的（要 ~22 s 才加载完）⇒
        导出刚开始合成时本机往往**还没就绪** ⇒ ``get_session()`` 抢先拿到
        **对端板子**的 TTS ⇒ 那个对端 worker 的 session 没加载 ⇒ 合成报
        「session not loaded; send action='load' first」（实测 2026-11-27
        04:05:14，整个作业失败）。

        ⇒ 所以：**合成之前先等本机的 TTS 跑起来**（等期间不浪费时间 ——
        识别 + 翻译已经在前面跑过了）。

        ⇒ 而且**必须用本机的**：对端的 TTS 要把文字发过网络、把 PCM 拉回来
        ⇒ 又慢又不稳（同传那边早就因此规定「语音必须本机出」）。

        Returns ``True`` = 本机有 TTS；``False`` = 超时（继续，退回
        ``get_session()`` 给的那个，可能是对端的）。
        """
        cap = "swcap_text_to_speech"
        try:
            from app.core.compute.registry import get_registry
            reg = get_registry()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("dub: 取 registry 失败（不挡合成）: %s", exc)
            return False

        deadline = time.time() + _DUB_WAIT_LOCAL_TTS_SEC
        while True:
            try:
                sessions = reg.list_sessions(cap, include_remote=False)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("dub: 查本机 TTS 实例失败: %s", exc)
                sessions = []
            if sessions:
                return True
            if time.time() >= deadline:
                LOG.warning("dub: 等本机的 TTS 超时（%.0f s）⇒ 继续"
                            "（可能要借对端板子的算力）",
                            _DUB_WAIT_LOCAL_TTS_SEC)
                job.warnings.append(
                    "本机的语音模型等了 {:.0f} 秒还没就绪 ⇒ 这次导出可能"
                    "借用了对端板子的算力（会慢一些）".format(
                        _DUB_WAIT_LOCAL_TTS_SEC))
                return False
            self._set_phase(job, "等语音模型就绪", 0.5)
            if self._cancelled():
                raise RuntimeError("用户取消了")
            time.sleep(1.0)

    # ------------------------------------------------------------------ #
    # 作业主体
    # ------------------------------------------------------------------ #
    def _run(self, job: DubJob) -> None:
        tmp_dir = None
        try:
            # ---------- ⓪ 等页面上正在跑的识别跑完 ----------------------- #
            # ⚠️ **不能掐断它**（用户实测：「实时结果只显示了一部分」）。
            self._wait_for_page_asr(job)

            tmp_dir = Path(tempfile.mkdtemp(prefix="qwen3-dub-"))
            src = Path(job.source_path)

            # ---------- ① 视频：先拆音轨 -------------------------------- #
            asr_source = src
            origin_sec = 0.0
            track_dur = 0.0
            if job.kind == "video":
                self._set_phase(job, "拆音轨", 0.02)
                audio_wav = tmp_dir / "track.wav"
                origin_sec, track_dur = split_audio_track(src, audio_wav)
                asr_source = audio_wav

            # ---------- ② ASR + 翻译 ------------------------------------ #
            # ⚠️ **优先复用页面上已经跑过的那次识别** —— 同一个源文件、
            # 从头跑到尾、还带译文 ⇒ 直接拿它的句子 ⇒ **不用再识别一遍**
            # （省一遍 NPU + 一遍等待）。
            #
            # ⚠️ **视频用「音轨时长」**（句子的时间轴是音轨相对时间），
            # **音频用「文件时长」**。
            if job.kind == "video":
                _dur = track_dur
            else:
                _dur = _media_duration_sec(src)
            sentences = self._reuse_page_result(job, src, _dur)
            if sentences:
                self._set_phase(job, "复用识别结果", 0.05)
            else:
                self._set_phase(job, "识别中", 0.05)
                sentences = self._run_asr(job, asr_source)
            if not sentences:
                raise RuntimeError(
                    "没有识别出任何能翻译的句子（翻译模型没起来？源文件有声音吗？）")
            job.sentences = sentences

            # ---------- ③ TTS ------------------------------------------- #
            # ⚠️ **先等本机的 TTS 就绪** —— 不等的话 ``get_session()`` 会
            #    抢先拿到**对端板子**的 TTS ⇒ 那个对端 worker 的 session
            #    没加载 ⇒ 「session not loaded」（实测 2026-11-27）。
            self._wait_local_tts(job)
            self._set_phase(job, "合成语音中", 0.55)
            # ⚠️ **视频**要按原句时间戳落位（音画/字幕同一时基）；
            # **纯音频**连续拼接即可。
            pcm, sr, placements = self._synthesize(
                job, sentences, align=(job.kind == "video"),
                origin_sec=origin_sec)

            # ---------- ④ 落盘（音频） ----------------------------------- #
            self._set_phase(job, "保存音频文件", 0.85)
            out_audio = self._output_path(job, ".wav")
            write_wav(out_audio, pcm, sr)
            job.audio_path = str(out_audio)

            # ---------- ⑤ 视频：烧双字幕 + 重新封装 ---------------------- #
            if job.kind == "video":
                self._set_phase(job, "烧字幕并重新封装", 0.88)
                self._render_video(job, src, out_audio, pcm, sr, sentences,
                                   placements, origin_sec)
            self._set_phase(job, "完成", 1.0)
            job.state = "done"
            job.message = "完成"
        except Exception as exc:  # noqa: BLE001
            LOG.exception("dub: 作业失败")
            job.state = "error"
            job.error = "{}: {}".format(type(exc).__name__, exc)
            job.message = job.error
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            job.finished_at = time.time()

    def _set_phase(self, job: DubJob, phase: str, progress: float) -> None:
        job.phase = phase
        job.progress = float(max(0.0, min(1.0, progress)))
        job.message = phase

    def _output_path(self, job: DubJob, suffix: str) -> Path:
        """``<源文件名>_<目标语言>_<音色><后缀>``（在源文件同级目录）。"""
        src = Path(job.source_path)
        voice = job.ref_speaker or job.speaker or "default"
        name = "{}_{}_{}{}".format(
            sanitize_name(src.stem, "media"),
            sanitize_name(job.target_lang or "und", "und"),
            sanitize_name(voice, "default"),
            suffix,
        )
        return src.parent / name

    # ------------------------------------------------------------------ #
    # ASR + 翻译
    # ------------------------------------------------------------------ #
    def _run_asr(self, job: DubJob, source: Path) -> List[DubSentence]:
        """跑一遍 ASR + 翻译，把「原文 + 译文 + 时间」收回来。

        ⚠️ **旁听翻译服务的结果**（``set_result_listener``）—— 那是**唯一**
        一份「原文 + 译文 + 时间」都齐的数据（句级翻译的载荷里三项都有）。

        ⚠️ **不改动翻译服务的行为** —— 监听器只是**多一个观察者**。
        """
        from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
        from wdnapps_audio_chat.cards.qwen3_asr.translation import (
            get_translation_service,
        )
        from app.core.audio.decode import iter_media_pcm

        svc = get_asr_service()
        tr = get_translation_service()
        collected: Dict[int, DubSentence] = {}
        stream_id = ""

        def _on_result(payload: Dict[str, Any]) -> None:
            sid = str(payload.get("stream_id") or "")
            if stream_id and sid and sid != stream_id:
                return          # 别的流（实时麦克风 / 行级）的译文，不管
            idx = int(payload.get("index") or 0)
            text = str(payload.get("text") or "")
            src_text = str(payload.get("source") or "")
            start = payload.get("start_sec")
            end = payload.get("end_sec")
            collected[idx] = DubSentence(
                orig=src_text,
                trans=text,
                start_sec=float(start) if start is not None else -1.0,
                end_sec=float(end) if end is not None else -1.0,
                status=str(payload.get("status") or "done"),
            )

        tr.set_result_listener(_on_result)
        try:
            def _factory():
                return iter_media_pcm(str(source),
                                      filename=source.name,
                                      chunk_seconds=1.0)

            rid = svc.transcribe_stream(
                _factory,
                options={},
                append=False,
                start_sec=0.0,
                lookback_sec=0.0,
                # ⚠️⚠️ **source_info 必须是**原始**源文件，不能是临时音轨**
                # （视频作业的 ``source`` 是 ``/tmp/qwen3-dub-*/track.wav``）——
                # 它会变成 ASR 会话的「当前来源」⇒ 前端恢复会话时把来源框
                # 改成那个临时路径 ⇒ 「列产物」请求越出资源根 ⇒ 400，
                # 而且作业结束临时目录被删 ⇒ 路径变成死链。
                source_info={"kind": job.kind,
                             "name": Path(job.source_path).name,
                             "path": job.source_path},
                # ⚠️ **句级翻译** —— 字幕要按「句」来对时间（行级是前端
                # 驱动的，导出这条链路不经过前端）。
                tr_mode="sentence",
                # ⚠️ **这一批翻成导出选的语言** —— 翻译服务的目标语言是
                # 全局的（页面改下拉会带偏）⇒ 这里显式钉死。
                target_lang=job.target_lang,
                # ⚠️ **主事件循环** —— 译文要广播给 WS 订阅者，而订阅队列
                # 属于主循环 ⇒ 必须把主循环传下去（在 ``start()`` 里抓的）。
                loop=job.loop,
            )
            stream_id = "file-{}".format(rid)

            # 等 ASR 跑完 + 译文落齐
            deadline = time.time() + 3600.0
            last_count = -1
            quiet_since = 0.0
            while time.time() < deadline:
                if self._cancelled():
                    raise RuntimeError("用户取消了")
                snap = svc.snapshot()
                # ⚠️ **不能用 ``snap["busy"]``** —— 那个字段会被**实时（麦克风）
                # 会话**盖掉 ⇒ 浏览器开着麦克风时这里会永远等下去。
                # ⇒ 用 ``file_busy()``（只认文件路径的口径）。
                busy = svc.file_busy()
                depth = tr.queue_depth()
                count = len(collected)
                if count != last_count:
                    last_count = count
                    quiet_since = time.time()
                    # 进度：识别阶段按轮次估，翻译阶段按收到的句子数估
                    rounds = int(snap.get("round_count") or 0)
                    if busy and rounds:
                        self._set_phase(job, "识别中",
                                        min(0.5, 0.05 + rounds / 400.0))
                if not busy and depth == 0:
                    if quiet_since and (time.time() - quiet_since) > 2.0:
                        break
                    if not quiet_since:
                        quiet_since = time.time()
                else:
                    quiet_since = 0.0
                time.sleep(0.25)
        finally:
            # ⚠️ **一定要摘掉监听器** —— 它挂在**全局单例**（翻译服务）上
            # ⇒ 留着的话，后面的译文也会往这个（已经结束的）作业里塞。
            tr.clear_result_listener()
        out: List[DubSentence] = []
        for idx in sorted(collected):
            s = collected[idx]
            if not s.trans:
                continue
            if s.start_sec < 0:
                continue
            if s.end_sec <= s.start_sec:
                s.end_sec = s.start_sec + _MIN_CUE_SEC
            out.append(s)
        return out

    # ------------------------------------------------------------------ #
    # 复用页面上已经跑过的那次识别
    # ------------------------------------------------------------------ #
    def _reuse_page_result(self, job: DubJob, src: Path,
                           duration_sec: float = 0.0) -> List[DubSentence]:
        """复用页面上**已经跑完**的那次识别 ⇒ **不用再识别一遍**。

        ⚠️ **什么时候能复用**：

        * ASR 服务记下了「这个源文件从头跑到尾了」（``completed_file_result``）
          —— 拖过进度条的那一遍**不算**（只覆盖了一部分）；
        * 这一批的**译文已经落地**（翻译队列排空）。

        ⚠️ **复用不了就返回空** ⇒ 调用方退回「自己再识别一遍」。

        ⚠️ **句子的时间轴是「音轨相对」** —— 与自己识别一遍得到的**完全
        同口径**（页面那次识别的输入也是同一条音轨）⇒ 字幕/配音的落位
        不用改。
        """
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import (
                get_asr_service,
            )
            from wdnapps_audio_chat.cards.qwen3_asr.translation import (
                get_translation_service,
            )
            svc = get_asr_service()
            rec = svc.completed_file_result(str(src))
            if not rec:
                return []
            rid = str(rec.get("request_id") or "")
            tr = get_translation_service()

            # ⚠️ **等译文落地** —— 页面那次识别的译文可能**还在飞**
            # （翻译队列是串行的，识别跑完时译文未必全到；行级翻译更是
            # **前端字幕推进时才送来**）。
            #
            # ⚠️ **队列是空的 ⇒ 不会有译文了**（行级模式下前端根本没送来
            # 行译文）⇒ **立刻放弃复用** ⇒ 退回自己识别（别让用户干等）。
            rows: List[Dict[str, Any]] = tr.batch_results(rid)
            if not rows and tr.queue_depth() == 0:
                LOG.info("dub: 这次识别没有可用的译文 ⇒ 自己再识别一遍")
                return []
            deadline = time.time() + 60.0
            quiet_since = 0.0
            while time.time() < deadline:
                rows = tr.batch_results(rid)
                if rows and tr.queue_depth() == 0:
                    if not quiet_since:
                        quiet_since = time.time()
                    elif (time.time() - quiet_since) > 1.5:
                        break
                else:
                    quiet_since = 0.0
                if self._cancelled():
                    raise RuntimeError("用户取消了")
                time.sleep(0.25)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — 复用是 best-effort
            LOG.debug("dub: 复用识别结果失败（退回自己识别）: %s", exc)
            return []

        # ⚠️⚠️ **目标语言必须一致** —— 页面那次识别的译文是按**页面上的**
        # 「实时翻译目标语言」翻的；导出要的是**导出下拉里选的语言**。
        # ⇒ 两者不同 ⇒ **绝不能复用**（否则文件名写着 ``_en_``、内容却是
        # 中文 —— 用户实测：「配音导出选择英文，生成的文件播放是中文」）。
        want_lang = str(job.target_lang or "").strip().lower()
        got_langs = {str((r or {}).get("target_lang") or "").strip().lower()
                     for r in rows}
        got_langs.discard("")
        if want_lang and (got_langs != {want_lang}):
            LOG.info(
                "dub: 复用的译文是 %s 的，导出要 %s ⇒ 自己再识别一遍",
                "/".join(sorted(got_langs)) or "（未知）", want_lang)
            job.warnings.append(
                "页面那次识别的译文是 {} 的，与本次目标语言 {} 不同 ⇒ "
                "已重新识别并翻译".format("/".join(sorted(got_langs)) or "未知",
                                          want_lang))
            return []

        out: List[DubSentence] = []
        for r in rows:
            orig = str(r.get("orig") or "").strip()
            trans = str(r.get("trans") or "").strip()
            if not orig or not trans:
                continue
            try:
                st = float(r.get("start_sec"))
                en = float(r.get("end_sec"))
            except (TypeError, ValueError):
                continue
            if st < 0:
                continue
            if en <= st:
                en = st + _MIN_CUE_SEC
            out.append(DubSentence(orig=orig, trans=trans,
                                   start_sec=st, end_sec=en))
        if not out:
            return []
        # ⚠️⚠️ **覆盖度检查** —— 页面那次识别的句子**不一定覆盖整段**：
        #
        # * **行级翻译**（视频来源）是**前端字幕推进到哪一行才送来哪一行**
        #   ⇒ 用户拖过进度条 / 只看了一半 ⇒ **中间缺一截** ⇒ 复用会让
        #   导出的配音**缺内容**、字幕**缺行**。
        # * 句子级（音频文件）由**后端**整段送 ⇒ 覆盖是全的。
        #
        # ⇒ 判据：**最后一句的结束时间**要接近源时长（留 10% 余量）。
        # ⇒ 不满足 ⇒ **退回自己识别一遍**（宁可慢，也不能缺内容）。
        if duration_sec and duration_sec > 0:
            covered = max(s.end_sec for s in out)
            if covered < duration_sec * 0.9:
                LOG.info(
                    "dub: 复用结果只覆盖了 %.1fs / %.1fs ⇒ 自己再识别一遍",
                    covered, duration_sec)
                return []
        # ⚠️ **按时间排一遍** —— 行级翻译（视频来源）是前端**按字幕推进**
        # 逐行送来的，顺序理论上有保证，但排一次更稳。
        out.sort(key=lambda s: s.start_sec)
        job.warnings.append(
            "复用了页面上那次识别的结果（{} 句，没有重新识别）".format(len(out)))
        LOG.info("dub: 复用识别结果 %d 句（源 %s）", len(out), src.name)
        return out

    # ------------------------------------------------------------------ #
    # TTS
    # ------------------------------------------------------------------ #
    def _synthesize(self, job: DubJob,
                    sentences: Sequence[DubSentence], *,
                    align: bool, origin_sec: float
                    ) -> Tuple[bytes, int, List[Tuple[float, float, float]]]:
        """逐句合成译文 ⇒ 拼成一条音轨。

        ⚠️ **两种拼法**：

        * ``align=True``（**视频**配音）—— **按原句时间戳落位**：第 i 句的
          译文落在「原句开始说话」的那一刻 ⇒ **新音轨、画面、字幕三者
          同一时基**，画面不会被拉长/压短。
        * ``align=False``（**纯音频**导出）—— **连续拼接**（句间 120 ms
          静音）。纯音频不用跟画面对齐，连续念更像人说话。

        返回 ``(pcm, sample_rate, placements)``，``placements`` 是
        ``(原句起点, 译文起点, 译文终点)``（都是**容器时间轴**，秒）。
        """
        import numpy as np

        from app.core.audio.tts_bridge import synthesize_pcm

        tts_lang = self._tts_language(job.target_lang)
        rate = 24000
        segs: List[Tuple[float, float, float, Any]] = []
        cursor = 0.0
        for i, s in enumerate(sentences):
            if self._cancelled():
                raise RuntimeError("用户取消了")
            self._set_phase(job, "合成语音中",
                            0.55 + 0.28 * (i / float(max(1, len(sentences)))))
            text = (s.trans or s.orig).strip()
            if not text:
                continue
            pcm, sr = synthesize_pcm(
                text=text,
                language=tts_lang or "auto",
                speaker=job.speaker,
                ref_speaker=job.ref_speaker,
                volume=0.6,
            )
            rate = sr
            arr = np.frombuffer(pcm, dtype="<f4")
            # ⚠️ **裁掉 TTS 自带的首尾静音**（见 ``_trim_silence``）——
            # 否则配音会比字幕慢半拍。
            arr = _trim_silence(arr, rate)
            dur = len(arr) / float(max(1, rate))
            # 原句在**容器**里的起点（ASR 给的是音轨相对时间）
            orig_start = origin_sec + float(s.start_sec)
            if align:
                # ⚠️ **不早于上一句的结束** —— 否则两句话会叠在一起念。
                start = max(orig_start, cursor)
            else:
                start = cursor
            segs.append((orig_start, start, start + dur, arr))
            cursor = start + dur
        if not segs:
            raise RuntimeError("没有句子能合成")

        pieces: List[Any] = []
        if align:
            # 按落位时间补静音 ⇒ 音轨和画面对齐
            pos = 0.0
            for (_orig, start, end, arr) in segs:
                if start > pos:
                    pieces.append(
                        np.zeros(int(round((start - pos) * rate)),
                                 dtype="<f4"))
                pieces.append(arr)
                pos = end
        else:
            gap = np.zeros(int(rate * 0.12), dtype="<f4")
            for k, (_o, _st, _en, arr) in enumerate(segs):
                if k:
                    pieces.append(gap)
                pieces.append(arr)
        joined = np.concatenate(pieces).astype("<f4")
        placements = [(o, st, en) for (o, st, en, _a) in segs]
        return joined.tobytes(), rate, placements

    @staticmethod
    def _tts_language(target_lang: str) -> str:
        """目标语言（provider 的 key）⇒ TTS 的语言 id。"""
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.interp import (
                _TTS_LANG_BY_PROVIDER,
            )
            return _TTS_LANG_BY_PROVIDER.get(target_lang or "", "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    # 视频
    # ------------------------------------------------------------------ #
    def _render_video(self, job: DubJob, source: Path, out_audio: Path,
                      pcm: bytes, sr: int, sentences: Sequence[DubSentence],
                      placements: Sequence[Tuple[float, float, float]],
                      origin_sec: float) -> None:
        """烧双字幕 + 换音轨 + 重新封装。"""
        from wdnapps_audio_chat.cards.qwen3_asr.video_burn import (
            SubtitleCue, render_dubbed_video,
        )

        # ⚠️ **字幕起点 = 原句起点**（容器时间）—— 需求：字幕节奏要和
        # **原声 / 画面**对应 ⇒ 起点跟着原声走，不跟着译文长度走。
        # 终点取「原句结束」与「译文念完」的较大者 ⇒ 译文比原句长时字幕
        # 不会提前消失。
        cues = []
        for s, (orig_start, _a_start, a_end) in zip(sentences, placements):
            start = float(orig_start)
            end = max(origin_sec + float(s.end_sec), float(a_end))
            if end - start < _MIN_CUE_SEC:
                end = start + _MIN_CUE_SEC
            cues.append(SubtitleCue(start_sec=start, end_sec=end,
                                    orig=s.orig, trans=s.trans))
        # ⚠️ **相邻两条重叠时截短前一条**（**不**推迟后一条）——
        # 推迟会让字幕偏离原声节奏；截短只影响「上一句多显示多久」。
        for i in range(len(cues) - 1):
            nxt = cues[i + 1].start_sec
            if cues[i].end_sec > nxt:
                cues[i].end_sec = max(cues[i].start_sec + _MIN_CUE_SEC, nxt)

        out_video = self._output_path(job, ".mp4")

        def _progress(pct: float, msg: str) -> None:
            self._set_phase(job, "烧字幕并重新封装", 0.88 + 0.11 * pct)

        result = render_dubbed_video(
            source,
            out_video,
            audio_pcm=pcm,
            audio_sample_rate=sr,
            cues=cues,
            fps=25,
            progress=_progress,
            cancel=self._cancelled,
        )
        job.video_path = str(out_video)
        for w in result.warnings:
            job.warnings.append(w)
        if result.frames == 0:
            raise RuntimeError("视频没有解出任何画面（源文件可能不是 H.264）")


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


_ENGINE: Optional[DubEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_dub_engine() -> DubEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = DubEngine()
        return _ENGINE


def close() -> None:
    """关服务时停掉正在跑的作业。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            try:
                _ENGINE.stop()
            except Exception:  # noqa: BLE001
                pass
            _ENGINE = None
