#!/usr/bin/env python3
# coding=utf-8
"""回归（浏览器级，2026-11-28 用户需求）：**配音产物的清单 + 删除**，
以及**结果区限高滚动**。

覆盖
====

A. **产物清单那一栏**
   1. 「生成的文件」下拉 / 「删除选中的」/「全部清除」按钮都在；
   2. 清单**在服务端落盘** ⇒ 页面一进来就列得出上次的产物
      （⇒ 「重启不消失」在页面上看得见）；
   3. 下拉里每一项带**类型 + 大小**；
   4. 「占用量」那一行显示「共 N 个 · 占用 …」；
   5. 「删除选中的」⇒ ``confirm`` ⇒ 真发 ``DELETE`` ⇒ 文件真没了、
      列表也更新了；
   6. 「全部清除」⇒ ``confirm`` ⇒ 真发 ``POST …/clear`` ⇒ 产物目录空了；
   7. **没有产物时**两个按钮都是**灰的**。

B. **结果区别无限往下长**（用户第三条）
   1. 「实时结果」的句子流**有高度上限 + 框内滚动**；
   2. 「识别过程」的逐轮明细**有高度上限 + 框内滚动**；
   3. ⚠️ **真的塞 400 句进去**，结果框的**高度不变**（不把页面顶长）；
   4. 计数那一行显示「共 N 句」/「共 N 轮」。

跑法::

    <wdnwebui>/.venv/bin/python scripts/verify_dub_artifacts_ui.py

⚠️ **会真的删产物目录里的文件** ⇒ 先跑完 ``verify_dub_export.py`` 再跑这个。
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
_HOST = REPO.parent.parent.parent
for p in (str(_HOST), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

BASE = "https://127.0.0.1:8000"
CARD = "qwen3-asr"

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


def _api(method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if payload is None
                                 else json.dumps(payload).encode("utf-8"))
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": repr(e)}


def _hidden(page, sel: str) -> bool:
    return bool(page.evaluate(
        "(s) => { const e = document.querySelector(s);"
        " return !e || e.classList.contains('hidden')"
        " || getComputedStyle(e).display === 'none'; }", sel))


def _has_size(text: str) -> bool:
    """下拉项里带没带**大小**（B / KB / MB / GB 任意一种）。"""
    import re
    return bool(re.search(r"\d+(\.\d+)?\s*(B|KB|MB|GB)", text or ""))


def _count_of(text: str) -> int:
    """从「共 N 句」里把 N 抠出来（抠不到 = -1）。"""
    import re
    m = re.search(r"共\s*(\d+)", text or "")
    return int(m.group(1)) if m else -1


def _px(v) -> float:
    """``"416px"`` / ``"26rem"`` / ``416`` ⇒ 像素数（rem 按 16 px 算）。

    ⚠️ **只在「上限是 rem」时粗略换** —— 断言是「不超过上限」，宁可
    **放宽一点**（rem 按 16 px 算 ⇒ 偏大 ⇒ 不会误判成超了）。
    """
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    try:
        if s.endswith("rem"):
            return float(s[:-3]) * 16.0
        if s.endswith("px"):
            return float(s[:-2])
        if s.endswith("vh"):
            return float(s[:-2]) * 11.0      # 视口 1100 px
        return float(s)
    except ValueError:
        return 1e9


def main() -> int:
    print("=" * 72)
    print("回归（浏览器）：配音产物清单 / 删除 / 一键清除 + 结果区限高滚动")
    print("=" * 72)

    from wdnapps_audio_chat.cards.qwen3_asr.dub import dub_output_root

    root = dub_output_root()
    root.mkdir(parents=True, exist_ok=True)

    deletes: list[str] = []
    clears: list[int] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        def on_request(req):
            if req.method == "DELETE" and "/api/qwen3-asr/dub/files/" in req.url:
                deletes.append(req.url)
            elif req.method == "POST" and \
                    "/api/qwen3-asr/dub/files/clear" in req.url:
                clears.append(1)

        page.on("request", on_request)

        # ⚠️ **confirm 一律放行**（不点「取消」）—— 否则删不掉。
        page.on("dialog", lambda d: d.accept())

        section("A. 打开卡片页")
        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        check("页面里有 #page-qwen3-asr",
              page.locator(f"#page-{CARD}").count() > 0)
        skip = page.locator("text=跳过向导").first
        if skip.count():
            skip.click()
            page.wait_for_timeout(1500)

        # ---- 造两个产物（服务端落盘 ⇒ 页面刷新就能看到）----
        (root / "ui_test_a.wav").write_bytes(b"RIFF" + b"\0" * 200)
        (root / "ui_test_b.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 300)

        section("B. 产物清单那一栏：元素 + 列表 + 占用量")
        els = page.evaluate(
            """() => ({
                sel: !!document.getElementById('asr-dub-files'),
                del: !!document.getElementById('asr-dub-del'),
                clear: !!document.getElementById('asr-dub-clear'),
                info: !!document.getElementById('asr-dub-files-info'),
                status: !!document.getElementById('asr-dub-files-status'),
            })""")
        check("有「生成的文件」下拉", els["sel"])
        check("有「删除选中的」按钮", els["del"])
        check("有「全部清除」按钮", els["clear"])
        check("有占用量显示元素", els["info"])
        check("有产物栏的状态文字", els["status"])

        # 重新拉一次列表（服务端落盘 ⇒ 模拟「重启后仍然在」）
        page.evaluate("() => window.Qwen3Asr._dubRefreshFiles()")
        page.wait_for_timeout(1200)
        items = page.evaluate(
            """() => {
                const sel = document.getElementById('asr-dub-files');
                const info = document.getElementById('asr-dub-files-info');
                return {
                    values: sel ? Array.from(sel.options).map(o => o.value) : [],
                    texts: sel ? Array.from(sel.options).map(o => o.textContent) : [],
                    info: info ? (info.textContent || '').trim() : '',
                };
            }""")
        check("下拉里列得出**上次的产物**（清单落盘 ⇒ 重启不消失）",
              "ui_test_a.wav" in items["values"]
              and "ui_test_b.mp4" in items["values"],
              f"实际 = {items['values']!r}")
        check("下拉项带**类型 + 大小**",
              any("音频" in t for t in items["texts"])
              and any("视频" in t for t in items["texts"])
              and any(_has_size(t) for t in items["texts"]),
              f"实际 = {items['texts']!r}")
        check("占用量那一行显示「共 N 个 · 占用 …」",
              "共 2 个" in items["info"] and "占用" in items["info"],
              f"实际 = {items['info']!r}")

        section("C. 「删除选中的」")
        page.evaluate(
            """() => {
                const sel = document.getElementById('asr-dub-files');
                sel.value = 'ui_test_b.mp4';
                sel.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
        page.wait_for_timeout(400)
        check("选中之后「删除选中的」**可点**",
              page.evaluate(
                  "() => !document.getElementById('asr-dub-del').disabled"),
              "")
        page.click("#asr-dub-del")
        page.wait_for_timeout(1500)
        check("点了「删除选中的」⇒ 真的发了 DELETE",
              len(deletes) == 1, f"实际 = {deletes!r}")
        check("文件**真的没了**",
              not (root / "ui_test_b.mp4").is_file(), "")
        after = page.evaluate(
            """() => {
                const sel = document.getElementById('asr-dub-files');
                return {
                    values: sel ? Array.from(sel.options).map(o => o.value) : [],
                    info: (document.getElementById('asr-dub-files-info')
                           || {}).textContent || '',
                    status: (document.getElementById('asr-dub-files-status')
                             || {}).textContent || '',
                };
            }""")
        check("列表里**没有**那个文件了",
              "ui_test_b.mp4" not in after["values"], f"实际 = {after['values']!r}")
        check("剩下的那个**还在**",
              "ui_test_a.wav" in after["values"], f"实际 = {after['values']!r}")
        check("占用量跟着更新（变成 1 个）",
              "共 1 个" in after["info"], f"实际 = {after['info']!r}")

        section("D. 「全部清除」")
        page.click("#asr-dub-clear")
        page.wait_for_timeout(1500)
        check("点了「全部清除」⇒ 真的发了 POST …/clear",
              len(clears) == 1, f"实际 = {clears!r}")
        left = [p.name for p in root.iterdir()
                if p.is_file() and p.name != "index.json"]
        check("产物目录里**没有产物了**", not left, f"实际 = {left!r}")
        page.evaluate("() => window.Qwen3Asr._dubRefreshFiles()")
        page.wait_for_timeout(800)
        empty = page.evaluate(
            """() => {
                const sel = document.getElementById('asr-dub-files');
                return {
                    texts: sel ? Array.from(sel.options).map(o => o.textContent) : [],
                    del: document.getElementById('asr-dub-del').disabled,
                    clear: document.getElementById('asr-dub-clear').disabled,
                };
            }""")
        check("列表回到「（还没有生成的文件）」",
              any("还没有生成的文件" in t for t in empty["texts"]),
              f"实际 = {empty['texts']!r}")
        check("**没有产物时**「删除选中的」是灰的", empty["del"], "")
        check("**没有产物时**「全部清除」是灰的", empty["clear"], "")

        section("E. 结果区别无限往下长（限高 + 框内滚动）")
        caps = page.evaluate(
            """() => {
                const pick = (id) => {
                    const el = document.getElementById(id);
                    const cs = el ? getComputedStyle(el) : null;
                    return cs ? {
                        maxHeight: cs.maxHeight,
                        overflowY: cs.overflowY,
                        clientHeight: el.clientHeight,
                    } : null;
                };
                return {
                    sents: pick('asr-sentences'),
                    rounds: pick('asr-rounds'),
                    transcript: pick('asr-transcript-body'),
                    live: pick('asr-live'),
                };
            }""")
        check("「实时结果」的句子流有**高度上限**",
              caps["sents"] and caps["sents"]["maxHeight"] not in ("", "none"),
              f"实际 = {caps['sents']!r}")
        check("「实时结果」超出部分**在框内滚动**",
              caps["sents"] and caps["sents"]["overflowY"] == "auto",
              f"实际 = {caps['sents']!r}")
        check("「识别过程」有**高度上限**",
              caps["rounds"] and caps["rounds"]["maxHeight"] not in ("", "none"),
              f"实际 = {caps['rounds']!r}")
        check("「识别过程」超出部分**在框内滚动**",
              caps["rounds"] and caps["rounds"]["overflowY"] == "auto",
              f"实际 = {caps['rounds']!r}")

        # ⚠️ **真的塞 400 句进去** —— 样式再「看起来对」，真塞进去才发现
        #    框还是会撑高就晚了。
        grow = page.evaluate(
            """() => {
                const box = document.getElementById('asr-sentences');
                const live = document.getElementById('asr-live');
                if (!box) return null;
                // ⚠️ **必须先把框显示出来** —— ``display:none`` 的元素
                //    ``clientHeight`` 恒为 0 ⇒ 量了个寂寞（第一版踩过）。
                box.classList.remove('hidden');
                box.style.display = 'block';
                if (live) { live.classList.remove('hidden'); live.style.display = 'block'; }
                const cap = getComputedStyle(box).maxHeight;
                const before = {
                    box: box.clientHeight,
                    live: live ? live.clientHeight : 0,
                    page: document.body.scrollHeight,
                };
                const add = (n) => {
                    for (let i = 0; i < n; i++) {
                        const row = document.createElement('div');
                        row.className = 'asr-sent';
                        const src = document.createElement('div');
                        src.className = 'asr-sent-src';
                        src.textContent = '测试句子 ' + i
                            + ' 这是一段用来把结果框撑长的文字';
                        row.appendChild(src);
                        box.appendChild(row);
                    }
                };
                add(400);
                const mid = {
                    box: box.clientHeight,
                    live: live ? live.clientHeight : 0,
                    page: document.body.scrollHeight,
                    scrollable: box.scrollHeight > box.clientHeight,
                };
                // ⚠️ **再加 400 句** —— 限高的真正判据是「**再塞也不会更高**」
                //    （第一版只塞了一次 ⇒ 量到的是「长到上限」而不是「封顶」）。
                add(400);
                const after = {
                    box: box.clientHeight,
                    live: live ? live.clientHeight : 0,
                    page: document.body.scrollHeight,
                    scrollable: box.scrollHeight > box.clientHeight,
                };
                return {before, mid, after, cap};
            }""")
        check("塞 800 句之后，句子流框**高度不再变**（限高封顶）",
              grow and grow["after"]["box"] == grow["mid"]["box"],
              f"实际 = {grow!r}")
        check("句子流框的高度**不超过**样式里的上限",
              grow and _px(grow["after"]["box"]) <= _px(grow["cap"]) + 2,
              f"实际 = {grow!r}")
        check("塞 800 句之后，**整个「实时结果」框不再变高**",
              grow and grow["after"]["live"] == grow["mid"]["live"],
              f"实际 = {grow!r}")
        check("塞 800 句之后，句子流**确实可滚**",
              grow and grow["after"]["scrollable"], f"实际 = {grow!r}")

        section("F. 计数（「共 N 句」/「共 N 轮」）")
        cnt = page.evaluate(
            """() => {
                window.Qwen3Asr._syncResultCount();
                const el = document.getElementById('asr-count');
                return {
                    text: el ? (el.textContent || '').trim() : '',
                    exists: !!el,
                };
            }""")
        check("有计数元素", cnt["exists"], "")
        check("计数显示「共 N 句」", "共 " in cnt["text"] and "句" in cnt["text"],
              f"实际 = {cnt['text']!r}")
        check("计数**不少于**塞进去的 400 句",
              _count_of(cnt["text"]) >= 400, f"实际 = {cnt['text']!r}")

        # ⚠️ **自己塞几条轮次** —— 计数取的是 ``Qwen3Asr.rounds``，那依赖
        #    「此刻识别过没有」⇒ 换个环境（没识别过）就是 0 ⇒ 断言会**假失败**。
        page.evaluate(
            """() => {
                window.Qwen3Asr.rounds = [
                    {round: 1, committed: '第一轮', unfixed: ''},
                    {round: 2, committed: '第一轮第二轮', unfixed: ''},
                    {round: 3, committed: '第一轮第二轮第三轮', unfixed: ''},
                ];
                const m = document.getElementById('asr-mode');
                m.value = 'rounds';
                m.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
        page.wait_for_timeout(500)
        cnt2 = page.evaluate(
            """() => {
                const el = document.getElementById('asr-count');
                return el ? (el.textContent || '').trim() : '';
            }""")
        check("切到「识别过程」⇒ 计数变成「共 N 轮」",
              cnt2.endswith("轮"), f"实际 = {cnt2!r}")
        check("「识别过程」的计数是 3 轮",
              _count_of(cnt2) == 3, f"实际 = {cnt2!r}")
        # 切回来
        page.evaluate(
            """() => {
                const m = document.getElementById('asr-mode');
                m.value = 'single';
                m.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
        page.wait_for_timeout(300)

        check("**没有 JS 报错**", not errors, f"实际 = {errors[:3]!r}")

        browser.close()

    print("\n" + "=" * 72)
    print(f"通过 {_OK} 条，失败 {len(_FAIL)} 条")
    if _FAIL:
        for f in _FAIL:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
