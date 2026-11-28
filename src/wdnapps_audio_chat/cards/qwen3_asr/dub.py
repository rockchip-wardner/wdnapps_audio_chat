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

文件名 = **源文件名称 + 目标语言 + 音色**，存在**工程目录的产物目录**里
（``app/uploads/qwen3_asr/dub/`` —— 宿主 ``app.core.uploads`` 给本卡
预留的可写目录）：

.. code-block:: text

    会议录音.wav      →  <工程目录>/app/uploads/qwen3_asr/dub/会议录音_zh-CN_news_female.wav
    宣传片.mp4        →  <工程目录>/app/uploads/qwen3_asr/dub/宣传片_zh-CN_news_female.wav
                       <工程目录>/app/uploads/qwen3_asr/dub/宣传片_zh-CN_news_female.mp4

⚠️ **产物为什么不再落在源文件同级目录**（2026-11-28 用户要求）
--------------------------------------------------------------

上一版把产物写进**源文件的同级目录** ⇒ 三个毛病：

1. **源目录可能是只读的**（系统目录 / 挂载的只读分区）⇒ 导出直接失败；
2. **产物散落在各处** —— 源文件在哪儿，产物就在哪儿 ⇒ 用户回头找
   不到自己生成的东西；
3. **没法统一管** —— 「占满本地磁盘」的风险管不住（这正是用户要
   「删除 / 一键清除」的原因）。

⇒ 现在统一落到**本卡自己的产物目录**（工程目录内）：一处可列、可播、
可删、可一键清空 ⇒ 磁盘占用始终看得见、管得住。

⚠️ **文件名规则没变**（``<源名>_<目标语言>_<音色>``）⇒ 同样的源 + 同样
的语言 + 同样的音色 ⇒ **同一个文件名** ⇒ 重复导出是**覆盖**，不是
无限堆积（这也是「不会占满磁盘」的一半保证）。

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

import json
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

# --------------------------------------------------------------------------- #
# 产物目录（2026-11-28 用户要求：产物落到**工程目录**里，不落源文件旁边）
# --------------------------------------------------------------------------- #

#: 本卡在宿主 ``app/uploads/`` 下的子目录名（**与 manifest 的
#: ``uploads_dir`` 一致** —— ``uploads/qwen3_asr``）。
#:
#: ⚠️ 用**下划线**不是连字符 —— 卡 id 是 ``qwen3-asr``，但 manifest 里
#:    声明的目录名是 ``uploads/qwen3_asr`` ⇒ 两边必须一致。
_DUB_CARD_ID = "qwen3_asr"

#: 产物目录里那个**清单**文件的名字（不是产物本身，别把它当产物列出去）。
_DUB_INDEX_NAME = "index.json"

#: 产物**总量**上限（字节）—— 超过就不让再导出。
#:
#: ⚠️ **为什么要上限**（用户原话：「防止生成的文件可能占满本地磁盘」）——
#:    产物是**用户点一次生成一次** ⇒ 没人盯着的话几十分钟就能堆几十 G
#:    ⇒ 光有「删除」按钮不够，得**在源头**拦住。
_DUB_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB

#: 产物允许的后缀（音频 + 视频）。
_DUB_ARTIFACT_EXTS = (".wav", ".mp4")


def dub_output_root() -> Path:
    """配音导出的**产物目录**（工程目录内）。

    ⚠️ **走宿主那一份**（``app.core.uploads.get_card_uploads_root``）——
       卡片**不自己拼路径**（宿主那边是唯一的真值源，且带「卡 id 合法性」
       校验与「目录不存在就建」的逻辑）。

    ⇒ 实际路径：``<宿主工程目录>/app/uploads/qwen3_asr/dub/``。

    ⚠️ ``OSError`` **不抛** —— 目录建不出来（只读 fs）时，让**第一次真的
       写文件**去报错（错误信息更贴近真正的原因）。
    """
    from app.core.uploads import get_card_uploads_root

    root = get_card_uploads_root(_DUB_CARD_ID) / "dub"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001
        LOG.warning("dub: 产物目录建不出来（%s）: %s", root, exc)
    return root


def _format_size(n: int) -> str:
    """字节数 ⇒ 人话（KB / MB / GB）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n < 1024:
        return "{} B".format(n)
    if n < 1024 * 1024:
        return "{:.1f} KB".format(n / 1024.0)
    if n < 1024 * 1024 * 1024:
        return "{:.1f} MB".format(n / (1024.0 * 1024))
    return "{:.2f} GB".format(n / (1024.0 * 1024 * 1024))


class DubQuotaError(Exception):
    """产物目录**已超上限** ⇒ 不让再导出（防占满磁盘）。

    ⚠️ **单独一个异常类型** —— API 层要把它映射成 **507**
    （「磁盘/配额不够」），跟 409（互斥）/ 400（参数错）/ 502（模型没起来）
    分开。
    """

#: ⚠️ **不再等页面上正在跑的识别**（2026-11-27 用户要求：严格互斥）——
#:    上一版是「等它跑完（上限 1800 s），超时才停掉它」⇒ 用户明确说
#:    **不要等** ⇒ 现在识别在跑时**直接 409**（见 :meth:`DubEngine.start`）。
#:    ⇒ 那个「等待上限」常量已删除。

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


class DubBusyError(Exception):
    """配音导出**起不来**：页面上**有识别在跑**（严格互斥）。

    ⚠️ **单独一个异常类型**（2026-11-27）—— 不要继承 ``ValueError`` /
    ``RuntimeError``：API 层要把它单独映射成 **409**（跟「参数错了」
    的 400、「模型没起来」的 502 分开）。

    ⚠️⚠️ **别把这个类插在 ``@dataclass`` 和 ``class DubSentence`` 之间**
    （踩到过一次）—— 装饰器会**贴到上一个类**上 ⇒ ``DubBusyError`` 变成
    dataclass（``__init__() takes 1 positional argument``）⇒ 409 变成 500；
    而 ``DubSentence`` **没了装饰器** ⇒ ``DubSentence() takes no arguments``
    ⇒ 导出作业全挂。
    """


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
    #: **源语言**（识别出来的那个语种，如 "English"）—— 结果面板要标
    #: 「源语言 → 导出语言」⇒ 没有它就只标得出导出语言那一头。
    source_lang: str = ""
    #: **源媒体时长**（秒）—— 页面那条完成信息里的「音频 15.1 s」。
    #:
    #: ⚠️ **音频 = 文件时长，视频 = 音轨时长**（见 :meth:`_run` 里的
    #:    ``_dur``）⇒ 与识别那条「音频 15.1 s」是同一个口径。
    duration_sec: float = 0.0
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
            "source_lang": self.source_lang,
            "duration_sec": round(float(self.duration_sec or 0.0), 2),
            "sentences": [s.as_dict() for s in self.sentences],
            "audio_path": self.audio_path,
            "video_path": self.video_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "warnings": list(self.warnings),
        }


class DubArtifactStore:
    """配音导出**产物的清单**（落盘 JSON ⇒ **重启不消失**）。

    ⚠️ **为什么要有清单**（2026-11-28 用户要求：「做成可以下拉的列表，
       重启不消失」）—— 上一版是**扫源文件同级目录**找 ``<源名>_*``：
    文件确实还在盘上，但① 清单是「扫出来的」，源文件换了就再也列不出来；
    ② 目录里**混着用户的其它文件**（扫不出哪些是产物）；③ **重启后**
    页面拉不到任何东西（列表依赖当前的源路径）。

    ⇒ 现在：**产物目录是本卡专属的**（``app/uploads/qwen3_asr/dub/``）⇒
    目录里**只有**产物 ⇒ 可以放心列、放心删、放心一键清空。

    ⚠️ **清单与磁盘互为兜底**（:meth:`list`）：
    * 清单里有、盘上没了 ⇒ **从清单里去掉**（文件被用户手工删了）；
    * 盘上有、清单里没有 ⇒ **补进清单**（老版本产物 / 清单丢了 /
      用户把文件拷进来）⇒ **不会有「看不见的产物」**。

    ⚠️ **写清单要原子**（先写临时文件再 ``replace``）—— 断电 / 进程被
       杀 ⇒ 清单不会写成半截 JSON（半截 ⇒ 读不出来 ⇒ 产物全成了
       「孤儿」）。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """产物目录（**每次现取** —— 目录可能被配置改掉）。"""
        if self._root is not None:
            return self._root
        return dub_output_root()

    @property
    def index_path(self) -> Path:
        return self.root / _DUB_INDEX_NAME

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = ""
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            LOG.info("dub: 读不到产物清单（%s）⇒ 稍后按磁盘重建", exc)
            return
        try:
            data = json.loads(raw or "{}")
        except ValueError as exc:
            LOG.warning("dub: 产物清单是坏 JSON（%s）⇒ 按磁盘重建", exc)
            return
        items = (data or {}).get("items")
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            self._items[name] = dict(it)

    def _save_locked(self) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "items": list(self._items.values()),
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / (_DUB_INDEX_NAME + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.index_path)
        except OSError as exc:
            LOG.warning("dub: 产物清单写不下去（%s）: %s", self.index_path, exc)

    def _safe_path(self, artifact_id: str) -> Optional[Path]:
        """把**产物 id**（= 文件名）解析成目录内的真实路径。

        ⚠️ **必须是目录里的一个裸文件名** —— 不许带 ``/``、不许是
        ``..``、不许越界 ⇒ 这套端点**没有鉴权** ⇒ 不能让它变成
        「任意文件删除」的口子。
        """
        name = str(artifact_id or "").strip()
        if not name or name in (".", ".."):
            return None
        if "/" in name or "\\" in name or "\x00" in name:
            return None
        if name == _DUB_INDEX_NAME:
            return None
        root = self.root
        try:
            p = (root / name).resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        try:
            if not p.is_relative_to(root.resolve(strict=False)):
                return None
        except (AttributeError, ValueError):  # pragma: no cover
            if not str(p).startswith(str(root)):
                return None
        return p

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def list(self) -> List[Dict[str, Any]]:
        """列出**全部**产物（新的在前）+ 和磁盘对账。"""
        with self._lock:
            self._load_locked()
            root = self.root
            on_disk: Dict[str, Path] = {}
            try:
                for p in root.iterdir():
                    if not p.is_file():
                        continue
                    if p.name == _DUB_INDEX_NAME:
                        continue
                    if p.suffix.lower() not in _DUB_ARTIFACT_EXTS:
                        continue
                    on_disk[p.name] = p
            except OSError as exc:
                LOG.debug("dub: 扫产物目录失败（%s）: %s", root, exc)

            dirty = False
            # 清单里有、盘上没了 ⇒ 去掉
            for name in list(self._items.keys()):
                if name not in on_disk:
                    self._items.pop(name, None)
                    dirty = True
            # 盘上有、清单里没有 ⇒ 补进去
            for name, p in on_disk.items():
                if name in self._items:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                self._items[name] = {
                    "name": name,
                    "kind": "video" if p.suffix.lower() == ".mp4" else "audio",
                    "size": int(st.st_size),
                    "created_at": float(st.st_mtime),
                    "source_name": "",
                    "target_lang": "",
                    "voice": "",
                    "duration_sec": 0.0,
                    "orphan": True,
                }
                dirty = True
            # 大小 / 时间刷新（用户可能手工改过文件）
            for name, p in on_disk.items():
                it = self._items.get(name)
                if it is None:
                    continue
                try:
                    size = int(p.stat().st_size)
                except OSError:
                    continue
                if it.get("size") != size:
                    it["size"] = size
                    dirty = True
            if dirty:
                self._save_locked()

            out: List[Dict[str, Any]] = []
            for it in self._items.values():
                item = dict(it)
                item["id"] = item.get("name") or ""
                item["path"] = str(root / item["id"])
                item["size_text"] = _format_size(item.get("size") or 0)
                item["url"] = "/api/qwen3-asr/dub/files/{}/raw".format(
                    _urlquote(item["id"]))
                out.append(item)
            out.sort(key=lambda it: (float(it.get("created_at") or 0.0),
                                     it.get("id") or ""), reverse=True)
            return out

    def total_size(self) -> int:
        """产物**总量**（字节）—— 用于「占满磁盘」的提示。"""
        return int(sum(int(it.get("size") or 0) for it in self.list()))

    def register(self, path: Path, *, kind: str = "audio",
                 source_name: str = "", target_lang: str = "",
                 voice: str = "", duration_sec: float = 0.0) -> Dict[str, Any]:
        """登记一个产物（导出成功后调用）。"""
        p = Path(path)
        with self._lock:
            self._load_locked()
            try:
                st = p.stat()
                size = int(st.st_size)
                mtime = float(st.st_mtime)
            except OSError:
                size, mtime = 0, time.time()
            item = {
                "name": p.name,
                "kind": kind or ("video" if p.suffix.lower() == ".mp4" else "audio"),
                "size": size,
                "created_at": time.time(),
                "mtime": mtime,
                "source_name": source_name or "",
                "target_lang": target_lang or "",
                "voice": voice or "",
                "duration_sec": round(float(duration_sec or 0.0), 2),
            }
            self._items[p.name] = item
            self._save_locked()
            return dict(item)

    def delete(self, artifact_id: str) -> bool:
        """删掉一个产物（文件 + 清单）。"""
        p = self._safe_path(artifact_id)
        if p is None:
            return False
        with self._lock:
            self._load_locked()
            existed = False
            try:
                if p.is_file():
                    p.unlink()
                    existed = True
            except OSError as exc:
                LOG.warning("dub: 删不掉产物 %s（%s）: %s", p, exc, exc)
                return False
            if self._items.pop(p.name, None) is not None:
                existed = True
            self._save_locked()
            return existed

    def clear(self) -> Dict[str, Any]:
        """**一键清空**：删掉目录里所有产物 + 清掉清单。"""
        removed = 0
        freed = 0
        with self._lock:
            self._load_locked()
            root = self.root
            try:
                for p in list(root.iterdir()):
                    if not p.is_file():
                        continue
                    if p.name == _DUB_INDEX_NAME:
                        continue
                    if p.suffix.lower() not in _DUB_ARTIFACT_EXTS:
                        continue
                    try:
                        freed += int(p.stat().st_size)
                        p.unlink()
                        removed += 1
                    except OSError as exc:
                        LOG.warning("dub: 清空时删不掉 %s（%s）", p, exc)
            except OSError as exc:
                LOG.warning("dub: 清空产物目录失败（%s）: %s", root, exc)
            had = len(self._items)
            self._items.clear()
            self._save_locked()
        return {"removed": max(removed, had), "freed": freed}

    def path_of(self, artifact_id: str) -> Optional[Path]:
        """产物 id ⇒ 真实路径（**只在目录内**；不在就 None）。"""
        p = self._safe_path(artifact_id)
        if p is None:
            return None
        try:
            if not p.is_file():
                return None
        except OSError:
            return None
        return p


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


def _wav_duration_sec(path: Path) -> float:
    """WAV 文件的**时长**（秒）—— 读文件头，不解码。

    ⚠️ **别拿 ``len(pcm) / sample_rate`` 算** —— ``pcm`` 是**字节数**
    （float32 = 4 字节/采样）⇒ 那样算出来是真实时长的 **4 倍**（踩过）。
    """
    try:
        with wave.open(str(path), "rb") as wf:
            return float(wf.getnframes()) / float(max(1, wf.getframerate()))
    except Exception:  # noqa: BLE001 — 读不到就当没有
        return 0.0


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
        #: **产物清单**（落盘 ⇒ 重启不消失）。
        self._store = DubArtifactStore()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def start(self, *, kind: str, source_path: str, target_lang: str,
              speaker: str = "", ref_speaker: str = "") -> Dict[str, Any]:
        """起一次配音导出。

        ⚠️⚠️ **严格互斥**（2026-11-27 用户要求）—— 页面上**有识别在跑**时，
        **直接拒**（抛 :class:`DubBusyError` ⇒ HTTP **409**）⇒
        **不打断它、也不等它跑完**。

        ⇒ 上一版是「等它跑完（最多 1800 s）」⇒ 用户明确说**不要等**：
        按了「生成配音版」却卡在「等页面上的识别跑完」，看着像没反应。
        ⇒ 现在改成「**要么识别先停，要么导出先来**」。

        ⚠️ **以前这里是**直接掐断**它**（``svc.stop(reason="dub")``）⇒
        用户自己的识别被**掐断** ⇒ 页面上的「实时结果」停在**半截**
        ⇒ 用户报「实时结果只显示了一部分」⇒ **掐断**和**等待**都不对，
        正确做法是**互斥**（两边都不动）。
        """
        if kind not in DUB_KINDS:
            raise ValueError("配音导出只支持音频文件 / 含音轨的视频")
        src = Path(source_path)
        if not src.is_file():
            raise ValueError("源文件不存在: {}".format(source_path))

        # ⚠️⚠️ **互斥检查**（**必须在建作业之前**）——
        #    识别在跑 ⇒ 直接拒（409）⇒ **不打断、不等待**。
        #
        #    ⚠️ 判据用 ``file_busy()`` —— 只认**文件/视频**那条识别；麦克风的
        #    **实时**会话在**另一个服务**里（``get_live_asr_service``）⇒ 不占
        #    ASR 单例 ⇒ **不该拦**。
        #
        #    ⚠️ **要在建作业之前查** —— 否则这次调用会把**上一个**作业顶掉
        #    （``start()`` 里会 ``self._cancel.set()`` + 新建作业）⇒ 明明
        #    这次没起成，却把上一个搞没了。
        self._assert_page_asr_idle()

        # ⚠️⚠️ **磁盘闸**（2026-11-28 用户要求：「防止生成的文件可能占满
        #    本地磁盘」）—— 产物目录已经**超过上限** ⇒ **直接拒**（507）
        #    ⇒ 用户得先删几个文件（或一键清空）才能再导出。
        #
        #    ⇒ 拦在**建作业之前** —— 否则这次调用会把上一个作业顶掉
        #    （``start()`` 里会 ``self._cancel.set()``）⇒ 明明没起成，
        #    却把上一个搞没了。
        self._assert_quota()

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

        # ① **页面上有识别在跑 ⇒ 已经在上面拒了**（严格互斥，见
        #    :meth:`_assert_page_asr_idle`）⇒ 这里没有「等它跑完」那套了。
        #    （作业线程里还有**第二道**检查：``_check_page_asr`` —— 那是防
        #    「起作业之后、真正用 ASR 之前」用户又起了识别。）

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

    def running(self) -> bool:
        """现在有导出作业在跑吗。

        ⚠️ **给「识别」那边的互斥判断用**（2026-11-27 用户要求）——
        「实时翻译」和「配音导出」**都可能用到 ASR + 翻译** ⇒ 两者互斥：
        导出在跑时，**不允许**再发起一次识别（否则同一个 ASR 单例被搅坏）。

        ⚠️ **整个作业期间都算「在跑」** —— 包括它**等页面上的识别跑完**
        那段（那时它还没占 ASR，但也还没结束 ⇒ 不该让识别插进来）。
        """
        with self._lock:
            job = self._job
        return bool(job is not None and job.state == "running")

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

    def reset(self) -> Dict[str, Any]:
        """清掉上一个作业的**结果**（逐句原文 / 译文）。

        ⚠️ **为什么必须服务端清**（2026-11-27 用户要求）——
        页面进的时候会拉一次**最新那个作业**的状态（``GET /dub/status``）
        ⇒ 只在前端清 DOM 的话，用户**一刷新页面**，上次的逐句结果又回来了。

        ⚠️ **产物文件不删** —— 生成的配音文件在**产物目录**里（用户还要
        拿去播 / 拿去用）⇒ 「清空」只清**结果**，不动文件。

        ⇒ 要删文件 ⇒ 用「生成的文件」那一栏的**「删除」/「全部清除」**
        （那是**另一个**动作，见 :meth:`delete_output` / :meth:`clear_outputs`）。

        ⚠️ **正在跑的时候不清** —— 否则会清出一个「半截」的作业状态
        （句子清了、作业还在跑）⇒ 拒掉，让用户先点「停止」。

        ⚠️⚠️ **但「已经点过停止」的不算「正在跑」**（2026-11-27 踩到）——
        ``stop()`` 只是**置一个取消标志**（``self._cancel``）⇒ 作业线程要
        等到下一个检查点才会退出 ⇒ 状态**在那几秒里还是 ``running``** ⇒
        用户明明点了「停止」，却清不掉 ⇒ 提示还写着「先点停止」（已经点过了！）
        ⇒ **取消标志已置 ⇒ 允许清**（那个线程已经在收尾，作业对象会被丢开）。

        ⇒ 清完 = **作业记录也扔掉**（``self._job = None``）⇒ 状态回到
        ``idle`` ⇒ 页面刷新时拉不到作业 ⇒ 结果不会复活。
        """
        with self._lock:
            job = self._job
            if job is None:
                return {"ok": True, "cleared": False, "reason": "没有可清的作业"}
            if job.state == "running" and not self._cancel.is_set():
                return {"ok": False, "cleared": False,
                        "reason": "配音导出还在跑，先点「停止」"}
            self._job = None
            self._thread = None
        return {"ok": True, "cleared": True}

    def list_outputs(self, source_path: str = "") -> List[Dict[str, Any]]:
        """列出**已经生成**的配音产物（供页面下拉选择）。

        ⚠️ **2026-11-28 起是全局的**（不再按源文件筛）—— 产物统一落在
        本卡专属的产物目录（``app/uploads/qwen3_asr/dub/``）⇒ 清单就是
        那一个目录里的东西 ⇒ 跟当前源文件**无关**。

        ⚠️ ``source_path`` **保留但不再使用** —— 旧调用方（回归脚本 /
        旧前端）还会传 ⇒ 别让它报错。

        ⇒ 「重启不消失」靠的是**清单落盘**（见 :class:`DubArtifactStore`），
        不是靠扫目录猜。
        """
        return self._store.list()

    def list_artifacts(self) -> Dict[str, Any]:
        """产物列表 + 占用量（页面「生成的文件」那一栏要的就是这个）。"""
        items = self._store.list()
        total = int(sum(int(it.get("size") or 0) for it in items))
        return {
            "items": items,
            "count": len(items),
            "total_size": total,
            "total_size_text": _format_size(total),
            "limit": _DUB_MAX_TOTAL_BYTES,
            "limit_text": _format_size(_DUB_MAX_TOTAL_BYTES),
            "dir": str(dub_output_root()),
        }

    def output_path(self, artifact_id: str) -> Optional[Path]:
        """产物 id ⇒ 真实路径（**只在产物目录内**；不在 / 不存在 ⇒ None）。

        ⚠️ API 层用它来**回放**文件 ⇒ 它是「不收用户给的路径」的那道闸
        （只认清单里的裸文件名）。
        """
        return self._store.path_of(artifact_id)

    def delete_output(self, artifact_id: str) -> Dict[str, Any]:
        """删掉**一个**产物（文件 + 清单项）。

        ⚠️ **只认清单里的 id**（= 产物目录里的一个裸文件名）⇒ 端点
        没有鉴权，这道闸不能松。
        """
        p = self._store.path_of(artifact_id)
        if p is None:
            return {"ok": False, "deleted": False,
                    "reason": "找不到这个文件（可能已经被删了）"}
        ok = self._store.delete(artifact_id)
        return {"ok": bool(ok), "deleted": bool(ok),
                "id": artifact_id, "name": p.name}

    def clear_outputs(self) -> Dict[str, Any]:
        """**一键清空**：产物目录里所有产物 + 清单。"""
        res = self._store.clear()
        return {
            "ok": True,
            "removed": res.get("removed", 0),
            "freed": res.get("freed", 0),
            "freed_text": _format_size(res.get("freed", 0)),
        }

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

    def _assert_quota(self) -> None:
        """产物目录**已超上限**吗 ⇒ 超了就**拒**（抛 :class:`DubQuotaError`）。

        ⚠️ **为什么要这道闸**（2026-11-28 用户原话：「这样做的目的是为了
        防止说生成的文件可能占满本地磁盘的风险」）——
        产物是「点一次生成一次」⇒ 用户不盯着就能堆几十 G ⇒ 光有
        「删除 / 一键清除」按钮不够，得**在源头**拦住。

        ⚠️ **拦在起作业之前** ⇒ 不会把上一个正在跑的作业顶掉。
        """
        try:
            used = self._store.total_size()
        except Exception as exc:  # noqa: BLE001 — 算不出来就放行
            LOG.debug("dub: 算产物占用失败（放行）: %s", exc)
            return
        if used >= _DUB_MAX_TOTAL_BYTES:
            raise DubQuotaError(
                "配音产物目录已经用了 {}（上限 {}）⇒ 先在「生成的文件」"
                "那里删掉几个，或点「全部清除」，再导出".format(
                    _format_size(used), _format_size(_DUB_MAX_TOTAL_BYTES)))

    def _assert_page_asr_idle(self) -> None:
        """页面上**有识别在跑**吗 ⇒ 有就**拒**（抛 :class:`DubBusyError`）。

        ⚠️ **严格互斥**（2026-11-27 用户要求）—— 上一版是「等它跑完」
        （最多 1800 s，超时才停掉它）⇒ 用户明确说**不要等**。

        ⇒ 「生成配音版」要么**先**开始（识别没在跑），要么用户**先停掉**
        识别 ⇒ 不再出现「按了没反应、卡在『等页面上的识别跑完』」。

        ⚠️ **只认「文件路径」的识别**（``file_busy()``）—— 麦克风那种
        **实时**会话在**另一个服务**里（``get_live_asr_service``）⇒ 不占
        ASR 单例 ⇒ **不拦**。
        """
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import (
                get_asr_service,
            )
            svc = get_asr_service()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("dub: 取 ASR 服务失败（best-effort）: %s", exc)
            return
        if svc.file_busy():
            LOG.info("dub: 页面上的识别还在跑 ⇒ 配音导出起不来（互斥）")
            raise DubBusyError(
                "识别还在跑 ⇒ 配音导出起不来（互斥）：先点「停止」停掉识别，"
                "或等它跑完再来导出")

    def _check_page_asr(self, job: DubJob) -> None:
        """页面上**还有识别在跑**吗 ⇒ 有就**拒**（**不打断、也不等**）。

        ⚠️ **2026-11-27 用户要求：严格互斥** —— 上一版这里是「**等**它跑完
        （上限 1800 s，超时才停掉它）」⇒ 用户明确说**不要等** ⇒ 改成
        **直接失败**。

        ⇒ 「生成配音版」要么**先**开始（识别没在跑），要么**先停掉识别**
        ⇒ 不再出现「按了没反应、卡在『等页面上的识别跑完』」。

        ⚠️ **为什么还要留这一层**（``start()`` 里已经拒过一次了）——
        ``start()`` 的拒是**给调用方**看的（HTTP 409）；这里是**给作业本身**
        看的（作业失败 + 人话）⇒ 因为 ``start()`` 之后、作业线程真正去用
        ASR 之间还有**时间窗** ⇒ 那时候用户可能又起了识别 ⇒ 这一刻必须
        再挡一次（否则两个流会搅坏同一个增量解码器）。

        ⚠️ **只认「文件路径」的识别**（``file_busy()``）—— 麦克风那种
        **实时**会话在**另一个服务**里（``get_live_asr_service``）⇒ 不占
        这个单例 ⇒ **不拦**。
        """
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import (
                get_asr_service,
            )
            svc = get_asr_service()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("dub: 取 ASR 服务失败（best-effort）: %s", exc)
            return
        if svc.file_busy():
            LOG.info("dub: 页面上的识别还在跑 ⇒ 配音导出起不来（互斥）")
            raise RuntimeError(
                "识别还在跑 ⇒ 配音导出起不来（互斥）：先停掉识别，或等它跑完")

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
            # ---------- ⓪ 页面上的识别在跑？⇒ 拒（互斥） ------------------ #
            # ⚠️ **不掐断它，也不等它**（2026-11-27 用户要求：严格互斥）。
            self._check_page_asr(job)

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
            # ⚠️ **源时长记在作业上**（2026-11-27 用户要求）—— 页面跑完要写
            #    一条完成信息「配音完成（12.34 s · 音频 15.1 s · 12 句）…」
            #    ⇒ 「音频 15.1 s」这一项就是它（与识别那条同一个口径）。
            job.duration_sec = float(_dur or 0.0)
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
            # ⚠️ **登记进产物清单**（2026-11-28）—— 页面上的下拉列表靠它
            #    ⇒ 清单落盘 ⇒ **重启不消失**。
            self._register_artifact(job, out_audio, "audio",
                                    duration_sec=_wav_duration_sec(out_audio))

            # ---------- ⑤ 视频：烧双字幕 + 重新封装 ---------------------- #
            if job.kind == "video":
                self._set_phase(job, "烧字幕并重新封装", 0.88)
                self._render_video(job, src, out_audio, pcm, sr, sentences,
                                   placements, origin_sec)
            self._set_phase(job, "完成", 1.0)
            job.state = "done"
            job.message = "完成"
            # ⚠️ **超了上限就说一声**（产物已经写好了，不能扔）⇒ 页面上的
            #    「生成的文件」那一栏会把占用量显示出来 ⇒ 用户自己决定删不删。
            try:
                used = self._store.total_size()
            except Exception:  # noqa: BLE001
                used = 0
            if used >= _DUB_MAX_TOTAL_BYTES:
                job.warnings.append(
                    "配音产物目录已经用了 {}（上限 {}）⇒ 建议在「生成的文件」"
                    "那里删掉几个，或点「全部清除」".format(
                        _format_size(used), _format_size(_DUB_MAX_TOTAL_BYTES)))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("dub: 作业失败")
            job.state = "error"
            job.error = "{}: {}".format(type(exc).__name__, exc)
            job.message = job.error
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            job.finished_at = time.time()

    def _register_artifact(self, job: DubJob, path: Path, kind: str,
                           duration_sec: float = 0.0) -> None:
        """把产物登记进清单（**失败不致命** —— 清单只是给人看的列表）。"""
        try:
            self._store.register(
                path,
                kind=kind,
                source_name=job.source_name or Path(job.source_path).name,
                target_lang=job.target_lang or "",
                voice=job.ref_speaker or job.speaker or "",
                duration_sec=duration_sec,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("dub: 登记产物失败（%s）: %s", path, exc)

    def _set_phase(self, job: DubJob, phase: str, progress: float) -> None:
        job.phase = phase
        job.progress = float(max(0.0, min(1.0, progress)))
        job.message = phase

    def _output_path(self, job: DubJob, suffix: str) -> Path:
        """``<源文件名>_<目标语言>_<音色><后缀>``，落在**产物目录**里。

        ⚠️ **不再落源文件同级目录**（2026-11-28 用户要求）—— 落到
        ``app/uploads/qwen3_asr/dub/``（工程目录内，本卡专属）⇒
        ① 源目录只读也能导出；② 产物集中一处，用户找得到；
        ③ 能统一列 / 统一删 ⇒ **磁盘占用管得住**。

        ⚠️ **文件名规则没变** ⇒ 同样的源 + 语言 + 音色 ⇒ **同一个文件**
        ⇒ 重复导出是**覆盖**，不会无限堆积。
        """
        src = Path(job.source_path)
        voice = job.ref_speaker or job.speaker or "default"
        name = "{}_{}_{}{}".format(
            sanitize_name(src.stem, "media"),
            sanitize_name(job.target_lang or "und", "und"),
            sanitize_name(voice, "default"),
            suffix,
        )
        return dub_output_root() / name

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
                # ⚠️ **源语言**（2026-11-27）—— 结果面板要标
                # 「源语言 → 导出语言」⇒ 这里顺手记下来。
                _lang = str(snap.get("language") or "").strip()
                if _lang and not job.source_lang:
                    job.source_lang = _lang
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
            # ⚠️ **源语言**（2026-11-27）—— 复用页面那次识别时，语种是
            # ASR 服务记下来的（``service.py::_remember_file_result``）。
            _lang = str(rec.get("language") or "").strip()
            if _lang:
                job.source_lang = _lang
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
        # ⚠️ **登记进产物清单**（2026-11-28）—— 与音频产物同一个清单 ⇒
        #    页面的下拉里两个都在。
        self._register_artifact(job, out_video, "video",
                                duration_sec=_media_duration_sec(out_video))
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
