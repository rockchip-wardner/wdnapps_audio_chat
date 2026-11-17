/* web-translate 卡片前端 —— 翻译服务的启停 + 状态展示。
 *
 * 这张卡只有一个真正的动作：**启用 / 禁用翻译服务**。
 *
 * 启用 = 起本板的翻译模型实例（默认第一张 1828 协卡）**并**把 NPU 这一路
 * provider 接进板端的翻译链（`app.core.translate.set_npu_translate_enabled`）。
 * 之后网页右上角「界面语言」切换就会走本板 NPU。
 *
 * 禁用 = 先把 NPU 摘出 provider 链，再停模型实例（顺序反了会有一段窗口：
 * 链里还留着 npu、模型已经在卸载，翻译请求一头撞在超时上）。
 *
 * 状态轮询 3s 一次 —— 模型加载要 20~30s，启停期间界面必须能看出变化。
 */
(function () {
  "use strict";

  const API = "/api/web-translate";
  const POLL_MS = 3000;

  // ---- 对外的翻译服务地址（宿主 core 的接口，不是卡片自己的） ----
  //
  // ⚠️ 这里只拼**路径**，host/协议交给 location.origin —— 板子前面有 TLS
  // relay（8000 HTTP / 8443 HTTPS 都通），用户从哪个口访问只有浏览器知道，
  // 后端猜端口必错。
  //
  // 指向 POST /api/translate（板端翻译代理）而不是卡片自己的
  // /api/web-translate/*：后两个是给这张卡的界面用的，别的程序要调翻译
  // 服务走前者。
  const TRANSLATE_API = "/api/translate";
  const TRANSLATE_STATUS_API = "/api/translate/status";

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ //
  // 源语言自动检测
  //
  // ⚠️ 为什么卡片要自己猜源语言：原来 source 被**写死成 "zh-CN"**，于是这张
  // 卡只能做「中文→X」。喂非中文内容时前提是假的 ⇒ 模型行为不可预测：
  //   · 目标 == 源（都 zh-CN）⇒ 恒等翻译 ⇒ 原样返回
  //   · 目标 ≠ 源 ⇒ 模型被告知「源是中文」却拿到英文 ⇒ 短句原样返回、
  //     **长句被翻成中文**（实测 3/3）
  //
  // 检测按**字符集**做 —— 这是唯一不依赖网络、也不会错的信号：
  //   假名 → ja，谚文 → ko，天城文 → hi，阿拉伯文 → ar，泰文 → th，
  //   希伯来文 → he，西里尔文 → ru，汉字 → zh-CN。
  // 拉丁字母那一片（英/法/德/西/葡/意/印尼/马来/土/波/荷/捷/越）**分不出来**
  //   ⇒ 一律当 en —— 模型对源语言标签不敏感（它真正在做的是「把这段文字翻成
  //   目标语言」），而真要精确可以在下拉框里手动选。
  // ------------------------------------------------------------------ //
  const SOURCE_RULES = [
    ["ja", /[\u3040-\u309f\u30a0-\u30ff]/],       // 假名
    ["ko", /[\uac00-\ud7af\u1100-\u11ff]/],       // 谚文
    ["hi", /[\u0900-\u097f]/],                    // 天城文
    ["ar", /[\u0600-\u06ff\u0750-\u077f]/],       // 阿拉伯文
    ["th", /[\u0e00-\u0e7f]/],                    // 泰文
    ["he", /[\u0590-\u05ff]/],                    // 希伯来文
    ["ru", /[\u0400-\u04ff]/],                    // 西里尔文
    ["zh-CN", /[\u4e00-\u9fff\u3400-\u4dbf]/],    // 汉字
  ];

  function detectSource(text) {
    const t = String(text || "");
    for (const [code, re] of SOURCE_RULES) {
      if (re.test(t)) return code;
    }
    return "en";   // 拉丁字母：分不出一律当英语
  }

  /** 语言码 → 中文名（给「源：英语」那句提示用）。 */
  function langLabel(code) {
    const names = {
      "zh-CN": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
      "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语",
      "ru": "俄语", "ar": "阿拉伯语", "hi": "印地语", "th": "泰语",
      "vi": "越南语", "id": "印尼语", "ms": "马来语", "tr": "土耳其语",
      "pl": "波兰语", "nl": "荷兰语", "cs": "捷克语", "he": "希伯来语",
      "uk": "乌克兰语",
    };
    return names[code] || code || "";
  }

  const WebTranslate = {
    status: null,
    _busy: false,      // 启停进行中 —— 轮询让位，别把用户的状态覆盖回去
    _timer: null,
    _inited: false,

    init() {
      if (this._inited) return;
      this._inited = true;

      $("wt-enable-btn")?.addEventListener("click", () => this.enable());
      $("wt-disable-btn")?.addEventListener("click", () => this.disable());
      $("wt-test-btn")?.addEventListener("click", () => this.test());
      $("wt-copy-url")?.addEventListener("click", () => this.copyUrl());
      $("wt-test-input")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.test();
        }
      });

      this.refresh().catch(() => {});
      if (!this._timer) {
        this._timer = setInterval(() => {
          if (document.visibilityState !== "visible") return;
          if (this._busy) return;          // 启停中不轮询
          this.refresh().catch(() => {});
        }, POLL_MS);
      }
    },

    // ------------------------------------------------------------------ //
    // 状态
    // ------------------------------------------------------------------ //
    async refresh() {
      let st;
      try {
        const r = await fetch(API + "/status", { cache: "no-store" });
        if (!r.ok) return;
        st = await r.json();
      } catch (_) {
        return;
      }
      this.status = st;
      this.render(st);
    },

    render(st) {
      const enabled = !!st.enabled;
      const pending = st.instance_status === "loading" || this._busy;
      const installed = !!st.installed;

      // ---- 状态条 ----
      const box = $("wt-service");
      if (box) {
        box.dataset.state = enabled ? "on" : (pending ? "pending" : "off");
      }
      const title = $("wt-service-title");
      if (title) {
        title.textContent = enabled
          ? "翻译服务已启用"
          : (pending ? "翻译服务处理中…" : "翻译服务未启用");
      }
      const badge = $("wt-service-badge");
      if (badge) {
        badge.textContent = enabled ? "NPU 翻译" : (pending ? "处理中" : "外网翻译");
        badge.dataset.kind = enabled ? "npu" : (pending ? "pending" : "remote");
      }
      const detail = $("wt-service-detail");
      if (detail) detail.textContent = st.detail || "";

      // ---- 按钮 ----
      const enableBtn = $("wt-enable-btn");
      const disableBtn = $("wt-disable-btn");
      if (enableBtn) {
        enableBtn.disabled = this._busy || enabled || !installed;
        enableBtn.textContent = this._busy && !enabled ? "正在启用…" : "启用翻译服务";
        enableBtn.title = !installed
          ? "本机未安装翻译模型，无法启用"
          : (enabled ? "翻译服务已启用" : "启用本板 NPU 翻译（默认第一张 1828 协卡）");
      }
      if (disableBtn) {
        disableBtn.disabled = this._busy || (!enabled && !pending);
        disableBtn.textContent = this._busy && enabled ? "正在禁用…" : "禁用翻译服务";
      }

      // ---- 冲突 / 错误提示 ----
      const note = $("wt-service-note");
      if (note) {
        if (st.conflict) {
          note.textContent = st.conflict;
          note.dataset.kind = "error";
        } else if (st.instance_error) {
          note.textContent = st.instance_error;
          note.dataset.kind = "error";
        } else {
          note.textContent = "";
          note.dataset.kind = "";
        }
      }

      // ---- 详情表 ----
      const model = $("wt-model");
      if (model) model.textContent = st.model_id || "—";

      const card = $("wt-card");
      if (card) {
        if (st.npu_index != null && st.npu_index >= 0) {
          card.textContent = "RK1828 协卡 #" + st.npu_index;
        } else {
          const n = (st.cards || []).length;
          card.textContent = n
            ? "默认 RK1828 协卡 #" + st.default_npu_index + "（本机共 " + n + " 张）"
            : "未检测到 NPU 协卡";
        }
      }

      const inst = $("wt-instance");
      if (inst) {
        inst.textContent = _instanceText(st);
      }

      // 并行实例：几份在跑 / 计划几份，以及各自在哪张卡。
      const slotsEl = $("wt-slots");
      if (slotsEl) {
        const running = Number(st.running_count || 0);
        const expected = Number(st.expected_count || 0);
        const insts = st.instances || [];
        const detail = insts
          .map((i) => "卡#" + i.npu_index + " 第" + ((i.replica || 0) + 1) + "份")
          .join("、");
        slotsEl.textContent = (expected
          ? running + " / " + expected + " 份在跑"
          : running + " 份在跑") + (detail ? "（" + detail + "）" : "");
      }

      const chain = $("wt-chain");
      if (chain) {
        chain.textContent = st.provider_in_chain
          ? "NPU（本板） → 外网翻译站（备选）"
          : "外网翻译站（本板 NPU 未启用）";
      }

      // ---- 服务地址（对外可调用的翻译接口） ----
      //
      // origin 来自浏览器（板子可能有 8000/8443 两个入口），后端给不出
      // 「用户看到的那个地址」。
      const origin = (window.location && window.location.origin) || "";
      const urlEl = $("wt-service-url");
      if (urlEl) {
        urlEl.textContent = origin + TRANSLATE_API;
        urlEl.title = origin + TRANSLATE_API;
      }
      const curlEl = $("wt-service-curl");
      if (curlEl) {
        curlEl.textContent =
          `curl -X POST ${origin}${TRANSLATE_API} `
          + `-H 'Content-Type: application/json' `
          + `-d '{"segments":["你好"],"source":"zh-CN","target":"en"}'`;
      }

      // ---- 页头 / 卡头徽标 ----
      const line = $("wt-status-line");
      if (line) {
        line.textContent = enabled ? "已启用" : (pending ? "处理中" : "未启用");
      }
      const inline = $("wt-service-badge-inline");
      if (inline) {
        inline.textContent = enabled ? "已启用" : (pending ? "处理中" : "未启用");
        inline.dataset.state = enabled ? "ok" : (pending ? "busy" : "idle");
      }
      const chainNote = $("wt-chain-note");
      if (chainNote) {
        chainNote.textContent = st.provider_in_chain
          ? "界面翻译走本板 NPU"
          : "界面翻译走外网翻译站";
      }

      // ---- 原始 JSON ----
      const raw = $("data-web-translate");
      if (raw) raw.textContent = JSON.stringify(st, null, 2);
    },

    // ------------------------------------------------------------------ //
    // 启停
    // ------------------------------------------------------------------ //
    async enable() {
      if (this._busy) return null;
      this._busy = true;
      this._setNote("正在启动翻译模型（约 20~30 秒）…", "busy");
      this.render(this.status || {});
      try {
        const st = await this._post(API + "/enable", {});
        this.status = st;
        this._setNote("翻译服务已启用 —— 右上角「界面语言」现在走本板 NPU。", "ok");
        return st;
      } catch (err) {
        this._setNote(_errText(err), "error");
        await this.refresh().catch(() => {});
        throw err;
      } finally {
        this._busy = false;
        this.render(this.status || {});
      }
    },

    async disable() {
      if (this._busy) return null;
      this._busy = true;
      this._setNote("正在停止翻译模型…", "busy");
      this.render(this.status || {});
      try {
        const st = await this._post(API + "/disable", {});
        this.status = st;
        this._setNote("翻译服务已禁用 —— 界面翻译回退到外网翻译站。", "ok");
        return st;
      } catch (err) {
        this._setNote(_errText(err), "error");
        await this.refresh().catch(() => {});
        throw err;
      } finally {
        this._busy = false;
        this.render(this.status || {});
      }
    },

    // ------------------------------------------------------------------ //
    // 试一下
    // ------------------------------------------------------------------ //
    async test() {
      const input = $("wt-test-input");
      const out = $("wt-test-output");
      const btn = $("wt-test-btn");
      const badge = $("wt-test-badge");
      const providerEl = $("wt-test-provider");
      const target = ($("wt-test-target") || {}).value || "en";
      const text = (input && input.value || "").trim();
      if (!text) return;

      // ⚠️ **源语言不能写死成 zh-CN**（2026-11-17 用户报）。
      // 写死 ⇒ 这张卡只能做「中文→X」：喂英文进来、选「翻译成中文」时
      // source == target ⇒ **恒等翻译** ⇒ 原样返回英文；选「翻译成英语」时
      // 模型被告知「源是中文」却拿到英文 ⇒ 前提是假的 ⇒ 短句原样返回、
      // 长句被翻成中文（实测 3/3）。
      const srcPick = ($("wt-test-source") || {}).value || "auto";
      const source = srcPick === "auto" ? detectSource(text) : srcPick;

      if (btn) btn.disabled = true;
      if (badge) { badge.textContent = "翻译中…"; badge.dataset.state = "busy"; }
      if (out) { out.textContent = ""; out.dataset.state = "busy"; }
      try {
        const r = await fetch(API + "/translate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ segments: [text], source: source, target: target }),
        });
        const data = await r.json().catch(() => null);
        if (!r.ok) {
          const msg = (data && data.detail) || ("HTTP " + r.status);
          throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
        }
        const outText = (data.translations || [])[0] || "";
        if (out) {
          out.textContent = outText;
          out.dataset.state = "ok";
        }
        if (badge) { badge.textContent = "完成"; badge.dataset.state = "ok"; }
        if (providerEl) {
          const npu = (data.providers || []).indexOf("npu") >= 0;
          // 顺带把「用的哪个源语言」说出来 —— 自动检测是猜的，用户得看得见
          const srcLabel = langLabel(source);
          const autoMark = srcPick === "auto" ? "（自动检测）" : "";
          providerEl.textContent = (srcLabel ? ("源：" + srcLabel + autoMark + " · ") : "")
            + (npu ? "由本板 NPU 翻译" : "由外网翻译站提供");
        }
      } catch (err) {
        if (out) {
          out.textContent = _errText(err);
          out.dataset.state = "error";
        }
        if (badge) { badge.textContent = "失败"; badge.dataset.state = "error"; }
      } finally {
        if (btn) btn.disabled = false;
      }
    },

    // ------------------------------------------------------------------ //
    // 复制服务地址
    // ------------------------------------------------------------------ //
    async copyUrl() {
      const btn = $("wt-copy-url");
      const el = $("wt-service-url");
      const text = (el && el.textContent) || "";
      if (!text || text === "—") return;
      const ok = await _copyText(text);
      if (!btn) return;
      const old = btn.textContent;
      btn.textContent = ok ? "已复制" : "复制失败";
      btn.dataset.kind = ok ? "ok" : "err";
      setTimeout(() => {
        btn.textContent = old;
        btn.dataset.kind = "";
      }, 1500);
    },

    // ------------------------------------------------------------------ //
    // 内部
    // ------------------------------------------------------------------ //
    async _post(url, body) {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) {
        const msg = (data && data.detail) || ("HTTP " + r.status);
        throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
      return data;
    },

    _setNote(text, kind) {
      const note = $("wt-service-note");
      if (!note) return;
      note.textContent = text || "";
      note.dataset.kind = kind || "";
    },
  };

  // ------------------------------------------------------------------ //
  // 小工具
  // ------------------------------------------------------------------ //
  // 复制到剪贴板。
  //
  // ⚠️ 板子多半是 http://192.168.x.x:8000 ⇒ **非安全上下文**，
  // navigator.clipboard 根本不存在 ⇒ 必须留 execCommand 的退路，
  // 否则「复制」按钮在真机上永远失败（本地调试却是 https，反而好使）。
  async function _copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* 落到下面的退路 */ }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
    } catch (_) {
      return false;
    }
  }

  function _instanceText(st) {
    const s = st.instance_status || "not_loaded";
    const label = {
      running: "运行中",
      loading: "加载中…",
      error: "启动失败",
      not_loaded: "未启动",
    }[s] || s;
    if (s === "error" && st.instance_error) {
      return label + "（" + st.instance_error + "）";
    }
    return label;
  }

  function _errText(err) {
    const raw = (err && err.message) ? err.message : String(err || "");
    // FastAPI 的 422 是 detail 数组 —— 直接显示 JSON 太丑，提取第一条。
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length && arr[0] && arr[0].msg) {
        return arr[0].msg;
      }
    } catch (_) { /* 不是 JSON,原样显示 */ }
    return raw || "未知错误";
  }

  WebTranslate.init();

  // ------------------------------------------------------------------ *
  // 向导完成回调
  //
  // 向导走完 = 用户已经明确要启用翻译服务 —— 所以**直接启用**（默认第一张
  // 1828 协卡），不用再让用户回功能页点一次「启用」。
  //
  // 失败要抛出去：向导的 finish 步会把它显示成错误（并给「重新开启模型」
  // 的重试入口），而不是「向导显示完成、功能页却没生效」。
  // ------------------------------------------------------------------ *
  window.ImApp_web_translate = window.ImApp_web_translate || {};
  window.ImApp_web_translate.applyWizardSnapshot = async function () {
    try {
      const st = await WebTranslate.enable();
      return { enabled: !!(st && st.enabled) };
    } catch (err) {
      throw new Error("start_failed:启用翻译服务失败 —— " + _errText(err));
    }
  };

})();
