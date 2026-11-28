#!/usr/bin/env python3
# coding=utf-8
"""回归（2026-11-28 用户需求）：**配音导出的产物管理**。

用户三条
========

1. 「配音导出」生成的文件保存在**工程目录**的某个合适目录下，
   **不要**保存在源文件同级目录；
2. 生成的文件做成**可以下拉的列表**，**重启不消失**；并且要能**删除**、
   能**一键清除所有**（防占满本地磁盘）；
3. 「实时结果」「识别过程」别无限往下长 ⇒ 上下拖动条。

本脚本覆盖 **①②**（③④ 的前端部分在 ``verify_dub_ui.py`` 与宿主的
``verify_asr_file_translate_ui.py`` 里）：

A. **产物目录**（纯逻辑，不占 NPU）
   * 产物落在 ``app/uploads/qwen3_asr/dub/``（**工程目录内**），
     **不是**源文件同级目录；
   * 文件名规则没变（``<源名>_<语言>_<音色>``）；
   * 同一个源 + 语言 + 音色 ⇒ **同一个文件**（重复导出是覆盖，不是堆积）。

B. **清单**（纯逻辑，不占 NPU）
   * 清单落盘 ⇒ **重启不消失**（新进程读得到）；
   * 清单与磁盘**互为兜底**（盘上有清单没有 ⇒ 补进去；清单有盘上没有 ⇒
     去掉）；
   * **路径穿越**一律拒（``../``、绝对路径、``..``）。

C. **HTTP 端点**（走运行中的服务）
   * ``GET  /api/qwen3-asr/dub/files`` —— 列出全部产物（**不按源文件筛**）；
   * ``GET  /api/qwen3-asr/dub/files/{id}/raw`` —— 取回文件（能播）；
   * ``DELETE /api/qwen3-asr/dub/files/{id}`` —— 删一个；
   * ``POST /api/qwen3-asr/dub/files/clear`` —— **一键清除所有**；
   * 越界的 id ⇒ 拒（404 / 不删任何东西）。

跑法::

    <wdnwebui>/.venv/bin/python -u scripts/verify_dub_artifacts.py

⚠️ **C 段会真的删产物目录里的文件** ⇒ 先跑完 ``verify_dub_export.py``
   再跑这个（否则会把它的产物清掉）。
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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


_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _api(method: str, path: str, payload: dict | None = None,
         base: str = "https://127.0.0.1:8000", timeout: float = 30.0,
         binary: bool = False):
    url = base + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            body = r.read()
            if binary:
                return r.status, body
            return r.status, json.loads(body.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if binary:
            return e.code, body.encode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": repr(e)}


# --------------------------------------------------------------------------- #
# A. 产物目录（纯逻辑）
# --------------------------------------------------------------------------- #
def part_a() -> None:
    section("A. 产物目录：在工程目录里，不在源文件旁边")
    from wdnapps_audio_chat.cards.qwen3_asr.dub import (
        DubEngine,
        DubJob,
        dub_output_root,
    )

    root = dub_output_root()
    check("产物目录 = <工程目录>/app/uploads/qwen3_asr/dub",
          str(root).endswith("app/uploads/qwen3_asr/dub"), f"得到 {root}")
    check("产物目录在**工程目录**内（不是 /userdata/assets）",
          "/app/uploads/" in str(root), f"得到 {root}")

    eng = DubEngine()
    j = DubJob(id="t", kind="audio", source_path="/uploads/会议.wav",
               source_name="会议.wav", target_lang="zh-CN",
               ref_speaker="news_female")
    out = eng._output_path(j, ".wav")
    check("文件名规则没变：<源名>_<语言>_<音色>.wav",
          out.name == "会议_zh-CN_news_female.wav", f"得到 {out.name}")
    check("落在**产物目录**（不是源文件同级目录）",
          out.parent == root, f"得到 {out.parent}")
    check("**不在**源文件同级目录",
          out.parent != Path("/uploads"), f"得到 {out.parent}")

    # 同一个源 + 语言 + 音色 ⇒ 同一个文件（重复导出是覆盖）
    out2 = eng._output_path(j, ".wav")
    check("同样的参数 ⇒ **同一个文件**（不会无限堆积）",
          out2 == out, f"{out} vs {out2}")

    # 视频产物在同一个目录里
    jv = DubJob(id="t", kind="video", source_path="/uploads/movie.mp4",
                source_name="movie.mp4", target_lang="en", speaker="serena")
    outv = eng._output_path(jv, ".mp4")
    check("视频产物与音频产物同目录",
          outv.parent == root and outv.name == "movie_en_serena.mp4",
          f"得到 {outv}")


# --------------------------------------------------------------------------- #
# B. 清单（纯逻辑）
# --------------------------------------------------------------------------- #
def part_b() -> None:
    section("B. 产物清单：落盘 ⇒ 重启不消失；与磁盘互为兜底")
    from wdnapps_audio_chat.cards.qwen3_asr.dub import DubArtifactStore

    d = Path(tempfile.mkdtemp(prefix="dub-store-test-"))
    st = DubArtifactStore(d)
    check("空目录 ⇒ 列表为空", st.list() == [], f"得到 {st.list()}")

    # 盘上有、清单里没有 ⇒ 补进去（模拟「清单丢了 / 老版本产物」）
    (d / "a_zh-CN_v.wav").write_bytes(b"x" * 100)
    (d / "b_en_v.mp4").write_bytes(b"y" * 200)
    items = st.list()
    check("盘上有、清单里没有 ⇒ **自动补进清单**",
          sorted(i["name"] for i in items) == ["a_zh-CN_v.wav", "b_en_v.mp4"],
          f"得到 {[i['name'] for i in items]}")

    # 清单落盘 ⇒ 新进程（= 重启）读得到
    st2 = DubArtifactStore(d)
    items2 = st2.list()
    check("**新进程**（模拟重启）读得到清单 ⇒ 列表不空",
          sorted(i["name"] for i in items2) == ["a_zh-CN_v.wav", "b_en_v.mp4"],
          f"得到 {[i['name'] for i in items2]}")
    check("清单文件落在产物目录里",
          (d / "index.json").is_file(), f"目录 {d}")

    # 登记（导出成功后的路径）—— ⚠️ **文件得先真的存在**（清单与磁盘
    # 对账时会把「清单有、盘上没有」的条目当成空气去掉）。
    (d / "c_zh-CN_v.wav").write_bytes(b"z" * 50)
    st2.register(d / "c_zh-CN_v.wav", kind="audio", source_name="src.wav",
                 target_lang="zh-CN", voice="v", duration_sec=1.5)
    st3 = DubArtifactStore(d)
    names = [i["name"] for i in st3.list()]
    check("登记过的产物在清单里", "c_zh-CN_v.wav" in names, f"得到 {names}")
    got = [i for i in st3.list() if i["name"] == "c_zh-CN_v.wav"][0]
    check("清单项带**来源 / 语言 / 音色 / 时长**",
          got.get("source_name") == "src.wav" and got.get("target_lang") == "zh-CN"
          and got.get("voice") == "v" and abs(got.get("duration_sec", 0) - 1.5) < 0.01,
          f"得到 {got}")
    check("清单项带 **id** 与 **url**",
          bool(got.get("id")) and got.get("url", "").endswith("/raw"),
          f"得到 {got.get('id')} {got.get('url')}")

    # 清单有、盘上没有 ⇒ 去掉
    (d / "a_zh-CN_v.wav").unlink()
    st4 = DubArtifactStore(d)
    names = [i["name"] for i in st4.list()]
    check("文件被手工删了 ⇒ **清单里也去掉**（不列空气）",
          "a_zh-CN_v.wav" not in names, f"得到 {names}")

    # 路径穿越
    check("删除 ``../evil`` ⇒ 拒",
          st4.delete("../evil") is False, "")
    check("删除绝对路径 ⇒ 拒",
          st4.delete("/etc/passwd") is False, "")
    check("删除 ``..`` ⇒ 拒",
          st4.delete("..") is False, "")
    check("删除 ``index.json``（清单本身）⇒ 拒",
          st4.delete("index.json") is False, "")
    check("穿越尝试之后，目录里的文件**一个都没少**",
          sorted(p.name for p in d.iterdir()) ==
          ["b_en_v.mp4", "c_zh-CN_v.wav", "index.json"],
          f"得到 {sorted(p.name for p in d.iterdir())}")

    # 删一个
    ok = st4.delete("b_en_v.mp4")
    check("删除一个产物 ⇒ 成功", ok is True, f"得到 {ok}")
    check("文件真的没了", not (d / "b_en_v.mp4").is_file(), "")
    check("清单里也没了",
          "b_en_v.mp4" not in [i["name"] for i in st4.list()], "")

    # 一键清空
    res = st4.clear()
    check("一键清除所有 ⇒ 报删掉了几个",
          isinstance(res.get("removed"), int) and res["removed"] >= 1,
          f"得到 {res}")
    check("清空后目录里**只剩清单**",
          sorted(p.name for p in d.iterdir()) == ["index.json"],
          f"得到 {sorted(p.name for p in d.iterdir())}")
    check("清空后列表为空", st4.list() == [], "")


# --------------------------------------------------------------------------- #
# C. HTTP 端点（走运行中的服务）
# --------------------------------------------------------------------------- #
def part_c(base: str) -> None:
    section("C. HTTP 端点：列表 / 取回 / 删除 / 一键清除")
    from wdnapps_audio_chat.cards.qwen3_asr.dub import dub_output_root

    root = dub_output_root()
    root.mkdir(parents=True, exist_ok=True)
    # 造两个**测试产物**（内容随便 —— 只验端点行为）
    (root / "zz_test_a.wav").write_bytes(b"RIFF" + b"\0" * 60)
    (root / "zz_test_b.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 60)

    code, d = _api("GET", "/api/qwen3-asr/dub/files", base=base)
    check("列表端点 200", code == 200, f"得到 {code}")
    items = (d or {}).get("items") or []
    names = [i.get("name") for i in items]
    check("列表里有刚造的测试产物",
          "zz_test_a.wav" in names and "zz_test_b.mp4" in names, f"得到 {names}")
    check("列表带**占用量**与**上限**",
          "total_size" in (d or {}) and "limit" in (d or {}), f"得到 {list((d or {}).keys())}")
    check("列表带**产物目录**路径",
          str((d or {}).get("dir") or "").endswith("app/uploads/qwen3_asr/dub"),
          f"得到 {(d or {}).get('dir')}")
    check("列表**不按源文件筛**（不带 path 也是全局的）",
          len(names) >= 2, f"得到 {names}")

    # 取回
    tgt = [i for i in items if i.get("name") == "zz_test_a.wav"]
    if tgt:
        code2, raw = _api("GET", tgt[0]["url"], base=base, binary=True)
        check("raw 端点能取回文件", code2 == 200, f"得到 {code2}")
        check("取回的是真字节（RIFF 头）",
              isinstance(raw, bytes) and raw[:4] == b"RIFF",
              f"前 4 字节 {raw[:4]!r}")
    else:
        check("raw 端点能取回文件", False, "列表里没有测试产物")

    # 越界 id ⇒ 拒
    code3, _ = _api("DELETE", "/api/qwen3-asr/dub/files/" +
                    urllib.parse.quote("../../etc/passwd"), base=base)
    check("删除越界 id ⇒ 拒（不是 200）", code3 != 200, f"得到 {code3}")
    check("越界删除之后，**源目录里的文件没被动过**",
          (root / "zz_test_a.wav").is_file(), "")

    # 删一个
    code4, d4 = _api("DELETE", "/api/qwen3-asr/dub/files/zz_test_b.mp4",
                     base=base)
    check("删除一个产物 ⇒ 200 + deleted",
          code4 == 200 and (d4 or {}).get("deleted") is True, f"得到 {code4} {d4}")
    check("文件真的没了", not (root / "zz_test_b.mp4").is_file(), "")
    code5, d5 = _api("GET", "/api/qwen3-asr/dub/files", base=base)
    check("列表里也没了",
          "zz_test_b.mp4" not in [i.get("name") for i in (d5 or {}).get("items") or []],
          "")

    # 一键清除
    code6, d6 = _api("POST", "/api/qwen3-asr/dub/files/clear", base=base)
    check("一键清除 ⇒ 200 + ok",
          code6 == 200 and (d6 or {}).get("ok") is True, f"得到 {code6} {d6}")
    left = [p.name for p in root.iterdir()
            if p.is_file() and p.name != "index.json"]
    check("清空之后产物目录里**没有产物了**", not left, f"得到 {left}")
    code7, d7 = _api("GET", "/api/qwen3-asr/dub/files", base=base)
    check("清空之后列表为空",
          not ((d7 or {}).get("items") or []), f"得到 {(d7 or {}).get('items')}")


def main() -> int:
    print("=" * 72)
    print("回归：配音导出的产物管理（产物目录 / 清单 / 删除 / 一键清除）")
    print("=" * 72)
    part_a()
    part_b()
    base = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000")
    part_c(base)
    print("\n" + "=" * 72)
    print(f"通过 {_OK} 条，失败 {len(_FAIL)} 条")
    if _FAIL:
        for f in _FAIL:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
