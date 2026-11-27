#!/usr/bin/env python3
"""回归测试: 「流式语音识别」卡（qwen3-asr）**配音导出块**的前端能不能用。

为什么要有这个脚本
------------------
2026-11-26 用户实测：「选好『音频文件』，**生成配音版**不能点击」。

根因有两处（都已修）：

1. ``_dubSyncButtons()`` **只在 init 和轮询里调** ⇒ 选完文件 / 填完路径
   之后按钮状态**从来不刷新** ⇒ 从初始化起就一直是 ``disabled``；
2. ``_dubSyncButtons()`` 用「有没有**设备端路径**」判「有没有源」
   ⇒ **本机上传的文件**（``_file`` 有值、``_path`` 为空）永远判成「没源」。

⇒ 这个脚本用 playwright 走**真实浏览器路径**：

1. 打开卡片页（跳过向导）；
2. 断言文件类来源下：同传块隐藏、配音导出块可见；
3. 填**设备端路径** ⇒ 断言「生成配音版」**可点**（原来不可点）；
4. 走**本机上传** ⇒ 断言「生成配音版」**也可点**；
5. 点「生成配音版」⇒ 断言真的发出了 ``POST /api/qwen3-asr/dub/start``；
6. 等作业跑完 ⇒ 断言「生成的文件」下拉里有产物、播放器拿到了 URL。

跑法::

    <wdnwebui>/.venv/bin/python scripts/verify_dub_ui.py
    <wdnwebui>/.venv/bin/python scripts/verify_dub_ui.py --keep-open
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
_HOST = REPO.parent.parent.parent

BASE = "https://127.0.0.1:8000"
CARD = "qwen3-asr"
#: 设备端的测试音频（真机素材在 resource_root 下）
DEFAULT_SRC = "/userdata/assets/uploads/asr_en.wav"

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


def _hidden(page, sel: str) -> bool:
    return bool(page.evaluate(
        "(s) => { const e = document.querySelector(s);"
        " return !e || e.classList.contains('hidden')"
        " || getComputedStyle(e).display === 'none'; }", sel))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--source", default=DEFAULT_SRC,
                    help="设备端音频路径（用来验「填路径 ⇒ 按钮可点」）")
    ap.add_argument("--upload", default=None,
                    help="本机文件（用来验「本机上传 ⇒ 按钮也可点」）")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--no-export", action="store_true",
                    help="只验按钮状态，不真的发起导出")
    args = ap.parse_args()

    posts: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console",
                lambda m: errors.append(f"[console.error] {m.text}")
                if m.type == "error" else None)

        def on_request(req):
            if req.method == "POST" and "/api/qwen3-asr/dub/start" in req.url:
                try:
                    posts.append(json.loads(req.post_data or "{}"))
                except Exception as exc:  # noqa: BLE001
                    posts.append({"_parse_error": str(exc)})

        page.on("request", on_request)

        bad_responses: list[str] = []

        def on_response(resp):
            if resp.status >= 400:
                body = ""
                try:
                    body = (resp.text() or "")[:200]
                except Exception:  # noqa: BLE001
                    body = "<读不出响应体>"
                bad_responses.append(f"{resp.status} {resp.request.method} "
                                     f"{resp.url} :: {body}")

        page.on("response", on_response)

        section("A. 打开卡片页")
        page.goto(f"{args.base}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        check("页面里�� #page-qwen3-asr（卡位占位符没漏）",
              page.locator(f"#page-{CARD}").count() > 0)

        skip = page.locator("text=跳过向导").first
        if skip.count():
            skip.click()
            page.wait_for_timeout(1500)

        section("B. 分流：文件类来源 ⇒ 显示配音块、隐藏同传块")
        check("同传块是隐藏的", _hidden(page, "#asr-interp-block"))
        check("配音导出块是显示的", not _hidden(page, "#asr-dub-block"))

        section("B2. 麦克风来源 ⇒ **同传照旧**、配音块隐藏")
        page.click(".asr-source-tab[data-source='board']")
        page.wait_for_timeout(1200)
        check("板载麦：同传块是显示的", not _hidden(page, "#asr-interp-block"))
        check("板载麦：配音导出块是隐藏的", _hidden(page, "#asr-dub-block"))
        page.click(".asr-source-tab[data-source='browser']")
        page.wait_for_timeout(1200)
        check("浏览器麦：同传块是显示的", not _hidden(page, "#asr-interp-block"))
        check("浏览器麦：配音导出块是隐藏的", _hidden(page, "#asr-dub-block"))
        # 切回「音频文件」
        page.click(".asr-source-tab[data-source='file']")
        page.wait_for_timeout(1200)
        check("切回音频文件：同传块又隐藏了", _hidden(page, "#asr-interp-block"))
        check("切回音频文件：配音导出块又显示了",
              not _hidden(page, "#asr-dub-block"))

        section("B3. 三个语言下拉的标签必须**区分开**（防「同名 ⇒ 以为联动」）")
        # ⚠️ **为什么要有这条**（2026-11-27）—— 用户实测报过「配音导出选择英文，
        #    生成的文件播放是中文」⇒ 根因之一就是两个下拉**都叫「目标语言」**
        #    ⇒ 用户以为它们联动、以为改一个另一个跟着变（实际不跟）。
        # ⇒ 现在三个下拉各有各的**不重名**标签，作用域也写在提示里。
        labels = page.evaluate(
            """() => {
                const pick = (id) => {
                    const el = document.querySelector('label[for="' + id + '"]');
                    return el ? (el.textContent || "").trim() : "";
                };
                const root = document.getElementById("page-qwen3-asr");
                const olds = root
                  ? Array.from(root.querySelectorAll("label"))
                      .map((l) => (l.textContent || "").trim())
                      .filter((t) => t === "目标语言")
                  : [];
                return {
                    tr: pick("asr-tr-target"),
                    interp: pick("asr-interp-target"),
                    dub: pick("asr-dub-target"),
                    olds: olds,
                };
            }""")
        check("实时翻译的下拉叫「显示语言」", labels["tr"] == "显示语言",
              f"实际 {labels['tr']!r}")
        check("同传的下拉叫「朗读语言」", labels["interp"] == "朗读语言",
              f"实际 {labels['interp']!r}")
        check("配音导出的下拉叫「导出语言」", labels["dub"] == "导出语言",
              f"实际 {labels['dub']!r}")
        check("这张卡里**再没有**叫「目标语言」的标签", not labels["olds"],
              f"实际 {labels['olds']!r}")

        section("C. 填**设备端路径** ⇒ 「生成配音版」必须可点")
        page.fill("#asr-source", args.source)
        page.wait_for_timeout(800)
        disabled = page.evaluate(
            "() => document.getElementById('asr-dub-start')?.disabled")
        check("「生成配音版」不是 disabled", disabled is False,
              f"disabled={disabled}")

        section("D. 选**本机文件** ⇒ 「生成配音版」也要可点")
        upload = args.upload or args.source
        if Path(upload).is_file():
            page.set_input_files("#asr-file", upload)
            page.wait_for_timeout(1200)
            disabled = page.evaluate(
                "() => document.getElementById('asr-dub-start')?.disabled")
            check("本机上传后「生成配音版」不是 disabled", disabled is False,
                  f"disabled={disabled}")
        else:
            check("本机上传后「生成配音版」不是 disabled", False,
                  f"文件不存在: {upload}")

        if args.no_export:
            browser.close()
            print("\n\033[1m结果\033[0m: 通过 %d，失败 %d" % (_OK, len(_FAIL)))
            return 1 if _FAIL else 0

        section("E. 点「生成配音版」⇒ 真的发出 POST /dub/start")
        page.click("#asr-dub-start")
        page.wait_for_timeout(2500)
        check("发出了 POST /api/qwen3-asr/dub/start", len(posts) > 0)
        if posts:
            body = posts[0]
            check("请求体带 kind", bool(body.get("kind")), str(body))
            check("请求体带 path", bool(body.get("path")), str(body))
            check("请求体带 target_lang", bool(body.get("target_lang")),
                  str(body))

        section("F. 等作业跑完 ⇒ 「生成的文件」下拉里有产物")
        deadline = time.time() + 300
        state = ""
        while time.time() < deadline:
            state = page.evaluate(
                "() => document.getElementById('asr-dub-status')"
                "?.textContent || ''")
            opts = page.evaluate(
                "() => Array.from(document.getElementById('asr-dub-files')"
                "?.options || []).map(o => o.value).filter(Boolean)")
            if opts:
                break
            page.wait_for_timeout(3000)
        opts = page.evaluate(
            "() => Array.from(document.getElementById('asr-dub-files')"
            "?.options || []).map(o => o.value).filter(Boolean)")
        check("产物下拉里有文件", len(opts) > 0, f"status={state!r}")
        if opts:
            print("    产物:", opts[:3])
            # ⚠️ **音频产物 ⇒ 音频播放器；视频产物 ⇒ 视频播放器**
            # （``_dubSyncPlayer`` 会把另一个元素的 src 清掉）⇒ 两个都看。
            srcs = page.evaluate(
                "() => [document.getElementById('asr-dub-player')?.src || '',"
                " document.getElementById('asr-dub-video')?.src || '']")
            check("播放器拿到了产物 URL",
                  any("/api/qwen3-asr/media" in s for s in srcs), str(srcs))

        check("页面没有 JS 报错", not errors, "; ".join(errors[:3]))
        if bad_responses:
            print("\n  \033[33m非 2xx 响应:\033[0m")
            for b in bad_responses[:10]:
                print("   ", b)
        if args.keep_open:
            input("按回车关闭浏览器…")
        browser.close()

    print("\n\033[1m结果\033[0m: 通过 %d，失败 %d" % (_OK, len(_FAIL)))
    for f in _FAIL:
        print("  FAIL:", f)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
