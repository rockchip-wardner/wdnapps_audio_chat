"""TTS **合成队列** —— 同声传译的播放侧（2026-11-21）。

为什么要有队列
--------------
TTS 卡是**单实例**：一次只能合成一句。同传是「一句接一句」地来
⇒ 直接调 ``/api/tts/start`` 会在忙的时候抛「TTS 正在运行」⇒ 句子全丢。

⇒ 这里给同传一条**独立**的排队通道：

* **两段流水线**（合成线程 + 播放线程）—— 见下面「为什么要拆两段」。
* **不丢内容** —— 队列满时**合并队尾两段**（文本拼成一段重新合成）⇒
  内容一个不丢，代价只是那句更长。
* **变速** —— ``tempo`` 只在**积压**时提速（sox ``tempo`` 效果，不变调），
  常态下音质完全不受影响。
* **滞后记账** —— ``lag_sec = 播完时刻 − 源话说完时刻`` ⇒ 面板能如实显示
  「落后几秒」。

为什么要拆成「合成线程 + 播放线程」
------------------------------------
**这是本文件存在的全部理由。**

Qwen3-TTS 的 decoder **首窗要 50 帧 = 4 秒音频**（``kWindowLen =
kChunkSize + kLeftContextSize``）⇒ talker 得先吐 50 个语义 token 才出
第一块 PCM ⇒ **首窗延迟 ≈ 3.7 秒**（实测：50 步 talker ≈ 3.7 s）。

原来「合成 + 播放」是焊死在一句里的（``PLAY_BOTH``：PCM 一到就进 aplay）：

    合成 3.7s（不出声）→ 边合成边播 6.6s → 合成完还要等播放尾巴
    ⇒ 一句 10.3s 墙钟，只装了 6.6s 音频 ⇒ **吞吐 0.67× 实时**

⇒ 实时同传的滞后**必然越落越远**（实测 25 s 源音频 ⇒ 滞后 23 s 且
持续增长；用户实测面板显示 18.5 s）。

拆开之后（``play_to=queue``：会话只产出 PCM，播放交给本文件的播放器
线程）：

    合成线程：  句1[0, 6.2]  句2[6.2, 12.4]  句3[12.4, 18.6] …
    播放线程：        句1[3.7, 10.3]  句2[10.3, 16.9] …

⇒ 下一句的合成与这一句的播放**重叠** ⇒ 每句墙钟 = 播放时长
⇒ **吞吐 ≈ 1× 实时**（实测 0.67× → 1.02×），滞后不再增长。

⚠️ **单实例下不需要 per-request PCM socket**：TTS 的 ``_run_safe`` **每次
run 都新建 UDS socket**（``service.py``）⇒ 串行消费时天然一请求一
socket，不会两条流交错。

⚠️ **队列满时合并队尾**，不丢内容 —— 这是「一句话都不能丢」原则的落点。

**共享 FIFO**（2026-11-22 加）
-------------------------
合成线程把 PCM **直接推进一个共享 FIFO**，播放线程从 FIFO 连续取 ⇒

* **句间不断音** —— 下一句的音频在上一句还在播的时候就已经在 FIFO 里
  （原来是「一句一缓冲」⇒ 播放线程要等这句合成完才去取下一句 ⇒ 下一句
  的首窗期间**管道是空的** ⇒ 每句之间断一次，实测 0.28~0.85 s）；
* **抖动缓冲 = FIFO 本身** —— 合成比播放快时它自动蓄水 ⇒ 合成抖动不会
  立刻变成断音；
* **顺序天然保证** —— FIFO ⇒ 播放顺序 = 合成顺序 = 提交顺序。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from app.core.audio.tts_bridge import PLAY_BOTH, TTSUnavailable

LOG = logging.getLogger(__name__)

#: TTS 的采样率（模型固定 24 kHz 单声道）—— 记账用。
_SAMPLE_RATE = 24000

#: 队列深度（段）。只播不存 ⇒ 内存里只有文本，很便宜。
QUEUE_MAX = 128

#: 变速上下限。超过 1.15 音质明显变紧（实测 tempo 1.15 缩短 13.0%）。
#:
#: ⚠️ **下限必须 < 1.0**（实测 2026-11-23）：TTS 的**可持续产能只有
#: ~0.96×**（talker 自回归 ⇒ 物理上限）⇒ 播放按 1.0 消费就比生产快
#: ⇒ **管道存货被慢慢吃空** ⇒ 到**收尾**时存货见底 ⇒ 用户听到
#: 「句子被隔断，隔了很长时间才播最后几个词」（实测 15 s 素材：句间
#: 间隔从 0.045 s 一路涨到 0.7 s）。
#: ⇒ 变速必须能**低于 1.0**，好让「播放速度 ≤ 可持续产能」⇒ 存货不塌。
TEMPO_MIN = 0.9
TEMPO_MAX = 1.15

#: 自动变速的档位（按积压秒数选）—— 只在**产能不足**时启用。
TEMPO_STEPS = (
    (8.0, 1.15),
    (4.0, 1.08),
    (2.0, 1.04),
)

#: **变速阶梯**（只取这几个值）—— 变速变了要**重开 aplay 管道**
#: （sox 的 tempo 是 spawn 时定死的）⇒ 变速必须**离散**，否则会频繁重开
#: ⇒ 每次重开都有一次句间缝隙。
#:
#: ⚠️ **阶梯里必须有 < 1.0 的档**（实测 2026-11-23）：可持续产能实测
#: **0.95~0.99** ⇒ 没有 0.92/0.96 这两档 ⇒ 上限只能取 1.0 ⇒ 比产能
#: 快 ⇒ 存货见底 ⇒ **收尾断音**（用户报「最后收尾句子被隔断」）。
TEMPO_LADDER = (0.92, 0.96, 1.0, 1.04, 1.08, 1.15)

#: **播放时钟卡住多久就放弃等待**（秒）—— 源不再往前走时不能把译文
#: **永久压住**（否则「有翻译、一声不响」，踩过好几次）。
_CLOCK_STALL_SEC = 8.0


#: **空闲多久就把 aplay 关掉**（秒）—— 复用 writer 的代价是**占着 ALSA
#: 设备**（设备是独占的）⇒ 同传一停下来就得让出来。
_WRAPPER_IDLE_CLOSE_SEC = 15.0

#: ⚠️ **「开口前先攒缓冲」这个机制已经删掉了**（2026-11-22）：
#:
#: 原来每句开头都要等「这句收到了 3 s 音频」才出声 ⇒ **句与句之间**
#: 白等一个首窗（~2 s）⇒ **每句之间断一次音**（实测句间间隔
#: 0.28~0.85 s ⇒ 用户报「播放中途会有卡顿断音」）。
#:
#: ⇒ 改成**共享 FIFO**（合成线程直接把 PCM 推进 FIFO）⇒ 下一句的音频
#: 在上一句还在播的时候就已经在 FIFO 里 ⇒ **句间零缝隙**。
#: ⇒ 抖动缓冲 = **FIFO 本身**（合成比播放快时它自动蓄水）。


def _estimate_audio_sec(text: str) -> float:
    """估算这句念出来有多长（秒）。

    粗估就够 —— 它只用来判断「积压了多少秒」，不参与播放。
    中文约 4.5 字/秒，英文约 14 字符/秒。
    """
    cjk = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            cjk += 1
    other = max(0, len(text) - cjk)
    return max(0.6, cjk / 4.5 + other / 14.0)


# --------------------------------------------------------------------------- #
# **开头静音裁剪**
# --------------------------------------------------------------------------- #

#: 判定「有声」的 RMS 阈值（0~1，相对满刻度）。
#:
#: ⚠️ 模型原始输出的峰值只有 0.15~0.40 ⇒ 0.0025 约等于峰值的 1% ⇒
#: 足够躲开静音，又不会把**轻声起始**误判成静音。
_TRIM_HEAD_RMS = 0.0025
#: 判定窗口（毫秒）。
_TRIM_HEAD_WIN_MS = 5
#: **最多裁掉多少秒**（防呆：万一整段前面都是静音，别把内容裁没了）。
_TRIM_HEAD_MAX_SEC = 1.2


def trim_head_silence(audio, sample_rate: int):
    """把 PCM **开头那段静音**切掉；没有可裁的返回 ``None``。

    ⚠️ **为什么裁**（实测 2026-11-23，M29；用户报「TTS 播放滞后非常多」）：

    * 模型每段的开头都带 **0.35~0.65 秒**静音（实测三段译文的 WAV）
      ⇒ ① 用户听到的声音要**等这段静音走完**才出来 ⇒ **可听滞后直接
      多这么多**；② 音频总长也多这么多 ⇒ **长度比 k 变大** ⇒ 滞后增长更快；
    * 裁掉的是**空白**，不是内容 ⇒ 字幕不变、一个字不少。

    ⚠️ **只裁「一段的开头」**（不是每块 PCM）—— 段中间的停顿是**语意
    停顿**，裁掉会听起来像掉字。
    """
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return None
    win = max(1, int(max(int(sample_rate), 1) * _TRIM_HEAD_WIN_MS / 1000))
    limit = min(arr.size, int(max(int(sample_rate), 1)
                              * _TRIM_HEAD_MAX_SEC) + win)
    if limit < win:
        return None
    cut = -1
    for i in range(0, limit - win + 1, win):
        seg = arr[i:i + win]
        if seg.size == 0:
            break
        rms = float(np.sqrt(np.mean(np.square(seg, dtype=np.float64))))
        if rms > _TRIM_HEAD_RMS:
            cut = i
            break
    if cut <= 0:
        # 整块都在阈值以下 ⇒ 这块是静音 ⇒ **留给下一块**继续裁
        return None
    # ⚠️ **回退一个窗口**（5 ms）—— 怕把轻声的起始切掉。
    cut = max(0, cut - win)
    if cut <= 0:
        return None
    return arr[cut:]


class _JobStream:
    """**一句**的 PCM 分片缓冲。

    合成线程（UDS reader 线程上）往里塞分片，播放线程往外取。
    有了它，播放器可以在**第一块 PCM 到达时**就出声，不必等整句合成完
    ⇒ 首窗延迟（3.7 s）不再被算进「播完时刻」。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._chunks: List[np.ndarray] = []
        self._pos = 0
        self._finished = False
        #: 已经收到的音频时长（秒）—— 记账用。
        self.audio_sec = 0.0
        #: **产量标记**（时刻, 累计音频秒）—— 用来算**边际速率**。
        #:
        #: ⚠️ **为什么不能只看「总音频 ÷ 总时间」**：那把**首窗延迟**
        #: （2~3 s 不出声）摊薄进去了 ⇒ 一句短文本的速率会低到离谱
        #: （0.4~0.6×）⇒ 变速判据被首窗带偏 ⇒ 「产能不够」的结论是假的。
        #: ⇒ 这里只取**首窗之后**的增量（首窗之后的每一块都是「流水线
        #: 稳态」下的产物）。
        self._marks: List[tuple] = []

    # -- 写（合成线程） --------------------------------------------------- #
    def push(self, audio, sample_rate: int, store: bool = False) -> None:
        """记一块 PCM（**默认不存**——真正的存放处是**共享 FIFO**）。

        ⚠️ **为什么默认不存**：PCM 已经进了共享 FIFO（见
        :meth:`TTSQueue._fifo_push`）⇒ 这里再存一份就是**双份内存**。
        ⇒ 这里只做**记账**（音频时长 / 产量标记）。
        """
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        with self._cond:
            if store:
                self._chunks.append(arr)
            self.audio_sec += arr.size / float(max(int(sample_rate), 1))
            self._marks.append((time.time(), self.audio_sec))
            self._cond.notify_all()

    def marginal_rate(self) -> float:
        """**边际生产速率**（首窗之后的音频秒 / 墙钟秒）。

        ⚠️ **这是「产能」的真值** —— 决定译文能不能跟住源话的就是它
        （首窗延迟是**一次性**的，不影响稳态产能）。
        """
        with self._cond:
            marks = list(self._marks)
        if len(marks) < 2:
            return 0.0
        t0, a0 = marks[0]
        t1, a1 = marks[-1]
        dt = t1 - t0
        if dt <= 0.2:
            return 0.0
        return (a1 - a0) / dt

    def finish(self) -> None:
        """合成结束（成功或失败）—— 播放器据此收尾。"""
        with self._cond:
            self._finished = True
            self._cond.notify_all()

    # -- 读（播放线程） --------------------------------------------------- #
    def take(self) -> List[np.ndarray]:
        """取走还没取过的分片（可能为空）。"""
        with self._cond:
            out = self._chunks[self._pos:]
            self._pos = len(self._chunks)
            return out

    def wait(self, timeout: float = 0.2) -> None:
        """等到有新分片或合成结束。"""
        with self._cond:
            if self._pos < len(self._chunks) or self._finished:
                return
            self._cond.wait(timeout)

    @property
    def has_data(self) -> bool:
        with self._cond:
            return self._pos < len(self._chunks)

    @property
    def done(self) -> bool:
        """合成结束 **且** 分片都取走了。"""
        with self._cond:
            return self._finished and self._pos >= len(self._chunks)


@dataclass
class TTSJob:
    """队列里的一句。"""
    seq: int
    text: str
    language: str = "auto"
    speaker: str = ""
    #: **参考音色**（voice-clone）的名字 —— 与 ``speaker`` 是**两个不同的
    #: SDK 参数**，不能混用！
    #:
    #: ⚠️ **混用 ⇒ 音色完全不生效**（实测）：SDK 侧
    #: ``ref_speaker`` 命中 ``speaker_embeddings`` 时**优先**用它；
    #: 而把参考音色名塞进 ``speaker``（内置音色 SPK_ID）⇒ SDK 打印
    #: ``[speaker] 'baby_girl' not in SPK_ID, skipping`` ⇒ **没有音色条件**
    #: ⇒ 模型每次自由发挥 ⇒ **每句话音色都不一样**。
    #:
    #: ⇒ 本板（Base 权重）只有**参考音色**可用 ⇒ 必须走 ``ref_speaker``。
    ref_speaker: str = ""
    volume: float = 0.5
    tempo: float = 1.0
    play_to: str = PLAY_BOTH
    stream_id: str = ""
    #: **首个解码窗口攒多少帧才开声**（0 = SDK 默认 25）。
    #:
    #: ⚠️ 上游首窗硬等 50 帧（4 s 音频）⇒ 实测「合成首窗」≈ 3.0 s ⇒
    #: 同传链路上**最大的一段固定延迟**。decoder 是因果的 ⇒ 首窗攒
    #: 25 帧即可 ⇒ 首声提前 ~1.5 s（见
    #: ``wdnai.backends.tts.FIRST_WINDOW_FRAMES``）。
    first_window_frames: int = 0
    #: 源话**说完**的时刻（秒，wall clock 口径）。用来算「播报滞后」。
    #:
    #: ⚠️ **实时流和文件源都要给** —— 文件源之前传 0.0 ⇒ 队列**不算滞后**
    #: ⇒ 面板只能显示**上一轮留下的残留值**（实测显示 18.48 秒，
    #: 而那一轮的文件源其实全程 0 滞后）⇒ 误导。
    src_end_ts: float = 0.0
    #: 源话**开口**的时刻（秒，wall clock 口径）—— 用来算**可听滞后**
    #: （译文出声的时刻 − 源话开口的时刻）⇒ 这才是用户感受到的那一口。
    src_start_ts: float = 0.0
    #: 源话在**媒体时间轴**上的开口位置（秒；-1 = 不知道）。
    #:
    #: ⚠️ **跟播的「播放时钟」靠它** —— 播放线程等播放头走到
    #: ``src_start_sec`` 才把这一句写进管道 ⇒ **译文跟源话对齐**
    #: （合成提前跑，播放按源话走 ⇒ 既有缓冲余量，又不滞后）。
    src_start_sec: float = -1.0
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    # ------------------------------------------------------------------ #
    # **分段计时**（诊断「同传滞后 15 秒」花在哪一段）
    # ------------------------------------------------------------------ #
    #: **第一块 PCM 到达**的时刻（wall clock）——
    #: 减去 ``started_at`` = **合成首窗延迟**（这段之前一声不响）。
    synth_first_pcm_at: float = 0.0
    #: **真正出声**的时刻（写进 aplay 的第一块）——
    #: 减去 ``synth_first_pcm_at`` = 播放侧的排队/启动开销。
    play_started_at: float = 0.0
    #: 估算音频时长（秒）—— 积压估算用。
    est_audio_sec: float = 0.0
    #: **实际收到的音频时长**（变速前，秒）—— 用来算**合成速率**。
    audio_sec: float = 0.0
    #: **边际生产速率**（首窗之后的音频秒 / 墙钟秒）—— 「产能」的真值。
    marginal_rate: float = 0.0
    #: **整体速率**（总音频 / 总合成时间，含首窗）—— 只作诊断。
    rate_overall: float = 0.0
    #: **开始播时管道里的存货**（秒）—— 用来判断「缓冲够不够」。
    pipe_stock_at_start: float = 0.0
    #: **饿死了多久**（管道空着等合成）—— 「卡顿」的度量。
    starved_sec: float = 0.0
    #: **可听滞后**（译文出声 − 源话开口，秒）—— 用户真正感受到的那一口。
    a_lag: float = 0.0
    #: 这句合并过几段（队满时合并队尾）。
    merged: int = 0
    #: 播完后的滞后（秒）。
    lag_sec: float = 0.0
    #: 本句**真正送进 aplay** 的 PCM 峰值（0~1）—— 电平探针。
    peak: float = 0.0
    #: 本句**缩放前**（模型原始）的 PCM 峰值。
    raw_peak: float = 0.0
    #: 本句的 PCM 分片缓冲（合成线程写、播放线程读）。
    stream: Optional[_JobStream] = None
    #: **开头静音已裁掉**（见 :func:`trim_head_silence`）—— 只裁**一段**的开头。
    head_trimmed: bool = False
    #: 合成失败 / 被清掉 ⇒ 播放器跳过它。
    cancelled: bool = False


@dataclass
class _Counters:
    seq: int = 0
    done: int = 0
    merged: int = 0
    dropped: int = 0
    failed: int = 0
    last_lag_sec: float = 0.0
    lag_sum_sec: float = 0.0
    lags: list = field(default_factory=list)
    #: **电平探针**：最近一句**真正出声**的 PCM 峰值（0~1）。
    #: ⚠️ ``0`` ⇒ **没出声**（aplay 没起来 / 没有 PCM）；
    #: ⚠️ 很小（< 0.05）⇒ 播了但**几乎听不见**（``volume`` 是振幅倍率）。
    last_peak: float = 0.0
    #: 最近一句**缩放前**（模型原始）的峰值 —— 用来算「本来该多大」。
    last_raw_peak: float = 0.0
    #: 最近播完的一句的**可听滞后**（译文出声 − 源话开口，秒）。
    last_a_lag: float = 0.0


class TTSQueue:
    """单实例串行合成队列（**合成与播放分两段**）。"""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._jobs: List[TTSJob] = []
        #: 已从 ``_jobs`` 取出、但**还没播完**的 job（合成中 / 等播 / 播中）。
        #:
        #: ⚠️ **必须算进积压** —— 两段流水线之后，「已合成、等播」的那几句
        #: 才是真正的积压（实测实时同传的滞后主要来自这里）⇒ 只统计
        #: ``_jobs`` 会让自动变速**看不到积压** ⇒ 永远不变速 ⇒ 越落越远。
        self._inflight: List[TTSJob] = []
        #: **PCM 先进先出缓冲**（``(job, chunk)``）—— 合成线程 → 播放线程的
        #: **唯一**音频通道。
        #:
        #: ⚠️ **为什么不能「一句一缓冲」**（实测 2026-11-22，用户报「播放
        #: 中途会有卡顿断音」）：原来每句一个 ``_JobStream`` ⇒ 播放线程
        #: **要等这句的合成结束**才去取下一句 ⇒ 下一句的首窗（~2 s）里
        #: **管道是空的** ⇒ **每句之间断一次音**（实测句间间隔
        #: 0.28~0.85 s）。
        #:
        #: ⇒ 现在合成线程把 PCM **直接推进这个共享 FIFO** ⇒ 下一句的音频
        #: 在上一句还在播的时候就已经在 FIFO 里等着 ⇒ **句间零缝隙**。
        self._fifo: "deque[tuple]" = deque()
        self._fifo_cond = threading.Condition(threading.Lock())
        #: FIFO 里还没写进管道的音频秒数（抖动缓冲的深度）。
        self._fifo_sec = 0.0
        self._c = _Counters()
        self._playing: Optional[TTSJob] = None      # 正在合成的
        self._speaking: Optional[TTSJob] = None     # 正在播的
        self._tempo = 1.0
        self._stop = False
        #: **实测合成速率**（音频秒 / 墙钟秒，EMA）—— 变速按它定。
        self._rate = 0.0
        #: **可持续速率**（整体速率的 EMA，含首窗与 prefill）——
        #: **变速上限按它算**（不是边际速率：边际速率把首窗排除 ⇒ 高估）。
        self._sustain_rate = 0.0
        #: **上一句的「播完时刻」** —— 用来算**句间间隔**（断音的度量）。
        self._last_finished_at = 0.0
        #: **播放时钟**（跟播用）—— 一个返回「当前播放头（媒体秒）」的
        #: 可调用对象；``None`` = 不按源话时钟播放（「now」模式）。
        #:
        #: ⚠️ **为什么要有它**（实测 2026-11-22）：「跟播」要同时满足
        #: ① 译文**跟源话对齐** ② 管线**有缓冲余量**（不断音）。
        #: 之前这两件事**挤在同一个「提前量」上** ⇒ 此消彼长：
        #: 提前量紧 ⇒ 没余量 ⇒ 断音；提前量松 ⇒ 译文晚好几秒。
        #:
        #: ⇒ **拆开**：放行器按「生产延迟 + 余量」提前放行（合成先跑），
        #: **播放**由**源话的播放头**驱动（等源话点到那一句才放出来）
        #: ⇒ 两件事各归各管。
        self._clock_provider = None
        #: **播放时钟的偏移**（秒）—— 播放头到达 ``src_start − offset``
        #: 才放这句译文。offset > 0 ⇒ 译文比源话**晚** offset 秒。
        self._clock_offset = 0.0
        #: **时钟卡住多久就放弃等待**（秒）—— 源不再往前走时不能把译文
        #: 永久压住（否则「有翻译、一声不响」）。
        self._clock_stall_at = 0.0
        self._clock_last = -1.0
        #: **FIFO 里留多少秒不播**（延迟线）—— 0 = 不留（直接播）。
        #:
        #: ⚠️ **为什么需要**（实测 2026-11-22）：「零断音」和「零滞后」
        #: 是**此消彼长**的 —— 放行器的提前量紧了 ⇒ 管线没有余量 ⇒ 合成
        #: 一抖就是断音；提前量松了 ⇒ 译文比源话晚好几秒。
        #:
        #: ⇒ 拆开：**提前量 = 生产延迟 + 留量**，而**留量**由 FIFO 扣着
        #: （最新的 N 秒不播）⇒ ① 管线永远有 N 秒余量（不断音）② 播放
        #: 时刻可精确对齐（可听滞后 ≈ 0）。
        self._reserve = 0.0
        #: **待归属的句间间隔**（管道在两句之间空着等了多久）——
        #: 归到**下一句**的 ``gap`` 上（见 ``_finish_job``）。
        self._gap_pending = 0.0
        #: **管道第一次被观测为「空」的时刻** —— 用来算**精确**的断音
        #: 时长（轮询粒度 0.2 s 只能给出「≤0.2 s」这种糊话）。
        self._dry_since = 0.0
        #: **当前复用的 aplay 管道**（跨句共用 ⇒ 消句间静音）。
        #: 放在队列上 ⇒ ``clear()`` / ``shutdown()`` 能把它关掉
        #: （ALSA 设备是**独占**的 ⇒ 同传停下来就得让出来）。
        self._writer = None
        #: 已经播完的句子（文本，环形保留最近 200 条）—— 回归脚本用它
        #: 断言「**播放顺序 = 提交顺序**」。
        self._played: List[str] = []
        #: **分段计时**（最近 60 句）—— 诊断「同传滞后 N 秒」花在哪一段。
        self._played_timings: List[dict] = []
        self._infer_thread = threading.Thread(
            target=self._infer_loop, name="tts-queue-infer", daemon=True)
        self._play_thread = threading.Thread(
            target=self._play_loop, name="tts-queue-play", daemon=True)
        self._infer_thread.start()
        self._play_thread.start()

    # ------------------------------------------------------------------ #
    # **共享 FIFO**（合成线程 → 播放线程）
    # ------------------------------------------------------------------ #
    def _fifo_push(self, job: TTSJob, chunk) -> None:
        """把一块 PCM 推进共享 FIFO（``(job, chunk, 生产时刻)``）。

        ⚠️ ``chunk is None`` = **结束标记**（这句合成完了）⇒ 播放线程据此
        判定「这句的音频全部写进管道了」⇒ 记账（播完时刻 / 滞后）。

        ⚠️ **带「生产时刻」** —— 播放线程靠它实现**延迟线**
        （见 :meth:`set_reserve`）：把最新的 N 秒**留在 FIFO 里**不写出去
        ⇒ 播放器**故意晚 N 秒**放 ⇒ ① 管线有 N 秒余量（合成抖动不会
        变成断音）② 播放时机可精确对齐（提前量 = 生产延迟 + N）。
        """
        with self._fifo_cond:
            self._fifo.append((job, chunk, time.time()))
            if chunk is not None:
                try:
                    self._fifo_sec += int(chunk.size) / float(_SAMPLE_RATE)
                except Exception:  # noqa: BLE001
                    pass
            self._fifo_cond.notify_all()

    def _fifo_pop(self, timeout: float = 0.2, reserve: float = 0.0):
        """取一块（``(job, chunk, ts)``）；超时返回 ``None``。

        ⚠️ **FIFO 是顺序的** ⇒ 播放顺序 = 合成顺序 = 提交顺序。

        ⚠️ **``reserve`` > 0 ⇒ 延迟线**：队头那块**太新**（生产出来还不到
        ``reserve`` 秒）就**不取**（等它变老）⇒ FIFO 里始终留着最新的
        ``reserve`` 秒不播 ⇒ 播放器故意晚 ``reserve`` 秒放。
        """
        deadline = time.time() + max(0.0, timeout)
        with self._fifo_cond:
            while True:
                if not self._fifo:
                    remain = deadline - time.time()
                    if remain <= 0.0:
                        return None
                    self._fifo_cond.wait(min(remain, 0.05))
                    continue
                if reserve > 0.0:
                    _j, _c, ts = self._fifo[0]
                    age = time.time() - ts
                    if age < reserve:
                        # ⚠️ **太新 ⇒ 留在 FIFO 里**（延迟线）⇒ 等它变老。
                        remain = min(reserve - age, deadline - time.time())
                        if remain <= 0.0:
                            return None
                        self._fifo_cond.wait(min(remain, 0.05))
                        continue
                job, chunk, _ts = self._fifo.popleft()
                if chunk is not None:
                    try:
                        self._fifo_sec -= int(chunk.size) / float(_SAMPLE_RATE)
                    except Exception:  # noqa: BLE001
                        pass
                return job, chunk, _ts

    def _clock_ready(self, job: TTSJob) -> bool:
        """这一句**现在能播吗**（按源话的播放头判断）。

        ⚠️ **时钟卡住 ⇒ 放行**（源不再往前走时不能把译文永久压住）。
        """
        with self._cond:
            provider = self._clock_provider
            offset = self._clock_offset
        if provider is None:
            return True
        if job.src_start_sec is None or job.src_start_sec < 0:
            return True
        try:
            ph = provider()
        except Exception:  # noqa: BLE001
            ph = None
        if ph is None:
            return True
        try:
            ph = float(ph)
        except (TypeError, ValueError):
            return True
        with self._cond:
            if ph > self._clock_last + 0.05:
                self._clock_last = ph
                self._clock_stall_at = time.time()
            stalled = time.time() - self._clock_stall_at
        if stalled > _CLOCK_STALL_SEC:
            # ⚠️ **源不再往前走**（暂停 / 卡住 / 没上报）⇒ 别把译文压住。
            return True
        return ph >= (float(job.src_start_sec) - offset)

    def _fifo_unpop(self, item) -> None:
        """把刚取出来的一块**放回 FIFO 头部**（播放时钟没到 ⇒ 等）。"""
        with self._fifo_cond:
            job, chunk, ts = item
            self._fifo.appendleft((job, chunk, ts))
            if chunk is not None:
                try:
                    self._fifo_sec += int(chunk.size) / float(_SAMPLE_RATE)
                except Exception:  # noqa: BLE001
                    pass
            self._fifo_cond.notify_all()

    def _has_pending_work(self) -> bool:
        """**还有活要干吗？**（还有句子要播 / 正在合成）

        ⚠️ 用来区分「**断音**」（管道空了但还有句子要播 ⇒ 合成跟不上）
        与「**空闲**」（没活了 ⇒ 管道空着是正常的）。

        """
        with self._cond:
            return bool(self._jobs) or self._playing is not None

    def _fifo_clear(self) -> int:
        """清空 FIFO（``clear()`` 用）⇒ 返回清掉的 job 数。"""
        cleared = 0
        with self._fifo_cond:
            while self._fifo:
                job, chunk, _ts = self._fifo.popleft()
                if chunk is not None:
                    try:
                        self._fifo_sec -= int(chunk.size) / float(_SAMPLE_RATE)
                    except Exception:  # noqa: BLE001
                        pass
                if job is not None and not job.cancelled:
                    job.cancelled = True
                    cleared += 1
            self._fifo_sec = 0.0
            self._fifo_cond.notify_all()
        return cleared

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def submit_queued(self, text: str, language: str = "auto", speaker: str = "",
                      ref_speaker: str = "", volume: float = 0.5,
                      tempo: Optional[float] = None,
                      src_end_ts: float = 0.0, src_start_ts: float = 0.0,
                      src_start_sec: float = -1.0, stream_id: str = "",
                      play_to: str = PLAY_BOTH,
                      first_window_frames: int = 0) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        self._check_language(language)

        with self._cond:
            # ⚠️ **``tempo=None`` ⇒ 继承队列当前的变速**（``set_tempo`` 设的
            # 那个值）—— 不是「1.0」。
            #
            # 为什么：``tempo`` 是**队列级**的设置（``/queue/tempo`` 改的
            # 就是它）⇒ 改了之后**新提交的句子也该按新速度播**。
            # 之前新句一律用调用方传的默认值 1.0 ⇒ 「设了 tempo 1.15，
            # 结果新句还是原速播」（实测：journal 里 ``aplay-writer``
            # 没带 ``tempo=1.150``）⇒ **变速对新句不生效**。
            #
            # ⇒ 显式给数值 = 覆盖；给 ``None`` = 跟随队列。
            job_tempo = (self._clamp_tempo(tempo) if tempo is not None
                         else self._tempo)
        job = TTSJob(
            seq=self._next_seq(),
            text=text,
            language=language,
            speaker=speaker,
            ref_speaker=ref_speaker,
            volume=float(volume),
            tempo=job_tempo,
            play_to=play_to,
            stream_id=stream_id,
            first_window_frames=int(first_window_frames or 0),
            src_end_ts=float(src_end_ts or 0.0),
            src_start_ts=float(src_start_ts or 0.0),
            src_start_sec=float(src_start_sec if src_start_sec is not None
                                else -1.0),
            created_at=time.time(),
            est_audio_sec=_estimate_audio_sec(text),
        )
        with self._cond:
            if len(self._jobs) >= QUEUE_MAX:
                # 队满 ⇒ **合并队尾两段**（内容不丢，只是那句更长）。
                tail = self._jobs.pop()
                job.text = tail.text + " " + job.text
                job.est_audio_sec = (tail.est_audio_sec
                                      + _estimate_audio_sec(job.text))
                job.merged = tail.merged + 1
                job.src_end_ts = tail.src_end_ts or job.src_end_ts
                job.src_start_ts = tail.src_start_ts or job.src_start_ts
                self._c.merged += 1
            self._jobs.append(job)
            self._cond.notify()
            depth = len(self._jobs)
        return {
            "seq": job.seq,
            "queued": depth,
            "est_audio_sec": round(job.est_audio_sec, 2),
            "depth": depth,
        }

    def _check_language(self, language: str) -> None:
        """目标语**能不能出声** —— 不能 ⇒ 抛错，让调用方降级为仅字幕。

        ⚠️ ``auto`` 永远可以（模型自己判语种）。
        """
        lang = (language or "auto").strip().lower()
        if not lang or lang == "auto":
            return
        try:
            from wdnapps_audio_chat.cards.tts.service import (
                get_session,
                voice_catalog,
            )
            catalog = voice_catalog(get_session().model_dir)
        except Exception as exc:  # noqa: BLE001
            raise TTSUnavailable("TTS 音色表不可用: %s" % exc) from exc
        langs = {str(x.get("id") or "").lower()
                 for x in (catalog.get("languages") or [])}
        if langs and lang not in langs:
            raise ValueError(
                "目标语 %r 不能出声（本模型可发声: %s）"
                % (language, ", ".join(sorted(langs))))

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def clear(self) -> dict:
        """清空**没开始播**的 job（正在合成的那句跑完）。"""
        cleared = 0
        with self._cond:
            cleared += len(self._jobs)
            self._jobs.clear()
            self._c.dropped += cleared
            self._cond.notify_all()
        # ⚠️ **FIFO 里还没播的音频**也清掉（上一轮的内容不该接着播）。
        fifo_cleared = self._fifo_clear()
        if fifo_cleared:
            with self._cond:
                self._c.dropped += fifo_cleared
                self._inflight = [j for j in self._inflight
                                  if not j.cancelled]
                self._cond.notify_all()
            cleared += fifo_cleared
        # ⚠️ **没人播了 ⇒ 把 aplay 关掉**（ALSA 设备独占 ⇒ 同传停下来
        # 就得让出来，否则别的卡播不出来）。
        with self._cond:
            speaking = self._speaking
        if speaking is None:
            self._close_writer()
        return {"cleared": cleared}

    def _close_writer(self) -> None:
        """关掉复用的 aplay 管道（释放 ALSA 设备）。"""
        with self._cond:
            writer = self._writer
            self._writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("tts-queue: 关 aplay 失败: %s", exc)

    def flush_by_position(self, pos_sec: float, direction: str = "") -> dict:
        """**seek 之后**：把与新位置不匹配的音频全部丢掉。

        ⚠️⚠️ **为什么不能直接用** :meth:`clear`（实测 2026-11-22，用户报
        「含音轨的视频多次拖动进度条，没跟字幕的翻译同声播出来」）：

        * ``clear()`` **不清**正在合成的那句 ⇒ 它的 PCM 之后还会进 FIFO
          ⇒ **陈旧的译文照样播出来**（实测：后跳之后播的是 48~77 s 的
          内容，而播放头在 27~46 s ⇒ 错位 **−20~−31 秒**）；
        * ``clear()`` **不复位播放时钟** ⇒ 后跳之后播放头**变小** ⇒
          播放时钟的「停滞判定」（``ph > _clock_last + 0.05``）**永远
          不成立** ⇒ 8 秒后兜底放行 ⇒ 播的还是旧内容。

        ⇒ 这里补上这两件事。

        ⚠️ **正在播的那句放完**（aplay 管道里的音频没法「撤回」）
        ⇒ 用户会听到**一句**旧内容 ⇒ 但**后面**全是新位置的 ⇒ 可接受。
        """
        pos = float(pos_sec or 0.0)
        res = self.clear()
        with self._cond:
            # ① 正在合成的那句也作废 —— 它的 PCM 进了 FIFO 之后，
            #    播放线程看到 ``job.cancelled`` 会**跳过**（不播）。
            for j in list(self._inflight):
                j.cancelled = True
            self._inflight = []
            self._speaking = None
            # ② **播放时钟复位** —— 后跳之后播放头变小 ⇒ 停滞判定会误触发
            #    ⇒ 复位后按新位置重新算。
            self._clock_last = -1.0
            self._clock_stall_at = time.time()
            self._cond.notify_all()
        LOG.info("tts-queue: seek 冲掉陈旧音频（位置 %.1f s，%s）⇒ 丢掉 %s 段",
                 pos, direction or "-", res.get("cleared"))
        return {"flushed": int(res.get("cleared") or 0), "pos": round(pos, 2),
                "dir": direction}

    def set_clock(self, provider=None, offset: float = 0.0) -> dict:
        """设**播放时钟**（跟播用）。

        ⚠️ ``provider`` = 返回「当前播放头（媒体秒）」的可调用对象；
        ``None`` = 关掉时钟（播放不再等源话）。

        ⚠️ **时钟卡住 ⇒ 自动放弃等待**（:data:`_CLOCK_STALL_SEC`）——
        源不再往前走时不能把译文**永久压住**（否则「有翻译、一声不响」，
        踩过好几次）。
        """
        with self._cond:
            self._clock_provider = provider
            try:
                self._clock_offset = max(-10.0, min(10.0, float(offset)))
            except (TypeError, ValueError):
                self._clock_offset = 0.0
            self._clock_last = -1.0
            self._clock_stall_at = time.time()
        with self._fifo_cond:
            self._fifo_cond.notify_all()
        return {"clocked": provider is not None,
                "offset": round(self._clock_offset, 2)}

    def set_reserve(self, seconds: float) -> dict:
        """设**延迟线的留量**（FIFO 里留多少秒不播）。

        ⚠️ 0 = 不留（合成出来就播）⇒ **跟播模式要 > 0**（否则管线没余量
        ⇒ 合成一抖就断音）。
        """
        try:
            v = float(seconds)
        except (TypeError, ValueError):
            v = 0.0
        v = max(0.0, min(10.0, v))
        with self._cond:
            self._reserve = v
        with self._fifo_cond:
            self._fifo_cond.notify_all()
        return {"reserve": round(v, 2)}

    def set_tempo(self, tempo: float) -> dict:
        """改**未开始** job 的变速（积压一降就自动回落 1.0）。"""
        t = self._clamp_tempo(tempo)
        with self._cond:
            self._tempo = t
            for job in self._jobs:
                job.tempo = t
            applied = len(self._jobs)
        return {"tempo": t, "applied": applied}

    @staticmethod
    def _clamp_tempo(tempo: float) -> float:
        try:
            t = float(tempo)
        except (TypeError, ValueError):
            return TEMPO_MIN
        if t < TEMPO_MIN:
            return TEMPO_MIN
        if t > TEMPO_MAX:
            return TEMPO_MAX
        return round(t, 3)

    def auto_tempo(self, queued_audio_sec: float) -> float:
        """按积压秒数选档位（只在产能不足时提速）。"""
        for threshold, tempo in TEMPO_STEPS:
            if queued_audio_sec > threshold:
                return tempo
        return TEMPO_MIN

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._cond:
            queued_sec = sum(j.est_audio_sec for j in self._jobs)
            # 在途的（已合成、等播 / 正在播）也算积压 —— 但**正在播的那句**
            # 已经在放出来了，不算积压。
            queued_sec += sum(j.est_audio_sec for j in self._inflight
                              if j is not self._speaking)
            depth = len(self._jobs)
            inflight = len(self._inflight)
            playing = self._playing
            speaking = self._speaking
            c = self._c
            tempo = self._tempo
            lags = list(c.lags)
        avg_lag = (c.lag_sum_sec / c.done) if c.done else 0.0
        busy = playing is not None or speaking is not None
        shown = speaking or playing
        return {
            "depth": depth,
            "inflight": inflight,
            "queued_audio_sec": round(queued_sec, 2),
            "playing": busy,
            "playing_seq": shown.seq if shown is not None else None,
            "playing_text": (shown.text[:40] if shown is not None else ""),
            "last_lag_sec": round(c.last_lag_sec, 2),
            # ⚠️ **可听滞后**（译文出声 − 源话开口）—— **用户真正感受到的
            # 那一口**（``last_lag_sec`` 是「播完 − 源话说完」，口径不同）。
            "last_a_lag": round(c.last_a_lag, 2),
            "avg_lag_sec": round(avg_lag, 2),
            "merged": c.merged,
            "dropped": c.dropped,
            "failed": c.failed,
            "done": c.done,
            "tempo": tempo,
            "max_tempo": TEMPO_MAX,
            # ⚠️ **实测合成速率**（音频秒 / 墙钟秒）—— <1.0 「产能不够」
            # ⇒ 变速必须 ≥ 1/速率（见 ``_note_rate``）。
            "rate": round(self._rate, 3),
            # ⚠️ **可持续速率**（含首窗与 prefill）—— 变速上限按它算。
            "sustain_rate": round(self._sustain_rate, 3),
            # ⚠️ **本机跑着几个 TTS 实例**（= 并行合成几句 ⇒ 聚合产能）。
            "tts_instances": self._tts_instance_count(),
            # ⚠️ **变速够不够**（1/速率）—— 调用方（传引擎）拿它定变速。
            # ⚠️ **取阶梯上的档**（离散）—— 变速变了要重开 aplay 管道 ⇒
            # 连续变速会频繁重开 ⇒ 频繁句间缝隙。
            "need_tempo": self._need_tempo_locked(),
            # **电平探针**（诊断「没播」vs「播得太小」）
            "last_peak": round(c.last_peak, 4),
            "last_raw_peak": round(c.last_raw_peak, 4),
            # **分段计时**（诊断滞后明细）
            "timings": list(self._played_timings),
        }

    def _need_tempo_locked(self) -> float:
        """按**实测速率**算出「最多能播多快」—— 取**阶梯**上的档。

        ⚠️ **为什么上限是速率 R，而不是 1/R**（实测 2026-11-22，纠正
        上一版的错误推理）：

        sox 的 tempo 效果在**播放器上游** ⇒ 变速越大，播放器从合成
        流里**拉得越快**（每墙钟秒消费 ``tempo`` 秒的变速前音频）⇒

        * 播放速度 = ``min(tempo, R)``（R = 边际生产速率）；
        * 译文相对源话的前进速度 = ``min(tempo, R) ÷ 译文长度比``。

        ⇒ **tempo > R 不会让译文更快**（受产能限制），只会**更快地吃空
        缓冲** ⇒ 断音更多、滞后更多。

        ⇒ 所以上限是 **R**（不能超过生产速度）。

        ⚠️⚠️ **R 必须是「可持续」产能**（实测 2026-11-23）：
        单卡的可持续速率只有 **~0.95×**（talker 自回归 ⇒ ~1.0× 实时，
        再扣掉每句的首窗与 prefill）⇒ 上限就是 1.0 ⇒ 变速永远起不来
        ⇒ 译文比源话长 k 倍 ⇒ 越播越落后。
        ⇒ **变速上限 = 单卡可持续速率**（不给它加速的机会 ⇒ 宁可滞后，
        也不要断音）。

        ⚠️ **用「整体速率」而不是「边际速率」**：边际速率把首窗延迟排除
        了 ⇒ 系统性高估 ⇒ 变速会超过真实产能 ⇒ 缓冲被吃空 ⇒ 断音。
        """
        if self._rate <= 0.0:
            return TEMPO_MIN
        capacity = self._sustain_rate
        for t in reversed(TEMPO_LADDER):
            if t <= capacity + 1e-6:
                return round(t, 3)
        return round(TEMPO_MIN, 3)

    def _tts_instance_count(self) -> int:
        """本机跑着几个 TTS 实例。

        ⚠️ **本板恒为 1**（npu2 是禁区 ⇒ 只有一个 TTS 卡位）⇒ 聚合产能
        = 单卡产能。这个字段只用于**面板显示**，不参与产能计算。
        """
        return 1

    def lags(self) -> list:
        with self._cond:
            return list(self._c.lags)

    def played_texts(self) -> List[str]:
        """已经播完的句子（按播放顺序）—— 回归脚本断言「顺序不乱」用。"""
        with self._cond:
            return list(self._played)

    # ------------------------------------------------------------------ #
    # 消费 —— 段一：合成
    # ------------------------------------------------------------------ #
    def _next_seq(self) -> int:
        with self._cond:
            self._c.seq += 1
            return self._c.seq

    def _infer_loop(self) -> None:
        """**合成线程** —— 一次一句，串行。

        每合成完一句就把它交给播放线程 ⇒ 播放线程可以在**第一块 PCM
        到达时**就出声，同时下一句已经开始合成 ⇒ 吞吐 ≈ 1× 实时。

        ⚠️ **只有一条合成线程**（2026-11-23 回滚说明）：曾试过「同一个模型
        跑在两张卡上、队列把句子分片」⇒ 产能确实翻倍、播报时长与断音都
        改善了；但**第三张 NPU 卡是禁区**（用户 2026-11-23 明确要求不得
        占用）⇒ 本板只有 npu1 一张发声卡 ⇒ **产能上限 = 模型速度
        （~1.0× 实时）**，这是物理限制，只能从「内容更短 / 少等一会儿」入手。
        """
        while True:
            with self._cond:
                while not self._jobs and not self._stop:
                    self._cond.wait(timeout=1.0)
                if self._stop:
                    return
                job = self._jobs.pop(0)
                self._playing = job
                self._inflight.append(job)
                job.started_at = time.time()
            job.stream = _JobStream()
            try:
                self._synthesize(job)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("tts-queue: 合成失败: %s", exc)
                job.cancelled = True
                with self._cond:
                    self._c.failed += 1
            finally:
                if job.stream is not None:
                    job.stream.finish()
                    # ⚠️ **实际收到的音频时长**（变速前）—— 用来算**合成
                    # 速率**（音频秒 / 墙钟秒）。
                    #
                    # ⚠️ **必须在这里取**（不能等到播放线程取）—— 播放线程
                    # 是**另一个线程**，等它算完再记 ⇒ 这里读到的是 0 ⇒
                    # 速率永远测不出来（实测 rate 一直 0.0）。
                    try:
                        job.audio_sec = float(job.stream.audio_sec or 0.0)
                    except Exception:  # noqa: BLE001
                        pass
                    # ⚠️ **边际速率**（首窗之后的产量）—— 「产能」的真值。
                    try:
                        job.marginal_rate = float(
                            job.stream.marginal_rate() or 0.0)
                    except Exception:  # noqa: BLE001
                        pass
                # ⚠️ **结束标记** —— 播放线程据此判定「这句的音频全部写进
                # 管道了」⇒ 记账（播完时刻 / 滞后 / 分段计时）。
                self._fifo_push(job, None)
                self._note_rate(job)
                with self._cond:
                    self._playing = None
                    self._cond.notify_all()

    def _note_rate(self, job: TTSJob) -> None:
        """记一句的**合成速率**（音频秒 / 墙钟秒），做 EMA。

        ⚠️ **用「边际速率」**（首窗之后的增量）—— 「总音频 ÷ 总时间」
        把**首窗延迟**（2~3 s 不出声）摊薄了 ⇒ 短句的速率被严重低估
        ⇒ 「产能不够」的结论是假的（实测：整体 0.947× 但边际 1.05×）。
        ⇒ **产能**看边际，**首声延迟**看整体，两个口径分开记。
        """
        audio = float(getattr(job, "audio_sec", 0.0) or 0.0)
        if not job.started_at:
            return
        wall = time.time() - job.started_at
        overall = (audio / wall) if wall > 0.2 else 0.0
        job.rate_overall = round(overall, 3)
        r = float(getattr(job, "marginal_rate", 0.0) or 0.0)
        if r <= 0.0:
            r = overall
        if r <= 0.0:
            return
        with self._cond:
            if self._rate <= 0.0:
                self._rate = r
            else:
                # EMA（α=0.4）—— 对单句抖动不敏感，但能跟上模型/负载变化。
                self._rate = 0.6 * self._rate + 0.4 * r
            # ⚠️ **可持续速率**（整体速率的 EMA）—— **变速上限用这个**
            # （实测 2026-11-23）：边际速率把「首窗 + prefill」排除掉了
            # ⇒ 系统性高估 ⇒ 变速超过真实产能 ⇒ 缓冲被吃空 ⇒ 断音。
            if overall > 0.0:
                if self._sustain_rate <= 0.0:
                    self._sustain_rate = overall
                else:
                    self._sustain_rate = 0.6 * self._sustain_rate + 0.4 * overall

    def _synthesize(self, job: TTSJob) -> None:
        """合成一句（重试 3 次）。

        ⚠️ **只在「一个 PCM 都没送到」时才重试** —— 已经播出去了半句再
        重试 ⇒ 观众会听到「半句 + 整句」⇒ 比失败还糟。

        """
        from wdnapps_audio_chat.cards.tts.service import get_session

        last_err = None
        for attempt in range(3):
            sess = get_session()
            if not sess.available:
                raise RuntimeError("TTS 不可用: %s" % sess.init_error)
            try:
                # ⚠️ **记下第一块 PCM 到达的时刻**（合成首窗延迟的度量）。
                #
                # ⚠️ **PCM 直接进共享 FIFO**（不是「一句一缓冲」）——
                # 播放线程从 FIFO 连续取 ⇒ 下一句的音频在上一句还在播的
                # 时候就已经在 FIFO 里 ⇒ **句间不断音**。
                push = job.stream.push
                fifo_push = self._fifo_push

                def _sink(chunk, sr, _push=push, _job=job, _fp=fifo_push):
                    if not _job.synth_first_pcm_at:
                        _job.synth_first_pcm_at = time.time()
                    # ⚠️ **裁掉这段开头的那截静音**（实测 2026-11-23，M29）——
                    # 模型每段开头带 0.35~0.65 s 静音 ⇒ 声音要等它走完才出来
                    # ⇒ **可听滞后直接多这么多** ⇒ 裁掉（内容一个字不少）。
                    if not _job.head_trimmed:
                        trimmed = trim_head_silence(chunk, sr)
                        if trimmed is not None:
                            chunk = trimmed
                            _job.head_trimmed = True
                    _push(chunk, sr)      # 记账（时长 / 产量标记）
                    _fp(_job, chunk)      # 真正的音频通道

                sess.run_queued(job, sink=_sink)
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if job.stream is not None and job.stream.has_data:
                    LOG.warning("tts-queue: 已播半句，放弃重试: %s", exc)
                    break
                LOG.warning("tts-queue: 合成失败（第 %d 次）: %s",
                            attempt + 1, exc)
                time.sleep(1.0)
        if last_err is not None:
            raise last_err

    # ------------------------------------------------------------------ #
    # 消费 —— 段二：播放
    # ------------------------------------------------------------------ #
    def _play_loop(self) -> None:
        """**播放线程** —— 从**共享 FIFO** 取 PCM，连续喂进 aplay 管道。

        ⚠️ **两个结构性的修复**（实测 2026-11-22，用户报「播放中途会有
        卡顿断音」）：

        ① **aplay 进程跨句复用** —— 之前**每句**都新建 + 关闭一个
           ``AplayWriter`` ⇒ 每句都要 ``amixer`` un-mute（fork+exec）→
           起 aplay/sox → **探活 0.12 s** ⇒ **句间静音 0.13~0.28 秒**
           ⇒ 句子 2~5 s ⇒ **每 5~10 秒就有一次「断音」**。
           ⇒ 现在**一个 writer 连续用** ⇒ 句间零缝隙。

        ② **共享 FIFO**（不是「一句一缓冲」）—— 之前播放线程**要等这句
           的合成结束**才去取下一句 ⇒ 下一句的首窗（~2 s）里**管道是
           空的** ⇒ **每句之间又断一次**（实测句间间隔 0.28~0.85 s）。
           ⇒ 现在合成线程把 PCM **直接推进共享 FIFO** ⇒ 下一句的音频在
           上一句还在播的时候就已经在 FIFO 里等着 ⇒ 句间零缝隙。

        ⚠️ **变速是 spawn 时定死的**（sox 管道）⇒ 变速变了才重开 writer。
        ⚠️ **空闲超过 :data:`_WRAPPER_IDLE_CLOSE_SEC` 就关掉** ——
        否则同传一停下来就把 ALSA 设备**一直占着**（设备是独占的），
        别的卡（TTS 卡自己的页面）就播不出来。
        """
        writer = None
        writer_tempo = None
        cur_job = None          # 最后写入的 job（归因 / 电平探针）
        idle_since = time.time()
        while True:
            item = self._fifo_pop(timeout=0.2, reserve=self._reserve)
            if item is None:
                # 空闲 ⇒ 攒够 :data:`_WRAPPER_IDLE_CLOSE_SEC` 就放开设备
                if (writer is not None
                        and time.time() - idle_since >= _WRAPPER_IDLE_CLOSE_SEC):
                    try:
                        writer.close()
                    except Exception as exc:  # noqa: BLE001
                        LOG.debug("tts-queue: 空闲关闭 aplay 失败: %s", exc)
                    writer = None
                    writer_tempo = None
                    self._dry_since = 0.0
                    with self._cond:
                        self._writer = None
                # ⚠️ **管道空了 = 断音**（aplay 正在等下一块）
                #
                # ⚠️ **只有「还有活要干」时才算断音** —— 两轮同传之间的
                # 空闲期管道本来就是空的（那是**没活**，不是**断音**）。
                # ⇒ 否则空闲 12 s 会被记成「句间间隔 12.6 s」（实测）。
                if writer is not None and self._has_pending_work():
                    try:
                        empty = float(writer.backlog_sec()) <= 0.02
                    except Exception:  # noqa: BLE001
                        empty = False
                    if empty:
                        if not self._dry_since:
                            # ⚠️ **第一次观测到「管道空了」** ⇒ 记下时刻
                            # ⇒ 断音时长可以**精确**算出来（而不是被
                            # 轮询粒度 0.2 s 糊掉）。
                            self._dry_since = time.time()
                        if cur_job is None:
                            # 句子之间 ⇒ 记成**句间间隔**
                            pass
                        else:
                            cur_job.starved_sec += 0.2
                continue
            job, chunk, _ts = item
            idle_since = time.time()
            if job is None:
                return
            # ⚠️⚠️ **先判「已取消」，再判播放时钟**（顺序反了会卡死整条
            # FIFO —— 实测 2026-11-22，用户报「拖了进度条之后没跟字幕同声
            # 播出来」）：
            #
            # * 被 seek 作废的那句（``job.cancelled``）**还会继续吐 PCM**
            #   （合成线程已经在跑了，停不掉）⇒ 它的分片会**继续进 FIFO**；
            # * 它的位置在**未来**（比如 47 s，而播放头在 20 s）⇒ 时钟说
            #   「还没到」⇒ 分片被**放回 FIFO 头部** ⇒ **整条 FIFO 被它
            #   堵住**（FIFO 是顺序的 ⇒ 队头不动 ⇒ 后面全不动）；
            # * ⇒ 播放头一路走到 47 s 才放开 ⇒ 这 30 秒里**一句都没播**
            #   ⇒ 放开时整队一起进管道 ⇒ **一串译文一起炸出来**
            #   （实测 6 句的 ``t_play`` 完全相同）。
            #
            # ⇒ **取消的 job 直接丢弃，不看时钟** —— 它本来就不该播。
            if job.cancelled:
                with self._cond:
                    if job in self._inflight:
                        self._inflight.remove(job)
                    self._cond.notify_all()
                continue
            # ⚠️ **播放时钟**（跟播）：这一句**还没轮到播** ⇒ 放回
            # FIFO 头部等（**不丢**）⇒ 等源话的播放头走到那一句。
            #
            # ⚠️⚠️ **必须睡一下**（实测踩过）：不放回的话这里就是
            # **空转** ⇒ 播放线程 100% CPU ⇒ **抢 ASR/MT 的算力**
            # ⇒ 整条链路变慢。⇒ 50 ms 一次的轮询足够（播放头也是
            # 250 ms 上报一次）。
            if chunk is not None and not self._clock_ready(job):
                self._fifo_unpop(item)
                time.sleep(0.05)
                continue
            if chunk is None:
                # ⚠️ **结束标记** —— 这句的音频全部写进管道了 ⇒ 记账。
                self._finish_job(job, writer)
                cur_job = None
                with self._cond:
                    if self._speaking is job:
                        self._speaking = None
                    if job in self._inflight:
                        self._inflight.remove(job)
                    self._cond.notify_all()
                continue
            # ⚠️ **变速变了 ⇒ aplay 管道得重开**（sox 的 tempo 是 spawn
            # 时定死的）。
            if writer is not None and writer_tempo != job.tempo:
                try:
                    writer.close()
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("tts-queue: 变速变化，重开 aplay 失败: %s", exc)
                writer = None
                writer_tempo = None
            try:
                if writer is None:
                    # ⚠️ **第一块 PCM 到了才起 aplay** —— 起早了也是白等
                    # （aplay 阻塞在 stdin 上，不会自己出声）。
                    from wdnapps_audio_chat.cards.tts.service import (
                        AplayWriter,
                    )
                    writer = AplayWriter(volume=job.volume, tempo=job.tempo)
                    if not getattr(writer, "ok", True):
                        raise RuntimeError("aplay 启动失败")
                    writer_tempo = job.tempo
                    with self._cond:
                        self._writer = writer
                else:
                    # ⚠️ **音量可以逐句改**（音量是写 PCM 时乘的，不在进程里）
                    try:
                        writer.set_volume(job.volume)
                    except Exception:  # noqa: BLE001
                        pass
                # ⚠️ **电平探针逐句复位**（writer 跨句复用 ⇒ 峰值会串到
                # 下一句 ⇒ 面板会显示上一句的音量）。
                if cur_job is not job:
                    try:
                        writer.reset_peaks()
                    except Exception:  # noqa: BLE001
                        pass
                # ⚠️ **记下真正出声的时刻**（写进 aplay 的第一块）
                if not job.play_started_at:
                    job.play_started_at = time.time()
                    # ⚠️ **开口时管道里的存货**（秒）—— 缓冲够不够的直接
                    # 证据（0 ⇒ 开口即断音）。
                    try:
                        job.pipe_stock_at_start = float(
                            writer.backlog_sec())
                    except Exception:  # noqa: BLE001
                        job.pipe_stock_at_start = 0.0
                if self._dry_since:
                    # ⚠️ **结算这次的断音时长**（精确到毫秒）。
                    dry = time.time() - self._dry_since
                    self._dry_since = 0.0
                    if cur_job is None:
                        self._gap_pending += dry
                    else:
                        cur_job.starved_sec += dry
                writer.write(chunk)
                cur_job = job
                with self._cond:
                    self._speaking = job
                    self._cond.notify_all()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("tts-queue: 播放失败: %s", exc)
                if writer is not None and not getattr(writer, "ok", True):
                    writer = None
                with self._cond:
                    self._c.failed += 1

    def _finish_job(self, job: TTSJob, writer) -> None:
        """这句的音频**全部写进管道**了 ⇒ 记账（播完时刻 / 滞后 / 时序）。

        ⚠️ **「播完时刻」** —— writer 跨句复用 ⇒ 不能靠 ``close()``
        （那会把整条管道关掉）⇒ 用 writer 的**消耗模型**估算
        （「已写入 − 已消费」= 这句还没放完的部分）。
        """
        _pe = 0.0
        if writer is not None:
            try:
                _pe = float(writer.playback_end() or 0.0)
            except Exception:  # noqa: BLE001
                _pe = 0.0
        job.finished_at = _pe or time.time()
        # ⚠️ **可听滞后** = 这句译文**出声**的时刻 − 源话**开口**的时刻
        # （出声 = 写进管道 + 管道里前面那点存货放完）。
        if job.play_started_at and job.src_start_ts:
            job.a_lag = (
                job.play_started_at
                + job.pipe_stock_at_start / max(float(job.tempo or 1.0), 0.01)
                - job.src_start_ts)
        # 滞后 = 播完时刻 − 源话说完时刻（实时流口径）。
        if job.src_end_ts:
            job.lag_sec = max(0.0, job.finished_at - job.src_end_ts)
        # ⚠️ **句间间隔** —— 这句出声之前，管道空着等了多久。
        # ⚠️ **这才是「断音」的度量**（> 0.1 s ⇒ 听得出来）。
        gap = self._gap_pending
        self._gap_pending = 0.0
        prev_end = self._last_finished_at
        self._last_finished_at = job.finished_at
        with self._cond:
            self._c.done += 1
            self._c.last_lag_sec = job.lag_sec
            self._c.last_a_lag = float(getattr(job, "a_lag", 0.0) or 0.0)
            self._c.last_peak = float(job.peak or 0.0)
            self._c.last_raw_peak = float(job.raw_peak or 0.0)
            self._played.append(job.text)
            # ⚠️ **分段计时**（诊断同传滞后明细）：这几句分别卡在哪一段
            self._played_timings.append({
                "seq": job.seq,
                "text": job.text[:40],
                "t_submit": round(job.created_at, 3),
                "t_synth_start": round(job.started_at, 3),
                "t_first_pcm": round(job.synth_first_pcm_at, 3),
                "t_play": round(job.play_started_at, 3),
                "t_end": round(job.finished_at, 3),
                "lag": round(job.lag_sec, 2),
                # ⚠️ **这一句用的变速**（自动变速有没有真的生效）
                "tempo": round(float(job.tempo or 1.0), 3),
                # ⚠️ **入队时的积压估计**（秒）—— 用来判断
                # 「是产能不足导致积压，还是单纯的合成延迟」。
                "backlog": round(job.est_audio_sec, 2),
                # ⚠️ **管线延迟**（入队 → 出声）—— 跟播的**提前量**要按
                # 它来放行，否则译文永远比源话晚这么多。
                "pipe_lat": round(job.play_started_at - job.created_at, 2)
                if job.play_started_at else 0.0,
                # ⚠️ **生产延迟**（开始合成 → 出声）—— **不含排队等待**。
                # ⚠️ **跟播提前量必须用这个**（不能用 ``pipe_lat``）——
                # ``pipe_lat`` 含「在队列里等了多久」，而那正是**提前量**
                # 造成的 ⇒ 提前量越大 ⇒ 排队越久 ⇒ ``pipe_lat`` 越大 ⇒
                # 提前量再变大 ⇒ **正反馈跑飞**（实测：提前量一路涨到上限
                # 6 s ⇒ 译文比源话晚 5 s）。而「生产延迟」只跟 TTS 本身
                # 有关（首窗 ~2 s）⇒ **跟队列深度无关** ⇒ 稳定。
                "synth_lat": round(job.play_started_at - job.started_at, 2)
                if (job.play_started_at and job.started_at) else 0.0,
                # ⚠️ **饿死了多久**（管道空着等合成）—— 「卡顿」的度量。
                "starved": round(job.starved_sec, 2),
                # ⚠️ **合成的音频时长**（变速前）—— 用来算**合成速率**。
                "audio_sec": round(job.audio_sec, 2),
                # ⚠️ **边际生产速率**（首窗之后的产量）—— 「产能」真值。
                "m_rate": round(float(job.marginal_rate or 0.0), 3),
                # ⚠️ **整体速率**（含首窗延迟）—— 只作诊断。
                "o_rate": round(float(job.rate_overall or 0.0), 3),
                # ⚠️ **句间间隔**（管道空着等下一句）—— 断音的度量。
                "gap": round(gap, 3),
                # ⚠️ **上一句播完 → 这一句出声**（墙钟距离；负 = 连着）
                "prev_end": round(prev_end, 3),
                # ⚠️ **开口时管道里的存货**（秒）—— 缓冲够不够的直接证据。
                "stock": round(job.pipe_stock_at_start, 2),
                # ⚠️ **可听滞后**（秒）—— **用户真正感受到的那一口**：
                # 「这句译文**出声**的时刻 − 源话**开口**的时刻」。
                # > 0 ⇒ 译文比源话晚这么多秒（= 用户抱怨的「滞后」）。
                # ⚠️ 写出第一块不等于出声 —— 管道里还压着前面的音频 ⇒
                # 要加上「存货 / 变速」这段时间。
                "a_lag": round(
                    (job.play_started_at
                     + job.pipe_stock_at_start / max(float(job.tempo or 1.0), 0.01)
                     - job.src_start_ts), 2)
                if (job.play_started_at and job.src_start_ts) else 0.0,
                # ⚠️ **源话开口 / 说完**（wall clock）—— 复算用。
                "src_start_ts": round(job.src_start_ts, 3),
                "src_end_ts": round(job.src_end_ts, 3),
                # ⚠️ **源话在媒体时间轴上的位置**（秒）—— seek 场景的
                # **同步度量**要用它：「这句译文出声时，源话的播放头在
                # 哪儿」⇒ 两者一比就知道**跟没跟上**（用户报「拖了进度条
                # 之后没跟字幕同声播」）。
                "src_start_sec": round(float(job.src_start_sec or -1.0), 2),
            })
            if len(self._played_timings) > 60:
                del self._played_timings[:-60]
            if len(self._played) > 200:
                self._played.pop(0)
            if job.lag_sec:
                self._c.lag_sum_sec += job.lag_sec
                self._c.lags.append(round(job.lag_sec, 2))
                if len(self._c.lags) > 200:
                    self._c.lags.pop(0)
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # 停止（进程退出 / 单测用）
    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        with self._cond:
            self._stop = True
            self._jobs.clear()
            self._cond.notify_all()
        # ⚠️ **唤醒播放线程**（它在 FIFO 上等）—— 用一个空标记。
        self._fifo_push(None, None)
        # ⚠️ **关掉复用的 aplay**（ALSA 设备独占 ⇒ 进程退出要还回去）。
        self._close_writer()


_QUEUE: Optional[TTSQueue] = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> TTSQueue:
    """队列单例（懒加载）。"""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = TTSQueue()
        return _QUEUE


# --------------------------------------------------------------------------- #
# 宿主桥（``app.core.audio.tts_bridge``）调用的薄封装
# --------------------------------------------------------------------------- #

def submit_queued(text: str, language: str = "auto", speaker: str = "",
                  ref_speaker: str = "", volume: float = 0.5,
                  tempo: Optional[float] = None,
                  src_end_ts: float = 0.0, src_start_ts: float = 0.0,
                  src_start_sec: float = -1.0, stream_id: str = "",
                  play_to: str = PLAY_BOTH,
                  first_window_frames: int = 0) -> dict:
    """⚠️ ``tempo=None``（默认）⇒ **继承队列当前的变速**；给数值 = 覆盖。"""
    return get_queue().submit_queued(
        text=text, language=language, speaker=speaker, ref_speaker=ref_speaker,
        volume=volume,
        tempo=tempo, src_end_ts=src_end_ts, src_start_ts=src_start_ts,
        src_start_sec=src_start_sec, stream_id=stream_id,
        play_to=play_to, first_window_frames=first_window_frames)


def snapshot() -> dict:
    return get_queue().snapshot()


def clear() -> dict:
    return get_queue().clear()


def flush_by_position(pos_sec: float, direction: str = "") -> dict:
    """**seek 之后**冲掉陈旧音频 —— 见 :meth:`TTSQueue.flush_by_position`。"""
    return get_queue().flush_by_position(pos_sec, direction)


def set_tempo(tempo: float) -> dict:
    return get_queue().set_tempo(tempo)


def set_clock(provider=None, offset: float = 0.0) -> dict:
    """设**播放时钟**（跟播用）—— 见宿主桥同名函数。"""
    return get_queue().set_clock(provider, offset)


def set_reserve(seconds: float) -> dict:
    """设**延迟线留量**（FIFO 里留多少秒不播）—— 见宿主桥同名函数。"""
    return get_queue().set_reserve(seconds)


def shutdown() -> None:
    """停队列线程（进程退出 / 单测用）。"""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            _QUEUE.shutdown()
            _QUEUE = None
