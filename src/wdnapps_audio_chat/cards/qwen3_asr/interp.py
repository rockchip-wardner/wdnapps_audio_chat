"""**同声传译引擎** —— 流式语音识别卡里的一个模式（2026-11-21）。

它是什么
--------
不是新卡片，而是 ASR 卡里的一个「模式/开关」，与「实时翻译」并列：
ASR 出句 → 翻译 → 播报。三段分别落在**卡 0**（ASR + MT）和**卡 1**
（单实例 TTS）。

输入源统一
----------
卡片有 4 个来源 tab，它们的切句信号 / 文本语义 / 播放头三者都不同 ⇒
本引擎只认一种东西：:class:`InterpSegment`（一句源文 + 它在时间轴上的
位置）。四个源各自负责把自己的原生信号**翻译成** ``InterpSegment``。

============  ==================  ==========  ==============
源             切句信号             文本会回滚   播放头
============  ==================  ==========  ==============
file          标点断句             ❌          ❌（要新建）
video         标点断句             ❌          ✅
board         endpoint(静音 1200ms) ✅          ❌
browser       endpoint             ✅          ❌
============  ==================  ==========  ==============

两条正交的轴
------------
* **切句档位** ``fast`` / ``steady``
  - 稳档：实时流用 endpoint（静音 1200 ms）；文件流用标点断句。
  - 快档：实时流用「最长公共前缀连续 N 轮不变」；文件流用**更短的强制
    切句**（24 字而不是 60 字 ⇒ 更早出句）。
* **播放时机** ``now``（不等播放）/ ``follow``（跟播放头）
  - 只有 file / video 有播放头 ⇒ board / browser 恒为 ``now``。

不丢内容
--------
| 场景              | 动作                                        |
|==================|============================================|
| 翻译失败          | 显示原文 + 标「未翻译」（**不算丢**）        |
| 播前复核不通过    | **作废重译**（不是丢弃）⇒ 计 ``retried``    |
| 重试 3 次仍不一致 | 放弃该段 + UI 标注 ⇒ 计 ``abandoned``       |
| 用户 seek 跳过    | 丢弃待播句（**用户自己跳过的**）⇒ ``skipped_seek`` |
| TTS 队列满        | **合并队尾两段**（在 TTS 卡那边做，不丢）      |

播前复核只在**实时流**有意义：翻译是**一次性定稿**的，真正的风险是
「译文回来前源文被改写」⇒ 不一致就重译。文件流的 ``committed`` 不回滚
⇒ 复核几乎必过。
"""
from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 快档：LCP 连续 N 轮不变才成句（实时流）。
INTERP_STABLE_ROUNDS = 2
#: 低于这个字数不切句（太碎的句子翻不准）。
INTERP_MIN_CHARS = 8
#: 单句上限（实时流快档）。
INTERP_MAX_CHARS = 40
#: **文件流快档**的强制切句长度（比稳档的 60 短 ⇒ 更早出句 ⇒ 跟播滞后更小）。
INTERP_FAST_MAX_CHARS = 24

#: 跟播：提前放行量（秒）—— 给 TTS 留出首帧时间。
FOLLOW_LEAD_SEC = 1.0

#: **跟播提前量的下限 / 上限**（秒）—— 提前量 = 实测的管线延迟
#: （入队 → 出声），夹在这个区间里。
#:
#: ⚠️ **为什么不是写死的 1.0 s**（实测 2026-11-22，用户报「播放还是滞后
#: 很多」）：放行之后 TTS 还要 **~3 s**（管线延迟）才出声 ⇒ 提前量 1.0 s
#: ⇒ **译文永远比源话晚 ~2 s**；译文又比源话长 16% ⇒ 越播越落后。
#: ⇒ 提前量改成「跟着实测管线延迟走」⇒ 译文与源话**同时**响。
FOLLOW_LEAD_MIN = 1.5
FOLLOW_LEAD_MAX = 6.0
#: 播放头回退多少秒算「用户 seek 了」。
SEEK_BACK_TOLERANCE = 2.0
#: 播放头**前跳**超过「两次上报之间的真实时长 + 这个容差」⇒ 算 seek。
#:
#: ⚠️ **判据是「比真实时长多跳了多少」，不是「跳了多少」**（实测
#: 2026-11-22，用户报「含音轨的视频多次拖动进度条，没跟字幕同声播」）：
#:
#: * 两次上报之间真实过去 ``dt`` 秒 ⇒ 播放头最多前进 ``dt`` 秒
#:   ⇒ ``now − prev > dt + 容差`` 才是 seek；
#: * 媒体**卡住 / 缓冲**时播放头会冻住，解冻后一次补上
#:   （``now − prev`` 可能好几秒）⇒ 那**不是** seek ⇒ 不能按绝对值判；
#: * 用户小幅前跳（拖一下 3 秒）也会被误判 ⇒ 误判会把**正在播的句子
#:   冲掉** ⇒ 容差不能太小。
SEEK_FWD_TOLERANCE = 1.5
#: seek 位置比较的容差（秒）—— 句子跟新位置差这么点算「在新位置里」。
SEEK_EPS_SEC = 0.25
#: 同传引擎的**重播缓存**上限（句）—— seek 之后要把「新位置之后」的
#: 句子重新武装（重新排队），缓存太大会占内存。
SEEK_REPLAY_CACHE_MAX = 240
#: 重播缓存**去重**的容差（秒）—— 同一处音频被重新识别出来时，时间戳
#: 会有零点几秒的偏差 ⇒ 起点差这么点算「同一处」⇒ 只留最新的一条
#: （否则 seek 重播时同一句话会播 N 遍，实测 3 遍）。
SEEK_DUP_SEC = 0.75
#: 跟播**兜底放行**（秒）—— 播放头一直不动 ⇒ 别把句子无限期压着。
#:
#: ⚠️ **为什么需要它**（实测踩过，用户报「有翻译，但是没有播放」）：
#: 浏览器按自动播放策略**拒绝** ``el.play()`` ⇒ 媒体一直暂停 ⇒ 播放头
#: 永远 0 ⇒ 「跟播放」把**所有**句子**永久压住** ⇒ 用户看到译文却
#: **一声不响**，面板上也看不出为什么。
#:
#: ⇒ 播放头**从没真正走过**（自动播放被拦的指纹）且**卡住**超过这个秒数
#: ⇒ 放行（状态里会标出来 ⇒ 前端提示「媒体没在放」）。
FOLLOW_STALL_SEC = 10.0
#: 播放头小于这个值 ⇒ 视为「媒体**还没真正开始播**」（自动播放被拦 /
#: 用户还没按播放）⇒ 与「播过再暂停」区分开。
FOLLOW_NEVER_PLAYED_SEC = 0.25
#: 播放头卡住多久就在状态里标「卡住」（前端提示用；比兜底早）。
FOLLOW_STALL_WARN_SEC = 3.0

#: **跟播模式的「合成余量」**（秒）—— 提前量 = 生产延迟 + 余量。
#:
#: ⚠️ **有了播放时钟之后，提前量不用再很大** —— 播放由**源话的播放头**
#: 驱动 ⇒ 提前量只负责「让合成在轮到播之前完成」⇒ 余量 1 s 就够
#: （之前提前量顶到 6 s ⇒ 整个 15 s 的文件被一次性放行 ⇒ 放行器等于
#: 没管 ⇒ 队列串行 ⇒ 滞后按产能缺口一路涨）。
FOLLOW_SYNTH_MARGIN_SEC = 1.0

#: **跟播模式的「延迟线留量」**（秒）—— FIFO 里留多少秒不播。
#:
#: ⚠️ **为什么必须有**（实测 2026-11-22，用户同时报「滞后很多」和
#: 「中途卡顿断音」—— 这两件事是**此消彼长**的）：
#:
#: * 放行器在「播放头到达 ``句首 − lead``」时放行 ⇒ ``lead`` 紧了 ⇒
#:   合成刚要开始就要播 ⇒ **管线没有余量** ⇒ 合成一抖就是**断音**
#:   （实测：lead ≈ 生产延迟 2 s 时，每句断 1.0~1.2 s）；
#: * ``lead`` 松了 ⇒ 译文比源话晚好几秒 ⇒ **滞后很多**。
#:
#: ⇒ **拆开这两件事**：
#:
#: * **提前量** ``lead = 生产延迟 + 留量``（放行器照旧提前放行）；
#: * **留量**由 TTS 队列的 **FIFO 扣着**（最新的 N 秒不写进管道）
#:   ⇒ ① 管线永远有 N 秒余量（合成抖动被 FIFO 吸收 ⇒ **不断音**）；
#:   ② 播放时机仍能精确对齐（可听滞后 ≈ 0）。
#:
#: ⚠️ **「now」模式留量 = 0**（不等播放头 ⇒ 没有对齐问题 ⇒ 留量只是
#: 白白多出来的延迟）。
FOLLOW_RESERVE_SEC = 2.5

#: 待译队列深度（段）。满了 ⇒ **合并队尾**（不丢）。
INTERP_QUEUE_MAX = 64
#: 播前复核重试上限。
RETRY_LIMIT = 3
#: 翻译线程数（一个 MT 实例本身并发度就是 1 ⇒ 这里给 2 只是为了两份实例）。
INTERP_WORKERS = 2
#: 单句翻译超时（秒）。
TRANSLATE_TIMEOUT_S = 20.0

#: 自动变速的档位（按积压秒数选）—— 只在**产能不足**时提速。
TEMPO_STEPS = ((8.0, 1.15), (4.0, 1.08), (2.0, 1.04))
#: ⚠️ **下限必须 < 1.0**（实测 2026-11-23，M28）：TTS 的可持续产能只有
#: **~0.96×**（talker 自回归 ⇒ 物理上限）⇒ 播放按 1.0 消费就比生产快
#: ⇒ **管道存货被慢慢吃空** ⇒ 到**收尾**时存货见底 ⇒ 用户听到
#: 「句子被隔断，隔了很长时间才播最后几个词」（实测 15 s 素材：句间
#: 间隔从 0.045 s 一路涨到 0.7 s）。
#: ⇒ 变速要能**低于 1.0** ⇒ 让「播放速度 ≤ 可持续产能」⇒ 存货不塌。
TEMPO_MIN = 0.9
TEMPO_MAX = 1.15
#: **起步用的变速**（= 1.0）—— 不是 :data:`TEMPO_MIN`：
#: ``TEMPO_MIN`` 是「**能降到的下限**」（产能不够时才降下去）⇒
#: 起步就按 0.9 播 ⇒ 一上来就比源话慢 10% ⇒ 白白多出滞后。
TEMPO_DEFAULT = 1.0

#: **变速阶梯**（只取这几个值）—— 变速变了要**重开 aplay 管道**
#: （sox 的 tempo 是 spawn 时定死的）⇒ 变速必须**离散**，否则会频繁重开
#: ⇒ 每次重开都有一次句间缝隙。
#:
#: ⚠️ **与 ``cards/tts/queue.py`` 的阶梯保持一致** —— 引擎算出的
#: ``want`` 会被队列的 ``_clamp_tempo`` 再夹一次 ⇒ 两边不一致的话，
#: 引擎以为变速生效了、队列其实夹回去了（实测「面板显示 1.04，管道
#: 还是 1.00」）。
TEMPO_LADDER = (0.92, 0.96, 1.0, 1.04, 1.08, 1.15)

#: **变速的死区**（档位差小于这个值就不动）—— 每次改变速 = 重开管道
#: = 一次句间缝隙 ⇒ 别让变速抖。
#:
#: ⚠️ **必须小于「一档」的间距**（阶梯间距 0.04）—— 死区 0.05 会把
#: **整整一档**（1.00 → 1.04）挡在门外 ⇒ 变速永远升不上去
#: （实测：产能 1.047 ⇒ 该用 1.04，但死区 0.05 ⇒ 卡在 1.00 不动）。
TEMPO_DEADBAND = 0.02
#: **降速的死区**（比升速的大）—— 升速要快（跟上产能），降速要慢
#: （避免在阈值附近来回抖 ⇒ 每次变速都要重开 aplay 管道 ⇒ 一次句间缝隙）。
#:
#: ⚠️ 实测 2026-11-23：原来只有**棘轮**（只升不降）⇒ 变速一旦锁在高档位
#: 就下不来 ⇒ 播放速度超过真实产能 ⇒ 缓冲被吃空 ⇒ 断音。
#:
#: ⚠️ **但必须 < 一档的间距（0.04）**（实测 2026-11-23，M28）：
#: 死区 0.06 会把**整整一档**（1.00 → 0.96）挡在门外 ⇒ 变速永远降不到
#: 产能线（0.96）⇒ 播放比生产快 ⇒ **存货被吃空** ⇒ 收尾断音
#: （用户报「最后收尾句子被隔断，隔了很长时间才播最后几个词」）。
#: ⇒ 降到 0.03（一档之内放行；抖动交给 :data:`TEMPO_MIN_INTERVAL` 挡）。
TEMPO_DEADBAND_DOWN = 0.03
#: **两次变速之间的最小间隔**（秒）。
TEMPO_MIN_INTERVAL = 8.0

#: **首个解码窗口攒多少帧才开声**（帧 = 80 ms @24 kHz）。
#:
#: ⚠️ 上游实现首窗硬等满 50 帧（= **4 s 音频**）⇒ 实测「合成首窗」
#: ≈ **3.0 s** —— 同传链路上**最大的一段固定延迟**（翻译推理只要 0.3 s）。
#:
#: ⚠️ **为什么 25 帧就够**：``speech_decoder`` 是**因果**的（分块解码：
#: 「左上下文 25 帧 + 新 25 帧」）⇒ 首个窗口**没有左上下文** ⇒ 未来的帧
#: 对已生成部分**没有影响** ⇒ 攒够 25 帧（= 后续窗口的粒度）就能开声
#: ⇒ 帧覆盖仍精确（首窗吃 0~24，下一窗吃 25~49，不重不漏）。
#:
#: ⇒ 首声提前 ~1.5 s；队列的合成吞吐也从 ~3.2 s/句 提到 ~1.7 s/句 ⇒
#: **TTS 产能不再低于源语速率** ⇒ 积压不再累积。
#:
#: ⚠️ **为什么改成 12 帧**（实测 2026-11-23，M28）：
#: 首窗的**唯一**作用是「让第一块音频早点到」⇒ 它**不改变**总合成
#: 耗时（还是那 25+ 帧逐帧生成）⇒ 改小它 = **纯粹省掉首声前的死等**。
#: 实测：25 帧 ⇒ 首声 +2.07 s；12 帧 ⇒ 首声 ≈ +1.05 s ⇒
#: **可听滞后直接少 1 s**（用户报「播放很滞后」）。
#: ⇒ 再降到 8 帧收益递减（解码器要喂「左上下文 25 帧 + 新帧」⇒
#: 首窗太短会让左上下文不足 ⇒ 音质下降）⇒ 12 帧是甜点。
#:
#: ⚠️⚠️ **但 12 帧会「断音」**（实测 2026-11-24，M34；用户报「第一句话
#: 翻译完播放总是卡顿断续」）—— **首窗太小 ⇒ 管道盖不住下一窗的延迟**：
#:
#: * TTS 是**按窗口**吐 PCM 的：首窗 ``F`` 帧，之后每窗 **25 帧**（2 s）；
#: * 实测（15 s 素材）：首窗（12 帧 = 0.96 s 音频）在 **+1.30 s** 到达，
#:   第二窗（2 s 音频）在 **+3.19 s** 到达 ⇒ **窗间隔 ≈ 1.9 s**；
#: * 播放从 +1.30 s 开始 ⇒ 管道里只有 0.96 s ⇒ **+2.26 s 管道就空了**
#:   ⇒ 第二窗 +3.19 s 才到 ⇒ **中间静默 ~0.94 s**；
#: * ⇒ 听起来就是「**第一句念完 ⇒ 断一下 ⇒ 再接着念**」（用户原话：
#:   「第一句话翻译完播放总是卡顿断续」）。
#:
#: ⇒ **首窗必须盖住窗间隔**：``F × 0.08 s ≥ 1.9 s`` ⇒ ``F ≥ 24`` ⇒
#: **取 25 帧**（= 2 s 音频，**与后续窗口同粒度** ⇒ 首窗↔第二窗之间
#: 也就不缺音频了）。
#:
#: ⚠️ **代价 = 首声晚 ~1 s**（常数，不是「越播越落后」）—— 换来的是
#: **中间不断音**。实测（2026-11-24）：12 帧 ⇒ ``starved`` 1.5~2.3 s
#: （可听静默）；25 帧 ⇒ ``starved`` ≈ 0。
#:
#: ⇒ ⚠️ 这正是 **SDK 的默认值**（``FIRST_WINDOW_FRAMES = 25``）⇒
#: **TTS 卡自己走的就是 25 帧** ⇒ 同传跟它对齐即可。
INTERP_FIRST_WINDOW_FRAMES = 25

#: **变速的下限**（= 1.0 ⇒ **永不变慢**）。
#:
#: ⚠️ **为什么不能低于 1.0**（实测 2026-11-23，M29；用户报「播放滞后很多」）：
#:
#: * 播放速度 = ``min(tempo, 产能)``；译文的**前进速度** = 播放速度 ÷ 长度比 k；
#: * ⇒ **可听滞后的增长率 = k ÷ min(tempo, 产能) − 1**；
#: * 产能 ~1.0、k = 1.10 ⇒ ``tempo = 1.0`` 时增长 **0.10 s/s**，
#:   而 ``tempo = 0.96`` 时增长 **0.15 s/s** ⇒ **变慢反而更滞后**；
#: * 之前 ``need_tempo``（= 实测可持续速率 0.96）会把变速**压到 1.0 以下**
#:   （实测面板显示 0.96）⇒ 每秒多落后 0.05 秒 ⇒ 5 分钟素材多落后 15 秒。
#:
#: ⇒ **变速只用来「抵消长度比」（k > 1 且产能够），永不用来「放慢」**。
TEMPO_FLOOR = 1.0

#: **合并窗口**（秒）—— 放行器放行前多等这么久，让「几乎同时到」的
#: 几句译文**凑成一段**再送进 TTS。
#:
#: ⚠️ **为什么必须合并**（实测 2026-11-23，M28）：
#: TTS 每合成**一句**都有 **~2.9 s 固定开销**（实测拟合：
#: ``音频秒 ≈ 2.25 + 0.119 × 字数``；其中首尾静音只占 0.55 s，其余是
#: 模型的慢速起调与标点停顿）⇒ 一句 16 字的译文，**一半时间是开销**。
#: ⇒ 4 句译文 = 17.0 s 音频里 **~9 s 是纯开销** ⇒ 长度比 k 被推到
#: **1.20** ⇒ 而 TTS 产能只有 **~0.96×** ⇒ **产能天生不够** ⇒
#: 可听滞后**单调增长**（实测 2.6 → 4.8 → 5.7 s）。
#:
#: ⇒ 合并成 1~2 段 ⇒ 开销只付一次 ⇒ 音频 17.0 s → ~12 s ⇒
#: **k ≈ 1.0** ⇒ 产能刚好跟得上 ⇒ **滞后不再增长**（实测 a_lag
#: 从 5.06 s 降到 ~2.2 s），而且**句间边界变少 ⇒ 顿挫也变少**。
INTERP_MERGE_HOLD_SEC = 1.0

#: **跟播模式（文件 / 视频）的「按内容量攒批」阈值**（秒的译文音频）。
#:
#: ⚠️ **为什么按「内容量」而不是按「时间」攒**（实测 2026-11-23，M29；
#: 用户报「ASR+翻译后，TTS 播放滞后非常多」）：
#:
#: * TTS **每批**都有固定开销：**~1.3 s 合成死时间**（首窗）+ **~0.55 s
#:   首尾静音** ⇒ 批次越碎 ⇒ 产能越低 ⇒ 译文越播越落后；
#: * 实测（15 s 素材，3 批）：死时间 3.9 s / 内容 15.0 s ⇒ **产能只有
#:   ~0.81×** ⇒ 译文每秒落后 **0.23 s**（实测 2.6 → 4.9 → 5.6 s）；
#: * 合成 1 批 ⇒ 死时间只付一次 ⇒ 产能 **~0.95×** ⇒ 几乎不落后；
#: * ⇒ 批次至少要攒够 **~16~20 秒音频**才能把产能拉回 0.95（推算：
#:   ``P = k·M / (1.3 + k·M/1.03) ≥ 0.95`` ⇒ ``k·M ≥ 15.8 s``）。
#:
#: ⚠️ **攒批的代价 = 多等一会儿**（延迟）—— 但**只有跟播模式**才付这个
#: 代价（播放时钟会把译文对齐到源话 ⇒ 攒得越久，播得越准）；
#: **实时流（now）低延迟优先** ⇒ 仍用 :data:`INTERP_MERGE_HOLD_SEC`。
MERGE_MIN_AUDIO_SEC = 18.0

#: **跟播模式下「攒批」的上限**（秒）—— 攒不满内容量也不能永远不开口。
#:
#: ⚠️ **为什么只给 4 秒**（实测 2026-11-23，M29）：这是**兜底**，正常路径
#: 两条都先命中 —— ① 源读完 ⇒ 放；② 攒够 18 秒音频 ⇒ 放。
#: ⇒ 只有「源读完」判定失效时（实测：ASR 服务被**别的识别**占用 ⇒
#: ``current_request_id`` 对不上 ⇒ 判不出「跑完了」）才会走到这里
#: ⇒ 上限给长了（原来 10 s）就直接变成**十几秒的起播延迟**（实测 17 s）。
#: ⇒ 兜底只保证「不无限期压着」，不承担攒批的职责。
MERGE_MAX_HOLD_SEC = 4.0

#: **视频源**的攒批**下限**（秒的译文音频）—— 跟字幕节奏要紧 ⇒ 尽量小。
#:
#: ⚠️ **为什么视频源不能沿用「内容量攒批」**（实测 2026-11-23，M31；
#: 用户报「含音轨的视频开始同传后，没有根据目标语音字幕的节奏播放」）：
#:
#: * **文件源是离线一次性转写** ⇒ 整个素材的句子**一次性到齐** ⇒ 攒
#:   18 秒音频只多花「首句」的等待 ⇒ 后面几句**本来就没到点**；
#: * **视频源是边播边识别**（解码比实时快，ASR 跑在播放头**前面**）⇒
#:   字幕行**随播放实时到达** ⇒ 攒 18 秒 = **译文晚 18 秒才开口**，
#:   而且一开口就是**一大块**（实测一批 **6 行 / 14.8 秒源话**）⇒
#:   批内后面的行**比自己的字幕早十几秒**播出 ⇒ **完全不是字幕的节奏**；
#: * ⇒ 视频源单独一套口径：**批次小 + 按积压自适应**（见
#:   :meth:`InterpEngine._video_batch_ready`）。
VIDEO_MERGE_MIN_AUDIO_SEC = 3.0

#: **视频源**的攒批**上限**（秒的译文音频）—— 产能不够时靠它兜。
#:
#: ⚠️ **为什么需要上限**：TTS **每次开口有 ~1.33 s 的固定开销**（实测：
#: 40 句逐句提交 ⇒ 估算音频 98.8 s / 墙钟 152 s ⇒ 吞吐只有 **0.65×**）
#: ⇒ 批次太碎 ⇒ 产能追不上 ⇒ 语音**越落越远**（而且是**一直**落下去）。
#: ⇒ 积压一大就放大批次 ⇒ 先保住「不落后」，节奏退让。
VIDEO_MERGE_MAX_AUDIO_SEC = 20.0

#: **视频源**：「TTS 还欠多少秒内容」⇒ 批次放大多少秒（自适应增益）。
#:
#: ⚠️ **为什么是「自适应」而不是固定值**：节奏与产能是**对立**的
#: ⇒ 固定小批次 ⇒ 产能不够（落后）；固定大批次 ⇒ 节奏粗（早）。
#: ⇒ 让批次**跟着「欠多少」走**：跟得上（欠 ≈ 0）⇒ 批次最小 ⇒ 节奏最紧；
#: 跟不上（欠得多）⇒ 批次自动变大 ⇒ 先把欠的内容追回来。
#:
#: ⚠️ **「欠多少」不是「队列里堆了多少音频」** —— 视频源里 TTS
#: **故意跑在播放前面** ⇒ 队列里堆着几十秒音频是**设计如此**（实测踩过：
#: 拿它当反馈量 ⇒ 阈值一路涨到上限 ⇒ 批次又变成一大坨）。
VIDEO_MERGE_BACKLOG_GAIN = 1.0


def _estimate_audio_sec(text: str) -> float:
    """估算这段译文念出来有多长（秒）。

    ⚠️ 只需要**大概**（用来判断「攒够没有」）⇒ 粗估即可。
    中文约 4.5 字/秒，英文约 14 字符/秒（与 TTS 队列的口径一致）。
    """
    cjk = 0
    for ch in (text or ""):
        if "一" <= ch <= "鿿":
            cjk += 1
    other = max(0, len(text or "") - cjk)
    return max(0.6, cjk / 4.5 + other / 14.0)

#: **TTS 文本里要删掉的「停顿标点」**（实测 2026-11-23，M29）。
#:
#: ⚠️ **为什么删**（用户报「ASR+翻译后，TTS 播放滞后非常多」）：
#:
#: * 模型会为**每一个标点**留一段静音 —— 实测中文 TTS **每个标点
#:   ~0.25~1.0 秒**（三段译文实测：标点停顿合计 **2.2~4.2 秒**，
#:   占音频总长 **9%~30%**）；
#: * 那段静音是**纯浪费** —— 字幕里它一个字都不占 ⇒ **字幕照旧保留
#:   标点**，只有**送进 TTS 的那一份**去掉 ⇒ 听感不变，内容不丢；
#: * 去掉之后译文比源话**短**（长度比 k 从 **1.10 → 0.99**）⇒
#:   **可听滞后不再随时间增长**（原来每 1 秒源话长 0.1~0.2 秒）。
#:
#: ⚠️ **只删 CJK 的停顿标点** —— 拉丁文的标点停顿很短（~0.15 s）
#: ⇒ 留着反而更清楚 ⇒ 目标语是英文/德文时本改动**是空操作**。
_TTS_PAUSE_PUNCT = "，。！？；：、…—～·「」『』（）《》〈〉【】“”‘’"


def _tts_text(text: str) -> str:
    """送进 TTS 的文本 —— **删掉停顿标点**（字幕仍用原文）。

    ⚠️ **只影响「念出来有多长」**，不影响字幕、不影响翻译内容。
    """
    if not text:
        return ""
    out = "".join(ch for ch in text if ch not in _TTS_PAUSE_PUNCT)
    return re.sub(r"\s+", " ", out).strip()


def _order_key(seg):
    """提交顺序 —— **媒体位置优先**，产生顺序兜底。

    ⚠️⚠️ **为什么不能只按 index**（实测 2026-11-23，用户报「翻译串句、
    重复、拼接错乱」）：``index`` 是**产生顺序**（全局自增）⇒ 用户拖动
    进度条后**重开识别**时，新一段的句子 **index 更大、但媒体位置更小**
    ⇒ 按 index 排 ⇒ **播放顺序 ≠ 源话顺序**。

    ⇒ 实测（用户那轮 + M32 探针）：``idx=357`` 的 1030.75 s 排在
    ``idx=356`` 的 1027.43 s **前面** ⇒ 译文顺序和字幕顺序对不上。

    ⇒ 位置已知的（文件 / 视频源）按**位置**排；实时流没有位置（-1）⇒
    退回 ``index``（本来就是产生顺序，行为不变）。

    ⚠️ 顺带修好一件事：:meth:`InterpEngine._submit` 用**队首**的
    ``src_start_sec`` 定位播放时钟 ⇒ 队首错了 ⇒ 整段译文**对错位置**
    播出；按位置排之后队首 = **最早**的那句 ⇒ 对齐正确。
    """
    start = getattr(seg, "src_start_sec", -1.0)
    if start is not None and start >= 0.0:
        return (0, float(start), int(getattr(seg, "index", 0)))
    return (1, 0.0, int(getattr(seg, "index", 0)))


#: provider 的语言 key → TTS 的语言 id。
#:
#: ⚠️ **MT 能翻 37 种，TTS 只能出声 11 种（含 auto）** ⇒ 交集 **10 种**
#: （zh-CN/zh-TW/en/de/it/pt/es/ja/ko/fr/ru）。不在表里的目标语 ⇒
#: 同传**降级为仅字幕**（不播报，但译文照常显示）。
_TTS_LANG_BY_PROVIDER = {
    "zh-CN": "chinese",
    "zh-TW": "chinese",
    "en": "english",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "es": "spanish",
    "ja": "japanese",
    "ko": "korean",
    "fr": "french",
    "ru": "russian",
}

#: TTS 语言 id → provider 的语言 key（面板上要显示目标语）。
_PROVIDER_BY_TTS_LANG = {
    "chinese": "zh-CN",
    "english": "en",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "spanish": "es",
    "japanese": "ja",
    "korean": "ko",
    "french": "fr",
    "russian": "ru",
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class InterpSegment:
    """一句源文（+ 它在时间轴上的位置）。"""
    index: int                       # 全局递增
    text: str                        # 源文
    lang: str                        # 源语（ASR 报的语言名，如 "English"）
    stream_id: str                   # 哪条流（file-3#1 / live-xxx）
    mode: str                        # "fast" | "steady"
    #: 源文在**音频时间轴**上的起止（秒）。实时流没有 ⇒ -1.0
    src_start_sec: float = -1.0
    src_end_sec: float = -1.0
    #: 这一刻（epoch 秒）—— 实时流用它算「说完→听到」的滞后
    created_ts: float = 0.0
    #: 来源标签（"live" | "file"）—— 决定用哪种滞后口径
    origin: str = "live"
    #: 译文（翻译完成后填）
    translated: str = ""
    # ------------------------------------------------------------------ #
    # **分段计时**（诊断「滞后 15 秒」花在哪一段）
    # ------------------------------------------------------------------ #
    #: 译文回来的时刻（wall clock）。
    translated_at: float = 0.0
    #: 交给 TTS 队列的时刻（wall clock）—— 减去 ``translated_at``
    #: 就是**放行器压住**的那段时间。
    submitted_at: float = 0.0
    # ------------------------------------------------------------------ #
    # **翻译段的三段拆分**（区分「等模型就绪」与「真推理」）
    # ------------------------------------------------------------------ #
    #: 从**翻译队列**里取出这一句的时刻 —— 减去 ``created_ts``
    #: = 在翻译队列里排了多久（前面有几句）。
    dequeued_at: float = 0.0
    #: **等 MT 模型就绪**花掉的秒数（冷启动时是大头，稳态是 0）。
    wait_mt_sec: float = 0.0
    #: **纯推理**（``translate_sentence``）累计花掉的秒数。
    infer_sec: float = 0.0
    #: 推理试了几次（1 = 一次过）。
    attempts: int = 0
    #: 这一句的计时记录（翻译完成时建好，入队时补 ``t_submit``）。
    timing: dict = field(default_factory=dict, repr=False)


@dataclass
class _Counters:
    segments: int = 0
    translated: int = 0
    retried: int = 0
    abandoned: int = 0
    submitted: int = 0
    skipped_seek: int = 0
    #: **seek 之后被重新武装（重播）的句子数** —— 用户拖回进度条时，
    #: 已经播过的句子要**再说一遍**（否则字幕有译文却一声不响）。
    replayed: int = 0
    failed: int = 0
    played: int = 0
    merged: int = 0
    lag_sec: float = 0.0
    lag_sum: float = 0.0
    tempo: float = 1.0
    last_error: str = ""
    tts_available: bool = True
    tts: dict = field(default_factory=dict)


class StablePrefixSegmenter:
    """快档切句器：从 ``unfixed`` 求「最长公共前缀」。

    规则：

    1. 与上一轮求 LCP；
    2. LCP 变长 ⇒ 候选更新，稳定计数清零；
    3. LCP 连续 N 轮「长度与内容都不变」⇒ 成句；
    4. LCP **变短**（模型改写）⇒ 作废候选，重新开始。

    为什么不用 ``committed`` 增量：文件流的 ``committed`` 只增长、不回滚 ⇒
    「说话中」与「停顿」分不开 ⇒ 快档判据在文件流上失效 ⇒ 文件流的快档
    改用「更短的强制切句」（见 service.py）。
    """

    def __init__(self, stable_rounds: int = INTERP_STABLE_ROUNDS,
                 min_chars: int = INTERP_MIN_CHARS,
                 max_chars: int = INTERP_MAX_CHARS) -> None:
        self._stable_rounds = stable_rounds
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._candidate = ""
        self._stable = 0

    def feed(self, unfixed: str) -> Optional[str]:
        """喂一轮 ``unfixed``；成句了返回句子，否则返回 None。"""
        text = (unfixed or "").strip()
        if not text:
            # 模型清空（退化 / 回看）⇒ 作废候选
            self._candidate = ""
            self._stable = 0
            return None
        prev = self._candidate
        # ⚠️ 比较的是**截断后**的文本 —— 候选本来就只留前 N 字 ⇒ 用原文
        # 比会把「一直在长」误判成「没变」。
        cur = text[: self._max_chars]
        if prev and not cur.startswith(prev):
            # LCP 变短（模型改写）⇒ 作废候选，从头再来
            prev = ""
            self._candidate = ""
            self._stable = 0
        if not prev:
            # 第一轮：候选 = 当前文本（截到上限）
            self._candidate = cur
            self._stable = 0
            return None
        if len(cur) > len(prev):
            # 还在长 ⇒ 候选更新，稳定计数清零
            self._candidate = cur
            self._stable = 0
            return None
        # 长度与内容都不变 ⇒ 稳定计数 +1
        self._stable += 1
        if self._stable < self._stable_rounds:
            return None
        out = self._candidate
        self._candidate = ""
        self._stable = 0
        if len(out) < self._min_chars:
            return None
        return out


class Gate:
    """放行器：决定「这句现在能不能进 TTS 队列」。"""

    def __init__(self, mode: str = "now") -> None:
        self._lock = threading.Lock()
        self._mode = mode if mode in ("now", "follow") else "now"
        #: **跟播提前量**（秒）—— 「播放头到达 ``句首 − lead``」就放行。
        #: 默认 :data:`FOLLOW_LEAD_SEC`（1.0）；跑起来后由
        #: :meth:`set_lead` 按**实测管线延迟**更新。
        self._lead: float = FOLLOW_LEAD_SEC
        self._playhead: dict[str, float] = {}
        self._playing: dict[str, bool] = {}
        #: 最近一次被更新的流 —— 前端不一定知道 epoch（文件流会换段）⇒
        #: 播放头找不到对应流时**回退**到它（同一时刻只有一路在跑）。
        self._last_stream = ""
        #: **最近一次上报的播放头**（不分流）—— 兜底用。
        #:
        #: ⚠️ **为什么必须有它**（实测踩过，用户报「有翻译，但是没有播放」）：
        #: 前端上报播放头时带的 ``stream_id`` **可能是空的**（前端的
        #: ``_interp.streamId`` 从来没被赋值 ⇒ 一直是 ``""``）⇒ 播放头
        #: 存进了 ``""`` 这个键 ⇒ 句子（``file-xxx#0``）**配不上** ⇒
        #: 「跟播放」把**所有**句子**永久压住** ⇒ 译文有、**一声不响**。
        #:
        #: ⇒ 同一时刻只有一路在跑 ⇒ 拿「最近一次上报的」兜底即可。
        self._last_ph: Optional[tuple] = None
        #: 播放头**最后一次变化**的时刻（wall clock）—— 算「卡住多久」。
        self._ph_at: float = 0.0
        #: 播放头**曾经真正走过**（> ``FOLLOW_NEVER_PLAYED_SEC``）——
        #: 区分「自动播放被拦（从没播过）」和「用户主动暂停（播过再停）」。
        self._ph_moved: bool = False
        #: 兜底放行的次数（给状态/诊断看）。
        self._stalled_released = 0
        #: **源播完了**（媒体 ``ended``）/ **媒体加载失败**（``failed``）——
        #: 这两种情况下源**不会再往前走** ⇒ 放行器**不再等**。
        #:
        #: ⚠️ **必须有它**（实测，用户报「只播放了第一句」）：媒体播完 ⇒
        #: ``playing=false`` ⇒ 「播放头到达就放行」那条判据失效 ⇒ 而兜底
        #: 又要求「播放头**从没走过**」⇒ 媒体播过 ⇒ 兜底也不触发
        #: ⇒ 媒体播完之后才识别出来的句子**被永久压住**。
        self._ended: dict[str, bool] = {}
        self._failed: dict[str, bool] = {}
        #: 最近一次上报的「源已结束」标志（按流 id 配不上时兜底用）。
        self._last_done: bool = False
        #: **跟播计时的起点** —— 一次会话里**还没有任何播放头上报**时，
        #: 用它算「卡住多久」（否则「没上报过」这种情况永远不算卡住 ⇒
        #: 兜底放行永远不触发 ⇒ 句子被无限期压住）。
        self._armed_at: float = time.time()
        #: **上一次播放头上报的墙钟时刻** —— 判「前跳型 seek」要用：
        #: 两次上报之间真实过去 ``dt`` 秒 ⇒ 播放头最多前进 ``dt`` 秒
        #: ⇒ 多出来的才算「用户拖了进度条」。
        self._ph_ts: float = 0.0
        #: **最近一次判定的 seek**：``{"dir": "back"/"fwd", "from":…, "to":…}``。
        #: 没有 ⇒ ``None``。
        self._last_seek: Optional[dict] = None
        #: **媒体时间轴的零点**（wall clock）—— 由播放头上报反推：
        #: ``anchor = 上报时刻 − playhead_sec``。
        #:
        #: ⚠️ **为什么需要它**（用户报「滞后 15 秒」）：
        #: **文件源**的句子是**离线一次性转写完**的 ⇒ 全部句子的
        #: ``created_ts`` 几乎相同（= 转写完成的时刻）⇒ 拿它当
        #: 「源话说完的时刻」⇒ 面板算出的滞后会**随句子数单调增长**
        #: （实测 6.4 → 10.4 → 14.6 → 17.8 秒），而用户实际听到的
        #: 译文与源话的错位只有几秒 ⇒ **面板在骗人**。
        #:
        #: ⇒ 文件源要用**音频时间轴**：源话结束的时刻 = ``anchor +
        #: src_end_sec`` ⇒ 面板显示的滞后才是「用户真正听到的错位」。
        self._anchor: float = 0.0

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        with self._lock:
            new = mode if mode in ("now", "follow") else "now"
            if new != self._mode:
                self._mode = new
                # 换档位 ⇒ 重新起算「卡住多久」
                self._armed_at = time.time()

    def set_lead(self, lead: float) -> None:
        """设**跟播提前量**（秒）—— 「播放头到达 ``句首 − lead``」就放行。

        ⚠️ **为什么提前量要自适应**（实测 2026-11-22，用户报「播放还是
        滞后很多」）：提前量应当等于 **TTS 的管线延迟**（放行 → 出声），
        否则译文跟源话对不上：
        * 提前量太小 ⇒ 译文**晚于**源话（永远差一个管线延迟）；
        * 提前量太大 ⇒ 译文**抢在**源话前面（听着更怪）。

        ⇒ 由 :meth:`InterpEngine._update_lead` 按实测值更新。
        """
        try:
            v = float(lead)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._lead = max(0.0, v)

    def reset(self) -> None:
        """**新会话** ⇒ 播放头记账清零。

        ⚠️ **为什么必须清**（实测踩过，用户报「有翻译，但是没有播放」）：

        ``_ph_moved``（播放头**曾经真的走过**）是「自动播放被拦」与
        「用户主动暂停」的分界线 ⇒ 它**跨会话残留**的话：

        * 会话 A 媒体播过（``_ph_moved=True``）⇒ 会话 B 若是「自动播放
          被拦」（媒体**从没播过**）⇒ ``never_played`` 永远是 False ⇒
          **兜底放行永远不触发** ⇒ 句子被永久压住 ⇒ **有翻译、一声不响**；
        * 反过来更糟：会话 A 从没播过 ⇒ 会话 B 用户**主动暂停** ⇒ 兜底
          误触发 ⇒ 用户按了暂停，译文还在念。

        ⇒ 每起一次同传（新会话）都要清。
        """
        with self._lock:
            self._playhead.clear()
            self._playing.clear()
            self._ended.clear()
            self._failed.clear()
            self._last_stream = ""
            self._last_ph = None
            self._last_done = False
            self._ph_at = 0.0
            self._ph_moved = False
            self._stalled_released = 0
            self._armed_at = time.time()
            self._anchor = 0.0
            self._ph_ts = 0.0
            self._last_seek = None

    def reset_stream(self) -> None:
        """**换流**（新一轮识别）⇒ 清流相关的记账，**保留媒体状态**。

        ⚠️ **和 :meth:`reset` 的区别**（实测，用户报「只播放了第一句」+
        「滞后 15 秒」，2026-11-22）：

        * ``reset()`` = **新会话** ⇒ 连「源已结束」一起清（用户重新开始
          了一次全新的同传）；
        * ``reset_stream()`` = **同一场同传、换了一轮识别** ⇒ 媒体还是
          **那个媒体**（文件源用的是同一个 ``<audio>`` 元素）⇒
          **「源已经播完」这个事实仍然成立** ⇒ 清掉的话 ⇒ 新一轮的句子
          会因为「还在等播放头」被**压住** ⇒ 回归第三场（「源结束后开始
          识别 ⇒ 剩下的译文照样全播」）**失败**。

        ⇒ 这里清：播放头映射 / 「播放头曾经走过」/ 媒体零点 / 兜底计数；
        ⇒ 这里**留**：``_ended`` / ``_failed`` / ``_last_done``（**媒体**的
        状态，不是流的状态）。
        """
        with self._lock:
            self._playhead.clear()
            self._playing.clear()
            self._last_stream = ""
            self._last_ph = None
            self._ph_at = 0.0
            self._ph_moved = False
            self._stalled_released = 0
            self._armed_at = time.time()
            self._anchor = 0.0
            self._ph_ts = 0.0
            # ⚠️ **``_last_seek`` 故意不清** —— 它是给面板看的**回执**
            #    （「已跟随进度条重新对齐」）⇒ 而「拖进度条」之后**紧接着**
            #    就是「重开识别」（视频源）⇒ 一清就没了 ⇒ 用户看不到反馈。
            # ⚠️ ``_ended`` / ``_failed`` / ``_last_done`` **故意不清** ——
            # 那是**媒体**的状态（媒体还是那个媒体）。

    def update_playhead(self, stream_id: str, playhead_sec: float,
                        playing: bool, ended: bool = False,
                        failed: bool = False) -> bool:
        """更新播放头；返回 **True = 判定为 seek（用户拖了进度条）**。

        ⚠️ ``ended`` / ``failed`` = **源不会再往前走了**（媒体播完 /
        媒体加载失败）⇒ 放行器**不再等**。

        ⚠️ **为什么必须有它**（实测，用户报「只播放了第一句」）：

        媒体播完 ⇒ ``paused=true, ended=true, currentTime=停在时长``
        ⇒ 前端上报的 ``playing`` 变成 **false** ⇒ 放行器的
        「播放头到达就放行」那条判据（要求 ``playing is not False``）
        **不再成立** ⇒ 而兜底放行又要求「播放头**从没走过**」
        ⇒ 媒体**播过** ⇒ 兜底**也不触发**
        ⇒ ⇒ **媒体播完之后才识别出来的句子被永久压住**。

        而文件源天然就是这个时序：源 15 s，ASR+MT 要十几秒才出齐
        ⇒ ��媒体还在播时出的那一句被播了，剩下的全卡住
        ⇒ **用户听到的只有第一句**。

        ⚠️⚠️ **seek 必须**双向**识别**（实测 2026-11-22，用户报「含音轨的
        视频多次拖动进度条，没跟字幕的翻译同声播出来」）：

        * **只认回退**（旧版）⇒ **前跳完全不被识别** ⇒ 用户跳过的那些
          句子**还在待播队列里** ⇒ 放行器一看播放头已经越过它们 ⇒
          **全部放行** ⇒ TTS 队列里灌进几十秒的**陈旧译文** ⇒ 接下来
          一两分钟讲的全是**用户已经跳过的内容** ⇒ 与画面字幕完全对不上
          （实测：播放头 61~74 s，播的却是 29~47 s 的内容 ⇒ 错位
          **+27~+35 秒**，17 句里 15 句不同步）；
        * **回退也要处理**（旧版只丢待播句，**没清队列里的陈旧音频**）⇒
          FIFO 里**旧位置**的译文照样播 ⇒ 实测错位 **−20~−31 秒**。

        ⇒ 前跳的判据是「**比两次上报之间的真实时长多跳了多少**」
        （见 :data:`SEEK_FWD_TOLERANCE`）⇒ 媒体卡住 / 缓冲造成的一次
        补跳**不会**被误判成 seek。
        """
        now_ts = time.time()
        with self._lock:
            prev = self._playhead.get(stream_id)
            now = float(playhead_sec)
            self._playhead[stream_id] = now
            self._playing[stream_id] = bool(playing)
            self._ended[stream_id] = bool(ended)
            self._failed[stream_id] = bool(failed)
            self._last_stream = stream_id
            self._last_ph = (now, bool(playing))
            self._last_done = bool(ended) or bool(failed)
            # ⚠️ **反推媒体零点**（媒体时间 0 对应的 wall clock）——
            # 文件源的滞后口径要用它（见 ``_anchor`` 的说明）。
            #
            # ⚠️ **只在「媒体真的在播」时更新** —— 播放前播放头恒为 0，
            # 那时 ``now − playhead = now`` ⇒ 零点会**一直往后滑** ⇒
            # 拿它算滞后会得到「还没开始播 ⇒ 源话还没说完」⇒ 滞后恒为 0。
            if now >= 0.0 and playing:
                self._anchor = now_ts - now
            if prev is None or abs(now - prev) > 0.05:
                self._ph_at = now_ts
                if now > FOLLOW_NEVER_PLAYED_SEC:
                    self._ph_moved = True
            elif self._ph_at <= 0.0:
                self._ph_at = now_ts
            # ⚠️ **seek 判定（双向）** —— 见上面的说明。
            seek_dir = ""
            if prev is not None:
                if now < prev - SEEK_BACK_TOLERANCE:
                    seek_dir = "back"
                else:
                    # 两次上报之间真实过去 ``dt`` 秒 ⇒ 播放头最多前进
                    # ``dt`` 秒 ⇒ 多出来的才算「用户拖了进度条」。
                    dt = max(0.0, now_ts - self._ph_ts) if self._ph_ts > 0 else 0.0
                    if (now - prev) > (dt + SEEK_FWD_TOLERANCE):
                        seek_dir = "fwd"
            self._ph_ts = now_ts
            if seek_dir:
                self._last_seek = {
                    "dir": seek_dir,
                    "from": float(prev),
                    "to": float(now),
                    "ts": now_ts,
                }
            return bool(seek_dir)

    def last_seek(self) -> Optional[dict]:
        """最近一次判定的 seek（方向 + 起止位置）；没有 ⇒ ``None``。

        ⇒ ``{"dir": "back"/"fwd", "from":…, "to":…, "ts":…}``
        """
        with self._lock:
            return dict(self._last_seek) if self._last_seek else None

    def media_anchor(self) -> float:
        """媒体时间轴零点（wall clock）；``0.0`` = 还没上报过播放头。

        ⇒ ``anchor + 音频秒`` = 该音频位置对应的**墙上时刻**。
        """
        with self._lock:
            return self._anchor

    def source_done(self, stream_id: str = "") -> bool:
        """源**不会再往前走了**（媒体播完 / 媒体加载失败）？

        ⇒ 这种情况下**不再等播放头**（剩下的译文直接补播）。
        """
        with self._lock:
            ended = self._ended.get(stream_id)
            failed = self._failed.get(stream_id)
            if ended is None and failed is None:
                return bool(self._last_done)
            return bool(ended) or bool(failed)

    def playhead(self, stream_id: str) -> Optional[float]:
        with self._lock:
            v = self._playhead.get(stream_id)
            if v is None and self._last_ph is not None:
                return self._last_ph[0]
            return v

    def is_playing(self, stream_id: str) -> bool:
        with self._lock:
            v = self._playing.get(stream_id)
            if v is None and self._last_ph is not None:
                return bool(self._last_ph[1])
            return bool(v if v is not None else True)

    # ------------------------------------------------------------------ #
    # 卡住判定
    # ------------------------------------------------------------------ #
    def _stall_state(self) -> tuple:
        """(卡住多久秒, 媒体是否**从没真正播过**, 最后一次报的 playing)"""
        with self._lock:
            at = self._ph_at
            moved = self._ph_moved
            playing = self._last_ph[1] if self._last_ph else None
            armed = self._armed_at
        # ⚠️ **一次播放头都没上报过**（``_ph_at == 0``）⇒ 用「跟播计时
        # 起点」当基准 —— 否则「卡住多久」永远是 0 ⇒ 兜底永远不触发
        # ⇒ 句子被无限期压住（前端没上报 = 媒体没在放 / 页面没在报）。
        base = at if at > 0.0 else armed
        if base <= 0.0:
            return 0.0, not moved, playing
        return max(0.0, time.time() - base), not moved, playing

    def stalled(self, stream_id: str = "") -> bool:
        """播放头**卡住**了（不动）—— 前端据此提示用户。

        ⚠️ **判据是「播放头有没有在动」，不是「媒体声称在不在播」**：

        实测踩过（用户报「只播放了第一句」）：浏览器上报
        ``playing=true``，但 ``currentTime`` **一动不动**（媒体卡在缓冲 /
        解码不动 / 服务端没喂上数据）⇒
        * 「播放头到达就放行」那条判据**永远不成立** ⇒ 句子全被压住；
        * 而旧的兜底要求「媒体报的是暂停」⇒ **也不触发**；
        * ⇒ 只有 ``src_start ≤ 1 s`` 的**那一句**被放行 ⇒
          **用户只听到第一句**，面板还一声不响。

        ⇒ 现在：**播放头冻住**就是卡住（不管媒体怎么说）。

        ⚠️ 「音轨比画面晚开始」不受影响：视频源的播放头就是
        ``video.currentTime`` ⇒ 它会往前走 ⇒ 不算卡住。
        """
        if self.mode != "follow":
            return False
        sec, _never, _playing = self._stall_state()
        return sec >= FOLLOW_STALL_WARN_SEC

    def stalled_released(self) -> int:
        with self._lock:
            return self._stalled_released

    def never_played(self) -> bool:
        """这一轮会话里，播放头**从没真正走过**（自动播放被拦的指纹）。"""
        with self._lock:
            return not self._ph_moved

    def admit(self, seg: InterpSegment) -> bool:
        """这句能不能进 TTS 队列。"""
        if self.mode == "now":
            return True
        with self._lock:
            ph = self._playhead.get(seg.stream_id)
            playing = self._playing.get(seg.stream_id)
            ended = self._ended.get(seg.stream_id)
            failed = self._failed.get(seg.stream_id)
            if ph is None and self._last_ph is not None:
                # ⚠️ **兜底回退**（见 ``_last_ph`` 的说明）—— 播放头是
                # 按「上报时的 stream_id」存的，而那可能与句子的
                # stream_id **对不上**（前端上报空 id / 文件流换 epoch）
                # ⇒ 配不上 ⇒ 句子被永久压住 ⇒「有翻译，但是没有播放」。
                ph, playing = self._last_ph
            # ⚠️⚠️ **「源已结束」是**媒体**的状态**（不是流的状态）——
            # 换流（**新一轮识别**）之后它**仍然成立** ⇒ 新一轮的句子
            # **直接补播**，不用再等 ``FOLLOW_STALL_SEC`` 的兜底。
            #
            # ⚠️ **为什么必须在这里兜底**（实测 2026-11-23，用户报
            # 「播放完一次之后，再开始识别就不在播放了」）：
            # ``reset_stream()`` 清了 ``_playhead`` / ``_last_ph`` ⇒
            # 新一轮的句子**配不上任何播放头** ⇒ 「播放头到达就放行」
            # 和「源已结束就放行」**两条都不成立** ⇒ 只能等 10 秒的
            # 兜底放行 ⇒ 译文**晚 10 秒**才出声（用户以为「没播」）。
            if ended is None and failed is None and self._last_done:
                ended = failed = True
        # ---------------------------------------------------------------- #
        # ① **源不会再往前走了**（媒体播完 / 媒体加载失败）⇒ **直接放**
        #
        # ⚠️ **为什么必须有这条**（实测，用户报「只播放了第一句」）：
        # 媒体播完 ⇒ ``playing=false`` ⇒ 下面那条「播放头到达就放行」的
        # 判据（要求 ``playing is not False``）**失效**；而兜底放行又要求
        # 「播放头**从没走过**」⇒ 媒体播过 ⇒ 兜底**也不触发**
        # ⇒ **媒体播完之后才识别出来的句子被永久压住** ⇒ 只播了第一句。
        #
        # ⇒ 源都结束了，就没什么可等的了 ⇒ 剩下的译文**直接补播**。
        # ---------------------------------------------------------------- #
        if ended or failed:
            return True
        if ph is not None and playing is not False and seg.src_start_sec >= 0:
            with self._lock:
                lead = self._lead
            if ph >= (seg.src_start_sec - lead):
                return True
        # ---------------------------------------------------------------- #
        # ⬇️ **兜底**：**别把句子无限期压着**
        #
        # 场景（实测踩过两次，用户报「有翻译，但是没有播放」/「只播第一句」）：
        #
        # * 浏览器按自动播放策略**拒绝**了 ``el.play()`` ⇒ 媒体一直暂停 ⇒
        #   播放头永远 0 ⇒「跟播放」把**所有**句子**永久压住**；
        # * 或者媒体**声称在播**（``playing=true``）但 ``currentTime``
        #   **一动不动**（卡在缓冲 / 解码不动）⇒ 同上，而且**面板一声不响**
        #   （旧的告警要求「媒体报的是暂停」⇒ 不触发）。
        #
        # ⇒ 判据统一成「**播放头冻住了**」：**从没走过** + 冻住超过
        #   ``FOLLOW_STALL_SEC`` ⇒ 放行（不管媒体声称在不在播）。
        #
        # ⚠️ **「用户主动暂停」要区别对待**：播放头**曾经走过**（媒体真的
        # 播过）⇒ 用户是在看字幕时主动暂停 ⇒ 译文跟着停是对的 ⇒ **不兜底**。
        # ⚠️ **「音轨比画面晚开始」不受影响**：视频源的播放头就是
        #   ``video.currentTime`` ⇒ 它会往前走 ⇒ ``moved=True`` ⇒ 不兜底。
        stalled_for, never_played, _playing_now = self._stall_state()
        if never_played and stalled_for >= FOLLOW_STALL_SEC:
            with self._lock:
                self._stalled_released += 1
            return True
        return False


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

class InterpEngine:
    """同传引擎。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running = False
        self._mode = "steady"            # fast | steady
        self._gate_mode = "now"          # now | follow
        self._target_lang = "zh-CN"      # provider 的语言 key
        self._speaker = ""
        #: **参考音色**（voice-clone）—— 与 ``_speaker``（内置音色）是两个
        #: 不同的 SDK 参数。本板只有参考音色 ⇒ 面板选的是参考音色时走这里。
        self._ref_speaker = ""
        #: **默认音量 0.5**（振幅倍率，不是硬件音量）。
        #:
        #: ⚠️ 之前是 0.1 ⇒ 板端实测出声峰值只有 **0.025（≈ −32 dBFS）**
        #: （模型原始输出本身才 0.15~0.40）⇒ 板载小喇叭上**基本听不见**
        #: ⇒ 用户会报「有翻译，但是没有播放」—— 其实是「太小」。
        #: ⇒ 与 TTS 卡自己的默认（0.5）对齐。
        self._volume = 0.5
        self._source = "live"            # live | file | video
        self._seg_index = 0
        self._queue: "queue.Queue[InterpSegment]" = queue.Queue(
            maxsize=INTERP_QUEUE_MAX)
        self._pending: list[InterpSegment] = []   # 已译好、等播放头的
        #: **每句「从可放行到真放行」的时刻**（按 ``index`` 索引）——
        #: 用来实现**合并窗口**（:data:`INTERP_MERGE_HOLD_SEC`）：
        #: 放行器认为某句「可以走了」之后，再多等这么久，让**后到的
        #: 几句**能凑成**一段**再送进 TTS ⇒ 摊薄 TTS 每句 ~2.9 s 的
        #: 固定开销（见 :data:`INTERP_MERGE_HOLD_SEC` 的说明）。
        self._ready_since: dict[int, float] = {}
        #: **已译好的句子缓存**（按产生顺序）—— seek 之后要靠它**重播**：
        #: 用户把进度条**拖回去** ⇒ 那些句子早就播过了（放行器放行过 ⇒
        #: 不在 ``_pending`` 里了）⇒ 没有缓存就**再也说不出来** ⇒
        #: 用户看到的字幕有译文、却**一声不响**。
        #:
        #: ⚠️ 只留**有时间轴**的句子（文件 / 视频源）—— 实时流没有位置。
        self._cache: list[InterpSegment] = []
        #: **当前已武装（在 ``_pending`` 里）的句子下标** —— 防止重复入队
        #: （seek 重播时用它判断「这句是不是已经在等了」）。
        self._armed: set[int] = set()
        #: **最近一次 seek 的回执**（给面板显示「已重新对齐」）。
        self._last_seek_info: Optional[dict] = None
        self._segmenters: dict[str, StablePrefixSegmenter] = {}
        self._gate = Gate("now")
        self._counters = _Counters()
        #: **每句的分段计时**（诊断「滞后 15 秒」到底花在哪一段）：
        #: ``{index, text, src_start, src_end, t_seg, t_tr, t_submit, t_text}``
        #: * ``t_seg``   —— ASR 切出这句的时刻（wall clock）
        #: * ``t_tr``    —— 译文回来的时刻
        #: * ``t_submit``—— 交给 TTS 队列的时刻（放行器压了多久）
        self._timings: list[dict] = []
        self._workers: list[threading.Thread] = []
        self._gate_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 取「当前公开文本」的回调（播前复核用）—— 由 ASR 服务注入。
        self._text_provider: Optional[Callable[[str], str]] = None
        #: 广播回调（WS 帧）—— 由 ASR 服务注入。
        self._broadcaster: Optional[Callable[[dict], None]] = None
        #: 最近一次送进 TTS 的句子的 src_end_sec（跟播滞后口径用）。
        self._last_src_end = 0.0
        #: **每个已提交 job 的「源话时长」**（按 seq 索引）——
        #: 用来跟队列回传的「译文音频时长」配对 ⇒ 算出**译文长度比**。
        self._src_by_seq: dict = {}
        #: 上一次改变速的时刻（变速太频繁 = 管道重开太频繁 = 断音）。
        self._tempo_changed_at = 0.0
        #: **播放时钟是否已接上**（跟播模式 = True）—— 用于避免重复设置。
        self._clocked = False
        #: 当前这条流（播放头按流维护）。
        self._stream_id = ""
        #: **主事件循环** —— 广播必须切回它（``asyncio.Queue`` 不是线程安全
        #: 的；同传引擎是**工作线程**）。由 API 层在起同传时注入。
        self._loop = None
        #: 本机 MT（翻译模型）状态 —— 同传**必须**有本机实例。
        #:
        #: ⚠️ **为什么必须是「本机」**：``npu_manager`` 只认本机实例
        #: （``list_sessions(include_remote=False)``）⇒ **对端 peer 的 MT
        #: 不算数**。而「字幕翻译」的开关（``POST /api/qwen3-asr/
        #: translate/disable``）会把本机 MT **停掉** ⇒ 板上只剩 peer 的
        #: MT ⇒ 同传立刻报「NPU 翻译未启用（swcap_translate 没有正在
        #: 运行的实例）」⇒ **一句都译不出来**（实测踩过）。
        self._mt_ready = False
        self._mt_waiting = False
        self._mt_error = ""
        #: **本机 TTS** 的状态 —— 同传的语音必须**本机**出（没有本机实例
        #: ⇒ ``get_session`` 会退回 LAN 对端 ⇒ 语音走网络 ⇒ 慢/易失败）。
        self._tts_ready = False
        self._tts_waiting = False
        self._tts_error = ""

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def start(self, *, mode: str = "steady", gate: str = "now",
              target_lang: str = "zh-CN", speaker: Optional[str] = None,
              ref_speaker: Optional[str] = None,
              volume: Optional[float] = None, source: str = "live") -> dict:
        with self._lock:
            self._mode = mode if mode in ("fast", "steady") else "steady"
            self._gate_mode = gate if gate in ("now", "follow") else "now"
            self._target_lang = target_lang or "zh-CN"
            # ⚠️ **音色 / 音量只在「传了」的时候才覆盖** ——
            #
            # 之前是 ``speaker or ""`` / ``ref_speaker or ""`` ⇒
            # 「开始同传」这个请求**没带音色字段**时会把用户刚在面板上
            # 选好的音色**抹成空** ⇒ 之后每句都没有音色条件 ⇒
            # **每句话音色都不一样**（实测踩过：先用 settings 选了
            # news_female，再 start ⇒ 音色被清掉 ⇒ worker 日志里
            # 没有 ``[speaker] using voice-clone`` 那行）。
            if speaker is not None:
                self._speaker = speaker
            if ref_speaker is not None:
                self._ref_speaker = ref_speaker
            if volume is not None:
                self._volume = float(volume)
            self._source = source or "live"
            if not self._running:
                # ⚠️ **新会话 ⇒ 放行器记账清零**（见 ``Gate.reset``）。
                #
                # 少了这一步 ⇒ 上一轮「媒体真的播过」（``_ph_moved=True``）
                # 会**残留**到这一轮 ⇒ 这一轮若是「自动播放被拦」（媒体
                # **从没播过**）⇒ 兜底放行**永远不触发** ⇒ 句子被永久
                # 压住 ⇒ **有翻译、一声不响**（用户实测报的就是这个）。
                self._gate.reset()
            self._gate.set_mode(self._gate_mode)
            self._apply_clock()
            # ⚠️ **新会话 ⇒ 变速复位**（棘轮只在一轮内生效）⇒ 新的一轮从
            # TEMPO_MIN 起步，再按实测速率往上提。
            if not self._running:
                self._counters.tempo = TEMPO_DEFAULT
            if not self._running:
                self._running = True
                self._stop.clear()
                self._start_workers()
        self._ensure_translate_model()
        self._ensure_tts_model()
        # ⚠️ **每次 start 都补记 autostart**（MT + TTS）——
        #
        # 为什么不能只在「把模型拉起来的那一次」记：
        # ``_ensure_tts_model`` / ``_ensure_translate_model`` 在**模型已经
        # 在跑**时会提前返回 ⇒ 那一轮**不会**记 ⇒ 而「模型在跑但 autostart
        # 里没有」正是「回首页清过 autostart」的常态 ⇒ 重启后照样没。
        #
        # ⇒ 这里**同步**补记（只是一次配置写入，配置没变就不落盘）。
        self._record_dep_autostart()
        self._broadcast({"type": "interp_status", **self.status()})
        return self.status()

    def _record_dep_autostart(self) -> None:
        """把**正在跑的**本机 MT / TTS 记进 ``compute.autostart``。

        ⚠️ **为什么同传要管 autostart**（实测 2026-11-22）：

        * 用户点「回首页」⇒ ``DELETE /api/compute/active-app`` ⇒
          ``clear_active_app`` 把 ``compute.autostart`` **整个清空**；
        * 再进 ASR 卡 ⇒ ``set_active_app`` 只按**该卡声明的 capability**
          加回 ``qwen3-asr``（ASR 卡只声明 ``swcap_audio_asr``）⇒
          **MT 和 TTS 都不在 autostart 里**；
        * ⇒ 重启后本机没有 TTS ⇒ **语音走了对端板子**（PCM 走网络）；
        * ⇒ MT 也要重新加载（~22 s）⇒ **同传第一句等 24 s**
          （实测「等 MT 就绪」= 24.1 s）。

        ⇒ 同传是**唯一**同时需要这两个模型的功能 ⇒ 由它补记最合适。
        """
        try:
            from app.core.compute.registry import get_registry
            reg = get_registry()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 取 registry 失败（不记 autostart）: %s", exc)
            return
        # TTS
        try:
            self._record_tts_autostart(reg, "swcap_text_to_speech")
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 记 TTS autostart 失败: %s", exc)
        # MT（翻译模型）
        try:
            from app.core.translate.npu_manager import (
                get_npu_translate_instances,
            )
            mgr = get_npu_translate_instances()
            for e in (reg.list_running_by_capability("swcap_translate")
                      or []):
                if str(e.get("source") or "") not in ("spawned",
                                                      "adopted_local"):
                    continue
                mid = str(e.get("model_id") or "")
                npu = e.get("npu_index")
                if not mid or npu is None:
                    continue
                if reg.record_autostart_entry(mid, npu):
                    LOG.info("interp: 本机 MT（%s @ npu%s）已写入 "
                             "compute.autostart", mid, npu)
                break
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 记 MT autostart 失败: %s", exc)

    # ------------------------------------------------------------------ #
    # TTS —— 同传的**硬依赖**（语音必须**本机**出）
    # ------------------------------------------------------------------ #
    def _ensure_tts_model(self) -> None:
        """确保**本机**有一个跑着的 TTS 实例（**后台**，不挡 HTTP 响应）。

        ⚠️ **为什么必须本机**（实测，用户报「只播放了第一句」，2026-11-22）：

        * ``get_session("swcap_text_to_speech")`` 是**本机优先**，但
          **本机没有就退回 LAN peer** ⇒ 语音要走网络（PCM 回传）
          ⇒ 延迟大、还可能失败 ⇒ 同传听起来「只播了一句」；
        * 现场证据：用户的浏览器点过「回首页」（``DELETE
          /api/compute/active-app``）⇒ **autostart 被清空** ⇒ 之后重新
          进卡只加回了 ASR + 翻译 ⇒ **TTS 没进 autostart** ⇒ 重启后
          本机没有 TTS 实例 ⇒ **语音走了对端板子**。

        ⇒ 同传起的时候顺手把本机 TTS 拉起来（跟 MT 一个套路）。
        """
        with self._lock:
            if self._tts_ready or self._tts_waiting:
                return

        def _run() -> None:
            try:
                from app.core.compute.registry import get_registry
                reg = get_registry()
                cap = "swcap_text_to_speech"
                # ① 本机已经有能出声的实例 ⇒ 够了
                # ⚠️ **但也要记 autostart** —— 「已经有了」多半是用户刚在
                # TTS 卡上跑过（那时 autostart 里有它），也可能是**重启后
                # 别的路径拉起来的**（那时 autostart 里没有）⇒ 不记的话
                # 下次重启又没了 ⇒ 语音又走对端板子。
                if reg.list_sessions(cap, include_remote=False):
                    self._record_tts_autostart(reg, cap)
                    with self._lock:
                        self._tts_ready = True
                    return
                with self._lock:
                    self._tts_waiting = True
                # ② 本机有一个正在加载的 ⇒ 等它（别再起一份抢显存）
                if reg.pending_models_for_capability(cap):
                    if self._wait_tts_ready(reg, cap, timeout=60.0):
                        self._record_tts_autostart(reg, cap)
                        with self._lock:
                            self._tts_ready = True
                            self._tts_waiting = False
                        return
                # ③ 起一个本机的（allow_adopt=False ⇒ 不许拿对端的）
                model_id = ""
                for m in reg.list_supported_models():
                    if cap in (m.get("capability_types") or []) and m.get("exists"):
                        model_id = str(m.get("id") or "")
                        break
                if not model_id:
                    raise RuntimeError("本机没有安装能出声的 TTS 模型")
                card = self._pick_tts_card(reg, model_id)
                bad = reg.npu_card_error(card)
                if bad:
                    raise RuntimeError(bad)
                with reg.model_op_lock(model_id):
                    inst = reg.ensure_worker_for_model(
                        model_id, allow_adopt=False, npu_index=card,
                        eager_load=False, replica=0,
                    )
                    reg.bind_instance(inst, card)
                if not self._wait_tts_ready(reg, cap, timeout=90.0):
                    raise RuntimeError("本机 TTS 加载超时")
                self._record_tts_autostart(reg, cap, model_id=model_id,
                                           npu_index=card)
                with self._lock:
                    self._tts_ready = True
                    self._tts_waiting = False
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._tts_waiting = False
                    self._tts_error = str(exc)[:180]
                LOG.warning("interp: 本机 TTS 启动失败: %s", exc)

        threading.Thread(target=_run, name="interp-tts-ensure",
                         daemon=True).start()

    @staticmethod
    def _record_tts_autostart(reg, cap: str, model_id: str = "",
                              npu_index=None) -> bool:
        """把本机 TTS 记进 ``compute.autostart``（**重启后自动回来**）。

        ⚠️ **为什么必须记**（实测，用户报「只播放了第一句」+「滞后 15 秒」，
        2026-11-22）：

        * 用户点「回首页」⇒ ``DELETE /api/compute/active-app`` ⇒
          ``clear_active_app`` 把 ``compute.autostart`` **整个清空**；
        * 再进 ASR 卡 ⇒ ``set_active_app`` 只按**该卡声明的 capability**
          加回 ``qwen3-asr``（ASR 卡只声明 ``swcap_audio_asr``）⇒
          **MT 和 TTS 都不在 autostart 里**了；
        * ⇒ 重启后本机没有 TTS ⇒ **语音走了对端板子**；MT 也要重新加载
          （~22 s）⇒ **同传第一句等 24 s**（实测「等 MT 就绪」24.1 s）。

        ⇒ ``record_autostart_entry`` 就是为「模型由宿主侧起、但卡片没声明
        那个能力」的场景写的（MT 走的是同一条路）。

        ⚠️ **只记本机实例**（``source`` 必须是 ``spawned`` /
        ``adopted_local``）—— peer 的实例记进来等于「下次开机等一个空头」。
        """
        if not model_id:
            # 没给 ⇒ 从在跑的实例里找（分支①/②：TTS 早就在跑了）
            try:
                for e in reg.list_running_by_capability(cap) or []:
                    if str(e.get("source") or "") not in ("spawned",
                                                          "adopted_local"):
                        continue
                    model_id = str(e.get("model_id") or "")
                    npu_index = e.get("npu_index")
                    break
            except Exception as exc:  # noqa: BLE001
                LOG.debug("interp: 找本机 TTS 实例失败: %s", exc)
        if not model_id:
            return False
        if npu_index is None:
            return False
        try:
            if reg.record_autostart_entry(model_id, npu_index):
                LOG.info("interp: 本机 TTS（%s @ npu%s）已写入 "
                         "compute.autostart", model_id, npu_index)
                return True
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 记录 TTS autostart 失败: %s", exc)
        return False

    @staticmethod
    def _wait_tts_ready(reg, cap: str, timeout: float = 60.0) -> bool:
        """等本机的 TTS 实例真的 ready（模型加载完）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if reg.list_sessions(cap, include_remote=False):
                return True
            time.sleep(0.5)
        return bool(reg.list_sessions(cap, include_remote=False))

    @staticmethod
    def _pick_tts_card(reg, model_id: str) -> int:
        """给 TTS 挑一张卡：**优先不是感知链那张**（ASR + 翻译在那儿）。"""
        try:
            from app.core.compute.card_roles import (
                fits,
                memory_of,
                perception_anchor_card,
            )
            cards = sorted(int(c) for c in reg.model_cards())
            if not cards:
                return 0
            per, _total, _remote = reg.local_memory_usage()
            need = memory_of(model_id)
            anchor = perception_anchor_card(reg)
            for c in cards:
                if c != anchor and fits(int((per or {}).get(c, 0)), need):
                    return c
            for c in cards:
                if fits(int((per or {}).get(c, 0)), need):
                    return c
            return cards[0]
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------ #
    # 翻译模型（MT）—— 同传的**硬依赖**
    # ------------------------------------------------------------------ #
    def _ensure_translate_model(self) -> None:
        """确保**本机**有一个跑着的 MT 实例（**后台**，不挡 HTTP 响应）。

        ⚠️ **为什么必须自己起**：

        * ``npu_manager`` 只认**本机**实例 ⇒ 对端 peer 的 MT 不算数；
        * 字幕翻译的开关会把本机 MT 停掉 ⇒ 同传会突然「翻译未启用」；
        * ``ensure()`` 要等模型加载完（**~30 s**）⇒ 不能同步挡着
          ``POST /interp/start``（前端会以为卡死了）。

        ⇒ 起一个后台线程去 ``ensure()``；翻译那一侧（``_handle``）会
        **等它就绪**再翻 ⇒ 冷启动那 30 s 里产生的句子**不会丢**。
        """
        with self._lock:
            if self._mt_ready or self._mt_waiting:
                return

        def _run() -> None:
            try:
                from app.core.translate.npu_manager import (
                    get_npu_translate_instances,
                )
                mgr = get_npu_translate_instances()
                if mgr.sessions():
                    with self._lock:
                        self._mt_ready = True
                    return
                with self._lock:
                    self._mt_waiting = True
                mgr.ensure()
                with self._lock:
                    self._mt_ready = True
                    self._mt_waiting = False
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._mt_waiting = False
                    self._mt_error = str(exc)[:180]
                LOG.warning("interp: 翻译模型启动失败: %s", exc)
                self._broadcast({"type": "interp_error",
                                 "reason": "翻译模型不可用：%s" % str(exc)[:160]})

        threading.Thread(target=_run, name="interp-mt-ensure",
                         daemon=True).start()

    def _wait_translate_model(self, timeout: float = 45.0) -> bool:
        """等本机 MT 就绪（冷启动 ~30 s）。

        ⇒ **返回 False 时这一句只能降级为字幕**（但**不丢**：原文照常
        显示 + 明确报错）。
        """
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self._mt_ready:
                    return True
                if self._mt_error:
                    return False
                waiting = self._mt_waiting
            if not waiting:
                # 没在起、也没就绪 ⇒ 探一次实况（可能别人刚起好，
                # 或者字幕翻译那边把 MT 起回来了）。
                try:
                    from app.core.translate.npu_manager import (
                        get_npu_translate_instances,
                    )
                    if get_npu_translate_instances().sessions():
                        with self._lock:
                            self._mt_ready = True
                        return True
                except Exception:  # noqa: BLE001
                    pass
                return False
            if time.time() >= deadline or not self.running:
                return False
            time.sleep(0.4)

    def stop(self) -> dict:
        """停引擎 + 清 TTS 队列（**正在合成的那句跑完**）。"""
        with self._lock:
            self._running = False
            self._stop.set()
            self._pending.clear()
            self._armed.clear()
            self._ready_since.clear()
            self._cache.clear()
            self._segmenters.clear()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        for w in self._workers:
            w.join(timeout=2.0)
        self._workers = []
        try:
            from app.core.audio.tts_bridge import queue_clear
            queue_clear()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 清 TTS 队列失败: %s", exc)
        self._broadcast({"type": "interp_status", **self.status()})
        return self.status()

    def settings(self, *, mode: Optional[str] = None,
                 gate: Optional[str] = None,
                 target_lang: Optional[str] = None,
                 speaker: Optional[str] = None,
                 ref_speaker: Optional[str] = None,
                 volume: Optional[float] = None) -> dict:
        """改设置（**不重启引擎**）。"""
        with self._lock:
            if mode is not None:
                self._mode = mode if mode in ("fast", "steady") else self._mode
            if gate is not None:
                self._gate_mode = gate if gate in ("now", "follow") else self._gate_mode
                self._gate.set_mode(self._gate_mode)
                self._apply_clock()
            if target_lang is not None:
                self._target_lang = target_lang
            if speaker is not None:
                self._speaker = speaker
            if ref_speaker is not None:
                self._ref_speaker = ref_speaker
            if volume is not None:
                self._volume = float(volume)
        self._broadcast({"type": "interp_status", **self.status()})
        return self.status()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def mode(self) -> str:
        """**切句档位**（``fast`` | ``steady``）。

        ⚠️ 必须有**公开**的读法 —— 调用方（ASR 服务的切句器）要按档位
        决定「强制切句长度」，写 ``engine.mode`` 时若引擎只有私有
        ``_mode`` 就会 ``AttributeError`` ⇒ 而那条异常在 best-effort 的
        ``except Exception`` 里只记 DEBUG ⇒ **整条同传链路静默失效**
        （踩过：文件源同传一条句子都没进来，journal 里 0 行日志）。
        """
        with self._lock:
            return self._mode

    @property
    def gate_mode(self) -> str:
        """**播放时机**（``now`` | ``follow``）。"""
        with self._lock:
            return self._gate_mode

    # ------------------------------------------------------------------ #
    # 源钩子（由 ASR 服务调用）
    # ------------------------------------------------------------------ #
    def on_endpoint(self, text: str, lang: str, stream_id: str,
                    ts: Optional[float] = None) -> None:
        """**稳档**（实时流）：endpoint 帧 ⇒ 成句。

        ⚠️ **实时流的稳档现在走「标点断句」**（见 :meth:`on_segment`）——
        这个入口只保留给「整个会话的最终文本」那种一次性场景。
        """
        if not self.running:
            return
        text = (text or "").strip()
        if not text:
            return
        self._push(text, lang, stream_id, "steady", ts)

    def on_segment(self, text: str, lang: str, stream_id: str,
                   ts: Optional[float] = None,
                   mode: str = "steady") -> None:
        """**已成句**的源文（稳档：标点断句切出来的**一句**）。

        ⚠️ 和 :meth:`on_endpoint` 的区别：**这个是「一句」**，那个是「整个
        会话的最终文本」⇒ 用错会**把同一段话播两遍**。
        """
        if not self.running:
            return
        text = (text or "").strip()
        if not text:
            return
        self._push(text, lang, stream_id, mode, ts)

    def on_unfixed(self, unfixed: str, lang: str, stream_id: str,
                   ts: Optional[float] = None) -> None:
        """**快档**（实时流）：``unfixed`` 的稳定前缀成句。"""
        if not self.running:
            return
        with self._lock:
            if self._mode != "fast":
                return
            seger = self._segmenters.get(stream_id)
            if seger is None:
                seger = StablePrefixSegmenter()
                self._segmenters[stream_id] = seger
        sentence = seger.feed(unfixed)
        if sentence:
            self._push(sentence, lang, stream_id, "fast", ts)

    def on_file_segment(self, text: str, seq: int, start_sec: float,
                        end_sec: float, stream_id: str,
                        ts: Optional[float] = None, lang: str = "") -> None:
        """**文件 / 视频路径**：切好的句子（带时间轴）。

        ⚠️ ``lang`` —— **这一轮 ASR 报的语种**（如 ``English``）。
        文件路径的语种**只有最后一帧** ``asr_final`` 才最终确定 ⇒
        句子在识别过程中就被切出来、就要送去翻译 ⇒ **不能等
        ``asr_final``**（等了就来不及了）⇒ 用**每轮**的语种
        （``asr_round`` 里就有 ``language``）。
        """
        if not self.running:
            return
        text = (text or "").strip()
        if not text:
            return
        seg = InterpSegment(
            index=self._next_index(),
            text=text,
            lang=lang or "",
            stream_id=stream_id,
            mode=self._mode,
            src_start_sec=float(start_sec or -1.0),
            src_end_sec=float(end_sec or -1.0),
            created_ts=ts if ts is not None else time.time(),
            origin="file",
        )
        self._set_stream(stream_id)
        self._enqueue(seg)

    def on_new_stream(self, stream_id: str) -> None:
        """**新一轮识别**（新的 ``stream_id``）⇒ 上一轮的待播/在队内容作废。

        ⚠️ **为什么必须有**（实测，用户报「ASR+翻译都结束了，同声传译才
        播放出声音，显示滞后 15 秒」，2026-11-22）：

        * 用户在浏览器里**又点了一次「开始识别」** ⇒ 同一个文件被**再转写
          一遍** ⇒ **同一批句子再进一次同传队列**；
        * 实测一轮 4 句的文件，按 3~4 次「开始识别」⇒ 同传收到 **14 句**
          （其中 **10 句是重复的**）⇒ TTS 队列里堆了 **3.5 倍**的音频
          （实测 35.6 s vs 源话 14 s）⇒ 队列必然积压 ⇒ **译文越播越落后**，
          面板显示「滞后 13.9 秒」；
        * ⇒ **每一轮重复识别都在往队列里灌一份重复内容** ⇒ 越点越滞后。

        ⇒ 新一轮识别 ⇒ **清掉**：
          ① 同传自己的待播队列（放行器压着的）；
          ② 翻译队列（还没翻完的）；
          ③ TTS 队列（上一轮已经排进去的译文）；
          ④ 放行器记账（流 id 变了 ⇒ 旧播放头对不上新句子）。

        ⚠️ **不清 TTS 队列会怎样**：上一轮的译文会**接着播完**（用户听到
        的是「旧译文 + 新译文」叠着）；清了 ⇒ 只播新一轮的。
        """
        with self._lock:
            self._pending.clear()
            self._armed.clear()
            self._ready_since.clear()
            # ⚠️ **重播缓存也要清** —— 新一轮识别 = **另一段内容**
            # （视频源拖动后重开识别就是这种情况）⇒ 旧位置的译文**不该**
            # 再被重播（否则用户听到的是「上一段」的译文）。
            self._cache.clear()
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
        # ⚠️ **放行器换流** —— 流 id 变了 ⇒ 旧的播放头 / 「播放头走过」
        # 对新句子没有意义；但**「源已结束」要留着**（媒体还是那个媒体，
        # 见 ``Gate.reset_stream`` 的说明）。
        self._gate.reset_stream()
        self._gate.set_mode(self._gate_mode)
        # TTS 队列（插件侧的全局队列）—— 上一轮的译文作废。
        try:
            from app.core.audio.tts_bridge import queue_clear
            queue_clear()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 新一轮识别清 TTS 队列失败: %s", exc)
        LOG.info("interp: 新一轮识别（%s）⇒ 上一轮的待播/在队译文已作废",
                 stream_id)

    def on_playhead(self, stream_id: str, playhead_sec: float,
                    playing: bool, ended: bool = False,
                    failed: bool = False) -> dict:
        """前端上报播放头（250 ms 一次）。

        ⚠️ ``ended`` / ``failed`` = **源不会再往前走**（媒体播完 / 媒体
        加载失败）⇒ 放行器**不再等**（不然剩下的句子被永久压住 ⇒
        「只播放了第一句」，实测踩过）。
        """
        self._set_stream(stream_id)
        seek = self._gate.update_playhead(stream_id, playhead_sec, playing,
                                           ended, failed)
        info = self._gate.last_seek() if seek else None
        if seek and info:
            self._resync_seek(info)
            self._broadcast({"type": "interp_status", **self.status()})
        return {"ok": True, "seek": bool(seek),
                "seek_dir": (info or {}).get("dir", "")}

    # ------------------------------------------------------------------ #
    # **seek 之后的重同步**
    # ------------------------------------------------------------------ #
    def _resync_seek(self, info: dict) -> None:
        """**用户拖了进度条** ⇒ 把整条链路对到新位置。

        ⚠️⚠️ **为什么必须做**（实测 2026-11-22，用户报「『含音轨的视频』
        播放的时候，字幕在实时翻译，但是多次拖动进度条，没有和字幕的
        翻译同声播放出来」）—— 实测（``tools/probes/interp/m23_seek_sync.py``，
        17 句里 **15 句**错位）：

        * **前跳**（播放头 0 → 60 s）：跳过的那些句子**还在待播队列里**
          ⇒ 放行器一看播放头已经越过它们 ⇒ **全部放行** ⇒ TTS 队列里
          灌进几十秒的**陈旧译文** ⇒ 接下来讲的全是**用户已经跳过的
          内容** ⇒ 实测错位 **+27~+35 秒**（播 29~47 s 的内容，播放头
          却在 61~74 s）；
        * **后跳**（播放头 60 → 20 s）：FIFO 里**旧位置**的译文还占着队头
          ⇒ 播放时钟说「还没到」⇒ 播的是**用户还没走到的位置**的内容
          ⇒ 实测错位 **−20~−31 秒**（播 48~77 s 的内容，播放头在 27~46 s）。

        ⇒ 所以 seek 之后要做三件事：

        ① **丢掉新位置之前的待播句**（用户跳过了 ⇒ 不该再播）；
        ② **冲掉队列里的陈旧音频**（按位置丢 ⇒ 见
           ``TTSQueue.flush_by_position``）**并复位播放时钟**；
        ③ **重新武装新位置之后的句子**（从**重播缓存**里取）⇒ 让放行器
           按播放头的节奏**重新放出来** ⇒ 译文跟得上字幕。

        ⚠️ **③ 为什么必须有缓存**：用户**拖回去**时，那些句子早就播过了
        （放行器放行过 ⇒ 不在 ``_pending`` 里）⇒ 没有缓存就**再也说不
        出来** ⇒ 用户看到的字幕有译文、却**一声不响**（这正是用户报的
        「没有和字幕的翻译同声播放出来」）。
        """
        pos = float(info.get("to") or 0.0)
        direction = str(info.get("dir") or "")
        dropped = 0
        replayed = 0
        with self._lock:
            # ① 新位置**之前**的待播句 ⇒ 丢（用户自己跳过的，不算「丢」）
            keep: list[InterpSegment] = []
            for seg in self._pending:
                if seg.src_end_sec >= 0.0 and seg.src_end_sec < (pos - SEEK_EPS_SEC):
                    dropped += 1
                    self._armed.discard(seg.index)
                    # ⚠️ **合并窗口的计时也要清** —— 这句被丢了 ⇒ 不能
                    # 再参与「等够了就放行」的判定（否则会提前放行一批）。
                    self._ready_since.pop(seg.index, None)
                else:
                    keep.append(seg)
            # ③ **重新武装**新位置**之后**的句子（重播缓存里取）——
            #    已经在等的（``_armed``）不重复入队。
            for seg in self._cache:
                if seg.index in self._armed:
                    continue
                if seg.src_end_sec < 0.0:
                    continue
                if seg.src_end_sec < (pos - SEEK_EPS_SEC):
                    continue          # 新位置之前 ⇒ 用户跳过了 ⇒ 不重播
                keep.append(seg)
                self._armed.add(seg.index)
                replayed += 1
            # ⚠️ **按位置排序** —— 重播的句子在**前面**（位置更靠前），
            # 不排序的话放行器会先放后面的 ⇒ **顺序乱**。
            keep.sort(key=lambda s: (s.src_start_sec if s.src_start_sec >= 0
                                     else 0.0))
            self._pending = keep
            self._counters.skipped_seek += dropped
            self._counters.replayed += replayed
        # ② 冲掉队列里的陈旧音频（并复位播放时钟）
        flushed = 0
        try:
            from app.core.audio.tts_bridge import queue_flush_position
            res = queue_flush_position(pos, direction) or {}
            flushed = int(res.get("flushed") or 0)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: seek 冲掉陈旧音频失败: %s", exc)
        LOG.info("interp: 用户 seek（%s → %.1f s，%s）⇒ 丢掉 %d 句被跳过的、"
                 "重播 %d 句、冲掉 %d 段陈旧音频",
                 direction, pos, "前跳" if direction == "fwd" else "后跳",
                 dropped, replayed, flushed)
        with self._lock:
            #: 给面板看的**回执**（前端据此提示「已重新对齐」）。
            self._last_seek_info = {
                "dir": direction,
                "to": round(pos, 2),
                "dropped": dropped,
                "replayed": replayed,
                "flushed": flushed,
                "ts": time.time(),
            }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _next_index(self) -> int:
        with self._lock:
            self._seg_index += 1
            return self._seg_index

    def _set_stream(self, stream_id: str) -> None:
        with self._lock:
            if stream_id:
                self._stream_id = stream_id

    def _push(self, text: str, lang: str, stream_id: str, mode: str,
              ts: Optional[float]) -> None:
        self._set_stream(stream_id)
        seg = InterpSegment(
            index=self._next_index(),
            text=text,
            lang=lang or "",
            stream_id=stream_id,
            mode=mode,
            created_ts=ts if ts is not None else time.time(),
            origin="live",
        )
        self._enqueue(seg)

    def _enqueue(self, seg: InterpSegment) -> None:
        """入队（队满 ⇒ **合并队尾**，内容不丢）。"""
        with self._lock:
            if self._queue.qsize() >= INTERP_QUEUE_MAX:
                try:
                    tail = self._queue.get_nowait()
                    seg.text = tail.text + " " + seg.text
                    seg.index = tail.index
                    self._counters.merged += 1
                except queue.Empty:
                    pass
            self._counters.segments += 1
        try:
            self._queue.put_nowait(seg)
        except queue.Full:
            # 还是满 ⇒ 丢掉**最新**这句（极端情况；计数让面板知道）
            with self._lock:
                self._counters.failed += 1
            LOG.warning("interp: 待译队列满，丢了一句")
            return
        self._broadcast({
            "type": "interp_segment",
            "index": seg.index,
            "source": seg.origin,
            "text": seg.text,
            "lang": seg.lang,
            "mode": seg.mode,
            "start_sec": seg.src_start_sec,
            "end_sec": seg.src_end_sec,
        })

    def _start_workers(self) -> None:
        for i in range(INTERP_WORKERS):
            t = threading.Thread(target=self._translate_worker,
                                 name=f"interp-tr-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        self._gate_thread = threading.Thread(target=self._gate_worker,
                                            name="interp-gate", daemon=True)
        self._gate_thread.start()

    # ------------------------------------------------------------------ #
    # 翻译 → 复核 → 放行 → 播报
    # ------------------------------------------------------------------ #
    def _translate_worker(self) -> None:
        while not self._stop.is_set():
            try:
                seg = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle(seg)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("interp: 处理失败: %s", exc)
                with self._lock:
                    self._counters.failed += 1
                    self._counters.last_error = str(exc)[:200]
            finally:
                self._queue.task_done()

    def _handle(self, seg: InterpSegment) -> None:
        # ⚠️⚠️ **只有标点的碎片不翻**（实测 2026-11-24，用户报「译文里出现
        # ``Translate the following text from zh to …``」）：模型**没东西可翻**
        # 时会把**我们发给它的指令**原样吐回来 ⇒ 那是**胡话** ⇒
        # 质量闸只查文种拦不住（指令也是拉丁字母）⇒ 指令被当成译文 ⇒
        # **被 TTS 念出来**。
        #
        # ⇒ 从**源头**掐掉：这种碎片本来就不该出声 ⇒ 直接跳过
        # （**连 MT 都不用等** —— 否则光等模型就白等好几秒）。
        try:
            from app.core.translate.npu_provider import has_translatable_content
            if not has_translatable_content(seg.text):
                LOG.info("interp: 跳过没有可译内容的片段（%r）",
                         (seg.text or "")[:20])
                return
        except Exception:  # noqa: BLE001 — 判据失败不影响识别
            pass
        # ⚠️ **先等翻译模型就绪** —— 冷启动时 MT 要 ~30 s 才起来
        # （本机实例被停掉又重新拉起）。等它 ⇒ 这 30 s 里产生的句子
        # **不丢**（不等 ⇒ 立刻报「翻译未启用」⇒ 整段同传哑掉）。
        #
        # ⚠️ **计时拆分**：`wait_mt`（等模型就绪）与 `infer`（真推理）
        # 必须分开记 —— 否则「MT 冷启动 24 s」会被误算成「翻译慢」。
        seg.dequeued_at = time.time()
        _t0 = time.time()
        mt_ok = self._wait_translate_model()
        seg.wait_mt_sec = time.time() - _t0
        if not mt_ok:
            with self._lock:
                self._counters.failed += 1
                self._counters.last_error = (
                    self._mt_error or "翻译模型未就绪（本机没有 MT 实例）")
            self._broadcast({"type": "interp_error", "index": seg.index,
                             "reason": self._last_error()})
            return
        translated = ""
        ok = False
        attempt = 0
        mt_retried = False
        text = seg.text
        _infer_total = 0.0
        while attempt < RETRY_LIMIT:
            attempt += 1
            try:
                _t1 = time.time()
                translated = self._translate(text, seg.lang)
                _infer_total += time.time() - _t1
                ok = True
            except Exception as exc:  # noqa: BLE001
                _infer_total += time.time() - _t1
                # 「翻译未启用」⇒ **本机 MT 被停掉了**（字幕翻译的开关会
                # 停它）⇒ 重新拉起 + 等它就绪 + 重试这一句（**不丢**）。
                if not mt_retried and self._is_mt_missing(exc):
                    mt_retried = True
                    with self._lock:
                        self._mt_ready = False
                        self._mt_waiting = False
                    self._ensure_translate_model()
                    _t2 = time.time()
                    _ok2 = self._wait_translate_model()
                    seg.wait_mt_sec += time.time() - _t2
                    if _ok2:
                        continue
                with self._lock:
                    self._counters.failed += 1
                    self._counters.last_error = str(exc)[:200]
                break
            if seg.origin != "live":
                break
            # 播前复核：译文回来前，源文可能被模型改写
            if not self._recheck_ok(seg, text):
                with self._lock:
                    self._counters.retried += 1
                latest = self._current_text(seg.stream_id).strip()
                if not latest or latest == text:
                    break            # 没有更「新」的文本 ⇒ 只能这样了
                text = latest
                seg.text = latest
                continue
            break
        if not ok:
            # 翻译失败 ⇒ 降级为仅字幕（**不算丢**）
            self._broadcast({"type": "interp_error", "index": seg.index,
                             "reason": self._last_error()})
            return
        seg.translated = translated
        seg.translated_at = time.time()
        seg.infer_sec = _infer_total
        seg.attempts = attempt
        # ⚠️ **计时记录在这里就建好**（不等放行）⇒ 放行器压住的那段
        # 也能被看见（否则「翻译完了但迟迟不出声」会看不到放行耗时）。
        seg.timing = {
            "index": seg.index,
            "text": seg.text[:40],
            "tr": seg.translated[:40],
            "src_start": round(seg.src_start_sec, 2),
            "src_end": round(seg.src_end_sec, 2),
            "t_seg": round(seg.created_ts, 3),
            "t_deq": round(seg.dequeued_at, 3),
            "t_tr": round(seg.translated_at, 3),
            "t_submit": 0.0,
            "wait_mt": round(seg.wait_mt_sec, 2),
            "infer": round(seg.infer_sec, 2),
            "attempts": seg.attempts,
        }
        with self._lock:
            self._timings.append(seg.timing)
            if len(self._timings) > 60:
                del self._timings[:-60]
            self._counters.translated += 1
            # ⚠️ **进「重播缓存」** —— seek 之后要靠它把「新位置之后」的
            # 句子**重新武装**（用户拖回去 ⇒ 那些句子早就播过了 ⇒ 没有
            # 缓存就再也说不出来 ⇒ 字幕有译文却一声不响）。
            if seg.origin != "live" and seg.src_end_sec >= 0.0:
                self._cache_put(seg)
        # ⚠️ **一律进「待放行」队列**（不再就地提交）——
        # ⇒ 放行线程能**成批**放行 ⇒ 几句译文合成成一段 ⇒ 摊薄 TTS
        # 每句 ~2.9 s 的固定开销（见 :data:`INTERP_MERGE_HOLD_SEC`）。
        with self._lock:
            self._pending.append(seg)
            self._armed.add(seg.index)
            # ⚠️ **记下「这句等放行多久了」的起点** —— 攒批的计时基准
            # （跟播模式按「内容量」攒 ⇒ 兜底上界按它算；实时模式按它
            # 算合并窗口）。
            self._ready_since.setdefault(seg.index, time.time())

    def _cache_put(self, seg: InterpSegment) -> None:
        """进「重播缓存」—— **同一处只留最新的一条**。

        ⚠️ **为什么必须去重**（实测 2026-11-23，用户报「翻译重复」）：
        同一处音频被**识别多次**（用户反复拖进度条 / 多次点「开始识别」）
        ⇒ 缓存里会有**多个副本** ⇒ seek 之后 :meth:`_resync_seek` 把它们
        **一起**重新武装 ⇒ 同一句话**播 N 遍**（实测 TTS 历史里同一段
        出现 3 次）。

        ⇒ 判据用**起点**（± :data:`SEEK_DUP_SEC` 视作同一处 —— 重新识别
        出来的时间戳会有零点几秒的偏差）⇒ **就地替换**旧的副本
        （保住顺序）⇒ 缓存里每个位置只有一条。

        ⚠️ 实时流没有位置 ⇒ 不进缓存（见调用点）。
        """
        with self._lock:
            for i, old in enumerate(self._cache):
                if abs(float(old.src_start_sec or 0.0)
                       - float(seg.src_start_sec or 0.0)) <= SEEK_DUP_SEC:
                    self._cache[i] = seg
                    break
            else:
                self._cache.append(seg)
            if len(self._cache) > SEEK_REPLAY_CACHE_MAX:
                del self._cache[:-SEEK_REPLAY_CACHE_MAX]

    def _is_mt_missing(self, exc: BaseException) -> bool:
        """这个异常是不是「本机没有 MT 实例」？"""
        msg = str(exc)
        return ("翻译未启用" in msg
                or "没有正在运行的实例" in msg
                or "swcap_translate" in msg)

    def _recheck_ok(self, seg: InterpSegment, text: str) -> bool:
        """译文对应的源文是不是**当前**公开文本的一部分。"""
        if self._text_provider is None:
            return True
        latest = self._current_text(seg.stream_id)
        if not latest:
            return True
        return text in latest

    def _current_text(self, stream_id: str) -> str:
        if self._text_provider is None:
            return ""
        try:
            return self._text_provider(stream_id) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _translate(self, text: str, lang: str) -> str:
        """翻一句（走宿主的 NPU 翻译管理器 = 同一个 MT 实例）。"""
        from app.core.translate.npu_manager import get_npu_translate_instances

        target = self._target_lang
        src = lang
        if not src:
            # 没给源语 ⇒ 用 ASR 当前语种
            src = self._current_lang()
        mgr = get_npu_translate_instances()
        out, _perf = mgr.translate_sentence(text, src, target)
        return str(out or "").strip()

    def _current_lang(self) -> str:
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
            return str(get_asr_service().snapshot().get("language") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _submit(self, segs) -> None:
        """把**一批**译文送进 TTS 队列（合成成**一段**）。

        ⚠️ **为什么是「一批」而不是「一句」**（实测 2026-11-23，M28）：
        TTS 每合成一句都有 **~2.9 s 固定开销**（首尾静音 0.55 s + 模型
        慢速起调与标点停顿）⇒ 4 句 = 17.0 s 音频里 ~9 s 是纯开销 ⇒
        长度比 k 被推到 1.20 ⇒ 产能（~0.96×）天生不够 ⇒ 滞后单调增长。

        ⇒ 这里把几句译文**拼成一段**送进去 ⇒ 开销只付一次 ⇒
        音频大幅缩短 ⇒ k 回到 1.0 上下 ⇒ 滞后不再增长、顿挫也少。

        ⚠️ **顺序必须保持**（按 **媒体位置** 排）—— 播放顺序 = 提交顺序
        = 源话顺序（用户报的「串句」就是这么来的）。

        ⚠️⚠️ **为什么按位置而不是按 index**（实测 2026-11-23）：拖动进度条
        后重开识别 ⇒ 新一段的句子 **index 更大、位置更小** ⇒ 按 index 排
        ⇒ **播放顺序 ≠ 源话顺序**。详见 :func:`_order_key`。
        """
        if isinstance(segs, InterpSegment):
            segs = [segs]
        segs = [s for s in segs if (s.translated or "").strip()]
        if not segs:
            return
        segs = sorted(segs, key=_order_key)
        first, last = segs[0], segs[-1]
        now = time.time()
        for s in segs:
            s.submitted_at = now
        tts_lang = _TTS_LANG_BY_PROVIDER.get(self._target_lang)
        if not tts_lang:
            with self._lock:
                self._counters.last_error = "目标语不支持语音"
            self._broadcast({"type": "interp_error", "index": first.index,
                             "reason": "目标语 %s 不能出声（降级为仅字幕）"
                                       % self._target_lang})
            return
        # ⚠️ **滞后的基准时刻** —— 「源话说完」到底是哪一刻：
        #
        # * **实时流**：``created_ts``（ASR 切出这句的时刻 ≈ 用户说完）。
        # * **文件源**：``created_ts`` **不能用** —— 文件是**离线一次性
        #   转写完**的 ⇒ 所有句子的 ``created_ts`` 几乎相同（= 转写完成
        #   时刻）⇒ 拿它当基准 ⇒ 面板的滞后会**随句子数单调增长**
        #   （实测 6.4 → 10.4 → 14.6 → 17.8 秒），而用户实际听到的
        #   译文与源话只错开几秒 ⇒ **面板在骗人**（用户报「滞后 15 秒」）。
        #
        #   ⇒ 文件源改用**音频时间轴** + 播放头反推的媒体零点：
        #   ``源话说完时刻 = 媒体零点 + src_end_sec``。
        src_end_ts = last.created_ts
        src_start_ts = 0.0
        if first.origin == "file" and last.src_end_sec >= 0.0:
            anchor = self._gate.media_anchor()
            if anchor > 0.0:
                src_end_ts = anchor + last.src_end_sec
                if first.src_start_sec >= 0.0:
                    src_start_ts = anchor + first.src_start_sec
        # ⚠️ **合并后的译文文本** —— 用**空格**连接（每段自带句末标点
        # ⇒ 空格只是段间的一个小停顿）。
        #
        # ⚠️⚠️ **送进 TTS 的那份要去掉停顿标点**（实测 2026-11-23，M29）：
        # 模型为每个标点留 ~0.25~1.0 秒静音 ⇒ 实测三段译文的标点停顿
        # 合计 **2.2~4.2 秒**（占音频 9%~30%）⇒ **长度比 k 从 1.10 降到
        # 0.99** ⇒ **可听滞后不再随时间增长**（原来每 1 秒源话长 0.1~0.2 秒
        # ⇒ 5 分钟素材 ⇒ 落后 30~60 秒 ⇒ 用户报「滞后非常多」）。
        # ⇒ **字幕不受影响**（字幕走 ``seg.translated``，标点原样保留）。
        text = " ".join(_tts_text(s.translated) for s in segs)
        try:
            from app.core.audio.tts_bridge import (
                TTSUnavailable,
                submit_queued,
            )
            res = submit_queued(
                text=text,
                language=tts_lang,
                speaker=self._speaker,
                # ⚠️ 参考音色与内置音色是**两个不同的 SDK 参数** ⇒ 分开传。
                # （混用 ⇒ SDK 跳过音色条件 ⇒ 每句话音色都不一样。）
                ref_speaker=self._ref_speaker,
                volume=self._volume,
                tempo=self._counters.tempo,
                # ⚠️ **文件源也要给** —— 之前文件源传 0.0 ⇒ 队列不算滞后
                # ⇒ 面板只能显示上一轮的残留值（实测 18.48 秒误导）。
                src_end_ts=src_end_ts,
                # ⚠️ **源话开口的时刻** —— 用来算**可听滞后**
                # （译文出声 − 源话开口）⇒ 用户真正感受到的那一口。
                src_start_ts=src_start_ts,
                # ⚠️ **源话在媒体时间轴上的位置** —— 跟播的**播放时钟**
                # 靠它决定「这一句什么时候放出来」。
                # ⚠️ **合并后取第一句的位置** —— 播放时钟据此对齐，
                # 后面几句的内容也在同一段里播（它们的源话位置更靠后
                # ⇒ 译文会**略微超前**源话 —— 这正是真人同传的行为）。
                src_start_sec=(first.src_start_sec if first.origin == "file"
                               else -1.0),
                stream_id=first.stream_id,
                # ⚠️ **首窗 12 帧**（不是上游的 50）—— 首窗是同传链路上
                # **最大的一段固定延迟**（实测 2.07 s vs 翻译推理 0.3 s）。
                # decoder 是因果的 ⇒ 首窗不需要未来帧 ⇒ 攒 12 帧即可开声
                # ⇒ **首声提前 ~1 s**。
                first_window_frames=INTERP_FIRST_WINDOW_FRAMES,
            )
            # ⚠️ **记下「这段的源话时长」**（按 seq 索引）——
            # 后面跟队列回传的「译文音频时长」配对 ⇒ 算出**长度比** ⇒
            # 变速瞄的就是它（见 ``_length_ratio``）。
            try:
                _seq = int((res or {}).get("seq") or 0)
            except Exception:  # noqa: BLE001
                _seq = 0
            if _seq:
                with self._lock:
                    _src = 0.0
                    for s in segs:
                        if s.src_end_sec >= 0.0 and s.src_start_sec >= 0.0:
                            _src += float(s.src_end_sec) - float(s.src_start_sec)
                    self._src_by_seq[_seq] = max(0.0, _src)
                    if len(self._src_by_seq) > 200:
                        for k in sorted(self._src_by_seq)[:-200]:
                            self._src_by_seq.pop(k, None)
        except TTSUnavailable as exc:
            with self._lock:
                self._counters.tts_available = False
                self._counters.last_error = str(exc)[:200]
            self._broadcast({"type": "interp_error", "index": first.index,
                             "reason": "TTS 未启用（降级为仅字幕）"})
            return
        except ValueError as exc:
            with self._lock:
                self._counters.last_error = str(exc)[:200]
            self._broadcast({"type": "interp_error", "index": first.index,
                             "reason": str(exc)[:120]})
            return
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._counters.failed += 1
                self._counters.last_error = str(exc)[:200]
            self._broadcast({"type": "interp_error", "index": first.index,
                             "reason": str(exc)[:120]})
            return
        with self._lock:
            self._counters.submitted += len(segs)
            self._last_src_end = last.src_end_sec
            # ⚠️ **记下每一句的分段计时**（诊断滞后花在哪一段）
            # （记录本体在翻译完成时已建好，这里只补「入队时刻」）。
            for seg in segs:
                if seg.timing:
                    seg.timing["t_submit"] = round(seg.submitted_at, 3)
                    seg.timing["src_end_ts"] = round(src_end_ts, 3)
                else:
                    seg.timing = {
                        "index": seg.index,
                        "text": seg.text[:40],
                        "tr": seg.translated[:40],
                        "src_start": round(seg.src_start_sec, 2),
                        "src_end": round(seg.src_end_sec, 2),
                        "t_seg": round(seg.created_ts, 3),
                        "t_deq": round(seg.dequeued_at, 3),
                        "t_tr": round(seg.translated_at, 3),
                        "t_submit": round(seg.submitted_at, 3),
                        "src_end_ts": round(src_end_ts, 3),
                        "wait_mt": round(seg.wait_mt_sec, 2),
                        "infer": round(seg.infer_sec, 2),
                        "attempts": seg.attempts,
                        #: ⚠️ **这段合并了几句源话**（诊断用）——
                        #: 1 = 没合并（旧的逐句路径）。
                        "batch": len(segs),
                    }
                    self._timings.append(seg.timing)
                    if len(self._timings) > 60:
                        del self._timings[:-60]
        for seg in segs:
            self._broadcast({
                "type": "interp_spoken",
                "index": seg.index,
                "text": seg.translated[:80],
                "lag_sec": 0.0,
                "tempo": self._counters.tempo,
            })

    # ------------------------------------------------------------------ #
    # 放行线程（跟播模式）
    # ------------------------------------------------------------------ #
    def _batch_is_final(self) -> bool:
        """这一批**不会再多了**吗？（⇒ 不用再等合并窗口）

        ⚠️ **为什么要这个**（实测 2026-11-23，M29）：
        合并窗口（:data:`INTERP_MERGE_HOLD_SEC` = 1 s）是用来「等后到的
        几句凑成一段」的 ⇒ **只有后面还会有句子时才值得等**。

        * 文件 / 视频源：ASR 是**离线一次性转写**的 ⇒ 转写完就没有新句子了
          ⇒ 再等 1 秒**纯属白等** ⇒ 实测首句的「放行等待」= 1.06 s
          ⇒ **首声白迟 ~1 秒**（首句的可听滞后里占 1/3）。
        * 实时流：用户可能还在说 ⇒ **永远不等**（等不到就是白等）。

        ⇒ 判据 = 「源已读完」**且**「所有句子都翻完了」（否则翻译队列里
        还有句子在排队 ⇒ 等它们进来才能凑成一段）。
        """
        with self._lock:
            if self._counters.translated < self._counters.segments:
                return False       # 还有句子没翻完 ⇒ 可能还会来
        if self._source == "video":
            # ⚠️⚠️ **视频源只看「媒体播完了」**（实测 2026-11-23，M31）——
            # 视频的识别**跑在播放前面**（解码比实时快 ⇒ ASR 几十秒就把整
            # 个视频识别完了，而视频才播了一半）⇒ 按 **ASR 的状态**放行
            # ⇒ 译文**抢在画面前面**播出 ⇒ 不是「跟字幕节奏」。
            # ⇒ 只认**媒体**结束（前端上报 ``ended``）⇒ 那时剩下的译文
            # **补播**（跟文件源「源读完 ⇒ 全放」一个道理）。
            return self._gate.source_done(self._source_stream_id())
        if self._source != "file":
            return False           # 实时流 ⇒ 用户可能还在说
        try:
            from wdnapps_audio_chat.cards.qwen3_asr.service import (
                get_asr_service,
            )
            snap = get_asr_service().snapshot()
            state = str(snap.get("state") or "")
        except Exception:  # noqa: BLE001
            return False
        if state not in ("done", "stopped", "error"):
            return False
        # ⚠️ **「跑完了」必须说的是「我们这一轮」** —— 否则**上一轮**跑完
        # 留下的 ``done`` 会让**这一轮**的句子一翻译完就单独放行
        # （攒批失效 ⇒ 每句都付一次固定开销 ⇒ 产能上不去 ⇒ 滞后照旧）。
        rid = self._rid_of_stream()
        cur = snap.get("current_request_id")
        if rid and cur is not None:
            try:
                if int(cur) != int(rid):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _rid_of_stream(self) -> int:
        """当前流的 id 里的 **请求号**（``file-<rid>#<epoch>`` ⇒ rid）。

        ⇒ 用来判断 ASR 的「跑完了」说的是不是**我们这一轮**。
        """
        with self._lock:
            sid = self._stream_id or ""
        if not sid.startswith("file-"):
            return 0
        try:
            return int(sid.split("#", 1)[0].split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    def _gate_worker(self) -> None:
        """放行线程 —— **成批**放行（批次按「内容量」攒）。

        ⚠️ **为什么改成「成批」**（实测 2026-11-23，M28）：
        TTS 每合成**一句**都有 **~2.9 s 固定开销** ⇒ 一句 16 字的译文
        一半时间是开销 ⇒ 4 句 = 17.0 s 音频里 ~9 s 是纯开销 ⇒ 长度比
        k 被推到 1.20 ⇒ 产能（~0.96×）**天生不够** ⇒ 可听滞后单调增长。

        ⚠️ **两种攒批口径**（实测 2026-11-23，M29）：

        * **跟播（文件 / 视频）**：按**内容量**攒（:data:`MERGE_MIN_AUDIO_SEC`
          = 18 秒音频）⇒ 每批的固定开销（~1.3 s 死时间 + ~0.55 s 首尾静音）
          被摊薄 ⇒ **产能回到 ~0.95×** ⇒ **译文不再越播越落后**。
          ⇒ 代价：多等一会儿（攒够 18 秒音频才送进 TTS）—— 但**播放时钟**
          会把译文对齐到源话 ⇒ 攒得越久，播得越准 ⇒ **净收益**。
        * **实时流（now）**：**低延迟优先** ⇒ 仍按「合并窗口」
          （:data:`INTERP_MERGE_HOLD_SEC` = 1 秒）攒 ⇒ 最多晚 1 秒开口。

        ⚠️ **不能无限等**：跟播模式有「攒够内容量」这个上界，实时流有
        合并窗口 ⇒ 延迟都有上界。
        """
        while not self._stop.is_set():
            time.sleep(0.2)
            if not self.running:
                continue
            with self._lock:
                pending = list(self._pending)
            if not pending:
                continue
            now = time.time()
            if not self._batch_ready(pending, now):
                continue
            # ⚠️ **按媒体位置排**（不是 index）—— 拖动重开识别后 index 与
            # 位置会**反序** ⇒ 按 index 排会把后面的句子先播出去（串句）。
            # 详见 :func:`_order_key`。
            batch = sorted(pending, key=_order_key)
            # ⚠️ **视频源：这一批只取「刚好够阈值」的那几句** —— 否则攒批
            # 会把**待播的全部**一次吞掉（实测一批 4~7 行 / 十几秒源话
            # ⇒ 仍然不是字幕的节奏）。
            if self._source == "video":
                batch = batch[:self._video_batch_size(batch)]
            with self._lock:
                for seg in batch:
                    if seg in self._pending:
                        self._pending.remove(seg)
                    self._armed.discard(seg.index)
                    self._ready_since.pop(seg.index, None)
            self._submit(batch)
            self._refresh_status()

    def _batch_ready(self, pending: list, now: float) -> bool:
        """这一批**可以送进 TTS**了吗？

        ⚠️ **跟播模式不看播放头**（实测 2026-11-23，M29）—— 播放时钟
        （队列侧 ``_clock_ready``）负责把译文**对齐到源话** ⇒ 放行器
        只管「攒够没有」⇒ **合成可以提前跑** ⇒ 管道有余量 ⇒ 不断音。
        """
        # ① 源已读完（文件源：离线转写，一次性出齐）⇒ 剩下的全放
        if self._batch_is_final():
            return True
        # ② **文件 / 视频源**（离线转写 ⇒ 内容成批出现）⇒ 按**内容量**攒
        #
        # ⚠️ **为什么不看「跟不跟播放」**（实测 2026-11-23，M29）：
        # 攒批的代价是「等一会儿才送进 TTS」—— 而这个代价**只对实时流
        # 成立**（人在说话 ⇒ 早说晚说都是滞后）。
        # 文件 / 视频源是**离线转写**（整个素材一次性转完）⇒
        # 「等」的成本只是**首句**多等一会儿，后面几句**本来就没到点**。
        # ⇒ 两种播放时机（now / follow）都该按内容量攒。
        if self._source == "file":
            audio = 0.0
            for seg in pending:
                audio += _estimate_audio_sec(seg.translated or "")
            if audio >= MERGE_MIN_AUDIO_SEC:
                return True
            # ⚠️ **兜底**：攒不满也不能永远不开口（源卡住 / 素材很稀疏）
            oldest = min(self._ready_since.get(s.index, now) for s in pending)
            return (now - oldest) >= MERGE_MAX_HOLD_SEC
        # ②b **视频源** ⇒ 跟字幕节奏走（批次小 + 按积压自适应）
        if self._source == "video":
            return self._video_batch_ready(pending, now)
        # ③ 实时流（人在说话）⇒ **低延迟优先**：等合并窗口（≤ 1 秒）
        ready = [s for s in pending if self._gate.admit(s)]
        if not ready:
            return False
        oldest = min(self._ready_since.get(s.index, now) for s in ready)
        return (now - oldest) >= INTERP_MERGE_HOLD_SEC

    def _video_batch_target(self) -> float:
        """视频源：这一批攒到**多少秒音频**才放行（自适应）。

        ⚠️ **反馈量是「TTS 还欠多少内容」**，不是「队列里堆了多少音频」
        （实测踩过：视频源里 TTS **故意跑在播放前面** ⇒ 队列里堆着几十秒
        音频是**设计如此** ⇒ 拿它当反馈量 ⇒ 阈值一路涨到上限 ⇒ 批次
        又变成一大坨 ⇒ 又不是字幕的节奏）。

        ⇒ 真正的反馈量 = **播放头已经走到哪** − **TTS 已经做到哪**
        （``_last_src_end`` = 最后提交的那句的源话结束位置）：
        * 正数 ⇒ TTS **落后**了 ⇒ 批次放大 ⇒ 先把欠的内容追回来；
        * 零/负数 ⇒ TTS **跑在前面** ⇒ 批次取下限 ⇒ 节奏最紧。
        """
        behind = 0.0
        try:
            ph = self._gate.playhead(self._source_stream_id())
            if ph is not None:
                with self._lock:
                    last_end = float(self._last_src_end or 0.0)
                behind = max(0.0, float(ph) - last_end)
        except Exception:  # noqa: BLE001
            behind = 0.0
        target = (VIDEO_MERGE_MIN_AUDIO_SEC
                  + VIDEO_MERGE_BACKLOG_GAIN * behind)
        return max(VIDEO_MERGE_MIN_AUDIO_SEC,
                   min(VIDEO_MERGE_MAX_AUDIO_SEC, target))

    def _video_batch_size(self, batch: list) -> int:
        """这一批**只取够阈值的那几句**（别把待播的全吞了）。

        ⚠️ **为什么必须限量**（实测 2026-11-23，M31）：阈值只是「够不够」
        的判据 ⇒ 攒批时**待播的往往已经有很多句**（翻译比放行快）⇒
        一次全吞 ⇒ 一批 4~7 行 / 十几秒源话 ⇒ **仍然不是字幕的节奏**。
        ⇒ 只取「刚好够阈值」的那几句 ⇒ 一批 ≈ 一行。
        """
        target = self._video_batch_target()
        acc = 0.0
        n = 0
        for seg in batch:
            acc += _estimate_audio_sec(seg.translated or "")
            n += 1
            if acc >= target:
                break
        return max(1, n)

    def _video_batch_ready(self, pending: list, now: float) -> bool:
        """**视频源**：批次**跟着字幕节奏**走（而不是「内容量」攒批）。

        ⚠️ **为什么视频源要单独一套口径**（实测 2026-11-23，M31；用户报
        「含音轨的视频开始同传后，没有根据目标语音字幕的节奏播放」）：

        * **文件源是离线一次性转写** ⇒ 句子一次性到齐 ⇒ 攒 18 秒音频只
          多花「首句」的等待；
        * **视频源是边播边识别** ⇒ 字幕行**随播放实时到达** ⇒ 攒 18 秒
          ⇒ **译文晚十几秒才开口**，而且一开口就是一大块（实测一批
          **6 行 / 14.8 秒源话**）⇒ 批内后面的行**比自己的字幕早十几秒**
          播出 ⇒ **完全不是字幕的节奏**。

        ⚠️ **批次大小按「TTS 欠多少内容」自适应**（节奏与产能是对立的）：

        * TTS **每次开口的固定开销 ~1.33 s**（实测：40 句逐句提交 ⇒
          估算音频 98.8 s / 墙钟 152 s ⇒ 吞吐 **0.65×**）⇒ 批次太碎 ⇒
          产能追不上 ⇒ 语音**越落越远**；
        * 批次太整 ⇒ 节奏粗（批内误差 ≤ 批次时长）⇒ 译文**早于字幕**；
        * ⇒ **跟得上 ⇒ 批次小**（节奏紧）；**跟不上 ⇒ 批次大**（先追平）。

        ⚠️ **不卡放行器**（跟文件源不同）—— 放行器会把句子压到「播放头
        到达那一行」才放 ⇒ TTS 的首窗 + 推理就落在**字幕之后** ⇒ 恒定
        滞后 ~2~3 秒。⇒ 视频源**译好就交** ⇒ 合成跑在播放前面 ⇒ 由
        **播放时钟**（队列侧）把它对齐到那一行的位置 ⇒ **零滞后**
        （这正是 M29「播放时钟负责对齐」的设计）。
        """
        # ① 攒够「自适应阈值」的译文音频 ⇒ 放（放多少由 _video_batch_size 定）
        audio = 0.0
        for seg in pending:
            audio += _estimate_audio_sec(seg.translated or "")
        if audio >= self._video_batch_target():
            return True
        # ② 兜底：攒不满也不能永远不开口（源很稀疏 / 字幕很碎）
        oldest = min(self._ready_since.get(s.index, now) for s in pending)
        return (now - oldest) >= MERGE_MAX_HOLD_SEC

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #
    def _refresh_status(self) -> None:
        """每秒刷新一次：TTS 队列状态 + 自动变速。"""
        try:
            from app.core.audio.tts_bridge import queue_status
            st = queue_status()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._counters.tts = st
            self._counters.played = int(st.get("done") or 0)
            if st.get("last_lag_sec"):
                self._counters.lag_sec = float(st["last_lag_sec"])
            # ⚠️ **自动变速：瞄准「译文长度比」，并受产能上限约束**
            #
            # ⚠️ **为什么是「长度比」**（实测 2026-11-22）：
            # 译文念出来比源话**长 k 倍**（EN→ZH 实测 ~1.15~1.25）⇒
            # 译文播完的时刻天然比源话晚 ⇒ 越播越落后。
            # ⇒ 变速就是用来**抵消这个长度差**的：**tempo = k** ⇒ 译文念完
            # 的时刻 ≈ 源话念完的时刻 ⇒ 译文跟得住。
            #
            # ⚠️ **但变速不能超过产能**：播放每墙钟秒从合成流里拉
            # ``tempo`` 秒的音频 ⇒ tempo > 产能 ⇒ 缓冲被吃空 ⇒ 断音。
            # ⇒ 上限 = 队列实测的 ``need_tempo``（= 最大的 ≤ 速率的档）。
            #
            # ⚠️ **旧的两种判据都是错的**：
            # * 「积压 > N 秒才提速」—— 跟播模式下积压在**放行器**那边，
            #   队列一直是空的 ⇒ 变速永远不启动；
            # * 「tempo ≥ 1/速率」—— **方向反了**（tempo > 速率只会更快
            #   地吃空缓冲，并不会让译文更快 —— 译文的速度上限是产能）。
            want = TEMPO_DEFAULT
            ratio = self._length_ratio(st)
            if ratio > 0.0:
                want = self._snap_tempo(ratio)
            for threshold, tempo in TEMPO_STEPS:
                if float(st.get("queued_audio_sec") or 0.0) > threshold:
                    want = max(want, tempo)
                    break
            # ⚠️ **产线上限是硬约束**（必须放在最后）——
            # 变速超过产能 ⇒ 播放拉不动合成流 ⇒ 缓冲被吃空 ⇒ 断音。
            need = float(st.get("need_tempo") or 0.0)
            if need > 0.0:
                want = min(want, need)
            # ⚠️⚠️ **但绝不慢于 1.0**（实测 2026-11-23，M29）——
            # 「宁可断音也不要慢」是**反的**：变慢 ⇒ 译文落后得更快
            # （k ÷ min(tempo, 产能) − 1 ⇒ tempo 0.96 比 1.0 多落后
            # 0.05 s/s）⇒ 用户抱怨的正是这个。
            # ⇒ 变速**只**用来抵消长度比（k > 1 且产能够），不用来放慢。
            want = max(want, TEMPO_FLOOR)
            # ⚠️ **变速要「少动」** —— 每次改变速 = **重开 aplay 管道**
            # （sox 的 tempo 是 spawn 时定死的）= 一次句间缝隙。
            # ⇒ 死区（:data:`TEMPO_DEADBAND`）+ 最小间隔
            # （:data:`TEMPO_MIN_INTERVAL`）⇒ 变速不抖 ⇒ 无缝隙。
            #
            # ⚠️⚠️ **一轮同传内「可以回落」**（实测 2026-11-23）：
            # 原来只有**棘轮**（只升不降）⇒ 产能估计一旦抖到高档位就**锁死**
            # ⇒ 之后即使真实产能掉下来（比如第二个 TTS 实例被停掉），
            # 变速也降不回去 ⇒ **播放速度超过产能 ⇒ 缓冲被吃空 ⇒ 断音**
            # （实测：变速锁在 1.08，而真实可持续速率只有 0.95 ⇒
            #  句间间隔从 0.29 s 一路涨到 0.73 s ⇒ 用户听到「顿挫」）。
            #
            # ⇒ **升得快、降得慢**：升用小死区（尽快跟上产能），降用大死区
            # （避免在阈值附近来回抖 ⇒ 每次变速都要重开 aplay 管道 ⇒
            #  一次句间缝隙）。
            now = time.time()
            cur = float(self._counters.tempo or 1.0)
            up = want > cur + TEMPO_DEADBAND
            down = want < cur - TEMPO_DEADBAND_DOWN
            if (up or down) and now - self._tempo_changed_at >= TEMPO_MIN_INTERVAL:
                self._counters.tempo = want
                self._tempo_changed_at = now
                try:
                    from app.core.audio.tts_bridge import queue_set_tempo
                    queue_set_tempo(want)
                except Exception:  # noqa: BLE001
                    pass
            # ⚠️ **跟播的提前量 = 实测的管线延迟**（入队 → 出声）
            #
            # ⚠️ **为什么**（实测 2026-11-22）：放行器原来只在播放头到达
            # 「句首 − 1 秒」时放行 ⇒ 而 TTS 管线要 **~3 s** 才出声 ⇒
            # **译文永远比源话晚 ~2~6 秒**（而且译文比源话长 16% ⇒ 越积
            # 越多）⇒ 用户看到的「滞后很多」。
            #
            # ⇒ 提前量改成**管线延迟**（实测值）⇒ 译文出声的时刻 ≈ 源话
            # 开口的时刻 ⇒ 「跟播」名副其实。
            self._update_lead(st)
            # 跟播滞后：已播句 src_end − 当前播放头
            if self._gate_mode == "follow":
                ph = self._gate.playhead(self._source_stream_id())
                if ph is not None and self._last_src_end > 0:
                    self._counters.lag_sec = max(0.0, self._last_src_end - ph)

    def _length_ratio(self, st: dict) -> float:
        """**译文长度比** k = 译文音频秒 ÷ 源话秒。

        ⚠️ **这是「变速该瞄的目标」**（实测 2026-11-22，用户报「播放还是
        滞后很多」）：译文念出来比源话长 k 倍（EN→ZH 实测 ~1.15~1.25）⇒
        播完的时刻天然比源话晚 ⇒ 越播越落后 ⇒ 面板上的滞后**单调增长**。
        ⇒ **tempo = k** ⇒ 两边同时念完 ⇒ 译文跟得住。

        ⚠️ **按 seq 配对**（不是按顺序）—— 队列满时会**合并队尾**
        （一个 job 对应好几句源话）⇒ 按顺序配会算错。
        """
        with self._lock:
            src_by_seq = dict(self._src_by_seq)
        if not src_by_seq:
            return 0.0
        src_sum = 0.0
        tr_sum = 0.0
        for t in (st.get("timings") or []):
            seq = t.get("seq")
            if seq is None:
                continue
            src = src_by_seq.get(seq)
            if not src or float(src) <= 0.0:
                continue
            audio = float(t.get("audio_sec") or 0.0)
            if audio <= 0.0:
                continue
            src_sum += float(src)
            tr_sum += audio
        if src_sum <= 0.5:
            return 0.0
        return tr_sum / src_sum

    @staticmethod
    def _snap_tempo(ratio: float) -> float:
        """把长度比**吸到变速阶梯上**（离散档 ⇒ 少重开 aplay 管道）。

        ⚠️ **下限 = :data:`TEMPO_FLOOR`（1.0）** —— 长度比小于 1（译文比
        源话**短**）时**不需要**变速 ⇒ 保持 1.0 ⇒ 播放不被拖慢。
        """
        best = TEMPO_FLOOR
        for t in TEMPO_LADDER:
            if t <= ratio + 1e-6 and t > best:
                best = t
        return min(best, TEMPO_MAX)

    def _apply_clock(self) -> None:
        """把**播放时钟**同步给 TTS 队列（跟播模式才有）。

        ⚠️ **跟播**：播放由**源话的播放头**驱动 ⇒
        ① 译文跟源话**对齐**（不再靠提前量硬凑）② 合成可以提前跑 ⇒
        管线有缓冲余量 ⇒ **不断音**。
        ⚠️ **now**：关掉时钟（不等播放头）⇒ 合成出来就播。
        """
        clocked = self._gate_mode == "follow"
        with self._lock:
            if clocked == self._clocked:
                return
            self._clocked = clocked
        try:
            from app.core.audio.tts_bridge import queue_set_clock
            if self._gate_mode == "follow":
                queue_set_clock(self._playhead_provider, 0.0)
            else:
                queue_set_clock(None, 0.0)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 设播放时钟失败: %s", exc)

    def _playhead_provider(self):
        """给 TTS 队列的**播放时钟**用：当前播放头（媒体秒）。

        ⚠️ 拿不到（没上报过 / 源不是媒体）⇒ 返回 ``None`` ⇒ 队列不按
        源话时钟播放（直接播）⇒ 不会把译文压住。
        """
        try:
            return self._gate.playhead(self._source_stream_id())
        except Exception:  # noqa: BLE001
            return None

    def _update_lead(self, queue_status: dict) -> None:
        """按**实测的生产延迟**更新放行器的提前量。

        ⚠️ **为什么提前量要跟着生产延迟走**（实测 2026-11-22，用户报
        「播放还是滞后很多」）：

        * 放行器在「播放头到达 ``句首 − lead``」时放行这句话；
        * 放行之后，TTS 还要 **生产延迟**（开始合成 → 出声）才响；
        * ⇒ 译文出声的时刻 = ``句首 − lead + 生产延迟``；
        * ⇒ 想让译文**跟源话同时响**（这才是「跟播」），就得
          ``lead = 生产延迟``。

        ⚠️⚠️ **必须用「生产延迟」（开始合成 → 出声），不能用「管线延迟」
        （入队 → 出声）**（实测踩过，正反馈跑飞）：

        * 「管线延迟」= 排队等待 + 生产延迟；
        * 而**排队等待恰恰是提前量造成的**（提前放行 ⇒ 句子提前进队列 ⇒
          排队更久）⇒ 提前量 ↑ ⇒ 管线延迟 ↑ ⇒ 提前量 ↑ ⇒ **正反馈
          跑飞**（实测：提前量一路顶到上限 6 s ⇒ 译文比源话晚 ~5 s，
          面板的滞后停在 5 s 下不来）；
        * 「生产延迟」只跟 TTS 本身有关（首窗 ~2 s）⇒ **跟队列深度
          无关** ⇒ 稳定。

        ⚠️ **取最近几句的中位数**，不用单句（单句会抖）；并**夹在**
        ``[FOLLOW_LEAD_MIN, FOLLOW_LEAD_MAX]`` 之间（太小 ⇒ 仍然晚；
        太大 ⇒ 译文抢在源话前面，听着更怪）。
        """
        tms = queue_status.get("timings") or []
        vals = []
        for t in tms[-8:]:
            v = t.get("synth_lat")
            if isinstance(v, (int, float)) and v > 0.0:
                vals.append(float(v))
        if not vals:
            # ⚠️ **兜底**：没有 ``synth_lat``（老格式的 timings）⇒ 退回
            # 管线延迟（会有上面说的正反馈，但不会更糟）。
            for t in tms[-8:]:
                v = t.get("pipe_lat")
                if isinstance(v, (int, float)) and v > 0.0:
                    vals.append(float(v))
        if not vals:
            return
        vals.sort()
        med = vals[len(vals) // 2]
        # ⚠️ **提前量 = 生产延迟 + 合成余量** —— 合成要在「轮到播」之前
        # 完成 ⇒ 提前量只需覆盖生产延迟（~2 s）+ 余量（1 s）。
        # ⚠️ **对齐交给播放时钟**（见 ``_apply_reserve``）⇒ 提前量不用大。
        lead = max(FOLLOW_LEAD_MIN,
                   min(FOLLOW_LEAD_MAX, med + FOLLOW_SYNTH_MARGIN_SEC))
        self._gate.set_lead(lead)

    def _last_error(self) -> str:
        with self._lock:
            return self._counters.last_error or "翻译失败"

    def _source_stream_id(self) -> str:
        with self._lock:
            return self._stream_id

    def status(self) -> dict:
        with self._lock:
            c = self._counters
            avg_lag = (c.lag_sum / c.played) if c.played else 0.0
            tts_lang = _TTS_LANG_BY_PROVIDER.get(self._target_lang)
            return {
                "running": self._running,
                "mode": self._mode,
                "gate": self._gate_mode,
                "source": self._source,
                # **当前流的 id**（句子用的那个）—— 前端上报播放头时要带上它，
                # 否则放行器配不上（实测：前端一直报空 id ⇒ 播放头配不上
                # 句子 ⇒「跟播放」把句子永久压住 ⇒ 有翻译没声音）。
                "stream_id": self._stream_id,
                "target_lang": self._target_lang,
                "tts_lang": tts_lang or "",
                "speaker": self._speaker,
                "ref_speaker": self._ref_speaker,
                "volume": self._volume,
                "segments": c.segments,
                "translated": c.translated,
                "retried": c.retried,
                "abandoned": c.abandoned,
                "submitted": c.submitted,
                "played": c.played,
                "failed": c.failed,
                "skipped_seek": c.skipped_seek,
                # ⚠️ **seek 之后重播的句数**（用户拖回进度条 ⇒ 已经播过
                # 的句子要**再说一遍**）。
                "replayed": c.replayed,
                # ⚠️ **最近一次 seek 的回执** —— 前端据此提示「已重新对齐」。
                "last_seek": dict(self._last_seek_info)
                if self._last_seek_info else None,
                "pending": len(self._pending),
                "queue_depth": self._queue.qsize(),
                "lag_sec": round(c.lag_sec, 2),
                "lag_avg_sec": round(avg_lag, 2),
                "tempo": round(c.tempo, 3),
                "tts": c.tts_available,
                "tts_status": c.tts,
                # ⚠️ **可听滞后**（译文出声 − 源话开口）—— **用户真正感受到
                # 的那一口**（``lag_sec`` 在跟播模式下是另一个口径：
                # 「源话比最后一句已提交的译文前进了多少」）。
                "audible_lag_sec": round(
                    float((c.tts or {}).get("last_a_lag") or 0.0), 2),
                # 本机 MT（翻译模型）状态 —— 冷启动时前端据此提示
                # 「翻译模型正在启动」。
                "mt_ready": self._mt_ready,
                "mt_waiting": self._mt_waiting,
                "mt_error": self._mt_error,
                # **本机 TTS** 就绪了吗（没就绪 ⇒ 语音可能要走对端 ⇒ 慢）。
                "tts_local_ready": self._tts_ready,
                "tts_waiting": self._tts_waiting,
                "tts_error": self._tts_error,
                # 跟播模式下**播放头卡住**（媒体没在放）⇒ 前端提示用户。
                "playhead_stalled": self._gate.stalled(self._source_stream_id()),
                "stalled_released": self._gate.stalled_released(),
                # **源播完了 / 媒体坏了** ⇒ 源不会再往前走 ⇒ 剩下的译文
                # 直接补播（前端据此提示「源已播完，正在补播」）。
                "source_done": self._gate.source_done(self._source_stream_id()),
                # **每句的分段计时**（诊断滞后明细用；最近 60 句）
                "timings": list(self._timings),
                # 这一轮会话里播放头**有没有真的走过**（自动播放被拦 ⇒ False）
                # —— 诊断用：False ⇒ 媒体**从没播过**。
                "playhead_moved": not self._gate.never_played(),
                "last_error": c.last_error,
                "playhead_sec": self._gate.playhead(self._source_stream_id()),
            }

    # ------------------------------------------------------------------ #
    # 注入的回调
    # ------------------------------------------------------------------ #
    def set_text_provider(self, fn: Optional[Callable[[str], str]]) -> None:
        self._text_provider = fn

    def set_broadcaster(self, fn: Optional[Callable[[dict], None]]) -> None:
        self._broadcaster = fn

    def set_loop(self, loop) -> None:
        """记下主事件循环（广播要切回它）。"""
        self._loop = loop

    def loop_missing(self) -> bool:
        """还没记住主事件循环吗（⇒ 广播会不安全）。"""
        return self._loop is None

    def _broadcast(self, payload: dict) -> None:
        if self._broadcaster is None:
            return
        loop = self._loop
        if loop is not None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                try:
                    loop.call_soon_threadsafe(self._broadcast, payload)
                except RuntimeError:  # pragma: no cover — 循环已关闭
                    pass
                return
        try:
            self._broadcaster(payload)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("interp: 广播失败: %s", exc)


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #

_ENGINE: Optional[InterpEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_interp_engine() -> InterpEngine:
    """同传引擎单例（懒加载）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = InterpEngine()
            _wire_engine(_ENGINE)
        return _ENGINE


def _wire_engine(engine: InterpEngine) -> None:
    """把「取文本」和「广播」两个回调接上（避免 core → 卡片 的循环依赖）。"""
    def _text_provider(stream_id: str) -> str:
        from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
        return get_asr_service().current_text(stream_id)

    def _broadcaster(payload: dict) -> None:
        from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
        get_asr_service().broadcast(payload)

    engine.set_text_provider(_text_provider)
    engine.set_broadcaster(_broadcaster)
