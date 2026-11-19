(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;

// 显示用标签。音色的**有效性**由后端 /api/tts/voices 给,这里只管
// 怎么写给人看 —— 没有标签就退回显示原始 id。
const VOICE_LABELS = {
  news_female: "知性女声",
  baby_girl: "萌娃奶音",
  lovely_girl: "甜美萝莉音",
  cool_lady: "高冷御姐音",
  male_announcer: "男播音员音",
  uncle_bass: "磁性大叔音",

  vivian: "Vivian · 中文女声",
  serena: "Serena · 中文女声",
  uncle_fu: "Uncle Fu · 中文男声",
  dylan: "Dylan · 北京话男声",
  eric: "Eric · 四川话男声",
  ryan: "Ryan · 英文男声",
  aiden: "Aiden · 英文男声",
  ono_anna: "Ono Anna · 日文女声",
  sohee: "Sohee · 韩文女声",
};
const LANGUAGE_LABELS = {
  auto: "auto（模型自动判断）",
  chinese: "中文",
  english: "英文",
  japanese: "日文",
  korean: "韩文",
  german: "德文",
  french: "法文",
  russian: "俄文",
  portuguese: "葡萄牙文",
  spanish: "西班牙文",
  italian: "意大利文",
};
const DIALECT_LABELS = {
  sichuan_dialect: "四川话",
  beijing_dialect: "北京话",
};

const TTS = {
  initialized: false,
  ws: null,
  backoff: 1.0,
  lastSnap: null,
  _autoName: "",       // 上次自动填进名字框的值（用来分辨"用户改过"）
  _voiceIds: new Set(), // 清单里所有音色 id（自动填名时避开撞名）
  _voices: null,       // 最近一次拉到的音色清单（向导页也要用）
  _catalogPromise: null, // 清单请求的 promise（向导可能在清单到达前就挂载）
  _wizardVoice: "",    // 向导里选中的音色（换步重建 select 后要还原）
  _droppedFile: null,  // 拖放区收下的 File（PC 上的文件）

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const textEl = document.getElementById("tts-text");
    const voiceEl = document.getElementById("tts-voice");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const volValEl = document.getElementById("tts-volume-value");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const audioEl = document.getElementById("tts-audio");

    if (voiceEl) voiceEl.addEventListener("change", () => {
      this._updateVoiceHint();
      this._updatePreviewButton();
    });
    if (langEl) langEl.addEventListener("change", () => this._updateVoiceHint());
    const previewBtn = document.getElementById("tts-voice-preview");
    if (previewBtn) {
      previewBtn.addEventListener("click", () => { this.previewVoice(); });
    }
    this._catalogPromise = this.loadCatalog();

    if (volEl && volValEl) {
      volEl.addEventListener("input", () => {
        volValEl.textContent = volEl.value + "%";
        if (audioEl) audioEl.volume = Number(volEl.value) / 100;
      });
    }
    if (audioEl) {
      audioEl.volume = 1.0;
      audioEl.addEventListener("error", () =>
        this.setStatus("浏览器播放失败 (autoplay 可能被拦截)", true));
    }
    document.querySelectorAll('input[name="tts-play-to"]').forEach((el) => {
      el.addEventListener("change", () => this._showAudioRowIfNeeded());
    });

    // start() 失败会抛错（向导要看见），但按钮这条路只需要状态行 ——
    // 起一个空的 catch，别在控制台刷 unhandled rejection。
    if (startBtn)   startBtn.addEventListener("click", () => { this.start().catch(() => {}); });
    if (stopBtn)    stopBtn.addEventListener("click", () => this.stop());
    const voiceUploadBtn = document.getElementById("tts-voice-upload-btn");
    if (voiceUploadBtn) {
      voiceUploadBtn.addEventListener("click", () => {
        this.uploadVoice().catch(() => {});
      });
    }
    // 「浏览…」→ 设备端文件浏览器（window.FSModal，走 /api/fs 列板子上的真实文件）。
    // 不用 <input type="file">：那开的是**浏览器所在机器**的文件对话框 —— 在板子
    // 自己的浏览器上要枚举板子的文件系统（慢到几十秒），远程访问时又只看得到 PC
    // 的文件。PC 上的文件走下面的拖放区。
    const voicePathEl = document.getElementById("tts-voice-path");
    const voiceBrowseBtn = document.getElementById("tts-voice-browse-btn");
    if (voiceBrowseBtn) {
      voiceBrowseBtn.addEventListener("click", () => {
        try {
          window.FSModal._sourceTarget = voicePathEl;
          window.FSModal.open("");
        } catch (e) {
          this._setUploadHint("打不开设备端文件浏览器: " + (e && e.message), true);
        }
      });
    }
    if (voicePathEl) {
      // 选完文件自动把名字填好 —— 文件名里的空格/点等字符后端不收，
      // 先净化一遍（中文保留）。
      voicePathEl.addEventListener("change", () => {
        this._setDroppedFile(null);
        this._autofillVoiceName(this._basename(voicePathEl.value));
      });
    }
    // 拖放区：把 PC 上的音频文件拖进来（全程不经过文件对话框）
    const voiceDropZone = document.getElementById("tts-voice-dropzone");
    if (voiceDropZone) {
      voiceDropZone.addEventListener("dragover", (e) => {
        if (!e.dataTransfer?.types?.includes?.("Files")) return;
        e.preventDefault();
        voiceDropZone.classList.add("dragover");
      });
      voiceDropZone.addEventListener("dragleave", () => {
        voiceDropZone.classList.remove("dragover");
      });
      voiceDropZone.addEventListener("drop", (e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        voiceDropZone.classList.remove("dragover");
        this._setDroppedFile(e.dataTransfer.files[0]);
      });
    }
    // 录音取音色（浏览器麦克风 / 板子麦克风两条路）
    const recStart = document.getElementById("tts-rec-start");
    const recStop = document.getElementById("tts-rec-stop");
    const recRetry = document.getElementById("tts-rec-retry");
    const recBoard = document.getElementById("tts-rec-board");
    if (recStart) {
      recStart.addEventListener("click", () => {
        RECORDER.start().catch(() => {});
      });
    }
    if (recStop) {
      recStop.addEventListener("click", () => RECORDER.stop());
    }
    if (recRetry) {
      recRetry.addEventListener("click", () => RECORDER.reset());
    }
    if (recBoard) {
      recBoard.addEventListener("click", () => {
        RECORDER.recordFromBoard().catch(() => {});
      });
    }
    if (typeof RECORDER !== "undefined") RECORDER.refreshAvailability();
    if (textEl) {
      textEl.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          this.start().catch(() => {});
        }
      });
    }
    // 能力变化（去算力页启用/停用模型）→ 页面可见时每 5s 复查；
    // 生成中不打扰。
    if (!this._capTimer) {
      this._capTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        if (this.lastSnap && this.lastSnap.busy) return;
        this.refresh().catch(() => {});
      }, 5000);
    }

    this._watchCloneCardHeights();
  },

  // ------------------------------------------------------------------ //
  // 音色克隆：两张卡等高
  //
  // 并排时靠 CSS grid 的 align-items:stretch 就等高了；窄屏（<900px）
  // grid 变成竖排、每行各占一行，高度就各算各的 —— 这里用 JS 拉齐。
  //
  // 用 ResizeObserver 而不是 load/resize 事件：TTS 页初始是隐藏的
  // （盒模型尺寸全 0），切过来时容器才从 0 变成真实尺寸，observer
  // 才会触发；窗口缩放、内容变化也都覆盖到了。
  // ------------------------------------------------------------------ //
  _syncCloneCardHeights() {
    const cards = Array.from(document.querySelectorAll(".tts-clone-card"));
    if (cards.length < 2) return;
    // 先复位，量到的才是「内容自然高度」（上一轮可能留了行内 height）
    cards.forEach((c) => { c.style.height = ""; });
    const rects = cards.map((c) => c.getBoundingClientRect());
    if (rects.some((r) => !r.height)) return;   // 页面还藏着，量不准
    const stacked = Math.abs(rects[0].top - rects[1].top) > 2;
    // 竖排时给两张卡打个标记：CSS 据此把多出来的高度让给朗读引用块，
    // 而不是让按钮悬在卡片底部、中间空一块。
    cards.forEach((c) => c.classList.toggle("is-stacked", stacked));
    if (!stacked) return;                        // 并排：CSS 已经等高
    const target = Math.max.apply(null, rects.map((r) => r.height));
    cards.forEach((c) => { c.style.height = target + "px"; });
  },

  _watchCloneCardHeights() {
    const grid = document.querySelector(".tts-clone-grid");
    if (!grid) return;
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", () => this._syncCloneCardHeights());
      return;
    }
    const ro = new ResizeObserver(() => this._syncCloneCardHeights());
    ro.observe(grid);
    this._syncCloneCardHeights();
  },

  // ------------------------------------------------------------------ //
  // 音色 / 语种清单
  // ------------------------------------------------------------------ //
  async loadCatalog() {
    try {
      const r = await fetch(API_PREFIX + "/tts/voices");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json() || {};
      const voices = data.voices || [];
      this._voices = voices;
      this._fillVoices(voices);
      this._fillLanguages(data.languages || []);
      this._updateVoiceHint();
    } catch (err) {
      // 清单拿不到 → 下拉留空,后端回落到默认参考音色,功能不瘫。
      const hintEl = document.getElementById("tts-voice-hint");
      if (hintEl) hintEl.textContent = "音色清单加载失败: " + err.message;
    }
  },

  _fillVoices(voices) {
    const el = document.getElementById("tts-voice");
    // 记下所有音色 id —— 自动填名时要躲开它们（重名后端只报 409）。
    this._voiceIds = new Set((voices || []).map((v) => v.id));
    if (!el) return;
    // 重填清单前先记住当前选择 —— 上传/删除音色后要重拉清单，
    // 不保留的话用户刚选好的音色会被无声地冲回第一项。
    const prev = el.value;
    this._appendVoiceOptions(el, voices);
    if (prev) {
      const opt = Array.prototype.find.call(el.options, (o) => o.value === prev);
      if (opt) opt.selected = true;
    }
    this._updatePreviewButton(el);
    this._renderUserVoices(voices);
  },

  // 音色分三组渲染（应用页的下拉、向导页的下拉共用一份）。
  // kind: "ref" = 内嵌参考音色, "builtin" = 模型自带的内置音色,
  // "user" = 用户上传参考音频现场提取的音色。
  _voiceGroups(voices) {
    const ref = [], builtin = [], user = [];
    (voices || []).forEach((v) => {
      if (v.kind === "ref") ref.push(v);
      else if (v.kind === "user") user.push(v);
      else builtin.push(v);
    });
    return [
      ["默认音色", ref],
      ["内置音色（模型自带）", builtin],
      ["我的音色", user],
    ];
  },

  _appendVoiceOptions(sel, voices) {
    sel.innerHTML = "";
    this._voiceGroups(voices).forEach(([label, list]) => {
      if (!list.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      list.forEach((v) => {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = VOICE_LABELS[v.id] || v.id;
        o.dataset.kind = v.kind || "";
        o.dataset.dialect = v.dialect || "";
        // 试听样例的 URL（没有样例 → 空 → 该音色不给试听按钮）
        if (v.preview) o.dataset.preview = v.preview;
        g.appendChild(o);
      });
      sel.appendChild(g);
    });
  },

  // ------------------------------------------------------------------ //
  // 向导里的音色下拉
  //
  // 选项是**运行时**才知道的（音色清单来自后端，用户音色随时会变），
  // wizard.json 是静态的写不了 —— 卡片自己填。
  // ------------------------------------------------------------------ //
  async onWizardStepRendered(appId) {
    if (appId !== "tts") return;
    const sel = document.querySelector(
      '#wd-wizard-root-tts [data-field-id="tts-voice"]');
    if (!sel) return;
    if (this._catalogPromise) await this._catalogPromise;
    const voices = this._voices || [];
    this._appendVoiceOptions(sel, voices);
    // 选谁：向导里之前选的 > 功能页当前选的 > 清单第一条（= SDK 默认音色）。
    //
    // 第一项必须是「向导里之前选的」—— 换步会重建整个 body，新 select 是空的，
    // 若回落成功能页的值，用户在向导里挑的音色会被无声地冲掉（实测：选
    // 「高冷御姐音」→ 下一步 → 上一步 → 变回第一个音色）。
    const appSel = document.getElementById("tts-voice");
    const ids = voices.map((v) => v.id);
    const want = [this._wizardVoice, appSel ? appSel.value : "",
                  ids.length ? ids[0] : ""]
      .find((id) => id && ids.indexOf(id) >= 0);
    if (want) {
      sel.value = want;
      // 派发 change → 向导把值记进 state.values，否则 finish 拿不到。
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    this._attachWizardPreviewBtn(sel);
  },

  // 向导的音色字段旁挂一个「试听」按钮（与功能页同一个）。
  _attachWizardPreviewBtn(sel) {
    const wrap = sel.closest(".wd-wizard-field");
    if (!wrap) return;
    if (wrap.querySelector(".tts-preview-btn")) return;   // 已经在上面了
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wd-wizard-browse-btn tts-preview-btn";
    btn.textContent = "试听";
    btn.title = "播放这个音色的试听样例";
    btn.addEventListener("click", () => { this.previewVoice(sel); });
    wrap.appendChild(btn);
  },

  // ------------------------------------------------------------------ //
  // 我的音色（上传参考音频 → 板端现场提取音色特征）
  // ------------------------------------------------------------------ //
  _renderUserVoices(voices) {
    const listEl = document.getElementById("tts-user-voices-list");
    const countEl = document.getElementById("tts-user-voices-count");
    if (!listEl) return;
    const users = (voices || []).filter((v) => v.kind === "user");
    if (countEl) countEl.textContent = users.length + " 个";
    listEl.innerHTML = "";
    if (!users.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "还没有上传音色。上传一段人声参考音频，系统会在板端现场提取音色特征。";
      listEl.appendChild(li);
      return;
    }
    users.forEach((v) => {
      const li = document.createElement("li");
      li.className = "vd-event";

      const meta = document.createElement("div");
      meta.className = "vd-event-meta";
      const name = document.createElement("span");
      name.className = "tag";
      name.textContent = v.id;
      meta.appendChild(name);

      const del = document.createElement("button");
      del.type = "button";
      del.className = "vd-btn vd-btn-mini vd-event-action";
      del.textContent = "删除";
      del.addEventListener("click", () => this.deleteVoice(v.id));
      meta.appendChild(del);

      li.appendChild(meta);
      listEl.appendChild(li);
    });
  },

  _setUploadHint(text, isError) {
    const el = document.getElementById("tts-voice-upload-hint");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = isError ? "#dc2626" : "";
  },

  // 音色名规则必须和后端一致（后端 _sanitize_voice_name / SDK 的
  // normalize_user_speaker_name）：Unicode 字母、数字、下划线、连字符，以字母或
  // 数字开头，最长 32 个字符。前端先收一道，是为了选完文件就能给出一个人能用的
  // 名字，而不是等上传了才吃一个 400。中文保留（名字是给人看的），只把空格、
  // 点、路径分隔符这些换成 _（后端对它们是直接 400）。
  _sanitizeVoiceName(raw) {
    let s = String(raw == null ? "" : raw).normalize("NFC").trim().toLowerCase();
    s = s.replace(/[^\p{L}\p{N}_-]+/gu, "_");   // 空格/点/斜杠… → _
    s = s.replace(/^[^\p{L}\p{N}]+/u, "");      // 开头必须是字母或数字
    if (s.length > 32) s = s.slice(0, 32);
    s = s.replace(/_+$/, "");             // 尾部多余的 _ 没意义
    return s;
  },

  // 选完文件自动填名。用户手打过的名字不动 —— 只覆盖我们自己填的那个。
  // sourceName 是**原始文件名**（带扩展名）。
  _autofillVoiceName(sourceName) {
    const nameEl = document.getElementById("tts-voice-name");
    if (!nameEl) return;
    const cur = (nameEl.value || "").trim();
    if (cur && cur !== this._autoName) return;  // 用户自己改过 → 不动

    const stem = String(sourceName || "").replace(/\.[^.]*$/, "");
    let base = this._sanitizeVoiceName(stem);
    if (!base) base = "voice";
    // 撞名就加序号 —— 后端对重名是 409 报错而不是覆盖，
    // 内置音色重名同理（下拉里会撞名）。这里先躲开。
    const taken = this._voiceIds || new Set();
    let name = base, i = 2;
    while (taken.has(name)) { name = base + "_" + i; i++; }
    nameEl.value = name;
    this._autoName = name;
  },

  _basename(p) {
    const s = String(p || "");
    const parts = s.split("/");
    return parts[parts.length - 1] || "";
  },

  // 拖放区收下的文件。存着，等点「上传并提取」时用。
  _setDroppedFile(file) {
    this._droppedFile = file || null;
    // 待提取状态显示在**共用区**（两卡下面那一条）—— 待提取的音频可能来自浏览器
    // 麦克风录音，也可能来自拖放，两种来源都在共用区提交，状态就该写在提交的地方。
    const hint = document.getElementById("tts-voice-pending");
    if (hint) {
      hint.textContent = file
        ? ("已就绪：" + file.name + "　点「上传并提取」")
        : "";
    }
    if (file) {
      // 拖进来的文件和路径框是两条互斥的路 —— 留一个就行。
      const pathEl = document.getElementById("tts-voice-path");
      if (pathEl) pathEl.value = "";
      this._autofillVoiceName(file.name);
    }
  },

  _selectVoice(id) {
    const el = document.getElementById("tts-voice");
    if (!el || !id) return;
    const opt = Array.prototype.find.call(el.options, (o) => o.value === id);
    if (opt) {
      el.value = id;
      this._updateVoiceHint();
      this._updatePreviewButton();
    }
  },

  async uploadVoice() {
    const pathEl = document.getElementById("tts-voice-path");
    const nameEl = document.getElementById("tts-voice-name");
    const btn = document.getElementById("tts-voice-upload-btn");
    const file = this._droppedFile;
    const path = (pathEl && pathEl.value || "").trim();
    if (!file && !path) {
      this._setUploadHint(
        "先选一个音频文件：点「浏览…」选板子上的，或把 PC 上的文件拖进来", true);
      return;
    }
    // 名字先按前端自己的规则净化一遍再发 —— 用户手打的「我的 声音」（带空格）
    // 看着合法，直接发出去会被后端 400，而那句话对用户来说莫名其妙。净化后回填
    // 到名字框，让他看见最终用的是哪个名字。
    const rawName = (nameEl && nameEl.value || "").trim();
    const name = this._sanitizeVoiceName(rawName);
    if (!name) {
      this._setUploadHint(
        "音色名不能为空：至少要有一个字母或数字（中文也可以）", true);
      return;
    }
    if (name !== rawName && nameEl) nameEl.value = name;
    if (btn) { btn.disabled = true; btn.textContent = "提取中…"; }
    this._setUploadHint("正在提取音色特征（板端 NPU 推理，约 1 秒）…", false);
    try {
      let r;
      if (file) {
        const fd = new FormData();
        fd.append("file", file, file.name);
        fd.append("name", name);
        r = await fetch(API_PREFIX + "/tts/voices/upload", {
          method: "POST", body: fd,
        });
      } else {
        r = await fetch(API_PREFIX + "/tts/voices/upload-from-path", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path, name: name }),
        });
      }
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      const vname = data.name || "";
      this._setUploadHint(
        "音色「" + vname + "」提取完成，已在下拉里选中。", false);
      this._setDroppedFile(null);
      if (pathEl) pathEl.value = "";
      if (nameEl) nameEl.value = "";
      this._autoName = "";
      await this.loadCatalog();
      this._selectVoice(vname);
    } catch (err) {
      this._setUploadHint("上传失败: " + err.message, true);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "上传并提取"; }
    }
  },

  async deleteVoice(name) {
    if (!window.confirm("删除音色「" + name + "」？该音色将不能再用于合成。")) return;
    this._setUploadHint("正在删除音色「" + name + "」…", false);
    try {
      const r = await fetch(
        API_PREFIX + "/tts/voices/" + encodeURIComponent(name),
        { method: "DELETE" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      this._setUploadHint("音色「" + name + "」已删除。", false);
      await this.loadCatalog();
    } catch (err) {
      this._setUploadHint("删除失败: " + err.message, true);
    }
  },

  _fillLanguages(languages) {
    const el = document.getElementById("tts-language");
    if (!el) return;
    el.innerHTML = "";
    languages.forEach((l) => {
      const o = document.createElement("option");
      o.value = l.id;
      o.textContent = LANGUAGE_LABELS[l.id] || l.id;
      el.appendChild(o);
    });
  },

  _selectedVoice() {
    const el = document.getElementById("tts-voice");
    const opt = this._voiceOpt(el);
    if (!opt || !opt.value) return { id: "", kind: "", dialect: "", label: "" };
    return {
      id: opt.value,
      kind: opt.dataset.kind || "",
      dialect: opt.dataset.dialect || "",
      label: opt.textContent || opt.value,
    };
  },

  // 当前选中的 <option>（功能页与向导页共用）。
  _voiceOpt(sel) {
    return sel && sel.selectedOptions ? sel.selectedOptions[0] : null;
  },

  // 试听按钮的可用性：当前音色没有样例 → 置灰（用户自己的音色就没有）。
  _updatePreviewButton(selectEl) {
    const btn = document.getElementById("tts-voice-preview");
    if (!btn) return;
    const opt = this._voiceOpt(selectEl || document.getElementById("tts-voice"));
    btn.disabled = !(opt && opt.dataset.preview);
  },

  // 试听：播放这个音色的样例（离线生成的一段 WAV）。
  // 样例只覆盖**自带音色** —— 用户自己的音色没有样例（那是 ta 本人的声音，
  // 也不该由系统替 ta 决定念哪句话）。
  async previewVoice(selectEl) {
    const opt = this._voiceOpt(selectEl || document.getElementById("tts-voice"));
    const url = opt ? (opt.dataset.preview || "") : "";
    const label = opt ? (VOICE_LABELS[opt.value] || opt.value) : "";
    if (!url) {
      this.setStatus("「" + (label || "当前音色") + "」没有试听样例", true);
      return;
    }
    const audio = document.getElementById("tts-voice-preview-audio");
    if (!audio) return;
    try {
      if (audio.dataset.src !== url) {
        audio.src = url;           // 换源 → 播放头自动回到 0
        audio.dataset.src = url;
      } else {
        audio.currentTime = 0;     // 同一段 → 手动回到开头重播
      }
      await audio.play();
      this.setStatus("正在试听「" + label + "」");
    } catch (err) {
      this.setStatus("试听失败: " + (err && err.message ? err.message : err), true);
    }
  },

  // SDK 里「方言音色 + 语种 auto/chinese」会把语言 id 锁成该方言 ——
  // 不说清楚,用户选了 auto 却听到四川话,看着像 bug。
  // 其余情况不提示 —— 少说两句比解释一堆更好。
  _updateVoiceHint() {
    const hintEl = document.getElementById("tts-voice-hint");
    if (!hintEl) return;
    const voice = this._selectedVoice();
    const langEl = document.getElementById("tts-language");
    const lang = langEl ? langEl.value : "auto";
    if (voice.dialect && (lang === "auto" || lang === "chinese")) {
      const d = DIALECT_LABELS[voice.dialect] || voice.dialect;
      hintEl.textContent = `「${voice.label}」自带${d}：语种 ${lang} 会锁定为${d}，改选其它语种可解除。`;
    } else {
      hintEl.textContent = "";
    }
  },

  async refresh() {
    try {
      const r = await fetch(API_PREFIX + "/tts");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this.lastSnap = snap;
      this.render(snap);
      this.refreshHistory();
    } catch (err) {
      this.setStatus("加载失败: " + err.message, true);
    }
  },

  async refreshHistory() {
    try {
      const r = await fetch(API_PREFIX + "/tts/history");
      if (!r.ok) return;
      const data = await r.json();
      this.renderHistory(data.items || []);
    } catch (_) {
  // ...
    }
  },

  render(snap) {
    this.lastSnap = snap;
    if (typeof DemoGuard !== "undefined") {
      DemoGuard.report("tts", { session: snap });
    }
    const banner = document.getElementById("tts-unavailable");
    const reasonEl = document.getElementById("tts-unavailable-reason");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const metaEl = document.getElementById("tts-meta");
    const lineEl = document.getElementById("tts-status-line");
    const dataEl = document.getElementById("data-tts");

    if (dataEl) dataEl.textContent = JSON.stringify(snap, null, 2);

    const isBusy = snap.busy === true || snap.running === true || snap.state === "loading" || snap.state === "running";
    const isRunning = snap.running === true;
    const capReady = snap.capability_ready !== false;
    // 「本机有这个能力的实例但还在加载」—— 与「压根没启用」是两回事，
    // 混成一句话会让用户以为自己没开模型，其实模型正在起来。
    const capLoading = Array.isArray(snap.capability_loading)
      && snap.capability_loading.length > 0;

    if (banner && reasonEl) {
      if (snap.available === false) {
        banner.classList.remove("hidden");
        reasonEl.textContent = snap.init_error || "upstream 模块缺失";
      } else if (!capReady) {
        banner.classList.remove("hidden");
        reasonEl.textContent = capLoading
          ? "模型正在加载中，请稍候（加载完成后即可开始生成）"
          : "模型未启用：请到「算力配置」页启用 [TTS] 文字转语音模型";
      } else {
        banner.classList.add("hidden");
      }
    }

    if (startBtn) {
      startBtn.disabled = isBusy || snap.available === false || !capReady;
      startBtn.title = capReady
        ? ""
        : (capLoading
            ? "模型正在加载中，请稍候"
            : "功能未启用：请先在算力配置启用 [TTS] 文字转语音模型");
    }
    if (stopBtn) {
      stopBtn.disabled = !isBusy;
    }

    if (lineEl) {
      lineEl.textContent = `状态: ${snap.state}` + (snap.error ? ` · ${snap.error}` : "");
    }

    if (metaEl) {
      metaEl.textContent = this.formatMeta(snap);
    }
  },

  formatMeta(snap) {
    if (!snap.available) return "语音生成不可用，请检查上游 Qwen3-TTS 模型路径 / rknn3 驱动";
    if (snap.capability_ready === false) {
      const loading = Array.isArray(snap.capability_loading)
        && snap.capability_loading.length > 0;
      return loading
        ? "模型正在加载中，请稍候…"
        : "模型未启用：请先在算力配置启用 [TTS] 模型";
    }
    if (snap.state === "idle")  return `模型: ${snap.model_dir}`;
    if (snap.state === "loading") return "正在加载模型…";
    if (snap.state === "running") {
      const f = snap.frames || 0;
      const sec = (snap.audio_sec || 0).toFixed(2);
      const wall = (snap.elapsed || 0).toFixed(2);
      return `推理中 · 已生成 ${f} 帧 / ${sec}s · 用时 ${wall}s`;
    }
    if (snap.state === "done") {
      return `已完成 · ${snap.frames} 帧 / ${(snap.audio_sec || 0).toFixed(2)}s · 用时 ${(snap.elapsed || 0).toFixed(2)}s`;
    }
    if (snap.state === "stopped") {
      return `已停止 · 输出: ${snap.output || "--"}`;
    }
    if (snap.state === "error") return `错误: ${snap.error}`;
    return `状态: ${snap.state}`;
  },

  /**
   * 启动失败 → 写状态行 **并抛错**。
   *
   * 为什么必须抛：向导的 finish 步靠捕获异常来显示「完成配置失败」，
   * 只写状态行的话向导会照样显示 ✓ —— 用户看到「配置完成」但什么都没
   * 发生（这正是 2026-10-23 实测到的回归：向导跑完没合成）。
   * 错误码用宿主向导认识的 `start_failed`（见 core/wizard/state.js）。
   */
  _startError(detail) {
    this.setStatus(detail, true);
    const e = new Error("WizardSnapshotError:start_failed:" + detail);
    e.retryable = false;
    return e;
  },

  async start() {
    const textEl = document.getElementById("tts-text");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const audioEl = this._audioEl();

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
      throw this._startError("请输入要合成的文本");
    }
    const playTo = this._selectedPlayTo();

    if (audioEl) {
      audioEl.pause();
      audioEl.removeAttribute("src");
      audioEl.load();
      audioEl.classList.add("hidden");
    }

  // ...
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._showAudioRowIfNeeded();

    this.setStatus(
      playTo === "browser" ? "提交中…（将边生成边播浏览器）" :
      playTo === "both"    ? "提交中…（设备 + 浏览器同时边播）" :
                             "提交中…（仅设备播放）",
      false,
    );

  // ...
    if (this._wantsBrowser()) {
      const ctx = await this._ensureAudioCtx();
      if (ctx) {
        try {
          if (ctx.state !== "running") await ctx.resume();
        } catch (_) {}
      }
    }

    const payload = {
      text,
      language: langEl ? langEl.value : "auto",
      volume: volEl ? Number(volEl.value) / 100 : 0.1,
      play_to: playTo,
    };
    // 音色二选一:参考音色和**用户音色**都走 ref_speaker —— 两者同在
    // SDK 的 speaker_embeddings 那张表里(tts.py 的查表就是
    // speaker_embeddings[ref_speaker])。内置音色走 speaker。
    // 两个都发会被 SDK 的 ref_speaker 优先级吃掉 —— 选了内置音色
    // 也是默认音色。
    const voice = this._selectedVoice();
    if (voice.id) {
      if (voice.kind === "builtin") payload.speaker = voice.id;
      else payload.ref_speaker = voice.id;
    }
    // 网络异常单独 catch —— 卡片自己的错误（_startError）不能再被包一层，
    // 否则状态行里会出现 "提交失败: WizardSnapshotError:start_failed:…"。
    let r, body;
    try {
      r = await fetch(API_PREFIX + "/tts/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      body = await r.json().catch(() => ({}));
    } catch (err) {
      throw this._startError("提交失败: " + err.message);
    }
    if (!r.ok) {
      throw this._startError("提交失败: " + (body.detail || r.statusText));
    }
    this.lastSnap = body;
    this.render(body);
    this.ensureWs();
    // ...
    if (this._wantsBrowser()) {
      this._subscribeAudio();
    }
  },

  async stop() {
    try {
      const r = await fetch(API_PREFIX + "/tts/stop", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      this.lastSnap = body;
      this.render(body);
    } catch (err) {
      this.setStatus("停止失败: " + err.message, true);
    }
  },

  setStatus(text, isError) {
    const lineEl = document.getElementById("tts-status-line");
    if (lineEl) {
      lineEl.textContent = text;
      lineEl.style.color = isError ? "#dc2626" : "";
    }
  },

  renderHistory(items) {
    const listEl = document.getElementById("tts-history-list");
    const countEl = document.getElementById("tts-history-count");
    if (!listEl) return;
    listEl.innerHTML = "";
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚无生成记录，启动合成后结果显示在这里。";
      listEl.appendChild(li);
    } else {
      items.slice().reverse().forEach((it) => {
        const li = document.createElement("li");
        li.className = "vd-event";

        const when = new Date((it.ts || 0) * 1000);
        const whenStr = when.toLocaleString();
        const url = it.output_url || "";
        const filename = (it.output || "").split("/").filter(Boolean).pop()
          || "tts.wav";

  // ...
        const meta = document.createElement("div");
        meta.className = "vd-event-meta";
        [
          whenStr,
          it.speaker || "--",
          (it.audio_sec || 0).toFixed(2) + "s",
          (it.frames || 0) + " 帧",
        ].forEach((txt) => {
          const sp = document.createElement("span");
          sp.className = "tag";
          sp.textContent = txt;
          meta.appendChild(sp);
        });
        const path = document.createElement("span");
        path.className = "vd-event-path";
        path.textContent = it.output || "";
        path.title = it.output || "";
        meta.appendChild(path);

        if (url) {
          const playBtn = document.createElement("button");
          playBtn.type = "button";
          playBtn.className = "vd-btn vd-btn-mini vd-event-action";
          playBtn.textContent = "▶ 试听";
          meta.appendChild(playBtn);

          const player = document.createElement("audio");
          player.className = "tts-history-player hidden";
          player.controls = true;
          player.preload = "none";
          player.src = url;
          meta.appendChild(player);

          const dlBtn = document.createElement("a");
          dlBtn.className = "vd-btn vd-btn-mini vd-event-action";
          dlBtn.href = url;
          dlBtn.download = filename;
          dlBtn.textContent = "⬇ 下载";
          dlBtn.title = "下载 " + filename;
          meta.appendChild(dlBtn);

          playBtn.addEventListener("click", async () => {
            const wasHidden = player.classList.contains("hidden");
            document.querySelectorAll(
              ".tts-history-player:not(.hidden)"
            ).forEach((other) => {
              if (other !== player) {
                try { other.pause(); } catch (_) {}
              }
            });
            const shared = this._audioEl();
            if (shared && !shared.classList.contains("hidden")) {
              try { shared.pause(); } catch (_) {}
            }
            if (wasHidden) {
              player.classList.remove("hidden");
              playBtn.textContent = "■ 停止";
              try {
                await player.play();
              } catch (e) {
                this.setStatus("试听失败: " + e.message, true);
              }
            } else {
              try { player.pause(); } catch (_) {}
              player.classList.add("hidden");
              playBtn.textContent = "▶ 试听";
            }
          });
          player.addEventListener("ended", () => {
            playBtn.textContent = "▶ 试听";
            player.classList.add("hidden");
          });
        }

        const text = document.createElement("div");
        text.className = "vd-event-text";
        text.textContent = it.text || "";

        li.appendChild(meta);
        li.appendChild(text);

        listEl.appendChild(li);
      });
    }
    if (countEl) countEl.textContent = items.length + " 条";
  },

  /* ------------------------------------------------------------------ *
   * WebSocket status stream — auto-reconnect with backoff.
   * ------------------------------------------------------------------ */
  ensureWs() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}${API_PREFIX}/tts/ws`;
      this.ws = new WebSocket(url);
    } catch (err) {
      this.setStatus("WS 创建失败: " + err.message, true);
      return;
    }
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (ev) => {
      this.audioFramesTotal = (this.audioFramesTotal || 0) + 1;
  // ...
      const data0 = ev.data;
      if (data0 instanceof Blob) {
        data0.text().then((t) => this._handleTextMessage(t, ev));
        return;
      }
      if (data0 instanceof ArrayBuffer) {
        this._handleBinaryChunk(data0);
        return;
      }
  // ...
      this._handleTextMessage(data0, ev);
    };
    this.ws.onopen = () => {
  // ...
      if (this._wantsBrowser()) {
        this._subscribeAudio();
      } else if (this.pendingAudioSubscribe) {
        this._subscribeAudio();
      }
      this._armPing();
    };
    this.ws.onclose = () => {
      this.ws = null;
      if (this.audioPingTimer) {
        clearInterval(this.audioPingTimer);
        this.audioPingTimer = null;
      }
  // ...
      if (this.lastSnap && (this.lastSnap.busy || this.lastSnap.running)) {
        const delay = Math.min(this.backoff, 8.0) * 1000;
        this.backoff = Math.min(this.backoff * 1.6, 8.0);
        setTimeout(() => this.ensureWs(), delay);
      }
    };
    this.ws.onerror = () => {
      try { this.ws && this.ws.close(); } catch (_) {}
    };
    this.backoff = 1.0;
  },

  /* ------------------------------------------------------------------ *
   * Browser-side audio playback (WebAudio, streaming chunks)
   * ------------------------------------------------------------------ */
  audioCtx: null,
  audioActive: false,        // current stream is feeding the context
  audioSampleRate: 24000,
  audioChannels: 1,
  audioScheduledUntil: 0,    // monotonic ctx time for back-to-back chunks
  audioPushedSec: 0,
  /* True when subscribe_audio was sent but the WS wasn't OPEN yet;
   * ``onopen`` re-issues subscribe when the WS finishes opening. */
  pendingAudioSubscribe: false,
  audioPingTimer: null,

  async _handleTextMessage(text) {
    let snap;
    try { snap = JSON.parse(text); } catch (_) { return; }
    if (!snap) return;
    if (snap.type === "audio_start") {
      this._onAudioStreamStart(snap.sample_rate, snap.channels);
      return;
    }
    if (snap.type === "audio_end") {
      this._onAudioStreamEnd();
      return;
    }
  // ...
    if (snap.type === "pong") return;
    this.lastSnap = snap;
    this.render(snap);
    if (snap.state === "done" || snap.state === "stopped" || snap.state === "error") {
      this.refreshHistory();
    }
  },

  async _ensureAudioCtx() {
    if (this.audioCtx) {
  // ...
      if (this.audioCtx.state !== "running") {
        try { await this.audioCtx.resume(); } catch (_) {}
      }
      return this.audioCtx;
    }
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this.audioCtx = new Ctor();
  // ...
    try { await this.audioCtx.resume(); } catch (_) {}
  // ...
    try {
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      gain.gain.value = 0;
      osc.connect(gain).connect(this.audioCtx.destination);
      osc.start();
    } catch (_) {}
    return this.audioCtx;
  },

  _subscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      this.pendingAudioSubscribe = true;
      return false;
    }
    try {
      ws.send(JSON.stringify({ type: "subscribe_audio" }));
      this.pendingAudioSubscribe = false;
      if (!this.audioActive) {
        this._setAudioChip("订阅已发送", "idle");
      }
      return true;
    } catch (err) {
      this._setAudioChip("订阅失败: " + err.message, "error");
      return false;
    }
  },

  _unsubscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "unsubscribe_audio" })); }
    catch (_) {}
  },

  /* WS-level keep-alive ping; some proxies and NATs reap idle WS
   * connections after ~30-60 s of silence. */
  _armPing() {
    if (this.audioPingTimer) clearInterval(this.audioPingTimer);
    this.audioPingTimer = setInterval(() => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ type: "ping" })); } catch (_) {}
    }, 15000);
  },

  async _onAudioStreamStart(sampleRate, channels) {
    const ctx = await this._ensureAudioCtx();
    if (!ctx) return;
    this.audioSampleRate = sampleRate || 24000;
    this.audioChannels = channels || 1;
    this.audioActive = true;
  // ...
    this.audioScheduledUntil = ctx.currentTime;
    this.audioPushedSec = 0;
    this._setAudioChip("浏览器音频流开始", "live");
  },

  _onAudioStreamEnd() {
    if (!this.audioActive) {
      this._setAudioChip("服务端 audio_end · 未播放", "idle");
      return;
    }
    const sec = this.audioPushedSec;
    this.audioActive = false;
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._setAudioChip(`浏览器音频流结束 · 已播 ${sec.toFixed(2)}s`, "idle");
  },

  _handleBinaryChunk(arrayBuf) {
    if (!this.audioCtx) {
      this._setAudioChip("WebAudio ctx 未创建,请刷新页面重试", "error");
      return;
    }
    if (!this.audioActive) return;

    const volEl = document.getElementById("tts-volume");
    const vol = volEl ? Number(volEl.value) / 100 : 1.0;

  // ...
    const view = new DataView(arrayBuf);
    if (arrayBuf.byteLength < 12) return;
    const sr = view.getUint32(0, true);
    const ch = view.getUint32(4, true);
    const n  = view.getUint32(8, true);
    const sampleCount = sr === this.audioSampleRate
      ? n
      : Math.floor(n * (this.audioSampleRate / (sr || this.audioSampleRate)));
    if (sampleCount <= 0) return;
    const expectedBytes = sampleCount * ch * 4;
    if (arrayBuf.byteLength < 12 + expectedBytes) return;

    const ctx = this.audioCtx;
    const f32 = new Float32Array(arrayBuf.slice(12, 12 + expectedBytes));
    for (let i = 0; i < f32.length; i++) f32[i] *= vol;
    const buffer = ctx.createBuffer(ch, sampleCount, this.audioSampleRate);
  // ...
    for (let c = 0; c < ch; c++) {
      const data = buffer.getChannelData(c);
      if (ch === 1) data.set(f32);
      else for (let i = 0; i < sampleCount; i++) data[i] = f32[i * ch + c];
    }

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
  // ...
    const now = ctx.currentTime;
    const startAt = this.audioScheduledUntil > now
                    ? this.audioScheduledUntil : now;
    src.start(startAt);
    this.audioScheduledUntil = startAt + buffer.duration;
    this.audioPushedSec += buffer.duration;
    this._setAudioChip(`播放 ${this.audioPushedSec.toFixed(2)}s`, "live");
  },

  _setAudioChip(text, state) {
    const chip = document.getElementById("tts-audio-chip");
    if (!chip) return;
    chip.textContent = text;
    if (state) chip.dataset.state = state;
  },

  _showAudioRowIfNeeded() {
    const row = document.getElementById("tts-audio-row");
    if (!row) return;
    row.classList.toggle("hidden", !this._wantsBrowser());
  },

  _audioEl() {
    return document.getElementById("tts-audio");
  },

  _selectedPlayTo() {
    const sel = document.querySelector('input[name="tts-play-to"]:checked');
    return sel ? sel.value : "device";
  },

  _wantsBrowser() {
    const v = this._selectedPlayTo();
    return v === "browser" || v === "both";
  },
};

/* Sidebar 底栏 popover 互相排他 —— 共享 system.js 的 PowerMenu / ConfigMenu /
 * CoprocessorMenu (挂在 ``window.SystemMenus``)。system.js defer 加载顺序
 * 早于本 IIFE,引用全局是安全的。system.js 未加载时静默 no-op(罕见的离线
 * 场景)。 */
function _wrapOpenToCloseOther(menu, other) {
  const orig = menu.open.bind(menu);
  menu.open = function () {
    if (other && typeof other.close === "function") other.close();
    orig();
  };
}
const _menus = window.SystemMenus
  ? [window.SystemMenus.PowerMenu,
     window.SystemMenus.ConfigMenu,
     window.SystemMenus.CoprocessorMenu]
  : [];
for (const a of _menus) {
  for (const b of _menus) {
    if (a !== b && a && b) _wrapOpenToCloseOther(a, b);
  }
}

// 已移除 root 警告横幅：服务不再要求以 root 启动，本系统也不用网页端
// 的关机/重启（那是 `#power-btn` 弹窗的事，与这里无关）。此前这段代码
// 挂在 TTS 卡的 app.js 里、页面一加载就拉 /api/power 并弹全局横幅，
// 导致不进 TTS 页也会看到，且提示的方向已经与实际部署方式相反。

  // ...



// im-bridge page（微信 iLink bot：登录 + 调试收发）**不在本文件** ——
// 它是宿主的一张系统页：app/system/static/im_bridge/app.js。
// （本文件里曾经有一份算力配置页 + 注册发现页的旧拷贝，两块都早已
//  搬进宿主、这边成了没人调用的死代码，已在 v0.32 删掉。）


// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   tts-text     → opts.text
//   tts-play-to  → opts.playTo("browser" | "device" | "both")
// 文本非空 → 写回 #tts-text,然后**启动合成**(复用本文件 TTS.start()
// 的 payload 装配 + play_to 逻辑,不重写一遍)。
// (不再模拟点按钮 —— 理由见下面 WizardStart 那段注释)
// 合成完成后自动试听逻辑同样复用,不重写 payload 装配。
// 播放目标变更 → 同步勾选对应 radio(应用页面 onStart 内部读
// `input[name=tts-play-to]:checked`)。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *

// 向导启动前的「能力就绪」探测 —— **直接问后端快照**,不等卡片自己的
// 5s 轮询那一拍。为什么不能靠卡片快照:快照里的 capability_ready 是
// 「registry 里有没有 running 的 TTS 实例」,模型刚就绪的那 1~5s 里
// 卡片还拿着旧快照 → capability_ready=false → 启动按钮是 disabled。
// 而 **disabled 按钮的 click() 被浏览器静默丢弃**(不报错、不打日志、
// 不触发 handler)—— 向导显示「完成配置」、语音却根本没生成
// (2026-10-23 实测:第一次跑完向导没出声,模型加载完再跑一遍就好了)。
// 详见宿主 core/wizard-start.js 的文件头。
async function ttsCapabilitySnapshot() {
  const r = await fetch(API_PREFIX + "/tts", { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
}

// 调宿主共享的启动器(core/wizard-start.js)。宿主版本太老、没有这个
// 模块时给一句人话错误,而不是 "Cannot read properties of undefined"。
function _wizardStart(opts) {
  const ws = window.WizardStart;
  if (!ws || typeof ws.run !== "function") {
    throw new Error("WizardSnapshotError:helper_missing:宿主缺少 /static/core/wizard-start.js,请更新 wdnwebui 后重试");
  }
  return ws.run(opts);
}

// --------------------------------------------------------------------------- //
// 录音取音色
//
// 两条路：
//   1. **浏览器麦克风**（主路）—— getUserMedia + ScriptProcessor 拿原始
//      PCM，前端编成 WAV，再走 /tts/voices/upload（multipart）。
//      **前提是安全上下文**：浏览器只在 https 或 http://localhost 下
//      暴露 navigator.mediaDevices，明文 http://<板子IP>:8000 里它是
//      undefined，点了也没反应（实测）。
//   2. **板子麦克风**（兜底）—— 明文 HTTP 下浏览器拿不到麦克风，但板子
//      自己带麦克风（ES8388），后端录完直接注册。
// --------------------------------------------------------------------------- //
const REC_MAX_SECONDS = 15;      // 录太久没意义（提取只用前 5 秒）
const REC_MIN_SECONDS = 1.0;      // 太短提不出音色
const REC_BOARD_SECONDS = 6;      // 板子麦克风那条路的默认时长

function recEl(id) { return document.getElementById(id); }

function recSetStatus(text) {
  const el = recEl("tts-rec-status");
  if (el) el.textContent = text || "";
}

function recSetHint(text, isError) {
  const el = recEl("tts-rec-hint");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#dc2626" : "";
}

function recSetMeter(level) {
  const el = recEl("tts-rec-meter");
  if (el) el.style.width = Math.max(0, Math.min(1, level || 0)) * 100 + "%";
}

// state: idle | recording | ready
function recButtons(state) {
  const s = recEl("tts-rec-start"), st = recEl("tts-rec-stop"),
        r = recEl("tts-rec-retry");
  if (s) s.disabled = (state === "recording");
  if (st) st.disabled = (state !== "recording");
  // 「重录」只在真的录到东西之后才有意义（idle 时点它没东西可重录）。
  if (r) r.disabled = (state !== "ready");
}

// Float32 PCM → 16bit PCM 的 RIFF/WAVE（44 字节头）
function encodeWav(samples, sampleRate) {
  const n = samples.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (off, s) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  let off = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    off += 2;
  }
  return buf;
}

const RECORDER = {
  active: false,
  stream: null,
  ctx: null,
  proc: null,
  mute: null,
  chunks: [],
  sampleRate: 0,
  startedAt: 0,
  timer: null,

  supported() {
    const AC = window.AudioContext || window.webkitAudioContext;
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && AC);
  },

  // 页面加载时把不支持的情况说清楚，别让人对着"开始录音"干瞪眼
  refreshAvailability() {
    const ok = this.supported();
    const start = recEl("tts-rec-start");
    if (start) start.disabled = !ok;
    if (!ok) {
      recSetHint("这个页面拿不到麦克风：浏览器只在 HTTPS 或 http://localhost 下"
        + "才给麦克风（现在这个是明文 HTTP）。可以点「用板子麦克风录」，"
        + "或者用 http://localhost:8000 打开本页。", true);
    }
    return ok;
  },

  async start() {
    if (this.active) return;
    if (!this.supported()) { this.refreshAvailability(); return; }
    recSetHint("");
    recButtons("recording");
    recSetMeter(0);
    recSetStatus("正在准备…");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true,
                 noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      recButtons("idle");
      recSetStatus("麦克风打不开");
      recSetHint("麦克风打不开: " + (e && e.message ? e.message : e)
        + "（浏览器会问你要麦克风权限，选「允许」）", true);
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC();
    // 自动播放策略下 AudioContext 可能一上来就是 suspended，
    // suspended 的 context 不出音频回调（onaudioprocess 不触发）。
    try {
      if (this.ctx.state === "suspended") await this.ctx.resume();
    } catch (e) { /* resume 失败就让它自己跑 */ }
    this.sampleRate = this.ctx.sampleRate;
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.proc = this.ctx.createScriptProcessor(4096, 1, 1);
    this.chunks = [];
    this.proc.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(input));
      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
      recSetMeter(Math.min(1, Math.sqrt(sum / input.length) * 6));
    };
    src.connect(this.proc);
    // 必须接到 destination，onaudioprocess 才会被调用；接一个 0 增益的
    // 节点是为了**不回放**麦克风声音（不然外放就啸叫了）。
    this.mute = this.ctx.createGain();
    this.mute.gain.value = 0;
    this.proc.connect(this.mute);
    this.mute.connect(this.ctx.destination);

    this.active = true;
    this.startedAt = Date.now();
    recSetStatus("录音中 0.0s");
    this.timer = setInterval(() => {
      const sec = (Date.now() - this.startedAt) / 1000;
      recSetStatus("录音中 " + sec.toFixed(1) + "s");
      if (sec >= REC_MAX_SECONDS) this.stop();
    }, 100);
  },

  stop() {
    if (!this.active) return;
    this.active = false;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    try { if (this.proc) { this.proc.onaudioprocess = null; this.proc.disconnect(); } } catch (e) { /* 已断开 */ }
    try { if (this.mute) this.mute.disconnect(); } catch (e) { /* 已断开 */ }
    try { if (this.ctx) this.ctx.close(); } catch (e) { /* 已关闭 */ }
    try { if (this.stream) this.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 已停 */ }
    this.proc = null; this.mute = null; this.ctx = null; this.stream = null;

    const total = this.chunks.reduce((a, c) => a + c.length, 0);
    const samples = new Float32Array(total);
    let off = 0;
    for (const c of this.chunks) { samples.set(c, off); off += c.length; }
    this.chunks = [];
    recSetMeter(0);

    if (total === 0) {
      recButtons("idle"); recSetStatus("没录到声音");
      recSetHint("没录到声音 —— 检查一下麦克风有没有被静音，再试一次", true);
      return;
    }
    const sec = total / this.sampleRate;
    if (sec < REC_MIN_SECONDS) {
      recButtons("idle"); recSetStatus("太短");
      recSetHint("只录到 " + sec.toFixed(1) + " 秒，太短了 —— 至少说满 1 秒", true);
      return;
    }
    // 归一化：麦克风电平不可控，太安静的音频提出来的音色不可靠
    // （和后端板子录音那条路用同一套 RMS 目标）。
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length);
    if (rms > 1e-5) {
      const gain = Math.min(0.05 / rms, 20);
      for (let i = 0; i < samples.length; i++) samples[i] *= gain;
    }
    const wav = encodeWav(samples, this.sampleRate);
    const file = new File([wav], "recording_" + Date.now() + ".wav",
                          { type: "audio/wav" });
    TTS._setDroppedFile(file);
    recButtons("ready");
    recSetStatus("已录 " + sec.toFixed(1) + "s");
    recSetHint("录好了，音色名已自动填好 —— 点「上传并提取」开始提取。", false);
  },

  reset() {
    recButtons("idle");
    recSetStatus("未开始");
    recSetHint("");
    recSetMeter(0);
    // 「重录」= 把上一段丢掉。不清掉的话提示还挂着「已就绪：xxx.wav」，
    // 用户会以为那段还在（名字框倒是该留着，用户可能改过名）。
    TTS._setDroppedFile(null);
  },

  // 板子麦克风那条路：音频在板子上，后端录完直接注册（不用再上传一次）
  async recordFromBoard() {
    const nameEl = recEl("tts-voice-name");
    let name = (nameEl && nameEl.value || "").trim();
    if (!name) {
      name = "rec_" + Date.now();
      if (nameEl) nameEl.value = name;
    }
    recButtons("recording");
    recSetStatus("板子录音中…");
    recSetHint("对着板子的麦克风说话，说满 " + REC_BOARD_SECONDS + " 秒。");
    try {
      const r = await fetch(API_PREFIX + "/tts/voices/record-from-board", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds: REC_BOARD_SECONDS, name: name }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      recButtons("ready");
      recSetStatus("已录 " + REC_BOARD_SECONDS + "s");
      recSetHint("板子麦克风录好了，音色「" + (data.name || name)
        + "」已经注册并在下拉里选中。", false);
      await TTS.loadCatalog();
      TTS._selectVoice(data.name || name);
    } catch (e) {
      recButtons("idle");
      recSetStatus("没录成");
      recSetHint("录音失败: " + (e && e.message ? e.message : e), true);
    }
  },
};

window.ImApp_tts = window.ImApp_tts || {};
// 把卡片对象挂出去：浏览器控制台里能直接 TTS.snapshot() 查状态。
window.ImApp_tts.TTS = TTS;
window.ImApp_tts.RECORDER = RECORDER;
// 向导里音色下拉的选项是运行时才有的 → 卡片自己填（详见 TTS.onWizardStepRendered）。
window.addEventListener("wizard:step-rendered", (ev) => {
  const d = ev && ev.detail;
  TTS.onWizardStepRendered(d && d.appId).catch(() => {});
});
// 向导里改了音色 → 记住。换步会重建 body，select 会被清空，没有这份记忆
// 用户在向导里选的音色会被无声地冲回功能页的值。
window.addEventListener("wizard:field-change", (ev) => {
  const vals = ev && ev.detail && ev.detail.values;
  const v = vals && vals["tts-voice"];
  if (v) TTS._wizardVoice = v;
});
window.ImApp_tts.applyWizardSnapshot = async function (opts) {
  const result = { text_filled: false, started: false };
  if (opts.text) {
    const textEl = document.getElementById("tts-text");
    if (textEl) textEl.value = opts.text;
    result.text_filled = true;
  }
  if (opts.playTo) {
    document.querySelectorAll('input[name="tts-play-to"]').forEach((r) => {
      r.checked = (r.value === opts.playTo);
    });
  }
  // 音色：向导里选的写回功能页的下拉 —— TTS.start() 就是从那个下拉读
  // 音色（_selectedVoice），写回即可，不用在这里重写一遍 payload 装配。
  if (opts.voice) {
    TTS._selectVoice(opts.voice);
  }
  // 文本非空 → 启动合成。**不能 btn.click()**:启动按钮的 disabled 由
  // 卡片快照的 capability_ready 决定,模型刚就绪时按钮还是灰的,而
  // disabled 按钮的 click() 会被浏览器**静默丢弃** → 向导显示「完成
  // 配置」、语音却没生成(2026-10-23 实测)。改成宿主共享的 WizardStart:
  // 先等能力真就绪(直接问后端),再直接调 TTS.start();等不到就抛错让
  // 向导显示「重新开启模型」。
  if (opts.text) {
    await _wizardStart({
      label: "语音合成",
      probe: ttsCapabilitySnapshot,
      // 就绪判据:后端说这个能力有 running 的实例(和 get_session 同口径)
      isReady: (s) => !!(s && s.available !== false && s.capability_ready === true),
      // 启动判据:服务已进入 loading/running(= 合成真的开跑了)
      isStarted: (s) => !!(s && (s.busy === true
        || s.state === "loading" || s.state === "running")),
      start: async () => {
        await TTS.start();
      },
      // 模型刚起来时 registry 可能还在收敛,给足等待再判定失败
      timeoutMs: 30000,
    });
    result.started = true;
  }
  return result;
};

// 卡片初始化放最后 —— RECORDER 是这个作用域里的 const，init() 里要挂
// 它的按钮，声明前引用会踩 TDZ（Cannot access before initialization）。
TTS.init();

})();
