"""烧双语字幕 + 换音轨 + 重新封装（2026-11-26）。

「含音轨的视频」来源的**配音导出**：源视频 → 拆出音轨 → ASR+翻译+TTS
得到目标语言音轨 ⇒ 把**原文 + 译文**两行字幕**烧进画面** ⇒ 再把烧好的
视频与新音轨**重新封装**成一个 MP4。

工具链（全部走**硬件 / SDK**，不引第三方编解码器）
================================================

============  ==========================================================
环节          实现
============  ==========================================================
容器解包      PyAV（**只做容器 I/O**：把 H.264 样本取出来）
H.264 解码    ``wdnav.mpp.MppDecoder``（MPP 硬件解码）
像素转换      ``wdnav.rga``（NV12 → BGR，经 ``DecodedFrame.numpy``）
字幕栅格化    ``wdnav.mali.MaliText``（freetype，**支持 CJK**）
H.264 编码    ``wdnav.mpp.MppEncoder``（MPP 硬件编码，BGR 入 NV12 出）
容器封装      PyAV（``add_mux_stream`` 封预编码样本）
音频编码      PyAV 的 AAC 编码器（**MPP 没有 AAC 编码器** ⇒ 只能走它）
============  ==========================================================

⚠️ **为什么要自己写 AVCC → Annex-B**
-----------------------------------

``wdnav.mpp.avcc_to_annexb`` 有一个**数据相关的坑**：它开头会判断

.. code-block:: python

    if data[:4] == b"\\x00\\x00\\x00\\x01": return data   # 已经是 Annex-B
    if data[:3] == b"\\x00\\x00\\x01":     return data

而 **MP4 里的 H.264 样本是 AVCC**（每个 NAL 前缀 4 字节长度）⇒ 那个
「长度前缀」**恰好**是 ``00 00 01 xx`` 时（长度 = 0x000001xx，常见！）
⇒ 它**把还没转换的 AVCC 当成 Annex-B 原样返回** ⇒ NAL 边界错位 ⇒
MPP 解码器只吐出前几帧就卡死。

实测（2026-11-26，本卡的 ``preview.mp4``）：用 SDK 的转换器只解出
**5/73 帧**；换成下面这个**不做启发式判断**的版本 ⇒ **71/73 帧**。

⇒ 这里**自己**转换（严格按 AVCC 的长度前缀走），不依赖 SDK 那个函数。
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, List, Optional, Sequence

LOG = logging.getLogger("qwen3_asr.video_burn")

#: 烧字幕的字体候选（按顺序找第一个存在的）。
#:
#: ⚠️ **必须有 CJK 字形** —— 字幕里有中文，DejaVu 之类的纯拉丁字体会把
#: 汉字画成豆腐块（板端**没有** emoji / 符号字体）。
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
)

#: 找不到 CJK 字体时退回的（只覆盖拉丁字符）。
_FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

#: 字幕字号占画面高度的比例（1080p ⇒ 约 34 px）。
_FONT_SIZE_RATIO = 0.032

#: 字幕离画面底边的距离（占画面高度的比例）。
_MARGIN_BOTTOM_RATIO = 0.045

#: 两行字幕之间、行与画面底边之间的间距（像素）。
_LINE_GAP = 6

#: 字幕背景带的压暗系数（0.35 ⇒ 保留 35% 亮度）。
_BAND_DIM = 0.35


def _fit_bitrate(width: int, height: int, fps: int, override: int = 0) -> int:
    """按分辨率/帧率给一个**合理**的目标码率。

    ⚠️ **不能一刀切 4 Mbps** —— 板子上 320×240 的小样片按 4 Mbps 算，
    144 秒就是 **72 MB**；1080p 反而不够。⇒ 用「每像素每帧 0.07 bit」
    的经验值，再夹到 500 kbps ~ 8 Mbps。

    ``override > 0`` 时直接用调用方给的值。
    """
    if override and int(override) > 0:
        return int(override)
    raw = float(width) * float(height) * float(max(1, fps)) * 0.07
    return int(max(500_000, min(8_000_000, raw)))


@dataclass
class SubtitleCue:
    """一条字幕（**容器时间轴**，秒）。

    ``orig`` / ``trans`` 两行都烧上去 ⇒ 观众既能看原声字幕，也能看译文。
    """

    start_sec: float
    end_sec: float
    orig: str = ""
    trans: str = ""


@dataclass
class BurnResult:
    """``render_dubbed_video`` 的结果。"""

    frames: int = 0
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: int = 0
    subtitle_cues: int = 0
    audio_sec: float = 0.0
    warnings: List[str] = field(default_factory=list)
    #: 最后一帧的 PTS（沿用源时间基）—— 编码器尾巴也要用它。
    last_pts: int = 0
    #: 最后一帧的 PTS（换算到 90 kHz，输出流的时间基）。
    last_pts90: int = 0
    #: 最后一帧的时间（秒，容器时间轴）。
    last_sec: float = 0.0
    #: 被丢掉的「时间戳倒序」的帧数（MPP 尾部重复帧）。
    dropped: int = 0


def resolve_font() -> str:
    """找一个能画中文的字体路径；找不到 CJK 就退回拉丁字体。"""
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    for path in _FALLBACK_FONTS:
        if os.path.isfile(path):
            LOG.warning("video-burn: 没有 CJK 字体，退回 %s（中文会变豆腐块）",
                        path)
            return path
    # 最后再试一次 fontconfig（也许装在别处）
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}\n", "wqy-zenhei"],
                             capture_output=True, text=True, timeout=2.0,
                             check=False)
        path = out.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except Exception:  # noqa: BLE001 — fontconfig 没有就当没有
        pass
    raise RuntimeError("找不到可用的字体（烧字幕需要）")


def avcc_to_annexb(data: bytes) -> bytes:
    """AVCC（4 字节长度前缀）→ Annex B（起始码）。

    ⚠️ **不做「看起来像起始码就原样返回」的启发式** —— 见模块开头：
    MP4 的 AVCC 样本开头**经常**是 ``00 00 01 xx``（长度前缀）⇒ 那个
    启发式会把没转换的数据当成 Annex-B ⇒ NAL 边界错位 ⇒ 解码只出几帧。

    输入已经是 Annex-B（真的以起始码开头）时也能正确处理：长度前缀解析
    会立刻失败 ⇒ 返回空串，调用方据此走「原样用」的分支。
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i + 4 <= n:
        nal_len = int.from_bytes(data[i:i + 4], "big")
        i += 4
        if nal_len <= 0 or i + nal_len > n:
            return b""
        out += b"\x00\x00\x00\x01"
        out += data[i:i + nal_len]
        i += nal_len
    if i != n:
        return b""
    return bytes(out)


def _is_annexb(data: bytes) -> bool:
    """这个样本**是不是**以 Annex-B 起始码开头。

    ⚠️ **只能当「兜底」用，不能当判据** —— 见 :func:`avcc_to_annexb`：
    AVCC 样本的长度前缀**经常**就是 ``00 00 01 xx`` ⇒ 光看开头三个字节
    分不清「已经是 Annex-B」和「还没转换的 AVCC」。

    ⇒ 真正的判据是 :func:`avcc_to_annexb` 的**严格解析**能不能把整个
    样本吃干净；这里只用来兜底（严格解析失败 ⇒ 才当它是 Annex-B）。
    """
    return (len(data) >= 4 and data[:4] == b"\x00\x00\x00\x01") or \
           (len(data) >= 3 and data[:3] == b"\x00\x00\x01")


def to_annexb(data: bytes) -> bytes:
    """一个视频样本 ⇒ Annex-B（空串 = 认不出）。

    ⚠️ **顺序很关键**：先按 **AVCC 严格解析**（能吃干净 ⇒ 就是 AVCC），
    解析失败**才**当它是 Annex-B ⇒ 这样「AVCC 的长度前缀恰好是
    ``00 00 01``」的样本不会被误判（那正是 SDK 那个函数的坑）。
    """
    out = avcc_to_annexb(data)
    if out:
        return out
    if _is_annexb(data):
        return data
    return b""


class _SubtitlePainter:
    """把两行字幕画到 BGR 画布上。

    用 ``wdnav.mali.MaliText``（freetype）栅格化 —— **支持 CJK**，
    板端没有 PIL / OpenCV 的 freetype 绑定，只有 SDK 这条路。
    """

    def __init__(self, width: int, height: int) -> None:
        from wdnav.mali import MaliText

        self._w = int(width)
        self._h = int(height)
        size = max(16, int(round(height * _FONT_SIZE_RATIO)))
        self._size = size
        self._text = MaliText(resolve_font(), size)
        self._line_h = max(1, self._text.measure("测试Ag")[1] or size)
        # ⚠️ ``MaliText.render`` 的 ``y`` 是**基线**（freetype 笔位约定），
        # 不是顶边 ⇒ 基线必须落在 ascent 上，否则字会掉到带子外面。
        # SDK 只给 ``line_h``（ascent+descent+leading）⇒ 用 0.82 估 ascent。
        self._ascent = max(1, int(round(self._line_h * 0.82)))

    def _wrap(self, s: str, max_w: int) -> List[str]:
        """按像素宽度折行（中文按字断，西文按词断）。"""
        s = (s or "").strip()
        if not s:
            return []
        if self._text.measure(s)[0] <= max_w:
            return [s]
        lines: List[str] = []
        cur = ""
        for ch in s:
            trial = cur + ch
            if cur and self._text.measure(trial)[0] > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def paint(self, bgr, cue: Optional[SubtitleCue]) -> None:
        """把一条字幕画到 ``bgr``（原地改）。"""
        if cue is None:
            return
        import numpy as np

        orig_lines = self._wrap(cue.orig, int(self._w * 0.92))
        trans_lines = self._wrap(cue.trans, int(self._w * 0.92))
        if not orig_lines and not trans_lines:
            return
        rows = len(orig_lines) + len(trans_lines)
        total_h = rows * self._line_h + max(0, rows - 1) * _LINE_GAP
        bottom = self._h - int(self._h * _MARGIN_BOTTOM_RATIO)
        top = max(0, bottom - total_h)
        # 半透明黑底（先压暗再画字 ⇒ 文字始终清晰）
        band = bgr[top:bottom, 0:self._w]
        # ⚠️⚠️ **不能写 ``band *= 0.35``** —— ``band`` 是 uint8 画布，
        # 原地乘一个浮点数会让 numpy 算出 float64 再塞回 uint8 ⇒
        # ``UFuncTypeError: Cannot cast ufunc 'multiply' output from
        # dtype('float64') to dtype('uint8') with casting rule 'same_kind'``。
        # ⇒ 显式升到 float 算完、再取整写回。
        band[:] = (band.astype(np.float32) * _BAND_DIM).astype(np.uint8)
        y = top + self._ascent
        for ln in orig_lines:
            # BGR 三元组 ⇒ 黄色 = (0, 214, 255)
            self._text.render(ln, 16, y, color_bgr=(0, 214, 255),
                              bgr_buf=bgr, buf_w=self._w, buf_h=self._h)
            y += self._line_h + _LINE_GAP
        for ln in trans_lines:
            self._text.render(ln, 16, y, color_bgr=(255, 255, 255),
                              bgr_buf=bgr, buf_w=self._w, buf_h=self._h)
            y += self._line_h + _LINE_GAP


def _cue_at(cues: Sequence[SubtitleCue], t: float) -> Optional[SubtitleCue]:
    """取 ``t`` 时刻该显示的那条字幕。

    ⚠️ **按时间重叠匹配，不做二分** —— 一段的字幕条数是**几十~几百**，
    线性扫足够快（每帧扫一遍 ⇒ 25 fps × 300 条 = 7500 次比较/秒，可忽略）。
    """
    for cue in cues:
        if cue.start_sec <= t < cue.end_sec:
            return cue
    return None


def _mux_audio(outc, pcm: bytes, sample_rate: int) -> int:
    """把目标语言音轨封进容器（PyAV 的 AAC 编码器）。

    ⚠️ **必须先封音频再封视频** —— PyAV 在写第一个包时才会打开各流的
    编码器并定下 ``stream.time_base``；音频流的 time_base 是
    ``1/sample_rate``，不先封好它，后面的音频包会因为「time base 为 0」
    被拒（``Cannot rebase to zero time``）。

    ⚠️ **按 1024 样本一块喂** —— 整条音轨塞进**一个** AudioFrame 在 AAC
    编码器上会出问题（超大帧），而且万一中途失败就得整条重编。
    """
    import numpy as np

    if not pcm:
        return 0
    arr = np.frombuffer(pcm, dtype="<f4")
    if arr.size == 0:
        return 0
    aus = outc.add_stream("aac", rate=sample_rate)
    aus.layout = "mono"
    aus.time_base = Fraction(1, sample_rate)
    n = 0
    chunk = 1024
    pos = 0
    while pos < arr.size:
        seg = arr[pos:pos + chunk]
        frame = av_frame_from_float(seg, sample_rate)
        # ⚠️ **显式给 pts**（累计样本数）—— 音频流的 time_base 是
        # ``1/sample_rate``，pts 就是第几个采样点。
        frame.pts = pos
        for pkt in aus.encode(frame):
            outc.mux(pkt)
            n += 1
        pos += int(seg.size)
    for pkt in aus.encode(None):
        outc.mux(pkt)
        n += 1
    return n


def av_frame_from_float(arr, sample_rate: int):
    """float32 单声道 PCM → PyAV 的 AAC 输入帧。"""
    import av
    import numpy as np

    clipped = np.clip(arr, -1.0, 1.0)
    s16 = (clipped * 32767.0).astype(np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(s16, format="s16", layout="mono")
    frame.sample_rate = int(sample_rate)
    frame.pts = 0
    return frame


def render_dubbed_video(
    source: Path,
    out_path: Path,
    *,
    audio_pcm: bytes = b"",
    audio_sample_rate: int = 24000,
    cues: Sequence[SubtitleCue] = (),
    fps: int = 25,
    bitrate: int = 0,
    progress: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> BurnResult:
    """把字幕烧进画面、换上目标语言音轨、重新封装成一个 MP4。

    ``cues`` 的时间必须是**容器时间轴**（不是音轨相对时间）—— 调用方
    负责把 ASR 的音轨时间加上「音轨在容器里的起点」。

    ⚠️ **输出帧的时间戳直接沿用源帧的 PTS** —— 不能「解出来的第 N 帧
    就当第 N/fps 秒」：

    * 源视频**不一定是恒定帧率**（板上的测试样片是 **1 fps** 的抽帧视频，
      145 帧铺满 144 秒）；硬编一个 25 fps 会把 144 秒的片子压成 5.8 秒。
    * 有 B 帧时**包 PTS 不是显示时间**（解码序 ≠ 显示序），但 **MPP 解出
      的帧 PTS 是正确的显示时间** ⇒ 直接拿它当时间轴。
    """
    import av
    from wdnav.mpp import (MppCodingType, MppDecoder, MppEncoder,
                           MppFrameFormat, parse_avcc_extradata)

    # ⚠️ 允许调用方直接传字符串（免得每个调用点都要 ``Path(...)``）
    source = Path(source)
    out_path = Path(out_path)

    result = BurnResult()
    container = av.open(str(source), timeout=(5.0, 15.0))
    try:
        vstream = next((s for s in container.streams if s.type == "video"), None)
        if vstream is None:
            raise RuntimeError("源文件里没有视频轨")
        width = int(vstream.width)
        height = int(vstream.height)
        result.width = width
        result.height = height
        result.subtitle_cues = len(cues)

        # ⚠️ **源的时间基** —— 输出流用它 ⇒ 帧 PTS 可以原样搬过去。
        src_tb = (Fraction(vstream.time_base)
                  if vstream.time_base else Fraction(1, 1000))
        # ⚠️ **标称帧率**（给编码器做码率/GOP 用）：优先用流头的
        # ``average_rate``；抽帧视频它会算成 ~1.0（这才是真实帧率）⇒ 正好。
        fps = _nominal_fps(vstream, default=int(fps))
        result.fps = fps
        bitrate = _fit_bitrate(width, height, fps, bitrate)

        sps_pps = parse_avcc_extradata(vstream.codec_context.extradata or b"")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        try:
            tmp_path.unlink()
        except OSError:
            pass

        # ⚠️ **总包数只用来显示进度** —— **不能**为了数它把 demux 走一遍
        # （``container.demux()`` 是**一次性**的生成器，走一遍就没了 ⇒
        # 后面的解码循环会**一帧都拿不到**）。⇒ 用流头里的时长估。
        total_pkts = max(1, int((vstream.duration or 0) *
                                float(src_tb) * float(fps)))

        outc = av.open(str(tmp_path), "w", format="mp4")
        try:
            # ⚠️⚠️ **时间基必须是 1/90000**（不要用源的时间基！）——
            # FFmpeg 的 MP4 muxer 会把 ``time_base.den`` 反复 ×2 抬到
            # >= 10000（``movenc.c`` 的 ``track->timescale`` 循环），
            # **只改流的时间基、不会重算已经写下去的 stts 时间戳** ⇒
            # 交上去的 pts 是「帧序号」的话，存下来会变成几十上百倍速。
            # ⇒ 统一换算到 90 kHz（SDK ``wdnav.encoder`` 也是这么干的）。
            vs = outc.add_mux_stream("h264", rate=int(fps),
                                     width=width, height=height,
                                     pix_fmt="yuv420p",
                                     time_base=Fraction(1, 90000))
            frame_dur = int(round(90000.0 / float(max(1, fps))))

            # ① 音轨先封（见 ``_mux_audio`` 的注释）
            if audio_pcm:
                n_audio = _mux_audio(outc, audio_pcm, audio_sample_rate)
                result.audio_sec = len(audio_pcm) / 4.0 / float(
                    max(1, audio_sample_rate))
                if not n_audio:
                    result.warnings.append("音轨没有封进容器")

            # ② 视频：MPP 解码 → 烧字幕 → MPP 编码 → 封装
            dec = MppDecoder(coding_type=MppCodingType.AVC,
                             output_format=MppFrameFormat.YUV420SP)
            enc = MppEncoder(coding_type=MppCodingType.AVC, width=width,
                             height=height, fps=int(fps), bitrate=int(bitrate),
                             gop=max(1, int(fps) * 2), profile="high",
                             input_format=MppFrameFormat.BGR888)
            painter = _SubtitlePainter(width, height)
            enc_extra = enc.extradata
            #: 最后一帧的画面（用来在末尾「冻结」几帧）。
            last_frame: dict = {}

            def _drain_all() -> int:
                return _drain(dec, painter, cues, enc, outc, vs, enc_extra,
                              src_tb, frame_dur, result, last_frame)

            try:
                n = 0
                for pkt in container.demux(vstream):
                    data = bytes(pkt)
                    if not data:
                        continue
                    annexb = to_annexb(data)
                    if not annexb:
                        # 既不是 AVCC 也不是 Annex-B ⇒ 跳过这一包
                        result.warnings.append(
                            "第 {} 个视频包格式认不出，跳过".format(n))
                        continue
                    if n == 0 and sps_pps:
                        annexb = sps_pps + annexb
                    # ⚠️ **缓冲满 ⇒ 先把解好的帧取出来再重试**（见 ``_feed``）
                    while True:
                        try:
                            dec.send_packet(annexb, pts=int(pkt.pts or 0))
                            break
                        except RuntimeError as exc:
                            if "-1012" not in str(exc):
                                raise
                            if _drain_all() == 0:
                                time.sleep(0.005)
                    _drain_all()
                    n += 1
                    if progress is not None:
                        progress(min(0.99, n / float(total_pkts)),
                                 "烧字幕 {}/{} 帧".format(n, total_pkts))
                    if cancel is not None and cancel():
                        raise RuntimeError("用户取消了")
                # 收尾：⚠️ **必须送 EOS** —— 否则解码器内部还没吐出来的
                # 尾帧（实测 73 包只出 69 帧）会**悄悄丢掉** ⇒ 片尾黑屏。
                try:
                    dec.send_packet(b"", pts=0, eos=True)
                except RuntimeError as exc:  # noqa: BLE001
                    LOG.debug("video-burn: 送 EOS 失败: %s", exc)
                idle = 0
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if _drain_all() == 0:
                        idle += 1
                        if idle >= 5:
                            break
                        time.sleep(0.01)
                    else:
                        idle = 0
                # ⚠️⚠️ **译文可能比原声长**（中文常比英文啰嗦）⇒ 音轨
                # 比画面长 ⇒ 片尾的话会被**切断**。⇒ 用**最后一帧冻结**
                # 把画面补到音轨那么长。
                #
                # ⚠️ **直接冻结原帧**（不再重画字幕）—— 音轨之所以长，
                # 通常就是**最后一句**的译文更长 ⇒ 那一帧上的字幕正是
                # 该句的译文 ⇒ 冻结它刚好对得上；而且重画会让字幕一层层
                # 叠在一起（黑带越叠越黑、文字叠字）。
                if audio_pcm and last_frame.get("bgr") is not None:
                    need = result.audio_sec - (result.last_sec +
                                               1.0 / float(max(1, fps)))
                    # ⚠️ **必须 ceil** —— ``int(0.98)`` = 0 ⇒ 差 0.02 秒
                    # 也不补 ⇒ 片尾照样被切断。
                    pad = int(math.ceil(need * float(max(1, fps))))
                    pad = max(0, min(pad, int(15 * float(max(1, fps)))))
                    if pad:
                        bgr = last_frame["bgr"]
                        for k in range(pad):
                            pts90 = result.last_pts90 + (k + 1) * frame_dur
                            pkt = enc.encode(bgr)
                            if pkt.data:
                                _mux_video(outc, vs, pkt.data, pts90, frame_dur)
                            result.frames += 1
                            result.last_pts90 = pts90
                            result.last_sec = pts90 / 90000.0
                        result.warnings.append(
                            "译文比原声长 {:.1f} 秒 ⇒ 片尾冻结了 {} 帧".format(
                                need, pad))
                # 编码器尾巴
                tail = enc.flush()
                while tail is not None:
                    if tail.data:
                        _mux_video(outc, vs, tail.data, result.last_pts90,
                                   frame_dur)
                        result.frames += 1
                    tail = enc.flush()
            finally:
                try:
                    enc.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    dec.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                outc.close()
            except Exception:  # noqa: BLE001
                pass

        # 原子替换（别让用户看到半个文件）
        shutil.move(str(tmp_path), str(out_path))
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass

    result.duration_sec = result.last_sec or (result.frames /
                                               float(max(1, fps)))
    if progress is not None:
        progress(1.0, "完成")
    return result


def _nominal_fps(vstream, default: int = 25) -> int:
    """从流头估一个**标称帧率**（给编码器做码率控制 / GOP 用）。

    ⚠️ **不能用「帧数 ÷ 时长」** —— 抽帧视频（1 fps）会被当成正常视频，
    编码器按 25 fps 配码率 ⇒ 文件暴涨。⇒ 用流头的 ``average_rate``，
    再夹到 1~120。
    """
    rate = 0.0
    try:
        rate = float(vstream.average_rate or 0.0)
    except Exception:  # noqa: BLE001
        rate = 0.0
    if rate < 1.0 or rate > 120.0:
        # ``average_rate`` 不靠谱（有些封装写 0）⇒ 退回 ``guessed_rate``。
        try:
            rate = float(vstream.guessed_rate or 0.0)
        except Exception:  # noqa: BLE001
            rate = 0.0
    if rate < 1.0 or rate > 120.0:
        rate = float(default or 25)
    return int(max(1, min(120, round(rate))))


def _drain(dec, painter, cues, enc, outc, vs, enc_extra, tb, frame_dur,
           result: BurnResult, last_frame: dict) -> int:
    """取光 decoder 里已经解好的帧 ⇒ 烧字幕 ⇒ 编码 ⇒ 封装。"""
    got = 0
    while True:
        frame = dec.recv_frame(timeout_ms=5)
        if frame is None:
            return got
        try:
            bgr = frame.numpy(fmt="bgr")
        finally:
            dec.release_frame(frame)
        if bgr is None:
            continue
        # ⚠️ **用帧自己的 PTS 当时间轴**（容器时间）—— MPP 解出来的
        # pts 就是**显示时间**（有 B 帧时包 PTS 不是显示时间）。
        src_pts = int(frame.pts)
        t = src_pts * float(tb)
        # ⚠️ **换算到 90 kHz**（输出流的时间基）—— 源的时间基可能是
        # 1/512 这种粗的，muxer 会擅自改写流的时间基 ⇒ 统一换算。
        pts90 = int(round(t * 90000.0))
        # ⚠️⚠️ **时间戳必须严格递增** —— MPP 的解码器在**尾部**会重复吐
        # 同一帧（实测 preview.mp4：35840 之后又来一个 35328）⇒ dts 倒序
        # ⇒ FFmpeg 的 muxer 直接报 ``Invalid argument (22)``。
        # ⇒ 丢掉倒序的帧（最多一两帧，片尾，不影响观感）。
        if result.frames and pts90 <= result.last_pts90:
            result.dropped += 1
            continue
        painter.paint(bgr, _cue_at(cues, t))
        pkt = enc.encode(bgr)
        if pkt.data:
            data = pkt.data
            if result.frames == 0 and enc_extra:
                data = enc_extra + data
            _mux_video(outc, vs, data, pts90, frame_dur)
        result.frames += 1
        result.last_pts = src_pts
        result.last_pts90 = pts90
        result.last_sec = t
        # ⚠️ **留一份最后一帧** —— 片尾「冻结」用（译文比原声长时）。
        # ⚠️ **只留引用、不 copy** —— ``bgr`` 是 RGA 每次新分配的数组
        # （``frame.numpy()`` 把像素**搬**出来）⇒ 引用是安全的；而每帧
        # copy 一份 1080p 画面会把导出拖慢一个数量级。
        last_frame["bgr"] = bgr
        got += 1


def _mux_video(outc, vs, data: bytes, pts: int, duration: int) -> None:
    """把一帧 H.264 封进容器（时间戳沿用源帧，换算到 90 kHz）。

    ⚠️ **必须显式给 ``dts`` 和 ``duration``** —— 只给 ``pts`` 时 FFmpeg 的
    mov muxer 会因为 dts 无效直接报 ``Invalid argument (22)``。
    """
    from av.packet import Packet

    pkt = Packet(data)
    pkt.stream = vs
    pkt.pts = int(pts)
    pkt.dts = int(pts)
    pkt.duration = int(duration)
    pkt.time_base = Fraction(1, 90000)
    outc.mux(pkt)
