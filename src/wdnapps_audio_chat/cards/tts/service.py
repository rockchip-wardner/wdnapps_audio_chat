"""Qwen3-TTS session service for wdnwebui.

Drives TTS inference through the v2 ``LLMSession.respond`` API; the
service owns the per-call state machine, history, broadcaster, and
WAV file I/O. ``model_dir`` resolution is the same as the SDK's
TTSBackend (env var → ``$WDNAI_MODELS_DIR/Qwen3-TTS`` →
``<default_models_root>/Qwen3-TTS``); the layout is documented in
``wdnai/models/README.md``.
"""
from __future__ import annotations

import atexit
import asyncio
import io
import logging
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from wdnai import SoftwareCapability
from pathlib import Path
from typing import Any, Callable, List, Optional

import numpy as np


# 播放目标常量 + TTSRequest + default_ref_speaker 已迁至 app.core.audio.tts_bridge,
# 此处仅 re-export 以保持旧 import 路径兼容。
from app.core.audio.tts_bridge import (  # noqa: F401  (re-export)
    PLAY_DEVICE, PLAY_BROWSER, PLAY_BOTH, PLAY_QUEUE, PLAY_TARGETS,
    TTSRequest,
    default_ref_speaker,
)

LOG = logging.getLogger("tts_session")


# Plain-print so diagnostics land in uvicorn's stdout regardless of
# how the root logger is configured.
def _tts_log(msg: str) -> None:
    print(f"[tts_session] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Non-blocking aplay subprocess
# --------------------------------------------------------------------------- #
class AplayWriter:
    """Subprocess wrapper that pipes raw float32 PCM into aplay
    without blocking the caller.

    Why this exists: the SDK's ``AudioPlayer.play_chunk`` does a
    synchronous ``self._proc.stdin.write(chunk.tobytes())``. With
    ``play_to="both"`` the SDK runs ``audio_player.play_chunk(audio)``
    and ``on_audio_chunk(audio, sr)`` on the same decoder worker
    thread; a 64 KB-OS-pipe filling up blocks the worker ~3 s for a
    4 s chunk, so ``on_audio_chunk`` (the browser's WS feed) trails
    the device by ~3 s. By running aplay ourselves from a dedicated
    thread, the SDK worker never blocks on aplay."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1,
                 volume: float = 0.5, device: str = "plughw:1,0",
                 tempo: float = 1.0) -> None:
        self._volume = float(volume)
        self._tempo = float(tempo or 1.0)
        #: **真实采样率**（记账用 —— 之前写死 24000 ⇒ 换采样率就错）。
        self._sample_rate = int(sample_rate or 24000)
        self._alive = True
        self._sink = None          # 我们真正往里写的那个 stdin
        self._upstream = None      # sox 进程（变速管道时才有）
        #: **电平探针**：缩放前（模型原始）/ 缩放后（真正出声）的峰值。
        self._peak = 0.0
        self._raw_peak = 0.0
        #: **已写入的音频秒数**（变速前）+ **第一块写入的时刻** ——
        #: 用来估算「这一句什么时候播完」（writer 跨句复用 ⇒ 不能靠
        #: ``close()`` 测了，见 :meth:`playback_end`）。
        self._written_sec = 0.0
        self._first_write_ts = 0.0
        #: **最后一次写入**的时刻（wall clock）。
        self._last_write_ts = 0.0
        #: **上一次「消费位置」推进**的时刻 —— ``_tick`` 从它起算
        #: （⚠️ **不能**从 ``_last_write_ts`` 起算 ⇒ 会重复扣减 ⇒ 管道
        #: 被误判成「空了」⇒ 报出来的断音全是假的）。
        self._last_tick_ts = 0.0
        #: **已写入但还没播出去的音频秒数**（变速前）—— 播放头的估算要用
        #: 它（aplay 是**实时消费**的 ⇒ 写进去 ≠ 播出来）。
        self._backlog_sec = 0.0
        #: **估算的「已消费」音频秒数**（变速前）—— 按墙钟推的
        #: （aplay 每墙钟秒消费 ``tempo`` 秒的变速前音频）。
        self._play_pos = 0.0
        # Best-effort: 板载 ES8388 codec 的 ``Speaker`` PA 默认 mute,
        # 用户首次启用 TTS 时听不到声音。play_to 启动 aplay 前 un-mute。
        # ``-c 1`` 选 ES8388 板载 codec(``amixer sset`` 在 default card
        # 上无 Speaker control)。失败不影响正常路径(其它 codec 不一定
        # 有该 control)。
        for cmd in (
            ["amixer", "-c", "1", "sset", "Speaker", "on"],
            ["amixer", "sset", "Speaker", "on"],
        ):
            try:
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2.0, check=False,
                )
                break  # 第一个成功的就用
            except Exception as exc:                   # noqa: BLE001
                LOG.debug("aplay-writer: amixer un-mute Speaker failed: %s", exc)
        cmd = ["aplay", "-t", "raw", "-f", "FLOAT_LE",
               "-r", str(sample_rate), "-c", str(channels)]
        if device:
            cmd += ["-D", device]
        cmd.append("-")
        # ⚠️ **重试**：ALSA 的 hw 设备是**独占**的 —— 同板两个 aplay
        # （同传队列在播 + TTS 卡自己的页面也在播）⇒ 后者会
        # 「Device or resource busy」**起来又立刻退出**（Popen 不会抛，
        # 是 aplay 自己退）⇒ 那条路就哑了，而且**毫无提示**。
        #
        # ⇒ 起 5 次，每次失败等 0.4 s（给前一个播完的机会）；
        # 每次起完都探一下进程还活着没（探活要 0.12 s —— 相对
        # 首窗延迟 4 s 可忽略）。
        last = ""
        ok = False
        self._proc = None
        for attempt in range(5):
            try:
                self._spawn(sample_rate, channels, device)
            except Exception as exc:                   # noqa: BLE001
                last = str(exc)
            else:
                time.sleep(0.12)
                if self._proc.poll() is None:
                    ok = True
                    break
                last = "aplay 启动后立刻退出（ALSA 设备可能被占用）"
                self._kill_spawned()
            if attempt < 4:
                time.sleep(0.4)
        if not ok:
            LOG.warning("aplay-writer: failed to start aplay: %s", last)
            self._alive = False
            self._proc = None
            return
        self._buf = bytearray()
        self._cond = threading.Condition(threading.Lock())
        self._thread = threading.Thread(target=self._drain,
                                        name="aplay-writer", daemon=True)
        self._thread.start()

    def _kill_spawned(self) -> None:
        """把刚起但立刻退的 aplay/sox 清干净（重试前必须清）。"""
        for proc in (self._proc, self._upstream):
            if proc is None:
                continue
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
        self._upstream = None
        self._sink = None

    @property
    def ok(self) -> bool:
        """aplay 进程是否真的起来了（起不来 ⇒ 写进去的 PCM 没人放）。

        同传队列用它判断「这句到底播出去没有」—— 起不来时如果照样
        记「播完」，滞后会被算成一个假的漂亮数字。
        """
        return bool(self._alive and self._proc is not None)

    def set_volume(self, volume: float) -> None:
        """改音量（**不用重开进程**）—— 音量是写 PCM 时乘的。

        ⚠️ **为什么需要**：aplay 管道现在**跨句复用**（消句间静音）⇒
        逐句的音量变化不能再靠「重新 spawn 一个 writer」实现。
        """
        try:
            self._volume = float(volume)
        except (TypeError, ValueError):
            pass

    def reset_peaks(self) -> None:
        """电平探针**逐句复位**（writer 跨句复用 ⇒ 峰值会串到下一句）。"""
        self._peak = 0.0
        self._raw_peak = 0.0

    def written_sec(self) -> float:
        """**累计**已写入的音频秒数（变速前）—— 逐句取差值 = 这句多长。"""
        with self._cond:
            return float(self._written_sec)

    def last_write_ts(self) -> float:
        """**最后一次写入**的时刻（wall clock）。"""
        with self._cond:
            return float(self._last_write_ts)

    def backlog_sec(self) -> float:
        """**已写入但还没播出去**的音频秒数（变速前）= 管道里的存货。

        ⚠️ **这是「播放器会不会断音」的唯一真实指标**：
        * ``> 0`` ⇒ aplay 手里有货 ⇒ 合成再慢也**断不了**；
        * ``== 0`` ⇒ aplay 正在**等下一块** ⇒ 这时候合成一慢就是
        #:   **断音**（用户报的「播放中途卡顿断音」）。
        """
        with self._cond:
            self._tick(time.time())
            return float(self._backlog_sec)

    def _tick(self, now: float) -> None:
        """按墙钟推进「已消费」的位置（**调用方必须持有锁**）。

        ⚠️ aplay 是**实时**消费 post-tempo 音频的 ⇒ 每墙钟秒消费
        ``tempo`` 秒的**变速前**音频 ⇒ 变速前音频的「消费位置」按
        ``tempo`` 推进，而**管道存货**（``_backlog_sec``）就是两者之差。

        ⚠️⚠️ **必须从「上一次 tick」起算，不能从「最后一次写入」起算**
        （实测 2026-11-22）：否则每次 tick 都会把「上次写入到现在的整段
        时间」**再扣一遍** ⇒ 存货被**高估消耗**（实测 1.0 s 的墙钟扣掉
        1.5 s 的音频）⇒ **管道被误判成「空了」** ⇒ 报出来的「断音」全是
        假的（实测每句 1.4 s 的「断音」，其实一秒钟都没断）。
        """
        if self._last_tick_ts <= 0.0:
            self._last_tick_ts = now
            return
        dt = now - self._last_tick_ts
        self._last_tick_ts = now
        if dt <= 0.0:
            return
        consumed = min(self._backlog_sec, dt * self._tempo)
        self._backlog_sec -= consumed
        self._play_pos += consumed

    def playback_end(self) -> float:
        """这一句的音频**大概什么时候播完**（wall clock）。

        ⚠️ **为什么需要**：writer 跨句复用 ⇒ **不能再靠 ``close()`` 的
        「等 aplay 放完」来测「播完时刻」**（那会把整条管道关掉）⇒
        改用「写进去多少 / 播出去多少」估算 —— aplay 是**实时**播放的
        ⇒ 误差在几十毫秒量级。

        ⚠️ **逐句**：调用方在**这句写完**的时刻调用 ⇒ ``_written_sec``
        恰好是「到这句为止的累计」⇒ 减去「已消费」= 这句还没播完的部分
        ⇒ 除以 ``tempo`` = 还要等多久 ⇒ 加上「现在」= 这句播完的时刻。
        """
        with self._cond:
            if not self._last_write_ts:
                return 0.0
            now = time.time()
            self._tick(now)
            remain = self._written_sec - self._play_pos
            if remain <= 0.0:
                return now
            return now + remain / max(self._tempo, 0.01)

    @property
    def peak(self) -> float:
        """真正送进 aplay 的 PCM 峰值（0~1）。"""
        return float(self._peak)

    @property
    def raw_peak(self) -> float:
        """**缩放前**的 PCM 峰值（模型原始输出）。"""
        return float(self._raw_peak)

    # ------------------------------------------------------------------ #
    # 起进程
    # ------------------------------------------------------------------ #
    def _spawn(self, sample_rate: int, channels: int, device: str) -> None:
        """起播放进程链。

        * ``tempo <= 1.001`` ⇒ **只有 aplay**（原路径，一字不改）。
        * ``tempo > 1.001`` ⇒ ``sox(变速) → aplay`` 两段管道。

        ⚠️ **本板 sox 14.4.2 的三个坑**（实测，别再踩）：

        1. ``-D`` **不是**选设备，而是 ``--no-dither`` ⇒ ``play`` 只能用
           **默认** ALSA 设备（板上是 card 0 = hdmiin，**只有采集**）⇒
           变速必须 **sox 负责变速、aplay 负责出声**。
        2. sox 的**效果参数放在输出文件之后** ⇒ ``sox ... - tempo 1.1``。
        3. sox 读 stdin 写 stdout 时，**输入和输出各要一份格式参数**。
        """
        aplay_cmd = cmd = ["aplay", "-t", "raw", "-f", "FLOAT_LE",
                           "-r", str(sample_rate), "-c", str(channels)]
        if device:
            cmd += ["-D", device]
        cmd.append("-")

        if self._tempo > 1.001:
            sox_fmt = ["-t", "raw", "-e", "floating-point", "-b", "32",
                       "-L", "-r", str(sample_rate), "-c", str(channels)]
            sox_cmd = (["sox"] + sox_fmt + ["-"] + sox_fmt + ["-",
                       "tempo", "%.3f" % self._tempo])
            self._upstream = subprocess.Popen(
                sox_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._proc = subprocess.Popen(
                aplay_cmd, stdin=self._upstream.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # 父进程不读 sox 的 stdout ⇒ 关掉这份副本，
            # 否则 aplay 永远等不到 EOF。
            try:
                self._upstream.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            self._sink = self._upstream.stdin
        else:
            self._proc = subprocess.Popen(
                aplay_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._sink = self._proc.stdin
        _tts_log(f"aplay-writer: pid={self._proc.pid} cmd={' '.join(aplay_cmd)}"
                 + (f" tempo={self._tempo}" if self._tempo > 1.001 else ""))

    def write(self, audio) -> None:
        if not self._alive or self._sink is None:
            return
        raw = np.asarray(audio, dtype=np.float32)
        # ⚠️ **电平探针**（诊断用）：记下**缩放前**（模型原始输出）和
        # **缩放后**（真正送到 aplay 的）的峰值。
        #
        # 为什么需要：``volume`` 是**振幅倍率**（不是 aplay 的硬件音量）⇒
        # 面板默认 10% ⇒ 峰值只剩 1/10 ⇒ 板载小喇叭上可能**基本听不见**
        # ⇒ 用户会报「有翻译，但是没有播放」—— 其实是「太小」。
        # ⇒ 没有这个数就没法判断「是没播」还是「播得太小」。
        if raw.size:
            self._raw_peak = max(self._raw_peak,
                                 float(np.max(np.abs(raw))))
        chunk = np.clip(raw * self._volume, -1.0, 1.0)
        if chunk.size:
            self._peak = max(self._peak, float(np.max(np.abs(chunk))))
        with self._cond:
            if not self._alive:
                return
            # ⚠️ **记账**：写了多少音频 + 什么时候写的
            # ⇒ :meth:`playback_end` 用它估「播完时刻」，
            # :meth:`backlog_sec` 用它算「管道里还剩多少货」。
            now = time.time()
            self._tick(now)
            if not self._first_write_ts:
                self._first_write_ts = now
            self._last_write_ts = now
            self._written_sec += raw.size / float(self._sample_rate)
            self._backlog_sec += raw.size / float(self._sample_rate)
            self._buf.extend(chunk.tobytes())
            self._cond.notify()

    def _drain(self) -> None:
        while True:
            with self._cond:
                while self._alive and not self._buf:
                    self._cond.wait()
                if not self._alive and not self._buf:
                    break
                # Snap off in 256 KB chunks so the lock doesn't hold
                # an arbitrary-long memcpy.
                data = bytes(self._buf[:256 * 1024])
                del self._buf[:256 * 1024]
            if self._sink is None:
                self._alive = False
                break
            try:
                self._sink.write(data)
                self._sink.flush()
            except (BrokenPipeError, OSError, ValueError, EOFError):
                # ``ValueError`` / ``EOFError`` happen when ``close()``
                # closed stdin while we were mid-write.
                self._alive = False
                break

    def close(self, drain_timeout: float = 15.0) -> None:
        """Stop accepting chunks; tear down in the right order so
        the drain thread never sees a half-closed subprocess."""
        with self._cond:
            self._alive = False
            self._cond.notify_all()
        # Wait for the drain thread to exit on its own. If it's
        # blocked on stdin.write, the close below closes stdin which
        # unblocks it via BrokenPipeError; we handle that in _drain.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=drain_timeout)
        if self._proc is None:
            return
        # ⚠️ 关的是**我们写的那个** stdin（变速管道时是 sox 的）。
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
            self._sink = None
        # sox 收到 EOF ⇒ 冲完剩余音频 ⇒ 退出 ⇒ aplay 的 stdin 也就 EOF 了。
        if self._upstream is not None:
            try:
                self._upstream.wait(timeout=drain_timeout)
            except Exception:
                pass
            self._upstream = None
        try:
            if self._proc.stdin is not None and self._proc.stdin is not self._sink:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=drain_timeout)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Lazy wdnai SDK loader
# --------------------------------------------------------------------------- #
_WDNAI_CACHE: dict = {}


def _load_wdnai():
    """Lazily import wdnai + the SDK's TTS bits. Only first call hits
    ``import``; subsequent calls return the cached entry."""
    if "_ok" in _WDNAI_CACHE:
        return (
            _WDNAI_CACHE["_ok"],
            _WDNAI_CACHE["_Attachment"],
            _WDNAI_CACHE["_LLMSession"],
            _WDNAI_CACHE["_SystemLanguageModel"],
            _WDNAI_CACHE["_LMOutput"],
            _WDNAI_CACHE["_Generable"],
            _WDNAI_CACHE["_Field"],
            _WDNAI_CACHE["_error"],
        )
    try:
        from wdnai import (
            Attachment,
            Field,
            Generable,
            LMOutput,
            LLMSession,
            SystemLanguageModel,
        )
        _WDNAI_CACHE.update(
            _ok=True,
            _Attachment=Attachment,
            _LLMSession=LLMSession,
            _SystemLanguageModel=SystemLanguageModel,
            _LMOutput=LMOutput,
            _Generable=Generable,
            _Field=Field,
            _error="",
        )
        return True, Attachment, LLMSession, SystemLanguageModel, LMOutput, Generable, Field, ""
    except Exception as e:
        _WDNAI_CACHE.update(
            _ok=False,
            _Attachment=None,
            _LLMSession=None,
            _SystemLanguageModel=None,
            _LMOutput=None,
            _Generable=None,
            _Field=None,
            _error=str(e),
        )
        return False, None, None, None, None, None, None, str(e)


# --------------------------------------------------------------------------- #
# Structured output schema — inherits LMOutput so the SDK can populate
# ``attachment`` automatically with the synthesised WAV bytes.
# --------------------------------------------------------------------------- #
_TTS_RESULT_CLS = None


def _get_tts_result_cls():
    """Build the ``TTSResult`` schema lazily, once wdnai is importable."""
    global _TTS_RESULT_CLS
    if _TTS_RESULT_CLS is not None:
        return _TTS_RESULT_CLS
    from typing import Optional
    _, _, _, _, LMOutput, Generable, Field, _ = _load_wdnai()

    @Generable
    class TTSResult(LMOutput):
        """Qwen3-TTS output: ``text`` echoes the prompt, ``attachment``
        carries the 24 kHz mono float32 WAV bytes (SDK fills it in)."""
        text:        str = Field(description="The input prompt that was synthesised.")
        sample_rate: int = Field(default=24000)
        channels:    int = Field(default=1)

    _TTS_RESULT_CLS = TTSResult
    return _TTS_RESULT_CLS


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """Decode the audio duration from a RIFF/WAVE header."""
    if not wav_bytes or len(wav_bytes) < 44:
        return 0.0
    try:
        bio = io.BytesIO(wav_bytes)
        head = bio.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0.0
        sample_rate = 24000
        bits_per_sample = 32
        num_channels = 1
        data_size = 0
        while True:
            head = bio.read(8)
            if len(head) < 8:
                break
            cid, csize = head[:4], struct.unpack("<I", head[4:8])[0]
            cdata = bio.read(csize)
            if cid == b"fmt ":
                # fmt chunk: format_tag(2) channels(2) sample_rate(4) byte_rate(4) block_align(2) bits_per_sample(2)
                _fmt, num_channels, sample_rate, _br, _ba, bits_per_sample = struct.unpack(
                    "<HHIIHH", cdata[:16]
                )
            elif cid == b"data":
                data_size = csize
                break
            if csize % 2:
                bio.read(1)
        if sample_rate > 0 and num_channels > 0 and bits_per_sample > 0 and data_size > 0:
            bytes_per_frame = num_channels * bits_per_sample // 8
            return data_size / (sample_rate * bytes_per_frame)
    except Exception:
        pass
    return 0.0


def _assemble_wav(pcm: np.ndarray, sample_rate: int) -> bytes:
    """从 flat float32 PCM ndarray 构建 RIFF/WAVE 字节串。

    格式 mirror SDK：format_tag=3 (IEEE_FLOAT)、1 通道、sample_rate Hz、
    32-bit LE float。可经 _wav_duration_seconds round-trip，saved 文件
    报告时长与累积 PCM 长度一致。
    """
    if pcm is None or pcm.size == 0:
        return b""
    sr = int(sample_rate) if sample_rate else 24000
    bits_per_sample = 32
    num_channels = 1
    byte_rate = sr * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    pcm_bytes = np.ascontiguousarray(pcm, dtype="<f4").tobytes()
    data_size = len(pcm_bytes)
    fmt_size = 16
    riff_size = 4 + (8 + fmt_size) + (8 + data_size)
    return (
        struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
        + struct.pack(
            "<4sIHHIIHH",
            b"fmt ", fmt_size, 3, num_channels, sr, byte_rate,
            block_align, bits_per_sample,
        )
        + struct.pack("<4sI", b"data", data_size)
        + pcm_bytes
    )


# --------------------------------------------------------------------------- #
# TTSRequest 已迁至 app.core.audio.tts_bridge(顶部 re-export)。

# Service singleton
# --------------------------------------------------------------------------- #
class TtsSession:
    """Single Qwen3-TTS session. State::

        idle → start() → loading → LLMSession ready → running
              → done / error       (terminal)
              → stop()  → stopped (terminal)

    Mirrors ``VideoSession`` / ``WdnaiDetector`` so the JS side can
    treat all three uniformly (same snapshot fields, broadcaster API).
    """

    STATE_IDLE       = "idle"
    STATE_LOADING    = "loading"
    STATE_RUNNING    = "running"
    STATE_DONE       = "done"
    STATE_ERROR      = "error"
    STATE_STOPPED    = "stopped"
    STATE_UNAVAILABLE = "unavailable"

    HISTORY_MAX = 50

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        # ``_lock`` is ``RLock`` because ``stop()`` returns ``snapshot()``
        # from inside its critical section; a plain ``Lock`` would
        # deadlock with the no-op ``stop()`` path on idle shutdown.
        self._lock = threading.RLock()
        self._state = self.STATE_IDLE
        self._error = ""
        self._init_error = ""
        self._request: Optional[TTSRequest] = None
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Active SDK session is owned by ComputeRegistry (算力配置) — see
        # ``app/services/compute_registry.py``. TtsSession no longer
        # builds an LLMSession itself; ``_infer_one`` looks it up via
        # ``get_registry().get_session("swcap_text_to_speech")`` and surfaces
        # "功能未启用: swcap_text_to_speech" if no instance is running.

        # Latest snapshot data surfaced to the WS
        self._frames = 0
        self._audio_sec = 0.0
        self._elapsed = 0.0
        self._output: Optional[Path] = None
        self._last_started = 0.0

        # Output WAV history
        self._history: List[dict] = []
        self._history_lock = threading.Lock()

        # Independent aplay subprocess used only for ``play_to=both``
        # so the SDK's decoder worker isn't blocked by aplay's stdin
        # pipe (which used to delay the browser-side WS feed by ~3 s
        # per chunk). ``None`` otherwise.
        self._aplay_writer: Optional[AplayWriter] = None

        self._broadcast_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

        # PCM-chunk subscribership (used when ``play_to`` involves the
        # browser). See ``subscribe_audio`` / ``_on_audio_chunk``.
        self._audio_lock = threading.Lock()
        self._audio_subs: dict = {}

        # 分片 UDS：worker 把 PCM 流回主进程。bound method 无法跨
        # wdndds RPC pickle，此路径是 live audio 的唯一通道。
        self._pcm_sock: Optional[socket.socket] = None
        self._pcm_sock_path: str = ""
        # 本次 run 的 UDS reader 线程（等它退出 ⇒ PCM 收齐了）。
        self._pcm_thread: Optional[threading.Thread] = None

        # **PCM 分片回调**（同传队列用）：``play_to=queue`` 时，会话只
        # 产出 PCM、不自己播 ⇒ 由队列的播放器线程消费这些分片。
        #
        # 为什么需要它：``PLAY_BOTH`` 的播放是「边合成边播」⇒ 下一句的
        # 合成必须等这一句的**合成**结束，而这一句的**播放**还没完 ⇒
        # 每句白等一个首窗延迟（3.7 s）⇒ 吞吐 0.67× 实时。
        # 拆开之后：合成完就把 PCM 交出去，播放线程接着播，下一句同时
        # 开始合成 ⇒ 吞吐 ≈ 1× 实时。
        self._chunk_sink: Optional[Callable[[np.ndarray, int], None]] = None
        # 本轮第一块 PCM 的到达时刻（wall clock）—— 同传首窗延迟的度量。
        self._run_first_pcm_t: Optional[float] = None

# per-run PCM 累加器（重组 saved WAV 用）。worker 不再在 RPC reply
        # 里塞 WAV 字节（那条路径触发 LAN 120s 超时），这里从浏览器
        # 正在播的同一 chunk 流拼回去。
        self._run_pcm_chunks: List[np.ndarray] = []
        self._run_pcm_sr: int = 24000
        self._run_pcm_lock = threading.Lock()

        # LAN worker 的 DDS 分片 reader：TopicReader 挂在
        # wdndds/audio/tts_chunk/<model_id>。本机无 DDS writer，
        # 订阅一直 idle；LAN worker UDS 连不上时由 _run_safe 启用，
        # finally 里 close。
        self._dds_chunk_reader: Optional[object] = None

        # 专属 audio chunk Hub（独立 DomainParticipant）：registry 的 Hub
        # 上挂了 set_peer_listener，会干扰同 participant 的
        # create_topic_reader（实测回调从不触发）。本 Hub 无 peer_listener，
        # 进程级常驻。
        self._audio_chunk_hub: Optional[Any] = None
        self._audio_chunk_hub_lock = threading.Lock()

        self._model_dir = self._resolve_model_dir(model_dir)

        atexit.register(self._cleanup_audio_chunk_hub)

        # Probe once at boot so the UI can render the banner without
        # waiting for the first /start request.
        ok, _, _, _, _, _, _, err = _load_wdnai()
        if not ok:
            self._state = self.STATE_UNAVAILABLE
            self._init_error = err
            LOG.warning("TTS unavailable: %s", err)

    @staticmethod
    def _resolve_model_dir(override: Optional[Path]) -> Path:
        if override is not None:
            return Path(override)
        env = os.environ.get("TTS_MODEL_DIR")
        if env:
            return Path(env)
        # Fall back to ``default_models_root()`` so off-device hosts
        # without ``/userdata/models`` still find the model dir under
        # ``/mnt/oem/models`` or ``<HOME>/models``.
        from app.core.config import default_models_root
        root = os.environ.get("WDNAI_MODELS_DIR", str(default_models_root()))
        return Path(root) / "Qwen3-TTS"

    @staticmethod
    def _read_device_name() -> str:
        """解析 device_name（mirror compute_registry._read_device_name，
        单调用点就不导出公开 helper）。fallback 到 socket.gethostname()
        保证 participant_name 非空。"""
        try:
            from app.core.config import get_config
            base = get_config().get_section("base") or {}
            v = (base.get("device_name") or "").strip()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        return socket.gethostname()

    def _get_or_init_audio_chunk_hub(self) -> Optional[Any]:
        """懒构造 + 缓存专属 audio chunk DDS Hub。

        已建则直接返回；否则起一个无 set_peer_listener 的 Hub，进程级
        常驻。失败（cshim 缺失 / NIC 超时等）打 warning、cache 清 None，
        调用方 fallback 到 WAV-only 路径。
        并发：_audio_chunk_hub_lock 保护 —— 多次 _dds_chunk_reader_thread
        并发进来会重复 init 出同名 DomainParticipant 泄漏一个。"""
        with self._audio_chunk_hub_lock:
            if self._audio_chunk_hub is not None:
                return self._audio_chunk_hub
            try:
                from wdndds.dds._rpc import Hub as _RpcHub
                device_name = self._read_device_name()
                hub = _RpcHub(
                    participant_name=f"wdnwebui-audio-chunk-{device_name}",
                    domain_id=0,
                    discovery_servers=[],
                )
                hub.start()
                # 故意不挂 set_peer_listener —— 整件事就是要把 audio
                # topic reader 跟 registry 的 capability-adoption
                # peer_listener 隔开。
                self._audio_chunk_hub = hub
                try:
                    hub_guid = hub.guid()
                except Exception:
                    hub_guid = "<unknown>"
                _tts_log(
                    f"dedicated audio chunk hub ready "
                    f"(participant=wdnwebui-audio-chunk-{device_name} "
                    f"guid={hub_guid})"
                )
                return hub
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "audio chunk Hub init failed: %s; "
                    "LAN streaming disabled, WAV fallback still works",
                    exc,
                )
                self._audio_chunk_hub = None
                return None

    def _cleanup_audio_chunk_hub(self) -> None:
        """atexit 注册的进程退出钩子。Hub.close() 幂等，best-effort 防止
        DomainParticipant 滞留到解释器退出之后。"""
        hub = self._audio_chunk_hub
        if hub is None:
            return
        try:
            hub.close()
        except Exception:  # noqa: BLE001
            pass
        self._audio_chunk_hub = None

    @property
    def available(self) -> bool:
        ok, *_ = _load_wdnai()
        return ok

    @property
    def model_dir(self) -> Path:
        return self._model_dir

    @staticmethod
    def _probe_capability_ready() -> bool:
        """能力是否可用（含 LAN peer，与 ``start()`` 的 ``get_session`` 同口径）。

        ``list_sessions`` 是纯查询；``get_session`` 每次会打一条 INFO，
        而 snapshot 由前端 5s 轮询 / WS 推进调用，不能污染日志。
        """
        try:
            from app.core.compute.registry import get_registry
            return bool(get_registry().list_sessions(
                "swcap_text_to_speech", include_remote=True))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _probe_capability_loading() -> List[str]:
        """本机持有 TTS 能力但**还在加载**的 model id 列表。

        与 ``_probe_capability_ready`` 是同一件事的两面：ready=False 时，
        只有这里非空才能区分「模型正在加载」和「压根没启用」—— 前者等
        一等就好，后者要去算力配置页开。前端 banner / 按钮 title 靠它
        给准确文案（不然加载中也会显示「模型未启用」，误导用户）。

        只看本机（peer 上的加载进度本机看不到，报「加载中」会让用户
        对着一个永远不会就绪的进度条干等）—— 口径同 registry 的
        ``pending_models_for_capability``。
        """
        try:
            from app.core.compute.registry import get_registry
            return list(get_registry().pending_models_for_capability(
                "swcap_text_to_speech"))
        except Exception:  # noqa: BLE001
            return []

    def snapshot(self) -> dict:
        capability_ready = self._probe_capability_ready()
        with self._lock:
            return {
                "state":          self._state,
                "available":      self.available,
                "capability_ready": capability_ready,
                # 本机有该能力但还在加载的模型（空 = 真的没启用）
                "capability_loading": self._probe_capability_loading(),
                "init_error":     self._init_error,
                "model_dir":      str(self._model_dir),
                "model_dir_exists": self._model_dir.is_dir(),
                "running":        self._state == self.STATE_RUNNING,
                "busy":           self._state in (self.STATE_LOADING, self.STATE_RUNNING),
                "frames":         self._frames,
                "audio_sec":      round(self._audio_sec, 3),
                "elapsed":        round(self._elapsed, 3),
                "output":         str(self._output) if self._output else "",
                "output_url":     self._output_url_for(self._output) if self._output else "",
                "error":          self._error,
                "request":        self._request_to_dict(self._request) if self._request else None,
                "history_count":  len(self._history),
            }

    def history(self) -> List[dict]:
        with self._history_lock:
            return list(self._history)

    @staticmethod
    def _request_to_dict(req: Optional[TTSRequest]) -> Optional[dict]:
        if req is None:
            return None
        return {
            "text":        req.text,
            "language":    req.language,
            "speaker":     req.speaker,
            "ref_speaker": req.ref_speaker,
            "volume":      req.volume,
            "no_play":     req.no_play,
            "play_to":     req.play_to,
            "output":      req.output,
        }

    @staticmethod
    def _output_url_for(path: Optional[Path]) -> str:
        """Return a stable URL the browser can use to fetch the saved WAV.

        The endpoint lives at ``/api/tts/output`` (see ``app/api/tts.py``)
        and is parameterised by the on-disk file path so the same snapshot
        shape (an absolute ``output`` + a relative ``output_url``) survives
        web reloads and history lookups. Path is URL-encoded (``/`` left
        intact) so future upload dirs with spaces in their name keep working."""
        if path is None:
            return ""
        return "/api/tts/output?path=" + urllib.parse.quote(
            str(path), safe="/")

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Audio-chunk subscribership
    # ------------------------------------------------------------------ #
    # WS clients that want PCM chunks subscribe here; the SDK's
    # ``on_audio_chunk`` callback (called from the decoder worker
    # thread) pushes the float32 ndarray into ``_audio_queue`` and we
    # hand it out per subscriber on the asyncio side. Per-subscriber
    # byte queues are bounded; if a slow client doesn't keep up we
    # drop the oldest chunk (latest-only, mirroring the rest of the
    # service's "drop oldest" policy for freshest data).
    def subscribe_audio(self, sub_id: str, loop: asyncio.AbstractEventLoop) -> "asyncio.Queue[bytes]":
        """Register a subscriber; return an asyncio Queue that will
        receive raw float32 LE bytes plus a sentinel ``None`` on EOF."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        with self._audio_lock:
            self._audio_subs[sub_id] = (loop, q)
            return q

    def unsubscribe_audio(self, sub_id: str) -> None:
        with self._audio_lock:
            self._audio_subs.pop(sub_id, None)

    def _broadcast(self, event: dict) -> None:
        with self._broadcast_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(event)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("tts broadcaster raised: %s", exc)

    def _on_audio_chunk(self, audio, sample_rate: int) -> None:
        """SDK callback: a new float32 mono PCM chunk is ready.

        Runs on the SDK's decoder thread. Convert to little-endian
        bytes once and push to every subscriber's asyncio.Queue via
        ``call_soon_threadsafe``; each subscriber's WS handler then
        forwards the bytes as a binary frame.
        """
        if audio is None or audio.size == 0:
            return
        produced_sec: Optional[float] = None
        # Per-chunk header: 8-byte (sample_rate, channels, sample_count)
        # little-endian. Browser ignores it for now (it uses the
        # constructor-passed SR) but it's there for future re-emit.
        try:
            chunk = audio.astype("<f4", copy=False).tobytes()
            sr = int(sample_rate)
            n = int(audio.size)
            header = struct.pack("<III", sr, 1, n) + chunk
        except Exception as exc:               # noqa: BLE001
            LOG.debug("audio chunk encode failed: %s", exc)
            return

        with self._audio_lock:
            subs = list(self._audio_subs.items())
        for _sub_id, (loop, q) in subs:
            try:
                loop.call_soon_threadsafe(self._enqueue_chunk, q, header)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("audio schedule failed: %s", exc)

        # Mirror to the local aplay subprocess when ``play_to=both``.
        # The writer's drain thread ships bytes to aplay's stdin
        # asynchronously, so the SDK worker never blocks here.
        if self._aplay_writer is not None:
            try:
                self._aplay_writer.write(audio)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("aplay-writer write failed: %s", exc)

        # 同传队列的分片出口（``play_to=queue``）：会话只产出 PCM，播放
        # 由队列自己的播放器线程做 ⇒ 下一句的合成可以和这一句的播放重叠。
        sink = self._chunk_sink
        if sink is not None:
            try:
                sink(audio, sr)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("chunk sink raised: %s", exc)
        # 本轮**第一块** PCM 的到达时刻（同传首窗延迟的度量）。
        if self._run_first_pcm_t is None:
            self._run_first_pcm_t = time.time()

        # 累加 PCM 用于合成结束后拼 WAV。worker 不再在 RPC reply 里塞 WAV，
        # 必须从浏览器正在听的同一 chunk 流重建。lock 短暂持有即可，
        # 让 _run_safe 能在推理完成后 snapshot + clear。
        try:
            flat = np.asarray(audio, dtype="<f4").reshape(-1)
            with self._run_pcm_lock:
                if flat.size:
                    self._run_pcm_chunks.append(flat)
                    if self._run_pcm_sr <= 0:
                        self._run_pcm_sr = sr
                    produced_sec = sum(c.size for c in self._run_pcm_chunks
                                       ) / float(max(sr, 1))
        except Exception as exc:               # noqa: BLE001
            LOG.debug("pcm accumulate failed: %s", exc)

        # Progress tick. This used to be the SDK's ``on_step(n_frames,
        # audio_sec)`` callback, which only existed in-process; now that
        # inference runs elsewhere it can't cross the wire. Deriving it
        # from the PCM stream is strictly better anyway: it's the audio
        # actually produced and actually on its way to the speakers,
        # not the decoder's internal frame counter.
        if produced_sec is not None:
            self._on_step(int(produced_sec * 50), produced_sec)

    @staticmethod
    def _enqueue_chunk(q: asyncio.Queue, data: bytes) -> None:
        """Runs on the subscriber's asyncio loop. Drop-oldest if the
        queue is full so a slow client doesn't backpressure the SDK."""
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _finish_audio_stream(self) -> None:
        """Send a ``None`` sentinel to every subscriber so the WS
        handler can close the audio portion of the stream cleanly."""
        with self._audio_lock:
            subs = list(self._audio_subs.items())
        for _sub_id, (loop, q) in subs:
            try:
                loop.call_soon_threadsafe(self._enqueue_chunk, q, b"EOF")
            except Exception as exc:           # noqa: BLE001
                LOG.debug("audio EOF schedule failed: %s", exc)

    def _dds_chunk_reader_thread(self, model_id: str) -> None:
        """订阅 LAN TTS worker 的音频分片 topic。

在 **专属 audio chunk Hub**（独立 DomainParticipant，与 registry
        Hub 隔离）上挂 cshim-backed DDS reader，避开 registry participant
        上 set_peer_listener 对 create_topic_reader 的干扰。每片 payload
        喂进 _on_audio_chunk（与 UDS reader 同路径）。
        cshim pump 由 wdndds 托管，本方法只建订阅、handle 存到
        self._dds_chunk_reader 供 _run_safe finally 关闭。
        Hub 进程级常驻，首次调用懒构造、后续复用。

        本机 worker 不向这个 topic 发 DDS（走 UDS），回调一直 idle，
        cleanup 时关闭 reader 即可。
        """
        self._dds_chunk_reader = None
        hub = self._get_or_init_audio_chunk_hub()
        if hub is None:
            LOG.debug(
                "DDS chunk reader: dedicated audio hub unavailable; "
                "LAN streaming off (WAV fallback still works)"
            )
            return

        def _on_chunk(payload: bytes) -> None:
            try:
                if len(payload) < 12:
                    LOG.debug(
                        "DDS chunk short header: got %d bytes", len(payload))
                    return
                sr, ch, n = struct.unpack("<III", payload[:12])
                pcm = payload[12:]
                want = n * ch * 4
                if len(pcm) < want:
                    LOG.debug(
                        "DDS chunk short: got %d bytes need %d",
                        len(pcm), want)
                    return
                audio = np.frombuffer(
                    pcm[:want], dtype="<f4").reshape(n, ch)
                self._on_audio_chunk(audio, sr)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("DDS chunk decode failed: %s", exc)

        try:
            from app.core.compute.registry import (
                subscribe_tts_chunk_topic,
            )
            reader = subscribe_tts_chunk_topic(hub, model_id, _on_chunk)
            self._dds_chunk_reader = reader
            _tts_log(
                f"DDS chunk reader subscribed via dedicated audio hub: "
                f"model_id={model_id}"
            )
        except Exception as exc:               # noqa: BLE001
            LOG.warning(
                "DDS chunk reader subscribe failed: %s; LAN streaming off",
                exc)
            self._dds_chunk_reader = None

    def _emit(self) -> None:
        self._broadcast(self.snapshot())

    # ------------------------------------------------------------------ #
    def start(self, req: TTSRequest) -> dict:
        if not self.available:
            raise RuntimeError(f"TTS 不可用: {self._init_error}")
        if not (req.text or "").strip():
            raise ValueError("text 不能为空")

        # Force the LMOutput subclass to materialise now so any import
        # error surfaces here, before the worker is spawned.
        _get_tts_result_cls()

        with self._lock:
            live_worker = self._worker is not None and self._worker.is_alive()
            if live_worker or self._state in (self.STATE_LOADING, self.STATE_RUNNING):
                raise RuntimeError(
                    "TTS 正在运行或正在清理，请稍候再试"
                )
            self._state = self.STATE_LOADING
            self._error = ""
            self._frames = 0
            self._audio_sec = 0.0
            self._elapsed = 0.0
            self._output = None
            self._request = req
            self._run_first_pcm_t = None
            with self._run_pcm_lock:
                self._run_pcm_chunks = []
                self._run_pcm_sr = 24000

        if not req.output:
            req.output = str(self._default_output_path())
        # 不要在这里赋 self._output —— _run_safe 写盘后才会赋，
        # 否则 stop 中断会留下 phantom output_url → 404。
        Path(req.output).parent.mkdir(parents=True, exist_ok=True)

        # For ``play_to=both`` we run our own aplay subprocess via
        # ``AplayWriter`` so the SDK's worker thread never blocks on
        # aplay's 64 KB stdin pipe. The SDK still receives each
        # chunk via ``on_audio_chunk`` and forwards it both to the
        # browser (immediate) and to the writer (queued to aplay).
        # For ``device`` we let the SDK own the aplay path; for
        # ``browser`` we don't touch aplay at all.
        if self._aplay_writer is not None:
            try:
                self._aplay_writer.close()
            except Exception:
                pass
        self._aplay_writer = None
        if req.play_to == PLAY_BOTH:
            try:
                # ⚠️ ``tempo>1`` ⇒ sox 变速管道（sox→aplay）；1.0 ⇒ 原路径。
                self._aplay_writer = AplayWriter(volume=req.volume,
                                                 tempo=req.tempo)
            except Exception as exc:
                LOG.warning("aplay-writer: could not be created: %s", exc)

        self._stop_event.clear()
        self._last_started = time.time()
        self._worker = threading.Thread(
            target=self._run_safe, args=(req,),
            name="tts-session", daemon=True,
        )
        self._worker.start()
        return self.snapshot()

    def _default_output_path(self) -> Path:
        # TTS 生成的 WAV 落到 config 资源目录的 ``tts`` 子目录下(典型
        # 设备端 ``/userdata/test/tts``,off-device ``<HOME>/test/tts``),
        # 不要走 ``get_card_uploads_root('tts')``(那个在 emmc 上的 repo
        # 内,emmc 寿命扛不住频繁写)。解析走
        # :func:`app.core.uploads.get_tts_output_root`,跟
        # ``api.tts_output`` 的路径校验同源。
        from app.core.uploads import get_tts_output_root
        return get_tts_output_root() / f"tts-{int(time.time())}.wav"

    def run_queued(self, job, sink=None) -> dict:
        """**同传队列用**：同步跑完一句（队列线程串行调用）。

        与 ``start()`` 的区别：

        * ``start()`` = 「发起就返回」（页面用），忙的时候抛「正在运行」；
        * ``run_queued()`` = 「**跑完才返回**」⇒ 队列线程天然串行 ⇒
          不会两条流交错，也不用抢「忙不忙」。

        ⚠️ ``save_wav=False`` ⇒ 不落 WAV（同传一次会议能写几百 MB）。
        ⚠️ ``play_to=queue`` ⇒ **会话自己不播**，PCM 通过 ``sink`` 回调
        交给队列的播放器线程 ⇒ 下一句的合成可以和这一句的播放**重叠**
        （``PLAY_BOTH`` 做不到：合成和播放焊死在一句里 ⇒ 每句多付一个
        3.7 s 的首窗延迟 ⇒ 吞吐只有 0.67× 实时）。

        ``sink`` 签名 ``(audio: np.ndarray, sample_rate: int) -> None``，
        在 **UDS reader 线程**上被调用（每个 decoder 窗口一次）。
        """
        if not self.available:
            raise RuntimeError(f"TTS 不可用: {self._init_error}")
        text = str(getattr(job, "text", "") or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        # ⚠️ TTS 卡自己的页面可能正在合成（``start()`` 忙时抛错）。
        # 同传**不能因此丢句子** ⇒ 先等它空出来（最多 60 s）。
        deadline = time.time() + 60.0
        while time.time() < deadline:
            with self._lock:
                busy = self._state in (self.STATE_LOADING, self.STATE_RUNNING)
            if not busy:
                break
            time.sleep(0.2)
        req = TTSRequest(
            text=text,
            language=str(getattr(job, "language", "auto") or "auto"),
            speaker=str(getattr(job, "speaker", "") or ""),
            # ⚠️ **参考音色走 ``ref_speaker``**（与内置音色 ``speaker`` 是
            # 两个不同的 SDK 参数）—— 混用 ⇒ SDK 打印「not in SPK_ID,
            # skipping」⇒ **没有音色条件** ⇒ 每句话音色都不一样。
            ref_speaker=str(getattr(job, "ref_speaker", "") or ""),
            volume=float(getattr(job, "volume", 0.1) or 0.1),
            # ⚠️ **PLAY_QUEUE**（不是 PLAY_BOTH）⇒ 会话只产出 PCM，播放
            # 交给队列的播放器线程 ⇒ 合成与播放可以重叠。
            play_to=PLAY_QUEUE,
            tempo=float(getattr(job, "tempo", 1.0) or 1.0),
            save_wav=False,
            # ⚠️ **首窗帧数**（同传用）—— 上游首窗硬等 50 帧 ⇒ 实测
            # 「合成首窗」≈ 3.0 s（同传链路上最大的一段固定延迟）。
            # decoder 是因果的 ⇒ 攒 25 帧即可开声 ⇒ 首声提前 ~1.5 s。
            first_window_frames=int(getattr(job, "first_window_frames", 0) or 0),
        )
        # ⚠️ sink 必须在 ``start()`` **之前**挂上 —— ``start()`` 会立刻
        # 起 worker 线程，PCM 分片随时可能到。
        self._chunk_sink = sink
        try:
            self.start(req)
            deadline = time.time() + 180.0
            while time.time() < deadline:
                with self._lock:
                    state = self._state
                if state not in (self.STATE_LOADING, self.STATE_RUNNING):
                    break
                time.sleep(0.05)
        finally:
            self._chunk_sink = None
        return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            if self._state not in (self.STATE_LOADING, self.STATE_RUNNING):
                return self.snapshot()
            self._state = self.STATE_STOPPED
            self._error = ""
        self._stop_event.set()
        # Tell the underlying LLM session to stop. ``rknn3lite``
        # forwards ``session_stop()`` to the C runtime which aborts
        # the in-flight inference cleanly. The session is owned by
        # ComputeRegistry (算力配置), so we look it up by capability.
        try:
            from app.core.compute.registry import get_registry
            sess = get_registry().get_session(SoftwareCapability.TEXT_TO_SPEECH)
        except RuntimeError:
            sess = None
        if sess is not None:
            try:
                # ``sess.stop()`` publishes the cancel out-of-band and
                # returns immediately; the worker notices once inference
                # returns. It is also the only thing that can work here:
                # the session lives in another process, so ``sess._backend``
                # is None and reaching for a C handle through it was
                # silently a no-op.
                sess.stop()
            except Exception as exc:           # noqa: BLE001
                LOG.debug("session.stop raised: %s", exc)
        return self.snapshot()

    def close(self) -> None:
        self.stop()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5)
        self._worker = None
        # LLMSession 由 ComputeRegistry 持有并负责释放 —— 这里只停本服务的 worker。

    def _infer_one(self, req: TTSRequest):
        """Single synchronous TTS inference: ``synthesize, return WAV bytes``.

        Wraps ``LLMSession.respond`` in a private asyncio loop because the
        C-level ``rknn3_session_run`` is blocking; the SDK is async-
        facing only for forwarding. ``no_play``/``stop_event``/``on_step``
        are forwarded so the SDK streams chunks live (when ``no_play``
        is False), the user can interrupt mid-run, and the WS UI gets
        incremental ``frames``/``audio_sec`` ticks.

        ``req.play_to`` (``device`` / ``browser`` / ``both``) is mapped
        onto the SDK's binary ``no_play`` flag: device-only and both
        keep device playback on (``no_play=False``); browser-only skips
        it so the SDK doesn't touch the host sound card at all
        (``no_play=True``).

        LLMSession 由 ComputeRegistry 统一管理（"算力配置"页面启用）。
        找不到 swcap_text_to_speech capability → RuntimeError("功能未启用: swcap_text_to_speech")。
        """
        try:
            from app.core.compute.registry import get_registry
            sess = get_registry().get_session(SoftwareCapability.TEXT_TO_SPEECH)
        except RuntimeError as e:
            # 「功能未启用」有两种完全不同的成因，用户看到的文案必须区分：
            #   a) 本机有该能力的实例但**还在加载** → 等一等就好；
            #   b) 压根没有该能力的实例 → 要去算力配置页启用。
            # 混成一句话，用户会以为是自己没开模型，其实模型正在起来 ——
            # 向导跑完「没出声」的那次就是这么误判的。
            pending = self._probe_capability_loading()
            if pending:
                msg = (f"TTS 模型正在加载中({pending[0]}),"
                       "请等模型就绪后重试")
                # 「加载中」不是 init_error —— 别把这条留在快照里,
                # 它只在 available=false 时才会显示,留着会误导。
            else:
                msg = str(e)
                self._init_error = msg
            raise RuntimeError(msg) from e
        try:
            ok, Attachment, *_ = _load_wdnai()
            TTSResult = _get_tts_result_cls()

            sess.reset()
            # ``play_to=both`` runs our own aplay via ``AplayWriter``,
            # so we tell the SDK to skip its own (blocking) aplay
            # path. ``play_to=device`` keeps the SDK's original
            # behaviour; ``play_to=browser`` already needs ``no_play``.
            no_play = req.play_to in (PLAY_BROWSER, PLAY_BOTH, PLAY_QUEUE)

            async def _run() -> TTSResult:
                kwargs = dict(
                    attachment=[Attachment(req.text, kind="text")],
                    generating=TTSResult,
                    language=req.language,
                    speaker=req.speaker,
                    ref_speaker=req.ref_speaker,
                    instruct=req.instruct,
                    volume=req.volume,
                    no_play=no_play,
                    non_streaming_mode=req.non_streaming_mode,
                                    )
                # ⚠️ **首窗帧数**透传给 SDK（0 = SDK 默认 25 帧）。
                # ⚠️ **必须显式带上** —— 不带的话 SDK 用默认 50 帧 ⇒
                # 首声要多等 ~1.5 s（同传链路最大的固定延迟）。
                _fw = int(getattr(req, "first_window_frames", 0) or 0)
                if _fw > 0:
                    kwargs["first_window_frames"] = _fw
                # **只传路径，不传回调。** 推理在另一个进程，callable
                # 无法 JSON 序列化；worker 收到这个路径后自己 connect
                # 并装 ``on_audio_chunk``，把 PCM 推回本进程在
                # _run_safe 里起的 AF_UNIX reader（同一条
                # ``_on_audio_chunk`` 路径）。LAN peer 走
                # ``wdndds/audio/tts_chunk/<model_id>`` 兜底
                # （见 ``_dds_chunk_reader_thread``）。
                if self._pcm_sock_path:
                    kwargs["tts_pcm_socket_path"] = self._pcm_sock_path
                return await sess.respond(**kwargs)
            return asyncio.run(_run())
        except Exception as e:  # pragma: no cover - depends on NPU
            raise RuntimeError(f"TTS 推理失败: {e}")

    def _on_step(self, n_frames: int, audio_sec: float) -> None:
        """SDK decoder-worker callback: tick the snapshot under ``_lock``
        and broadcast. Runs off the main thread."""
        with self._lock:
            self._frames = n_frames
            self._audio_sec = round(float(audio_sec), 3)
            self._elapsed = round(time.time() - self._last_started, 3)
        self._emit()

    # ------------------------------------------------------------------ #
    def _run_safe(self, req: TTSRequest) -> None:
        t0 = time.time()
        sock: Optional[socket.socket] = None
        sock_path = ""
        try:
            self._set_state(self.STATE_RUNNING, "synthesizing")

            # When the browser (or both) is the audio sink, stand up
            # the per-chunk Unix domain socket BEFORE the RPC so the
            # worker can connect and start streaming as soon as its SDK
            # decoder thread produces its first window. ``play_to=
            # device`` skips this path entirely (SDK owns the audio
            # output; no WS feed needed).
            if req.play_to in (PLAY_BROWSER, PLAY_BOTH, PLAY_QUEUE):
                sock_path = (
                    f"/tmp/wdnwebui-tts-{os.getpid()}-{time.time_ns()}.sock"
                )
                try:
                    if os.path.exists(sock_path):
                        os.unlink(sock_path)
                except OSError:
                    pass
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(sock_path)
                sock.listen(1)
                sock.settimeout(60.0)
                self._pcm_sock = sock
                self._pcm_sock_path = sock_path

                def _pcm_reader():
                    conn = None
                    try:
                        conn, _ = sock.accept()
                        conn.settimeout(None)
                        f = conn.makefile("rb")
                        while True:
                            header = f.read(12)
                            if not header or len(header) < 12:
                                break
                            try:
                                sr, ch, n = struct.unpack("<III", header)
                            except struct.error:
                                break
                            want = n * ch * 4
                            buf = b""
                            while len(buf) < want:
                                piece = f.read(want - len(buf))
                                if not piece:
                                    break
                                buf += piece
                            if len(buf) < want:
                                break
                            audio = np.frombuffer(
                                buf, dtype="<f4").reshape(n, ch)
                            self._on_audio_chunk(audio, sr)
                    except Exception as e:           # noqa: BLE001
                        LOG.debug("pcm reader terminated: %s", e)
                    finally:
                        try:
                            if conn is not None:
                                conn.close()
                        except Exception:
                            pass

                self._pcm_thread = threading.Thread(
                    target=_pcm_reader,
                    name="tts-pcm-reader",
                    daemon=True,
                )
                self._pcm_thread.start()
            # LAN fallback：worker 连不上 UDS 就改发
            # wdndds/audio/tts_chunk/qwen3-tts；本机无 DDS writer
            # 所以 reader 一直 idle、回调不触发。
            self._dds_chunk_reader_thread(model_id="qwen3-tts")

            # ``_infer_one`` already routes through the SDK with the
            # user's ``no_play`` intent — the SDK's internal
            # ``AudioPlayer`` streams chunks live; we don't re-spawn
            # aplay here (would replay the synthesis on top of the
            # live stream).
            try:
                result = self._infer_one(req)
            finally:
                # ⚠️ **等 UDS reader 线程把 socket 里的 PCM 收完** ——
                # worker 在 RPC 结束时 close socket ⇒ reader 读到 EOF 退出。
                #
                # 为什么必须等：``respond`` 返回 ≠ 最后一块 PCM 已被读走
                # （reader 可能还卡在 socket 缓冲上）⇒ 不等的话**最后
                # 一块分片会丢**（同传队列的播放尾巴被削掉）。
                # ``PLAY_BOTH`` 原来的「drain aplay」顺手给了几秒，
                # 这里显式等 ⇒ 两条路径的 PCM 完整性一致。
                th = self._pcm_thread
                if th is not None:
                    th.join(timeout=3.0)
                    self._pcm_thread = None
                # Always signal EOF to browser-side subscribers so the
                # AudioContext stops scheduling further chunks even if
                # the inference raised.
                self._finish_audio_stream()
                # Drain the local aplay writer so the device finishes
                # playing whatever's still in its stdin pipe. We do
                # this after ``_finish_audio_stream`` so the last
                # chunk has already been enqueued.
                if self._aplay_writer is not None:
                    try:
                        self._aplay_writer.close()
                    except Exception as exc:           # noqa: BLE001
                        LOG.debug("aplay-writer close failed: %s", exc)
                    self._aplay_writer = None

            attachments = list(getattr(result, "attachment", []) or [])
            wav_bytes = attachments[0].data if attachments else b""

            # worker reply 不再带 TTS WAV —— 从浏览器刚播的 PCM chunks 拼回。
            # lock 下 snapshot + clear 防止迟到的 DDS chunk 在读到一半
            # 时 append。
            with self._run_pcm_lock:
                pcm_chunks = self._run_pcm_chunks
                self._run_pcm_chunks = []
                pcm_sr = self._run_pcm_sr

            if not wav_bytes and pcm_chunks:
                try:
                    pcm = np.concatenate(pcm_chunks)
                except Exception as e:           # noqa: BLE001
                    LOG.warning("pcm concat failed: %s", e)
                    pcm = np.array([], dtype="<f4")
                if pcm.size > 0:
                    wav_bytes = _assemble_wav(pcm, pcm_sr)
                    chunk_count = len(pcm_chunks)
                    chunk_audio_sec = pcm.size / max(pcm_sr, 1)
                    sdk_audio_sec = float(self._audio_sec or 0.0)
                    if sdk_audio_sec > 0:
                        dev = abs(chunk_audio_sec - sdk_audio_sec) / sdk_audio_sec
                        if dev > 0.20:
                            LOG.warning(
                                "tts: WAV from PCM chunks deviates from SDK "
                                "audio_sec (chunks=%.3fs sdk=%.3fs dev=%.1f%%); "
                                "BEST_EFFORT drop gap likely",
                                chunk_audio_sec, sdk_audio_sec, dev * 100,
                            )
                    _tts_log(
                        f"WAV assembled from {chunk_count} PCM chunks "
                        f"({pcm.size} samples, {chunk_audio_sec:.3f}s, "
                        f"sr={pcm_sr}Hz)"
                    )

            if wav_bytes:
                # ⚠️ ``save_wav=False``（同传队列）⇒ **不落盘**：
                # 同传一次会议能写几百 MB WAV，eMMC 扛不住，也没人要听回放。
                # 但**时长照样要算**（滞后记账要用）。
                audio_sec = _wav_duration_seconds(wav_bytes)
                elapsed = time.time() - t0
                output_path = None
                size = 0
                if req.save_wav:
                    output_path = Path(req.output)
                    with open(output_path, "wb") as f:
                        f.write(wav_bytes)
                    try:
                        size = output_path.stat().st_size
                    except OSError:
                        size = 0
                # 顺序很重要：先写盘，再在 lock 下 publish self._output ——
                # 否则 200 ms 的 snapshot poll 会读到还没落盘的
                # output_url → 404。
                with self._lock:
                    self._audio_sec = audio_sec
                    self._elapsed = elapsed
                    self._output = output_path
                with self._history_lock:
                    self._history.append({
                        "ts":         time.time(),
                        "text":       req.text,
                        "language":   req.language,
                        "speaker":    req.ref_speaker or req.speaker,
                        "output":     str(output_path) if output_path else "",
                        "output_url": (self._output_url_for(output_path)
                                       if output_path else ""),
                        "play_to":    req.play_to,
                        "size":       size,
                        "audio_sec":  round(audio_sec, 3),
                        "frames":     self._frames,
                    })
                    while len(self._history) > self.HISTORY_MAX:
                        self._history.pop(0)

                self._set_state(
                    self.STATE_DONE,
                    f"saved {output_path}" if output_path else "done (no wav)",
                )
            else:
                with self._lock:
                    self._audio_sec = 0.0
                    self._elapsed = time.time() - t0
                    self._output = None
                self._set_state(self.STATE_DONE, "no audio produced (wav_bytes=0)")
        except Exception as exc:                   # noqa: BLE001
            LOG.exception("tts synthesis failed")
            self._set_state(self.STATE_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                self._pcm_sock = None
            if sock_path:
                try:
                    if os.path.exists(sock_path):
                        os.unlink(sock_path)
                except OSError:
                    pass
                self._pcm_sock_path = ""
            # Tear down the DDS chunk reader. The Hub keeps the
            # DomainParticipant alive for the lifetime of the main
            # process, so leaked readers pile up across runs.
            dds_reader = getattr(self, "_dds_chunk_reader", None)
            if dds_reader is not None:
                try:
                    dds_reader.close()
                except Exception as exc:           # noqa: BLE001
                    LOG.debug("DDS chunk reader close failed: %s", exc)
                self._dds_chunk_reader = None
            if req.play_to == PLAY_QUEUE:
                # 同传路径的**一行**计时（合成耗时 / 首块 PCM 延迟）。
                # 首块 PCM 的延迟 = Qwen3-TTS 的**首窗延迟**（decoder 首窗
                # 要 50 帧 ⇒ talker 先吐 50 个 token ⇒ 实测 ~3.7 s）⇒
                # 这是同传滞后里**唯一**的固定开销，队列流水线就是把它
                # 从「每句」摊成「每轮一次」。
                _first = getattr(self, "_run_first_pcm_t", None)
                _tts_log(
                    "tts-timing: infer=%.2fs first_pcm=%.2fs"
                    % (time.time() - t0,
                       (0.0 if _first is None
                        else max(0.0, _first - self._last_started))))

    def _set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state = state
            if state == self.STATE_ERROR:
                self._error = message
        self._emit()


# --------------------------------------------------------------------------- #
# Voice / language catalog
# --------------------------------------------------------------------------- #
#: 试听样例目录 —— 卡片 static 下，每个自带音色一个 ``<id>.wav``。
#: 离线生成（scripts/gen_voice_previews.py），不在运行时算：一个音色合成
#: 一段要十几秒，放到页面上等不得。
VOICE_PREVIEW_DIR = Path(__file__).resolve().parent / "static" / "previews"


def _preview_url(name: str) -> str:
    """自带音色的试听样例 URL；没有样例文件 → 空串（前端就不显示试听）。"""
    try:
        if not (VOICE_PREVIEW_DIR / (name + ".wav")).is_file():
            return ""
    except OSError:
        return ""
    return "/api/tts/voice-preview/" + name


def voice_catalog(model_dir=None) -> dict:
    """可用音色 + 语种清单(前端下拉的数据源)。

    全部取自 SDK 的表,不硬编码:参考音色 = 内嵌的 voice-clone 向量;
    内置音色 = ``available_config_speakers()`` 实测该模型真带的。
    ``dialect`` 非空表示该音色自带方言(SDK 会把语种锁成它)。

    另外合并**用户音色**(上传参考音频现场提取的 x-vector)——
    目录 ``<resource_dir>/tts_voices`` 下每个 ``<name>.npy`` 算一个,
    ``kind="user"``,前端按 ``kind`` 分组渲染。

    自带音色额外带 ``preview``（试听样例的 URL，没有样例文件时为空串）——
    用户选音色前先听一段,不然只能凭名字猜。
    """
    from wdnai.backends.tts import (
        CODEC_LANGUAGE_ID,
        SPK_IS_DIALECT,
        _get_bundled_speaker_embeddings,
        available_config_speakers,
    )

    # 「false」是 SDK 的哨兵值。方言不是独立语种 —— 它由对应音色携带,
    # 不单独列进语种下拉。
    dialects = {v for v in SPK_IS_DIALECT.values() if v and v != "false"}

    def _dialect_of(name: str) -> str:
        d = SPK_IS_DIALECT.get(name, "")
        return d if d in dialects else ""

    # 顺序跟着 SDK 走:内嵌字典的第一条就是默认音色(前端下拉默认选中
    # 它)。所以「默认音色」= SDK 内嵌表的**第一条**,不另外写死名字。
    voices = [
        {"id": name, "kind": "ref", "dialect": "", "preview": _preview_url(name)}
        for name in _get_bundled_speaker_embeddings()
    ]
    voices += [
        {"id": name, "kind": "builtin", "dialect": _dialect_of(name)}
        for name in available_config_speakers(model_dir)
    ]
    voices += _user_voice_entries()
    languages = [{"id": "auto"}] + [
        {"id": k} for k in CODEC_LANGUAGE_ID if k not in dialects
    ]
    return {"voices": voices, "languages": languages}


def _user_voice_entries() -> list:
    """列出用户上传的音色（现场提取 x-vector 的那些）。

    目录 ``<resource_dir>/tts_voices``，每个 ``<name>.npy`` 算一个。
    读不出来 / 目录不存在 → 返回空列表,不影响内建音色的渲染。
    """
    try:
        from app.core.uploads import get_tts_user_voices_root
        root = get_tts_user_voices_root()
    except Exception:  # noqa: BLE001
        return []
    try:
        names = sorted(p.stem for p in root.glob("*.npy"))
    except OSError:
        return []
    return [{"id": n, "kind": "user", "dialect": ""} for n in names]


# --------------------------------------------------------------------------- #
# Singleton accessor
# --------------------------------------------------------------------------- #
_session: Optional[TtsSession] = None
_session_lock = threading.Lock()


def close_all() -> None:
    """关掉**默认会话**。

    ⚠️ ``cards/tts/__init__.py::register_shutdown`` 会按名字找
    ``close`` / ``stop`` / ``shutdown`` / ``close_all`` ⇒ 用 ``close_all``
    这个名字 ⇒ 关机钩子能自动调到它。
    """
    try:
        get_session().close()
    except Exception:  # noqa: BLE001
        pass


def get_session() -> TtsSession:
    """Lazy singleton. The first call probes the wdnai SDK; on non-RK
    dev boxes the returned session just reports ``state="unavailable"``.
    """
    global _session
    with _session_lock:
        if _session is None:
            _session = TtsSession()
        return _session
