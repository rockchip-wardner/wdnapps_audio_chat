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

        # ⚠️ **说明必须紧贴自己解释的那一行**（2026-11-27 用户要求）——
        #    放在块底部离得太远 ⇒ 用户得自己猜是哪一项的说明。
        hints = page.evaluate(
            """() => {
                const hintAfter = (selId) => {
                    const el = document.getElementById(selId);
                    const row = el && el.closest
                        ? el.closest('.asr-tr-head') : null;
                    const nxt = row ? row.nextElementSibling : null;
                    if (!nxt || !nxt.classList.contains('asr-tr-hint')) {
                        return null;
                    }
                    return (nxt.textContent || '').trim();
                };
                return {
                    tr: hintAfter('asr-tr-target'),
                    interp: hintAfter('asr-interp-target'),
                    dub: hintAfter('asr-dub-target'),
                };
            }""")
        check("「显示语言」的说明**紧贴**它那一行下面",
              bool(hints["tr"]) and "显示语言" in hints["tr"],
              f"实际 = {hints['tr']!r}")
        check("「朗读语言」的说明**紧贴**它那一行下面",
              bool(hints["interp"]) and "朗读语言" in hints["interp"],
              f"实际 = {hints['interp']!r}")
        check("「导出语言」的说明**紧贴**它那一行下面",
              bool(hints["dub"]) and "导出语言" in hints["dub"],
              f"实际 = {hints['dub']!r}")
        # ⚠️ **动态状态行不能被静态说明占掉** —— ``#asr-interp-hint``
        #    是 JS 每次都重写的状态行 ⇒ 静态说明必须另起元素。
        check("「朗读语言」的说明**没有**占用动态状态行 #asr-interp-hint",
              page.evaluate("""() => {
                  const el = document.getElementById('asr-interp-hint');
                  if (!el) return true;
                  return !el.classList.contains('asr-tr-hint');
              }"""))

        section("B4. 三个语言下拉的**选项排法**必须一致（中文名 + 本族名）")
        # ⚠️ **为什么要有这条**（2026-11-27）—— 「导出语言」的下拉原先直接用
        #    `it.name` ⇒ 选项是**纯英文名**（"English" / "Chinese"）⇒ 跟
        #    「显示语言」那个下拉的「英语 English」**排法完全不一样** ⇒ 看着
        #    像两套东西 ⇒ 用户以为不是一回事。
        # ⇒ 现在三个下拉统一走 `_trLangLabel()`（同一份标签表 + 字体兜底）。
        opts = page.evaluate(
            """() => {
                const dump = (id) => {
                    const el = document.getElementById(id);
                    if (!el) return [];
                    return Array.from(el.options || [])
                        .map((o) => (o.textContent || "").trim());
                };
                return {
                    tr: dump("asr-tr-target"),
                    interp: dump("asr-interp-target"),
                    dub: dump("asr-dub-target"),
                };
            }""")
        check("「导出语言」下拉有选项", len(opts["dub"]) > 0,
              f"实际 {len(opts['dub'])} 项")
        check("「导出语言」的选项是「中文名 + 本族名」（如「英语 English」）",
              "英语 English" in opts["dub"], f"实际 = {opts['dub']!r}")
        # ⚠️ **防回退**：纯英文名（老排法）不许再出现。
        check("「导出语言」里**没有**纯英文名的选项（老排法）",
              not any(t in ("English", "Chinese", "Japanese")
                      for t in opts["dub"]), f"实际 = {opts['dub']!r}")
        check("「朗读语言」也是同一套排法",
              "英语 English" in opts["interp"], f"实际 = {opts['interp']!r}")
        check("「显示语言」也是同一套排法",
              "英语 English" in opts["tr"], f"实际 = {opts['tr']!r}")
        # ⚠️ **两个下拉的选项集合不一样，这是对的** ——
        #   * 「显示语言」= **翻译模型**能翻的（36 种）；
        #   * 「朗读语言」/「导出语言」= **TTS 能出声**的（10 种）⇒
        #     有些语言能翻但**不能出声**（例：泰语能翻，但本板念不了）。
        # ⇒ 契约是「**导出/朗读 ⊆ 显示**」，不是相等。
        check("「朗读语言」与「导出语言」的选项**集合相同**（都是能出声的那些）",
              set(opts["dub"]) == set(opts["interp"]),
              f"dub={opts['dub']!r} interp={opts['interp']!r}")
        check("「导出语言」的每个语言**在「显示语言」里也找得到**（子集）",
              set(opts["dub"]) <= set(opts["tr"]),
              f"导出={opts['dub']!r} 显示={opts['tr']!r}")

        section("B5. 识别的三个按钮必须与「配音导出」**同一档位置**")
        # ⚠️ **为什么要有这条**（2026-11-27 用户要求）——
        #   「清空 / 停止 / 开始识别」原来挂在**页面最底下**（结果框正上方），
        #   而「配音导出」的「生成配音版 / 停止」是**控件行里并排**的
        #   ⇒ 同一张卡上两套按钮位置完全不一样 ⇒ 用户要求把前者也搬上去，
        #   跟「显示语言」「实时翻译」开关并排。
        # ⇒ 现在三个按钮在「实时翻译」那一行里 ⇒ 断言钉住，防回退。
        btns = page.evaluate(
            """() => {
                const ids = ['asr-reset-btn', 'asr-stop-btn', 'asr-start-btn'];
                const tr = document.getElementById('asr-tr-block');
                const ctrl = document.querySelector('.asr-controls');
                const row = tr ? tr.querySelector('.asr-tr-head') : null;
                const run = tr ? tr.querySelector('.asr-tr-run') : null;
                const sel = document.getElementById('asr-tr-target');
                const y = (el) => el ? Math.round(el.getBoundingClientRect().y) : -1;
                const mid = (el) => {
                    if (!el) return -1;
                    const r = el.getBoundingClientRect();
                    return Math.round(r.y + r.height / 2);
                };
                return {
                    inTr: ids.filter((id) => !!tr.querySelector('#' + id)),
                    inCtrl: ids.filter((id) => !!ctrl && !!ctrl.querySelector('#' + id)),
                    sameRow: ids.every((id) => {
                        const e = document.getElementById(id);
                        return !!(row && e && row.contains(e));
                    }),
                    oneGroup: ids.every((id) => {
                        const e = document.getElementById(id);
                        return !!(run && e && run.contains(e));
                    }),
                    btnY: y(document.getElementById('asr-start-btn')),
                    selY: y(sel),
                    // ⚠️ **比垂直中心，不比顶边** —— 按钮和下拉的**高度不同**
                    //    （``align-items: center``）⇒ 顶边差几像素是正常的，
                    //    只要在**同一行**就对了。
                    btnMid: mid(document.getElementById('asr-start-btn')),
                    selMid: mid(sel),
                    ctrlHasMode: !!(ctrl && ctrl.querySelector('#asr-mode')),
                    dubInRow: (() => {
                        const d = document.getElementById('asr-dub-block');
                        const r = d ? d.querySelector('.asr-tr-head') : null;
                        const b = document.getElementById('asr-dub-start');
                        return !!(r && b && r.contains(b));
                    })(),
                };
            }""")
        check("三个按钮都在「实时翻译」块里", len(btns["inTr"]) == 3,
              f"实际 = {btns['inTr']!r}")
        check("三个按钮**不在**页面底部的控制行里", not btns["inCtrl"],
              f"实际 = {btns['inCtrl']!r}")
        check("三个按钮与「显示语言」**并排在同一行**", btns["sameRow"]
              and abs(btns["btnMid"] - btns["selMid"]) <= 12,
              f"按钮中心 y={btns['btnMid']} 下拉中心 y={btns['selMid']}")
        check("三个按钮**成组**（窄屏时整组换行，不拆散）", btns["oneGroup"])
        check("「配音导出」的「生成配音版」也在它自己的控件行里",
              btns["dubInRow"], "防回退")
        check("底部的「显示模式」下拉还在结果区那边", btns["ctrlHasMode"])

        section("B6. 「实时结果」框顶上要有一行**语言提示**")
        # ⚠️ **为什么要有这条**（2026-11-27 用户要求）——
        #   「实时翻译」的结果框原来**只有句子、没有标题** ⇒ 用户得自己猜
        #   哪行是原文、哪行是译文、译文是哪种语言 ⇒ 跟「配音导出结果」那个
        #   带标题的面板并排时更乱 ⇒ 现在两处**同一个口径**（都标语言）。
        cap_exist = page.evaluate(
            """() => {
                const c = document.getElementById('asr-live-cap');
                const live = document.getElementById('asr-live');
                return {
                    exists: !!c,
                    inLive: !!(c && live && live.contains(c)),
                };
            }""")
        check("「实时结果」框顶上有一行语言提示的元素", cap_exist["exists"])
        check("语言提示在「实时结果」框**里面**", cap_exist["inLive"])

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
        # ⚠️ **等作业「跑完」**，别等「下拉里有文件」——
        #    「生成的文件」下拉**进页面时就有上一个作业的产物**
        #    （``_dubRefreshStatus`` 会补上）⇒ 只等下拉会**立刻返回**
        #    ⇒ 后面 F2 段会在作业**还没跑完**时就断言 ⇒ 假失败。
        deadline = time.time() + 300
        state = ""
        while time.time() < deadline:
            state = page.evaluate(
                "() => document.getElementById('asr-dub-status')"
                "?.textContent || ''")
            if "完成" in state or "失败" in state:
                break
            page.wait_for_timeout(3000)
        opts = page.evaluate(
            "() => Array.from(document.getElementById('asr-dub-files')"
            "?.options || []).map(o => o.value).filter(Boolean)")
        check("作业跑完了（状态 = 完成）", "完成" in state, f"status={state!r}")
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

        section("F2. 配音导出结果面板：逐句原文/译文 + **两种语言**")
        # ⚠️ **为什么要有这一段**（2026-11-27 用户要求）——
        #   配音导出**做了完整的翻译**（ASR → 翻译 → TTS）⇒ 逐句的原文和
        #   译文都在作业里 ⇒ 但页面以前只给一句「完成」和一个文件下拉 ⇒
        #   用户**看不到导出的到底是什么、翻成了什么语言**。
        # ⇒ 现在有一个独立的结果面板：逐句「原文 / 译文」+ **两种语言都标**。
        panel = page.evaluate(
            """() => {
                const wrap = document.getElementById('asr-dub-out');
                const cap = document.getElementById('asr-dub-out-cap');
                const rows = Array.from(
                    document.querySelectorAll('#asr-dub-list .asr-sent'));
                const txt = (el) => (el ? (el.textContent || '').trim() : '');
                return {
                    exists: !!wrap,
                    visible: !!wrap && !wrap.classList.contains('hidden'),
                    cap: txt(cap),
                    n: rows.length,
                    rows: rows.slice(0, 3).map((r) => ({
                        srcTag: txt(r.querySelector('.asr-sent-src .asr-lang-tag')),
                        src: txt(r.querySelector('.asr-sent-src .asr-lang-body')),
                        dstTag: txt(r.querySelector('.asr-sent-dst .asr-lang-tag')),
                        dst: txt(r.querySelector('.asr-sent-dst .asr-lang-body')),
                    })),
                };
            }""")
        check("结果面板存在且**可见**", panel["exists"] and panel["visible"],
              f"exists={panel['exists']} visible={panel['visible']}")
        check("面板里有逐句结果", panel["n"] > 0, f"行数 = {panel['n']}")
        check("标题标了「源语言」和「导出语言」",
              "源语言" in panel["cap"] and "导出语言" in panel["cap"],
              f"实际 = {panel['cap']!r}")
        if panel["rows"]:
            r0 = panel["rows"][0]
            check("每一句都有**原文**和**译文**",
                  bool(r0["src"]) and bool(r0["dst"]),
                  f"实际 = {r0!r}")
            check("原文那行标了**源语言**", bool(r0["srcTag"]),
                  f"实际 = {r0['srcTag']!r}")
            check("译文那行标了**导出语言**", bool(r0["dstTag"]),
                  f"实际 = {r0['dstTag']!r}")
            # ⚠️ **不能拿「导出语言」下拉的当前选中项来比** —— 下拉是**下次**
            #    作业的默认值（默认跟随「显示语言」⇒ 全局目标语言一变它就变），
            #    而面板说的是**刚跑完那个作业**的语言 ⇒ 两者可以合法地不同。
            # ⇒ 改成断言面板**内部自洽**：标题里的「导出语言」 == 每行译文
            #    那个标签。
            cap_tgt = ""
            if "→ 导出语言：" in panel["cap"]:
                cap_tgt = panel["cap"].split("→ 导出语言：", 1)[1]
                cap_tgt = cap_tgt.rstrip("）").strip()
            check("标题里的「导出语言」 = 译文那行的语言标签（面板内部自洽）",
                  bool(cap_tgt) and cap_tgt == r0["dstTag"],
                  f"标题={cap_tgt!r} 标签={r0['dstTag']!r}")

        section("F3. 配音导出有内容 ⇒ 「实时结果」**不重复显示两遍**")
        # ⚠️ **合并规则**（2026-11-27 用户要求）—— 有了「配音导出结果」面板，
        #   它是**更完整**的版本（原文 + 译文 + **两种语言都标出来**）⇒
        #   两个都显示 ⇒ 同一批句子在屏幕上**出现两遍** ⇒ 藏掉「实时结果」。
        # ⇒ 跟「同传有内容 ⇒ 藏掉实时结果」是**同一条规则**。
        merge = page.evaluate(
            """() => {
                const c = document.getElementById('asr-live-cap');
                const s = document.getElementById('asr-sentences');
                const d = document.getElementById('asr-dub-out');
                const live = document.getElementById('asr-live');
                const hid = (el) => !!el && el.classList.contains('hidden');
                return {
                    sentsHidden: hid(s),
                    capHidden: hid(c),
                    liveHidden: hid(live),
                    capText: c ? (c.textContent || '').trim() : '',
                    dubVisible: !hid(d),
                };
            }""")
        check("配音导出结果面板是显示的", merge["dubVisible"])
        check("「实时结果」句子流被藏了（同一批句子不显示两遍）",
              merge["sentsHidden"], f"隐藏={merge['sentsHidden']}")
        # ⚠️ **框里没内容 ⇒ 框本身也要藏** —— 否则屏幕上留一个**空白大框**
        #    （看着像页面坏了）。
        check("「实时结果」框本身也藏了（不留空白大框）",
              merge["liveHidden"], f"隐藏={merge['liveHidden']}")
        # ⚠️ **提示行不能留「有标题、没内容」** ⇒ 它的显隐必须跟句子流一致。
        check("语言提示的显隐跟句子流一致（不留有标题没内容的框）",
              merge["capHidden"] == merge["sentsHidden"],
              f"提示隐藏={merge['capHidden']} 句子流隐藏={merge['sentsHidden']}")
        # ⚠️ 提示**内容**里要有语言 ⇒ 但实时翻译关着时只有「源语言」
        #    （那一行就是原文，没有译文）⇒ 只钉「源语言」。
        check("语言提示里标了「源语言」",
              "源语言" in merge["capText"], f"实际 = {merge['capText']!r}")

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
