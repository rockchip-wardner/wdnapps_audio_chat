#!/usr/bin/env python3
# coding=utf-8
"""回归：**配音导出**（2026-11-26 用户需求）。

需求
====
1. 「浏览器麦克风」「板载麦克风」⇒ **保留**实时翻译 + 同声传译（不变）；
2. 「音频文件」「含音轨的视频」⇒ **保留**实时翻译 + 字幕实时显示，
   **去掉**同声传译；
3. 「音频文件」⇒ 生成**目标语言的音频文件**（ASR + 翻译 + TTS），
   文件名 = **源文件名 + 目标语言 + 音色**，存在**源文件同级目录**，
   生成完**可选目标文件播放**；
4. 「含音轨的视频」⇒ 拆音轨 → ASR + 翻译 + TTS → 目标语言音频文件；
   视频按 ASR + 翻译的结果加**双字幕**（原文 + 译文，节奏跟原声对齐）；
   语音文件与加完字幕的视频**重新合成** ⇒ 音视频文件。

跑法
====

.. code-block:: text

    # 纯逻辑部分（不占 NPU）
    <wdnwebui>/.venv/bin/python -u scripts/verify_dub_export.py --no-npu

    # 真机端到端（**走运行中的服务**：算力注册表是**进程内**的
    # ⇒ 另起进程够不着那三个 worker ⇒ 必须打服务的 HTTP 接口）
    <wdnwebui>/.venv/bin/python -u scripts/verify_dub_export.py --http

⚠️ **真机部分会真的产出文件**（落在 ``<resource_root>/uploads``）⇒ 跑完
   可以直接听 / 看。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: 宿主仓库根（``wdnwebui``）—— ``app.core.*`` 在那儿。
_HOST = REPO.parent.parent.parent

for p in (str(_HOST), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

_OK = 0
_FAIL: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _OK
    if cond:
        _OK += 1
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        _FAIL.append(name)
        print(f"  \033[31mFAIL\033[0m {name} {extra}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------------------- #
# HTTP 客户端（服务是 HTTPS + 自签 CA ⇒ 不校验证书）
# --------------------------------------------------------------------------- #
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _api(method: str, path: str, payload: dict | None = None,
         base: str = "https://127.0.0.1:8000", timeout: float = 30.0,
         binary: bool = False):
    url = base + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            raw = r.read()
            # ⚠️ **媒体文件是二进制**（WAV / MP4）⇒ 不能按 UTF-8 解码
            #    （会抛 UnicodeDecodeError，测试脚本自己崩掉）。
            if binary:
                return r.status, raw
            body = raw.decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body


# --------------------------------------------------------------------------- #
# A. 纯逻辑：命名规则 / 文件名清洗（不占 NPU）
# --------------------------------------------------------------------------- #
def part_a() -> None:
    section("A. 命名规则：源文件名 + 目标语言 + 音色")
    from wdnapps_audio_chat.cards.qwen3_asr.dub import (
        DubEngine,
        DubJob,
        sanitize_name,
    )

    check("清洗：路径分隔符被换掉",
          "/" not in sanitize_name("a/b") and ".." not in sanitize_name(".."))
    check("清洗：空格被换掉", " " not in sanitize_name("a b c"))
    check("清洗：空串有兜底", sanitize_name("") == "x")

    eng = DubEngine()
    j = DubJob(id="t", kind="audio", source_path="/uploads/会议.wav",
               source_name="会议.wav", target_lang="zh-CN",
               ref_speaker="news_female")
    out = eng._output_path(j, ".wav")
    check("音频：文件名 = 源名_目标语言_音色.wav",
          out.name == "会议_zh-CN_news_female.wav", f"得到 {out.name}")
    check("音频：落在源文件同级目录",
          out.parent == Path("/uploads"), f"得到 {out.parent}")

    j2 = DubJob(id="t", kind="video", source_path="/uploads/movie.mp4",
                source_name="movie.mp4", target_lang="en", speaker="serena")
    out2 = eng._output_path(j2, ".mp4")
    check("视频：文件名 = 源名_目标语言_音色.mp4",
          out2.name == "movie_en_serena.mp4", f"得到 {out2.name}")
    check("视频：与音频同目录",
          out2.parent == Path("/uploads"), f"得到 {out2.parent}")


# --------------------------------------------------------------------------- #
# B. 纯逻辑：AVCC → Annex-B（SDK 那个函数有数据相关的坑）
# --------------------------------------------------------------------------- #
def part_b() -> None:
    section("B. AVCC → Annex-B（不能踩 SDK 那个「看起来像起始码」的坑）")
    from wdnapps_audio_chat.cards.qwen3_asr.video_burn import (
        _is_annexb,
        avcc_to_annexb,
        to_annexb,
    )
    from wdnav.mpp import avcc_to_annexb as sdk_avcc_to_annexb

    # 一个长度前缀恰好是 00 00 01 xx 的 AVCC 样本 ⇒ SDK 的函数会**原样返回**
    # ⚠️ 长度前缀必须是**合法**的（长度 = 后面 NAL 的实际字节数）⇒
    #    0x163 = 355 字节 ⇒ 前缀字节 = 00 00 01 63。
    nal_len = 0x163
    payload = bytes((i % 251) for i in range(nal_len))
    sample = nal_len.to_bytes(4, "big") + payload
    check("样本造得对（前缀 = 00 00 01 63）", sample[:4] == b"\x00\x00\x01\x63")
    check("样本长度自洽", len(sample) == 4 + nal_len)
    sdk_out = sdk_avcc_to_annexb(sample)
    # ⚠️ **2026-11-27：SDK 的这个 bug 已修**（wdnav 仓）—— 旧实现用
    # 「开头像不像起始码」当判据 ⇒ MP4 的 AVCC 长度前缀恰好是
    # ``00 00 01 xx`` ⇒ 误判成 Annex-B ⇒ 只解出 5/73 帧。
    # 现在是「严格解析能不能吃干净」⇒ 与本模块的实现一致。
    check("SDK 的函数能正确转换（wdnav 已修：严格 AVCC 解析）",
          sdk_out == b"\x00\x00\x00\x01" + payload,
          f"SDK 行为变了 ⇒ 本模块的说明要跟着改：{sdk_out[:12].hex()}")
    ours = avcc_to_annexb(sample)
    check("本模块的函数正确转成 Annex-B",
          ours == b"\x00\x00\x00\x01" + payload, f"得到 {ours[:12].hex()}")
    # ⚠️ **判定必须用「严格解析」，不能用「开头是不是起始码」** ——
    #    AVCC 的长度前缀经常就是 00 00 01 xx ⇒ 只看开头会误判。
    check("to_annexb：AVCC 样本转成 Annex-B（不误判）",
          to_annexb(sample) == b"\x00\x00\x00\x01" + payload,
          f"得到 {to_annexb(sample)[:12].hex()}")
    annexb_in = b"\x00\x00\x00\x01" + payload
    check("to_annexb：本来就是 Annex-B 的原样用",
          to_annexb(annexb_in) == annexb_in)
    check("to_annexb：认不出的返回空串",
          to_annexb(b"\xff\xff\xff\xffxx") == b"")
    check("Annex-B 判定：认得起始码", _is_annexb(annexb_in))
    check("本模块与 SDK 的转换结果一致（同一份严格实现）",
          to_annexb(sample) == sdk_avcc_to_annexb(sample))
    check("Annex-B 判定：AVCC 的 00 00 01 前缀也会被误判（所以不能当判据）",
          _is_annexb(sample))


# --------------------------------------------------------------------------- #
# E. 纯逻辑：分流（文件类**不做**同传；麦克风类**照旧**）
# --------------------------------------------------------------------------- #
def part_e() -> None:
    section("E. 分流：文件类来源**不做**同传；麦克风类来源**照旧**")
    card = REPO / "src" / "wdnapps_audio_chat" / "cards" / "qwen3_asr"
    api_py = (card / "api.py").read_text(encoding="utf-8")
    js = (card / "static" / "app.js").read_text(encoding="utf-8")
    tpl = (card / "static" / "template.html").read_text(encoding="utf-8")

    check("后端：同传拒绝 file / video 来源",
          'if source in ("file", "video"):' in api_py)
    check("前端：文件类来源藏起同传块",
          'interpBlock.classList.toggle("hidden", this._isFileSource());' in js)
    check("前端：文件类来源显示配音导出块",
          'dubBlock.classList.toggle("hidden", !this._isFileSource());' in js)
    check("前端：_interpStart 对文件类来源直接拒绝",
          "文件类来源不做同传" in js)
    check("前端：切到文件类来源 ⇒ 停掉同传",
          "if (this._isFileSource() && this._interp.running)" in js)
    check("模板：有配音导出块", 'id="asr-dub-block"' in tpl)
    check("模板：同传块默认隐藏（麦克风才显示）",
          'id="asr-interp-block"' in tpl and
          'asr-interp-block hidden' in tpl)
    # 麦克风来源的同传**没被动过**
    check("前端：麦克风仍然自动开同传",
          "_autoStartInterpForMic" in js and
          'if (src !== "board" && src !== "browser") return;' in js)


# --------------------------------------------------------------------------- #
# F. 纯逻辑：烧字幕 + 重封装踩过的四个坑（2026-11-27 真机实测）
# --------------------------------------------------------------------------- #
def part_f() -> None:
    section("F. 烧字幕 + 重封装的四个坑（uint8 / 帧率 / 时间基 / 静音）")
    import numpy as np

    from wdnapps_audio_chat.cards.qwen3_asr.dub import _trim_silence
    from wdnapps_audio_chat.cards.qwen3_asr.video_burn import (
        SubtitleCue,
        _fit_bitrate,
        _nominal_fps,
        _SubtitlePainter,
    )

    # ① **uint8 画布不能原地乘浮点数** ——
    #    ``band *= 0.35`` ⇒ numpy 算 float64 再塞回 uint8 ⇒ UFuncTypeError。
    canvas = np.full((240, 320, 3), 200, dtype=np.uint8)
    try:
        painter = _SubtitlePainter(320, 240)
        painter.paint(canvas, SubtitleCue(0.0, 2.0, orig="中文原文",
                                          trans="english line"))
        ok = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = "{}: {}".format(type(exc).__name__, exc)
    check("烧字幕：uint8 画布不炸（不能 band *= 0.35）", ok, err)
    check("烧字幕：画布仍然是 uint8", canvas.dtype == np.uint8)
    band = canvas[int(240 * 0.85):int(240 * 0.97), :, :]
    check("烧字幕：底部半透明黑带压暗了画面",
          float(band.mean()) < 200.0, f"band mean={band.mean():.1f}")

    # ② **标称帧率必须来自源流** —— 抽帧视频（1 fps）不能当成 25 fps，
    #    否则 144 秒的片子会被压成 5.8 秒。
    class _V:
        average_rate = 1.0066649688913032
        guessed_rate = 25
    check("帧率：1 fps 的抽帧视频 ⇒ 1（不是 25）",
          _nominal_fps(_V()) == 1, f"得到 {_nominal_fps(_V())}")

    class _V24:
        average_rate = 24
        guessed_rate = 24
    check("帧率：24 fps ⇒ 24", _nominal_fps(_V24()) == 24)

    class _VNone:
        average_rate = None
        guessed_rate = None
    check("帧率：流头没有帧率 ⇒ 退回默认 25",
          _nominal_fps(_VNone()) == 25)

    # ③ **码率要跟分辨率/帧率走** —— 一刀切 4 Mbps 会让 320×240 的小样片
    #    变成 72 MB。
    br_small = _fit_bitrate(320, 240, 1)
    br_big = _fit_bitrate(1920, 1080, 25)
    check("码率：小样片不该按 4 Mbps 算", br_small <= 1_000_000,
          f"得到 {br_small}")
    check("码率：1080p 给到几 Mbps", 2_000_000 <= br_big <= 8_000_000,
          f"得到 {br_big}")
    check("码率：显式指定时听调用方的", _fit_bitrate(1920, 1080, 25, 1234) == 1234)

    # ④ **TTS 自带首尾静音** —— 不裁的话配音会比字幕慢半拍。
    rate = 24000
    tone = np.zeros(rate * 2, dtype="<f4")
    tone[int(rate * 0.6):int(rate * 1.4)] = 0.5   # 0.6 s 处才开声
    trimmed = _trim_silence(tone, rate)
    dur_before = len(tone) / rate
    dur_after = len(trimmed) / rate
    check("静音裁剪：首部静音被裁掉", dur_after < dur_before - 0.4,
          f"{dur_before:.2f}s -> {dur_after:.2f}s")
    nz = np.nonzero(np.abs(trimmed) > 0.05)[0]
    # ⚠️ 裁剪会把整段**前移** ⇒ 位置要相对**裁剪后**的数组算：
    #    正文起点 ≈ 余量 25 ms，总长 ≈ 正文 0.8 s + 首尾两个余量。
    check("静音裁剪：正文没被削掉",
          nz.size > 0 and abs(nz[0] / rate - 0.025) < 0.02,
          f"起点 {nz[0] / rate:.3f}s")
    check("静音裁剪：正文长度保持",
          abs(dur_after - (0.8 + 0.05)) < 0.06, f"得到 {dur_after:.3f}s")

    # ⑤ 源码级断言：mux 必须显式给 dts/duration + 时间基用 90 kHz，
    #    且必须丢弃「时间戳倒序」的帧（MPP 尾部会重复吐一帧）。
    src = (REPO / "src" / "wdnapps_audio_chat" / "cards" / "qwen3_asr"
           / "video_burn.py").read_text(encoding="utf-8")
    check("mux：显式给 dts（只给 pts 会被 muxer 拒）", "pkt.dts = int(pts)" in src)
    check("mux：显式给 duration", "pkt.duration = int(duration)" in src)
    check("mux：时间基用 1/90000", "time_base=Fraction(1, 90000)" in src)
    check("mux：丢掉时间戳倒序的帧",
          "pts90 <= result.last_pts90" in src)
    check("mux：送 EOS 冲尾帧（否则片尾丢帧）", "eos=True" in src)



# --------------------------------------------------------------------------- #
# G. 复用：配音导出**不再识别一遍**（纯逻辑 + 真机）
# --------------------------------------------------------------------------- #
def part_g_logic() -> None:
    section("G1. 复用：批次缓存（原文 + 译文 + 时间）")
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        TranslationService, _Item,
    )

    svc = TranslationService()
    item = _Item(index=1, text="Hello there.", lang="en", stream_id="file-1",
                 loop=None, enqueued_at=0.0, start_sec=1.0, end_sec=2.5,
                 batch_id="42")
    svc._remember(item, "done", "你好啊")
    rows = svc.batch_results("42")
    check("批次缓存：记下了这一句", len(rows) == 1, f"得到 {rows}")
    if rows:
        check("批次缓存：原文/译文/时间都在",
              rows[0].get("orig") == "Hello there."
              and rows[0].get("trans") == "你好啊"
              and rows[0].get("start_sec") == 1.0
              and rows[0].get("end_sec") == 2.5, f"得到 {rows[0]}")
    # 失败的句子**不记**（没译文，导出会拿到空句）
    svc._remember(item, "failed", "")
    check("批次缓存：失败的句子不记", len(svc.batch_results("42")) == 1)
    # 没有批次号的（实时麦克风路径）不记
    item2 = _Item(index=2, text="x", lang="en", stream_id="live-1", loop=None,
                  enqueued_at=0.0, batch_id="")
    svc._remember(item2, "done", "y")
    check("批次缓存：没有批次号的不记", svc.batch_results("") == [])
    svc.drop_batch("42")
    check("批次缓存：可以丢掉一批", svc.batch_results("42") == [])


def part_g_http(base: str) -> None:
    section("G2. 复用：真机 —— 先识别、再导出 ⇒ 导出**不重新识别**")
    src = "/userdata/assets/uploads/asr_en.wav"
    if not Path(src).is_file():
        check("源音频存在（%s）" % src, False, "找不到测试素材")
        return

    # ⚠️⚠️ **两个前置条件** —— 少了任何一个，G2 都会**假失败**：
    #   ① 翻译**开关**开着 + 目标语言 = zh-CN（复用是**按语言**判定的：
    #      页面那次翻成什么语言，导出就得是那个语言才复用）；
    #   ② 翻译**模型**已经 running（模型在重载时，那次识别的句子**翻不
    #      出来** ⇒ 批次缓存是空的 ⇒ 无从复用 ⇒ 报「重新识别」）。
    # ⇒ 之前这两条都靠「**上一个脚本留下的状态**」⇒ **顺序依赖** ⇒
    #    换个顺序跑就红（实测踩到：前一个脚本把目标语言留成了无效值 ⇒
    #    模型重载 ⇒ G2 报「没有复用」⇒ 其实跟复用机制本身无关）。
    code, d = _api("POST", "/api/qwen3-asr/translate/settings",
                   {"enabled": True, "target_lang": "zh-CN"}, base=base)
    check("翻译开关已打开、目标语言 = zh-CN", code == 200,
          f"code={code} {d}")
    # ⚠️ **光等不够，还要先把它拉起来** —— 前一个脚本（``verify_asr_wizard_
    # translate.py`` 的用例 2）会**停掉**翻译模型去测「模型起不来」的降级
    # 路径 ⇒ 轮到本脚本时模型可能**正在被拉起**（或还停着）⇒
    # 那样这次识别的句子**翻不出来** ⇒ 批次缓存是空的 ⇒ 无从复用。
    _api("POST", "/api/qwen3-asr/translate/enable", {}, base=base, timeout=120)
    deadline = time.time() + 180
    ready = False
    while time.time() < deadline:
        stt = _api("GET", "/api/qwen3-asr/translate/status", base=base)[1] or {}
        if isinstance(stt, dict) and (stt.get("model") or {}).get("running"):
            ready = True
            break
        time.sleep(2.0)
    check("翻译模型已就绪（否则句子翻不出来 ⇒ 无从复用）", ready)

    # ① 页面式识别（句级翻译 ⇒ 后端整段送译文）
    code, d = _api("POST", "/api/qwen3-asr/transcribe", base=base,
                   timeout=60) if False else (0, None)
    # 上面的接口是 multipart ⇒ 单独走 urllib
    try:
        from urllib import request as _rq
        boundary = "----wdnverify%d" % int(time.time())
        body = b""
        for k, v in (("path", src), ("translate_mode", "sentence")):
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, k, v)).encode()
        body += ("--%s--\r\n" % boundary).encode()
        req = _rq.Request(base + "/api/qwen3-asr/transcribe", data=body,
                          headers={"Content-Type":
                                   "multipart/form-data; boundary=" + boundary},
                          method="POST")
        with _rq.urlopen(req, timeout=60, context=_CTX) as r:
            code, d = r.status, json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        check("发起识别", False, str(exc))
        return
    check("识别已发起", code == 200 and isinstance(d, dict), f"得到 {code}")

    # ② 等识别跑完
    deadline = time.time() + 600
    st = {}
    while time.time() < deadline:
        st = _api("GET", "/api/qwen3-asr/", base=base)[1] or {}
        if isinstance(st, dict) and st.get("state") == "done":
            break
        time.sleep(3)
    check("识别跑完了", st.get("state") == "done", f"state={st.get('state')}")
    rounds = st.get("round_count") or 0
    check("识别出了轮次", rounds > 0, f"round_count={rounds}")

    # ③ 导出（**目标语言与页面那次相同**）⇒ **应当复用**
    code, d = _api("POST", "/api/qwen3-asr/dub/start",
                   {"kind": "audio", "path": src, "target_lang": "zh-CN",
                    "ref_speaker": "news_female"}, base=base, timeout=60)
    check("导出作业已受理", code == 200 and isinstance(d, dict), f"得到 {code}")
    job = _wait_job(base, limit=1800)
    check("导出作业成功", job.get("state") == "done",
          f"state={job.get('state')} err={job.get('error')}")
    warns = " | ".join(job.get("warnings") or [])
    check("导出**复用了**页面上那次识别（没有重新识别）",
          "复用" in warns and "没有重新识别" in warns,
          f"warnings={warns!r}")
    sents = job.get("sentences") or []
    check("复用到的句子带原文 + 译文 + 时间",
          bool(sents) and all(s.get("orig") and s.get("trans")
                              for s in sents), f"得到 {len(sents)} 句")
    audio = job.get("audio_path") or ""
    check("产物落在源文件同级目录",
          bool(audio) and Path(audio).parent == Path(src).parent, audio)

    # ④ ⚠️ **目标语言不同** ⇒ **绝不能复用** ——
    #    用户实测：「配音导出选择英文，生成的文件播放是中文」。
    #    页面那次识别的译文是**页面上的**目标语言（zh-CN）⇒ 拿来当英文
    #    导出用 ⇒ 文件名叫 ``_en_``、内容却是中文。
    code, d = _api("POST", "/api/qwen3-asr/dub/start",
                   {"kind": "audio", "path": src, "target_lang": "en",
                    "ref_speaker": "news_female"}, base=base, timeout=60)
    check("（语言不同）导出作业已受理", code == 200 and isinstance(d, dict),
          f"得到 {code}")
    job2 = _wait_job(base, limit=1800,
                     job_id=str((d or {}).get("id") or ""))
    check("（语言不同）导出作业成功", job2.get("state") == "done",
          f"state={job2.get('state')} err={job2.get('error')}")
    warns2 = " | ".join(job2.get("warnings") or [])
    check("（语言不同）**没有**复用页面那次译文",
          "没有重新识别" not in warns2, f"warnings={warns2!r}")
    check("（语言不同）已重新识别并翻译",
          "重新识别" in warns2, f"warnings={warns2!r}")
    sents2 = job2.get("sentences") or []
    zh = sum(1 for s in sents2
             if any("\u4e00" <= c <= "\u9fff" for c in (s.get("trans") or "")))
    check("（语言不同）译文是**英文**（没有汉字）", zh == 0,
          f"{zh}/{len(sents2)} 句含汉字")


# --------------------------------------------------------------------------- #
# C / D. 真机端到端（走运行中的服务）
# --------------------------------------------------------------------------- #
def _wait_job(base: str, limit: float = 2400.0,
              job_id: str = "") -> dict:
    """等一个配音导出作业跑完。

    ⚠️ **必须按作业 id 等** —— ``/dub/status`` 返回的是**最新那个**作业；
    下一个作业刚 POST 出去、还没顶掉上一个时，状态里还是**上一个已经
    done 的作业** ⇒ 不按 id 等就会读到**上一次的产物**（假失败）。
    """
    deadline = time.time() + limit
    while time.time() < deadline:
        st = _api("GET", "/api/qwen3-asr/dub/status", base=base)[1]
        if not isinstance(st, dict):
            time.sleep(1.0)
            continue
        same = (not job_id) or (str(st.get("id") or "") == str(job_id))
        if same and st.get("state") in ("done", "error", "stopped"):
            return st
        time.sleep(2.0)
    return _api("GET", "/api/qwen3-asr/dub/status", base=base)[1]


def _check_sentences(sent: list, tag: str) -> None:
    check(f"{tag}：拿到了带时间的译文句", len(sent) > 0, f"得到 {len(sent)} 句")
    if not sent:
        return
    s0 = sent[0]
    check(f"{tag}：句子里有原文 + 译文 + 时间",
          bool(s0.get("orig")) and bool(s0.get("trans"))
          and s0.get("end_sec", -1) > s0.get("start_sec", -1),
          f"得到 {s0}")
    # ⚠️ **时间必须递增** —— 字幕的节奏靠它
    times = [(s.get("start_sec"), s.get("end_sec")) for s in sent]
    inc = all(times[i][0] <= times[i + 1][0] for i in range(len(times) - 1))
    check(f"{tag}：句子的时间递增（字幕跟得住原声）", inc, f"得到 {times[:5]}")


def part_c(base: str) -> None:
    section("C. 真机端到端：音频文件 → 目标语言音频文件")
    uploads = Path("/userdata/assets/uploads")
    src = uploads / "asr_en.wav"
    if not src.is_file():
        check("源音频存在（%s）" % src, False, "找不到测试素材")
        return

    code, d = _api("POST", "/api/qwen3-asr/dub/start", {
        "kind": "audio", "path": str(src), "target_lang": "zh-CN",
        "ref_speaker": "news_female"}, base=base)
    check("音频导出：接口接受了作业", code == 200 and d.get("ok"),
          f"得到 {code} {d}")
    st = _wait_job(base, job_id=str((d or {}).get("id") or ""))
    print("  状态:", st.get("state"), "|", st.get("phase"), "|",
          st.get("error") or "")
    check("音频导出：作业成功", st.get("state") == "done",
          f"得到 {st.get('state')} {st.get('error')}")
    if st.get("state") != "done":
        return
    _check_sentences(st.get("sentences") or [], "音频导出")

    out_path = st.get("audio_path") or ""
    check("音频导出：产物路径是源文件同级目录",
          out_path and Path(out_path).parent == uploads, f"得到 {out_path}")
    check("音频导出：文件名符合规则",
          out_path and Path(out_path).name ==
          "asr_en_zh-CN_news_female.wav",
          f"得到 {Path(out_path).name if out_path else ''}")
    if out_path and Path(out_path).is_file():
        with wave.open(out_path, "rb") as wf:
            dur = wf.getnframes() / float(wf.getframerate())
            sr = wf.getframerate()
        check("音频导出：WAV 能读、有内容", dur > 1.0, f"时长 {dur:.1f}s")
        print(f"  产物: {out_path}（{sr} Hz, {dur:.1f} s）")
    else:
        check("音频导出：产物文件存在", False, f"得到 {out_path}")

    # 「可选目标文件进行播放」—— 列表接口要把产物列出来
    code, d = _api("GET", "/api/qwen3-asr/dub/files?path=" +
                   urllib.parse.quote(str(src)), base=base)
    items = (d or {}).get("items") or []
    check("音频导出：产物能被列出来（供页面下拉）",
          any(f["name"].endswith(".wav") for f in items),
          f"得到 {[f.get('name') for f in items]}")
    check("音频导出：列表项带播放用的 url",
          all(f.get("url") for f in items), f"得到 {items[:2]}")
    # 播放用的 url 要真的能取到文件（``/media`` 端点）
    if items:
        code2, raw = _api("GET", items[0]["url"], base=base, binary=True)
        check("音频导出：产物能通过 /media 取到", code2 == 200, f"得到 {code2}")
        check("音频导出：/media 取到的是真音频（RIFF 头）",
              isinstance(raw, bytes) and raw[:4] == b"RIFF",
              f"前 4 字节 {raw[:4]!r}")


def part_d(base: str) -> None:
    section("D. 真机端到端：含音轨的视频 → 音频 + 双字幕视频")
    uploads = Path("/userdata/assets/uploads")
    src = uploads / "asr_zh_seek_test.mp4"
    if not src.is_file():
        check("源视频存在（%s）" % src, False, "找不到测试素材")
        return

    code, d = _api("POST", "/api/qwen3-asr/dub/start", {
        "kind": "video", "path": str(src), "target_lang": "en",
        "ref_speaker": "news_female"}, base=base)
    check("视频导出：接口接受了作业", code == 200 and d.get("ok"),
          f"得到 {code} {d}")
    st = _wait_job(base, job_id=str((d or {}).get("id") or ""))
    print("  状态:", st.get("state"), "|", st.get("phase"), "|",
          st.get("error") or "")
    check("视频导出：作业成功", st.get("state") == "done",
          f"得到 {st.get('state')} {st.get('error')}")
    if st.get("state") != "done":
        return
    _check_sentences(st.get("sentences") or [], "视频导出")

    audio = st.get("audio_path") or ""
    video = st.get("video_path") or ""
    check("视频导出：音频产物在源同级目录",
          audio and Path(audio).parent == uploads, f"得到 {audio}")
    check("视频导出：音频文件名符合规则",
          audio and Path(audio).name == "asr_zh_seek_test_en_news_female.wav",
          f"得到 {Path(audio).name if audio else ''}")
    check("视频导出：视频产物在源同级目录",
          video and Path(video).parent == uploads, f"得到 {video}")
    check("视频导出：视频文件名符合规则",
          video and Path(video).name == "asr_zh_seek_test_en_news_female.mp4",
          f"得到 {Path(video).name if video else ''}")

    if video and Path(video).is_file():
        import av
        c = av.open(str(video))
        streams = [(s.type, s.codec_context.name) for s in c.streams]
        c.close()
        # ⚠️⚠️ **同一个容器只能解一遍** —— PyAV 的 ``Container.decode()``
        #    是**一次性**的生成器（走完就没了）⇒ 先解视频再解音频 ⇒ 音频
        #    **0 帧**（测试自己骗自己，产物其实是好的）。
        # ⇒ 每条轨**各开一个容器**。
        c = av.open(str(video))
        nv = sum(1 for _ in c.decode(video=0))
        c.close()
        c = av.open(str(video))
        na = sum(1 for _ in c.decode(audio=0))
        c.close()
        check("视频导出：产物里有视频轨", any(t == "video" for t, _ in streams),
              f"得到 {streams}")
        check("视频导出：产物里有音频轨（目标语言）",
              any(t == "audio" for t, _ in streams), f"得到 {streams}")
        check("视频导出：视频能解出画面", nv > 0, f"得到 {nv} 帧")
        check("视频导出：音频能解出声音", na > 0, f"得到 {na} 帧")
        print(f"  产物: {video}（{nv} 帧视频 / {na} 帧音频）")
    else:
        check("视频导出：视频产物存在", False, f"得到 {video}")


def part_h() -> None:
    section("H. 本机没有 TTS ⇒ 导出必须**等本机就绪**（不许借对端）")
    # ⚠️ **为什么有这一段**（2026-11-27 真机踩到）：
    # ``synthesize_pcm`` 走 ``get_session("swcap_text_to_speech")`` ⇒
    # **本机优先，本机没有就退回 LAN peer**。而本机的 TTS 是后台拉起来的
    # （~22 s 才加载完）⇒ 导出刚开始合成时本机往往还没就绪 ⇒ 抢先拿到
    # **对端板子**的 TTS ⇒ 那个对端 worker 的 session 没加载 ⇒
    # 「session not loaded; send action='load' first」⇒ **整个作业失败**。
    #
    # ⇒ 所以 ``DubEngine._wait_local_tts`` 必须在合成**之前**等本机就绪。
    from wdnapps_audio_chat.cards.qwen3_asr import dub as dub_mod
    from wdnapps_audio_chat.cards.qwen3_asr.dub import DubEngine, DubJob

    # ① 源码级：合成**之前**必须等本机 TTS。
    src = (REPO / "src" / "wdnapps_audio_chat" / "cards" / "qwen3_asr"
           / "dub.py").read_text(encoding="utf-8")
    wait_at = src.find("self._wait_local_tts(job)")
    synth_at = src.find("self._synthesize(")
    check("合成之前必须等本机 TTS 就绪",
          wait_at > 0 and synth_at > 0 and wait_at < synth_at,
          f"wait@{wait_at} synth@{synth_at}")
    check("等本机 TTS 用的是**本机**实例（不拿对端）",
          "include_remote=False" in src)

    # ② 行为级：**没有本机实例** ⇒ 必须**等**，等到了才返回。
    class _FakeReg:
        """假 registry：前 N 次没有本机实例，之后有。"""

        def __init__(self, become_after: int):
            self.calls = 0
            self._become_after = become_after

        def list_sessions(self, cap, include_remote=False):
            self.calls += 1
            if self.calls <= self._become_after:
                return []
            return [object()]

    real_registry = None
    try:
        import app.core.compute.registry as reg_mod
        real_registry = reg_mod.get_registry
    except Exception:  # noqa: BLE001
        reg_mod = None

    if reg_mod is None:
        check("能拿到宿主 registry（才能验本机实例）", False, "import 失败")
        return

    eng = DubEngine()
    job = DubJob(id="h-1", kind="audio", source_path="/tmp/x.wav",
                 source_name="x.wav", target_lang="en")

    fake = _FakeReg(become_after=2)
    reg_mod.get_registry = lambda: fake
    old_cap = dub_mod._DUB_WAIT_LOCAL_TTS_SEC
    dub_mod._DUB_WAIT_LOCAL_TTS_SEC = 30.0
    try:
        got = eng._wait_local_tts(job)
    except Exception as exc:  # noqa: BLE001
        got = False
        check("等本机 TTS 不炸", False, f"{type(exc).__name__}: {exc}")
    finally:
        dub_mod._DUB_WAIT_LOCAL_TTS_SEC = old_cap
        if real_registry is not None:
            reg_mod.get_registry = real_registry
    check("本机 TTS 起来之后 ⇒ 等到了（返回 True）", got is True)
    check("它**确实等过**（不是一查就有）", fake.calls >= 3,
          f"list_sessions 调了 {fake.calls} 次")

    # ③ 行为级：**一直没有** ⇒ 超时返回 False + 说清楚（不硬失败）。
    class _NeverReg:
        def list_sessions(self, cap, include_remote=False):
            return []

    reg_mod.get_registry = lambda: _NeverReg()
    job2 = DubJob(id="h-2", kind="audio", source_path="/tmp/x.wav",
                  source_name="x.wav", target_lang="en")
    dub_mod._DUB_WAIT_LOCAL_TTS_SEC = 2.0
    try:
        got2 = eng._wait_local_tts(job2)
    except Exception as exc:  # noqa: BLE001
        got2 = False
        check("等不到本机 TTS 也不炸", False, f"{type(exc).__name__}: {exc}")
    finally:
        dub_mod._DUB_WAIT_LOCAL_TTS_SEC = old_cap
        if real_registry is not None:
            reg_mod.get_registry = real_registry
    check("等不到 ⇒ 返回 False（不硬失败，继续合成）", got2 is False)
    check("等不到 ⇒ 在 warnings 里说清楚", len(job2.warnings) >= 1,
          f"warnings={job2.warnings}")


def _post_transcribe(base: str, src: str, timeout: float = 60.0):
    """发一次**识别**请求（multipart）⇒ 返回 ``(状态码, 体)``。

    ⚠️ ``/transcribe`` 是 **multipart**（不是 JSON）⇒ 不能用 ``_api``。
    """
    from urllib import request as _rq
    boundary = "----wdnverify%d" % int(time.time() * 1000)
    body = b""
    for k, v in (("path", src), ("translate_mode", "sentence")):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode()
    body += ("--%s--\r\n" % boundary).encode()
    req = _rq.Request(base + "/api/qwen3-asr/transcribe", data=body,
                      headers={"Content-Type":
                               "multipart/form-data; boundary=" + boundary},
                      method="POST")
    try:
        with _rq.urlopen(req, timeout=timeout, context=_CTX) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def part_i(base: str) -> None:
    section("I. 「清空」：服务端把**结果**清掉（刷新页面不会复活）")
    # ⚠️ **为什么要有这一段**（2026-11-27 用户要求）——
    #   「配音导出」加了「清空」按钮 ⇒ 后端要有一个 ``POST /dub/reset``。
    # ⚠️ **必须服务端清** —— 页面进时会拉一次**最新那个作业** ⇒ 只清前端
    #    DOM 的话，用户一刷新页面，上次的逐句结果又回来了。
    uploads = Path("/userdata/assets/uploads")
    src = uploads / "asr_en.wav"
    if not src.is_file():
        check("源音频存在（%s）" % src, False, "找不到测试素材")
        return

    code, d = _api("POST", "/api/qwen3-asr/dub/start", {
        "kind": "audio", "path": str(src), "target_lang": "zh-CN",
        "ref_speaker": "news_female"}, base=base)
    check("清空：能起一个作业（用来验「跑着的时候不许清」）",
          code == 200 and (d or {}).get("ok"), f"得到 {code} {d}")
    check("清空：作业刚起来是 running",
          (d or {}).get("state") == "running",
          f"得到 {(d or {}).get('state')}")

    # ⚠️ **反向也要挡**（2026-11-27 用户要求：两者**互斥**）——
    #    导出在跑时，**不允许**再发起识别 ⇒ 两者都占**同一个 ASR 单例**
    #    （导出那条流也走 ``svc.transcribe_stream``）⇒ 同时跑会搅坏。
    code, t = _post_transcribe(base, str(src))
    check("互斥：导出在跑时，识别请求被拒（409）",
          code == 409 and "配音导出" in str(t), f"得到 {code} {t}")

    # ⚠️ **跑着的时候不许清** —— 半路清会留下一个「半截」的作业状态
    #    （句子清了、作业还在跑）。
    code, r = _api("POST", "/api/qwen3-asr/dub/reset", {}, base=base)
    check("清空：作业在跑时**拒掉**（不许清一半）",
          code == 200 and (r or {}).get("ok") is False,
          f"得到 {code} {r}")
    check("清空：拒掉时带了一句人话（能显示给用户）",
          bool((r or {}).get("reason")), f"得到 {r}")

    # ⇒ 停掉它（别留着占着 ASR 单例），再清。
    _api("POST", "/api/qwen3-asr/dub/stop", {}, base=base)
    st = _wait_job(base, job_id=str((d or {}).get("id") or ""))
    check("清空：停掉之后作业不再 running",
          st.get("state") in ("stopped", "done", "error"),
          f"得到 {st.get('state')}")

    before = sorted(p.name for p in uploads.glob("asr_en_*.wav"))
    code, r = _api("POST", "/api/qwen3-asr/dub/reset", {}, base=base)
    check("清空：停完之后能清", code == 200 and (r or {}).get("ok") is True
          and (r or {}).get("cleared") is True, f"得到 {code} {r}")

    st2 = _api("GET", "/api/qwen3-asr/dub/status", base=base)[1] or {}
    check("清空：服务端**没有作业了**（刷新页面不会复活）",
          not st2.get("id"), f"得到 {st2.get('id')!r}")
    check("清空：逐句结果**没了**", not (st2.get("sentences") or []),
          f"得到 {len(st2.get('sentences') or [])} 句")

    after = sorted(p.name for p in uploads.glob("asr_en_*.wav"))
    check("清空：生成的文件**没有被删**（清的是结果，不是文件）",
          before == after, f"清空前 {before} / 清空后 {after}")

    code, r = _api("POST", "/api/qwen3-asr/dub/reset", {}, base=base)
    check("清空：没有作业时再清一次也不报错",
          code == 200 and (r or {}).get("ok") is True, f"得到 {code} {r}")


def part_j() -> None:
    section("J. 「清空」的判定逻辑（纯逻辑，不占 NPU）")
    # ⚠️ **为什么要有这一段**（2026-11-27 用户要求）——
    #   「清空」有三种情形：没作业 / 正在跑 / **点过「停止」但还在收尾**。
    #   第三种最容易漏：``stop()`` 只是置一个取消标志 ⇒ 作业线程要到下一个
    #   检查点才退出 ⇒ 状态**那几秒里还是 running** ⇒ 用户明明点了「停止」
    #   却清不掉，提示还写着「先点停止」（已经点过了！）⇒ 必须钉住。
    from wdnapps_audio_chat.cards.qwen3_asr.dub import DubEngine, DubJob

    eng = DubEngine()
    r = eng.reset()
    check("清空：没有作业时**不报错**（ok，但没清掉什么）",
          r.get("ok") is True and r.get("cleared") is False, f"得到 {r}")

    job = DubJob(id="dub-1", kind="audio", source_path="/uploads/x.wav",
                 source_name="x.wav", target_lang="zh-CN", state="running")
    eng._job = job
    r = eng.reset()
    check("清空：正在跑 ⇒ **拒掉**（不许清一半）",
          r.get("ok") is False and r.get("cleared") is False, f"得到 {r}")
    check("清空：拒掉时带了一句人话（能显示给用户）",
          bool(r.get("reason")), f"得到 {r}")
    check("清空：拒掉时**作业还在**（没被清掉）", eng._job is job)

    # ⇒ 点了「停止」⇒ 取消标志置上 ⇒ 状态还停在 running 也允许清。
    eng._cancel.set()
    r = eng.reset()
    check("清空：点过「停止」⇒ **允许清**（不等它收尾）",
          r.get("ok") is True and r.get("cleared") is True, f"得到 {r}")
    st = eng.status()
    check("清空：作业记录被扔掉了（刷新页面不复活）",
          st.get("state") == "idle" and not st.get("id"), f"得到 {st}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8000")
    ap.add_argument("--no-npu", action="store_true",
                    help="只跑纯逻辑部分（不占 NPU）")
    ap.add_argument("--http", action="store_true",
                    help="跑真机端到端（走运行中的服务）")
    args = ap.parse_args()

    print("\033[1m配音导出回归\033[0m")
    part_a()
    part_b()
    part_e()
    part_f()
    part_g_logic()
    part_h()
    part_j()
    if args.http:
        part_c(args.base)
        part_d(args.base)
        part_g_http(args.base)
        part_i(args.base)
    else:
        print("\n（真机端到端没跑：加 --http）")
    print("\n\033[1m结果\033[0m: 通过 %d，失败 %d" % (_OK, len(_FAIL)))
    for f in _FAIL:
        print("  FAIL:", f)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  （part_c 用得到）
    sys.exit(main())
