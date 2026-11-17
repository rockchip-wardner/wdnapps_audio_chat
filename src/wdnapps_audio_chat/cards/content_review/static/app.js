/* content-review 卡片前端 —— 决策模型的启停 + 审查维度列表 + 概率结果。
 *
 * 这张卡的核心不是聊天，而是一张**可增删的列表**：用户维护一组审查维度，
 * 输入一张图 + 一段文字后，后端把每一项翻译成一个决策字段（是否），模型
 * 逐项给出概率（0~100%）。各项**互相独立**（一条内容可命中多项）。
 *
 * 维度名会直接进模型 prompt（决策任务的字段名），所以「广告」「诈骗」这种
 * 带语义的写法比 item_0 准得多 —— 这也是默认维度用中文名的原因。
 */
(function () {
  "use strict";

  const API = "/api/content-review";
  const POLL_MS = 3000;
  const LS_KEY = "wdnwebui.content_review.items";
  /** 模型硬上限（决策任务最多 16 个字段）。 */
  const MAX_ITEMS = 16;
  /** 图像上传前先在浏览器缩到这么大长边（省流量；模型端还会再缩到 384×384）。 */
  const MAX_IMAGE_EDGE = 1280;
  const JPEG_QUALITY = 0.85;

  const DEFAULT_ITEMS = [
    { name: "广告", description: "画面或文字里有推销、引流、优惠信息" },
    { name: "诈骗", description: "涉及转账、冒充、诱导点击等欺诈内容" },
    { name: "色情低俗", description: "露骨或低俗的性相关内容" },
    { name: "暴力血腥", description: "血腥、暴力、伤害相关的画面或描述" },
  ];

  const $ = (id) => document.getElementById(id);

  const ContentReview = {
    status: null,
    _busy: false,      // 启停 / 审查进行中 —— 轮询让位
    _timer: null,
    _inited: false,
    _items: [],
    _imageB64: "",
    _imageName: "",

    init() {
      if (this._inited) return;
      this._inited = true;

      this._items = loadItems();
      this.renderItems();

      $("cr-enable-btn")?.addEventListener("click", () => this.enable());
      $("cr-disable-btn")?.addEventListener("click", () => this.disable());
      $("cr-add-item")?.addEventListener("click", () => this.addItem());
      $("cr-reset-items")?.addEventListener("click", () => {
        this._items = DEFAULT_ITEMS.map((x) => ({ ...x }));
        saveItems(this._items);
        this.renderItems();
      });
      $("cr-run-btn")?.addEventListener("click", () => this.run());
      $("cr-image-btn")?.addEventListener("click", () => $("cr-image-input")?.click());
      $("cr-image-input")?.addEventListener("change", (e) => this.pickImage(e));
      $("cr-image-clear")?.addEventListener("click", () => this.clearImage());

      this.refresh().catch(() => {});
      if (!this._timer) {
        this._timer = setInterval(() => {
          if (document.visibilityState !== "visible") return;
          if (this._busy) return;          // 启停/审查中不轮询
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

      const box = $("cr-service");
      if (box) box.dataset.state = enabled ? "on" : (pending ? "pending" : "off");

      const title = $("cr-status-title") || $("cr-service-title");
      if (title) {
        title.textContent = enabled
          ? "决策模型已就绪"
          : (pending ? "决策模型处理中…" : "决策模型未启用");
      }
      const badge = $("cr-service-badge");
      if (badge) {
        badge.textContent = enabled ? "就绪" : (pending ? "处理中" : "未就绪");
        badge.dataset.kind = enabled ? "npu" : (pending ? "pending" : "remote");
      }
      const detail = $("cr-service-detail");
      if (detail) detail.textContent = st.detail || "";

      const enableBtn = $("cr-enable-btn");
      const disableBtn = $("cr-disable-btn");
      if (enableBtn) {
        enableBtn.disabled = this._busy || enabled || !st.installed;
        enableBtn.textContent = this._busy && !enabled ? "正在启用…" : "启用决策模型";
        enableBtn.title = !st.installed
          ? "本机未安装决策模型，无法启用"
          : (enabled ? "决策模型已启用" : "起一个决策模型实例（默认第一张 1828 协卡）");
      }
      if (disableBtn) {
        disableBtn.disabled = this._busy || (!enabled && !pending);
        disableBtn.textContent = this._busy && enabled ? "正在禁用…" : "禁用决策模型";
      }

      const note = $("cr-service-note");
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

      // 模型信息（算力页那张卡的模型 id / 标签）
      const modelLabel = $("cr-model-label");
      if (modelLabel) modelLabel.textContent = st.model_label || st.model_id || "—";

      const line = $("cr-status-line");
      if (line) line.textContent = enabled ? "已就绪" : (pending ? "处理中" : "未启用");
      const inline = $("cr-items-limit");
      if (inline && !inline.dataset.fixed) {
        inline.textContent = "上限 " + (st.max_items || MAX_ITEMS) + " 项";
        inline.dataset.fixed = "1";
      }

      const raw = $("data-content-review");
      if (raw) raw.textContent = JSON.stringify(st, null, 2);
    },

    // ------------------------------------------------------------------ //
    // 审查维度列表（可增删）
    // ------------------------------------------------------------------ //
    addItem() {
      if (this._items.length >= MAX_ITEMS) {
        this.setRunNote("最多 " + MAX_ITEMS + " 项（模型上限）", "error");
        return;
      }
      this._items.push({ name: "", description: "" });
      saveItems(this._items);
      this.renderItems();
      // 焦点落到新加的那一项，省得用户再去点输入框
      const inputs = document.querySelectorAll("#cr-items .cr-item-name");
      const last = inputs[inputs.length - 1];
      if (last) last.focus();
    },

    renderItems() {
      const box = $("cr-items");
      if (!box) return;
      box.innerHTML = "";
      this._items.forEach((item, idx) => {
        const row = document.createElement("div");
        row.className = "cr-item-row";

        const name = document.createElement("input");
        name.type = "text";
        name.className = "cr-item-name";
        name.placeholder = "维度名（如：广告）";
        name.value = item.name || "";
        name.addEventListener("input", () => {
          this._items[idx].name = name.value;
          saveItems(this._items);
        });

        const desc = document.createElement("input");
        desc.type = "text";
        desc.className = "cr-item-desc";
        desc.placeholder = "说明（可选）：这一项具体指什么";
        desc.value = item.description || "";
        desc.addEventListener("input", () => {
          this._items[idx].description = desc.value;
          saveItems(this._items);
        });

        const del = document.createElement("button");
        del.type = "button";
        del.className = "vd-btn cr-item-del";
        del.textContent = "删除";
        del.addEventListener("click", () => {
          this._items.splice(idx, 1);
          saveItems(this._items);
          this.renderItems();
        });

        row.appendChild(name);
        row.appendChild(desc);
        row.appendChild(del);
        box.appendChild(row);
      });

      const badge = $("cr-items-badge");
      if (badge) {
        badge.textContent = this._items.length + " 项";
        badge.dataset.state = this._items.length ? "ok" : "idle";
      }
      const addBtn = $("cr-add-item");
      if (addBtn) addBtn.disabled = this._items.length >= MAX_ITEMS;
    },

    // ------------------------------------------------------------------ //
    // 图片
    // ------------------------------------------------------------------ //
    async pickImage(e) {
      const input = e && e.target;
      const file = input && input.files && input.files[0];
      if (!file) return;
      if (!/^image\//.test(file.type || "")) {
        this.setRunNote("请选择图片文件", "error");
        return;
      }
      try {
        const b64 = await fileToBase64(file);
        this._imageB64 = b64;
        this._imageName = file.name || "";
        const preview = $("cr-image-preview");
        if (preview) {
          preview.src = "data:image/jpeg;base64," + b64;
          preview.hidden = false;
        }
        const clear = $("cr-image-clear");
        if (clear) clear.hidden = false;
        const hint = $("cr-image-hint");
        if (hint) hint.textContent = this._imageName + "（已就绪）";
        this.setRunNote("", "");
      } catch (err) {
        this.setRunNote("图片读取失败：" + (err && err.message || err), "error");
      } finally {
        if (input) input.value = "";
      }
    },

    clearImage() {
      this._imageB64 = "";
      this._imageName = "";
      const preview = $("cr-image-preview");
      if (preview) {
        preview.removeAttribute("src");
        preview.hidden = true;
      }
      const clear = $("cr-image-clear");
      if (clear) clear.hidden = true;
      const hint = $("cr-image-hint");
      if (hint) hint.textContent = "可不选 —— 纯文字也能审查";
    },

    // ------------------------------------------------------------------ //
    // 审查
    // ------------------------------------------------------------------ //
    async run() {
      if (this._busy) return;
      const items = this._items
        .map((i) => ({ name: (i.name || "").trim(), description: (i.description || "").trim() }))
        .filter((i) => i.name);
      if (!items.length) {
        this.setRunNote("请至少填写一个维度名", "error");
        return;
      }
      if (items.length > MAX_ITEMS) {
        this.setRunNote("最多 " + MAX_ITEMS + " 项（模型上限）", "error");
        return;
      }
      const text = ($("cr-text") && $("cr-text").value || "").trim();
      if (!text && !this._imageB64) {
        this.setRunNote("请输入待审查的文字，或选择一张图片", "error");
        return;
      }

      this._busy = true;
      this.setRunNote("审查中…（图文决策约 9 秒，首次还要加载视觉模型）", "busy");
      const badge = $("cr-review-badge");
      if (badge) { badge.textContent = "审查中…"; badge.dataset.state = "busy"; }
      const btn = $("cr-run-btn");
      if (btn) btn.disabled = true;

      try {
        const r = await fetch(API + "/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: text,
            items: items,
            image_b64: this._imageB64 || "",
          }),
        });
        const data = await r.json().catch(() => null);
        if (!r.ok) {
          const msg = (data && data.detail) || ("HTTP " + r.status);
          throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
        }
        this.renderResults(data);
        this.setRunNote("完成", "ok");
        if (badge) { badge.textContent = "完成"; badge.dataset.state = "ok"; }
      } catch (err) {
        this.setRunNote(errText(err), "error");
        if (badge) { badge.textContent = "失败"; badge.dataset.state = "error"; }
      } finally {
        this._busy = false;
        if (btn) btn.disabled = false;
      }
    },

    renderResults(data) {
      const box = $("cr-results");
      if (!box) return;
      box.innerHTML = "";
      const results = (data && data.results) || [];
      if (!results.length) {
        const p = document.createElement("p");
        p.className = "cr-empty";
        p.textContent = "没有返回任何维度的结果。";
        box.appendChild(p);
        return;
      }
      results.forEach((r) => {
        const pct = Math.max(0, Math.min(100, Math.round((r.probability || 0) * 100)));
        const row = document.createElement("div");
        row.className = "cr-result-row";
        if (pct >= 50) row.dataset.level = "high";
        else if (pct >= 20) row.dataset.level = "mid";
        else row.dataset.level = "low";

        const head = document.createElement("div");
        head.className = "cr-result-head";

        const name = document.createElement("span");
        name.className = "cr-result-name";
        name.textContent = r.name || "";
        head.appendChild(name);

        const val = document.createElement("span");
        val.className = "cr-result-pct";
        val.textContent = pct + "%";
        head.appendChild(val);

        const bar = document.createElement("div");
        bar.className = "cr-bar";
        const fill = document.createElement("div");
        fill.className = "cr-bar-fill";
        fill.style.width = pct + "%";
        bar.appendChild(fill);

        const desc = document.createElement("div");
        desc.className = "cr-result-desc";
        const bits = [];
        if (r.description) bits.push(r.description);
        if (r.symbol) bits.push("符号 " + r.symbol + (r.label ? "（" + r.label + "）" : ""));
        desc.textContent = bits.join(" · ");

        row.appendChild(head);
        row.appendChild(bar);
        if (desc.textContent) row.appendChild(desc);
        box.appendChild(row);
      });

      const meta = $("cr-result-meta");
      if (meta) {
        const bits = [];
        if (data.elapsed_sec != null) bits.push(data.elapsed_sec + " 秒");
        if (data.has_image) bits.push("图文");
        else bits.push("纯文本");
        if (data.temperature != null) {
          bits.push("校准温度 " + Number(data.temperature).toFixed(4));
        }
        meta.textContent = bits.join(" · ");
      }
      const raw = $("data-content-review");
      if (raw) raw.textContent = JSON.stringify(data, null, 2);
    },

    // ------------------------------------------------------------------ //
    // 启停
    // ------------------------------------------------------------------ //
    async enable() {
      if (this._busy) return null;
      this._busy = true;
      this.setNote("正在启动决策模型（约 40 秒）…", "busy");
      this.render(this.status || {});
      try {
        const st = await this._post(API + "/enable", {});
        this.status = st;
        this.setNote("决策模型已就绪 —— 可以提交图文审查。", "ok");
        return st;
      } catch (err) {
        this.setNote(errText(err), "error");
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
      this.setNote("正在停止决策模型…", "busy");
      this.render(this.status || {});
      try {
        const st = await this._post(API + "/disable", {});
        this.status = st;
        this.setNote("决策模型已禁用。", "ok");
        return st;
      } catch (err) {
        this.setNote(errText(err), "error");
        await this.refresh().catch(() => {});
        throw err;
      } finally {
        this._busy = false;
        this.render(this.status || {});
      }
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

    setNote(text, kind) {
      const note = $("cr-service-note");
      if (!note) return;
      note.textContent = text || "";
      note.dataset.kind = kind || "";
    },

    setRunNote(text, kind) {
      const note = $("cr-run-note");
      if (!note) return;
      note.textContent = text || "";
      note.dataset.kind = kind || "";
    },
  };

  // ------------------------------------------------------------------ //
  // 小工具
  // ------------------------------------------------------------------ //
  /** 图片 → base64（先缩到 MAX_IMAGE_EDGE 再转 JPEG，省流量）。 */
  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.onload = () => {
        const img = new Image();
        img.onerror = () => reject(new Error("图片解码失败"));
        img.onload = () => {
          const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(img.width, img.height));
          const w = Math.max(1, Math.round(img.width * scale));
          const h = Math.max(1, Math.round(img.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, w, h);
          // JPEG 去掉 alpha（决策模型的视觉输入是 RGB）
          resolve(canvas.toDataURL("image/jpeg", JPEG_QUALITY).split(",")[1]);
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  function loadItems() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      const arr = raw ? JSON.parse(raw) : null;
      if (Array.isArray(arr) && arr.length) {
        return arr
          .filter((x) => x && typeof x === "object")
          .slice(0, MAX_ITEMS)
          .map((x) => ({ name: String(x.name || ""), description: String(x.description || "") }));
      }
    } catch (_) { /* 坏了就用默认 */ }
    return DEFAULT_ITEMS.map((x) => ({ ...x }));
  }

  function saveItems(items) {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(items));
    } catch (_) { /* 配额满了就算了 */ }
  }

  function errText(err) {
    const raw = (err && err.message) ? err.message : String(err || "");
    // FastAPI 的 422 是 detail 数组 —— 直接显示 JSON 太丑，提取第一条。
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length && arr[0] && arr[0].msg) {
        return arr[0].msg;
      }
    } catch (_) { /* 不是 JSON，原样显示 */ }
    return raw || "未知错误";
  }

  ContentReview.init();

  // ------------------------------------------------------------------ *
  // 向导完成回调
  //
  // 向导走完 = 用户已经明确要用这张卡 —— 所以**直接启用**决策模型，
  // 不用再让用户回功能页点一次「启用」。
  //
  // 失败要抛出去：向导的 finish 步会把它显示成错误（并给「重新开启模型」
  // 的重试入口），而不是「向导显示完成、功能页却没生效」。
  // ------------------------------------------------------------------ *
  window.ImApp_content_review = window.ImApp_content_review || {};
  window.ImApp_content_review.applyWizardSnapshot = async function () {
    try {
      const st = await ContentReview.enable();
      return { enabled: !!(st && st.enabled) };
    } catch (err) {
      throw new Error("start_failed:启用决策模型失败 —— " + errText(err));
    }
  };

})();
