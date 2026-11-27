(function () {
  "use strict";
  const API_PREFIX = "/api";
  const STATIC_PREFIX = "/static";

  /* ------------------------------------------------------------------ *
   * 含音轨的视频容器（后缀，不含点）
   *
   * 与后端 ``app/core/audio/decode.py::VIDEO_EXTS`` 对齐 —— 前端判
   * 「这个文件该用 <video> 播还是 <audio> 播」，后端判「这个文件能不能
   * 当媒体取流」。两边口径不一致的话，会出现「前端当视频播、后端当
   * 音频收」这种错位。
   * ------------------------------------------------------------------ */
  const ASR_VIDEO_EXTS = new Set([
    "mp4", "mov", "mkv", "avi", "flv", "webm", "ts", "m4v",
    "wmv", "mpg", "mpeg", "3gp",
  ]);

  /* ------------------------------------------------------------------ *
   * 实时识别（麦克风）的常量
   *
   * CHUNK_MS —— 采集端攒块粒度。500 ms 兼顾「首字延迟」与「上行开销」：
   * 攒得越大首字越晚，越小上行越频繁。
   *
   * MAX_PENDING —— L1 采集层背压：发送队列上限（块）。满了丢**最旧**的
   * 块。理由：实时场景下「晚到的旧音频」价值低于「最新的音频」——
   * 宁可丢字，不可延迟膨胀。
   * ------------------------------------------------------------------ */
  const LIVE_CHUNK_MS = 500;
  const LIVE_CHUNK_SAMPLES = (16000 * LIVE_CHUNK_MS) / 1000;   // 8000
  const LIVE_MAX_PENDING = 2;

  /* ------------------------------------------------------------------ *
   * 节奏字幕（YouTube 式实时字幕）的常量
   *
   * 字幕不是「推」出来的，是**按播放头的位置渲染**出来的 —— 服务端是
   * 整文件快转，识别结果在播放开始前就全到手了。所以这些常量描述的
   * 是「怎么把一段文本摆到画面上」，不是「什么时候推」。
   * ------------------------------------------------------------------ */
  const SUB_MAX_VISIBLE_LINES = 3;    // 画面上最多同时显示几行
  const SUB_PREV_HOLD_SEC = 3.2;      // 上一行说完后还留多久（然后淡出）
  //: 「当前行」判据的**边界容差**（秒）。
  //:
  //: 判据是「这一行的时间区间**包含**播放头」⇒ 两行相接的地方（上一行
  //: 结束 == 下一行开始）播放头正好卡在边界上 ⇒ 不给容差的话，每一帧
  //: 都可能判成「洞」⇒ 字幕一帧一帧地闪。
  const SUB_CUR_TOL_SEC = 0.25;
  const SUB_MAX_LINE_COLS = 42;       // 一行最多多少「列」（CJK 占两列）
  const SUB_MIN_SOFT_CHARS = 10;      // 逗号断行至少要攒这么多字
  const SUB_MAX_LINE_SEC = 7.0;       // 一行最多跨多少秒
  const SUB_MIN_LINE_SEC = 0.5;       // 一行至少占多久（防「闪过去」）

  /* ------------------------------------------------------------------ *
   * 「实时翻译」的请求调度
   *
   * ⚠️ **为什么要有上限**：行级翻译是**前端驱动**的（每行一次请求）。一集
   * 98 分钟的访谈有 **1477 行**（实测：580 秒 ⇒ 146 行）⇒ 一次「打开实时
   * 翻译」就会**同时**发出上千个请求 ⇒ 服务端队列（当时上限 32）瞬间爆掉，
   * **丢最旧的** ⇒ 丢掉的正是**播放头附近那批行** ⇒ 画面字幕上一直**没有
   * 译文**（用户报：「再勾选实时翻译，字幕上没有实时翻译」）。
   *
   * ⇒ 三个约束：
   *   ① **每轮最多发 N 个**（``_TR_REQ_BATCH``）⇒ 队列永远不满；
   *   ② **播放头附近的行先发**（``_trRequestLines`` 里的顺序）⇒ 屏幕上
   *      那一行 **1 秒内**就有译文，其余的行在后台慢慢补；
   *   ③ **速率不超过服务端的消化能力**（一台翻译实例 ≈ 3.8 句/秒）⇒
   *      队列不会积压 ⇒ **不会丢**任何一句。
   * ------------------------------------------------------------------ */
  const TR_REQ_BATCH = 2;             // 每轮最多发几个翻译请求
  const TR_REQ_TICK_MS = 600;         // 多久跑一轮（≈ 3.3 个/秒）

  /**
   * **播放头的心跳**（毫秒）—— 播放头**没变**时也要每隔这么久上报一次。
   *
   * ⚠️ **为什么要有**（2026-11-25）：播放头原来只在**变化**时上报 ⇒ 媒体
   * **暂停**在某个位置后就**再也不报了** ⇒ 服务端那边「播放头多久没来」
   * 一直涨 ⇒ 被误判成「**前端失联**」（`playhead_missing`）⇒ 面板会说
   * 「播放头没上报」—— 而真相是「**媒体暂停着**」。
   *
   * ⇒ 这两件事**必须分开说**：**前端还在不在**（心跳）≠ **媒体在不在动**。
   * ⇒ 心跳让服务端的「多久没来」只反映前者。
   */
  const PH_HEARTBEAT_MS = 2000;

  /**
   * **服务端队列的「软上限」** —— 队列深到这个数就**先别再塞**。
   *
   * ⚠️ **为什么需要**：客户端的速率**只是上限**，不是承诺 —— 服务端这台
   * 板子的翻译实例可能只有 **2.7 句/秒**（实测），比上限低 ⇒ 队列会
   * **慢慢积压** ⇒ 积到 ``QUEUE_MAX``（256）就**丢最旧的** ⇒ 丢的正是
   * **先要的那几行**（= 播放头附近）⇒ 画面字幕上**永远只有中文**
   * （用户报：「勾着实时翻译，字幕却只有中文」）。
   *
   * ⇒ 队列深了就**停发** ⇒ 等它消化掉再继续（这就是**背压**）。
   *
   * ⚠️ 这个值离 ``QUEUE_MAX``（256）差得远 ⇒ 队列**永远碰不到上限**
   * ⇒ **不会丢任何一句**。
   * ------------------------------------------------------------------ */
  const TR_QUEUE_SOFT_MAX = 48;

  /**
   * **「要了但一直没回来」多久算过期**（毫秒）。
   *
   * ⚠️ **为什么需要**：请求发出去了，但**没收到译文帧** ⇒ 那一行**永远
   * 没有译文**。三种情况会这样：
   *
   * * 服务端**拒收**（``{"ok": false, "skipped": "翻译没开"}``）——
   *   开关的本地状态与服务端状态不一致的那几十毫秒；
   * * 服务端队列**满了**（``QUEUE_MAX``）⇒ 丢最旧的；
   * * 网络失败（fetch reject）。
   *
   * ⇒ 过期了就**重要一次**（见 ``_trRequestLines``）⇒ 那一行最终一定有
   *    译文（用户报：「勾着实时翻译，字幕却只有中文」）。
   *
   * ⚠️ 同时也是**退避**：服务端一直拒收时，不会每个 tick（600 ms）都重发
   * ⇒ 5 秒一次，够狠又不至于刷屏。
   */
  const TR_REQ_STALE_MS = 5000;

  /**
   * **同一行最多重试几次**（2026-11-26）。
   *
   * ⚠️ **为什么必须封顶**（实测用户那一轮的行级翻译日志）：重试是
   * **每 5 秒一次、无上限** ⇒ 一句**翻不出来**的字幕（模型复读指令 /
   * 复读原文）⇒ 被**无限重试** ⇒ 实测**同 7 句话**被重试了**几百次**
   * （「Translate the following text from zh to en:」**338** 次 /
   *  「己的概括能力。」**174** 次 / 「徐老师。」**158** 次…）⇒
   * ⇒ 每次都是**一次真推理** ⇒ **翻译模型的算力全烧在这 7 句上**
   * ⇒ ⇒ ASR/TTS 抢不到算力 ⇒ **译文越落越远** ⇒ 音频跟不上字幕。
   *
   * ⇒ 封顶：同一行重试 **3** 次就不再来（那行保留原文，显示为「没翻出来」）。
   * ⇒ **用户想再试**：把「实时翻译」关掉再打开 ⇒ 记账清零 ⇒ 重新要一遍
   * （见 ``_trLineReq.clear()``）。
   *
   * ⚠️ 服务端那边还有**第二道闸**（负缓存，同一句 300 s 内不烧推理）⇒
   * 两道闸都在 ⇒ 前端的重试**不会**变成算力消耗。
   */
  const TR_REQ_MAX_ATTEMPTS = 3;

  /* ASR 报出来的**语种名**（英文名，如 "English" / "Chinese"）⇒ 语言码
     ⇒ 再走同一份 ``TR_LANG_LABELS``（跟三个语言下拉同款排法）。

     ⚠️ 标签表里的 ``native`` 是「**本族名**」（简体中文 / 日本語…）⇒ 不能
     拿它反查英文名 ⇒ 这里单列一张「英文名 → 码」的表。 */
  const TR_LANG_BY_EN = {
    "english": "en", "chinese": "zh-CN", "mandarin": "zh-CN",
    "mandarin chinese": "zh-CN", "japanese": "ja", "korean": "ko",
    "french": "fr", "german": "de", "spanish": "es", "portuguese": "pt",
    "italian": "it", "russian": "ru", "arabic": "ar", "hebrew": "he",
    "hindi": "hi", "bengali": "bn", "thai": "th", "vietnamese": "vi",
    "indonesian": "id", "malay": "ms", "turkish": "tr", "polish": "pl",
    "dutch": "nl", "czech": "cs", "ukrainian": "uk", "tamil": "ta",
    "telugu": "te", "urdu": "ur", "gujarati": "gu", "marathi": "mr",
    "persian": "fa", "tagalog": "tl", "khmer": "km", "myanmar": "my",
    "burmese": "my", "tibetan": "bo", "kazakh": "kk", "mongolian": "mn",
    "uyghur": "ug", "nepali": "ne", "swahili": "sw",
  };

  /* ------------------------------------------------------------------ *
   * 翻译目标语言：**中文名 + 本族名**（跟右上角「简体中文」下拉一个排法）
   *
   * ⚠️ 表里的 key = 后端 provider 的语言码（``LANG_CODES``），不是通用
   * 的 ISO 码 —— 「模型支持哪些语言」和「语言叫什么」是两件事。
   *
   * ⚠️ 表里**没有**的码（将来模型加了新语言）⇒ 前端退回英文名
   * （``it.name``），不会显示成空白。
   * ------------------------------------------------------------------ */
  const TR_LANG_LABELS = {
    "zh-CN": { zh: "中文（简体）", native: "简体中文" },
    "en":     { zh: "英语",       native: "English" },
    "ja":     { zh: "日语",       native: "日本語" },
    "ko":     { zh: "韩语",       native: "한국어" },
    "fr":     { zh: "法语",       native: "Français" },
    "de":     { zh: "德语",       native: "Deutsch" },
    "es":     { zh: "西班牙语",   native: "Español" },
    "pt":     { zh: "葡萄牙语",   native: "Português" },
    "it":     { zh: "意大利语",   native: "Italiano" },
    "ru":     { zh: "俄语",       native: "Русский" },
    "ar":     { zh: "阿拉伯语",   native: "العربية" },
    "hi":     { zh: "印地语",     native: "हिन्दी" },
    "th":     { zh: "泰语",       native: "ไทย" },
    "vi":     { zh: "越南语",     native: "Tiếng Việt" },
    "id":     { zh: "印尼语",     native: "Bahasa Indonesia" },
    "ms":     { zh: "马来语",     native: "Bahasa Melayu" },
    "tr":     { zh: "土耳其语",   native: "Türkçe" },
    "pl":     { zh: "波兰语",     native: "Polski" },
    "nl":     { zh: "荷兰语",     native: "Nederlands" },
    "cs":     { zh: "捷克语",     native: "Čeština" },
    "he":     { zh: "希伯来语",   native: "עברית" },
    "uk":     { zh: "乌克兰语",   native: "Українська" },
    "tl":     { zh: "塔加洛语",   native: "Tagalog" },
    "km":     { zh: "高棉语",     native: "ភាសាខ្មែរ" },
    "my":     { zh: "缅甸语",     native: "မြန်မာဘာသာ" },
    "fa":     { zh: "波斯语",     native: "فارسی" },
    "gu":     { zh: "古吉拉特语", native: "ગુજરાતી" },
    "ur":     { zh: "乌尔都语",   native: "اردو" },
    "te":     { zh: "泰卢固语",   native: "తెలుగు" },
    "mr":     { zh: "马拉地语",   native: "मराठी" },
    "bn":     { zh: "孟加拉语",   native: "বাংলা" },
    "ta":     { zh: "泰米尔语",   native: "தமிழ்" },
    "bo":     { zh: "藏语",       native: "བོད་ཡིག" },
    "kk":     { zh: "哈萨克语",   native: "Қазақша" },
    "mn":     { zh: "蒙古语",     native: "Монгол" },
    "ug":     { zh: "维吾尔语",   native: "ئۇيغۇرچە" },
  };

  /* ------------------------------------------------------------------ *
   * 「这段文字板端**渲染得出来**吗」
   *
   * ⚠️ **为什么需要它**：板端字体覆盖有限 —— 泰语 / 天城文（印地语）/
   * 藏文 / 高棉文 / 缅甸文 / 古吉拉特文 / 泰卢固文 / 泰米尔文 /
   * 孟加拉文 **都没有字体** ⇒ 那些本族文字会渲染成**豆腐块**（□）。
   * （右上角「简体中文」下拉里的「印地语 / 泰语」现在就是这样 —— 实测
   * 截过图：हिन्दी 和 ไทย 都是一排方框。）
   *
   * 判据：把字符画到 canvas 上，跟「**一定渲染不出来**的字符」
   * （U+FFFF，非字符位 ⇒ 所有字体都只能给 .notdef）逐像素比 —— 一模
   * 一样就是豆腐块。
   *
   * ⚠️ 不用「量宽度」：豆腐块和正常字符可能同宽（都是方块）⇒ 量宽度
   * 判不出来。**必须比像素**。
   *
   * ⚠️ **判据只有一份** —— 宿主框架出了 ``/static/core/font-check.js``
   * （``window.WdnFontCheck.canRender``），右上角「界面语言」下拉用的
   * 就是它 ⇒ 同一套判据在两个仓里各写一份的话，迟早漂移 ⇒ 这里**优先用
   * 框架的**，框架没有（老宿主 / 单测环境）才退回自己这份（``_canRenderLocal``）。
   * ------------------------------------------------------------------ */
  const _renderableCache = new Map();
  let _renderCanvas = null;

  function _canRenderLocal(text) {
    if (!text) return false;
    if (_renderableCache.has(text)) return _renderableCache.get(text);
    let ok = true;
    try {
      if (!_renderCanvas) {
        _renderCanvas = document.createElement("canvas");
        _renderCanvas.width = 40;
        _renderCanvas.height = 40;
      }
      const ctx = _renderCanvas.getContext("2d");
      if (!ctx) throw new Error("no 2d context");
      const cs = getComputedStyle(document.body);
      const font = (cs.fontSize || "16px") + " " + (cs.fontFamily || "sans-serif");
      const draw = (ch) => {
        ctx.clearRect(0, 0, 40, 40);
        ctx.fillStyle = "#000";
        ctx.textBaseline = "top";
        ctx.font = font;
        ctx.fillText(ch, 0, 0);
        return ctx.getImageData(0, 0, 40, 40).data;
      };
      const notdef = draw("\uFFFF");
      for (const ch of text) {
        if (ch === " " || ch === "\u00a0") continue;
        const px = draw(ch);
        let same = true;
        for (let i = 3; i < px.length; i += 4) {
          if (px[i] !== notdef[i]) { same = false; break; }
        }
        if (same) { ok = false; break; }
      }
    } catch (e) {
      // canvas 不可用（老浏览器 / 隐私模式）⇒ 保守按「能渲染」处理
      ok = true;
    }
    _renderableCache.set(text, ok);
    return ok;
  }

  /**
   * 优先用**宿主框架**的判据（``/static/core/font-check.js`` 的
   * ``window.WdnFontCheck.canRender``），没有才退回自己这份。
   *
   * ⚠️ **判据只有一份** —— 右上角「界面语言」下拉用的就是框架那份 ⇒
   * 同一套判据在两个仓里各写一份的话，迟早漂移（板子换板 / 字体装没装
   * 的结论就不一致了）。
   */
  function _canRender(text) {
    const fc = window.WdnFontCheck;
    if (fc && typeof fc.canRender === "function") return fc.canRender(text);
    return _canRenderLocal(text);
  }


  /* ------------------------------------------------------------------ *
   * 「拖动进度条 → 识别跟过去」的三个参数
   * ------------------------------------------------------------------ */

  //: 往回找多少秒作为**引导**（lead-in）。
  //:
  //: 新会话的头 4 轮**不允许提交**（SDK ``asr.py:1067`` 的
  //: ``chunk_idx < rollback_rounds``）→ 从拖动点**正好**开始识别的话，
  //: 前 4 秒是模型的纯猜测，committed 恒为空 → 字幕在那里是空的。
  //: 往前多带 8 秒，模型就有上下文了（不会「零上下文 + 半个句子」→
  //: 语法不通的碎片），而且这 8 秒正好把热身期吃掉 → 拖动点本身就有字幕。
  const SUB_SEEK_LEAD_IN_SEC = 8.0;

  //: 判定「这个位置识别过了」的**余量**（秒）。
  //:
  //: 已确认文本比音频**滞后** 4~5 轮（回退策略）→ 识别区间虽然到了
  //: 某个时间，字幕（committed 派生的）其实还差几秒。留 2 秒余量，
  //: 免得在**边界**上反复重开识别。
  const SUB_SEEK_COVER_MARGIN_SEC = 4.0;

  //: 两次「为拖动重开识别」之间的**静默期**（毫秒）—— 防抖。
  const SUB_SEEK_DEBOUNCE_MS = 1500;

  //: 判定「播放头这一刻**有没有字幕可显示**」的两端余量（秒）。
  //:
  //: ⚠️ 这跟 :data:`SUB_SEEK_COVER_MARGIN_SEC` 是**两件事**：
  //:
  //: * ``SUB_SEEK_COVER_MARGIN_SEC`` —— 「拖动要不要**重开识别**」：
  //:   宁可多重开一次，也不能让用户停在没有字幕的地方。
  //: * 这里 —— 「屏幕上**能不能留字幕**」：播放头落在**还没识别过**的
  //:   音频上时，画面上留着的必然是**拖动前**那一行（旧字幕不更新）⇒
  //:   必须清掉。
  //:
  //: 头端留 0.2 秒（区间起点比播放头晚一点点）、尾端留 1.5 秒
  //: （识别前沿比播放头快好几倍，只有刚开识别的那一瞬会贴脸）。
  const SUB_SHOW_RANGE_HEAD_SEC = 0.2;
  const SUB_SHOW_RANGE_TAIL_SEC = 1.5;

  /** CJK（含日文假名 / 韩文）—— 逐字点亮，不按空格切。 */
  const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]/;

  /** 句末标点：断句。 */
  const SUB_HARD_END = /[.!?。！？…]["'”’)\]】」』]*$/;
  /** 句中标点：够长就断句。 */
  const SUB_SOFT_END = /[,;:，；、、]$/;

  /* ------------------------------------------------------------------ *
   * 节奏字幕的两个小工具
   * ------------------------------------------------------------------ */

  /**
   * 最长公共前缀长度（按 UTF-16 码元比）。
   *
   * 用来回答：「这一轮模型**猜**的文本，和最终确认下来的文本，前缀
   * 能对上多少字？」—— 也就是「到此刻为止，最终字幕里已经被说出来
   * 多少字」。见 ``_epochTable``。
   */
  function _lcp(a, b) {
    const n = Math.min(a.length, b.length);
    let i = 0;
    while (i < n && a[i] === b[i]) i += 1;
    // 别把一个代理对劈成两半（emoji / 少数字的扩展汉字）。
    if (i > 0 && i < n) {
      const c = a.charCodeAt(i - 1);
      if (c >= 0xd800 && c <= 0xdbff) i -= 1;
    }
    return i;
  }

  /**
   * 把文本切成「词」。
   *
   * 拉丁文按空格切；CJK 逐字切（中文没有空格，逐字点亮才对得上
   * 说话的节奏）。词里**带上后面的空白**，渲染时原样拼回去 —— 不用
   * 自己决定词间要不要补空格，中英混排也就不会串版。
   */
  function _splitWords(text) {
    const out = [];
    const n = text.length;
    let i = 0;
    while (i < n) {
      if (/\s/.test(text[i])) { i += 1; continue; }
      const start = i;
      if (CJK_RE.test(text[i])) {
        i += 1;
      } else {
        while (i < n && !/\s/.test(text[i]) && !CJK_RE.test(text[i])) i += 1;
      }
      // 带上尾随空白（渲染时原样输出）
      while (i < n && /\s/.test(text[i])) i += 1;
      out.push({ text: text.slice(start, i), trim: text.slice(start, i).trim(), i0: start, i1: i });
    }
    return out;
  }

  /* ------------------------------------------------------------------ *
   * 与 Python 对齐的两个小函数
   *
   * numpy 那边用的是 ``round()``（**银行家舍入**，半数取偶）和
   * ``//``（向下取整除法）。JS 的 ``Math.round`` 是「半数向上」，
   * 直接用会在极少数采样上差 1 → 两端 PCM 不再逐字节相同。
   * ------------------------------------------------------------------ */

  /** 银行家舍入（Python ``round(float)`` 的语义）。 */
  function _pyRoundToEven(x) {
    const f = Math.floor(x);
    const diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }

  /** 向下取整除法（numpy 的 ``//``；这里只用于非负分子）。 */
  function _pyFloorDiv(a, b) {
    const mod = a % b;
    return Math.floor((a - mod) / b);
  }

  /** float32 数组 → s16le 字节（向零截断，与 numpy ``astype("<i2")`` 一致）。 */
  function _quantizeS16(values) {
    const out = new Uint8Array(values.length * 2);
    const view = new DataView(out.buffer);
    for (let i = 0; i < values.length; i++) {
      let v = values[i];
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      v = Math.trunc(v);
      view.setInt16(i * 2, v, true);
    }
    return out;
  }

/**
 * Qwen3-ASR 流式语音识别 —— 前端。
 *
 * 两种显示模式吃的是**同一份**数据（每轮的 committed / unfixed），
 * 跟 C demo 的两种输出方式一一对应：
 *
 *   UI「实时结果」  ←→  内部 single  ←→  rknn_qwen3_asr_demo_online ... -s
 *                 一行里：已确认文本（黑）+ 未确认尾巴（蓝），整行原地刷新
 *   UI「识别过程」  ←→  内部 rounds  ←→  rknn_qwen3_asr_demo_online ...
 *                 每轮一行：该轮确认的增量 + 该轮未确认的尾巴
 *
 * ⚠️ **UI 文案用大白话**（「实时结果」/「识别过程」），内部注释与
 * C demo 的说法（实时单行 / 逐轮明细）保持原样 —— 后者是机制术语，
 * 写在这里是为了对得上 C demo 的两种输出方式。用户看不懂「未确认尾巴」
 * 这种词，所以界面上不出现。
 *
 * 两种音频来源：
 *
 *   本机上传    → POST /transcribe 的 multipart `file`
 *   设备端路径  → POST /transcribe 的 form `path`（向导「浏览…」选出来的）
 *
 * 两种**识别模式**（互不干扰）：
 *
 *   文件 / 视频 → 一次性送整段，服务端按 1 秒一块增量解码
 *   实时麦克风  → 浏览器采集 → 逐块上行 → 服务端增量解码
 *
 * WS 帧：
 *   snapshot / state / round / done / error
 *   round 帧的 committed 是**累计**的已确认文本，unfixed 是本轮未确认的尾巴。
 *   实时模式另有 gap（音频丢失）/ endpoint（一句话结束）/ live_done。
 */
const Qwen3Asr = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  rounds: [],
  // 当前 rounds 是**哪一次识别**产生的（file / video / board / browser）。
  //
  // 为什么必须记：视频字幕是按播放头去 rounds 里取轮的，而 rounds 在
  // 「切换来源」时**不会**自动清 —— 于是切到「含音轨的视频」后，字幕会
  // 拿**上一来源**（比如「音频文件」）的轮次来渲染。实测：音频文件识别
  // 完再切到视频，画面下方凭空多出一句「嗯哼。」—— 而那是音频文件
  // 第 1 轮那个**会被回滚**的瞬时猜测，视频当时根本还没开始识别。
  //
  // 有了这个标记，字幕只在「轮次确实来自视频这次识别」时才渲染。
  _roundsSource: "",
  // 轮次「代」计数器 —— 每次发起新识别 / 清空就 +1。
  // 用途：刷新恢复（``_restoreFromServer``）拉轮次的过程中，用户要是
  // 又点了「开始识别」→ 代数变了 → **丢弃这次恢复**（旧轮次不能盖掉
  // 新识别刚起的轮次 = 串台）。
  _roundGen: 0,
  // 「停止」是**本页面**发起的吗？—— 用来区分两种「stopped」帧：
  //   * 自己点的停止 → 画面在 click 的那一刻就冻住了（见 ``stop()``），
  //     1 秒后到达的 stopped 帧**不该**再冻一次（那时用户可能已经
  //     自己按了播放）；
  //   * 别的页面 / 别的标签页发的停止 → 本页面没冻过 → 要冻。
  _stopInitiated: false,
  // 「清空」之后忽略迟到的轮次帧（清空前广播出去的那几帧）。
  // 新一轮识别开始时归零。
  _ignoreRounds: false,
  _file: null,
  _path: "",
  _starting: false,
  _source: "file",        // file / video / board / browser
  _boardState: null,      // 板载采集进程状态（后端返回）

  /* ---------------- 视频播放（含音轨的视频来源） ---------------- */
  _fileBlobUrl: "",        // 本机上传的 blob URL（要 revoke）
  _videoSrc: "",          // 当前 <video> 的 src（避免重复 load）
  _audioOriginSec: 0,     // 音轨在容器里的起点（后端给）

  /* ---------------- 节奏字幕（YouTube 式） ---------------- */
  _subEpochCache: [],     // 每个 epoch 的行缓存（key = 该 epoch 的轮数）
  _subTl: null,          // 整条时间轴的缓存
  _subTlKey: -1,         // 时间轴缓存对应的轮次数
  _subSlots: [],          // 常驻的行槽（3 个），便于做淡出/淡入
  _subSig: "",            // 上次渲染的签名 —— 没变就不碰 DOM
  _subRaf: 0,             // 逐帧刷新字幕的 rAF 句柄
  // 已识别的**音频时间区间**（秒，音轨相对起点）。拖动进度条时靠它判断
  // 「这个位置识别过没有」—— 没识别过就要把识别**开过去**（见
  // ``_maybeFollowSeek``）。
  _subRanges: [],
  _seekBusy: false,       // 正在为拖动重开识别（别重复发起）
  _seekGuardUntil: 0,     // 重开后的静默期（毫秒时间戳）
  _seekTarget: -1,        // 最近一次请求识别的位置（去抖）
  _seekPending: null,     // 重开**过程中**用户又拖到的位置（排队跟上）

  /* ---------------- 「识别过程」面板（逐轮明细） ----------------
   * 面板是**追加**的：每来一轮就多一行。老实现每帧重建整张表 →
   * 几千轮的片子里每帧几十毫秒，把浏览器主线程占死（识别都被拖慢）。
   * 现在：面板隐藏时不重建；可见时只补新增的行。 */
  _roundsShown: 0,        // DOM 里已经画到第几轮
  _roundsPrev: "",        // 上一轮的有效 committed（算本轮增量用）
  _roundsDirty: false,    // DOM 跟 ``this.rounds`` 对不上了（要整表重建）

  /* ---------------- 字幕同款转写框（视频来源下的「文字显示框」） ----------------
   * 画面字幕只留当前那几行；文字框留**整条时间轴**，可上拉回看。
   * 两者共用同一份时间轴 + 同一个词状态算法（``_revealState``）——
   * 「显示效果一样」必须是同一份代码算出来的，否则两边迟早对不上。 */
  _trRows: [],            // 转写框的行元素（与时间轴同序）
  _trTl: null,            // 已同步的那份时间轴（对象同一性 → 该重排了）
  _trCur: -1,             // 当前行下标（按播放头）
  _trPassVer: -1,         // 上次「整表刷译文」时的译文版本号（≠ ⇒ 该刷了）
  _trFollow: true,        // 是否跟随播放头滚动（用户上翻时暂停跟随）
  _trAutoAt: 0,           // 上次自动滚动的时间戳（用来分辨「谁滚的」）

  /* ---------------- 实时识别状态 ---------------- */
  _live: {
    active: false,
    starting: false,
    streamId: "",
    ctx: null,
    stream: null,
    node: null,
    source: null,
    seq: 0,
    queue: [],
    sending: false,
    /**
     * 会话代号 —— 每次「开始」/「中止」+1。
     *
     * 用来分辨「这个响应属于哪一轮」：上一轮的在途请求返回时（哪怕是
     * 404/409），**不许**再去中止当前这一轮 —— 否则一次「中止 → 重开」
     * 就会把刚开好的新会话杀掉（实测：按钮卡在「结束实时识别」且禁用，
     * 再也点不动）。
     */
    gen: 0,
    dropped: 0,        // 客户端丢掉的块（L1）
    serverDropped: 0,  // 服务端丢掉的块（L3）
    buf: null,         // 攒块缓冲（Uint8Array）
    resampler: null,   // 整块线性插值器的状态（与板载同算法）
    segments: [],      // 已完成的句子（端点切分）
    committed: "",
    unfixed: "",
    language: "",
  },

  /* ---------------- 同声传译状态（2026-11-21） ---------------- */
  _interp: {
    running: false,      // 同传引擎在跑吗
    mode: "steady",      // 切句档位：fast | steady
    gate: "now",         // 播放时机：now | follow
    targetLang: "zh-CN", // 目标语言（provider 的语言 key）
    speaker: "",         // 内置音色（SDK 的 SPK_ID）
    refSpeaker: "",      // 参考音色（voice-clone）—— 本板只有这类可用
    gateTouched: false,  // 用户**自己**选过播放时机吗（没选过才用默认）
    // ⚠️ **默认 0.05（5%）**（2026-11-25 用户定案，同日第二次调整）——
    // 同传的默认音量：1% 太小声（信号落到板载 codec 噪声底附近 ⇒ 听见的
    // 「杂音」主要是底噪），0.5 太吵 ⇒ 5% 是折中。
    // ⚠️ 想听清楚就**拉滑块**，别改默认值。
    volume: 0.05,
    source: "live",      // live | file —— 决定滞后口径
    tts: true,           // TTS 可用吗（不可用 ⇒ 降级为仅字幕）
    status: {},          // 最近一次后端状态
    loaded: false,       // 下拉选项拉过了
    phTimer: null,       // 播放头上报的定时器
    lastPh: -1,          // 上次上报的播放头（没变就不报）
    lastPlaying: null,
    lastEnded: false,    // 上次上报的「媒体已播完」
    lastFailed: false,   // 上次上报的「媒体出错」
    timer: null,         // 状态轮询的定时器
    streamId: "",        // 当前流 id（播放头按流上报）
    clientId: "",        // **标签页 id**（2026-11-25）—— 见 _interpClientId()
    phIgnored: false,   // 服务端说这个标签页的播放头**被忽略**了（不是发起方）
    audioSrc: "",        // 音频播放器的当前 src（换了才 reload）
    // 媒体**为什么没在放**（空 = 没有已知原因）——
    // ⚠️ 面板提示每秒被 _interpSyncUi() 刷一遍 ⇒ 光在 play() 的
    // catch 里写一句，下一秒就没了 ⇒ 用户只看到泛泛的提示，
    // 不知道**到底为什么没声音** ⇒ 记成状态。
    mediaBlocked: "",
    // ---- 向导里的选择（2026-11-25 新增）----
    // ⚠️ **换步会重建 body**（select 会被清空）⇒ 这些值必须记在这儿，
    //    否则用户在向导里选的朗读语言 / 音色会被无声地冲回默认值。
    wizardOn: false,      // 向导里勾了「开启同声传译」
    wizardLang: "",       // 向导里选的朗读语言（"" = 跟随翻译目标语言）
    wizardVoice: "",      // 向导里选的音色（"kind:id"，kind = ref | builtin）
    // ---- 同传的逐句结果（2026-11-25 新增）----
    // ⚠️ **Map 而不是数组** —— 查找要按 ``index``（后端给的句号）⇒ 数组
    //    查找是 O(n) ⇒ 长音频几千句时每来一帧都要扫一遍 ⇒ O(n²)。
    //    Map 既有 O(1) 查找，又**保序**（插入顺序 = 到达顺序）。
    segs: new Map(),      // index → {index, text, tr, lang, start, end, spoken, err}
    // ⚠️⚠️ **「当前正在播报的那一句」**（2026-11-25，用户要「显示当前正在
    // 播放的同传结果」）—— 由**播放头**算出（``_interpSyncCurrent``）⇒
    // 列表**跟字幕节奏走** ⇒ 高亮 + 滚动到它。
    curSeg: null,        // 当前那一句（对象）
    follow: true,        // 还跟着播放头滚吗（用户手动上翻 ⇒ false）
    autoAt: 0,           // 自己最后一次滚动的时刻（判「用户滚的」）
  },

  /* ---------------- 配音导出状态（2026-11-26） ---------------- */
  /**
   * 「音频文件」/「含音轨的视频」两个来源用**配音导出**（不再同传）。
   *
   * 产物：ASR → 翻译 → TTS ⇒ 一条目标语言的音轨；视频还会再烧一层
   * **双字幕**（原文 + 译文）并与新音轨重新封装。
   *
   * ⚠️ 文件名规则在**服务端**（``dub.py::_output_path``）—— 前端只负责
   * 把生成的文件列出来让用户挑着播。
   */
  _dub: {
    targetLang: "",       // 目标语言（provider 的语言 key）
    speaker: "",          // 内置音色（SDK 的 SPK_ID）
    refSpeaker: "",       // 参考音色（voice-clone）—— 本板只有这类可用
    running: false,       // 导出作业在跑吗
    state: "",            // 后端回的状态（idle/running/done/error/stopped）
    phase: "",            // 人话状态（「识别中」…）
    progress: 0,          // 0..1
    message: "",
    error: "",
    audioPath: "",        // 生成的目标语言音频文件
    videoPath: "",        // 生成的双字幕视频文件（视频来源才有）
    sourcePath: "",       // 作业的**原始**源路径（列产物用它，别用 _path）
    sourceLang: "",      // 识别出来的语种（如 "English"）—— 结果面板要标「源语言」
    hasRows: false,       // 导出结果面板**有内容**吗 ⇒ 藏掉重复的「实时结果」
    files: [],            // 源文件同级目录里已生成的产物 [{name, path, kind, url}]
    timer: null,          // 轮询定时器
    loaded: false,        // 语言 / 音色下拉拉过了
    langItems: null,
    voiceItems: null,
    wizardVoice: "",      // 向导里选的音色（"kind:id"）—— 换步会重建 body，得记住
  },

  /* ---------------- 实时翻译状态（流式同传） ---------------- */
  _tr: {
    enabled: false,      // 「实时翻译」开关
    targetLang: "en",    // 目标语言（provider 的语言 key）
    modelReady: false,   // 翻译模型在不在跑
    modelStatus: "",     // 人类可读的模型状态
    items: [],           // 已收到的译文 [{index, source, text, status, start_sec, end_sec, ...}]
    busy: false,         // 「启用翻译模型」进行中
    loaded: false,       // 语言列表拉过了
    langItems: null,     // 语言清单（运行时才有的，缓存一份）
    wizardTarget: "",    // 向导里选的目标语言（换步会重建 body，得记住）
    modelId: "",         // 翻译模型的 model_id（后端定的，进度条要用）
  },

  /* ---------------- 句子流状态（结果框按句排版） ---------------- */
  /**
   * 已成句的原文。
   *
   * ⚠️ **断句只有一份实现** —— 文件 / 视频路径的句子由**服务端**的
   * ``_SentenceSplitter`` 切，随 round 帧的 ``sentences`` 字段送来；
   * 麦克风路径由端点检测切。前端**不自己**断句 —— 自己断就是两份口径，
   * 迟早漂移。
   */
  _sentences: [],   // [{i: 句序号, t: 原文}]
  /**
   * 「已确认但还没成句」的尾巴（**服务端**算好送来，见
   * ``service.py::_sent_tail_text``）。
   *
   * ⚠️ **不让前端自己切** —— 服务端给的 ``committed`` 是**段内**累积文本
   * （换 epoch 就从头数）⇒ 前端拿偏移量去切，跨段就对不上。
   */
  _sentTail: "",
  /** 最近一轮的「未确认尾巴」—— 重画句子流时要用。 */
  _sentUnfixed: "",

  /**
   * 译文列表的**版本号** —— 每次改动 +1。
   *
   * 用来让「按时间挂到字幕行上」的缓存（见 ``_trTimedSorted``）失效重建。
   */
  _trVer: 0,
  _trTimedCache: null,
  _trTimedVer: -1,

  /**
   * **翻译的单位**：``"sentence"``（后端按标点断句）还是 ``"line"``
   * （前端逐行来要译文）。
   *
   * ⚠️ 视频来源必须是 ``"line"`` —— 字幕的**显示单位是「行」**，译文必须
   * **逐行**跟着（YouTube 的 cue 对 cue）。句子级做不到：一句跨 3 行时
   * 译文只挂得上最后那行，中间两行光秃秃，而且译文永远慢半拍（实测
   * 字幕行 ~1.5 s 一行、译文 3~9 s 一句 ⇒ 看着像「不是实时翻译」）。
   */
  _trMode: "sentence",
  /** 行级翻译：给每行编的序号（前端自己编，后端原样带回）。 */
  _trLineSeq: 0,
  /**
   * 槽位号 → 已经要过的**文本** + **什么时候要的**。
   *
   * ⚠️ 带上时间戳是为了能判断「要了但**一直没回来**」⇒ 过期后**重要
   * 一次**（否则那一行**永远没有译文** —— 见 :data:`TR_REQ_STALE_MS`）。
   */
  _trLineReq: new Map(),
  /**
   * **全局翻译请求的闸门**（时间戳，毫秒）。
   *
   * ⚠️ **为什么要有**：``_trRequestLines`` 有**两个**调用者 ——
   * 「节拍器」（每 600 ms）和「**round 帧驱动**」（**每来一轮识别就调
   * 一次**）。识别是实时的 ⇒ 一集 98 分钟的访谈 ≈ **3.8 轮/秒** ⇒ 每轮
   * 都发 ``TR_REQ_BATCH``（2）个 ⇒ **≈7.6 个/秒** + 节拍器的 3.3 ⇒
   * **≈10 个/秒**。
   *
   * ⚠️ 而一台翻译实例只吃 **≈2.7~3.8 句/秒** ⇒ 队列**必然积压** ⇒ 满了
   * **丢最旧的** ⇒ 丢的正是**先要的那几行**（= 播放头附近）⇒ 画面字幕上
   * **永远只有中文**（用户报：「勾着实时翻译，字幕却只有中文」）。
   *
   * ⇒ 闸门放在**函数内部**（不是放在调用者里）⇒ **不管谁调**，600 ms 最多
   *    发 2 个 ⇒ 速率封顶 ≈ 3.3 个/秒。
   */
  _trReqGateAt: 0,
  /**
   * 服务端翻译队列的深度（句数）—— **背压**用。
   *
   * ⚠️ 来源：每次 ``POST /translate/line`` 的响应里带着（服务端入队后
   *    的实时深度）+ ``GET /translate/status`` 的轮询值。
   */
  _trQueueDepth: 0,
  /**
   * **本地刚改过、还没被服务端确认**的翻译设置。
   *
   * ⚠️ 用来防「5 秒一次的轮询把用户刚点的开关**撤销**」—— 详见
   * :meth:`_trRefresh` 的说明。
   */
  _trPending: null,
  /** 行序号 → 槽位号（译文回来时据此对回是哪一行）。 */
  _trLineIdBySeq: new Map(),
  /** 槽位号 → 这一行当前的请求 {seq, item, text}。 */
  _trLineSlot: new Map(),
  /** 槽位号 → 译文（**按槽位对**，不按行 id —— 文本改了行 id 就变了）。 */
  _trLineTr: new Map(),
  /**
   * 句子级（音频文件来源）：**已经要过的句子文本**。
   *
   * ⚠️ 句子级翻译是**服务端在句子切出来那一刻一次性送出去**的 ⇒ 开关关着
   * 时切出来的句子**永远不会被补翻** ⇒ 用户「取消勾选 → 再勾选」之后，
   * 结果框里的那些句子**永远没有译文**。
   *
   * ⇒ 重新打开开关时，前端用这个表找出「还没翻过的句子」，再逐句要一次
   * （走同一个 ``/translate/line`` 端点 ⇒ 译文照样从 WS 回来）。
   *
   * ⚠️ 值 = ``{text, at}`` —— 带上时间戳才能判断「要了但一直没回来」
   * ⇒ 过期后重要一次（见 :data:`TR_REQ_STALE_MS`）。
   */
  _trSentReq: new Map(),
  /** 句子级翻译的请求序号（前端自己编，后端原样带回）。 */
  _trSentSeq: 0,

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);

    $("asr-file")?.addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      this._file = f || null;
      if (f) this._path = "";          // 两者互斥：选了本机文件就不再用路径
      const nameEl = $("asr-file-name");
      // 没选文件时**留空**：浏览器自带的 file input 已经显示了
      // 「未选择任何文件」，再补一句「未选择文件」是同一句话说两遍。
      if (nameEl) nameEl.textContent = f ? f.name : "";
      this._setFileBlob(f);            // 视频来源要用 blob URL 播
      this._syncSourceUi();
      this._syncVideoPlayer();
      this._syncAudioPlayer();          // 音频文件源（2026-11-21）
    });

    $("asr-source")?.addEventListener("input", (e) => {
      this._path = (e.target.value || "").trim();
      if (this._path) this._file = null;   // 反向：填了路径就不再用本机文件
      this._setFileBlob(null);              // 改成设备端路径 → 释放 blob
      // ⚠️ 路径**后缀**决定来源类型（视频 / 音频）⇒ 页签跟着走。
      //   不跟着走的话：用户填了 .mp4、页签还停在「音频文件」⇒ 前端按
      //   音频播、后端按视频记 ⇒ 播放器不出现，两边口径也对不上。
      this._applyKindFromPath(this._path);
      this._syncAudioPlayer();          // 音频文件源（2026-11-21）
    });

    // 设备端文件浏览（与 vtl / vd 共用 FSModal）
    $("asr-browse")?.addEventListener("click", () => {
      const target = $("asr-source");
      try {
        window.VdFsModal._sourceTarget = target;
        window.VdFsModal.open("");
      } catch (e) {
        console.warn("[qwen3-asr] browse failed:", e);
      }
    });

    $("asr-start-btn")?.addEventListener("click", () => this.start());
    $("asr-stop-btn")?.addEventListener("click", () => this.stop());
    $("asr-reset-btn")?.addEventListener("click", () => this.reset());

    // 向导框架的应用页契约（wizard.json 的 open_source_button + "-open"
    // → "-close" 约定）—— 卡片页上这两个按钮是隐藏的，点了的是向导里的
    // 「预览播放 / 关闭」。
    $("asr-open")?.addEventListener("click", () => this._previewOpen());
    $("asr-close")?.addEventListener("click", () => this._previewClose());
    $("asr-mode")?.addEventListener("change", () => this._applyMode());
    $("asr-mic-btn")?.addEventListener("click", () => this._liveToggle());
    $("asr-mic-stop-btn")?.addEventListener("click", () => this._liveAbort());
    $("asr-board-start")?.addEventListener("click", () => this._boardStart());
    $("asr-board-stop")?.addEventListener("click", () => this._boardStop());

    // 实时翻译（流式同传）—— 开关 / 目标语言 / 启用翻译模型
    $("asr-tr-enabled")?.addEventListener("change", (e) => {
      const on = !!e.target.checked;
      // ⚠️ **先本地置位，别等服务端往返** ——
      //
      // ``_trSyncUi()`` 会把勾选框按 ``this._tr.enabled`` **回写**，而它在
      // **每一帧译文到达时**都会被调（见 WS 的 ``translation`` 分支）⇒ 服务端
      // 往返的那几十毫秒里，只要来一帧译文，就会用**旧值**（还是开着的）把用户
      // 刚点的「取消」**悄悄勾回去** ⇒ 表现为「关不掉 / 再打开也没反应」。
      //
      // ⇒ 先把本地状态改成用户点的那个值 ⇒ 回写时两边一致 ⇒ 点不动不了。
      // （服务端那边失败时，``_trRefresh`` 会用权威值改回来。）
      this._tr.enabled = on;
      this._trSyncUi();
      if (!on) {
        // ⚠️ **关掉 ⇒ 画面字幕上的译文收掉，但**数据留着** ——
        //
        // 以前是把译文**删掉**（``_tr.items`` / ``_trLineTr`` 清空）⇒
        // 再勾选回来时**每一行都要重新翻一遍** ⇒ 一集 98 分钟 ≈ 1500 行
        // ⇒ 按每轮 2 个的限速，要等 **40 秒以上**才轮得到播放头附近那几行
        // ⇒ 用户看到的就是「**勾着实时翻译，字幕却只有中文**」
        // （用户报：「有概率出现选中了实时翻译但还是……只有中文」）。
        //
        // ⇒ 改成**只隐藏、不删除**：译文还在（重新勾上**立刻**就有），
        //    显示由 ``_tr.enabled`` 统一管（见 ``_trItemsForLine`` /
        //    ``_renderSentences``）。
        this._trVer++;   // 版本号 +1 ⇒ 缓存失效
        this._trRender();
        this._renderVideoSubtitle();
        this._renderTranscript();
      } else {
        // ⚠️⚠️ **重新打开 ⇒ 重试次数清零**（2026-11-26）——
        //
        // 之前翻不出来的那几行（模型复读指令 / 复读原文）已经被
        // :data:`TR_REQ_MAX_ATTEMPTS` 封顶 ⇒ 不再自动重试 ⇒
        // ⇒ 用户手动「关掉再打开」= **显式地说「再试一遍」** ⇒
        // ⇒ 给它们**再试一次**的机会（译文还在，只是重试次数归零）。
        this._trLineReq.forEach((req, slot) => {
          this._trLineReq.set(slot, { text: req.text, at: 0, tries: 1 });
        });
      }
      this._trSet({ enabled: on });
    });
    $("asr-tr-target")?.addEventListener("change", (e) => {
      // ⚠️ **换显示语言 ⇒ 旧译文作废** —— 那些译文是**上一种语言**的
      // （「中文→英语」的译文挂在字幕上，用户刚换成「日语」⇒ 字幕上还是
      // 英文）⇒ 清掉译文与记账 ⇒ 重新按新语言翻一遍。
      //
      // ⚠️ **这里只管「显示语言」** —— **不碰**「配音导出」的「导出语言」
      // （那是**产物**的语言 ⇒ 由用户**显式**决定 ⇒ 2026-11-27 起两者
      // 在 UI 上就分开了，不再同名也不再联动）。
      this._tr.items = [];
      this._trLineTr.clear();
      this._trLineReq.clear();
      this._trLineSlot.clear();
      this._trSentReq.clear();
      this._trVer++;
      this._trRender();
      this._renderVideoSubtitle();
      this._renderTranscript();
      this._trSet({ target_lang: e.target.value });
    });
    $("asr-tr-model-btn")?.addEventListener("click", () => this._trEnableModel());
    // 预览播放器的收尾守卫（向导卸载 / 离开卡片时停掉播放）
    this._bindPreviewGuards();

    // ---- 同声传译（2026-11-21） ------------------------------------- //
    $("asr-interp-start")?.addEventListener("click", () => this._interpStart());
    $("asr-interp-stop")?.addEventListener("click", () => this._interpStop());
    $("asr-interp-mode")?.addEventListener("change", (e) => {
      this._interp.mode = e.target.value;
      this._interpSettings({ mode: e.target.value });
    });
    $("asr-interp-gate")?.addEventListener("change", (e) => {
      this._interp.gate = e.target.value;
      this._interp.gateTouched = true;     // 用户选过了 ⇒ 不再用默认值
      this._interpSettings({ gate: e.target.value });
    });
    $("asr-interp-target")?.addEventListener("change", (e) => {
      this._interp.targetLang = e.target.value;
      this._interpSettings({ target_lang: e.target.value });
    });
    $("asr-interp-speaker")?.addEventListener("change", (e) => {
      // ⚠️ **音色分两类，必须分开传给后端**：
      // * ``kind === "ref"``     ⇒ **参考音色**（voice-clone）⇒ ``ref_speaker``；
      // * ``kind === "builtin"`` ⇒ **内置音色**（SDK 的 SPK_ID）⇒ ``speaker``。
      //
      // ⇒ 混用 ⇒ SDK 打印「not in SPK_ID, skipping」⇒ **没有音色条件**
      // ⇒ 模型每次自由发挥 ⇒ **每句话音色都不一样**（用户实测报的现象）。
      const id = e.target.value || "";
      const kind = e.target.dataset.kind === "builtin" ? "builtin" : "ref";
      this._interp.speaker = (kind === "builtin") ? id : "";
      this._interp.refSpeaker = (kind === "ref") ? id : "";
      this._interpSettings({
        speaker: this._interp.speaker,
        ref_speaker: this._interp.refSpeaker,
      });
    });
    $("asr-interp-volume")?.addEventListener("change", (e) => {
      // ⚠️ **别用 `|| 10` 这种写法** —— 音量 0（静音）是合法值，
      // 兜底值应该跟默认档一致（**0.01 = 1%**，2026-11-25 用户定案）。
      const v = Number(e.target.value);
      this._interp.volume = Number.isFinite(v) ? v / 100 : 0.05;
      this._interpSettings({ volume: this._interp.volume });
    });
    this._interpLoadOptions();
    this._interpSyncUi();

    // ---- 配音导出（2026-11-26）----
    // ⚠️ 语音生成卡的音色表在**别的卡**里 ⇒ 拉它的接口（跨卡 HTTP 是
    //    允许的；只是不能 import 它的代码）。
    $("asr-dub-start")?.addEventListener("click", () => this._dubStart());
    $("asr-dub-stop")?.addEventListener("click", () => this._dubStop());
    $("asr-dub-files")?.addEventListener("change", () => this._dubSyncPlayer());
    this._dubLoadOptions();
    this._dubSyncButtons();
    // ⚠️ **拉一次上次的作业** —— 刷新页面后也能看到上次的导出结果
    //    （逐句原文 / 译文 + 两种语言）。
    this._dubRefreshStatus();
    // ⚠️ **刷新页面后要把「同传还在跑」接回来**（2026-11-25）——
    //
    // 症状（实测）：用户在同传跑着的时候刷新了页面 ⇒ 前端的
    // ``_interp.running`` 回到 false ⇒ **播放头上报的定时器根本没起来**
    // ⇒ 服务端**再也收不到播放头** ⇒
    //   ① 队列的播放时钟拿不到播放头 ⇒ 译文**不等字幕位置**（不同步）；
    //   ② 服务端**看不见拖进度条** ⇒ seek 处理（丢陈旧 + 重播）全部失效。
    //
    // ⇒ 这里问服务端「同传还在跑吗」⇒ 在跑就**接管**：把状态、轮询、
    //    播放头上报都接回来。
    this._interpRestoreState();

    // 输入来源切换（音频文件 / 含音轨的视频 / 板载麦克风 / 浏览器麦克风）
    document.querySelectorAll("#asr-source-tabs .asr-source-tab")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this._switchSource(btn.dataset.source || "file");
        });
      });

    // 视频播放头 → 字幕跟着画面走
    const video = $("asr-video");
    if (video) {
      video.addEventListener("timeupdate", () => this._renderVideoSubtitle());
      video.addEventListener("loadedmetadata", () => this._renderVideoSubtitle());
      // ⚠️ 拖动进度条 → 字幕要跟到**新位置**。而那个位置很可能**还没识别**
      // （服务端是从头顺序识别的）→ 见 ``_maybeFollowSeek``。
      video.addEventListener("seeked", () => {
        this._renderVideoSubtitle();
        this._maybeFollowSeek();
      });
      // ⚠️ ``timeupdate`` 只有 4Hz —— 撑不起「逐词点亮」，高亮会一跳一跳。
      // 播放期间挂一个 rAF 循环，每帧算一次；DOM 只在**签名变了**时才动。
      video.addEventListener("play", () => this._startSubLoop());
      video.addEventListener("pause", () => this._stopSubLoop());
      video.addEventListener("ended", () => this._stopSubLoop());
    }

    // 转写框的滚动：用户上翻回看时**暂停跟随**播放头，滚回来再恢复。
    const trBody = $("asr-transcript-body");
    if (trBody) {
      trBody.addEventListener("scroll", () => this._onTranscriptScroll());
    }

    // ⚠️ **同传结果列表的滚动**（2026-11-25）—— 同理：用户上翻回看 ⇒
    // 暂停跟随播放头（别把人拽走）⇒ 滚回来再恢复。
    const interpList = $("asr-interp-list");
    if (interpList) {
      interpList.addEventListener("scroll", () => this._onInterpScroll());
    }

    this._bindLiveGuards();
    this._applyMode();
    this._switchSource(this._source || "file");
    this.ensureWs();
    // 刷新恢复：把服务端的轮次 + 归属拉回来（转写框 / 逐轮明细 /
    // 来源页签）。**异步** —— 不阻塞首屏，WS 照常先连上。
    this._restoreFromServer();
    this._probeCap().catch(() => {});
    // 实时翻译：语言列表 + 状态（异步，不阻塞首屏）
    this._trLoadLanguages();
    this._trRefresh();
    if (!this._capTimer) {
      this._capTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        this._probeCap().catch(() => {});
        this._trRefresh();
      }, 5000);
    }
    // ⚠️ **翻译请求的节拍器** ——
    //
    // 「实时翻译」开着时，每 ``TR_REQ_TICK_MS`` 跑一轮：把**还没翻**的行
    // （视频）或句（音频文件）要过来，每轮最多 ``TR_REQ_BATCH`` 个 ⇒ 服务端
    // 的翻译队列（32）永远不满 ⇒ **不会丢请求**。
    //
    // ⚠️ **为什么不能只靠 round 帧驱动**：识别结束之后**没有 round 帧了**
    // ⇒ 「再勾选实时翻译」时那几百行永远不会被要 ⇒ 画面字幕上一直没有译文
    // （用户报：「再勾选实时翻译，字幕上没有实时翻译」）。
    if (!this._trTimer) {
      this._trTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        if (!this._tr.enabled) return;
        this._trRequestLines(false);
        this._trRequestSentences();
      }, TR_REQ_TICK_MS);
    }
    this._installLiveHooks();
  },

  /* ------------------------------------------------------------------ *
   * 输入来源切换
   *
   * 四种来源：
   *   file    音频文件（本机上传 / 设备端路径）
   *   video   含音轨的视频（同一套控件，accept 换成视频）
   *   board   板载麦克风（设备端 ALSA，独立进程采集）
   *   browser 浏览器麦克风（getUserMedia）
   * ------------------------------------------------------------------ */
  _switchSource(kind) {
    const valid = ["file", "video", "board", "browser"];
    const prev = this._source;
    this._source = valid.includes(kind) ? kind : "file";
    const kind_ = this._source;
    const $ = (id) => document.getElementById(id);

    // ⚠️ **来源变了 → 上一来源的识别结果不能留在界面上**。
    //
    // 症状（用户实测）：「音频文件」识别结束后切到「含音轨的视频」，
    // 画面下方凭空多出一句「嗯哼。」—— 那是**音频文件**第 1 轮的
    // 瞬时猜测（committed='' / unfixed='嗯哼。'），第 2 轮就被回滚
    // 推翻了，最终文本里根本没有这句；而这个视频**还没开始识别**。
    //
    // 根因有两层：
    //   1. ``this.rounds`` 跨来源保留 → 视频字幕按播放头取轮时取到了
    //      上一来源的轮次；
    //   2. 字幕取的是那一轮的 ``unfixed`` —— 那是**会被回滚**的猜测。
    //
    // 修法：来源一变就清掉上一来源的结果（正在跑的那次不能丢，见下）。
    if (prev && prev !== kind_) {
      this._onSourceChanged();
    }

    document.querySelectorAll("#asr-source-tabs .asr-source-tab")
      .forEach((btn) => {
        const on = btn.dataset.source === kind_;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      });

    const isFile = kind_ === "file" || kind_ === "video";
    const filePanel = $("asr-panel-file");
    if (filePanel) filePanel.classList.toggle("hidden", !isFile);

    const boardPanel = $("asr-panel-board");
    if (boardPanel) {
      boardPanel.classList.toggle("hidden", kind_ !== "board");
    }
    const browserPanel = $("asr-panel-browser");
    if (browserPanel) {
      browserPanel.classList.toggle("hidden", kind_ !== "browser");
    }

    // 文件与视频：只差「接受什么类型」和一句说明
    const fileInput = $("asr-file");
    if (fileInput) {
      fileInput.accept = kind_ === "video"
        ? "video/*,.mp4,.mov,.mkv,.avi,.flv,.webm,.ts,.m4v,.wmv,.mpg,.mpeg,.3gp"
        : "audio/*,.wav,.mp3,.m4a,.aac,.flac,.ogg,.opus,.wma,.amr,.aiff";
    }
    const hint = $("asr-source-hint");
    if (hint) {
      hint.textContent = kind_ === "video"
        ? "选择含音轨的视频（只取它的声音）；或填写 / 浏览设备端的文件路径。"
        : "选择音频文件，或填写 / 浏览设备端的文件路径。";
    }
    const pathInput = $("asr-source");
    if (pathInput) {
      pathInput.placeholder = kind_ === "video"
        ? "/userdata/assets/uploads/movie.mp4"
        : "/userdata/assets/uploads/asr_en.wav";
    }

    this._syncSourceUi();
    this._syncLiveUi();          // 实时区块的显隐由「flag + 当前来源」共同决定
    this._syncVideoPlayer();     // 视频来源才有播放器
    this._syncAudioPlayer();     // 音频文件源的播放器（2026-11-21，同传的播放头）
    this._interpSyncUi();        // 播放时机的可用性跟着来源走
    // ⚠️ **配音导出**（2026-11-26）—— 切到文件类来源 ⇒ 把下拉（语言 /
    // 音色）和「已生成的产物」拉出来；切走 ⇒ 停掉轮询（别在后台问）。
    if (this._isFileSource()) {
      this._dubLoadOptions();
      this._dubRefreshFiles();
    } else {
      this._dubPollStop();
    }
    // 显示模式要跟着来源走：视频来源下「文字显示框」是字幕同款转写框，
    // 音频文件来源下才是原来的单行「实时结果」。
    this._applyMode();
    if (kind_ !== "video") this._stopSubLoop();
    if (kind_ === "board") {
      this._boardRefresh();
      this._boardLoadDevices();
    }
  },

  /* ------------------------------------------------------------------ *
   * 视频播放（含音轨的视频来源）
   *
   * 两种来源走两条路：
   *   本机上传   → ``blob:`` URL（浏览器本地，不经服务端）
   *   设备端路径 → ``/api/qwen3-asr/media``（Range 支持，可拖进度条）
   *
   * 字幕按**播放头时间**对齐：识别轮次带 ``chunk_start_sec`` /
   * ``chunk_end_sec``（相对**音轨**起点），播放头是相对**容器**起点 ——
   * 两者差一个 ``audio_origin_sec``（音轨在容器里的起点）。
   * ------------------------------------------------------------------ */
  _setFileBlob(file) {
    // 换文件 → 旧的 blob URL 必须 revoke，否则内存一直涨。
    if (this._fileBlobUrl) {
      try { URL.revokeObjectURL(this._fileBlobUrl); } catch (e) { /* noop */ }
      this._fileBlobUrl = "";
    }
    if (file && typeof URL.createObjectURL === "function") {
      try {
        this._fileBlobUrl = URL.createObjectURL(file);
      } catch (e) {
        this._fileBlobUrl = "";
      }
    }
  },

  _releaseVideo() {
    const video = document.getElementById("asr-video");
    if (video) {
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch (e) { /* noop */ }
    }
    this._videoSrc = "";
  },

  _videoUrl() {
    if (this._file && this._fileBlobUrl) return this._fileBlobUrl;
    const p = (this._path || "").trim();
    if (!p) return "";
    return API_PREFIX + "/qwen3-asr/media?path=" + encodeURIComponent(p);
  },

  _syncVideoPlayer() {
    const wrap = document.getElementById("asr-video-wrap");
    const video = document.getElementById("asr-video");
    const hint = document.getElementById("asr-video-hint");
    if (!wrap || !video) return;

    const isVideo = this._source === "video";
    wrap.classList.toggle("hidden", !isVideo);
    if (!isVideo) {
      this._releaseVideo();
      if (hint) hint.textContent = "";
      return;
    }

    const url = this._videoUrl();
    if (!url) {
      this._releaseVideo();
      if (hint) {
        hint.textContent = "选一个含音轨的视频（上传或填设备端路径），"
          + "这里会同步播放，字幕跟着画面走。";
      }
      return;
    }

    if (url !== this._videoSrc) {
      this._videoSrc = url;
      video.src = url;
      try { video.load(); } catch (e) { /* noop */ }
    }
    if (hint) {
      hint.textContent = this._file
        ? "正在播本机上传的视频（识别走它的音轨）。"
        : "正在播设备端的视频（识别走它的音轨）。";
    }
    this._renderVideoSubtitle();
  },

  /* ------------------------------------------------------------------ *
   * 向导「预览播放」：音频文件 / 含音轨的视频 都能在**向导面板里**放
   *
   * 向导框架（core/wizard/state.js）点「预览播放」时：
   *   ① 把 wizard.json 的 ``canvas_id`` 元素搬进向导面板；
   *   ② 点应用页的 ``#asr-open``（= ``open_source_button``）。
   * ⇒ 这里是 ② 的实现：按**文件后缀**选 <audio> 还是 <video>，用
   *   与页面同一个媒体端点（``/api/qwen3-asr/media``）取流。
   *
   * ⚠️ 用**独立**的播放器，不复用页面的 ``#asr-video``：页面上那个
   * 绑着字幕渲染循环（字幕条是它的兄弟节点），把它搬进向导会留下
   * 一个没有字幕条的孤儿视频，而页面自己也会因此看不到画面。
   * ------------------------------------------------------------------ */
  _previewOpen() {
    const path = (document.getElementById("asr-source")?.value || "").trim();
    if (!path) return;
    // ⚠️ 框架点「预览播放」时把向导里的值**直接写进**页面的输入框，
    // 但**不派发 input 事件** ⇒ 卡片的内部状态（``_path``）还是旧的。
    // 不同步的话：向导里选了 A、预览了 A、然后跳过向导 → 页面输入框
    // 显示 A，内部却是旧值 B ⇒ 点「开始识别」识别的是 B。
    // ⇒ 这里补一次同步（与输入框的 input 处理器同口径）。
    if (path !== this._path) {
      this._path = path;
      this._file = null;
      this._setFileBlob(null);
      // ⚠️ 页签跟着路径的后缀走（.mp4 → 「含音轨的视频」）。
      //   不然「跳过向导」那条路会留下错位：向导把路径写进了页面的
      //   #asr-source（框架在点「预览播放」时写的），但 skip() **不调**
      //   applyWizardSnapshot ⇒ 页签停在「音频文件」、播放器不出现。
      this._applyKindFromPath(path);
    }
    const ext = (/\.([a-z0-9]+)$/i.exec(path) || [])[1] || "";
    const isVideo = ASR_VIDEO_EXTS.has(ext.toLowerCase());
    const video = document.getElementById("asr-preview-video");
    const audio = document.getElementById("asr-preview-audio");
    const el = isVideo ? video : audio;
    const other = isVideo ? audio : video;
    if (!el) return;
    // ⚠️⚠️ 切换预览源时，**上一个**播放器必须**停掉**，不能只藏起来。
    //
    // 症状（用户实测）：先预览**视频**、再预览**音频** ⇒ 上一次的视频
    // **还在放声音**，两个声音叠在一起。
    //
    // 根因：``display:none`` **不会**让浏览器暂停媒体 —— 播放与元素可见性
    // 无关。原来这里只做了 ``other.classList.add("hidden")``（藏起来），
    // 播放头照走（实测 1.5s → 3.1s → 4.3s）、``paused`` 恒为 false。
    // ⇒ 藏 **+** 停，两件事都要做。
    this._stopPreviewEl(other);
    el.classList.remove("hidden");
    const url = API_PREFIX + "/qwen3-asr/media?path=" + encodeURIComponent(path);
    try {
      // 同一个源重复点「预览播放」**不要**重头再来（会打断正在播的）；
      // 只有换了源才重新 load。播完了（ended）再点 ⇒ 从头播。
      if (el.getAttribute("src") !== url) {
        el.src = url;
        el.load();
      } else if (el.ended) {
        try { el.currentTime = 0; } catch (e) { /* noop */ }
      }
      const p = el.play();
      if (p && typeof p.catch === "function") p.catch(() => { /* 自动播放被拦 */ });
    } catch (e) { /* noop */ }
  },

  /**
   * 停掉一个预览播放器，并清掉它的源。
   *
   * 「藏起来」≠「停了」—— ``display:none`` 的媒体元素照样播。
   */
  _stopPreviewEl(el) {
    if (!el) return;
    try { el.pause(); } catch (e) { /* noop */ }
    try { el.removeAttribute("src"); el.load(); } catch (e) { /* noop */ }
    el.classList.add("hidden");
  },

  _previewClose() {
    ["asr-preview-video", "asr-preview-audio"].forEach((id) => {
      this._stopPreviewEl(document.getElementById(id));
    });
  },

  /**
   * 预览播放器的收尾守卫。
   *
   * ⚠️ 预览元素被向导框架**搬进向导面板**、向导关闭时按原位搬回卡片页 ——
   * **搬回去了不等于停了**：``<video>`` / ``<audio>`` 还在播，声音会在
   * 后台继续放（卡片页上那个 holder 是 hidden 的，用户只看到页面、听
   * 到声音 ⇒ 典型的「幽灵声音」）。
   * ⇒ 两个收尾点都要停：
   *   * 向导卸载（框架 dispatch ``wizard:unmounted``）；
   *   * 离开这张卡（hashchange）。
   */
  _bindPreviewGuards() {
    if (this._previewGuardsBound) return;
    this._previewGuardsBound = true;
    window.addEventListener("wizard:unmounted", (e) => {
      if (e && e.detail && e.detail.appId === "qwen3-asr") this._previewClose();
    });
    window.addEventListener("hashchange", () => {
      const cur = (location.hash || "").replace(/^#/, "");
      if (cur !== "qwen3-asr") {
        this._previewClose();
        // ⚠️ 离开卡片 ⇒ **同传必须停**（否则板载扬声器还在播译文）。
        this._interpLeaveGuard();
      }
    });
  },

  /**
   * 离开卡片时的同传收尾（停引擎 + 清 TTS 队列）。
   *
   * ⚠️ 用 ``sendBeacon`` 兜底（页面正在卸载，普通 fetch 可能发不出去）。
   */
  _interpLeaveGuard() {
    if (!this._interp.running) return;
    this._interp.running = false;
    this._interpStopPlayhead();
    this._interpStopTimer();
    try {
      const blob = new Blob([JSON.stringify({})],
        { type: "application/json" });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/qwen3-asr/interp/stop", blob);
      }
    } catch (e) { /* noop */ }
    try {
      fetch("/api/qwen3-asr/interp/stop", { method: "POST",
        keepalive: true }).catch(() => { /* noop */ });
    } catch (e) { /* noop */ }
  },

  /**
   * **来源变了**时的清理。
   *
   * 字幕是**视频专属**的展示位（音频文件来源下压根没有这个元素），
   * 所以「切到含音轨的视频」的那一刻，字幕条会**凭空冒出来** ——
   * 而它渲染的是**上一来源**留下的轮次。
   *
   * 实测（用户报的 bug）：「音频文件」识别结束后切到「含音轨的视频」，
   * 画面下方多出一句「嗯哼。」—— 那是音频文件第 1 轮的瞬时猜测
   * （``committed='' / unfixed='嗯哼。'``），第 2 轮就被回滚推翻了，
   * 最终文本里根本没有这句；而这个视频**还没开始识别**。
   *
   * 这里只清**字幕**（以及音轨起点偏移）：
   * * 轮次 / 主文本**不清** —— 那是上一次识别的结果，用户可能还要看，
   *   真要清有「清空」按钮；
   * * 但字幕**必须**清 —— 它会把别的媒体的文字画到当前画面上。
   *
   * 双保险：``_renderVideoSubtitle()`` 里还有一道来源守卫
   * （``_roundsSource !== "video"`` 就清空），所以即使识别中途切来源，
   * 字幕也不会串台。
   */
  _onSourceChanged() {
    const sub = document.getElementById("asr-video-sub");
    if (sub) this._subClear(sub);
    // 时间轴缓存也跟着作废 —— 轮次是**上一来源**的。
    this._subInvalidate();
    // 音轨在容器里的起点是**上一个文件**的 → 必须归零，否则播放头换算
    // 播放头 = 容器时间 - 音轨起点
    // 会算错（切到视频后按旧偏移取值 → 字幕取到错误的轮次）。
    this._audioOriginSec = 0;
  },

  /* ------------------------------------------------------------------ *
   * 节奏字幕（YouTube 式实时字幕）
   *
   * 目标：字幕**跟着播放头的位置**走 —— 一个字一个字地点亮，当前
   * 正在说的那个词高亮，说完的行淡出上移。
   *
   * 为什么不需要「实时推送」：服务端是**整文件快转**（1 秒一块），
   * 识别结果在播放开始前就全到手了。所以「实时字幕」= 一张
   * 「字符 → 音频时间」的对照表 + 播放头位置。这里做的全是**本地
   * 时间轴计算**，不碰后端契约。
   * ------------------------------------------------------------------ */

  /** 来源 / 识别批次变了 → 字幕的时间轴缓存作废。 */
  _subInvalidate() {
    this._subEpochCache = [];
    this._subTl = null;
    this._subTlKey = -1;
    this._subSig = "";
    this._subRanges = [];
    this._seekTarget = -1;
  },

  /* ------------------------------------------------------------------ *
   * 「拖动进度条 → 识别跟过去」
   *
   * 背景
   * ----
   * 服务端是**从头顺序**识别整个文件的。识别速度（RTF≈0.18）比播放快
   * 5~6 倍，所以**顺着播**时字幕总是有内容；但用户把进度条**拖到后面**
   * 就会拖到「还没识别到」的位置 → 那里没有字幕（画面在播第 40 分钟的
   * 声音，字幕却还停在第 5 分钟）。
   *
   * 做法
   * ----
   * 1. 记住**已识别的音频时间区间**（``_subRanges``）；
   * 2. 拖动落点不在已识别区间里 → **重开识别**：停掉当前这一轮，
   *    从「拖动点往前 8 秒」重新开始（``start_sec`` + ``append``）；
   * 3. 新一轮的轮次**追加**到已有的轮次后面 → 字幕时间轴变成
   *    「几段拼起来」，按绝对时间排序后照常渲染。
   *
   * ⚠️ 一次只能跑一个识别（RPC session 只有一条 delta 通道，见
   * ``service.py::_ASR_RPC_LOCK``）→ 所以要**先停再开**。
   * ------------------------------------------------------------------ */

  /** 记一段已识别的音频时间（秒，音轨相对起点）。 */
  _subNoteRange(start, end) {
    const s = Number(start);
    const e = Number(end);
    if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return;
    const rs = this._subRanges || (this._subRanges = []);
    rs.push({ start: s, end: e });
    // 合并相邻 / 重叠的区间（识别是连续推进的，区间会连成一片）
    rs.sort((a, b) => a.start - b.start);
    const out = [];
    for (const r of rs) {
      const last = out[out.length - 1];
      if (last && r.start <= last.end + 0.5) {
        if (r.end > last.end) last.end = r.end;
      } else {
        out.push({ start: r.start, end: r.end });
      }
    }
    this._subRanges = out;
  },

  /** 这个音频时间**识别过**吗？（字幕已经有内容） */
  _subCovered(t) {
    const x = Number(t);
    if (!Number.isFinite(x)) return true;
    for (const r of (this._subRanges || [])) {
      if (x >= r.start - 0.2 && x <= r.end - SUB_SEEK_COVER_MARGIN_SEC) {
        return true;
      }
    }
    return false;
  },

  /** 从一批轮次**重建**已识别区间（刷新恢复用）。 */
  _rebuildSubRanges(rounds) {
    for (const r of (rounds || [])) {
      if (!r) continue;
      if (r.chunk_start_sec === undefined || r.chunk_start_sec === null) continue;
      this._subNoteRange(r.chunk_start_sec, r.chunk_end_sec);
    }
  },

  /**
   * 播放头这一刻**有没有字幕可显示**？
   *
   * 与 :meth:`_subCovered` 的区别（两个判据问的不是同一件事）：
   *
   * * ``_subCovered`` —— 「拖动要不要**重开识别**」：宁可多重开一次，
   *   也不能让用户停在没有字幕的地方 ⇒ 余量给得很大（4 秒）。
   * * 这里 —— 「屏幕上**能不能留字幕**」：播放头落在**还没识别过**的
   *   音频上时，画面上留着的必然是**拖动前**那一行 ⇒ 必须清掉。
   *
   * ⚠️ 为什么不能用「当前行距播放头多远」来判断：识别过的音频里可能
   * 有一大段**静音**（访谈里的停顿），那时「当前行」自然停在静音前
   * 那一行 —— 那是**对的**（话没说完，字幕停住），不能一起清掉。
   * ⇒ 用**区间**判断「这一段音频到底识别过没有」。
   */
  _subRecognizedAt(t) {
    const x = Number(t);
    if (!Number.isFinite(x)) return false;
    for (const r of (this._subRanges || [])) {
      if (x >= r.start - SUB_SHOW_RANGE_HEAD_SEC
          && x <= r.end + SUB_SHOW_RANGE_TAIL_SEC) {
        return true;
      }
    }
    return false;
  },

  /**
   * 拖动 → 必要时把识别**开过去**。
   *
   * 只在「含音轨的视频」来源下做（只有视频有播放头 / 进度条）。
   */
  _maybeFollowSeek() {
    if (this._source !== "video") return;
    if (!this._path && !this._file) return;
    const video = document.getElementById("asr-video");
    if (!video) return;
    const t = (video.currentTime || 0) - (this._audioOriginSec || 0);
    if (!Number.isFinite(t)) return;
    // 已经识别过 → 字幕本来就有，什么都不用做。
    if (this._subCovered(t)) { this._seekPending = null; return; }
    // 正在重开识别 → 记下这次的位置，等当前这次做完再跟过去
    //（用户拖一下不够，还要能「拖着拖着换个地方」）。
    if (this._seekBusy) { this._seekPending = t; return; }
    const now = Date.now();
    if (now < (this._seekGuardUntil || 0)) {
      // 静默期内：只有**换了个位置**才排队。
      if (Math.abs(t - (this._seekTarget ?? -1)) > 1.0) this._seekPending = t;
      return;
    }
    // 同一个位置别重复开（seeked 可能连着触发好几个）。
    if (Math.abs(t - (this._seekTarget ?? -1)) < 1.0) return;
    this._seekTarget = t;
    this._followSeek(t);
  },

  /** 停掉当前识别 → 从 ``audioSec`` 重新开始（保留已识别的轮次）。 */
  async _followSeek(audioSec) {
    this._seekBusy = true;
    const now = Date.now();
    this._seekGuardUntil = now + SUB_SEEK_DEBOUNCE_MS;
    try {
      const hint = document.getElementById("asr-video-hint");
      if (hint) hint.textContent = "正在识别拖到的位置…";
      // ① 停掉正在跑的那一轮（一次只能跑一个识别）
      //
      // ⚠️ **reason=internal** —— 这次停**不是**用户喊停，是前端为了
      // 「重开识别」而停的 ⇒ **画面不能停**（用户正在拖进度条看视频，
      // 停了就没法看了，字幕也得继续跟着播放头走）。后端把 reason
      // 带在 stopped 帧里回来，前端据此**不冻画面**。
      //
      // 踩过的坑：没带 reason 时，stopped 帧一律冻画面 → 拖一下进度条
      // 视频就停了（用户报「拖动进度条后视频和字幕都暂停了」）。
      if (this.snapshot && this.snapshot.busy) {
        await fetch(API_PREFIX + "/qwen3-asr/stop?reason=internal",
                    { method: "POST" })
          .catch(() => {});
        await this._waitIdle(20000);
      }
      // ② 从拖动点往前 8 秒重新开始（容器时间 = 播放头时间）
      const video = document.getElementById("asr-video");
      const containerSec = Math.max(
        0, (video ? (video.currentTime || 0) : audioSec)
           - SUB_SEEK_LEAD_IN_SEC);
      const fd = new FormData();
      if (this._file) {
        fd.append("file", this._file, this._file.name);
      } else {
        fd.append("path", this._path);
      }
      fd.append("start_sec", String(containerSec));
      fd.append("append", "true");
      // ⚠️ **翻译的单位**要跟第一次一致（见 ``start()``）—— 不然这一段
      // 会跑句子级翻译 ⇒ 同一批里两种译文混在一起。
      if (this._trMode === "line") fd.append("translate_mode", "line");
      const r = await fetch(API_PREFIX + "/qwen3-asr/transcribe", {
        method: "POST", body: fd,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || ("HTTP " + r.status));
      }
      // ③ 新一轮的轮次是**追加**的 → 字幕时间轴要重建
      this._subTl = null;
      this._subTlKey = -1;
      this._renderVideoSubtitle();
    } catch (e) {
      const hint = document.getElementById("asr-video-hint");
      if (hint) hint.textContent = "识别拖到的位置失败: " + e;
      console.warn("[qwen3-asr] follow seek failed:", e);
    } finally {
      this._seekBusy = false;
      // 提示文案还原（_syncVideoPlayer 会写回默认的那句）
      this._syncVideoPlayer();
      // 用户在「重开识别」的过程中又拖了别处 → 接着跟过去
      // （否则识别会停在旧的那个位置）。
      const pend = this._seekPending;
      this._seekPending = null;
      if (pend !== null
          && Math.abs(pend - (this._seekTarget ?? -1)) > 1.0) {
        this._seekTarget = pend;
        this._followSeek(pend);
      }
    }
  },

  /** 等服务端**不再忙**（识别线程收尾）。 */
  _waitIdle(timeoutMs) {
    const t0 = Date.now();
    return new Promise((resolve) => {
      const tick = () => {
        if (!this.snapshot || !this.snapshot.busy) { resolve(true); return; }
        if (Date.now() - t0 > timeoutMs) { resolve(false); return; }
        setTimeout(tick, 150);
      };
      tick();
    });
  },

  /**
   * 把 round 帧切成 **epoch**（SDK 会话）。
   *
   * 服务端每 60 秒切一个新会话（``_FILE_SESSION_MAX_SEC``），切的时候
   * ``committed`` 归零、``round`` 编号回到 1 —— 但**音频时间轴是连续
   * 的**（``chunk_start_sec`` 接着上一段往下数）。所以：
   *
   *   * 用「round 编号回退」或「committed 长度变短」认出 epoch 边界；
   *   * 每个 epoch **单独**建时间轴（committed 是段内累积的，跨段
   *     直接拼会错位），再按时间顺序把各段的行拼起来。
   */
  _splitEpochs(rounds) {
    const epochs = [];
    let cur = null;
    let prevRound = null;
    let prevLen = 0;
    for (const r of rounds) {
      const cLen = (r.committed || "").length;
      const rn = typeof r.round === "number" ? r.round : null;
      const isNew = !cur
        || (rn !== null && prevRound !== null && rn <= prevRound)
        || cLen < prevLen;
      if (isNew) { cur = []; epochs.push(cur); }
      cur.push(r);
      if (rn !== null) prevRound = rn;
      prevLen = cLen;
    }
    return epochs;
  },

  /**
   * 一个 epoch 的「字符数 → 音频时间」插值表。
   *
   * 锚点取 **(假设文本与最终文本的最长公共前缀长度, 该轮 chunk 终点)**。
   *
   * 为什么不是「假设文本的长度」：模型每轮的**假设**（committed+unfixed）
   * 会被回滚、也会改标点 —— 第 1 轮在英文音频上先猜了个中文填充词
   * 「嗯哼。」，第 2 轮拿到上下文后整个推翻。而**最终字幕**用的是最后
   * 确认下来的文本 F。所以真正要问的是「到此刻为止，**F 里**已经被说
   * 出来多少字」。
   *
   * committed 一定是 F 的前缀（只增不减），unfixed 不一定 → 公共前缀
   * 长度正好就是这个数。
   *
   * 实测（15 秒英文样本 / 90 秒样本，各按音频能量包络打分）：
   *   假设长度    浊音覆盖 0.728 / 0.689
   *   公共前缀    浊音覆盖 0.750 / 0.717   ← 采用
   * 「浊音覆盖」= 每个词的时间窗有多少落在真实语音段里（1.0 = 完全对上）。
   */
  _epochTable(ep, finalText) {
    const raw = [];
    for (const f of ep) {
      const te = f.chunk_end_sec;
      if (te === undefined || te === null) continue;
      const t = Number(te);
      if (!Number.isFinite(t)) continue;      // 坏时间戳 → 当这一轮不存在
      const hyp = (f.committed || "") + (f.unfixed || "");
      raw.push([_lcp(hyp, finalText), t]);
    }
    // 高水位线：回滚时计数不许倒退（否则插值会出现负斜率）。
    const mono = [];
    let hi = -1;
    for (const pair of raw) {
      const x = pair[0] < hi ? hi : pair[0];
      if (mono.length && mono[mono.length - 1][0] === x) continue;
      hi = x;
      mono.push([x, pair[1]]);
    }
    return mono.length ? mono : null;
  },

  /** 字符下标 → 音频时间（表内线性插值）。 */
  _timeAt(table, idx) {
    if (!table || !table.length) return null;
    if (idx <= table[0][0]) return table[0][1];
    for (let i = 1; i < table.length; i++) {
      const x0 = table[i - 1][0], t0 = table[i - 1][1];
      const x1 = table[i][0], t1 = table[i][1];
      if (idx <= x1) {
        if (x1 <= x0) return t1;
        return t0 + (idx - x0) * (t1 - t0) / (x1 - x0);
      }
    }
    return table[table.length - 1][1];
  },

  /**
   * 一个 epoch 的字幕行。
   *
   * 先把最终文本切成词、给每个词算出时间，再按 YouTube 的习惯分行：
   * 句末标点必断、句中标点够长就断、太长或太长时段也断。
   */
  _epochLines(ep, epochIndex) {
    // 这个 epoch 的最终文本 = 最后一个非空 committed（段内只增不减）
    let finalText = "";
    for (const f of ep) if (f.committed) finalText = f.committed;
    if (!finalText.trim()) return [];
    const table = this._epochTable(ep, finalText);
    if (!table) {
      // 拿不到时间戳（round 帧里没有 chunk 边界）→ 没法做节奏字幕，
      // 退化成「整句一次性显示」（旧行为），而不是什么都不显示。
      const words = _splitWords(finalText).map((w) => ({
        text: w.text, start: 0, end: 0,
      }));
      if (!words.length) return [];
      return [{
        id: epochIndex + ":0:" + words.length + ":0",
        words: words, start: 0, end: 0,
      }];
    }

    const timed = [];
    for (const w of _splitWords(finalText)) {
      const s = this._timeAt(table, w.i0);
      const e = this._timeAt(table, w.i1);
      if (s === null || e === null) continue;
      timed.push({ text: w.text, start: s, end: e < s ? s : e });
    }
    if (!timed.length) return [];

    const lines = [];
    let cur = [];
    let cols = 0;
    const flush = () => {
      if (!cur.length) return;
      lines.push({
        // id = 这行的**内容指纹**：行号 + 词数 + 首尾时间 + **文本**。
        //
        // ⚠️ 文本必须进 id：还在长的那个 epoch 里，模型可能给某个词
        // **补上标点或空格**（词数不变、时间也不变）—— 只拿「词数 +
        // 起始时间」判不出内容变了 → 那一行会永远停在旧文本上
        // （实测：转写框里少了一个空格，画面字幕也是旧的那一版）。
        // 「画面字幕」和「文字转写框」都靠这个 id 判断「要不要重建」，
        // 指纹不准就等于两边显示的不是同一份内容。
        id: epochIndex + ":" + lines.length + ":" + cur.length + ":"
            + cur[0].start.toFixed(3) + ":"
            + cur[cur.length - 1].end.toFixed(3) + ":"
            + cur.map((w) => w.text).join(""),
        words: cur,
        start: cur[0].start,
        end: cur[cur.length - 1].end,
      });
      cur = [];
      cols = 0;
    };
    for (const w of timed) {
      cur.push(w);
      const trim = w.text.trim();
      cols += trim.length + (CJK_RE.test(trim) ? 1 : 0);   // CJK 占两列
      const dur = cur[cur.length - 1].end - cur[0].start;
      if (SUB_HARD_END.test(trim)) flush();
      else if (SUB_SOFT_END.test(trim) && cols >= SUB_MIN_SOFT_CHARS) flush();
      else if (cols >= SUB_MAX_LINE_COLS) flush();
      else if (dur >= SUB_MAX_LINE_SEC) flush();
    }
    flush();
    this._spaceOutLines(lines);
    return lines;
  },

  /**
   * 给每一行兜一个**最短占位时长**。
   *
   * 什么时候会需要：模型偶尔会**卡一下**（同一段音频反复转写），然后
   * 在一秒里把积压的一大段一次性提交。实测（90 秒重复音频）出现
   * 「7 行挤在 1 秒里」→ 字幕一帧一帧地闪过去，根本来不及看。
   *
   * 做法：把后面几行的**开始时间**往后推，保证每行至少占
   * ``SUB_MIN_LINE_SEC``；行内的词整体跟着平移（保持相对关系），
   * 所以「词的时间」和「行的时间」始终一致。
   *
   * 只在本段（epoch）内推 —— 段与段之间本来就是连续的音频，不该被
   * 上一段的排版影响。
   */
  _spaceOutLines(lines) {
    let prevStart = null;
    let prevEnd = null;
    for (const ln of lines) {
      let want = ln.start;
      if (prevStart !== null) {
        want = Math.max(want, prevEnd, prevStart + SUB_MIN_LINE_SEC);
      }
      if (want > ln.start + 1e-9) {
        const d = want - ln.start;
        ln.start = want;
        ln.end += d;
        for (const w of ln.words) { w.start += d; w.end += d; }
      }
      prevStart = ln.start;
      prevEnd = ln.end;
    }
  },

  /**
   * 整条字幕时间轴（各 epoch 的行按时间拼起来）。
   *
   * 缓存粒度 = **epoch**：已结束的 epoch 不会再变（文本已定），只有
   * 最后一段还在长。所以每来一条 round 帧只需重算最后一段 —— 长音频
   * 也不会越跑越卡。
   */
  _buildSubTimeline() {
    const rounds = this.rounds || [];
    const key = rounds.length;
    if (this._subTl && this._subTlKey === key) return this._subTl;
    const epochs = this._splitEpochs(rounds);
    const cache = this._subEpochCache;
    const lines = [];
    // ⚠️ **多段识别**（拖动后重开）→ epoch 的时间**不一定**按顺序来：
    // 用户往回拖过一次，两段的时间就会**倒序**（后一段的起点比前一段早）。
    // 时间轴是按「最后一个 start ≤ 播放头的行」找当前行的 → 必须先按
    // **绝对时间**排序，否则会停在错误的行上。
    const order = epochs.map((ep, i) => ({
      i: i, ep: ep,
      start: (ep.length && Number(ep[0].chunk_start_sec)) || 0,
    }));
    order.sort((a, b) => (a.start - b.start) || (a.i - b.i));
    let guardEnd = -Infinity;
    for (const o of order) {
      const ep = o.ep;
      let hit = cache[o.i];
      if (!hit || hit.key !== ep.length) {
        hit = { key: ep.length, lines: this._epochLines(ep, o.i) };
        cache[o.i] = hit;
      }
      for (const ln of hit.lines) {
        // 重叠区（两段识别到了同一段音频）：保留**先开始**的那一段 ——
        // 它是在有上下文的时候转写的（后一段从中间开始，起点是零上下文）。
        if (ln.start < guardEnd - 1e-6) continue;
        lines.push(ln);
      }
      const last = hit.lines[hit.lines.length - 1];
      if (last && last.end > guardEnd) guardEnd = last.end;
    }
    if (cache.length > epochs.length) cache.length = epochs.length;
    this._subTl = { lines };
    this._subTlKey = key;
    return this._subTl;
  },

  /** 常驻 3 个行槽（内容变了才重建内部 span，淡出动画才不会断）。 */
  _subEnsureSlots(sub) {
    if (this._subSlots.length === SUB_MAX_VISIBLE_LINES
        && sub.children.length === SUB_MAX_VISIBLE_LINES) {
      return this._subSlots;
    }
    sub.innerHTML = "";
    this._subSlots = [];
    for (let i = 0; i < SUB_MAX_VISIBLE_LINES; i++) {
      const el = document.createElement("div");
      el.className = "asr-sub-line";
      el.style.opacity = "0";
      el.style.visibility = "hidden";
      sub.appendChild(el);
      // 每个槽 = 「原文行 + 译文行」两段（译文可有可无）。
      //
      // ⚠️ 译文**不是**独立的一行 —— 它是这一行的**下半部分** ⇒ 跟着
      // 同一行的淡入淡出、同一套槽位 ⇒ 「翻译跟随字幕的显示节奏」是
      // **结构上**成立的，不是靠额外同步。
      const orig = document.createElement("div");
      orig.className = "asr-sub-orig";
      el.appendChild(orig);
      const tr = document.createElement("div");
      tr.className = "asr-sub-tr hidden";
      el.appendChild(tr);
      this._subSlots.push({ el: el, orig: orig, tr: tr, id: "", spans: [],
                            trKey: "" });
    }
    return this._subSlots;
  },

  _subClear(sub) {
    if (!sub.firstChild && this._subSig === "") return;
    this._subSig = "";
    sub.innerHTML = "";
    this._subSlots = [];
  },

  /**
   * 逐帧刷新字幕。
   *
   * ``timeupdate`` 只有 4Hz，撑不起「逐词点亮」—— 高亮会一跳一跳的。
   * 所以播放期间挂一个 rAF 循环，每帧算一次；真正改 DOM 只在**签名
   * 变了**的时候（见 ``_renderVideoSubtitle``）。
   */
  _startSubLoop() {
    if (this._subRaf) return;
    const tick = () => {
      this._subRaf = requestAnimationFrame(tick);
      this._renderVideoSubtitle();
    };
    this._subRaf = requestAnimationFrame(tick);
  },

  _stopSubLoop() {
    if (this._subRaf) {
      cancelAnimationFrame(this._subRaf);
      this._subRaf = 0;
    }
  },

  /**
   * 一行在时刻 ``t`` 的**词状态**：已经点亮到第几个词、当前正在说的
   * 是第几个词。
   *
   * 画面字幕和文字转写框**共用**这一个函数 —— 「显示效果一样」不是
   * 「看起来像」，是**同一份算法算出来的**；两边各写一份的话，早晚
   * 会对不上（高亮的位置差一帧这种问题最难查）。
   */
  _revealState(ln, t) {
    let revealed = 0;
    let current = -1;
    for (let i = 0; i < ln.words.length; i++) {
      const w = ln.words[i];
      if (w.start <= t) {
        revealed = i + 1;
        if (t < w.end) current = i;
      }
    }
    return { revealed: revealed, current: current };
  },

  /**
   * 按播放头渲染字幕。
   *
   * 渲染规则（YouTube 实时字幕那套）：
   *   * 当前行 = 最后一个「开始时间 ≤ 播放头」的行；
   *   * 行内的词**逐个点亮**（还没说到的词占位但不可见）；
   *   * 正在说的那个词高亮；
   *   * 说完的行保留一小会儿（淡出），再上面就没了。
   */
  _renderVideoSubtitle() {
    // 文字转写框（视频来源下的「文字显示框」）跟画面字幕**同步**渲染
    // —— 同一帧、同一份时间轴、同一份词状态算法。
    this._renderTranscript();
    const video = document.getElementById("asr-video");
    const sub = document.getElementById("asr-video-sub");
    if (!video || !sub) { this._stopSubLoop(); return; }
    // ⚠️ 轮次必须来自**视频**这次识别。上一来源（音频文件）的轮次在
    // 切换来源时仍然留在 ``this.rounds`` 里 —— 拿它渲染字幕，就会把
    // 别的媒体的轮次画到这个视频画面上（实测：切到视频后字幕显示
    // 「嗯哼。」，而视频当时还没开始识别）。
    if (this._source !== "video" || this._roundsSource !== "video") {
      this._subClear(sub);
      return;
    }
    const lines = this._buildSubTimeline().lines;
    if (!lines.length) { this._subClear(sub); return; }

    // 播放头（容器时间）→ 音频时间（chunk 时间戳也是音轨相对时间）
    const t = (video.currentTime || 0) - (this._audioOriginSec || 0);

    // ⚠️ **播放头必须落在「已识别的音频区间」里** —— 否则画面上留着的
    // 是**拖动前**那一行（旧字幕一直不更新，用户报「拖动后还保留着
    // 前面或者不是实时语音的字幕」）。
    //
    // 实测（150 秒对谈片段，拖到 110 s）：播放头 112.4 s，画面上还挂着
    // 78.7~80.1 s 那一行（**滞后 32 秒**）；而已识别区间是
    // (0.1, 82.1) 和 (103.4, 108.4) —— 播放头落在**两个区间之外**
    //（新识别还没追上来）。
    //
    // ⇒ 落在区间外 ⇒ **清空**（等新识别追上来的那一瞬自然恢复）。
    if (!this._subRecognizedAt(t)) { this._subClear(sub); return; }

    // ⚠️ **当前行**用**同一个方法**算（``_curSubIndex``）—— 画面显示的是哪
    // 一行，译文就该**先要哪一行** ⇒ 两边必须一致。
    //
    // ⚠️ 判据是「**区间包含播放头**」，不是「最后一个 start ≤ 播放头」
    // （见 ``_curSubIndex`` 的注释）：落在时间轴的**洞**里 ⇒ 没有当前行
    // ⇒ 画面上**不留旧行**当「当前行」。
    const pick = this._subPick(lines, t);
    const cur = pick.cur;
    const prev = pick.prev;

    // 可见行：当前行 + 上面还没淡出的行
    //
    // ⚠️ 记的是**行号**（不是行对象）—— 找译文要靠「上一行的结束时间」
    // 定区间（见 ``_trItemsForLine``）⇒ 需要它在时间轴里的位置。
    const wantIdx = [];
    const wantDim = [];      // 这一行是不是「已经不是当前行了」（压暗）
    if (cur >= 0) {
      for (let i = cur; i >= 0 && wantIdx.length < SUB_MAX_VISIBLE_LINES; i--) {
        const ln = lines[i];
        // ⚠️ 用 ``_subLineEnd``（不是 ``ln.end``）—— **零时长**的行
        // （锚点表塌成一个点）也得有同样的宽限，否则它**立刻**消失。
        if (i < cur && t - this._subLineEnd(ln) > SUB_PREV_HOLD_SEC) break;
        wantIdx.unshift(i);
        wantDim.unshift(false);
      }
    } else if (prev >= 0) {
      // ⚠️ **洞里**（这一刻没有任何一行覆盖播放头）：当前行 = 没有。
      //
      // 老判据下这里是「洞前那一行」⇒ 它被当成「当前行」⇒ **永远不淡出**
      // ⇒ 画面上钉着一句已经说完的话（实测连续段里落后最久 29.75 s；
      // 拖动过的洞可以长达几分钟）⇒ 用户看到的就是「字幕比声音更慢」。
      //
      // ⇒ 现在只保留**刚说完**的那一行（还在 ``SUB_PREV_HOLD_SEC`` 的
      //    淡出窗口里），而且它**不再是当前行**（压暗、没有当前词高亮）。
      //    出了窗口 ⇒ 画面清空 ⇒ 屏幕上的东西永远跟得上声音。
      const ln = lines[prev];
      if (t - this._subLineEnd(ln) <= SUB_PREV_HOLD_SEC) {
        wantIdx.push(prev);
        wantDim.push(true);
      }
    }
    if (!wantIdx.length) { this._subClear(sub); return; }
    const want = wantIdx.map((i) => lines[i]);

    // 每行：已经点亮到第几个词 + 当前正在说的词 + **这一行的译文**
    const states = want.map((ln, k) => {
      const rv = this._revealState(ln, t);
      const trs = this._trItemsForLine(lines, wantIdx[k]);
      return { ln: ln, revealed: rv.revealed, current: rv.current,
               trs: trs, trKey: this._trKeyOf(trs), dim: wantDim[k] };
    });

    // 签名没变 → 什么都不用动（rAF 每秒 60 次，别白折腾 DOM）
    //
    // ⚠️ 签名里**必须含译文** —— 译文是**异步**到的（要等模型翻完），
    // 到了之后这一行的内容变了 ⇒ 签名得变 ⇒ DOM 得重画。
    //
    // ⚠️ 签名里**必须含 dim** —— 「这一行已经不是当前行了」（洞里的
    // 残留行）只改**透明度**，别的字段可能都没变 ⇒ 不进签名 ⇒ 透明度
    // 改不动 ⇒ 那行还是「当前行」的样子（满亮度 + 当前词高亮）。
    const sig = states.map((s) => s.ln.id + ":" + s.revealed + ":" + s.current
                            + ":" + s.trKey + ":" + (s.dim ? 1 : 0)).join(";");
    if (sig === this._subSig) return;
    this._subSig = sig;

    const slots = this._subEnsureSlots(sub);
    const n = states.length;
    const offset = slots.length - n;
    for (let i = 0; i < slots.length; i++) {
      const slot = slots[i];
      const st = i >= offset ? states[i - offset] : null;
      if (!st) {
        slot.el.style.opacity = "0";
        slot.el.style.visibility = "hidden";
        continue;
      }
      slot.el.style.visibility = "visible";
      // 越旧越淡（当前行最亮）
      //
      // ⚠️ ``dim`` = 这一行**已经不在当前行了**（落在时间轴的洞里）
      // ⇒ 压到跟「上一行」一样的亮度，别让它看起来像正在被说。
      const age = n - 1 - (i - offset);
      slot.el.style.opacity = st.dim ? "0.7"
                            : (age === 0 ? "1" : (age === 1 ? "0.7" : "0.42"));
      if (slot.id !== st.ln.id) {
        slot.id = st.ln.id;
        slot.orig.innerHTML = "";
        slot.spans = st.ln.words.map((w) => {
          const sp = document.createElement("span");
          sp.className = "asr-sub-word";
          sp.textContent = w.text;
          slot.orig.appendChild(sp);
          return sp;
        });
        // ⚠️ **槽位换行 ⇒ 必须把译文一起清掉** —— 槽位是**复用**的
        // （固定几个槽位轮着用），上一行挂的译文会留在 DOM 里。
        //
        // ⚠️ 光把 ``trKey`` 置空是**不够**的：下面那段「key 变了才写 DOM」
        // 的判断里，新旧 key 都是 ""（这一行本来就没译文）⇒ **不写 DOM**
        // ⇒ 旧译文的文字**留在屏幕上**。
        //
        // 症状（实测）：同一句译文同时显示在**两行**上；关掉「实时翻译」
        // 之后画面字幕上的译文也**收不掉**。
        slot.trKey = "";
        slot.tr.textContent = "";
        slot.tr.classList.add("hidden");
      }
      for (let k = 0; k < slot.spans.length; k++) {
        const spoken = k < st.revealed;
        slot.spans[k].classList.toggle("asr-sub-pending", !spoken);
        slot.spans[k].classList.toggle("asr-sub-current", spoken && k === st.current);
      }
      // ---- 译文（双语字幕的另一半）------------------------------- //
      //
      // 译文挂在**这一行**下面 ⇒ 跟这一行一起淡入淡出 ⇒ 天然跟着字幕
      // 的节奏。译文可能**比原文晚到**（要等模型翻完）⇒ 每帧都看一眼，
      // 变了才动 DOM。
      if (slot.trKey !== st.trKey) {
        slot.trKey = st.trKey;
        const txt = this._trTextOf(st.trs);
        slot.tr.textContent = txt;
        slot.tr.classList.toggle("hidden", !txt);
      }
    }
  },

  /* ------------------------------------------------------------------ *
   * 字幕同款转写框（视频来源下的「文字显示框」）
   *
   * 用户要求：「含音轨的视频」来源下，主文字框也按**实时字幕的格式**
   * 输出 —— 同样的分行、同样的逐词点亮、同样的当前词高亮 ——
   * 而且**历史全部保留**，框可以**上拉回看**。
   *
   * 和画面字幕的差别只有一个：**范围**
   *   * 画面字幕：当前那几行，说完就淡出（历史不留）；
   *   * 转写框：整条时间轴的**所有行**都在，按播放头标出「当前行」。
   *
   * 所以两者共用同一份时间轴（``_buildSubTimeline``）和同一个词状态
   * 算法（``_revealState``）——「显示效果一样」不是「看起来像」，是
   * **同一份代码算出来的**。
   *
   * 性能
   * ----
   * 98 分钟的片子时间轴有几千行，每帧重建整个 DOM 是不可接受的。
   * 所以：① 行按 **id** 增量维护（只重建变了的行）；② 每帧只更新
   * **当前行**的词状态（几十个 span），其余行不动。
   * ------------------------------------------------------------------ */

  /**
   * 渲染文字转写框。
   *
   * 只在「含音轨的视频」来源下生效；其它来源（音频文件 / 麦克风）
   * 走原来的单行「实时结果」。
   */
  _renderTranscript() {
    const box = document.getElementById("asr-transcript");
    if (!box) return;
    const live = document.getElementById("asr-live");
    const isVideo = this._source === "video" && this._roundsSource === "video";
    if (live) {
      live.classList.toggle("asr-live-video", isVideo);
      // ⚠️ **实时来源的归属守卫** —— 单行文字框显示的是「**最近一批**」
      // 的文本。切到**实时来源**（麦克风）时，那一行是**文件批次**的历史，
      // 而麦克风的文字在它自己的框里（``#asr-rt-committed``）→ 藏起来，
      // 别让两段文字并排出现。
      //
      // ⚠️ **文件 ↔ 视频之间不藏**：那两栏是同一批结果的两种视图
      // （视频来源多一个「字幕同款转写框」）—— 藏了的话，视频的识别
      // 结果在切回文件页签后就**彻底看不见**了（U18 的契约）。
      // 真正要挡住的是「画到画面上」的那几处：视频字幕 / 转写框靠
      // ``_roundsSource`` 把关（见 ``_renderVideoSubtitle``）。
      live.classList.toggle("asr-live-notfile", !this._isFileSource());
    }
    // ⚠️⚠️ **显隐统一走 ``_syncResultBoxes``**（2026-11-25 合并）——
    // 这个框**每次 timeupdate 都会被重画** ⇒ 在这里直接 toggle 会把
    // 「同传有内容 ⇒ 藏掉实时结果」那条**合并规则覆盖掉** ⇒ 两个列表
    // 又同时出现 ⇒ 同一批句子显示两遍。
    this._syncResultBoxes();
    if (!isVideo) { this._transcriptClear(); return; }

    const tl = this._buildSubTimeline();
    const lines = tl.lines || [];
    if (!lines.length) { this._transcriptClear(); return; }

    // 时间轴换了 → 行要重排（只重建**变了的**那几行）
    let rebuilt = false;
    if (this._trTl !== tl) {
      this._trTl = tl;
      this._transcriptSync(lines);
      rebuilt = true;
    }

    // 播放头（容器时间）→ 音频时间
    const video = document.getElementById("asr-video");
    const t = (video ? (video.currentTime || 0) : 0) - (this._audioOriginSec || 0);

    // ⚠️ **当前行**必须跟画面字幕用**同一份判据**（``_curSubIndex``）——
    // 两边各写一份的话，早晚会对不上（这里是曾经就有过的那份 inline 副本）。
    //
    // 判据 = 「时间区间**包含**播放头」；落在时间轴的**洞**里 ⇒ 没有当前行
    // ⇒ 转写框里也不该有一行被标成「当前」（见 ``_curSubIndex``）。
    const pick = this._subPick(lines, t);
    const cur = pick.cur;
    const prev = pick.prev;
    this._transcriptPaint(lines, cur, prev, t, rebuilt);
    this._transcriptScroll(cur);
  },

  /**
   * 让 DOM 里的行与时间轴**对齐**（增量）。
   *
   * 时间轴是「往后长」的（每来一条 round 帧多几行），所以**公共前缀**
   * 直接复用，只重建后面变掉的那几行 —— 长片子也是 O(变化量)。
   */
  _transcriptSync(lines) {
    const body = document.getElementById("asr-transcript-body");
    if (!body) return;
    const rows = this._trRows;
    let keep = 0;
    const n = Math.min(rows.length, lines.length);
    while (keep < n && rows[keep].id === lines[keep].id) keep += 1;
    // 变掉的后缀：先拆掉旧的，再按新顺序追加（顺序自然对齐）
    for (let i = keep; i < rows.length; i++) {
      if (rows[i].el.parentNode === body) body.removeChild(rows[i].el);
    }
    rows.length = keep;
    for (let i = keep; i < lines.length; i++) {
      const ln = lines[i];
      const el = document.createElement("div");
      el.className = "asr-sub-line asr-tr-future";
      el.dataset.lineId = ln.id;
      // 原文一行（逐词点亮的那段）
      const orig = document.createElement("div");
      orig.className = "asr-sub-orig";
      const spans = [];
      for (const w of ln.words) {
        const sp = document.createElement("span");
        sp.className = "asr-sub-word";
        sp.textContent = w.text;
        orig.appendChild(sp);
        spans.push(sp);
      }
      el.appendChild(orig);
      // 译文一行（双语字幕的另一半）—— 与画面字幕**同一份映射**
      // （见 ``_trItemsForLine``）⇒ 两边显示的译文必然一致。
      const tr = document.createElement("div");
      tr.className = "asr-sub-tr asr-sub-tr-box hidden";
      el.appendChild(tr);
      body.appendChild(el);
      rows.push({ id: ln.id, el: el, spans: spans, state: "future",
                  tr: tr, trKey: "" });
    }
  },

  /**
   * 给行标状态（过去 / 当前 / 未来），并更新**当前行**的词状态。
   *
   * 平时只碰「状态发生变化的那一段」—— 当前行往前挪一步，就只改这两行；
   * 往回拖很多秒也只是多改几行，不会全量重刷。
   *
   * 但**时间轴重排过**（拖动重开识别 → 新的一段插到中间）时，行的
   * 下标全变了 → 得**全量**刷一遍状态，否则会出现「已经说过的行还
   * 标着未来（压暗）」的错位。
   */
  _transcriptPaint(lines, cur, prev, t, full) {
    const rows = this._trRows;
    if (!rows.length) return;
    // 「过去 / 未来」的分界：洞里（``cur < 0``）没有当前行 ⇒ 用「最后一行
    // **已经开始过**」的行（``prev``）当分界。
    const mark = cur >= 0 ? cur : prev;
    const lo = full ? 0 : Math.min(mark, this._trCur);
    const hi = full ? rows.length - 1 : Math.max(mark, this._trCur);
    for (let i = lo; i <= hi; i++) {
      if (i < 0 || i >= rows.length) continue;
      // ⚠️ 洞里（``cur < 0``）⇒ **没有任何一行是「当前行」** ⇒ 已经说过的
      // 那几行算「过去」；别把它标成「未来」（它明明已经说完了）。
      const st = (cur >= 0)
        ? (i < cur ? "past" : (i === cur ? "current" : "future"))
        : (i <= mark ? "past" : "future");
      this._transcriptSetState(rows[i], st);
    }
    // 当前行：逐词点亮 + 当前词高亮（与画面字幕同一份算法）
    if (cur >= 0 && cur < rows.length) {
      const rv = this._revealState(lines[cur], t);
      const spans = rows[cur].spans;
      for (let k = 0; k < spans.length; k++) {
        const spoken = k < rv.revealed;
        spans[k].classList.toggle("asr-sub-pending", !spoken);
        spans[k].classList.toggle("asr-sub-current", spoken && k === rv.current);
      }
    }
    // ---- 译文（双语）----------------------------------------------- //
    //
    // 译文是**异步**到的（要等模型翻完）⇒ 到的时候要补到对应行上。
    // ⚠️ 只刷 [lo..hi] 不够：刚到的译文可能属于**已经滚过去的**行 ⇒
    // 「译文版本变了」时整表刷一遍（一次几千行也就几毫秒，而译文一两句
    // 才来一次）。
    const trAll = full || this._trVer !== this._trPassVer;
    if (trAll) {
      this._trPassVer = this._trVer;
      for (let i = 0; i < rows.length; i++) {
        this._transcriptSetTr(rows[i], lines, i);
      }
    } else {
      for (let i = lo; i <= hi; i++) {
        if (i < 0 || i >= rows.length) continue;
        this._transcriptSetTr(rows[i], lines, i);
      }
    }
    this._trCur = cur;
  },

  /** 给一行填上它的译文（没有就收起来）。 */
  _transcriptSetTr(row, lines, i) {
    if (!row || !row.tr) return;
    const trs = this._trItemsForLine(lines, i);
    const key = this._trKeyOf(trs);
    if (row.trKey === key) return;
    row.trKey = key;
    const txt = this._trTextOf(trs);
    row.tr.textContent = txt;
    row.tr.classList.toggle("hidden", !txt);
  },

  /** 切换一行的状态（过去 / 当前 / 未来）。 */
  _transcriptSetState(row, st) {
    if (row.state === st) return;
    const was = row.state;
    row.state = st;
    row.el.classList.toggle("asr-tr-past", st === "past");
    row.el.classList.toggle("asr-tr-future", st === "future");
    row.el.classList.toggle("asr-tr-current", st === "current");
    // 离开「当前行」→ 把逐词点亮的状态收掉（整行都算「已说过」）
    if (was === "current" && st !== "current") {
      for (const sp of row.spans) {
        sp.classList.remove("asr-sub-pending");
        sp.classList.remove("asr-sub-current");
      }
    }
  },

  /**
   * 滚动到当前行（只在它**看不见**的时候才动）。
   *
   * 已经在视野里 → 一点不动（用户正在看它，别把画面推走）；
   * 看不见了 → 把它带回来（这是「跟着播放头走」）。
   */
  _transcriptScroll(cur) {
    if (!this._trFollow) return;
    const body = document.getElementById("asr-transcript-body");
    if (!body || cur < 0 || cur >= this._trRows.length) return;
    // 框没布局（隐藏 / 高度为 0）→ 别乱滚
    if (!body.clientHeight) return;
    const row = this._trRows[cur].el;
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const viewTop = body.scrollTop;
    const viewBottom = viewTop + body.clientHeight;
    if (top >= viewTop && top + h <= viewBottom) return;
    this._trAutoAt = Date.now();
    body.scrollTop = (top < viewTop)
      ? Math.max(0, top - 4)
      : Math.max(0, top + h - body.clientHeight + 4);
  },

  /**
   * 用户**手动**滚动时的处理。
   *
   * 用户上翻回看 → 当前行看不见了 → **暂停跟随**（别把人拽回来）；
   * 滚回当前行附近 → 恢复跟随。
   */
  _onTranscriptScroll() {
    // 自己触发的滚动不算「用户滚动」
    if (Date.now() - (this._trAutoAt || 0) < 250) return;
    const body = document.getElementById("asr-transcript-body");
    if (!body) return;
    if (!body.clientHeight) return;      // 没布局 → 不动跟随状态
    const cur = this._trCur;
    if (cur < 0 || cur >= this._trRows.length) { this._trFollow = false; return; }
    const row = this._trRows[cur].el;
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const visible = (top < body.scrollTop + body.clientHeight)
                 && (top + h > body.scrollTop);
    this._trFollow = visible;
  },

  /** 清空转写框（切来源 / 清空按钮 / 还没识别）。 */
  _transcriptClear() {
    const body = document.getElementById("asr-transcript-body");
    if (body && body.firstChild) body.innerHTML = "";
    this._trRows = [];
    this._trTl = null;
    this._trCur = -1;
    this._trFollow = true;
  },

  _playVideoIfAny() {
    if (this._source !== "video") return;
    const video = document.getElementById("asr-video");
    if (!video || !video.src) return;
    // ⚠️ **已经播完就别再 play()** —— HTML5 规定：媒体 ``ended`` 之后
    // 再 ``play()`` 会**从头重播** ⇒ 播放头回到 0 ⇒「源已结束」被清掉
    // ⇒ 跟播放又得从头等 ⇒ 后面识别出来的句子又被压住
    // ⇒ **「只播放了第一句」**（实测，用户报）。
    if (video.ended) return;
    const p = video.play();
    if (p && typeof p.catch === "function") {
      p.catch((e) => {
        // 自动播被浏览器策略拦下（少见：点「开始识别」已是用户手势）
        // → 不算错误，用户手动点一下播放键即可。
        console.info("[qwen3-asr] 视频自动播放未执行:", e && e.name);
      });
    }
  },

  /** 音频文件来源 → 起播（同传的「跟播放」需要播放头在走）。 */
  _playAudioIfAny() {
    if (this._source !== "file") return;
    const a = document.getElementById("asr-audio-player");
    if (!a || !a.src) return;
    // ⚠️ **已经播完就别再 play()** —— HTML5 规定：媒体 ``ended`` 之后
    // 再 ``play()`` 会**从头重播** ⇒ 播放头回到 0 ⇒「源已结束」被清掉
    // ⇒ 跟播放又得从头等 ⇒ 后面识别出来的句子又被压住
    // ⇒ **「只播放了第一句」**（实测，用户报）。
    //
    // ⇒ 源已经播完了 ⇒ 剩下的译文靠「源已结束 ⇒ 直接补播」那条路
    //    （服务端知道），**不需要**把媒体倒回去重播。
    //
    // ⚠️ **2026-11-22 实测过「倒回 0 再播」**：会把「源已结束」清掉
    // ⇒ 跟播放又从头等 ⇒ 回归第三场（「源结束后开始识别 ⇒ 全部放行
    // + source_done」）**失败** ⇒ 保持现状（不倒带）。
    if (a.ended) return;
    const p = a.play();
    if (p && typeof p.catch === "function") {
      p.catch((e) => {
        // 自动播被浏览器策略拦下（点「开始识别」是用户手势 ⇒ 正常不该
        // 发生；真发生了用户手动点一下播放键即可）。
        console.info("[qwen3-asr] 音频自动播放未执行:", e && e.name);
      });
    }
  },

  /* ------------------------------------------------------------------ *
   * 板载麦克风（设备端 ALSA → 独立采集进程）
   * ------------------------------------------------------------------ */
  async _boardRefresh() {
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/status");
      const d = await r.json().catch(() => ({}));
      if (!r.ok) return;
      this._boardState = d;
      this._boardRender();
    } catch (e) { /* best-effort */ }
  },

  _boardRender() {
    const $ = (id) => document.getElementById(id);
    const st = this._boardState || {};
    const running = !!st.running;
    const startBtn = $("asr-board-start");
    const stopBtn = $("asr-board-stop");
    const status = $("asr-board-status");
    const sel = $("asr-board-device");
    if (startBtn) startBtn.disabled = running;
    if (stopBtn) stopBtn.disabled = !running;
    if (sel) sel.disabled = running;
    if (status) {
      if (running) {
        status.textContent = `采集中（${st.device || ""} · pid ${st.pid || 0}）`;
      } else if (st.last_exit !== undefined && st.last_exit !== null) {
        status.textContent = `已停止（退出码 ${st.last_exit}）`;
      } else {
        status.textContent = "未开始";
      }
    }
    const hint = $("asr-board-hint");
    if (hint && !running && st.log) {
      hint.textContent = `采集日志：${st.log}`;
    }
  },

  async _boardLoadDevices() {
    const $ = (id) => document.getElementById(id);
    const sel = $("asr-board-device");
    if (!sel) return;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/devices");
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !Array.isArray(d.devices)) return;
      sel.innerHTML = "";
      d.devices.forEach((dev) => {
        const opt = document.createElement("option");
        opt.value = dev.name;
        opt.textContent = `${dev.name} — ${dev.description || ""}`;
        if (dev.default) opt.selected = true;
        sel.appendChild(opt);
      });
    } catch (e) { /* best-effort */ }
  },

  async _boardStart() {
    const $ = (id) => document.getElementById(id);
    const sel = $("asr-board-device");
    const device = sel ? sel.value : "";
    const status = $("asr-board-status");
    if (status) status.textContent = "启动中…";
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device: device }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (status) status.textContent = "启动失败：" + (d.detail || `HTTP ${r.status}`);
        return;
      }
      this._boardState = d;
      this._boardRender();
      // ⚠️⚠️ **板载麦 ⇒ 自动开同传**（真同传，2026-11-26）——
      // 与浏览器麦同一套逻辑（见 ``_autoStartInterpForMic``）。
      await this._autoStartInterpForMic();
    } catch (e) {
      if (status) status.textContent = "启动失败：" + e;
    }
  },

  async _boardStop() {
    const $ = (id) => document.getElementById(id);
    const status = $("asr-board-status");
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/stop",
                           { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (status) {
        status.textContent = r.ok ? "已停止" : ("停止失败：" + (d.detail || r.status));
      }
      await this._boardRefresh();
    } catch (e) {
      if (status) status.textContent = "停止失败：" + e;
    }
  },

  /**
   * 诊断钩子：把重采样器暴露给回归脚本。
   *
   * ``window.__asrLiveResample(float32, srcRate)`` → s16le 16 kHz 字节。
   * ``scripts/verify_asr_wizard.py`` 的 **V6a** 用它和 Python 的
   * ``StreamResampler`` 做**逐字节**比对 —— 「跨端一致」（清单 B3）
   * 的可执行判据就落在这里。
   */
  _installLiveHooks() {
    if (window.__asrLiveResample) return;
    window.__asrLiveResample = (f32, srcRate) => {
      const st = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: srcRate / 16000,
      };
      const a = this._resampleTo16k(new Float32Array(f32), srcRate, st);
      const b = this._resampleFlush(st, srcRate);
      const out = new Uint8Array(a.length + b.length);
      out.set(a, 0);
      out.set(b, a.length);
      return out;
    };
  },

  _busy() {
    return !!(this.snapshot && this.snapshot.busy);
  },

  _hasSource() {
    return !!(this._file || (this._path && this._path.trim()));
  },

  /** 当前来源的描述（给提示行用）。 */
  _sourceLabel() {
    if (this._file) return "本机上传：" + this._file.name;
    if (this._path) return "设备端：" + this._path;
    return "";
  },

  _syncSourceUi() {
    const $ = (id) => document.getElementById(id);
    const fileName = $("asr-file-name");
    if (fileName && this._path) fileName.textContent = "";
    // 「开始识别」只对**文件 / 视频**来源有意义 —— 实时两种来源各有各
    // 的按钮（板载采集 / 浏览器麦克风）。
    const startBtn = $("asr-start-btn");
    if (startBtn) {
      startBtn.disabled = !this._isFileSource() || !this._hasSource()
        || this._busy() || this._starting;
    }
    // ⚠️ **结果框画什么** —— 三种来源三个框（都在**页面底部**）：
    //   * 音频文件 ⇒ 「句子流」（原文 + 译文成对），见 `#asr-sentences`；
    //   * 含音轨的视频 ⇒ 「字幕同款转写框」（见 `#asr-transcript`）；
    //   * 实时（麦克风）⇒ 句子流（见 `#asr-rt-sentences`）。
    //
    // ⚠️ **三个框都在底部，但同一时刻只显示一个** —— 否则文件来源的句子
    // 会和麦克风来源的句子挤在一起（用户实测：「每个句子显示两遍」）。
    //
    // `asr-live-file` 这个类用来**藏起那两个「镜像」span**（连续文本）——
    // 否则句子流与连续文本在同一个框里出现两遍。
    const liveBox = $("asr-live");
    if (liveBox) {
      liveBox.classList.toggle("asr-live-file", this._source === "file");
      // ⚠️ **麦克风来源的排版** —— 它现在也用句子流（不是连续文本）⇒
      // 那个框也要 `white-space: normal`（见 app.css）。
      liveBox.classList.toggle("asr-live-mic", !this._isFileSource());
    }
    const sents = $("asr-sentences");
    if (sents) sents.classList.toggle("hidden", this._source !== "file");
    // ⚠️ **麦克风的句子流（2026-11-25）** —— 它从「实时识别」块搬到了
    // 页面底部 ⇒ **不再跟着那一整块一起隐藏** ⇒ 必须**单独**管显隐。
    const rtSents = $("asr-rt-sentences");
    if (rtSents) rtSents.classList.toggle("hidden", this._isFileSource());
    // ⚠️ **「生成配音版」的按钮状态**（2026-11-26）——
    //   源变了（选了文件 / 填了路径 / 切了页签）就得重算 ⇒ 否则按钮**一直
    //   是禁用的**（用户实测：「选好音频文件，生成配音版不能点击」）。
    this._dubSyncButtons();
    // ⚠️⚠️ **两块功能按来源二选一**（2026-11-26）：
    //   * 「板载麦克风 / 浏览器麦克风」⇒ **同声传译**（边识别边念）；
    //   * 「音频文件 / 含音轨的视频」⇒ **配音导出**（产出成品文件）。
    //
    // ⇒ 两块**互斥**：文件类来源**不再**有同传（用户要求），麦克风来源
    //    **不做**导出（没有文件可导）。
    const interpBlock = $("asr-interp-block");
    if (interpBlock) {
      interpBlock.classList.toggle("hidden", this._isFileSource());
    }
    const dubBlock = $("asr-dub-block");
    if (dubBlock) {
      dubBlock.classList.toggle("hidden", !this._isFileSource());
    }
    // ⚠️ **切到文件类来源 ⇒ 同传必须停** —— 引擎是**服务端全局单例**，
    // 留着它会继续念（用户已经切走了）。
    if (this._isFileSource() && this._interp.running) {
      this._interpStop();
    }
    // ⚠️⚠️ **合并规则**（2026-11-25）—— 同传在跑 ⇒ 只留「同传结果」一个
    // 列表（它是「实时结果」的更完整版本）⇒ 同一批句子**不显示两遍**。
    // ⇒ 显隐的**最终口径**统一在 ``_syncResultBoxes`` 里（那里还多一条
    // 「同传有内容 ⇒ 藏掉实时结果」）。
    this._syncResultBoxes();
  },

  /** 当前来源是不是「文件 / 视频」（决定「开始识别」按钮是否可用）。 */
  _isFileSource() {
    return this._source === "file" || this._source === "video";
  },

  /**
   * 按**路径后缀**判定来源类型（"video" / "file"）。
   *
   * 与后端 ``api.py::_source_kind`` **同口径**（同一份扩展名表）——
   * 前端判「要不要放视频播放器」，后端判「这批轮次属于哪种来源」；
   * 两边不一致 ⇒ 字幕 / 转写框的来源守卫和实际媒体对不上（U15 串台坑
   * 的同源版本）。
   *
   * 只认后缀、**不做内容嗅探**（与后端一致）：嗅探要解容器，慢，而且对
   * 「无音轨的视频」会判错。
   */
  _kindFromPath(path) {
    const ext = (/\.([a-z0-9]+)$/i.exec(String(path || "").trim()) || [])[1] || "";
    return ASR_VIDEO_EXTS.has(ext.toLowerCase()) ? "video" : "file";
  },

  /**
   * 按路径推断出的类型**切来源页签**。
   *
   * ⚠️ 只在**推断结果与当前页签不同**时才切 —— 同类型重复切会触发
   * ``_onSourceChanged()``（清字幕 / 作废时间轴），在打字过程中反复
   * 触发会把正在看的字幕清掉。
   */
  _applyKindFromPath(path) {
    const kind = this._kindFromPath(path);
    if (kind !== this._source) {
      this._switchSource(kind);
    } else {
      this._syncSourceUi();
      this._syncVideoPlayer();
    }
    return kind;
  },

  /**
   * 流式参数**不再由前端传**。
   *
   * 分块长度 / 每块 token 上限 / 回退 token / 前几轮不提交这四个值
   * 等于 C++ demo 的 ONLINE_CHUNK_MS / ONLINE_MAX_NEW_TOKENS /
   * ONLINE_ROLLBACK_TOKENS / ONLINE_ROLLBACK_ROUNDS —— 它们是
   * 「与 C demo 对齐」的**内部契约**，不是用户该调的东西（每一段音频
   * 都要解析，没有「跳过某些段」这回事），所以既不在向导里、也不在
   * 功能页里露出来。
   *
   * 不传 → SDK 用自己的 ONLINE_*_DEFAULT（1.0 / 50 / 5 / 4，与 C demo
   * 一致）。**单一真值源留在 SDK**，前端不再复制一份常量。
   */
  _applyMode() {
    const mode = document.getElementById("asr-mode")?.value || "single";
    const rounds = document.getElementById("asr-rounds");
    // ⚠️ 「实时结果」框本身要不要藏 ⇒ 走 ``_syncLiveBox``（那里还有
    //    「框里有没有内容」这条）。
    this._syncLiveBox();
    if (rounds) rounds.classList.toggle("hidden", mode !== "rounds");
    // 面板刚露出来 → 把这段时间攒下的轮次补上（隐藏时是**不重建**的）
    if (mode === "rounds") this._renderRounds();
    // 视频来源下「文字显示框」= 字幕同款转写框 → 切模式时要重画。
    this._renderTranscript();
  },

  /**
   * 「实时结果」框（``#asr-live``）**本身**要不要藏。
   *
   * ⚠️ **两个条件**：
   *   ① 显示模式不是「实时结果」（那就是「识别过程」⇒ 归 ``#asr-rounds``）；
   *   ② 框里**没有内容**了 —— 配音导出 / 同传有内容 ⇒ 里面的句子流被藏 ⇒
   *      整个框空着 ⇒ **一起藏**，否则屏幕上留一个**空白大框**
   *      （看着像页面坏了）。
   *
   * ⚠️ ``_applyMode`` 与 ``_syncResultBoxes`` **都要调它** —— 两处都改这个
   *    框的显隐 ⇒ 口径必须**只有一份** ⇒ 否则一边藏一边露。
   */
  _syncLiveBox() {
    const live = document.getElementById("asr-live");
    if (!live) return;
    const mode = document.getElementById("asr-mode")?.value || "single";
    const shown = ["asr-sentences", "asr-rt-sentences", "asr-transcript"]
      .some((id) => {
        const el = document.getElementById(id);
        return !!(el && !el.classList.contains("hidden"));
      });
    live.classList.toggle("hidden", mode !== "single" || !shown);
  },

  /**
   * 后端**不再忙**了 ⇒ 最后一行也定稿了 ⇒ 把它（和还没翻的）要过来。
   *
   * ⚠️ 正常流程里「最后一行的定稿」是识别结束的 ``done`` 帧触发的
   * （见 :meth:`_trRequestLines`）。刷新页面后那个帧**随旧连接一起没了**
   * ⇒ 最后一行永远等不到译文 ⇒ 靠「后端不忙了」这个信号补上。
   *
   * ⚠️ 反复调用**没有副作用** —— :meth:`_trRequestLines` 会跳过「文本没变」
   * 的行 ⇒ 只有真的有新内容才会再要一次。
   */
  _trMaybeFinalizeLines() {
    if (this._trMode !== "line") return;
    if (!this.snapshot || this.snapshot.busy) return;
    if (!this.rounds || !this.rounds.length) return;
    this._trRequestLines(true);
  },

  async _probeCap() {
    let data;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/", { cache: "no-store" });
      if (!r.ok) return null;
      data = await r.json();
    } catch (_) {
      return null;
    }
    this._applySnapshot(data);
    const banner = document.getElementById("asr-unavailable");
    if (banner) {
      // capability 未启用时后端会返回 502；这里用 model_id 判断。
      const missing = !data || !data.model_id;
      banner.classList.toggle("hidden", !missing);
    }
    return data;
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = snap;
    // 音轨在容器里的起点 —— 视频来源要靠它换算播放头 ↔ 音频时间。
    if (snap.audio_origin_sec !== undefined && snap.audio_origin_sec !== null) {
      this._audioOriginSec = Number(snap.audio_origin_sec) || 0;
    }
    const state = snap.state || "idle";
    const badge = document.getElementById("asr-state-badge");
    if (badge) {
      badge.dataset.state = state;
      badge.textContent = {
        idle: "空闲", running: "识别中", done: "完成", error: "出错", stopped: "已停止",
      }[state] || state;
    }
    const line = document.getElementById("asr-status-line");
    if (line) {
      line.textContent = snap.busy
        ? `识别中 · 第 ${snap.round_count || 0} 轮`
        : (snap.error ? "出错" : (snap.language ? `语种 ${snap.language}` : "空闲"));
    }
    const lang = document.getElementById("asr-language");
    if (lang) lang.textContent = snap.language ? `语种：${snap.language}` : "";
    // ⚠️ **「实时结果」顶上那行提示跟着语种走**（源语言 → 显示语言）
    //    ⇒ 语种是**识别出来之后**才有的 ⇒ 得在这里刷一次。
    this._syncResultBoxes();

    const startBtn = document.getElementById("asr-start-btn");
    if (startBtn) startBtn.disabled = !this._hasSource() || snap.busy || this._starting;
    const stopBtn = document.getElementById("asr-stop-btn");
    if (stopBtn) stopBtn.disabled = !snap.busy;

    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.toggle("hidden", this.rounds.length > 0 || !!snap.last_result);

    this._updateHint();
    this._syncSourceUi();
    this._syncLiveUi();
    // ⚠️ **行级翻译**：「最后一行的定稿」正常是靠识别结束的 ``done``
    // 帧触发的 —— 刷新页面后那个帧**随旧连接一起没了** ⇒ 最后一行永远
    // 等不到译文 ⇒ 靠「后端已经不忙了」这个信号补上（见
    // ``_trMaybeFinalizeLines``）。
    this._trMaybeFinalizeLines();
    if (typeof DemoGuard !== "undefined") {
      // 实时会话在跑时，快照的 busy 必须是 true —— DemoGuard 靠它决定
      // 「离开这张卡要不要拦一下」。
      const snap = { ...(this.snapshot || {}) };
      if (this._live.active) {
        snap.busy = true;
        snap.state = "running";
      }
      DemoGuard.report("qwen3-asr", { session: snap });
    }
  },

  /**
   * 实时区块的显隐 —— 由后端的 feature flag 决定。
   *
   * flag 关掉时后端返回 404，前端**整块隐藏**（B11：旧路径零影响）。
   * 快照里带 ``live_enabled``，前端不用再单独探测一个端点。
   */
  _syncLiveUi() {
    const box = document.getElementById("asr-live-block");
    if (!box) return;
    const enabled = !!(this.snapshot && this.snapshot.live_enabled);
    // flag 关掉 → 整块隐藏；当前来源是文件 / 视频 → 也隐藏（实时区块
    // 只在选了实时来源时出现）。
    box.classList.toggle("hidden", !enabled || this._isFileSource());
    const btn = document.getElementById("asr-mic-btn");
    if (btn) {
      btn.disabled = !enabled || this._live.active || this._live.starting;
      btn.textContent = this._live.active ? "结束实时识别" : "开始实时识别";
    }
    const stop = document.getElementById("asr-mic-stop-btn");
    if (stop) stop.disabled = !this._live.active;
    const boardStart = document.getElementById("asr-board-start");
    if (boardStart && this._boardState) {
      boardStart.disabled = !enabled || !!this._boardState.running;
    }
    // 非安全上下文 → 浏览器不给麦克风接口。提前说清楚，别让用户点了
    // 才发现（点了也只会看到一句没头没尾的「已停止」）。
    const micHint = document.getElementById("asr-mic-hint");
    if (micHint) {
      const insecure = !window.isSecureContext;
      micHint.classList.toggle("hidden", !insecure);
      if (insecure) {
        micHint.textContent =
          "当前地址不是安全上下文（" + location.protocol + "//"
          + location.hostname + "），浏览器不会提供麦克风接口。"
          + "请改用 https://" + location.hostname + ":8443 访问，"
          + "或改用「板载麦克风」。";
      }
    }
  },

  _liveStatus(text) {
    const el = document.getElementById("asr-live-status");
    if (el) el.textContent = text || "";
  },

  _liveGapHint(msg) {
    const el = document.getElementById("asr-live-gap");
    if (!el) return;
    if (msg && msg.dropped_chunks) {
      el.textContent =
        `⚠ 有音频丢失（${msg.dropped_chunks} 块），识别结果可能有缺口`;
    } else {
      el.textContent = "⚠ 有音频丢失，识别结果可能有缺口";
    }
    el.classList.remove("hidden");
  },

  _liveClearGap() {
    const el = document.getElementById("asr-live-gap");
    if (el) el.classList.add("hidden");
  },

  _liveRender() {
    const L = this._live;
    const c = document.getElementById("asr-rt-committed");
    const u = document.getElementById("asr-rt-unfixed");
    const head = L.segments.length ? L.segments.join(" ") + " " : "";
    if (c) c.textContent = head + (L.committed || "");
    if (u) u.textContent = L.unfixed || "";
    // 句子流（原文 + 译文成对）—— 麦克风的句子由**端点检测**给出
    // （文件 / 视频路径的句子由服务端切，见 `_renderLive`）。
    this._sentPaint();
  },

  // ------------------------------------------------------------------ //
  // 实时翻译（流式同传，第一期）
  //
  // ⚠️ 译文**沿同一条 WS** 过来（服务端 ``broadcast()``）—— 不开第二条
  // 连接，也不做「两个 WS 的帧合并」。
  //
  // ⚠️ 板端**没有 emoji / 符号字体** ⇒ 这里只用普通文字，不用 ⚠ 之类
  // 的符号（会渲染成豆腐块）。
  // ------------------------------------------------------------------ //
  /**
   * 拉目标语言清单（运行时才知道 ⇒ 不能写死在 wizard.json / template.html）。
   *
   * 向导里的下拉和功能页的下拉共用这一份 —— 两处必须同源，否则用户在
   * 向导里选了功能页没有的语言，翻译会静默失败。
   */
  async _trFetchLanguages() {
    if (this._tr.langItems) return this._tr.langItems;
    try {
      const r = await fetch("/api/qwen3-asr/translate/languages",
        { cache: "no-store" });
      if (!r.ok) return [];
      const data = await r.json();
      const items = Array.isArray(data.items) ? data.items : [];
      this._tr.langItems = items;
      return items;
    } catch (e) {
      console.warn("[qwen3-asr] 拉翻译语言列表失败:", e);
      return [];
    }
  },

  /**
   * 一个目标语言的**显示文案**：「中文名 本族名」。
   *
   * 跟右上角「简体中文」下拉一个排法（左边中文名、右边本族名）——
   * 那边是两个并排的 span，这边是原生 ``<select>`` ⇒ 只能拼成一段文字。
   *
   * ⚠️ **本族名渲染不出来时退回英文名** —— 板端没有泰语 / 天城文 / 藏文
   * 那些字体 ⇒ 本族文字是**豆腐块**（右上角那个下拉里的「印地语 / 泰语」
   * 现在就是一排方框）。⇒ 「泰语 Thai」比「泰语 ไทย（一排方框）」好看。
   */
  _trLangLabel(it) {
    const code = (it && it.id) || "";
    const en = (it && it.name) || code;
    const lab = TR_LANG_LABELS[code];
    if (!lab) return en;
    const native = _canRender(lab.native) ? lab.native : en;
    return lab.zh + " " + native;
  },

  /* ------------------------------------------------------------------ *
   * 语言标签的两个来源
   *
   * ⚠️ **源语言**是 ASR 报出来的**英文名**（"English"）⇒ 先映射回语言码
   *    ⇒ 再走 ``_trLangLabel`` ⇒ 跟三个语言下拉**同一套排法**
   *    （「英语 English」）⇒ 映射不到就原样显示（不显示成空白）。
   *
   * ⚠️ **目标语言**直接取那个语言下拉里那一项的文案 ⇒ 保证跟用户
   *    在下拉里看到的**一模一样**（不用再维护第二份标签表）。
   * ------------------------------------------------------------------ */
  _srcLangLabel(raw) {
    const name = String(raw || "").trim();
    if (!name) return "";
    const code = TR_LANG_BY_EN[name.toLowerCase()];
    if (code) return this._trLangLabel({ id: code, name: name });
    return name;
  },

  /** 某个语言下拉里那一项的文案（选不中就退回标签表）。 */
  _selLangLabel(selId, code) {
    const c = String(code || "").trim();
    if (!c) return "";
    const sel = document.getElementById(selId);
    if (sel) {
      const opt = Array.from(sel.options || [])
        .find((o) => o.value === c);
      if (opt) return (opt.textContent || "").trim();
    }
    // ⚠️ **下拉可能还没填好** —— ``_dubLoadOptions`` 之类是**异步**的（要
    //    拉接口）⇒ 进页面第一次渲染时它可能还是空的 ⇒ 退回**标签表**
    //    （同一份 ``TR_LANG_LABELS`` ⇒ 排法与下拉一致，不会漂）。
    const lab = TR_LANG_LABELS[c];
    if (lab) {
      const native = _canRender(lab.native) ? lab.native : c;
      return lab.zh + " " + native;
    }
    return c;
  },

  _dubTgtLangLabel(code) {
    return this._selLangLabel("asr-dub-target", code);
  },

  /**
   * 「实时结果」框顶上那一行提示（源语言 → 显示语言）。
   *
   * ⚠️ **为什么要它**（2026-11-27 用户要求）—— 这个框原来**只有句子、
   *    没有标题** ⇒ 用户得自己猜哪行是原文、哪行是译文、译文是哪种语言；
   *    跟「配音导出结果」那个带标题的面板并排时更乱 ⇒ 现在两处**同一个
   *    口径**。
   *
   * ⚠️ **实时翻译关着** ⇒ 只标源语言（那一行就是原文，没有译文）。
   * ⚠️ **视频来源不标** —— 那个框里显示的是**字幕转写框**（它自己有标题
   *    「字幕文本 · 与画面字幕同步，可上拉回看」）⇒ 两个标题挨着会重复。
   */
  _syncLiveCap() {
    const cap = document.getElementById("asr-live-cap");
    if (!cap) return;
    // ⚠️ **句子流被藏起来 ⇒ 提示行也要藏** —— 「配音导出有内容」或「同传有
    //    内容」时，句子流是隐藏的（合并规则）⇒ 只剩一个「有标题、没内容」
    //    的框 ⇒ 一起藏。
    const sents = document.getElementById("asr-sentences");
    const rtSents = document.getElementById("asr-rt-sentences");
    const shown = (sents && !sents.classList.contains("hidden")) ||
                  (rtSents && !rtSents.classList.contains("hidden"));
    const isVideo = this._source === "video";
    const src = this._srcLangLabel(
      (this.snapshot && this.snapshot.language) || "");
    const trOn = !!this._tr.enabled;
    const tgt = trOn
      ? this._selLangLabel("asr-tr-target", this._tr.targetLang) : "";
    let text = "";
    if (!isVideo) {
      if (trOn) {
        text = "实时翻译结果 · 一句一句（源语言：" + (src || "未知") +
          " → 显示语言：" + (tgt || "未知") + "）";
      } else if (src) {
        text = "识别结果 · 一句一句（源语言：" + src + "）";
      }
    }
    if (cap.textContent !== text) cap.textContent = text;
    cap.classList.toggle("hidden", !text || !shown);
  },

  /**
   * 行级翻译：把**定稿的行**送去翻译。
   *
   * 「定稿」= 这一行的文字**不会再变了** —— 后面还有内容 ⇒ 它不会再长。
   * ⚠️ **最后一行**还可能在长（模型还在往里加词）⇒ 等它定稿。
   *
   * ⚠️ **为什么不等「整句」**：句子的边界是**后端**按标点切的，字幕行的
   * 边界是**前端**按显示宽度切的 —— 两者对不上。等句定稿 ⇒ 译文要等
   * 3~9 s ⇒ 字幕已经翻过去两三行了 ⇒ 看着像「不是实时翻译」。
   *
   * ``force`` —— 识别结束（``done``）时传 true：那时最后一行也定稿了。
   *
   * ⚠️ **每轮最多发 ``TR_REQ_BATCH`` 个** + **播放头附近的先发** ——
   *
   * 一集 98 分钟的访谈有 **~1477 行** ⇒ 「打开实时翻译」时如果一次性全要，
   * 服务端队列（当时上限 32）瞬间爆掉，而它**丢最旧的** ⇒ 丢掉的正是**播放头
   * 附近的那几行** ⇒ 画面字幕上一直**没有译文**（用户报：「再勾选实时翻译，
   * 字幕上没有实时翻译」）。
   *
   * ⇒ 现在：① 离播放头越近的越先要（屏幕上那一行 **1 秒内**就有译文）；
   *   ② 每轮最多发 ``TR_REQ_BATCH`` 个 ⇒ 队列永远不满 ⇒ 不会丢。
   */
  _trRequestLines(force) {
    if (this._trMode !== "line") return;
    // ⚠️⚠️ **麦克风来源不走前端驱动**（2026-11-26）——
    //
    // ⚠️ **为什么**：麦克风的字幕翻译是**服务端驱动**的（实时会话结束一句
    // 就自己送去翻译）⇒ 前端这套「按行要译文」在麦克风下**没有对应的行**
    // ⇒ 只会**空转**（每 600 ms 跑一轮，什么也做不了）。
    //
    // ⇒ 麦克风 ⇒ 字幕由服务端推 ⇒ 前端**不主动要**。
    // ⇒ 语音播报走**同传**（``_autoStartInterpForMic`` 自动开）。
    const src = this._source || "file";
    if (src === "board" || src === "browser") return;
    if (!this._tr.enabled) return;
    // ⚠️ **翻译模型没就绪 ⇒ 别要** —— 服务端会回「翻译模型未启用」
    //    （``status=unavailable``）⇒ 白白占队列、还把「失败」计数刷上去。
    //    ⇒ 模型就绪后 ``_trRefresh`` 会把还欠着的行补要一次。
    if (!this._tr.modelReady) return;
    // ⚠️ **全局闸门 + 背压**（见 ``_trReqGateAt`` / ``TR_QUEUE_SOFT_MAX``
    //    的说明）—— **限速必须放在函数内部** ⇒ 「round 帧驱动」再怎么
    //    频繁调用也突破不了这个上限。
    const now = Date.now();
    if (now - (this._trReqGateAt || 0) < TR_REQ_TICK_MS) return;
    if ((this._trQueueDepth || 0) > TR_QUEUE_SOFT_MAX) {
      // ⚠️ **队列太深 ⇒ 先别塞**（等它消化）—— 这就是**背压**。
      // ⇒ 不消耗闸门：队列一浅下来就能立刻接着发。
      return;
    }
    this._trReqGateAt = now;
    const rounds = this.rounds || [];
    if (!rounds.length) return;
    const lines = this._buildSubTimeline().lines;
    if (!lines.length) return;
    // 每个 epoch 的最后一行 = 还「在长」的那行 ⇒ 其余的都定稿了
    const lastOfEpoch = new Map();
    for (let i = 0; i < lines.length; i++) {
      lastOfEpoch.set(this._trEpochOf(lines[i]), i);
    }
    // ⚠️ **还在长的 epoch** = 轮次里**最后**那个。拖动重开识别会追加新的
    // epoch ⇒ 老那个已经结束 ⇒ 它的末行也定稿了（不然那一行永远没译文）。
    const lastEpochIdx = this._splitEpochs(rounds).length - 1;
    // ⚠️ **按「离播放头多远」排序** ⇒ 屏幕上那一行先要。
    const order = this._trLineOrder(lines);
    let sent = 0;
    for (const i of order) {
      if (sent >= TR_REQ_BATCH) break;
      const ln = lines[i];
      if (!ln || !ln.id) continue;
      const slot = this._trSlotOf(ln);
      const text = (ln.words || []).map((w) => w.text).join("").trim();
      if (!text) continue;
      // ⚠️ 文本**没变** ⇒ 不用重翻（行 id 变了但只是标点/空格 ⇒ 译文一样）
      const prev = this._trLineReq.get(slot);
      if (prev !== undefined && this._trNormText(prev.text) === this._trNormText(text)) {
        // ⚠️ **已经要过同样的文本** ⇒ 三种情况要分开看：
        //
        // ① 译文**已经回来了**（``done``）或**根本不用翻**（``skipped``）
        //    ⇒ 不用重翻；
        // ② 还在**路上**（没超时）⇒ 等；
        // ③ **没回来**（服务端拒收 / 队列满了把它丢了 / 网络失败）**或**
        //    **回来了但是翻失败**（``failed`` / ``unavailable``）⇒ 等够了
        //    就**重要一次** —— 不重要的话这一行**永远没有译文**
        //    （用户报：「勾着实时翻译，字幕却只有中文」）。
        if (!this._trLineNeedRetry(slot)) continue;
        if (now - (prev.at || 0) < TR_REQ_STALE_MS) continue;
        // ⚠️⚠️ **重试封顶**（2026-11-26）—— 同一行重试超过
        // :data:`TR_REQ_MAX_ATTEMPTS` 次 ⇒ **不再要** ⇒
        // ⇒ 「翻不出来」的那一行不会把翻译模型的算力**烧光**
        // （实测：同 7 句话被无限重试了几百次 ⇒ ASR/TTS 抢不到算力
        //   ⇒ 译文越落越远 ⇒ 音频跟不上字幕）。
        // ⇒ 用户想再试 ⇒ 把「实时翻译」关掉再打开（记账清零）。
        if (Number(prev.tries || 1) >= TR_REQ_MAX_ATTEMPTS) continue;
        // ⇒ 过期 ⇒ 下面重新要一次（``_trRequestLine`` 会覆盖记账）
      }
      const ep = this._trEpochOf(ln);
      const epIdx = parseInt(ep, 10);
      // 定稿？—— ① 它的 epoch 里它后面还有行；② 它的 epoch 已经结束
      // （后面还有别的 epoch 在长）；③ 识别结束了
      const closed = force
        || (lastOfEpoch.get(ep) > i)
        || (!isNaN(epIdx) && epIdx < lastEpochIdx);
      if (!closed) continue;
      this._trRequestLine(ln, slot, text);
      sent++;
    }
  },

  /**
   * 行要译文的**优先级顺序**：**离播放头越近的越先来**。
   *
   * ⚠️ **为什么不能按时间顺序**：一集 98 分钟的访谈有 ~900 行 ⇒ 全部要一遍
   * 要 900 次请求 ⇒ 服务端队列（32）爆掉、**丢最旧的** ⇒ 丢的正是**屏幕
   * 上那一行** ⇒ 画面字幕一直没有译文（用户报：「再勾选实时翻译，字幕上
   * 没有实时翻译」）。
   *
   * ⇒ 改成「从当前行往两边扩」⇒ 屏幕上那 3 行**第一批**就要到 ⇒ 1 秒内
   *    出译文；其余的行在后面的轮次里慢慢补。
   */
  _trLineOrder(lines) {
    const n = (lines && lines.length) || 0;
    if (!n) return [];
    // ⚠️ **按「这一行离播放头有多远」排序** —— 而不是「从当前行往两边扩」。
    //
    // 为什么换：拖动进度条之后，**播放头常常不在已识别的区间里**
    // （识别还没跟过去；或识别只跑到播放头前面）⇒ 「当前行」没有意义 ⇒
    // 旧写法退化成「**从头排**」⇒ 前面几百行排在新位置前面 ⇒ 播放头附近
    // 那几行要等**几十秒**才拿到译文 ⇒ 用户看到「勾着实时翻译，字幕却
    // 只有中文」（用户报）。
    //
    // ⇒ 一律按**距离**排 ⇒ 最近的先要 ⇒ 屏幕上的那几行永远排在最前面
    //    （不管播放头在不在区间里）。
    const video = document.getElementById("asr-video");
    const t = (video ? (video.currentTime || 0) : 0)
      - (this._audioOriginSec || 0);
    const order = [];
    for (let i = 0; i < n; i++) {
      const ln = lines[i];
      const s = (ln && ln.start) || 0;
      const e = (ln && ln.end) || 0;
      // 距离 = 播放头到这一行**时间区间**的间隔（区间内 = 0）。
      const d = (t < s) ? (s - t) : ((t > e) ? (t - e) : 0);
      order.push({ i: i, d: d });
    }
    order.sort((a, b) => (a.d - b.d) || (a.i - b.i));
    return order.map((x) => x.i);
  },

  /**
   * 一行的**有效结束时间**。
   *
   * ⚠️ 有些行会塌成**零时长**（``start == end``）：模型的锚点表整段只剩
   * 一个点时（见 :meth:`_epochTable`），这一段的所有词都拿到同一个时间
   * ⇒ 行的开始和结束是同一个数。
   *
   * 实测：视频第 35~65 s 那一段的整段文本只有「有风。」三个字，锚点表
   * 只有一条 ⇒ 这一行是 ``[36.02, 36.02]``。
   *
   * ⇒ 判「区间包含播放头」时必须给它一个**最小占位**，否则这一行
   * **永远不会被显示**（没有任何播放头能落进 ``[36.02, 36.02]``）。
   */
  _subLineEnd(ln) {
    const s = (ln && ln.start) || 0;
    const e = ln && ln.end;
    if (typeof e !== "number" || !isFinite(e)) return s + SUB_MIN_LINE_SEC;
    return e > s ? e : s + SUB_MIN_LINE_SEC;
  },

  /**
   * 一次扫描同时算出两件事：
   *   * ``cur``  —— **区间包含播放头**的那一行（落在洞里 = -1）；
   *   * ``prev`` —— 最后一行「已经开始过」的行（洞里 = 洞**前**那一行）。
   *
   * ``prev`` 是给「洞里还能显示什么」用的：洞里没有当前行，但**刚说完**
   * 的那一行还应该按 ``SUB_PREV_HOLD_SEC`` 淡出，而不是立刻消失。
   */
  _subPick(lines, t) {
    const out = { cur: -1, prev: -1 };
    if (!lines || !lines.length) return out;
    if (t === undefined || t === null) {
      const video = document.getElementById("asr-video");
      if (!video) return out;
      t = (video.currentTime || 0) - (this._audioOriginSec || 0);
    }
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const s = ln.start;
      // 后面的行只会更晚 ⇒ 到这儿就没必要再看了
      if (s > t + SUB_CUR_TOL_SEC) break;
      if (s <= t) out.prev = i;
      const e = this._subLineEnd(ln);
      // 区间包含播放头（两端各留一点容差）
      if (t >= s - SUB_CUR_TOL_SEC && t <= e + SUB_CUR_TOL_SEC) out.cur = i;
      // ⚠️ 多个候选时**后一个覆盖前一个** ⇒ 取「开始时间最近」的那行
      // （两行相接处提前 0.25 s 换行 ⇒ 不会闪）。
    }
    return out;
  },

  /**
   * **当前行**的下标 = 时间区间**包含播放头**的那一行。
   *
   * ⚠️ 判据**不是**「最后一个 ``start ≤ 播放头`` 的行」—— 那个判据在
   * 时间轴上有**洞**的时候，会把**洞之前**的那一行永远钉在屏幕上。
   *
   * 实测（98 分钟对谈视频，**剔掉拖动造成的洞**之后的连续识别段）：
   *   * 每个 epoch 的边界都有约 **2 s** 的洞（服务端把每段的首尾各
   *     丢掉了 1 秒，见 ``_epochTable``）；
   *   * 偶尔整段塌掉（锚点表只剩一个点）⇒ 那一段有 **7~9 s** 没有字幕；
   *   * 老判据下画面上那行「落后」最久 **29.75 s**（开头 171 s 那一段里
   *     **23.1%** 的时间落后超过 1 秒）；另一段 59 s 的最久 **8.02 s**、
   *     占 **14.2%**；干净的长段（1000 s / 601 s）几乎为零。
   *
   * ⇒ 用户看到的现象就是「字幕比声音更慢」：画面上钉着一句**已经说完**
   * 的话，声音早就走到别处去了。
   *
   * ⇒ 现在改成「**这一刻**该显示哪一行」：区间包含播放头的那一行；
   * 落在洞里 ⇒ **没有当前行**（由调用方决定淡出还是清空）。
   */
  _curSubIndex(lines, t) {
    return this._subPick(lines, t).cur;
  },

  /** 一行属于哪个 epoch（行 id 的第一段就是 epoch 下标）。 */
  _trEpochOf(ln) {
    const s = (ln && ln.id) || "";
    const i = s.indexOf(":");
    return i < 0 ? s : s.slice(0, i);
  },

  /**
   * 一行的**槽位号** = ``epoch 下标 : 段内行号``（行 id 的前两段）。
   *
   * ⚠️ **不用整个行 id 当钥匙** —— 行 id 里含**文本**和**时间** ⇒ 模型
   * 改一个词、或者排版挪一下时间，行 id 就变了 ⇒ 译文会「对不上行」
   * （行上没了译文）⇒ 还得重翻一遍（实测 7 行发了 11 次请求）。
   *
   * ⇒ 用**槽位号**：文本改了它也不变 ⇒ 译文一直挂在那儿 ✓。
   */
  _trSlotOf(ln) {
    const s = (ln && ln.id) || "";
    const i = s.indexOf(":");
    if (i < 0) return s;
    const j = s.indexOf(":", i + 1);
    return j < 0 ? s : s.slice(0, j);
  },

  /** 比文本用：去掉空白和标点（模型改标点不该触发重翻）。 */
  _trNormText(text) {
    return String(text || "").replace(/[\s.,!?;:'"、，。！？；：]/g, "");
  },

  /**
   * 认领这批轮次的**来源**，并连带认领**翻译的单位**。
   *
   * ⚠️ **两者必须一起认领** —— 视频来源的行级翻译是**前端驱动**的
   * （只有前端知道行是怎么排的，见 :meth:`_trRequestLine`）。
   *
   * ⚠️ **为什么不能只看来源**：「刷新页面时识别正在跑」这个场景下，
   * 本地**没走过** :meth:`start` ⇒ ``_trMode`` 还是默认的 ``"sentence"``
   * ⇒ 行级翻译**一次都不会发起**；而后端那次识别是按
   * ``translate_mode=line`` 跑的（**不做**句子级翻译）⇒
   * **刷新后一句译文都没有**（改行级之前是有的：句子级翻译在后端做，
   * 刷新后新订阅者照样收得到）。
   *
   * ``force`` —— **服务端说了算**时用（刷新恢复：服务端说这是视频就是
   * 视频，前端猜不得）；否则**只认领一次**（识别中途切来源时，轮次仍
   * 属于**原来源**，覆盖会把译文挂到错的行上）。
   */
  _claimRoundsSource(kind, force) {
    if (!force && this._roundsSource) return;
    this._roundsSource = kind;
    this._trMode = (kind === "video") ? "line" : "sentence";
  },

  /**
   * 丢掉一条行级译文（模型改词了 ⇒ 旧译文是**旧文本**的）。
   *
   * ⚠️ 不丢的话「译文面板」里会**同时**留着旧版和新版 ⇒ 同一行出现两条
   * 译文（实测：7 行翻了 11 次，面板里堆了 11 条）。
   */
  _trDropLineItem(item) {
    if (!item) return;
    const pos = this._tr.items.findIndex((x) => x.index === item.index);
    if (pos >= 0) this._tr.items.splice(pos, 1);
    this._trVer++;
  },

  /**
   * **源语**认出来了**没有**。
   *
   * ⚠️ **为什么要单独判**：模型在**纯静音 / 开头还没说话**上会吐
   * ``language = "None"`` ⇒ 服务端把翻译请求拿去做时判「**认不出源语言
   * : 'None'**」⇒ 那一句**翻不出来**（译文 = 原文）⇒ **字幕上只有中文、
   * 没有译文**（用户报：「勾着实时翻译，字幕却只有中文」）。
   *
   * ⇒ 语种还没认出来就**别要** ⇒ 等它认出来（第一轮 round 帧之后）再要
   *    ⇒ 那一行的译文**不会**被这次失败**钉死**。
   *
   * @returns {string} 认出来的源语；没认出来返回空串。
   */
  _trSrcLang() {
    const s = this.snapshot || {};
    const lang = String(s.language || "").trim();
    if (!lang) return "";
    if (["none", "null", "unknown", "und", "n/a"]
        .indexOf(lang.toLowerCase()) >= 0) return "";
    return lang;
  },

  /**
   * **这一行的译文**还要不要再要一次**？
   *
   * ⚠️ **三种结局，三种处理**：
   *
   * * ``done`` —— 翻好了 ⇒ **不用**再要；
   * * ``skipped`` —— 源语 == 目标语（服务端透传）⇒ 再要也是这个 ⇒ **不用**；
   * * ``failed`` / ``unavailable`` / **压根没回来** ⇒ **要重来一次**
   *   （用户报：「那几行永远只有中文」）。
   */
  _trLineNeedRetry(slot) {
    const it = this._trLineTr ? this._trLineTr.get(slot) : null;
    if (!it) return true;                        // 压根没回来
    if (it.status === "done") return false;      // 翻好了
    if (it.status === "skipped") return false;   // 源语 == 目标语
    return true;                                 // failed / unavailable
  },

  /** 要一整行字幕的译文（走后端 → 译文走 WS 回来）。 */
  _trRequestLine(ln, slot, text) {
    if (!text) return;
    // ⚠️ **源语还没认出来 ⇒ 别要**（见 :meth:`_trSrcLang`）—— 服务端会判
    //    「认不出源语言」⇒ 那一句翻不出来 ⇒ 字幕上只有中文。
    const lang = this._trSrcLang();
    if (!lang) return;
    const seq = this._trLineSeq++;
    this._trLineIdBySeq.set(seq, slot);
    // ⚠️ 这一行**之前**要过译文 ⇒ 旧的作废（模型改词了 ⇒ 旧译文是旧文本的）
    const old = this._trLineSlot.get(slot);
    if (old && old.item) this._trDropLineItem(old.item);
    this._trLineSlot.set(slot, { seq: seq, item: null, text: text });
    // ⚠️ **记账带上时间戳 + 次数** —— 「要了但一直没回来」要能过期重试
    // （见 ``_trRequestLines`` 里的过期判定与 :data:`TR_REQ_STALE_MS`）；
    // ⚠️ **次数**用来封顶重试（:data:`TR_REQ_MAX_ATTEMPTS`）⇒
    // 「翻不出来」的行不会无限重试 ⇒ 不会把翻译模型的算力烧光。
    const prevReq = this._trLineReq.get(slot);
    const sameText = prevReq
      && this._trNormText(prevReq.text) === this._trNormText(text);
    const tries = sameText ? Number(prevReq.tries || 1) + 1 : 1;
    this._trLineReq.set(slot, { text: text, at: Date.now(), tries: tries });
    const body = {
      text: text,
      start_sec: ln.start,
      end_sec: ln.end,
      lang: lang,
      seq: seq,
    };
    fetch(API_PREFIX + "/qwen3-asr/translate/line", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json().catch(() => ({}))).then((d) => {
      // ⚠️ 服务端**拒收**（``{"ok": false, "skipped": "翻译没开"}``）⇒ 这一行
      //    **没有**译文。
      if (d && d.ok === false) {
        console.warn("[qwen3-asr] 行级翻译被拒:", d);
        // ⚠️⚠️ **拒收 = 服务端的开关是关的** ⇒ 本地那个「开着」是**陈旧**
        //    的（典型的：服务重启后服务端开关复位，而页面里的勾还是开的）
        //    ⇒ 不停手的话，这一行会**每 5 秒重试一次、永远**（实测
        //    ~67 次/分 × 6 个标签页 ⇒ 白白空转，还把「失败」计数刷爆）。
        //
        // ⇒ **服务端才是权威** ⇒ 把本地开关拉回服务端的值 ⇒ 立刻停手。
        this._trSyncEnabledFromServer(d);
        return;
      }
      // ⚠️ **队列深度**（服务端入队后的实时值）⇒ 拿它做**背压**
      //    （见 ``TR_QUEUE_SOFT_MAX``）。
      if (d && typeof d.queue_depth === "number") {
        this._trQueueDepth = d.queue_depth;
      }
    }).catch((e) => {
      // 翻译是**旁路** —— 请求失败不许打断识别
      console.warn("[qwen3-asr] 行级翻译请求失败:", e);
    });
  },

  /**
   * 句子级翻译（**音频文件**来源）：把**还没翻过**的句子补要一次。
   *
   * ⚠️ **为什么需要**：句子级翻译是**服务端在句子切出来那一刻一次性送出**
   * 的（见服务端 ``_tr_emit``）⇒ 开关关着时切出来的句子**永远不会被补翻**
   * ⇒ 用户「取消勾选 → 再勾选」之后，结果框里那些句子**永远没有译文**。
   *
   * ⇒ 重新打开开关时，前端找出「还没翻过的句子」，用同一个
   * ``/translate/line`` 端点逐句要一次（译文照样从 WS 回来 ⇒ 前端按**原文
   * 文本**配对，见 ``_renderSentences``）。
   *
   * ⚠️ **每轮最多 ``TR_REQ_BATCH`` 个** —— 同 ``_trRequestLines``（长音频
   * 几千句 ⇒ 一次全要会把服务端的翻译队列打爆 ⇒ 丢最旧的 ⇒ 谁都翻不到）。
   */
  _trRequestSentences() {
    if (this._trMode !== "sentence") return;
    if (!this._tr.enabled) return;
    // ⚠️ **翻译模型没就绪 ⇒ 别要**（同 ``_trRequestLines``）。
    if (!this._tr.modelReady) return;
    // ⚠️ **全局闸门 + 背压**（与行级**共用一个**预算 ⇒ 两种单位**加
    //    起来**也不会超速）—— 见 ``_trReqGateAt`` 的说明。
    const now = Date.now();
    if (now - (this._trReqGateAt || 0) < TR_REQ_TICK_MS) return;
    if ((this._trQueueDepth || 0) > TR_QUEUE_SOFT_MAX) return;
    this._trReqGateAt = now;
    const sents = this._sentences || [];
    if (!sents.length) return;
    // 已经有译文的句子 —— 按**原文文本**配对（与 ``_renderSentences`` 同口径）
    // ⚠️ **只算「真的翻出来了」的**（``done``）—— 翻失败的（failed /
    //    unavailable）广播出来的 text 是「原文（未翻译）」⇒ 不算它
    //    「有译文」⇒ 它才有机会**重试**（见下面）。
    const have = new Set();
    for (const it of (this._tr.items || [])) {
      const src = (it.source || "").trim();
      if (src && (!it.status || it.status === "done")) have.add(src);
    }
    let sent = 0;
    for (const s of sents) {
      if (sent >= TR_REQ_BATCH) break;
      const text = (s.t || "").trim();
      if (!text) continue;
      // 已经要过同样的文本 ⇒ 别重复要（译文可能还在路上）
      const prev = this._trSentReq.get(s.i);
      if (prev !== undefined && this._trNormText(prev.text) === this._trNormText(text)) {
        // ⚠️ **要了但一直没回来** ⇒ 等够 ``TR_REQ_STALE_MS`` 就**重要一次**
        // （同 ``_trRequestLines``：不然这一句**永远没有译文**）。
        if (have.has(text)) continue;
        if (now - (prev.at || 0) < TR_REQ_STALE_MS) continue;
      }
      if (have.has(text)) continue;
      this._trSentReq.set(s.i, { text: text, at: now });
      this._trRequestSentence(text);
      sent++;
    }
  },

  /** 要一句（音频文件来源）的译文 —— 走同一个端点，译文从 WS 回来。 */
  _trRequestSentence(text) {
    if (!text) return;
    // ⚠️ **源语还没认出来 ⇒ 别要**（见 :meth:`_trSrcLang`）。
    const lang = this._trSrcLang();
    if (!lang) return;
    const seq = this._trSentSeq++;
    const body = {
      text: text,
      start_sec: null,
      end_sec: null,
      lang: lang,
      seq: seq,
    };
    fetch(API_PREFIX + "/qwen3-asr/translate/line", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json().catch(() => ({}))).then((d) => {
      // ⚠️ 服务端**拒收** ⇒ 这一句**没有**译文。
      if (d && d.ok === false) {
        console.warn("[qwen3-asr] 句子翻译被拒:", d);
        // ⚠️⚠️ 同 :meth:`_trRequestLine` —— 拒收 ⇒ 服务端开关是关的 ⇒
        //    本地「开着」是陈旧的 ⇒ 拉回来、停手（别每 5 秒空转一次）。
        this._trSyncEnabledFromServer(d);
        return;
      }
      // ⚠️ **队列深度** ⇒ 背压（见 ``TR_QUEUE_SOFT_MAX``）。
      if (d && typeof d.queue_depth === "number") {
        this._trQueueDepth = d.queue_depth;
      }
    }).catch((e) => {
      // 翻译是**旁路** —— 请求失败不许打断识别
      console.warn("[qwen3-asr] 句子翻译请求失败:", e);
    });
  },

  /**
   * **服务端拒收了一次翻译请求** ⇒ 把本地开关拉回服务端的权威值 ⇒ 停手。
   *
   * ⚠️ **为什么必须有这个**（2026-11-26 实测）：
   *
   * * 服务端翻译没开 ⇒ 每次 ``POST /translate/line`` 都返回
   *   ``{"ok": false, "skipped": "翻译没开"}``（**200 OK**）；
   * * 前端原来只 ``console.warn`` 就 ``return`` ⇒ **既不增长 ``tries``、
   *   也不停请求** ⇒ 那几行**每 5 秒重试一次、永远**；
   * * 实测 **~67 次/分 × 6 个标签页** ⇒ 白白空转，还把「失败」计数刷爆。
   *
   * ⚠️ **典型触发场景**：**服务重启** ⇒ 服务端开关复位成关，而页面里的
   * 勾还开着（页面没刷新）⇒ 前端与服务端**不一致** ⇒ 前端一直要。
   *
   * ⇒ **服务端才是权威** ⇒ 照它说的改本地状态 ⇒ 立刻停手。
   * ⇒ 用户想再打开 ⇒ 点一下复选框（``_trSet``）⇒ 正常走服务端。
   *
   * @param {object} d 服务端的响应体（要带 ``enabled`` 字段）。
   */
  _trSyncEnabledFromServer(d) {
    // ⚠️ 服务端**没给**权威值 ⇒ 不猜（宁可继续重试，也别瞎改用户的开关）。
    if (!d || !("enabled" in d)) return;
    const srv = !!d.enabled;
    if (this._tr.enabled === srv) return;
    this._tr.enabled = srv;
    // ⚠️ **把勾选框同步回服务端的样子** —— 用户得看得见「服务端把它关了」。
    this._trSyncUi();
  },

  async _trLoadLanguages() {
    const sel = document.getElementById("asr-tr-target");
    if (!sel || this._tr.loaded) return;
    const items = await this._trFetchLanguages();
    sel.innerHTML = "";
    items.forEach((it) => {
      const opt = document.createElement("option");
      opt.value = it.id || "";
      opt.textContent = this._trLangLabel(it);
      sel.appendChild(opt);
    });
    sel.value = this._tr.targetLang;
    this._tr.loaded = true;
  },

  async _trRefresh() {
    try {
      const r = await fetch("/api/qwen3-asr/translate/status",
        { cache: "no-store" });
      if (!r.ok) return;
      const st = await r.json();
      const model = st.model || {};
      // ⚠️ **本地刚改过、服务端还没确认的开关值 ⇒ 不要用它覆盖** ——
      //
      // 这个 5 秒一次的轮询**正好落在「用户点了开关」与「服务端处理完」
      // 之间**时，读到的还是**旧值** ⇒ 把用户的选择**悄悄撤销** ⇒
      // 表现为「勾上了『实时翻译』，但服务端其实还关着」。
      //
      // ⇒ 服务端一关 ⇒ 每一次行级请求都被**拒收**
      //   （``{"ok": false, "skipped": "翻译没开"}``）⇒ 那几行**永远没有
      //   译文**（用户报：「有概率出现选中了实时翻译，但字幕只有中文」）。
      //
      // ⇒ 本地有**待确认**的开关值时，保持用户的值（等服务端确认后，
      //    ``_trSet`` 收尾会再刷新一次）。
      const pendEnabled = this._trPending
        && Object.prototype.hasOwnProperty.call(this._trPending, "enabled");
      if (!pendEnabled) this._tr.enabled = !!st.enabled;
      this._tr.targetLang = st.target_lang || this._tr.targetLang;
      // ⚠️ **队列深度** ⇒ 背压（见 ``TR_QUEUE_SOFT_MAX``）。
      // ⇒ 这条是**兜底**信号（每次请求的响应里也有一个更准的）⇒ 万一
      //    请求被拒收 / 响应丢了，这里还能把队列深度更新过来。
      if (typeof st.queue_depth === "number") {
        this._trQueueDepth = st.queue_depth;
      }
      this._tr.modelReady = !!model.running;
      this._tr.modelId = model.model_id || this._tr.modelId || "";
      this._tr.modelStatus = model.running
        ? "翻译模型就绪"
        : (model.installed ? "翻译模型未启动" : "本机没装翻译模型");
      this._trSyncUi();
      // ⚠️ **刚知道「实时翻译开着」⇒ 把还欠着的行补要一次**。
      //
      // 刷新恢复时有个竞态：``_restoreFromServer`` 可能跑在 ``_trRefresh``
      // 之前 ⇒ 那一刻 ``_tr.enabled`` 还是 false ⇒ 行级请求被跳过 ⇒
      // 刷新后一句译文都没有（这里把它补回来）。
      this._trRequestLines(false);
      this._trMaybeFinalizeLines();
    } catch (e) {
      /* 网络问题不打扰用户 */
    }
  },

  _trSyncUi() {
    const cb = document.getElementById("asr-tr-enabled");
    if (cb && cb.checked !== this._tr.enabled) cb.checked = this._tr.enabled;
    const sel = document.getElementById("asr-tr-target");
    if (sel && sel.value !== this._tr.targetLang) sel.value = this._tr.targetLang;
    const btn = document.getElementById("asr-tr-model-btn");
    if (btn) {
      btn.disabled = this._tr.busy || this._tr.modelReady;
      btn.textContent = this._tr.busy
        ? "正在启动翻译模型…"
        : (this._tr.modelReady ? "翻译模型已启用" : "启用翻译模型");
    }
    const el = document.getElementById("asr-tr-status");
    if (el) el.textContent = this._trStatusText();
  },

  _trStatusText() {
    const bits = [this._tr.modelStatus || "翻译未启用"];
    if (this._tr.enabled) {
      bits.push("实时翻译已开启");
      const n = this._tr.items.length;
      if (n) bits.push(`已译 ${n} 句`);
    }
    return bits.join(" · ");
  },

  /** 改翻译设置。返回 true = 改成功。 */
  async _trSet(patch) {
    let ok = false;
    // ⚠️ **记下「本地刚改过、还没被服务端确认」的设置** ——
    // ⇒ ``_trRefresh``（5 秒一次的轮询）就不会用服务端的**旧值**把它
    //    撤销（见那里的说明）。
    const pend = Object.assign({}, this._trPending || {});
    Object.assign(pend, patch || {});
    const at = Date.now();
    pend.at = at;
    this._trPending = pend;
    try {
      const r = await fetch("/api/qwen3-asr/translate/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch || {}),
      });
      ok = !!r.ok;
      if (!r.ok) {
        console.warn("[qwen3-asr] 设置翻译失败:", r.status);
      }
    } catch (e) {
      console.warn("[qwen3-asr] 设置翻译失败:", e);
    }
    // ⚠️ 本地这次改动**已经落地**（成功或失败）⇒ 服务端的值现在说了算 ⇒
    //    撤掉「待确认」标记（否则轮询会一直不采信服务端）。
    //
    // ⚠️ **只有最后一次改动**才撤（连点两次时，前一次收尾不能把后一次的
    //    「待确认」标记抹掉 ⇒ 否则又会被轮询撤销）。
    if (this._trPending && this._trPending.at === at) {
      delete this._trPending.enabled;
    }
    await this._trRefresh();
    return ok;
  },

  async _trEnableModel() {
    this._tr.busy = true;
    this._trSyncUi();
    try {
      // ⚠️ **慢端点** —— 起模型要等 ~30 s（模型加载完才返回）。
      const r = await fetch("/api/qwen3-asr/translate/enable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        this._tr.modelStatus = "启动失败：" + (data.detail || `HTTP ${r.status}`);
      }
    } catch (e) {
      this._tr.modelStatus = "启动失败：" + (e && e.message ? e.message : e);
    } finally {
      this._tr.busy = false;
      await this._trRefresh();
    }
  },

  // ------------------------------------------------------------------ //
  // 向导里的「翻译目标语言」
  //
  // 目标语言清单是**运行时**才有的（后端 provider 的语言表）⇒ wizard.json
  // 是静态的写不了，走宿主框架的通用机制：字段渲染完框架发
  // ``wizard:step-rendered``，卡片往自己的字段里塞选项（和 TTS 卡的音色
  // 下拉同一个套路，见 tts/static/app.js::onWizardStepRendered）。
  // ------------------------------------------------------------------ //
  async onWizardStepRendered(appId) {
    if (appId !== "qwen3-asr") return;
    // ⚠️ **同传那一步的选项要独立灌** —— 不能被下面的早退跳过：
    //    ``wizard:step-rendered`` 是**按步**触发的 ⇒ 渲染「同声传译」
    //    那一步时，翻译那一步的 select **根本不在 DOM 里** ⇒ 早退 ⇒
    //    朗读语言 / 音色的下拉会是空的。
    await this._interpWizardOptions();
    const sel = document.querySelector(
      '#wd-wizard-root-qwen3-asr [data-field-id="asr-tr-target"]');
    if (!sel) return;
    const items = await this._trFetchLanguages();
    const ids = items.map((it) => it.id);

    sel.innerHTML = "";
    // ⚠️ **「不翻译」必须是第一项**（也是默认项）—— 它是哨兵值
    // （``off``），用户明确说「不要翻译」⇒ 不能被卡片当前状态盖掉。
    const off = document.createElement("option");
    off.value = "off";
    off.textContent = "不翻译";
    sel.appendChild(off);
    items.forEach((it) => {
      const opt = document.createElement("option");
      opt.value = it.id || "";
      opt.textContent = this._trLangLabel(it);
      sel.appendChild(opt);
    });

    // 选谁：向导里之前选的 > 卡片当前真实状态 > 「不翻译」
    //
    // ⚠️ 回落**不能**用 ``this._tr.targetLang`` —— 那是卡片页的默认值
    // （"en"），跟「用户要不要翻译」无关。拿它当默认 ⇒ 用户根本没碰过
    // 下拉，也会被当成「选了英语」⇒ 白白加载一个 2.2 GB 的翻译模型。
    // 只有「实时翻译当前是开着的」才用当前目标语言当默认。
    //
    // ⚠️ 「不翻译」是一个**显式选择**，不能被卡片当前状态盖掉：
    //   wizardTarget === "off" ⇒ 一律保持 "off"（用户明确不要翻译）。
    const remembered = this._tr.wizardTarget;   // "" = 没记过；"off" = 明确不要
    let want;
    if (remembered) {
      want = (remembered !== "off" && ids.indexOf(remembered) >= 0)
        ? remembered : "off";
    } else {
      const cur = this._tr.enabled ? this._tr.targetLang : "";
      want = (cur && ids.indexOf(cur) >= 0) ? cur : "off";
    }
    sel.value = want;
    this._tr.wizardTarget = want;
    // 派发 change → 向导把值记进 state.values，否则 finish 拿不到。
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  },

  /**
   * 向导里选定了目标语言 ⇒ 把翻译模型拉起来 + 开实时翻译。
   *
   * ⚠️ **翻译是旁路，识别才是主链路** —— 模型起不来（本机没装 / 显存
   * 不够 / 卡被占）或加载超时，都**不能**拖垮识别。所以：
   *   * 失败只记一条警告，识别照常启动；
   *   * 开关在**模型就绪之后**才打开 —— 顺序反了的话，模型没起来的那些
   *     句子会被标成「翻译模型未启用」（status=unavailable），白噪音。
   *
   * 返回 true = 翻译已就绪。
   */
  async _trEnableForWizard(lang) {
    const hint = this._trWizardHint("翻译模型与识别模型同时加载（通常 30 秒左右）…");

    // 优先用 preloadExtraModels 已经**发起**的那次 —— 框架等的就是它
    // （并行：两个模型同时加载、同一块进度条里两行）。
    const pending = this._trEnablePromise;
    this._trEnablePromise = null;

    let ok = false;
    try {
      const p = pending || this._trStartModel(lang);
      await p;
      ok = true;
    } catch (e) {
      const msg = (e && (e.message || e)) || "未知原因";
      this._tr.modelStatus = "翻译模型启动失败：" + msg;
      console.warn("[qwen3-asr] 向导里启动翻译模型失败（继续启动识别）:", e);
    }
    if (hint && hint.parentNode) hint.parentNode.removeChild(hint);
    if (ok) {
      await this._trSet({ enabled: true, target_lang: lang });
    }
    await this._trRefresh();
    return ok;
  },

  /**
   * **发起**翻译模型的加载 —— **不等**它加载完。
   *
   *  返回 promise：resolve = 模型就绪，reject = 起不来。
   *
   *  ⚠️ 为什么不等：`POST /translate/enable` 是慢端点（服务端要等模型
   *  加载完才返回，~30 s）。这里 await 它的话，框架的等待就被推迟 30 s
   *  ⇒ 变回「先等 ASR、再加载翻译」的串行。
   */
  _trStartModel(lang) {
    const ctl = (typeof AbortController !== "undefined")
      ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), 150000) : null;
    return fetch("/api/qwen3-asr/translate/enable", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      signal: ctl ? ctl.signal : undefined,
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      return data;
    }).finally(() => {
      if (timer) clearTimeout(timer);
    });
  },

  /** 在向导「完成配置」的摘要里加一行提示（卡片页在向导背后，看不到）。 */
  _trWizardHint(text) {
    const root = document.getElementById("wd-wizard-root-qwen3-asr");
    const summary = root && root.querySelector(".wd-wizard-summary");
    if (!summary) return null;
    const row = document.createElement("div");
    row.className = "wd-wizard-summary-row";
    const k = document.createElement("span");
    k.textContent = "翻译模型：";
    const v = document.createElement("span");
    v.className = "wd-wizard-summary-val";
    v.textContent = text;
    row.appendChild(k);
    row.appendChild(v);
    summary.appendChild(row);
    return row;
  },

  /**
   * 有时间的译文（按**结束时间**排序）。
   *
   * 只有文件 / 视频路径的译文带时间（实时路径是端点切句，不按时间排）⇒
   * 画面字幕的双语显示靠它。
   */
  _trTimedSorted() {
    if (this._trTimedCache && this._trTimedVer === this._trVer) {
      return this._trTimedCache;
    }
    const out = [];
    for (const it of this._tr.items) {
      if (it.start_sec === null || it.end_sec === null) continue;
      out.push(it);
    }
    out.sort((a, b) => (a.end_sec - b.end_sec));
    this._trTimedCache = out;
    this._trTimedVer = this._trVer;
    return out;
  },

  /**
   * 第 ``i`` 条字幕行该挂**哪些**句译文（数组）。
   *
   * 规则（YouTube 的「cue 对 cue」）
   * --------------------------------
   * 译文 T 归给「**T 结束的那一刻正在显示的那一行**」—— 也就是
   * 「最后一个 开始时间 ≤ T.结束 的行」。
   *
   * 等价说法：T 的结束时刻落在 ``[本行开始, 下一行开始)`` 这个**显示窗口**
   * 里。
   *
   * ⚠️ **为什么不用「本行开始 ≤ T.结束 ≤ 本行结束」**：译文的结束时刻是
   * **后端算的**、字幕行的时间是**前端算的**，两者虽用同一套锚点算法，
   * 但后端在**切句那一刻**就把时间**冻结**了，而前端的行时间会随着最后
   * 一段（正在长的那段）继续变 ⇒ 译文的结束时刻偶尔会落在**两行之间的
   * 空隙**里。
   *
   * ⇒ 用「当时正在显示哪一行」这个判据：空隙里的那一刻，屏幕上还是**上一
   * 行** ⇒ 译文归上一行 ⇒ **稳定**，不会在两行之间来回跳。
   *
   * 为什么不用序号对齐：译文按「**句**」断（后端），字幕按「**行**」断
   * （前端，YouTube 式排版）⇒ 两者的切点不一定重合 ⇒ 靠**时间**对齐最稳。
   */
  _trItemsForLine(lines, i) {
    const ln = lines[i];
    if (!ln) return [];
    // ⚠️ **「实时翻译」关着 ⇒ 不显示译文** ——
    //
    // ⚠️ **只隐藏，不删除** —— 译文还在 ``_trLineTr`` / ``_tr.items`` 里
    // ⇒ 重新勾上立刻就有（见开关的处理）⇒ 不会有一段时间「勾着开关，
    // 字幕却只有中文」（用户报）。
    if (!this._tr.enabled) return [];
    // ⚠️ **行级**（视频来源）⇒ 直接按**槽位号**对 —— 译文就是这一行的
    // 译文，**一一对应**。不用时间窗口（行的时间会随排版微调，对不上）。
    if (this._trMode === "line") {
      const it = this._trLineTr.get(this._trSlotOf(ln));
      if (!it) return [];
      if (it.status && it.status !== "done") return [];
      return [it];
    }
    const timed = this._trTimedSorted();
    if (!timed.length || i < 0 || i >= lines.length) return [];
    // 本行的**显示窗口** = [本行开始, 下一行开始)
    const nextStart = (i + 1 < lines.length) ? lines[i + 1].start : Infinity;
    // 第一个 end_sec >= 本行开始的（二分）
    let a = 0, b = timed.length;
    while (a < b) {
      const m = (a + b) >> 1;
      if (timed[m].end_sec < ln.start - 1e-9) a = m + 1; else b = m;
    }
    if (a >= timed.length) return [];
    // ⚠️ 窗口里可能挤了**不止一句**（短句连着来时）⇒ 全收，别丢。
    //
    // ⚠️ 只收 **status == "done"** 的：翻失败的（failed / unavailable），
    // 广播出来的 text 是「原文（未翻译）」⇒ 挂在字幕行下面等于**把英文
    // 又抄了一遍**，比没有译文还难看。失败的句子在「译文面板」里有交代
    // （灰色 + 原因），字幕行上不重复。
    const out = [];
    for (let k = a; k < timed.length; k++) {
      const it = timed[k];
      if (it.end_sec >= nextStart - 1e-9) break;
      if (it.status && it.status !== "done") continue;
      out.push(it);
    }
    return out;
  },

  /** 一组译文的**变更标识**（判断「要不要动 DOM」用）。 */
  _trKeyOf(items) {
    if (!items || !items.length) return "";
    return items.map((it) => it.index).join(",");
  },

  /** 一组译文的显示文本。 */
  _trTextOf(items) {
    if (!items || !items.length) return "";
    return items
      .map((it) => it.text || it.source || "")
      .filter((s) => !!s)
      .join(" ");
  },

  /**
   * 译文刷新 ⇒ 把译文刷进**句子流**。
   *
   * ⚠️ **不再有独立的译文列表** —— 那一栏把原文又抄了一遍（与结果框逐字
   * 重复）⇒ 同一句话在屏幕上出现两遍。现在译文**挂在它自己的原文下面**
   * ⇒ 一一对应，不重复（见 `_renderSentences`）。
   *
   * ⇒ 这一块只剩**控制条**（目标语言 / 开关 / 启用翻译模型 / 状态），
   * 译文本身一律在结果框里。
   */
  _trRender() {
    this._sentPaint();      // 麦克风来源的句子流
    this._sentPaintFile();  // 文件 / 视频来源的句子流
  },

  _updateHint() {
    const hint = document.getElementById("asr-hint");
    if (!hint) return;
    const st = (this.snapshot && this.snapshot.state) || "";
    if (!this._hasSource()) {
      hint.textContent = "选择一个音频来源后点「开始识别」。模型按 1 秒一块增量解码，输出实时刷新。";
    } else if (this._busy()) {
      hint.textContent = "识别中…… 已确认的文本会实时出现，蓝色是尚未确认的部分。";
    } else if (st === "stopped" && !this._seekBusy) {
      // ⚠️ 「已停止」和「完成」要说清楚区别 —— 用户点「停止」是**提前
      // 收尾**：已识别的那部分留下了，画面也停住了（字幕不再滚）。
      hint.textContent = "已停止 —— 已识别的内容保留在下方转写框里；"
        + "要重跑点「开始识别」。";
    } else {
      hint.textContent = "点击「开始识别」重新跑一遍。";
    }
  },

  // ------------------------------------------------------------------ 实时
  /**
   * 离开页面 / 切走这张卡 → 停掉麦克风 + 停掉后端实时流。
   *
   * ⚠️ **不动 framework 的注入逻辑**（AGENTS.md 明令）—— 这里只在自己
   * 的 app.js 里挂两个监听器，不碰 ``_loadedApps`` / ``_wizardMounted``。
   *
   * 后端那条 ``DELETE /live/stop`` 由 manifest 的 ``demo_guard`` 兜底
   * （页面关闭 / 刷新时走 ``sendBeacon``）；这里负责的是**浏览器侧**
   * 的设备资源（麦克风轨道 + AudioContext）—— 那些是本浏览器的东西，
   * 服务端关不掉。
   */
  _bindLiveGuards() {
    if (this._liveGuardsBound) return;
    this._liveGuardsBound = true;
    window.addEventListener("hashchange", () => {
      const cur = (location.hash || "").replace(/^#/, "");
      if (cur !== "qwen3-asr") {
        if (this._live.active) this._liveAbort();
        // 板载采集是**设备端进程** —— 离开页面必须把它停掉，
        // 否则声卡一直被占着。
        if (this._boardState && this._boardState.running) {
          this._boardStop();
        }
      }
    });
    window.addEventListener("pagehide", () => {
      if (this._live.active) this._liveAbort();
      if (this._boardState && this._boardState.running) {
        // pagehide 里 fetch 不一定送达 → 用 keepalive 兜底
        try {
          fetch(API_PREFIX + "/qwen3-asr/live/board/stop", {
            method: "POST", keepalive: true,
          });
        } catch (e) { /* best-effort */ }
      }
    });
  },

  async _liveToggle() {
    if (this._live.active) {
      await this._liveFinish();
    } else {
      await this._liveStart();
    }
  },

  /**
   * **麦克风来源 ⇒ 自动开同传**（真同传，2026-11-26）。
   *
   * ⚠️ **用户要求**（2026-11-26）：「板载麦克风」「浏览器麦克风」这两种
   * 输入**做成真实的同传逻辑**。
   *
   * ⇒ 麦克风下**不该**还要用户再点一次「开始同传」⇒ 识别一开始 ⇒
   *   **同传就跟着开** ⇒ 「说一句 → 立刻翻译 → 立刻念出来」。
   *
   * ⚠️ **为什么不需要播放头**：麦克风没有「字幕位置」⇒ 播放时机只能是
   * 「不等播放」（``gate=now``）⇒ 译文**合成完就播** —— 这正是真人同传
   * 的行为（用户说完一句，译文几秒后接上）。
   *
   * ⚠️ **失败不打断识别** —— 同传起不来（TTS 没装 / MT 没装）⇒ 识别照样
   *   跑 ⇒ 只是没有语音播报。
   */
  async _autoStartInterpForMic() {
    // ⚠️ **只有麦克风两类**才自动开（文件 / 视频要用户点「开始同传」——
    // 那是「按字幕播」的另一种逻辑，见 ``_interpStart``）。
    const src = this._source || "file";
    if (src !== "board" && src !== "browser") return;
    // ⚠️ **已经在跑 ⇒ 不重复开**（否则会清掉已有的同传结果）。
    if (this._interp.running) return;
    try {
      await this._interpStart();
    } catch (e) {
      console.warn("[qwen3-asr] 麦克风来源自动开同传失败:", e);
    }
  },

  async _liveStart() {
    const L = this._live;
    if (L.active || L.starting) return;
    L.starting = true;
    this._liveStatus("正在打开麦克风…");
    this._syncLiveUi();
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: "browser",
          sample_rate: 16000,
          n_channels: 1,
          sample_width: 2,
          chunk_ms: LIVE_CHUNK_MS,
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        this._liveStatus("无法开始：" + (data.detail || `HTTP ${r.status}`));
        return;
      }
      L.streamId = data.stream_id || "";
      L.seq = 0;
      L.queue.length = 0;
      L.dropped = 0;
      L.serverDropped = 0;
      // ⚠️ **新一轮** —— 代号 +1、复位 ``sending``。
      //    上一轮的在途请求（如果还有）回来时看到代号变了就知道自己过期了；
      //    ``sending`` 必须复位，否则本轮的 ``_livePump()`` 会直接 return
      //    —— 没人上行 ⇒ 队列溢出 ⇒ 「有音频丢失」。
      L.gen = (L.gen || 0) + 1;
      L.sending = false;
      L.segments = [];
      L.committed = "";
      L.unfixed = "";
      L.buf = null;
      L.resampler = null;
      // 新会话 ⇒ 上一轮的译文也清掉（句序号会从头数）
      this._tr.items = [];
      this._trVer++;   // 版本号 +1 ⇒ 缓存失效
      this._trRender();
      // ⚠️ **句子流也清** —— 上一轮画出来的句子要撤掉（否则两轮混在
      // 一个框里）。只清麦克风这个框 —— 文件来源的结果**不受影响**。
      this._sentClear(["asr-rt-sentences"]);
      this._liveClearGap();
      this._liveRender();
      await this._liveOpenMic();
      L.active = true;
      this._liveStatus("识别中 · 说句话试试");
      // ⚠️⚠️ **麦克风 ⇒ 自动开同传**（真同传，2026-11-26）——
      // 「说一句 → 立刻翻译 → 立刻念出来」⇒ 用户不用再点「开始同传」。
      await this._autoStartInterpForMic();
    } catch (e) {
      // 失败原因要**留住** —— ``_liveAbort(status)`` 不会再用「已停止」盖掉它。
      this._liveStatus("无法开始：" + e);
      await this._liveAbort("无法开始：" + e);
    } finally {
      L.starting = false;
      this._syncLiveUi();
    }
  },

  /** 打开麦克风 + 建音频图。优先 AudioWorklet，ScriptProcessor 仅作回退。 */
  async _liveOpenMic() {
    const L = this._live;
    // ⚠️ **非安全上下文 → 浏览器根本不提供麦克风 API**。
    //
    // ``navigator.mediaDevices`` 只在**安全上下文**里存在：HTTPS，或
    // http://localhost / 127.0.0.1。用局域网 IP 走明文 http:// 访问时，
    // 它是 ``undefined`` —— 点「开始实时识别」什么都不会发生。
    //
    // 所以这一条要**单独报**，不能跟「浏览器不支持」混为一谈：后者要
    // 换浏览器，前者只要换访问方式（https://…:8443）或改用板载麦克风。
    if (!window.isSecureContext) {
      throw new Error(
        "当前不是安全上下文（" + location.protocol + "//" + location.host
        + "），浏览器不提供麦克风接口；"
        + "请改用 https://" + location.hostname + ":8443 访问，"
        + "或用「板载麦克风」");
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("这个浏览器不支持麦克风采集");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    L.stream = stream;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("这个浏览器不支持 WebAudio");
    // ⚠️ **不指定 sampleRate** —— 让 AudioContext 跑在设备的原生采样率
    // （通常 44.1/48 kHz），重采样由**我们自己的**线性插值做。
    //
    // 为什么不用 ``new Ctx({sampleRate: 16000})``：那样 WebAudio 会用
    // **它自己的**重采样器（带滤波和延迟），而板载路径用的是我们的
    // 线性插值 —— 两端算法不同 → 同一段录音从两端进来可能得到不同的
    // 16 kHz PCM → 文本分叉（清单 B3「跨端一致」）。
    //
    // 代价：线性插值比 WebAudio 的 sinc 重采样器**质量略差**。但板载
    // 路径本来就用它（要和 SDK 的 ``_resample_linear`` 同款），**统一
    // 算法比统一质量更重要**。
    const ctx = new Ctx();
    L.ctx = ctx;
    // 诊断钩子：浏览器**可能**拿不到 48 kHz（某些设备是 44.1 kHz），
    // 现场排查时看一眼这个值就知道重采样比是多少。
    window.__asrLiveCtx = ctx;
    if (ctx.state === "suspended") await ctx.resume();
    const src = ctx.createMediaStreamSource(stream);
    L.source = src;

    // 节点必须连到 destination 才会被音频图驱动；用 gain=0 的静音支路
    // 免得麦克风声音从扬声器放出来（啸叫）。
    const mute = ctx.createGain();
    mute.gain.value = 0;
    mute.connect(ctx.destination);

    let node = null;
    if (ctx.audioWorklet) {
      try {
        await ctx.audioWorklet.addModule(
          `${STATIC_PREFIX}/apps/qwen3_asr/capture-worklet.js`);
        node = new AudioWorkletNode(ctx, "asr-capture");
        node.port.onmessage = (ev) => {
          if (ev.data && ev.data.samples) this._liveOnSamples(ev.data.samples);
        };
      } catch (e) {
        console.warn("[qwen3-asr] AudioWorklet 不可用，回退 ScriptProcessor:", e);
        node = null;
      }
    }
    if (!node) {
      // ScriptProcessor **已废弃**，但仍是唯一回退。
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => {
        this._liveOnSamples(ev.inputBuffer.getChannelData(0));
      };
    }
    src.connect(node);
    node.connect(mute);
    L.node = node;
  },

  /** 收到一段 Float32 采样 → 电平表 + 重采样 + 攒块 + 上行。 */
  _liveOnSamples(samples) {
    const L = this._live;
    if (!samples || !samples.length) return;
    this._liveLevel(samples);
    const pcm = this._liveResample(samples);
    this._liveAppend(pcm);
  },

  /** 把 16 kHz 的 PCM 攒成 500 ms 一块并入队。 */
  _liveAppend(pcm) {
    const L = this._live;
    if (!pcm || !pcm.length) return;
    const need = LIVE_CHUNK_SAMPLES * 2;
    if (!L.buf) L.buf = pcm;
    else {
      const merged = new Uint8Array(L.buf.length + pcm.length);
      merged.set(L.buf, 0);
      merged.set(pcm, L.buf.length);
      L.buf = merged;
    }
    while (L.buf.length >= need) {
      const chunk = L.buf.slice(0, need);
      L.buf = L.buf.slice(need);
      this._liveEnqueue(chunk);
    }
  },

  /**
   * 线性插值重采样：Float32（源采样率）→ s16le 16 kHz。
   *
   * ⚠️ 与 ``app/core/audio/capture.py::StreamResampler`` **逐样本等价**
   *   —— 这是「跨端一致」（清单 B3）的地基：板载路径用 Python 的
   *   StreamResampler，浏览器路径用这里；同一段录音从两端进来必须得到
   *   **逐字节相同**的 16 kHz PCM，否则识别文本可能在边缘处分叉。
   *
   * 等价的四个要点（改了任何一条都会分叉）：
   *
   *   1. **整数域插值** —— 先把 float 还原成 s16 数值再插值
   *      （numpy 那边输入本来就是 s16 整数）；
   *   2. **全局输出索引 + carry** —— 流式相位连续
   *      （逐块独立插值会让每块相位从 0 重来 → 累积漂移）；
   *   3. **末尾外插推迟到 flush** —— 最后一个采样等收尾时才产出；
   *   4. **量化向零截断** —— numpy ``astype("<i2")`` 的语义
   *      （**不是**四舍五入）。
   *
   * 回归脚本 ``scripts/verify_asr_wizard.py`` 的 **V6a** 会把这里的输出
   * 和 Python 的输出**逐字节**比对 —— 改了这个函数必须重跑那条用例。
   */
  _resampleTo16k(samples, srcRate, state) {
    const DST = 16000;
    // 同采样率 → 直通（但仍要量化成 s16）
    if (srcRate === DST) return this._f32ToS16(samples);
    if (!state) {
      state = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: srcRate / DST,
      };
    }
    const n = samples.length;
    if (!n) return new Uint8Array(0);

    // 1) float → 整数域
    const ints = new Int32Array(n);
    for (let i = 0; i < n; i++) {
      let v = Math.trunc(Math.fround(samples[i] * 32768));
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      ints[i] = v;
    }

    // 2) carry + 新数据
    let x = ints;
    if (state.carry.length) {
      x = new Int32Array(state.carry.length + n);
      x.set(state.carry, 0);
      x.set(ints, state.carry.length);
    }
    state.totalIn += n;

    // 3) 这次能产出多少个输出采样
    //    「右邻居是真实样本」才产出 —— 末尾那一格推迟到 flush。
    const offline = _pyRoundToEven((state.totalIn * DST) / srcRate);
    let nTarget = 0;
    if (state.totalIn >= 2) {
      const jLimit = _pyFloorDiv(state.totalIn - 2, state.ratio) + 1;
      nTarget = Math.min(jLimit, offline);
    }
    const nNew = nTarget - state.outIndex;
    if (nNew <= 0) {
      state.carry = x;
      return new Uint8Array(0);
    }

    // 4) 渲染（numpy 那边是 float32 → 这里用 Math.fround 对齐）
    const baseGlobal = state.totalIn - x.length;
    const out = new Float64Array(nNew);
    for (let i = 0; i < nNew; i++) {
      const j = state.outIndex + i;
      const idx = (j * srcRate) / DST;
      const leftG = Math.floor(idx);
      const left = leftG - baseGlobal;
      const right = Math.min(left + 1, x.length - 1);
      const frac = idx - leftG;
      out[i] = Math.fround(x[left] * (1 - frac) + x[right] * frac);
    }
    state.outIndex += nNew;
    const consumedGlobal = Math.floor((state.outIndex - 1) * state.ratio) + 1;
    state.carry = x.slice(consumedGlobal - baseGlobal);
    return _quantizeS16(out);
  },

  /**
   * 重采样收尾：补上整段路径会外插出来的最后几个采样。
   *
   * 不 flush 的话浏览器喂的音频会比板载**少最后 1~2 个采样** ——
   * 文本可能因此在末尾分叉（清单 B3）。
   */
  _resampleFlush(state, srcRate) {
    const DST = 16000;
    if (!state || !state.totalIn || srcRate === DST) {
      if (state) state.carry = new Int32Array(0);
      return new Uint8Array(0);
    }
    const offline = _pyRoundToEven((state.totalIn * DST) / srcRate);
    const nNew = offline - state.outIndex;
    if (nNew <= 0 || !state.carry.length) {
      state.carry = new Int32Array(0);
      return new Uint8Array(0);
    }
    const baseGlobal = state.totalIn - state.carry.length;
    const carry = state.carry;
    const out = new Float64Array(nNew);
    for (let i = 0; i < nNew; i++) {
      const j = state.outIndex + i;
      const idx = (j * srcRate) / DST;
      const leftG = Math.floor(idx);
      const left = leftG - baseGlobal;
      const right = Math.min(left + 1, carry.length - 1);
      const frac = idx - leftG;
      out[i] = Math.fround(carry[left] * (1 - frac) + carry[right] * frac);
    }
    state.outIndex += nNew;
    state.carry = new Int32Array(0);
    return _quantizeS16(out);
  },

  _liveResample(samples) {
    const L = this._live;
    const sr = L.ctx ? L.ctx.sampleRate : 16000;
    if (!L.resampler) {
      L.resampler = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: sr / 16000,
      };
    }
    return this._resampleTo16k(samples, sr, L.resampler);
  },

  _f32ToS16(samples) {
    // ⚠️ 用 **32768** 而不是 32767：WebAudio 把 s16 采样除以 32768
    // 变成 float，这里再乘回去才能**逐位还原**原始的 s16 采样。
    // 用 32767 的话每个采样都会被压低约 3e-5 —— 幅度上看不出来，
    // 但 mel 频谱的末位比特会变，token 可能翻转（SDK 那边就是这么
    // 敏感的）。
    //
    // ⚠️ 量化用**向零截断**（Math.trunc）而不是四舍五入 —— numpy 那边
    // ``astype("<i2")`` 就是截断，两边必须一样，否则重采样结果差 1 LSB。
    const out = new Uint8Array(samples.length * 2);
    const view = new DataView(out.buffer);
    for (let i = 0; i < samples.length; i++) {
      let v = Math.trunc(Math.fround(samples[i] * 32768));
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      view.setInt16(i * 2, v, true);
    }
    return out;
  },

  _liveLevel(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / Math.max(1, samples.length));
    const db = rms > 0 ? 20 * Math.log10(rms) : -120;
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    const fill = document.getElementById("asr-meter-fill");
    if (fill) fill.style.width = pct.toFixed(1) + "%";
  },

  /** L1 背压：入队；队列满就丢**最旧**的块。 */
  _liveEnqueue(bytes) {
    const L = this._live;
    const seq = L.seq++;
    L.queue.push({ seq, bytes });
    while (L.queue.length > LIVE_MAX_PENDING) {
      L.queue.shift();          // 丢最旧
      L.dropped++;
      this._liveGapHint();
    }
    this._livePump();
  },

  /** L2 传输层：串行 ``await`` 响应 → 发送速率被推理速率钳住。 */
  async _livePump() {
    const L = this._live;
    // ⚠️ **记住本轮代号** —— 下面的每个 ``await`` 之后都可能已经换轮了
    //    （用户在推理途中点了「中止」）。过期的这一轮**不许**再动队列、
    //    **不许**再中止当前会话。
    const gen = L.gen;
    if (L.sending) return;
    L.sending = true;
    try {
      while (L.queue.length) {
        if (gen !== L.gen) return;   // 过期了 → 别再碰队列
        const item = L.queue.shift();
        try {
          const r = await fetch(API_PREFIX + "/qwen3-asr/live/chunk", {
            method: "POST",
            headers: {
              "Content-Type": "application/octet-stream",
              "X-Asr-Stream-Id": L.streamId,
              "X-Asr-Seq": String(item.seq),
            },
            body: item.bytes,
          });
          if (gen !== L.gen) return;   // 过期响应 → 丢掉，不碰当前会话
          const data = await r.json().catch(() => ({}));
          if (!r.ok) {
            this._liveStatus("上行失败：" + (data.detail || `HTTP ${r.status}`));
            if (r.status === 404 || r.status === 409) {
              await this._liveAbort();
              return;
            }
            continue;
          }
          if ((data.dropped_chunks || 0) > L.serverDropped) {
            L.serverDropped = data.dropped_chunks;
            this._liveGapHint();
          }
          this._liveStatus(
            `识别中 · 丢块 ${L.dropped} · 延迟 ${data.lag_ms || 0} ms`);
        } catch (e) {
          if (gen !== L.gen) return;   // 过期轮次的错误 → 静默
          this._liveStatus("上行失败：" + e);
        }
      }
    } finally {
      // ⚠️ **只有当前这一轮**才允许复位 ``sending`` —— 过期的那一轮如果
      //    也去复位，会把新一轮的「正在上行」状态抹掉（两个泵同时跑 ⇒
      //    音频块乱序）。
      if (gen === L.gen) L.sending = false;
    }
  },

  /** 只停浏览器侧的设备（麦克风 + AudioContext）。 */
  async _liveStopCapture() {
    const L = this._live;
    // 收尾：把重采样器末尾外插的采样补上（板载路径也 flush）——
    // 否则浏览器喂的音频比板载**少最后 1~2 个采样**。
    if (L.resampler) {
      const sr = L.ctx ? L.ctx.sampleRate : 16000;
      const tail = this._resampleFlush(L.resampler, sr);
      if (tail && tail.length) this._liveAppend(tail);
    }
    try { if (L.node) L.node.disconnect(); } catch (e) { /* noop */ }
    try { if (L.source) L.source.disconnect(); } catch (e) { /* noop */ }
    try {
      if (L.stream) L.stream.getTracks().forEach((t) => t.stop());
    } catch (e) { /* noop */ }
    try { if (L.ctx) await L.ctx.close(); } catch (e) { /* noop */ }
    L.node = null;
    L.source = null;
    L.stream = null;
    L.ctx = null;
    L.buf = null;
    L.resampler = null;
    L.active = false;
    // ⚠️ **不清空上传队列** —— 收尾（_liveFinish）要把队列里剩下的块
    // 发完再 POST /live/finish，否则最后半秒音频会丢。硬中止
    // （_liveAbort）才清队列。
  },

  /** 等上传队列排空（L2 串行 await）。 */
  async _liveDrain() {
    const L = this._live;
    for (let i = 0; i < 200; i++) {
      if (!L.sending && !L.queue.length) return;
      await new Promise((r) => setTimeout(r, 50));
    }
  },

  /** 结束（收尾）：停设备 → 等队列排空 → POST /live/finish → 拿最终文本。 */
  async _liveFinish() {
    const L = this._live;
    const streamId = L.streamId;
    if (!streamId) return;
    await this._liveStopCapture();
    // ⚠️ **先把队列发完再收尾** —— 否则最后半秒的音频还在队列里，
    // 服务端先收尾 → 尾巴丢了（文本末尾可能少几个词）。
    await this._liveDrain();
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/finish", {
        method: "POST",
        headers: { "X-Asr-Stream-Id": streamId },
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        const text = (data.text || "").trim();
        if (text) {
          // 服务端返回的是**累积**转写（端点切出的各句 + 当前句）。
          // 直接**替换** —— 各句已经通过 endpoint 帧拼过一次了，
          // 再追加就会重复。
          L.segments = [text];
          L.committed = "";
          L.unfixed = "";
          this._liveRender();
        }
        this._liveStatus(
          `已结束 · ${(data.elapsed_sec || 0).toFixed(1)} s · 丢块 ${L.dropped}`);
      } else {
        this._liveStatus("结束失败：" + (data.detail || `HTTP ${r.status}`));
      }
    } catch (e) {
      this._liveStatus("结束失败：" + e);
    }
    L.streamId = "";
    // ⚠️ **收尾之后再作废这一轮** —— 不能提前：提前作废会让在途的那次
    //    请求被当成「过期」而丢弃，队列排不空（``_liveDrain`` 会白等 10 s）。
    L.gen = (L.gen || 0) + 1;
    L.sending = false;
    this._syncLiveUi();
  },

  /** 立即中止：停设备 + DELETE /live/stop（不产最终文本）。 */
  async _liveAbort(status) {
    const L = this._live;
    const streamId = L.streamId;
    // ⚠️ **先作废上一轮**（代号 +1）—— 在途请求回来时看到代号变了，
    //    就不会再去中止当前会话、也不会再碰队列。
    L.gen = (L.gen || 0) + 1;
    // ⚠️ **复位 ``sending``** —— 它要等在途的 ``fetch`` 返回才归零，而
    //    服务端要等推理跑完才回（几百 ms～2 s）。不复位的话，下一轮
    //    「开始实时识别」的 ``_livePump()`` 会看到 ``sending`` 还是 true
    //    就直接 return —— **没人上行** ⇒ 队列（只容 2 块）溢出 ⇒
    //    「有音频丢失，识别结果可能有缺口」。
    L.sending = false;
    await this._liveStopCapture();
    L.queue.length = 0;         // 硬中止 → 队列里的块直接丢
    if (streamId) {
      try {
        await fetch(API_PREFIX + "/qwen3-asr/live/stop", {
          method: "DELETE",
          headers: { "X-Asr-Stream-Id": streamId },
        });
      } catch (e) { /* best-effort */ }
    }
    L.streamId = "";
    // ⚠️ **不要覆盖调用方给的文案** —— 失败路径会先写「无法开始：xxx」，
    // 要是这里无条件写「已停止」，用户就只看到「已停止」，完全不知道
    // 出了什么事（实测：非安全上下文时点按钮，状态行只显示「已停止」）。
    this._liveStatus(status || "已停止");
    this._syncLiveUi();
  },

  /* ------------------------------------------------------------------ *
   * 刷新页面后恢复
   *
   * 为什么必须有：轮次只存在**服务端的内存**里（``service.py::_rounds``），
   * 浏览器一刷新 ``this.rounds`` 就空了 → 视频来源的「字幕同款转写框」
   * 被清空 → 用户看到「刚才识别出来的内容全没了」。
   *
   * 归属由**服务端**说了算（``GET /rounds`` 一起返回 ``source``）：
   * 前端自己猜会把「音频文件」的轮次画到视频画面上（U15 那个串台坑）。
   * ------------------------------------------------------------------ */
  async _restoreFromServer() {
    // 拉取期间用户又发起了识别 → 丢弃这次恢复（否则旧轮次会盖掉新轮次）。
    const gen0 = this._roundGen;
    let data = null;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/rounds");
      if (!r.ok) return;
      data = await r.json();
    } catch (e) { return; }
    if (!data || this._roundGen !== gen0) return;

    const server = Array.isArray(data.rounds) ? data.rounds : [];
    const src = data.source || {};
    // 服务端什么都没有 → 不动界面（首次进入页面就是这个分支）。
    if (!server.length && !src.kind) return;

    // 服务端那份是**同一批轮次的前缀**（服务端先把轮次记进 ``_rounds``
    // 再广播给 WS → 浏览器收到的每一条服务端都已经有了）；本地多出来
    // 的是「拉取窗口内」新到的 → 合并，别覆盖也别重复。
    const merged = this._mergeRounds(server, this.rounds);

    const kind = (src.kind === "video") ? "video" : "file";
    const path = (typeof src.path === "string") ? src.path.trim() : "";
    if (path) {
      // 设备端路径 → 播放器能重新加载。浏览器上传的 File 对象**刷新后
      // 拿不回来** → path 为空，轮次 / 文本照常恢复，只是放不了片子。
      this._path = path;
      this._file = null;
      const el = document.getElementById("asr-source");
      if (el) el.value = path;
    }

    // 先切页签（会清字幕 / 作废时间轴），再摆轮次 —— 顺序反了会被
    // ``_switchSource`` 里的 ``_renderTranscript`` 覆盖掉。
    if (this._source !== kind) this._switchSource(kind);

    // 浏览器上传的文件**恢复不了**（File 对象刷新后就没了）→ 轮次和
    // 文本恢复了，但来源框是空的。把原因说清楚，别让用户对着一个空
    // 来源框发愣（来源框空了 → 「开始识别」也是灰的）。
    if (!path && src.name) {
      const hint = document.getElementById("asr-source-hint");
      if (hint) {
        hint.textContent = "已恢复上次识别的结果（" + src.name
          + "）—— 浏览器上传的文件刷新后无法恢复，要重跑请重新选择文件。";
      }
    }

    this.rounds = merged;
    // ⚠️ **句子也要恢复** —— 结果框画的是「句子流」⇒ 句子没了框就是空的
    // （用户刷新一下，刚识别出来的内容就没了）。
    //
    // ⚠️ **只恢复「音频文件」的** —— 视频来源的结果走「字幕同款转写框」
    // （那里已经有原文 + 译文）⇒ 恢复一份句子只会让两处对不上。
    if (kind === "file" && Array.isArray(data.sentences)) {
      this._sentClear(["asr-sentences"]);
      this._sentPush(data.sentences);
    }
    // 归属：**只认服务端说的那份**（file / video）。前端猜不得。
    // ⚠️ **翻译的单位也跟着认领**（视频 ⇒ 行级）—— 刷新时本地没走过
    // ``start()`` ⇒ 不认领的话行级翻译一次都不会发起 ⇒ 刷新后没有译文。
    this._claimRoundsSource(kind, true);
    this._subInvalidate();
    // ⚠️ **已识别区间要从恢复出来的轮次重建** —— ``_subInvalidate()``
    // 把 ``_subRanges`` 清空了，而它是两个判据的输入：
    //
    // * 「拖动要不要重开识别」（``_subCovered``）—— 空 ⇒ 每次拖动都
    //   重新识别一遍（白烧算力）；
    // * 「画面上能不能留字幕」（``_subRecognizedAt``）—— 空 ⇒ 刷新后
    //   字幕**永远不显示**。
    this._rebuildSubRanges(merged);
    this._roundsDirty = true;      // 轮次表整体换过 → 重建面板
    this._renderRounds();
    this._renderTranscript();

    // 单行「实时结果」（音频文件来源用；视频来源下它是隐藏的镜像 ——
    // 回归脚本要从 ``#asr-live-committed`` 读最终文本）。
    const res = this.snapshot && this.snapshot.last_result;
    const last = merged.length ? merged[merged.length - 1] : null;
    if (res && res.text) this._renderLive(res.text, "");
    else if (last) this._renderLive(last.committed || "", last.unfixed || "");

    if (this.snapshot) this._applySnapshot(this.snapshot);
    // ⚠️ **行级翻译**：刷新后前端的行级记账是**空的** ⇒ 恢复出来的这些行
    // 还得**重新要一次**译文（刷新前要过的译文随页面一起没了）。
    //
    // ⚠️ 后端**已经跑完**的话 ⇒ 最后一行的 ``done`` 帧也随旧连接一起没了
    // ⇒ 它永远不会「定稿」⇒ 得 force（``_trMaybeFinalizeLines``）。
    this._trRequestLines(false);
    this._trMaybeFinalizeLines();
  },

  /**
   * 合并「服务端恢复的轮次」与「本地已收到的轮次」。
   *
   * 去重靠 ``chunk_start_sec``（**绝对音频时间**，跨 append 也单调）：
   * ⚠️ 不能靠 ``round`` 序号 —— 拖动重开（append）会开新会话，序号
   * 从 0 重来，拿序号去重会把新的一段认成旧的。
   */
  _mergeRounds(serverRounds, localRounds) {
    const out = Array.isArray(serverRounds) ? serverRounds.slice() : [];
    const seen = new Set();
    let maxStart = -Infinity;
    for (const r of out) {
      const s = Number(r && r.chunk_start_sec);
      if (Number.isFinite(s)) {
        seen.add(s);
        if (s > maxStart) maxStart = s;
      }
    }
    for (const r of (localRounds || [])) {
      const s = Number(r && r.chunk_start_sec);
      if (!Number.isFinite(s)) { out.push(r); continue; }
      if (seen.has(s)) continue;    // 服务端已经有了 → 丢弃本地这份
      if (s < maxStart) continue;   // 服务端更新 → 本地这份是旧的
      out.push(r);
      seen.add(s);
      maxStart = s;
    }
    return out;
  },

  // ------------------------------------------------------------------ REST
  async start() {
    // 只有「文件 / 视频」来源走这条路径（实时两种来源各有各的按钮）。
    // 向导走完会直接调 start() —— 那时若当前停在实时来源上，就**切回
    // 文件来源**（向导填的是设备端路径）。
    if (!this._isFileSource()) {
      if (!this._hasSource()) return false;
      this._switchSource("file");
    }
    if (!this._hasSource() || this._starting || this._busy()) return false;
    this._starting = true;
    this.rounds = [];
    this._roundGen++;            // 新的一批轮次 → 作废在途的「刷新恢复」
    this._stopInitiated = false;  // 新的一批 → 「谁停的」重新算
    this._ignoreRounds = false;  // 新的一批 → 轮次帧照收
    // 新的一批 ⇒ 上一批的译文也清掉。
    //
    // ⚠️ **句序号会重新从 1 数**（后端每次识别换一个序号区间，见
    // ``service.py`` 的 ``_TR_FILE_INDEX_BASE``）⇒ 不清的话上一批的
    // 译文会**留在列表里**，跟这一批混在一起。
    this._tr.items = [];
    this._trVer++;   // 版本号 +1 ⇒ 缓存失效
    this._trRender();
    // ⚠️ **新的一批 ⇒ 句子流也清空** —— 句子序号从头数 ⇒ 上一批的句子
    // 不清的话会**接着**这一批往下画（两批混在一个框里）。
    this._sentClear(["asr-sentences"]);
    // ⚠️⚠️ **新的一批 ⇒ 配音导出的结果面板也收起来**（2026-11-27）——
    //    「配音导出有内容 ⇒ 藏掉实时结果」那条合并规则是**粘的**
    //    （``_dub.hasRows`` 一直是 true）⇒ 不收的话，用户点「开始识别」
    //    想看**新的**识别结果，实时结果框却**回不来**（一直被藏着）⇒
    //    看着像「点了没反应」。
    // ⇒ 收掉导出结果 ⇒ 实时结果框回来 ⇒ 两边也不再重复。
    this._dubClearResult();
    // ⚠️ **翻译的单位** —— 视频 ⇒ 逐行；音频文件 ⇒ 逐句。
    //
    // ⚠️ 视频必须是「逐行」：字幕的显示单位是「行」，译文必须**逐行**跟着
    // （YouTube 的 cue 对 cue）。句子级做不到 —— 一句跨 3 行时译文只挂得
    // 上最后那行，中间两行光秃秃，而且译文永远慢半拍 ⇒ 看着像「不是实时
    // 翻译」。
    this._trMode = (this._source === "video") ? "line" : "sentence";
    // 新的一批 ⇒ 行级翻译的记账也归零（已请求的行 / 行号 / 行译文）
    this._trLineSeq = 0;
    this._trLineReq.clear();
    this._trLineIdBySeq.clear();
    this._trLineSlot.clear();
    this._trLineTr.clear();
    // 句子级（音频文件）的记账也归零 —— 新的一批 ⇒ 句子从头数
    this._trSentSeq = 0;
    this._trSentReq.clear();
    // 记下这批轮次来自**哪个来源** —— 视频字幕靠它判断「该不该渲染」
    // （否则会拿上一来源的轮次画到当前视频上，见 _onSourceChanged）。
    this._roundsSource = this._source;
    // 轮次换了 → 字幕时间轴重建
    this._subInvalidate();
    this._renderRounds();
    this._renderLive("", "");
    this._renderTranscript();
    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.remove("hidden");
    this._applySnapshot({ ...(this.snapshot || {}), busy: true, state: "running" });

    try {
      const fd = new FormData();
      if (this._file) {
        fd.append("file", this._file, this._file.name);
      } else {
        fd.append("path", this._path);
      }
      // ⚠️ **翻译的单位** —— 视频 ⇒ **逐行**（前端逐行来要译文，译文
      // 跟那一行走 ⇒ 节奏和字幕一致）；音频文件 ⇒ 逐句（没有字幕行，
      // 译文只出现在「译文面板」里）。
      if (this._trMode === "line") fd.append("translate_mode", "line");
      // 不带 online_* 参数 → SDK 用 ONLINE_*_DEFAULT（= C demo 常量）

      // ⚠️⚠️ **媒体必须在 ``await`` 之前起播**（实测 2026-11-23）：
      //
      // 「点开始识别」这个**用户手势**只在**当前任务**里有效 → 一旦
      // ``await fetch(...)`` 把控制权交出去，手势就**过期**了 → 浏览器
      // 的 autoplay 策略会**拦下带声音的自动播放** → 媒体**根本没播**
      // → 播放头**一动不动** →「跟播放」永远等不到 → 全部句子只能靠
      // 10 秒的兜底放行 → 用户看到的是「译文慢半拍、甚至不出声」
      // （实测：journal 里整个会话只有**一次** ``interp/playhead`` 上报
      // —— 媒体压根没动过）。
      //
      // → 先起播（**同步**，还在手势里），再发识别请求。
      this._playVideoIfAny();
      this._playAudioIfAny();

      const r = await fetch(API_PREFIX + "/qwen3-asr/transcribe", {
        method: "POST", body: fd,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const detail = data.detail || `识别请求失败（HTTP ${r.status}）`;
        this._showError(detail);
        if (data.snapshot) this._applySnapshot(data.snapshot);
        return false;
      }
      if (data.snapshot) this._applySnapshot(data.snapshot);
      return true;
    } catch (e) {
      this._showError("识别请求失败: " + e);
      return false;
    } finally {
      this._starting = false;
      const startBtn = document.getElementById("asr-start-btn");
      if (startBtn) {
        startBtn.disabled = !this._isFileSource() || !this._hasSource()
          || this._busy();
      }
    }
  },

  async stop() {
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/stop", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (data.snapshot) this._applySnapshot(data.snapshot);
      // ⚠️ **画面也要停住** —— 字幕是**播放头**的渲染（YouTube 式：字幕
      // 跟着画面走，**跟识别进度无关**）。只停后端识别的话，视频还在播
      // → 字幕的 rAF 循环继续跑 → **字幕继续按播放头滚动** → 用户以为
      // 「识别还在跑」（这正是用户报的 bug）。
      //
      // 「停止」的语义 = 停下来（识别 + 同步播放）。想继续看视频可以自己
      // 按播放键 —— 那时字幕会跟着**已识别**的那部分继续走（正常）。
      this._stopInitiated = true;
      this._freezePlayback();
    } catch (e) {
      this._showError("停止失败: " + e);
    }
  },

  /**
   * 冻结画面：暂停视频 + 停掉字幕 rAF 循环。
   *
   * 字幕的驱动源是**播放头**（``_renderVideoSubtitle`` 按
   * ``video.currentTime`` 取当前行）→ 播放头不动，字幕自然停住。
   *
   * ``video.pause()`` 会派发 'pause' 事件 → ``_stopSubLoop``；这里再
   * 显式调一次是**兜底**（元素不在 / 事件没派发时也保证循环停了）。
   */
  _freezePlayback() {
    const video = document.getElementById("asr-video");
    if (video && !video.paused) {
      try { video.pause(); } catch (e) { /* noop */ }
    }
    this._stopSubLoop();
  },

  async reset() {
    // 识别**进行中**点「清空」= 停止 + 清空（后端 /reset 会把正在跑的那
    // 一轮打断，见 service.py::reset）⇒ **画面也要停**：不然视频还在
    // 播、字幕还在滚，而识别已经被清空了 —— 那就是「点停止后字幕还在
    // 滚」那个 bug 的翻版。
    if (this.snapshot && this.snapshot.busy) this._freezePlayback();

    // ⚠️ **本地先清，不等服务器** —— 点了就该立刻看到「清空了」。
    // ``POST /reset`` 要等在飞的那次 RPC 被打断才返回（实测 ~1 s），
    // 等它才清 → 用户会觉得「点了没反应」。
    this.rounds = [];
    this._roundGen++;          // 清空 → 作废在途的「刷新恢复」
    this._roundsSource = "";
    // 清空之后**忽略迟到的轮次帧**：服务端已经在清空时断了那一轮，
    // 但清空前广播出去的几帧还会到 —— 收到就又长回来。
    this._ignoreRounds = true;
    this._renderRounds();
    this._renderLive("", "");
    // 译文也一起清（服务端 /reset 也会清统计）
    this._tr.items = [];
    this._trLineTr.clear();   // 行级译文（视频来源）也一起清
    this._trLineReq.clear();
    this._trLineSlot.clear();
    this._trSentReq.clear();  // 句子级（音频文件来源）的记账也一起清
    this._trVer++;   // 版本号 +1 ⇒ 缓存失效
    this._trRender();
    // ⚠️ **句子流也清** —— DOM 与状态一起清（见 ``_sentClear``）。
    this._sentClear();
    // ⚠️ **同传结果也清**（2026-11-25）—— 「清空」= 把页面上的结果
    // 全部清掉 ⇒ 底部那一栏同传结果也得跟着清（不然它还在长）。
    this._interpClearSegs();
    this._trSyncUi();
    this._audioOriginSec = 0;
    this._subInvalidate();
    const sub = document.getElementById("asr-video-sub");
    if (sub) this._subClear(sub);
    this._transcriptClear();
    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.remove("hidden");

    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/reset", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (data.snapshot) this._applySnapshot(data.snapshot);
    } catch (e) {
      this._showError("清空失败: " + e);
    }
    // ⚠️ **再清一次译文** —— 「本地先清」与「服务端 /reset 回来」之间
    // 可能刚好到了一条译文帧（那一句已经在推理）⇒ 不再清一次，页面上
    // 就留下一行「刚清空却还在」的译文。
    //
    // 服务端那边也做了同一件事的另一半：``/reset`` 会把翻译队列的**代数**
    // +1 ⇒ 之后才回来的译文**根本不会推过来**。
    this._tr.items = [];
    this._trLineTr.clear();   // 行级译文（视频来源）也一起清
    this._trLineReq.clear();
    this._trLineSlot.clear();
    this._trSentReq.clear();  // 句子级（音频文件来源）的记账也一起清
    this._trVer++;   // 版本号 +1 ⇒ 缓存失效
    this._trRender();
    // ⚠️ **再清一次句子流** —— 「本地先清」与「服务端 /reset 回来」之间
    // 可能刚好到了一条句子帧（那一句已经在推理）⇒ 不再清一次，页面上
    // 就留下一句「刚清空却还在」的原文。
    this._sentClear();
    this._trSyncUi();
  },

  _showError(detail) {
    const hint = document.getElementById("asr-hint");
    if (hint) hint.textContent = detail;
    const pre = document.getElementById("data-qwen3-asr");
    if (pre) pre.textContent = JSON.stringify({ error: detail }, null, 2);
  },

  // ------------------------------------------------------------------ 渲染
  /**
   * 渲染「实时结果」框。
   *
   * 三种来源三种形态：
   * * **视频** ⇒ 框里是「字幕同款转写框」（`_renderTranscript` 画的，
   *   已经是「原文 + 译文」逐句成对）⇒ 这里只更新那两个**镜像** span
   *   （回归脚本从 ``#asr-live-committed`` 读最终文本）⇒ 由 CSS 隐藏。
   * * **音频文件** ⇒ 句子流（原文 + 译文成对），见 `_sentPaintFile`。
   * * **麦克风** ⇒ 它自己的框（`_liveRender`）。
   */
  _renderLive(committed, unfixed) {
    const c = document.getElementById("asr-live-committed");
    const u = document.getElementById("asr-live-unfixed");
    if (c) c.textContent = committed || "";
    if (u) u.textContent = unfixed || "";
    this._sentUnfixed = unfixed || "";
    if (this._source !== "file") return;      // 视频走转写框；麦克风走自己的框
    this._sentPaintFile();
  },

  /**
   * 收下服务端切好的句子（原文）。
   *
   * ⚠️ **只收「音频文件」来源的** —— 视频来源的结果走「字幕同款转写框」
   * （那里已经是「原文 + 译文」成对）⇒ 再收一份句子，两处内容对不上。
   */
  _sentPush(list) {
    if (this._source !== "file") return;
    if (!Array.isArray(list)) return;
    for (const s of list) {
      if (!s) continue;
      // ⚠️ ``tr`` = 这句话**真的送去翻译了**吗（服务端给的）⇒ 译文位
      // 该显示「翻译中」还是空着。
      this._sentences.push({
        i: Number(s.i) || 0,
        t: s.t || "",
        tr: !!s.tr,
      });
    }
  },

  /**
   * 画「句子流」（**音频文件 / 视频**来源）。
   *
   * 句子由**服务端**切（随 round 帧的 ``sentences`` 送来），尾巴由服务端
   * 算好（``sentence_tail``）⇒ 前端只负责**画**。
   */
  _sentPaintFile() {
    const box = document.getElementById("asr-sentences");
    if (!box) return;
    this._renderSentences(box, this._sentences, this._sentTail, this._sentUnfixed);
  },

  /**
   * 画「句子流」（**麦克风**来源）。
   *
   * ⚠️ 麦克风的句子由**端点检测**给出（``_live.segments``）—— 那是另一条
   * 路径的句子（文件 / 视频路径的句子见 `_sentPaintFile`）。
   *
   * 当前正在说的那一句**还没结束** ⇒ 它是「尾巴」（已确认部分）+ 蓝色
   * 的未确认部分。
   */
  _sentPaint() {
    const box = document.getElementById("asr-rt-sentences");
    if (!box) return;
    const L = this._live;
    const segs = L.segments || [];
    const sentences = [];
    for (let i = 0; i < segs.length; i++) {
      // ⚠️ 麦克风的句子在**端点命中时**就送去翻译了（服务端
      // ``_submit_translation``）⇒ 翻译开着就一定会来 ⇒ 可以占「翻译中」。
      sentences.push({ i: i + 1, t: segs[i], tr: !!this._tr.enabled });
    }
    this._renderSentences(box, sentences, L.committed || "", L.unfixed || "");
  },

  /**
   * 清空句子流（新一次识别 / 「清空」按钮）。
   *
   * ⚠️ **DOM 与状态要一起清** —— ``_sentRows`` 挂在 DOM 元素上（增量渲染
   * 用的行缓存）⇒ 只清数组不清 DOM ⇒ 下一轮的句子会**接着上一轮的句子
   * 往下画**（屏幕上看着像「清空没生效」）。
   *
   * ``ids`` —— 只清哪几个框（不传 = 两个都清）。两条来源各有一个框，
   * 换来源时**不该**把另一条的结果也抹掉。
   */
  _sentClear(ids) {
    this._sentences = [];
    this._sentTail = "";
    this._sentUnfixed = "";
    const list = (ids && ids.length)
      ? ids
      : ["asr-sentences", "asr-rt-sentences"];
    for (const id of list) {
      const box = document.getElementById(id);
      if (!box) continue;
      if (box._sentRows) {
        box._sentRows.forEach((row) => row.remove());
        box._sentRows.clear();
      }
      if (box._sentTail) box._sentTail.textContent = "";
      if (box._sentLive) box._sentLive.textContent = "";
    }
  },

  /**
   * 渲染「句子流」：一句一句，**原文 + 译文成对**。
   *
   * ⚠️ **译文不再单独一栏** —— 那一栏把原文又抄了一遍（与结果框逐字
   * 重复）⇒ 同一句话在屏幕上出现两次。现在译文**挂在它自己的原文下面**
   * ⇒ 一一对应，不重复。
   *
   * ⚠️ **增量渲染** —— 句子会一直增加（长音频几千句）⇒ 每帧重建整个
   * DOM 不可接受（同 ``_renderRounds`` 的教训）⇒ 新句才建节点，老句只
   * 更新译文那一行。
   */
  _renderSentences(box, sentences, tail, unfixed) {
    if (!box) return;
    if (!box._sentRows) {
      box._sentRows = new Map();
      box._sentTail = document.createElement("div");
      box._sentTail.className = "asr-sent-tail";
      box._sentLive = document.createElement("div");
      box._sentLive.className = "asr-sent-live";
      box.appendChild(box._sentTail);
      box.appendChild(box._sentLive);
    }
    // 译文按**原文**配对：服务端给的 ``source`` 就是那句原文 ⇒ 精确匹配
    // （句子序号在两次识别之间会重排，不能拿它当键）。
    //
    // ⚠️ **「实时翻译」关着 ⇒ 不配对**（只隐藏，不删除 —— 见开关的处理）
    const trBySrc = new Map();
    if (this._tr.enabled) {
      for (const it of (this._tr.items || [])) {
        const src = (it.source || "").trim();
        if (src && !trBySrc.has(src)) trBySrc.set(src, it);
      }
    }
    const on = !!this._tr.enabled;
    for (const s of (sentences || [])) {
      const key = String(s.i) + "|" + (s.t || "");
      let row = box._sentRows.get(key);
      if (!row) {
        row = document.createElement("div");
        row.className = "asr-sent";
        const src = document.createElement("div");
        src.className = "asr-sent-src";
        src.textContent = s.t || "";
        row.appendChild(src);
        const dst = document.createElement("div");
        dst.className = "asr-sent-dst";
        row.appendChild(dst);
        box.insertBefore(row, box._sentTail);
        box._sentRows.set(key, row);
      }
      const dst = row.querySelector(".asr-sent-dst");
      if (!dst) continue;
      const it = trBySrc.get((s.t || "").trim());
      let txt = "";
      let muted = false;
      if (it) {
        if (it.status === "done") {
          txt = it.text || "";
        } else if (it.status === "skipped") {
          // ⚠️ **不要把原文再抄一遍**（实测 2026-11-24，M36，用户报
          // 「每个句子显示两遍」）——
          // 「源语 == 目标语」时译文**本来就是**原文 ⇒ 抄一遍只是重复
          // 占屏（上面一行已经有原文了）⇒ 只留一句短的说明。
          //
          // ⚠️ 「没有可译内容」（只有标点的碎片）⇒ 连说明都不显示。
          txt = it.reason && it.reason.indexOf("没有可译内容") >= 0
            ? ""
            : "（" + (it.reason || "未翻译") + "）";
          muted = true;
        } else {
          txt = "（未翻译）" + (it.reason || "");
          muted = true;
        }
      } else if (on && s.tr) {
        // 译文还没到 ⇒ 先占个位（用户知道这句在等）
        //
        // ⚠️ **只给「真的送去翻译了」的句子占位**（服务端给的 ``tr``）——
        // 翻译开关可能是**识别过程中**才打开的 ⇒ 开关打开前切出来的句子
        // 永远不会有译文 ⇒ 占着「翻译中」就是骗人。
        //
        // ⚠️ 板端**没有符号字体** ⇒ 只用文字，不写省略号（会渲染成豆腐块）。
        txt = "翻译中";
        muted = true;
      }
      if (dst.textContent !== txt) dst.textContent = txt;
      dst.classList.toggle("is-muted", muted);
      dst.classList.toggle("hidden", !txt);
    }
    if (box._sentTail.textContent !== (tail || "")) {
      box._sentTail.textContent = tail || "";
    }
    box._sentTail.classList.toggle("hidden", !tail);
    if (box._sentLive.textContent !== (unfixed || "")) {
      box._sentLive.textContent = unfixed || "";
    }
    box._sentLive.classList.toggle("hidden", !unfixed);
  },

  /**
   * 渲染「识别过程」面板（逐轮明细）。
   *
   * ⚠️ **不能每来一条 round 帧就重建整张表**。
   *
   * 面板是**逐轮追加**的（每轮一行），而一轮 = 一秒音频。98 分钟的片子
   * 就是 **5869 轮** → 每帧重建 5869 行 × 4 个元素 ≈ 2.3 万个 DOM 节点。
   * 实测（板端 headless Chromium，5869 轮）：**每帧中位 59 ms / 最大
   * 74 ms** —— 浏览器主线程被占死，**识别本身都被拖慢了**（同样这片子，
   * 开着页面识别 1077 s，不开页面约 480 s）。
   *
   * 所以两条规矩：
   *   1. **面板看不见的时候不重建**（数据还在 ``this.rounds`` 里，切回
   *      「识别过程」模式再补上 —— 见 ``_applyMode``）；
   *   2. 面板可见时**只追加新增的那几轮**，不重画老行。
   */
  _renderRounds() {
    const box = document.getElementById("asr-rounds");
    if (!box) return;
    // ① 面板隐藏 → 跳过（并记下「DOM 跟不上了」）
    if (box.classList.contains("hidden")) {
      this._roundsDirty = true;
      return;
    }
    const n = this.rounds.length;
    // ② 轮次被换过（重新识别 / 清空）→ 整表重建
    if (this._roundsDirty || this._roundsShown > n) {
      box.innerHTML = "";
      this._roundsShown = 0;
      this._roundsPrev = "";
      this._roundsDirty = false;
    }
    if (this._roundsShown === n) {
      box.scrollTop = box.scrollHeight;
      return;
    }
    // ③ 只追加新增的轮次
    let prev = this._roundsPrev || "";
    for (let i = this._roundsShown; i < n; i++) {
      const r = this.rounds[i];
      const row = document.createElement("div");
      row.className = "asr-round";
      const no = document.createElement("span");
      no.className = "asr-round-no";
      no.textContent = `#${r.round}`;
      const add = document.createElement("span");
      add.className = "asr-round-add";
      // committed 是累计的，这里只显示本轮的增量。
      add.textContent = r.committed && r.committed.startsWith(prev)
        ? r.committed.slice(prev.length)
        : (r.committed || "");
      const tail = document.createElement("span");
      tail.className = "asr-round-tail";
      tail.textContent = r.unfixed || "";
      row.appendChild(no);
      row.appendChild(add);
      row.appendChild(tail);
      box.appendChild(row);
      prev = r.committed || prev;
    }
    this._roundsShown = n;
    this._roundsPrev = prev;
    box.scrollTop = box.scrollHeight;
  },

  // ------------------------------------------------------------------ WS
  ensureWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${API_PREFIX}/qwen3-asr/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch (_) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.wsRetry = 0;
      const startBtn = document.getElementById("asr-start-btn");
      if (startBtn) startBtn.disabled = !this._hasSource() || this._busy();
    };
    this.ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      this._onFrame(msg);
    };
    this.ws.onclose = () => {
      this.ws = null;
      this._scheduleReconnect();
    };
    this.ws.onerror = () => { /* onclose 会接手重连 */ };
  },

  _scheduleReconnect() {
    if (this.wsTimer) return;
    const delay = Math.min(1000 * Math.pow(2, this.wsRetry++), 15000);
    this.wsTimer = setTimeout(() => {
      this.wsTimer = null;
      this.ensureWs();
    }, delay);
  },

  /* ================================================================ *
   * 同声传译（2026-11-21）
   *
   * 流程：ASR 出句（实时 endpoint / 文件标点）→ 翻译 → 播报（板载扬声器）。
   * 服务端在 ``cards/qwen3_asr/interp.py``；这里只管**面板**和**播放头**。
   * ================================================================ */

  /** 这一路是「实时流」还是「文件流」？ */
  _interpSource() {
    const src = this._source || "file";
    if (src === "board" || src === "browser") return "live";
    // ⚠️⚠️ **视频源要单列**（实测 2026-11-23，M31；用户报「含音轨的视频
    // 开始同传后，没有根据目标语音字幕的节奏播放」）：
    //
    // * **音频文件 = 离线一次性转写** ⇒ 句子一次性到齐 ⇒ 服务端按
    //   「内容量」攒批（18 秒）只多花首句的等待；
    // * **视频 = 边播边识别**（解码比实时快，ASR 跑在播放头**前面**）⇒
    //   字幕行**随播放实时到达** ⇒ 沿用文件源的攒批口径 ⇒ 译文**晚十几秒**
    //   才开口，而且一开口就是一大块 ⇒ **完全不是字幕的节奏**；
    // ⇒ 视频必须告诉服务端「我是视频」⇒ 服务端用**单独一套**攒批口径
    //   （小批次 + 按积压自适应，见 ``interp.py::_video_batch_ready``）。
    if (src === "video") return "video";
    return "file";
  },

  /** 有播放头的源（文件 / 视频）才能「跟播放」。 */
  _interpHasPlayhead() {
    const src = this._source || "file";
    return src === "file" || src === "video";
  },

  /**
   * 「开始同传」要发给服务端的**播放时机**（2026-11-25）。
   *
   * ⚠️ **不直接用 ``this._interp.gate``** —— 那是**本地状态**，它在没有
   * 播放头的源上会被强制改成「now」，而用户在麦克风与视频之间来回切时
   * 这个「now」会**留在本地** ⇒ 于是「开始同传」把「不等播放」发出去 ⇒
   * **队列的播放时钟被关掉** ⇒ 译文**不等字幕位置**（实测比字幕位置超前
   * 15~75 秒 ⇒ 用户报「不跟字幕的节奏」）。
   *
   * ⇒ 规则：**有播放头的源 ⇒ 除非用户自己选过，否则一律「跟播放」**；
   *   没有播放头的源 ⇒ 只能「不等播放」。
   */
  _interpStartGate() {
    if (!this._interpHasPlayhead()) return "now";
    const g = this._interp.gateTouched ? this._interp.gate : "follow";
    // 面板上的下拉也要跟实际发出去的一致（否则界面在骗人）。
    this._interp.gate = (g === "follow") ? "follow" : "now";
    const sel = document.getElementById("asr-interp-gate");
    if (sel) sel.value = this._interp.gate;
    return this._interp.gate;
  },

  /** 拉「能出声的目标语言」和音色列表。 */
  /**
   * **向导第 5 步「同声传译」**的选项 —— **运行时**灌进去。
   *
   * ⚠️ 为什么要自己灌：朗读语言的表在**后端**（``/interp/languages`` 只列
   *    **能用语音播报**的 10 种），音色的表在**TTS 卡**（``/api/tts/voices``）
   *    ⇒ ``wizard.json`` 里只有占位项。
   *
   * ⚠️ **不能复用卡片页的 ``_interpLoadOptions``**：它按 **id** 取元素
   *    （``getElementById``）⇒ 拿不到**向导里**那份 select（向导的字段
   *    带 ``data-field-id``，id 由框架生成）⇒ 只能各灌各的。
   */
  async _interpWizardOptions() {
    // ⚠️ **2026-11-26**：向导的「同声传译」步换成了「配音导出」步 ⇒
    // 同传的两个字段（朗读语言 / 音色）**已经不在向导里了** ⇒ 这里
    // 改成灌**配音导出的音色**（``asr-dub-voice``）。
    const voiceSel = document.querySelector(
      '#wd-wizard-root-qwen3-asr [data-field-id="asr-dub-voice"]');
    const langSel = document.querySelector(
      '#wd-wizard-root-qwen3-asr [data-field-id="asr-dub-target"]');
    if (!langSel && !voiceSel) return;

    if (langSel) {
      try {
        const r = await fetch("/api/qwen3-asr/interp/languages",
          { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        const items = (data && data.items) || [];
        langSel.innerHTML = "";
        items.forEach((it) => {
          const opt = document.createElement("option");
          opt.value = it.id || "";
          // ⚠️ 同「显示语言」那个下拉**同款排法**（中文名 + 本族名）。
          opt.textContent = this._trLangLabel(it);
          langSel.appendChild(opt);
        });
        langSel.value = this._dub.targetLang ||
          (items[0] ? items[0].id : "");
        if (langSel.value) this._dub.targetLang = langSel.value;
      } catch (e) {
        console.warn("[qwen3-asr] 向导配音语言拉取失败:", e);
      }
    }

    if (voiceSel) {
      try {
        const r = await fetch("/api/tts/voices", { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        const voices = (data && data.voices) || [];
        voiceSel.innerHTML = "";
        voices.forEach((v) => {
          const opt = document.createElement("option");
          const id = v.id || "";
          // ⚠️ **音色类别必须带上** —— 参考音色（voice-clone）和内置音色
          //    是**两个不同的 SDK 参数** ⇒ 混用 ⇒ 音色不生效。
          //    ⇒ 值里带前缀（``kind:id``），回填时据此拆成两个参数。
          const kind = v.kind === "builtin" ? "builtin" : "ref";
          opt.value = kind + ":" + id;
          // ⚠️ 板端**没有符号字体** ⇒ 只用普通汉字。
          opt.textContent = (kind === "ref" ? "参考音色 " : "内置音色 ") + id;
          voiceSel.appendChild(opt);
        });
        const want = this._dub.wizardVoice || "";
        if (want) voiceSel.value = want;
        if (!voiceSel.value && voiceSel.options.length) {
          voiceSel.selectedIndex = 0;
          this._dub.wizardVoice = voiceSel.options[0].value;
        }
        // ⚠️ **默认用参考音色**（本板只有这类能出声）⇒ 内置音色排前面时
        // 也不能选它。
        const firstRef = voices.find((v) => (v.kind || "ref") === "ref");
        if (firstRef) {
          voiceSel.value = "ref:" + firstRef.id;
          this._dub.wizardVoice = voiceSel.value;
        }
      } catch (e) {
        console.warn("[qwen3-asr] 向导配音音色拉取失败:", e);
      }
    }
  },

  /**
   * 同传的**目标语言** —— 向导选的「朗读语言」，没选就跟随翻译目标语言。
   *
   * ⚠️ **只有一个语言概念** —— 同传的朗读语言与字幕的翻译语言**同一个**：
   *    用户在向导里选了「朗读语言 = 日语」⇒ 字幕也翻成日语、声音也是日语
   *    ⇒ 音字幕对得上（需求：语音节奏与字幕节奏一致）。
   */
  _interpTargetLang() {
    const w = String(this._interp.wizardLang || "").trim();
    if (w) return w;
    const t = String(this._tr.targetLang || "").trim();
    if (t && t !== "off") return t;
    return this._interp.targetLang || "zh-CN";
  },

  /**
   * **发起** TTS 模型的加载 —— **不等**它加载完。
   *
   * ⚠️ 为什么不等：``POST /interp/ensure-tts`` 会等 worker 注册完才返回
   * （~1 s），而模型的真正加载（~30 s）是后台的 ⇒ 进度条那边按
   * ``model_id`` 等实例变 ``running`` 就行。
   */
  async _interpEnsureTts() {
    try {
      const r = await fetch("/api/qwen3-asr/interp/ensure-tts",
        { method: "POST" });
      const data = await r.json();
      if (!r.ok || !data || !data.ok) {
        const why = (data && (data.detail || data.detail)) || "起不来";
        console.warn("[qwen3-asr] TTS 模型拉起失败:", why);
        return false;
      }
      return true;
    } catch (e) {
      console.warn("[qwen3-asr] TTS 模型拉起请求失败:", e);
      return false;
    }
  },

  /**
   * TTS 模型的 ``model_id`` —— 给向导的「模型加载进度条」用。
   *
   * ⚠️ 进度条是**按 model_id 找实例**的 ⇒ 必须先知道要等的是谁。
   * ⚠️ 走同传 status 拿（宿主桥按 capability 查，不写死 id）。
   */
  async _interpTtsModelId() {
    try {
      const r = await fetch("/api/qwen3-asr/interp/status",
        { cache: "no-store" });
      const data = r.ok ? await r.json() : null;
      return String((data && data.tts_model_id) || "");
    } catch (e) {
      console.warn("[qwen3-asr] 同传 TTS 模型 id 拉取失败:", e);
      return "";
    }
  },

  async _interpLoadOptions() {
    if (this._interp.loaded) return;
    this._interp.loaded = true;
    const sel = document.getElementById("asr-interp-target");
    if (sel) {
      try {
        const r = await fetch("/api/qwen3-asr/interp/languages",
          { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        const items = (data && data.items) || [];
        sel.innerHTML = "";
        items.forEach((it) => {
          const opt = document.createElement("option");
          opt.value = it.id || "";
          // ⚠️ 同「显示语言」那个下拉**同款排法**（中文名 + 本族名）——
          //    见 `_trLangLabel` 的说明（三处语言下拉是同一套）。
          opt.textContent = this._trLangLabel(it);
          sel.appendChild(opt);
        });
        if (this._interp.targetLang) sel.value = this._interp.targetLang;
      } catch (e) {
        console.warn("[qwen3-asr] 同传目标语言拉取失败:", e);
      }
    }
    const spk = document.getElementById("asr-interp-speaker");
    if (spk) {
      try {
        const r = await fetch("/api/tts/voices", { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        const voices = (data && data.voices) || [];
        spk.innerHTML = "";
        voices.forEach((v) => {
          const opt = document.createElement("option");
          const id = v.id || "";
          // ⚠️ **标明音色类别** —— 参考音色和内置音色是**两个不同的 SDK
          // 参数** ⇒ 用户看得见自己选的是哪一类 ⇒ 也不会传错。
          // （板端**没有符号字体** ⇒ 只用普通汉字，不用分隔符。）
          const kind = v.kind === "builtin" ? "builtin" : "ref";
          opt.dataset.kind = kind;
          opt.value = id;
          opt.textContent = (kind === "ref" ? "参考音色 " : "内置音色 ") + id;
          spk.appendChild(opt);
        });
        // ⚠️ 默认选中**当前生效**的那个（参考音色优先 —— 本板只有这类可用）
        const want = this._interp.refSpeaker || this._interp.speaker;
        if (want) spk.value = want;
        if (!want && spk.options.length) {
          spk.selectedIndex = 0;
          const opt = spk.options[0];
          const kind = opt.dataset.kind === "builtin" ? "builtin" : "ref";
          this._interp.speaker = (kind === "builtin") ? opt.value : "";
          this._interp.refSpeaker = (kind === "ref") ? opt.value : "";
        }
      } catch (e) {
        console.warn("[qwen3-asr] 同传音色拉取失败:", e);
      }
    }
  },

  /** 开始同传。 */
  async _interpStart() {
    // ⚠️⚠️ **同传只对麦克风两种来源开放**（2026-11-26 用户要求）——
    // 「音频文件」/「含音轨的视频」改成**配音导出**（见 ``_dubStart``）。
    // ⇒ 这两类来源点「开始同传」直接拒绝（后端也会拒）。
    if (this._isFileSource()) {
      this._interpSetStatus?.("文件类来源不做同传，请用「配音导出」");
      const el = document.getElementById("asr-interp-status");
      if (el) el.textContent = "文件类来源不做同传，请用「配音导出」";
      return;
    }
    // ⚠️⚠️ **自动播放必须放在最前面（同步执行）** ——
    //
    // 浏览器的自动播放策略：**没有用户手势**时 ``el.play()`` 会被拒绝。
    // 「开始同传」这个**按钮点击本身就是用户手势** ⇒ 必须在**同一个
    // 同步调用栈**里调 ``play()``。
    //
    // ⚠️ 之前是 ``await fetch(...)`` **之后**才自动播放 ⇒ 手势早就失效了
    // ⇒ 媒体一直暂停 ⇒ 播放头永远 0 ⇒ 「跟播放」把**所有**句子压住 ⇒
    // **用户看到译文却一声不响**（实测：用户报「有翻译，但是没有播放」，
    // 日志里播放头只上报过 1 次就再没有）。
    if (this._interpHasPlayhead()) {
      // ⚠️ 每次开始都**重置**「媒体没在放」的原因（上一轮的结论不能
      // 带到这一轮 —— 媒体这次可能正常播了）。
      this._interp.mediaBlocked = "";
      this._interpAutoPlay();
    }
    const body = {
      mode: this._interp.mode,
      // ⚠️⚠️ **媒体来源 ⇒ 除非用户自己选过「不等播放」，否则一律「跟播放」**
      // （2026-11-25）——
      //
      // 「播放时机」是**本地状态**，而它在**没有播放头的源**（板载麦 /
      // 浏览器麦）上会被强制改成「now」⇒ 用户从麦克风切回视频时，这个
      // 「now」可能还**留在本地** ⇒ 「开始同传」就把它发给服务端 ⇒
      // **播放时钟被关掉** ⇒ 译文不等字幕位置（实测超前 15~75 秒）。
      //
      // ⇒ 这里**不信任本地残留值**：用户没显式选过 ⇒ 媒体源一律「跟播放」。
      gate: this._interpStartGate(),
      target_lang: this._interp.targetLang,
      // ⚠️ 内置音色 / 参考音色**分开传**（两个不同的 SDK 参数）
      speaker: this._interp.speaker,
      ref_speaker: this._interp.refSpeaker,
      volume: this._interp.volume,
      source: this._interpSource(),
      // ⚠️ **标签页 id**（2026-11-25）—— 服务端只认这个标签页的播放头
      // （多标签页会互相覆盖播放头 ⇒ 对齐失效 ⇒ 声音不跟随字幕）。
      client_id: this._interpClientId(),
    };
    try {
      const r = await fetch("/api/qwen3-asr/interp/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      this._interp.running = true;
      this._interp.status = data;
      // ⚠️ **这一轮是本标签页发起的** ⇒ 服务端会认本标签页的播放头 ⇒
      // 清掉「被忽略」的标记（2026-11-25，多标签页隔离）。
      this._interp.phIgnored = false;
      // ⚠️ **新一轮同传 ⇒ 底部结果列表清空**（2026-11-25）——
      // 两轮的结果混在一起 ⇒ 用户分不清哪句是哪轮的。
      this._interpClearSegs();
      this._interpSyncUi();
      this._interpStartPlayhead();
      this._interpPoll();
    } catch (e) {
      this._interp.running = false;
      const el = document.getElementById("asr-interp-status");
      if (el) el.textContent = "同传启动失败：" + (e.message || e);
      this._interpSyncUi();
    }
  },

  /** 停止同传（并清 TTS 队列）。 */
  async _interpStop() {
    try {
      await fetch("/api/qwen3-asr/interp/stop", { method: "POST" });
    } catch (e) {
      console.warn("[qwen3-asr] 同传停止失败:", e);
    }
    this._interp.running = false;
    this._interpStopPlayhead();
    this._interpStopTimer();
    this._interpSyncUi();
  },

  /** 改设置（不重启引擎）。 */
  async _interpSettings(patch) {
    try {
      const r = await fetch("/api/qwen3-asr/interp/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch || {}),
      });
      const data = await r.json();
      if (r.ok) {
        this._interp.status = data;
        this._interpSyncUi();
      }
    } catch (e) {
      console.warn("[qwen3-asr] 同传设置失败:", e);
    }
  },

  /** 每秒拉一次状态（面板上的滞后 / 队列 / 变速）。 */
  _interpPoll() {
    if (this._interp.timer) return;
    this._interp.timer = setInterval(async () => {
      if (!this._interp.running) {
        this._interpStopTimer();
        return;
      }
      try {
        const r = await fetch("/api/qwen3-asr/interp/status",
          { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        this._interp.status = data;
        // ⚠️⚠️ **「同传在不在跑」以服务端为准**（2026-11-25）——
        //
        // 症状（实测）：同传**不是**通过界面停的（另一处 API 调用停的 /
        // 引擎自己停了 / 源跑完了）⇒ 前端的 ``_interp.running`` 还停在
        // true ⇒ 面板显示「同传中」⇒ **「开始同传」按钮一直 disabled**
        // ⇒ 用户（和回归脚本）再也开不了第二轮。
        //
        // ⇒ 这里把服务端的 ``running`` 同步回来 ⇒ 面板永远跟着引擎的真实
        // 状态走。
        if (typeof data.running === "boolean"
            && data.running !== this._interp.running) {
          this._interp.running = data.running;
          if (!data.running) {
            // 服务端不跑了 ⇒ 别再上报播放头（否则服务端收到的是幽灵数据）。
            this._interpStopPlayhead();
          }
        }
        this._interpSyncUi();
      } catch (e) {
        /* 网络问题不打扰用户 */
      }
    }, 1000);
  },

  _interpStopTimer() {
    if (this._interp.timer) {
      clearInterval(this._interp.timer);
      this._interp.timer = null;
    }
  },

  /**
   * **刷新页面后接管「同传还在跑」**（2026-11-25）。
   *
   * ⚠️ **为什么必须接管**：同传是**服务端**的引擎，**刷新页面不会停它**
   * ⇒ 但前端的状态全在内存里 ⇒ 刷新后 ``_interp.running`` 变成 false ⇒
   * * 面板显示「同传未开启」（其实它在跑）；
   * * **播放头上报的定时器没起来** ⇒ 服务端收不到播放头 ⇒
   *   「跟字幕节奏」与「拖进度条后重新对齐」**双双失效**。
   *
   * ⇒ 这里问一次服务端的状态，在跑就接回来。
   */
  async _interpRestoreState() {
    try {
      const r = await fetch("/api/qwen3-asr/interp/status",
        { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      if (!data || !data.running) return;
      this._interp.running = true;
      this._interp.status = data;
      // ⚠️ **服务端的播放时机是权威** —— 刷新前用户选过什么就还是什么。
      if (data.gate === "follow" || data.gate === "now") {
        this._interp.gate = data.gate;
        this._interp.gateTouched = true;   // 服务端这么跑 ⇒ 用户选过
      }
      if (data.mode === "fast" || data.mode === "steady") {
        this._interp.mode = data.mode;
      }
      // ⚠️⚠️ **源**不用在这里接管（2026-11-25 改）——
      //
      // 「刷新后源要切回去」这件事**已经有人做了** ⇒ `_restoreFromServer()`
      // 从 `GET /rounds` 的 `source.{kind,path}` 把**路径**和**源**一起
      // 恢复（服务端说了算 ⇒ 那才是权威的 —— 轮次就是那个源的）。
      //
      // ⇒ 在这里再切一次会**跟它打架**（两个异步恢复各切各的）⇒ 而且
      // `_switchSource()` 在源**真变了**时会清字幕 ⇒ 可能把刚恢复出来的
      // 字幕又清掉。
      //
      // ⇒ 只接管**同传自己的状态**（在不在跑 / 播放时机 / 档位）。
      this._interpSyncUi();
      this._interpPoll();
      this._interpStartPlayhead();
      console.info("[qwen3-asr] 同传还在跑，已接管（gate=" + this._interp.gate + "）");
    } catch (e) {
      /* 网络问题不打扰用户 */
    }
  },

  /** 面板渲染。 */
  _interpSyncUi() {
    const st = this._interp.status || {};
    const btnStart = document.getElementById("asr-interp-start");
    const btnStop = document.getElementById("asr-interp-stop");
    const el = document.getElementById("asr-interp-status");
    const hint = document.getElementById("asr-interp-hint");
    const gateSel = document.getElementById("asr-interp-gate");
    // 播放时机：没有播放头的源 ⇒ 置灰 + 强制「不等播放」
    const hasPh = this._interpHasPlayhead();
    const prevGate = this._interp.gate;
    if (gateSel) {
      gateSel.disabled = !hasPh;
      if (!hasPh) {
        // ⚠️⚠️ **别吃掉用户自己的选择**（2026-11-25）——
        //
        // 「没有播放头的源 ⇒ 强制『不等播放』」是对**默认值**的降级 ⇒
        // 用户**自己**选过的那个（``gateTouched``）不该被覆盖 ⇒ 否则：
        // 用户在视频源上选过「跟播放」⇒ 切到麦克风（这里被强制成
        // 「now」）⇒ 再切回视频 ⇒ 选择**丢了** ⇒ 面板显示「不等播放」，
        // 而服务端其实还在「跟播放」⇒ **界面在骗人**。
        //
        // ⇒ 只有**用户没选过**的时候才强制降级。
        if (!this._interp.gateTouched) {
          this._interp.gate = "now";
        }
        gateSel.value = this._interp.gate;
      } else if (!this._interp.gateTouched) {
        // ⚠️ **有播放头的源（文件 / 视频）默认「跟播放」**。
        //
        // 为什么：「不等播放」在文件源上是**错的** —— 译文比源短（或源里有
        // 停顿）⇒ 译文会**跑到源前面**，而且越播越前；反过来，若译文比
        // 源长（翻译后的音更啰嗦），「不等播放」又会让**音频越落越远**
        // （实测：源 15 s 的文件，译文字频播完要 44 s ⇒ 落后 18 秒以上）。
        //
        // ⇒ 「跟播放」= 译文贴着源走 ⇒ 既有播放头可对齐 ⇒ 默认用它。
        this._interp.gate = "follow";
        gateSel.value = "follow";
      }
    }
    // ⚠️⚠️ **服务端也要跟着换**（2026-11-25，用户报「含音轨的视频的同传
    // 不跟字幕的节奏」）。
    //
    // 症状（实测）：用户浏览器那一场同传是 **gate=now**（引擎日志：「播放
    // 时钟 已关掉（gate=now）」）⇒ 队列的播放时钟被关掉 ⇒ 译文**合成完就
    // 播** ⇒ 而 ASR 跑在播放头**前面** ⇒ 译文比字幕位置**超前 15~75 秒**
    // （实测 a_lag）⇒ 完全不同步。
    //
    // 根因：**「播放时机」是前端的本地状态**，而它在「没有播放头的源」
    // （板载麦 / 浏览器麦）上会被**强制改成「不等播放」** ⇒ 用户从麦克风
    // 切回视频时这个「now」**留在本地** ⇒ 「开始同传」就带着 now 发出去
    // ⇒ 服务端从此不跟播放头。
    //
    // ⇒ **来源变了、服务端在跑 ⇒ 把播放时机同步过去**（用户自己选过就
    // 不动）。
    if (this._interp.running && hasPh && this._interp.gate !== prevGate) {
      this._interpSettings({ gate: this._interp.gate });
    }
    if (btnStart) btnStart.disabled = this._interp.running;
    if (btnStop) btnStop.disabled = !this._interp.running;
    if (el) {
      if (!this._interp.running) {
        el.textContent = "同传未开启";
      } else if (this._interp.phIgnored) {
        // ⚠️⚠️ **这个标签页的播放头被服务端忽略了**（2026-11-25）——
        //
        // 同传引擎是**服务端全局单例** ⇒ 只认**发起这一轮**的那个标签页的
        // 播放头（实测有 6 个标签页同时上报 ⇒ 播放头来回跳 ⇒ 对齐失效）。
        //
        // ⚠️ **这条必须排在「播放头没上报」前面** —— 被忽略的上报**不进**
        // 服务端的播放头记账 ⇒ 服务端那边 age 一直涨 ⇒ 一定会先命中
        // 「播放头没上报」⇒ 用户以为页面坏了 ⇒ 其实是「多标签页」。
        el.textContent = "这个标签页不是发起同传的那个"
          + "· 它的播放头被服务端忽略（多标签页互相覆盖播放头）"
          + "· 要在这里播，就重新点一次「开始同传」";
      } else if (st.playhead_missing) {
        // ⚠️⚠️ **播放头没上报**（2026-11-25）——
        //
        // 「跟播放」的整套机制（队列的播放时钟 + 拖进度条后重新对齐）**全靠
        // 播放头** ⇒ 前端一停止上报（刷新页面没接回来 / 媒体还没加载），
        // 系统就**静默**退化成「不等播放」⇒ 译文合成完就播 ⇒ 而 ASR 跑在
        // 播放头**前面** ⇒ 译文比字幕位置**超前 15~75 秒**（实测）。
        //
        // ⇒ **说清楚**：这跟「媒体没在放」是两件事 ⇒ 给出的出路也不同。
        const age = Number(st.playhead_age_sec || -1);
        const ageTxt = age >= 0 ? `${Math.round(age)} 秒没收到` : "从没收到过";
        el.textContent = "播放头没上报（" + ageTxt + "）"
          + "· 译文不等字幕位置（会越走越超前）"
          + "· 刷新一下页面，或确认媒体已加载";
      } else if (st.media_paused) {
        // ⚠️⚠️ **媒体暂停了 ⇒ 播报也停**（2026-11-25，用户提的第三点：
        // 「视频在暂停的时候，同声传译播完视频实时字幕上的翻译语言后，
        //  也要暂停播报」）。
        //
        // 服务端队列在**句边界**按住（aplay 管道里已写入的 PCM 撤不回来
        // ⇒ 当前那句会**播完**，下一句才停）⇒ 这就是用户要的语义。
        //
        // ⚠️ **这条要排在「播放头卡住」前面** —— 暂停时播放头当然不动 ⇒
        // 不然用户看到的是「播放头卡住（媒体没在放 / 卡在缓冲）」⇒ 意思
        // 差不多但没告诉他「这是你按的暂停，播报已经跟着停了」。
        el.textContent = "已暂停（媒体暂停了，播报跟着停）"
          + "· 恢复播放后译文继续跟着字幕走";
      } else if (this._interp.gate === "now" && hasPh) {
        // ⚠️ **「不等播放」+ 有播放头的源** ⇒ 译文**不等字幕位置**。
        //
        // 用户可能是有意的（源比译文短 / 不想等），但**默认不该是这个**
        // ⇒ 说清楚它意味着什么。
        el.textContent = "不等播放（译文不等字幕位置，会越走越超前）"
          + "· 改成「跟播放」才会贴着字幕";
      } else if (st.playhead_stalled && !st.source_done) {
        // ⚠️ **播放头卡住** —— 媒体没在放（自动播放被拦 / 用户暂停）
        // ⇒ 「跟播放」压着句子不放 ⇒ 用户看不到译文出声。
        // ⇒ **说清楚**，并给出两条可操作的路。
        //
        // ⚠️⚠️ **「没有句子压着」时也要说**（实测 2026-11-25）——
        // **文件源**的放行器**不看播放头**（``_batch_ready`` 按内容量攒批，
        // 跟播对齐交给队列的播放时钟）⇒ 句子**不会被压住** ⇒ 原来那个
        // 「``pending > 0`` 才提示」的条件**永远不成立** ⇒ 面板只显示
        // 「正在播报 · 可听滞后 xx 秒」，用户**看不到任何异常提示** ⇒
        // 明明「媒体没在放」，却只看到一个越来越大的滞后。
        // ⇒ **只要播放头卡住就说**（压着没播 ⇒ 说压着多少；没压着 ⇒ 说
        // 译文在等播放头）。
        const pend = Number(st.pending || 0);
        const tail = "· 点媒体的播放键，或把「播放时机」改成「不等播放」";
        el.textContent = pend > 0
          ? `等播放头：播放头卡住（媒体没在放 / 卡在缓冲），`
            + `${pend} 段译文压着没播 ${tail}`
          : `等播放头：播放头卡住（媒体没在放 / 卡在缓冲）`
            + `· 译文在等播放头 ${tail}`;
      } else {
        const tts = st.tts === false ? "（仅字幕：TTS 未启用）" : "";
        // ⚠️ **优先显示「可听滞后」**（译文出声 − 源话开口）——
        // 那是用户**真正感受到**的那一口；``lag_sec`` 在跟播模式下是
        // 另一个口径（「源话比最后一句已提交的译文前进了多少」）。
        const audible = Number(st.audible_lag_sec || 0);
        const lag = audible > 0 ? audible : Number(st.lag_sec || 0);
        const lagTag = audible > 0 ? "可听滞后" : "滞后";
        const depth = Number(st.queue_depth || 0);
        const tempo = Number(st.tempo || 1);
        // ⚠️ **并行合成的卡数**（实测 2026-11-23）—— 本板 **npu2 是禁区**
        // ⇒ 只有一个 TTS 卡位 ⇒ 队列恒为**单 worker 串行** ⇒ 这里不显示
        // 任何并行字样（显示了就是骗人）。
        const par = "";
        const ts = st.tts_status || {};
        // ⚠️ **兜底放行**的提示：播放头一直没动 ⇒ 句子被强行放行了 ⇒
        // 译文比画面/声音**超前**，得让用户知道。
        //
        // ⚠️ **读队列侧的 ``clock_released``**（2026-11-25）—— 队列的
        // 「8 秒兜底放行」才是真正会「狂喷积压」的那个（实测一次放出
        // 184 秒 ⇒ 声音跑到画面前面 174 秒）；``stalled_released`` 是
        // **放行器**的计数（另一条路径）。
        const stall = Number(ts.clock_released || st.stalled_released || 0) > 0
          ? "（播放头没动，已兜底放行）" : "";
        // ⚠️⚠️ **电平探针**（2026-11-25，用户报「显示已播报，喇叭没声音」）
        // ——
        //
        // 「正在播报」**不等于「听得见」**：
        // * ``last_out_peak = 0`` ⇒ PCM 写进了**死管道** ⇒ **一声不响**；
        // * 峰值太小 ⇒ 播了，但**太小声**（板载小喇叭上听不见）⇒ 实测
        //   1.6% 满量程 = **-36 dBFS** ⇒ 用户只看到「已播报」。
        //
        // ⚠️ **读 ``last_out_peak``**（真正送进 aplay 的那个）——
        // 之前读的是 ``last_peak``，而它**恒为 0**（队列从来没给 ``job.peak``
        // 赋值）⇒ 这个提示**永远不会显示**。
        //
        // ⚠️ **阈值用绝对电平**（0.02 ≈ -34 dBFS）—— 归一化之后输出峰值
        // ≈ 0.9 × 音量 ⇒ 5% ⇒ 0.045 ⇒ 用旧的 0.05 阈值会**永远报警**。
        const outPeak = Number(ts.last_out_peak || 0);
        const rawPeak = Number(ts.last_raw_peak || 0);
        let level = "";
        if (outPeak <= 0) {
          level = "（没出声：PCM 写进了死管道）";
        } else if (outPeak < 0.02) {
          const db = (20 * Math.log10(Math.max(outPeak, 1e-6))).toFixed(0);
          level = `（太小听不见：出声峰值 ${outPeak.toFixed(3)} ≈ ${db}dB）`
            + "· 把音量调大";
        } else if (rawPeak > 0) {
          // ⚠️ **顺带报一下模型原始电平** —— 归一化之前它有多小 ⇒ 用户
          // 一眼看出「自动增益把它拉上来了多少」。
          const db = (20 * Math.log10(Math.max(outPeak, 1e-6))).toFixed(0);
          level = `（出声峰值 ${outPeak.toFixed(3)} ≈ ${db}dB`
            + `· 模型原始 ${rawPeak.toFixed(2)} 已归一化）`;
        }
        // ⚠️ **刚拖过进度条 ⇒ 告诉用户「已经跟上了」**（实测 2026-11-22，
        // 用户报「多次拖动进度条，没和字幕的翻译同声播放出来」⇒ 用户需要
        // 一个可见的反馈确认「拖动被识别了、译文已重新对齐」）。
        const sk = st.last_seek || null;
        const skAge = sk && sk.ts ? (Date.now() / 1000 - Number(sk.ts)) : 1e9;
        const aligned = (sk && skAge >= 0 && skAge < 20)
          ? `（已跟随进度条重新对齐${Number(sk.replayed || 0) > 0
              ? `，重播 ${Number(sk.replayed)} 句` : ""}）` : "";
        el.textContent = `正在播报${stall}${level}${aligned} · ${lagTag} ${lag.toFixed(1)} 秒 · `
          + `队列 ${depth} 段 · 变速 ${tempo.toFixed(2)}${par}${tts}`;
      }
    }
    if (hint) {
      const src = this._source || "file";
      let text = "同传把识别结果翻译后从板载扬声器播报。一句话都不会丢（逐句结果见页面底部）。";
      if (this._interp.running) {
        if (src === "board" || src === "browser") {
          // ⚠️⚠️ **真同传**（2026-11-26）—— 麦克风是「说一句 → 立刻译 →
          // 立刻念」⇒ 说清楚这是**实时**的，别让用户以为在等字幕。
          text = "实时同传：说一句就译一句、译完就念（不等字幕位置）。";
        } else if (this._interp.gate === "follow") {
          // ⚠️⚠️ **预合成**（2026-11-26）—— 文件 / 视频源「后台一直处理」⇒
          // 用户能拖到哪就立刻播到哪 ⇒ 提示里要说清「已经合成多少」。
          const pf = Number(st.prefetch_entries || 0);
          const pfHits = Number(st.prefetch_hits || 0);
          // ⚠️ **过期被跳过的句数**（2026-11-26）—— 「重新武装时头几句已经
          // 对不上画面了」⇒ 它们不播（否则译文跟不上当前字幕）⇒ 让用户
          // 看见这个数 ⇒ 他就知道「刚才那几句没念是有意的」。
          const pfStale = Number(st.prefetch_stale_skipped || 0);
          const pfTxt = pf > 0
            ? `（后台已合成 ${pf} 段${pfHits > 0 ? `，拖动后直接命中 ${pfHits} 次` : ""}`
              + `${pfStale > 0 ? `，${pfStale} 句已过期未念（跟不上画面就不念）` : ""}）`
            : "";
          if (this._interp.mediaBlocked) {
            // ⚠️ **具体原因优先**（比泛泛的「译文跟着播放头走」有用得多）：
            // 浏览器拒了自动播放 / 媒体加载失败 ⇒ 媒体不动 ⇒ 播放头不动
            // ⇒ 句子被压住。
            text = this._interp.mediaBlocked;
          } else if (st.source_done) {
            // ⚠️ **源已经播完** ⇒ 不会再有新的源内容 ⇒ 剩下的译文**补播**。
            text = "源已播完 · 剩下的译文正在补播（识别比播放慢时会晚一点）。";
          } else if (st.playhead_stalled) {
            // ⚠️ 同上：**文件源**的句子不会被压住 ⇒ 「压着不播」这句
            // 只在**确实压着**时说 ⇒ 否则说「译文在等播放头」。
            const pend = Number(st.pending || 0);
            text = pend > 0
              ? "播放头卡住（媒体没在放 / 卡在缓冲）⇒ 译文压着不播。"
                + "点播放键，或把播放时机改成「不等播放」。"
              : "播放头卡住（媒体没在放 / 卡在缓冲）⇒ 译文在等播放头。"
                + "点播放键，或把播放时机改成「不等播放」。";
          } else {
            text = "语音跟着当前字幕走（后台已在预合成，拖动进度条也能立刻跟上）"
              + pfTxt + "。";
          }
        } else {
          // ⚠️ **说清楚代价**（2026-11-25）—— 「不等播放」在有播放头的源
          // 上意味着「译文不等字幕位置」⇒ 越播越超前（实测 15~75 秒）。
          text = "不等播放：识别一句就播一句（译文不等字幕位置 ⇒ 越播越超前）。";
        }
      }
      hint.textContent = text;
    }
  },

  /* ---------------- 同传结果列表（页面底部，2026-11-25） ---------------- */
  /**
   * 收下一句同传结果（原文 / 译文）。
   *
   * ⚠️ **为什么需要它**：同传**本来没有任何结果列表** ——
   * ``interp_segment`` 是**入队时**的帧（只有原文，译文还没算）、
   * ``interp_spoken`` 是**播完时**的帧（译文被后端截到 80 字）⇒ 页面上
   * 只有一句状态行（「正在播报 · 可听滞后 3.2 秒」），**看不到说了什么**。
   *
   * ⇒ 后端补了 ``interp_translated``（原文 + **完整**译文）⇒ 这里把两句
   * 合成一行（``index`` 相同 ⇒ 同一句），画在页面底部。
   */
  _interpSegUpsert(patch) {
    const segs = this._interp.segs;
    const idx = Number(patch.index) || 0;
    // ⚠️ **句号从 1 开始**（后端 ``_next_index``）⇒ 0 = 「没带句号」⇒
    // 不建行（不然会多出一行没有句子的空行）。
    if (!idx) return;
    let it = segs.get(idx);
    if (!it) {
      it = { index: idx, text: "", tr: "", lang: "",
             start: 0, end: 0, spoken: false, err: "" };
      segs.set(idx, it);
    }
    if (patch.text) it.text = patch.text;
    if (patch.tr) it.tr = patch.tr;
    if (patch.lang) it.lang = patch.lang;
    if (patch.start !== undefined) it.start = patch.start;
    if (patch.end !== undefined) it.end = patch.end;
    // ⚠️ **只画这一行** —— 每来一句就重画整张表 ⇒ 长音频（几千句）就是
    // O(n²) ⇒ 浏览器主线程被占死（同 ``_renderRounds`` 的教训）。
    this._interpSegPaint(it);
    this._interpRenderSegs();
  },

  /** 按 index 找一句（同传结果列表用）。 */
  _interpSegFind(index) {
    return this._interp.segs.get(Number(index) || 0) || null;
  },

  /**
   * 清空同传结果列表（**状态与 DOM 一起清**）。
   *
   * ⚠️ DOM 的行缓存挂在元素上（增量渲染用）⇒ 只清 Map 不清 DOM ⇒ 下一轮
   * 的句子会**接着**上一轮往下画（同 ``_sentClear`` 的教训）。
   */
  _interpClearSegs() {
    this._interp.segs.clear();
    // ⚠️ **当前句也要清**（2026-11-25）—— 否则高亮还留在一个已经不存的
    // 句子上 ⇒ 「正在播报」的标记悬着。
    this._interp.curSeg = null;
    this._interp.follow = true;
    const box = document.getElementById("asr-interp-list");
    if (box) {
      if (box._interpRows) {
        box._interpRows.forEach((row) => row.remove());
        box._interpRows.clear();
      }
      box.innerHTML = "";
    }
    const wrap = document.getElementById("asr-interp-out");
    if (wrap) wrap.classList.add("hidden");
    // ⚠️ **同传没了 ⇒ 「实时结果」那几栏要回来**（合并规则的另一半）。
    this._syncResultBoxes();
  },

  /**
   * 画「同传结果」列表的**外壳**（有没有内容 ⇒ 显不显）。
   *
   * ⚠️ **不重画行** —— 每一行由 :meth:`_interpSegPaint` **单独**更新 ⇒
   * 加一句只动一个节点（同 ``_renderRounds`` 的增量规矩）。
   */
  _interpRenderSegs() {
    const box = document.getElementById("asr-interp-list");
    if (!box) return;
    const wrap = document.getElementById("asr-interp-out");
    const segs = this._interp.segs;
    if (wrap) wrap.classList.toggle("hidden", segs.size === 0);
    // ⚠️ **有同传结果 ⇒ 「还没有识别结果」的占位就该让位** ——
    // 只**藏**不**显**（显的那一头由 ``_applySnapshot`` 管 ⇒ 两边不打架）。
    const empty = document.getElementById("asr-empty");
    if (empty && segs.size > 0) empty.classList.add("hidden");
    // ⚠️⚠️ **合并**（2026-11-25）—— 同传结果列表是「实时结果」的**更完整
    // 版本**（原文 + 译文 + 播报状态 + **跟播放头**）⇒ 两个同时显示 =
    // **同一批句子显示两遍**（用户实测过这个）。
    // ⇒ 同传有内容 ⇒ 只留同传结果这一个列表。
    this._syncResultBoxes();
    // ⚠️ **滚动** —— 有「当前句」就滚到它（跟字幕节奏）⇒ 没有（实时流没有
    // 播放头）⇒ 才滚到底（新句子在下面 ⇒ 跟句子流一个习惯）。
    if (this._interp.curSeg) {
      this._interpScrollTo(this._interp.curSeg);
    } else {
      box.scrollTop = box.scrollHeight;
    }
  },

  /**
   * **底部结果框的显隐**（2026-11-25 合并）—— 三个「实时结果」框与
   * 「同传结果」列表**同一时刻只显示一组**。
   *
   * ⚠️ **为什么要合并**（用户问「实时结果和同传结果合并在一起是否合理」）：
   *
   * | 框 | 内容 | 节奏 |
   * |---|---|---|
   * | `#asr-sentences`（音频文件·句子流） | 原文 + 译文 | 识别到就出 |
   * | `#asr-rt-sentences`（麦克风·句子流） | 原文 + 译文 | 识别到就出 |
   * | `#asr-transcript`（视频·字幕文本） | 原文（跟画面） | 跟播放头 |
   * | `#asr-interp-list`（同传结果） | 原文 + 译文 + **播报状态** | **跟播放头** |
   *
   * ⇒ 前三个与第四个**是同一批句子**（只是粒度不同：字幕行 vs 同传句）
   * ⇒ 同时显示 ⇒ **同一句话显示两遍**，用户得自己在两栏之间对号入座。
   *
   * ⇒ **同传在跑 ⇒ 只留「同传结果」**（它最全：原文 + 译文 + 播报状态 +
   * 跟播放头高亮）；**同传没跑 ⇒ 留原来的那一个**。
   *
   * ⚠️ **不是「把两个列表拼成一个」** —— 粒度不同（字幕**行** vs 同传**句**）
   * ⇒ 拼不到一行一行 ⇒ 只能「**同屏只留一个**」。
   */
  _syncResultBoxes() {
    const interpHas = this._interp.segs.size > 0;
    // ⚠️ **配音导出有内容 ⇒ 藏掉「实时结果」**（2026-11-27 用户要求）——
    //    配音导出结果面板是**更完整**的版本（原文 + 译文 + **两种语言都
    //    标出来了**）⇒ 两个都显示 ⇒ 同一批句子**显示两遍**。
    // ⇒ 跟「同传有内容 ⇒ 藏掉实时结果」是**同一条合并规则**。
    //
    // ⚠️ **只藏「句子流」那一份**（文件来源）—— 视频来源的
    //    ``#asr-transcript`` 是**跟播放头绑定**的字幕转写框（能上拉回看），
    //    配音导出面板**替代不了**它 ⇒ 留着。
    const dubHas = !!(this._dub && this._dub.hasRows);
    // 音频文件 · 句子流
    const sents = document.getElementById("asr-sentences");
    if (sents) {
      sents.classList.toggle("hidden",
        this._source !== "file" || interpHas || dubHas);
    }
    // 麦克风 · 句子流
    const rtSents = document.getElementById("asr-rt-sentences");
    if (rtSents) {
      rtSents.classList.toggle("hidden",
        this._isFileSource() || interpHas || dubHas);
    }
    // 视频 · 字幕文本（**它自己的规则**在 ``_renderTranscript`` 里）
    const tr = document.getElementById("asr-transcript");
    if (tr) {
      const isVideo = this._source === "video" && this._roundsSource === "video";
      tr.classList.toggle("hidden", !isVideo || interpHas);
    }
    // ⚠️ 顶上那一行提示也跟着走（源语言 → 显示语言）。
    this._syncLiveCap();
    // ⚠️ **框里没内容了 ⇒ 框本身也藏**（不留空白大框）。
    this._syncLiveBox();
  },

  /**
   * 按**播放头**找「当前那一句」（同传结果列表跟字幕节奏走）。
   *
   * ⚠️ **判据与字幕框完全一致**（「时间区间包含播放头」）—— 两边各写
   * 一份的话，早晚对不上（这个坑在字幕框那边已经踩过一次）。
   *
   * ⚠️ **位置未知（实时流）⇒ 找不到**（``start < 0``）⇒ 此时列表退回
   * 「新句子在下面 ⇒ 滚到底」的老行为。
   */
  _interpPickByTime(t) {
    let best = null;
    let bestStart = -1;
    for (const it of this._interp.segs.values()) {
      const s = Number(it.start);
      const e = Number(it.end);
      if (!(s >= 0)) continue;                 // 位置未知（实时流）
      if (t < s) continue;
      if (e > 0 && t >= e) continue;           // 已经过去了
      // ⚠️ **重叠时取「最晚开始」的那句** —— 后一句更贴近当前画面。
      if (s >= bestStart) { best = it; bestStart = s; }
    }
    return best;
  },

  /**
   * **播放头变了 ⇒ 更新「当前正在播报的那一句」**。
   *
   * ⚠️ **为什么要自己做**（2026-11-25，用户要「显示当前正在播放的同传
   * 结果」）：后端的 ``interp_spoken`` 是「这句**播完**了」⇒ 只能标
   * 「已播报」⇒ **看不出现在正在念哪句**。
   * ⇒ 而「正在念哪句」= **播放头所在的那一句**（同传就是贴着字幕播的）
   * ⇒ 前端自己算 ⇒ 不用后端再发一帧。
   */
  _interpSyncCurrent(t) {
    const cur = this._interpPickByTime(Number(t));
    const prev = this._interp.curSeg;
    if (cur === prev) return;
    if (prev) { prev.current = false; this._interpSegPaint(prev); }
    this._interp.curSeg = cur;
    if (cur) {
      cur.current = true;
      this._interpSegPaint(cur);
      this._interpScrollTo(cur);
    }
  },

  /**
   * 把「当前那一句」滚进视野（**不跟用户抢**）。
   *
   * ⚠️ **用户手动上翻过 ⇒ 暂停跟随**（跟字幕框 ``_trFollow`` 同一套
   * 规矩）—— 别把人正在看的内容拽走。
   */
  _interpScrollTo(it) {
    if (!this._interp.follow) return;
    const box = document.getElementById("asr-interp-list");
    if (!box || !it) return;
    if (!box.clientHeight) return;            // 框没布局（隐藏）⇒ 别乱滚
    const rows = box._interpRows;
    const row = rows && rows.get(it.index);
    if (!row) return;
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const viewTop = box.scrollTop;
    const viewBottom = viewTop + box.clientHeight;
    if (top >= viewTop && top + h <= viewBottom) return;   // 已经在视野里
    this._interp.autoAt = Date.now();
    box.scrollTop = (top < viewTop)
      ? Math.max(0, top - 4)
      : Math.max(0, top + h - box.clientHeight + 4);
  },

  /** 用户**手动**滚动同传结果列表 ⇒ 暂停跟随（别把人拽回来）。 */
  _onInterpScroll() {
    // 自己触发的滚动不算「用户滚动」
    if (Date.now() - (this._interp.autoAt || 0) < 250) return;
    const box = document.getElementById("asr-interp-list");
    if (!box || !box.clientHeight) return;
    const cur = this._interp.curSeg;
    if (!cur) { this._interp.follow = false; return; }
    const rows = box._interpRows;
    const row = rows && rows.get(cur.index);
    if (!row) { this._interp.follow = false; return; }
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const visible = (top < box.scrollTop + box.clientHeight)
                 && (top + h > box.scrollTop);
    this._interp.follow = visible;
  },

  /**
   * 画**一句**同传结果（原文 / 译文 / 状态）。
   *
   * ⚠️ **增量** —— 新句才建节点，老句只更新译文那一行。
   */
  _interpSegPaint(it) {
    const box = document.getElementById("asr-interp-list");
    if (!box || !it) return;
    if (!box._interpRows) box._interpRows = new Map();
    const rows = box._interpRows;
    let row = rows.get(it.index);
    if (!row) {
      row = document.createElement("div");
      row.className = "asr-sent asr-interp-sent";
      const src = document.createElement("div");
      src.className = "asr-sent-src";
      const dst = document.createElement("div");
      dst.className = "asr-sent-dst";
      const tag = document.createElement("span");
      tag.className = "asr-interp-tag";
      row.appendChild(src);
      row.appendChild(dst);
      row.appendChild(tag);
      box.appendChild(row);
      rows.set(it.index, row);
    }
    const src = row.querySelector(".asr-sent-src");
    const dst = row.querySelector(".asr-sent-dst");
    const tag = row.querySelector(".asr-interp-tag");
    if (src && src.textContent !== (it.text || "")) {
      src.textContent = it.text || "";
    }
    // ⚠️ **译文还没来 ⇒ 占一行「翻译中」**（跟实时翻译的句子流同口径）
    let txt = it.tr || "";
    let muted = false;
    if (!txt) {
      txt = it.err ? ("（" + it.err + "）") : "翻译中";
      muted = true;
    }
    if (dst && dst.textContent !== txt) dst.textContent = txt;
    if (dst) dst.classList.toggle("is-muted", muted);
    // ⚠️⚠️ **「正在播报」高亮**（2026-11-25，用户要「显示当前正在播放的
    // 同传结果」）—— 由**播放头**算出（``_interpSyncCurrent``）⇒ 不用
    // 后端再发一帧。
    row.classList.toggle("is-current", !!it.current);
    // ⚠️ **把媒体位置挂到 DOM 上**（2026-11-25）—— 行本身**自描述** ⇒
    // ① 调试时 DOM 里就能看出这是第几秒的那一句；② 回归脚本能断言
    // 「高亮的那一句的区间确实包含播放头」（不用猜）。
    const ds = String(Number(it.start) || 0);
    const de = String(Number(it.end) || 0);
    if (row.dataset.start !== ds) row.dataset.start = ds;
    if (row.dataset.end !== de) row.dataset.end = de;
    if (tag) {
      // ⚠️ 板端**没有符号字体** ⇒ 只用文字，不用 ✓ / ● 之类。
      //
      // ⚠️ **三种状态**：正在播报（播放头在这儿）/ 已播报（这句念完了）/
      // 未播出（这句没念成）。
      const t = it.current ? "正在播报"
              : (it.err ? "未播出" : (it.spoken ? "已播报" : ""));
      if (tag.textContent !== t) tag.textContent = t;
      tag.classList.toggle("hidden", !t);
      tag.classList.toggle("is-live", !!it.current);
    }
  },

  /* ---------------- 播放头（跟播放模式） ---------------- */

  /**
   * **标签页 id**（2026-11-25）—— 同传引擎是**服务端全局单例** ⇒ 多个
   * 标签页会**共用一个播放头槽位** ⇒ 后上报的覆盖先上报的。
   *
   * ⇒ 实测那一轮日志里有 **6 个浏览器连接**同时上报播放头（172.16.9.18
   * 的 6 个源端口）⇒ 播放头在 6 个位置之间**来回跳** ⇒ 时钟的对齐判定
   * 完全失效 ⇒ **声音不跟随字幕**（用户原话）。
   *
   * ⇒ 用 **sessionStorage** 存一个 id：
   * * 不同标签页 ⇒ 不同 id ⇒ 互相不覆盖；
   * * **刷新后不变** ⇒ 刷新后接管的服务端会话仍然认这个标签页
   *   （``_interpRestoreState`` 不重新 start ⇒ 靠的就是这个 id 不变）。
   */
  _interpClientId() {
    if (this._interp.clientId) return this._interp.clientId;
    let id = "";
    try {
      id = window.sessionStorage.getItem("wdn_qwen3_asr_client") || "";
      if (!id) {
        id = "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
        window.sessionStorage.setItem("wdn_qwen3_asr_client", id);
      }
    } catch (e) {
      // ⚠️ **隐私模式下 sessionStorage 可能不可用** ⇒ 退化成「本次页面
      // 的随机 id」（刷新后会变 ⇒ 服务端会拒收 ⇒ 面板会提示重新开始）。
      id = "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    }
    this._interp.clientId = id;
    return id;
  },

  /** 当前播放头（秒，音频时间轴）；没有播放头 ⇒ null。 */
  _interpPlayhead() {
    const src = this._source || "file";
    if (src === "video") {
      const v = document.getElementById("asr-video");
      if (!v) return null;
      // ⚠️ **媒体还没加载 ⇒ 位置没有意义**（2026-11-25）——
      //
      // 刷新页面后媒体元素是**新的**（``currentTime`` = 0）⇒ 报一个「0」
      // 上去 ⇒ 服务端以为用户**跳回开头** ⇒ 把待播的句子全当成「被跳过」
      // 丢掉。
      //
      // ⇒ **没有源就不上报**（``null`` ⇒ 前端跳过这次上报）。
      if (!v.src && !v.currentSrc) return null;
      const t = (v.currentTime || 0) - (this._audioOriginSec || 0);
      return {
        sec: Math.max(0, t),
        playing: !v.paused && !v.ended,
        // ⚠️ **源播完了**（媒体 ended）⇒ 源不会再往前走 ⇒ 放行器不再等
        // （不然剩下的句子被永久压住 ⇒ 用户只听到第一句）。
        ended: !!v.ended,
        failed: !!(v.error),
      };
    }
    if (src === "file") {
      const a = document.getElementById("asr-audio-player");
      if (!a) return null;
      if (!a.src && !a.currentSrc) return null;
      return {
        sec: Math.max(0, a.currentTime || 0),
        playing: !a.paused && !a.ended,
        ended: !!a.ended,
        failed: !!(a.error),
      };
    }
    return null;
  },

  /** 每 250 ms 上报一次播放头（只在「跟播放」且有播放头时）。 */
  _interpStartPlayhead() {
    this._interpStopPlayhead();
    this._interp.phTimer = setInterval(() => {
      if (!this._interp.running) return;
      if (this._interp.gate !== "follow") return;
      if (!this._interpHasPlayhead()) return;
      const ph = this._interpPlayhead();
      if (!ph) return;
      // ⚠️ 媒体**真的在播**了 ⇒ 「没在放」不再成立 ⇒ 清原因。
      if (ph.playing) this._interp.mediaBlocked = "";
      // ⚠️ **「结束 / 失败」也算变化** —— 媒体播完的那一刻 ``playing``
      // 会从 true 变 false（会被上报），但「源结束了」这个状态必须
      // **单独**让服务端知道 ⇒ 不然它只会看到「暂停」⇒ 以为还能等。
      //
      // ⚠️⚠️ **心跳**（2026-11-25）—— 播放头**没变**时也要定期上报：
      //
      // 原来只在**变化**时上报 ⇒ 媒体**暂停**在某个位置后就**再也不报了**
      // ⇒ 服务端那边「播放头多久没来」就一直涨 ⇒ 被误判成「**前端失联**」
      // ⇒ 面板会说「播放头没上报」—— 而真相是「**媒体暂停着**」。
      //
      // ⇒ 这两件事**必须分开说**：前端还在不在（心跳）≠ 媒体在不在动。
      // ⇒ 心跳让服务端的「多久没来」**只反映前端在不在**。
      const nowMs = Date.now();
      const changed = Math.abs(ph.sec - this._interp.lastPh) >= 0.05
        || ph.playing !== this._interp.lastPlaying
        || !!ph.ended !== !!this._interp.lastEnded
        || !!ph.failed !== !!this._interp.lastFailed;
      const beat = (nowMs - (this._interp.lastPhAt || 0)) >= PH_HEARTBEAT_MS;
      if (!changed && !beat) return;
      this._interp.lastPh = ph.sec;
      this._interp.lastPlaying = ph.playing;
      this._interp.lastEnded = !!ph.ended;
      this._interp.lastFailed = !!ph.failed;
      this._interp.lastPhAt = nowMs;   // 心跳的基准（见上面的说明）
      // ⚠️⚠️ **同传结果列表跟播放头走**（2026-11-25，用户要「显示当前正在
      // 播放的同传结果」）—— 高亮「播放头所在的那一句」+ 滚到它 ⇒
      // ⇒ 列表的节奏 = **字幕的节奏**（不是「新句子在下面」）。
      this._interpSyncCurrent(ph.sec);
      // ⚠️ **带上引擎当前的流 id** —— 播放头是**按流**存的 ⇒ id 对不上
      // ⇒ 放行器配不上这句 ⇒「跟播放」把它永久压住 ⇒「有翻译没声音」
      // （实测：前端之前一直报空 id ⇒ 必然配不上）。
      const sid = (this._interp.status && this._interp.status.stream_id)
        || this._interp.streamId || "";
      fetch("/api/qwen3-asr/interp/playhead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stream_id: sid,
          playhead_sec: ph.sec,
          playing: ph.playing,
          // ⚠️ **「源结束了 / 媒体坏了」必须一起报** —— 服务端据此判断
          // 「不会再有新的源内容了」⇒ 剩下的译文**直接补播**
          // （不然媒体播完之后识别出来的句子会被永久压住 ⇒
          //  **用户只听到第一句**，实测踩过）。
          ended: !!ph.ended,
          failed: !!ph.failed,
          // ⚠️ **标签页 id**（2026-11-25）—— 引擎是全局单例 ⇒ 多个标签页
          // 的播放头会互相覆盖 ⇒ 只认**发起这一轮**的那个标签页。
          client_id: this._interpClientId(),
        }),
      })
        .then((r) => (r && r.json ? r.json() : null))
        .then((d) => {
          // ⚠️ **服务端说这次上报被忽略了** ⇒ 这个标签页**不是发起同传的
          // 那个** ⇒ 它的播放头**不算数** ⇒ 得告诉用户（否则用户看到的是
          // 「播放头没上报」，会以为是页面坏了）。
          if (d && d.ignored) this._interp.phIgnored = true;
        })
        .catch(() => { /* 上报失败不影响播放 */ });
    }, 250);
  },

  _interpStopPlayhead() {
    if (this._interp.phTimer) {
      clearInterval(this._interp.phTimer);
      this._interp.phTimer = null;
    }
  },

  /** 文件 / 视频源：开始同传时**自动播放**媒体（播放头不动 ⇒ 跟播放放不出声）。 */
  _interpAutoPlay() {
    const src = this._source || "file";
    const el = src === "video"
      ? document.getElementById("asr-video")
      : document.getElementById("asr-audio-player");
    if (!el) return;
    const hint = document.getElementById("asr-interp-hint");
    const play = () => {
      try {
        const p = el.play();
        if (p && typeof p.catch === "function") {
          p.catch(() => {
            // 两种可能：**自动播放被浏览器策略拦了**，或者**媒体本身加载失败**。
            // ⚠️ 后者更隐蔽（元素一直 paused ⇒ 播放头不动 ⇒ 跟播放压住
            // 所有句子 ⇒ 「有翻译但没声音」）⇒ 分开说清楚。
            //
            // ⚠️ **记成状态**（不是只写一句）—— 面板提示每秒会被
            // ``_interpSyncUi`` 刷一遍 ⇒ 只在这里写的话下一秒就没了。
            const e = el.error;
            this._interp.mediaBlocked = e
              ? "媒体加载失败（code " + e.code + "）—— 换个浏览器能放的格式。"
              : "浏览器拦了自动播放 —— 点媒体的播放键，译文才会跟着走。";
            if (hint) hint.textContent = this._interp.mediaBlocked;
          });
        }
      } catch (e) { /* noop */ }
    };
    play();
    // ⚠️ **媒体还没加载完时 play() 可能无效**（src 后到 / 缓冲中）⇒
    // 补一次「加载完就播」。这次调用**没有**用户手势 ⇒ 若策略仍拦，
    // 上面的 catch 会给出提示。
    if (el.readyState < 3) {
      const once = () => {
        el.removeEventListener("loadeddata", once);
        el.removeEventListener("canplay", once);
        play();
      };
      el.addEventListener("loadeddata", once);
      el.addEventListener("canplay", once);
    }
  },

  /* ---------------- 音频文件源的播放器（2026-11-21） ---------------- */

  /** 音频文件源的 URL（本机上传 ⇒ blob；设备端路径 ⇒ media 端点）。 */
  _audioUrl() {
    if (this._file && this._fileBlobUrl) return this._fileBlobUrl;
    const p = (this._path || "").trim();
    if (!p) return "";
    return API_PREFIX + "/qwen3-asr/media?path=" + encodeURIComponent(p);
  },

  /**
   * 同步音频播放器（文件源专用）。
   *
   * ⚠️ **不能用向导的 ``#asr-preview-audio``** —— 那个只在点「预览播放」
   * 时加载、只吃设备端路径、还会被向导框架搬来搬去 ⇒ 拿它当播放头不可靠。
   */
  _syncAudioPlayer() {
    const wrap = document.getElementById("asr-audio-wrap");
    const el = document.getElementById("asr-audio-player");
    const hint = document.getElementById("asr-audio-hint");
    if (!wrap || !el) return;
    const isFile = this._source === "file";
    wrap.classList.toggle("hidden", !isFile);
    if (!isFile) {
      try {
        el.pause();
        el.removeAttribute("src");
        el.load();
      } catch (e) { /* noop */ }
      this._interp.audioSrc = "";
      if (hint) hint.textContent = "";
      return;
    }
    const url = this._audioUrl();
    if (!url) {
      if (hint) {
        hint.textContent = "选一个音频文件，这里会同步播放，译文跟着它走。";
      }
      return;
    }
    if (url !== this._interp.audioSrc) {
      this._interp.audioSrc = url;
      el.src = url;
      try { el.load(); } catch (e) { /* noop */ }
    }
    if (hint) {
      hint.textContent = this._file
        ? "正在播本机上传的音频（同传译文跟着它走）。"
        : "正在播设备端的音频（同传译文跟着它走）。";
    }
  },

  _onFrame(msg) {
    const pre = document.getElementById("data-qwen3-asr");
    if (pre) pre.textContent = JSON.stringify(msg, null, 2);

    switch (msg.type) {
      case "snapshot":
      case "state":
        this._applySnapshot(msg.state || msg.snapshot);
        break;
      case "round": {
        // 实时模式的 round 帧带 stream_id → 走实时显示，不进文件模式的列表。
        if (msg.stream_id) {
          const L = this._live;
          L.committed = msg.committed || "";
          L.unfixed = msg.unfixed || "";
          if (msg.language) L.language = msg.language;
          this._liveRender();
          break;
        }
        // ⚠️ 「清空」之后迟到的轮次帧 → 忽略（否则清完又长回来）。
        if (this._ignoreRounds) break;
        this.rounds.push({
          round: msg.round,
          committed: msg.committed || "",
          unfixed: msg.unfixed || "",
          language: msg.language || "",
          final: !!msg.final,
          // ⚠️ 时间戳**必须留着** —— 视频来源要靠它把字幕对齐到画面
          // （``chunk_start_sec`` / ``chunk_end_sec`` 相对**音轨**起点）。
          chunk_start_sec: msg.chunk_start_sec,
          chunk_end_sec: msg.chunk_end_sec,
        });
        // 来源还没标过（**刷新页面时识别正在跑**的场景：轮次是 WS 推来的，
        // 本地没有走过 start()）→ 按当前来源认领。
        // ⚠️ 用「为空才补」而不是「每次覆盖」：识别中途切来源时，
        // 轮次仍属于**原来源**，覆盖会把字幕串到新画面上。
        // ⚠️ **翻译的单位也跟着认领**（`_claimRoundsSource`）—— 不然
        // 刷新后 `_trMode` 还是默认的 "sentence" ⇒ 行级翻译一次都不发起
        // ⇒ 刷新后一句译文都没有。
        this._claimRoundsSource(this._source, false);
        // 音轨在容器里的起点（容器里音轨可能晚于画面起点）
        if (msg.audio_origin_sec !== undefined
            && msg.audio_origin_sec !== null) {
          this._audioOriginSec = Number(msg.audio_origin_sec) || 0;
        }
        // 记下「这一秒已经识别过了」→ 拖动进度条时靠它判断要不要
        // 把识别**开过去**（见 _maybeFollowSeek）。
        if (msg.chunk_start_sec !== undefined && msg.chunk_start_sec !== null) {
          this._subNoteRange(msg.chunk_start_sec, msg.chunk_end_sec);
        }
        // ⚠️ **句子由服务端切**（随这一帧送来）⇒ 前端不自己断句。
        // 断句口径只有一份（服务端的 `_SentenceSplitter`）—— 自己断就是
        // 两份口径，迟早漂移。
        this._sentPush(msg.sentences);
        // 「已确认但还没成句」的尾巴 —— **服务端算好**（``committed`` 是段内
        // 累积文本，前端自己切会跨段错位）。
        if (msg.sentence_tail !== undefined && msg.sentence_tail !== null) {
          this._sentTail = String(msg.sentence_tail || "");
        }
        this._renderLive(msg.committed || "", msg.unfixed || "");
        this._renderRounds();
        this._renderVideoSubtitle();
        // ⚠️ **行级翻译**：时间轴长了一行 ⇒ 之前那行**定稿**了 ⇒ 该要
        // 它的译文了（译文跟那一行走 ⇒ 节奏和字幕一致）。
        this._trRequestLines(false);
        const empty = document.getElementById("asr-empty");
        if (empty) empty.classList.add("hidden");
        if (this.snapshot) {
          this.snapshot.round_count = this.rounds.length;
          this.snapshot.language = msg.language || this.snapshot.language;
          this._applySnapshot(this.snapshot);
        }
        break;
      }
      case "gap": {
        // 音频有丢失 → 识别结果可能有缺口。
        if (msg.stream_id) this._liveGapHint(msg);
        break;
      }
      case "endpoint": {
        // 一句话结束（端点检测命中）→ 拼到已完成的句子里。
        if (!msg.stream_id) break;
        const L = this._live;
        const text = (msg.text || "").trim();
        if (text) L.segments.push(text);
        L.committed = "";
        L.unfixed = "";
        this._liveRender();
        this._liveStatus(`又识别了一句（第 ${msg.index || 0} 句）`);
        break;
      }
      case "live_done": {
        if (!msg.stream_id) break;
        this._liveStatus(
          `实时会话结束（${msg.reason || "done"}）· 丢块 ${msg.dropped_chunks || 0}`);
        break;
      }
      case "translation": {
        // 一句译文（流式同传）—— 与识别帧**同一条 WS**。
        if (!msg.stream_id) break;
        const it = {
          index: Number(msg.index) || 0,
          source: msg.source || "",
          text: msg.text || "",
          status: msg.status || "failed",
          reason: msg.reason || "",
          latency_ms: Number(msg.latency_ms) || 0,
          // ⚠️ **时间范围**（可选）—— 文件 / 视频路径给 ⇒ 画面字幕能把它
          // 挂到对应的字幕行上（双语字幕，译文跟着字幕的节奏走）。
          start_sec: (msg.start_sec === undefined || msg.start_sec === null)
            ? null : Number(msg.start_sec),
          end_sec: (msg.end_sec === undefined || msg.end_sec === null)
            ? null : Number(msg.end_sec),
          // ⚠️ **行级**翻译 ⇒ 前端给这一行编的序号（原样带回）。
          // 句子级翻译没有这个（``null``）。
          seq: (msg.seq === undefined || msg.seq === null)
            ? null : Number(msg.seq),
        };
        // ⚠️ **行级** ⇒ 译文挂到**那一行**上（一一对应）。
        //
        // ⚠️ 按**槽位号**对（``epoch:行号``），不按行 id —— 行 id 里含文本
        // 和时间 ⇒ 模型改个词就变了 ⇒ 译文会「对不上行」。
        let expired = false;
        if (it.seq !== null && this._trMode === "line") {
          const slot = this._trLineIdBySeq.get(it.seq);
          const info = slot ? this._trLineSlot.get(slot) : null;
          // ⚠️ 这一行**已经又改过**了 ⇒ 这条是**过期**的译文 ⇒ 丢掉
          if (slot && info && info.seq === it.seq) {
            info.item = it;
            this._trLineTr.set(slot, it);
            // ⚠️ **翻失败了**（``failed`` / ``unavailable``）⇒ 这一行
            //    **没有**译文（服务端广播的 text 是「原文」）⇒ 它得
            //    **再要一次** ⇒ 把**重试时间**推到现在 ⇒ 等够
            //    ``TR_REQ_STALE_MS`` 才会重试（否则会热循环）。
            //
            // ⚠️ ``skipped``（源语 == 目标语）**不重试** —— 再要也是同一个。
            if (it.status && it.status !== "done" && it.status !== "skipped") {
              const req = this._trLineReq.get(slot);
              // ⚠️ **重试次数要带上**（2026-11-26）—— 否则这里把 ``tries``
              // 抹掉 ⇒ 封顶失效 ⇒ 无限重试又回来了。
              if (req) {
                this._trLineReq.set(slot, { text: req.text, at: Date.now(),
                                           tries: Number(req.tries || 1) });
              }
            }
          } else {
            expired = true;
          }
        } else if (it.status && it.status !== "done" && it.status !== "skipped") {
          // ⚠️ **句子级**翻失败了 ⇒ 同样把**重试时间**推到现在
          //    （否则一句失败的话会**立刻**被重试 ⇒ 热循环）。
          const src = (it.source || "").trim();
          if (src) {
            for (const [k, v] of this._trSentReq) {
              if (v && this._trNormText(v.text) === this._trNormText(src)) {
                this._trSentReq.set(k, { text: v.text, at: Date.now() });
              }
            }
          }
        }
        // ⚠️ **按句序号覆盖**，不是 append —— 翻译是**乱序完成**的
        //（多实例并行），append 会让字幕顺序乱掉。
        if (!expired) {
          const pos = this._tr.items.findIndex((x) => x.index === it.index);
          if (pos >= 0) this._tr.items[pos] = it;
          else this._tr.items.push(it);
        }
        this._tr.items.sort((a, b) => a.index - b.index);
        // 版本号 +1 ⇒ 「按时间挂译文」的缓存失效（字幕下一帧就会重画）
        this._trVer++;
        this._trRender();
        this._trSyncUi();
        // 转写框（视频来源）**立刻**补上译文 —— 视频**暂停**时 rAF 循环
        // 不跑 ⇒ 只靠 rAF 的话，译文要等用户点了播放才出现。
        this._renderTranscript();
        break;
      }
      case "done": {
        // ⚠️ 收尾时切出来的**最后一句**跟着这一帧来（没有下一帧可以搭
        // 便车了）⇒ 不接住它，那句话就永远不出现。
        this._sentPush(msg.sentences);
        // ⚠️ **尾巴要清掉** —— 收尾把尾巴切成了句子 ⇒ 尾巴还留着的话，
        // 那句话在屏幕上出现两遍（一次是尾巴，一次是句子）。
        if (msg.sentence_tail !== undefined && msg.sentence_tail !== null) {
          this._sentTail = String(msg.sentence_tail || "");
        }
        this._renderLive(msg.text || "", "");
        this._renderRounds();
        // ⚠️ 识别结束 ⇒ **最后一行**也定稿了 ⇒ 把它要过来（不然它永远
        // 没有译文）。
        this._trRequestLines(true);
        const hint = document.getElementById("asr-hint");
        if (hint) {
          // 音频时长 + 耗时都报出来 —— 用户能看出「多长的音频被识别了」
          // （不再有 60 秒截断，所以这个数字可能远大于 60）。
          const audio = msg.audio_sec ? ` · 音频 ${msg.audio_sec.toFixed(1)} s` : "";
          hint.textContent = `识别完成（${(msg.elapsed_sec || 0).toFixed(2)} s${audio}）`
            + `· 语种 ${msg.language || "未知"}`;
        }
        break;
      }
      case "stopped": {
        // 停止 → **正常收尾**，不是失败：把已识别的文本交回界面。
        // （后端在取消时不再广播 error 帧。）
        // ⚠️ 收尾时切出来的**最后一句**跟着这一帧来（同 ``done``）。
        this._sentPush(msg.sentences);
        if (msg.sentence_tail !== undefined && msg.sentence_tail !== null) {
          this._sentTail = String(msg.sentence_tail || "");
        }
        this._renderLive(msg.text || "", "");
        this._renderRounds();
        // ⚠️ **要不要把画面也停住，取决于「谁喊的停」**：
        //   * ``internal`` —— 前端为了「拖动进度条 → 重开识别」而停的
        //     ⇒ **画面继续**（用户在拖进度条看视频，冻了就看不了）。
        //   * ``user`` —— 用户点了「停止」⇒ 画面停（字幕别再滚）。
        //     本页面点的 → click 那一刻就冻过了 → 这里**别再冻**
        //     （用户可能已经自己按了播放）；别的标签页点的 → 本页面
        //     没冻过 → 要冻（否则那边停了这边字幕还在滚）。
        const internal = (msg.reason === "internal");
        if (!internal && !this._stopInitiated) this._freezePlayback();
        const hint = document.getElementById("asr-hint");
        if (hint && !internal) {
          const audio = msg.audio_sec
            ? ` · 已识别 ${msg.audio_sec.toFixed(1)} s` : "";
          hint.textContent = `已停止${audio} —— 已识别的内容保留在下方，`
            + "要重跑点「开始识别」。";
        }
        break;
      }
      case "interp_status": {
        // 同传的指标（每秒一帧）。
        this._interp.status = { ...(this._interp.status || {}), ...msg };
        this._interpSyncUi();
        break;
      }
      case "interp_segment": {
        // 成句（还没播）—— 面板上记一句 + **页面底部**列一行（原文 + 占位）。
        this._interp.lastSegment = msg;
        this._interpSegUpsert({
          index: Number(msg.index) || 0,
          text: msg.text || "",
          lang: msg.lang || "",
          start: Number(msg.start_sec) || 0,
          end: Number(msg.end_sec) || 0,
        });
        break;
      }
      case "interp_translated": {
        // ⚠️ **译文回来了（完整译文）** —— 后端 2026-11-25 新增的一帧。
        // 之前没有这一帧 ⇒ 前端拿不到完整译文 ⇒ 同传**没法列结果**。
        this._interpSegUpsert({
          index: Number(msg.index) || 0,
          text: msg.text || "",
          tr: msg.tr || "",
          lang: msg.lang || "",
          start: Number(msg.start_sec) || 0,
          end: Number(msg.end_sec) || 0,
        });
        break;
      }
      case "interp_spoken": {
        // 播完一句。
        this._interp.lastSpoken = msg;
        // ⚠️ 标「已播报」（结果列表里能看出播到哪了）。
        const seg = this._interpSegFind(msg.index);
        if (seg && !seg.spoken) {
          seg.spoken = true;
          this._interpSegPaint(seg);
        }
        break;
      }
      case "interp_error": {
        const hint = document.getElementById("asr-interp-status");
        if (hint && msg.reason) {
          hint.textContent = "同传提示：" + msg.reason;
        }
        // ⚠️ **这一句失败了 ⇒ 在结果列表里标出来**（别让用户以为还在等）。
        const seg = this._interpSegFind(msg.index);
        if (seg) {
          seg.err = msg.reason || "翻译失败";
          this._interpSegPaint(seg);
        }
        break;
      }
      case "error":
        if (msg.stream_id) {
          // 实时会话的错（熔断 / 能力不可用）→ 落在实时区块里，
          // 不要覆盖文件模式的状态行。
          this._liveStatus("出错：" + (msg.detail || "识别出错"));
          break;
        }
        this._showError(msg.detail || "识别出错");
        break;
      default:
        break;
    }
  },

  /* ------------------------------------------------------------------ *
   * 配音导出（2026-11-26）
   *
   * 「音频文件」/「含音轨的视频」两个来源用这一块（麦克风两种来源用
   * 「同声传译」）。流程：ASR → 翻译 → TTS ⇒ 目标语言的音轨；视频还会
   * 再烧一层**双字幕**并与新音轨重新封装。
   *
   * ⚠️ 产物落在**源文件同级目录**，文件名 = 源文件名 + 目标语言 + 音色
   *    ⇒ 生成完把目录里已有的产物列出来，用户可以**挑一个播放**。
   * ------------------------------------------------------------------ */
  async _dubLoadOptions() {
    if (this._dub.loaded) return;
    this._dub.loaded = true;
    const $ = (id) => document.getElementById(id);

    // ---- 目标语言：能出声的（TTS 会念的那些）----
    const langSel = $("asr-dub-target");
    if (langSel) {
      try {
        const r = await fetch("/api/qwen3-asr/interp/languages",
                              { cache: "no-store" });
        const d = await r.json();
        const items = (d && d.items) || [];
        this._dub.langItems = items;
        langSel.innerHTML = "";
        items.forEach((it) => {
          const o = document.createElement("option");
          o.value = it.id;
          // ⚠️ **显示格式与「实时翻译」的「显示语言」完全一致** ——
          //    「中文名 本族名」（例：英语 English）⇒ 三个语言下拉看起来
          //    是**同一套**排法 ⇒ 用户不会以为这是两套东西。
          // ⚠️ **必须用 `_trLangLabel`**（**同一份**标签表 + 字体兜底）⇒
          //    别在这里自己拼 ⇒ 标签表只有一份 ⇒ 不会漂。
          o.textContent = this._trLangLabel(it);
          langSel.appendChild(o);
        });
        // ⚠️ **默认跟随「实时翻译」的显示语言**（没开就退回第一条）。
        //
        // ⚠️⚠️ **只跟这一次** —— 本函数首行就是 ``if (this._dub.loaded) return;``
        // ⇒ 用户之后改「显示语言」，**这里不会再变**。
        //
        // ⇒ 这是**故意的**（2026-11-27）：导出语言是**产物**的语言 ⇒ 必须由
        //    用户**显式**决定 ⇒ 绝不能被「显示语言」的改动悄悄带偏。
        //    （用户实测报过「导出选英文、出来的文件却是中文」⇒ 根因就是
        //      两个下拉**同名** + 默认联动 ⇒ 现在 UI 上叫「导出语言」，
        //      且明写了与「显示语言」互不影响。）
        const want = this._tr.targetLang;
        const hit = items.find((it) => it.id === want);
        langSel.value = hit ? hit.id : (items[0] ? items[0].id : "");
      } catch (e) {
        langSel.innerHTML = "<option value=''>（语言列表拿不到）</option>";
      }
      langSel.addEventListener("change", () => {
        this._dub.targetLang = langSel.value;
      });
      this._dub.targetLang = langSel.value;
    }

    // ---- 音色：跟「语音生成」卡同一份表（``/api/tts/voices``）----
    const voiceSel = $("asr-dub-voice");
    if (voiceSel) {
      try {
        const r = await fetch("/api/tts/voices", { cache: "no-store" });
        const d = await r.json();
        const voices = (d && d.voices) || [];
        this._dub.voiceItems = voices;
        voiceSel.innerHTML = "";
        voices.forEach((v) => {
          const o = document.createElement("option");
          const kind = v.kind || "ref";
          o.value = kind + ":" + v.id;
          const tag = kind === "builtin" ? "内置" : (kind === "user" ? "自定义" : "参考");
          o.textContent = (v.name || v.id) + "（" + tag + "）";
          voiceSel.appendChild(o);
        });
        // ⚠️ 默认用「参考音色」的第一条 —— 本板**只有**参考音色能用
        // （内置音色 SDK 会打印「not in SPK_ID, skipping」⇒ 音色不生效）。
        const firstRef = voices.find((v) => (v.kind || "ref") === "ref");
        voiceSel.value = firstRef ? "ref:" + firstRef.id
                                  : (voices[0] ? "ref:" + voices[0].id : "");
      } catch (e) {
        voiceSel.innerHTML = "<option value=''>（音色列表拿不到）</option>";
      }
      voiceSel.addEventListener("change", () => {
        this._dubSetVoice(voiceSel.value);
      });
      this._dubSetVoice(voiceSel.value);
    }
  },

  /**
   * 音色选择框的值（``kind:id``）⇒ 拆成**两个不同的 SDK 参数**。
   *
   * ⚠️ **混用 ⇒ 音色不生效**（SDK 打印「not in SPK_ID, skipping」⇒
   * 每句话音色都不一样）—— 与同传面板同一坑。
   */
  _dubSetVoice(value) {
    const v = String(value || "");
    const idx = v.indexOf(":");
    const kind = idx >= 0 ? v.slice(0, idx) : "ref";
    const vid = idx >= 0 ? v.slice(idx + 1) : v;
    if (kind === "builtin") {
      this._dub.speaker = vid;
      this._dub.refSpeaker = "";
    } else {
      this._dub.refSpeaker = vid;
      this._dub.speaker = "";
    }
  },

  /** 配音导出用的源（设备端路径优先 —— 上传的先传上去）。 */
  _dubSourcePath() {
    if (this._path) return this._path;
    return "";
  },

  /**
   * 把本机上传的文件**传到设备端**（导出必须在设备端做：产物要落在
   * 源文件同级目录，而浏览器里的 blob 没有「同级目录」这回事）。
   */
  async _dubUpload(file) {
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const r = await fetch("/api/qwen3-asr/upload", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok || !d.path) {
        this._dubSetStatus("上传失败：" + (d.detail || "未知错误"));
        return "";
      }
      // ⚠️ 记住设备端路径 ⇒ 后面「列产物」也有路径可用。
      this._path = d.path;
      const el = document.getElementById("asr-source");
      if (el) el.value = d.path;
      return d.path;
    } catch (e) {
      this._dubSetStatus("上传失败：" + e);
      return "";
    }
  },

  async _dubStart() {
    const $ = (id) => document.getElementById(id);
    let path = this._dubSourcePath();
    if (!path && this._file) {
      // ⚠️ 本机上传的文件**先传到设备端** —— 导出要在设备端跑，产物也
      // 要落在设备端（源文件同级目录）。
      this._dubSetStatus("正在上传到设备端…");
      path = await this._dubUpload(this._file);
      if (!path) return;
    }
    if (!path) {
      this._dubSetStatus("先选一个音频文件或含音轨的视频");
      return;
    }
    const target = this._dub.targetLang ||
      (this._dub.langItems && this._dub.langItems[0]
        ? this._dub.langItems[0].id : "");
    if (!target) {
      this._dubSetStatus("没有可用的目标语言");
      return;
    }
    this._dubSetStatus("正在提交…");
    try {
      const r = await fetch("/api/qwen3-asr/dub/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: this._source === "video" ? "video" : "audio",
          path: path,
          target_lang: target,
          speaker: this._dub.speaker,
          ref_speaker: this._dub.refSpeaker,
        }),
      });
      const d = await r.json();
      if (!r.ok) {
        this._dubSetStatus("起不来：" + (d.detail || d.error || "未知错误"));
        return;
      }
      this._dub.running = true;
      this._dub.state = d.state || "running";
      // ⚠️ 记下作业的**原始**源路径 ⇒ 列产物用它（见 ``_dubRefreshFiles``）
      this._dub.sourcePath = d.source_path || path;
      this._dubSetStatus("已提交，正在处理…");
      this._dubPoll();
    } catch (e) {
      this._dubSetStatus("提交失败：" + e);
    }
  },

  async _dubStop() {
    try {
      await fetch("/api/qwen3-asr/dub/stop", { method: "POST" });
    } catch (e) { /* 忽略 */ }
    this._dub.running = false;
    this._dubPollStop();
    this._dubSetStatus("已停止");
  },

  _dubPollStop() {
    if (this._dub.timer) {
      clearInterval(this._dub.timer);
      this._dub.timer = null;
    }
  },

  _dubPoll() {
    this._dubPollStop();
    this._dub.timer = setInterval(() => this._dubTick(), 1500);
    this._dubTick();
  },

  async _dubTick() {
    let d = null;
    try {
      const r = await fetch("/api/qwen3-asr/dub/status", { cache: "no-store" });
      d = await r.json();
    } catch (e) {
      return;
    }
    if (!d) return;
    this._dub.state = d.state || "";
    this._dub.phase = d.phase || "";
    this._dub.progress = Number(d.progress) || 0;
    this._dub.message = d.message || "";
    this._dub.error = d.error || "";
    this._dub.audioPath = d.audio_path || "";
    this._dub.videoPath = d.video_path || "";
    this._dub.sourcePath = d.source_path || "";
    // ⚠️ **源语言 + 导出语言**（2026-11-27）—— 结果面板要标这两种语言。
    if (d.source_lang) this._dub.sourceLang = d.source_lang;
    if (d.target_lang) this._dub.targetLang = d.target_lang;
    // ⚠️ **逐句结果** —— 配音导出**做了完整的翻译** ⇒ 原文 + 译文都在
    //    作业里 ⇒ 这里渲染出来（以前前端根本没读 `sentences`）。
    this._dubRenderSentences(d.sentences || []);

    const pct = Math.round((this._dub.progress || 0) * 100);
    if (this._dub.state === "running") {
      this._dubSetStatus((this._dub.phase || "处理中") + "（" + pct + "%）");
    } else if (this._dub.state === "done") {
      this._dub.running = false;
      this._dubPollStop();
      this._dubSetStatus("完成");
      await this._dubRefreshFiles();
    } else if (this._dub.state === "error") {
      this._dub.running = false;
      this._dubPollStop();
      this._dubSetStatus("失败：" + (this._dub.error || "未知错误"));
      await this._dubRefreshFiles();
    } else if (this._dub.state === "stopped") {
      this._dub.running = false;
      this._dubPollStop();
      this._dubSetStatus("已停止");
    }
    this._dubSyncButtons();
  },

  _dubSyncButtons() {
    const $ = (id) => document.getElementById(id);
    const start = $("asr-dub-start");
    const stop = $("asr-dub-stop");
    // ⚠️⚠️ **「有源」= 设备端路径 **或** 本机上传的文件**（2026-11-26）。
    //
    // 之前只看 ``_dubSourcePath()``（设备端路径）⇒ **本机上传的文件永远
    // 判成「没源」⇒ 按钮一直点不动**（用户实测：「选好音频文件，生成配音版
    // 不能点击」）。
    //
    // ⇒ 本机上传的文件**也能导** —— ``_dubStart()`` 会先把它传到设备端
    //    （导出必须在设备端跑，产物也落在设备端）。
    const hasSrc = !!(this._path || this._file);
    if (start) start.disabled = this._dub.running || !hasSrc;
    if (stop) stop.disabled = !this._dub.running;
  },

  /**
   * 拉一次服务端**最新那个作业**的状态。
   *
   * ⚠️ **为什么要有它**（2026-11-27）—— 配音导出的作业是**服务端**记着的
   *    ⇒ 刷新页面后作业还在、结果还在 ⇒ 但页面只在「点了生成配音版」之后
   *    才开始轮询 ⇒ 刷新一下，上次导出的逐句结果就**看不见了**。
   * ⇒ 这里进页面先拉一次：把上次的源语言 / 导出语言 / 逐句结果补上。
   */
  async _dubRefreshStatus() {
    let d = null;
    try {
      const r = await fetch("/api/qwen3-asr/dub/status", { cache: "no-store" });
      d = await r.json();
    } catch (e) {
      return;
    }
    if (!d || !d.id) return;
    this._dub.sourcePath = d.source_path || "";
    if (d.source_lang) this._dub.sourceLang = d.source_lang;
    if (d.target_lang) this._dub.targetLang = d.target_lang;
    this._dubRenderSentences(d.sentences || []);
    if (d.state === "running") {
      this._dub.running = true;
      this._dubPoll();
    } else if (d.state === "done") {
      this._dubSetStatus("完成");
      await this._dubRefreshFiles();
    } else if (d.state === "error") {
      this._dubSetStatus("失败：" + (d.error || "未知错误"));
      await this._dubRefreshFiles();
    }
    this._dubSyncButtons();
  },

  _dubSetStatus(text) {
    const el = document.getElementById("asr-dub-status");
    if (el) el.textContent = text || "";
  },

  /** 收掉「配音导出结果」面板（⇒ 「实时结果」框回来）。 */
  _dubClearResult() {
    const box = document.getElementById("asr-dub-list");
    const wrap = document.getElementById("asr-dub-out");
    this._dub.hasRows = false;
    if (wrap) wrap.classList.add("hidden");
    if (box) {
      if (box._rows) box._rows.clear();
      while (box.firstChild) box.removeChild(box.firstChild);
    }
    this._syncResultBoxes();
  },

  /**
   * 渲染「配音导出结果」—— 逐句**原文 / 译文**，并标清两种语言。
   *
   * ⚠️ **为什么要有这个面板**（2026-11-27 用户要求）——
   * 配音导出**做了完整的翻译**（ASR → 翻译 → TTS）⇒ 每一句的原文和译文
   * 都在作业里（``job.sentences``）⇒ 但页面以前只给一句「完成」和一个
   * 文件下拉 ⇒ 用户**看不到导出的到底是什么**。
   *
   * ⚠️ **每行前面挂语言标签** —— 原文那行标**源语言**、译文那行标
   *    **导出语言** ⇒ 一眼分得清（这正是用户要的）。
   *
   * ⚠️ **增量渲染** —— 长音频几千句 ⇒ 每次轮询全量重建 DOM 会越来越慢
   *    ⇒ 按行号复用已有的行（跟 ``_renderSentences`` 同一个套路）。
   */
  _dubRenderSentences(sentences) {
    const box = document.getElementById("asr-dub-list");
    const wrap = document.getElementById("asr-dub-out");
    if (!box || !wrap) return;
    const list = sentences || [];
    // ⚠️ **「有没有内容」要记在状态里** —— 结果框的显隐（合并规则：
    //    「配音导出有内容 ⇒ 藏掉实时结果」）靠它。
    this._dub.hasRows = list.length > 0;
    if (!list.length) {
      wrap.classList.add("hidden");
      // ⚠️ **DOM 也要清** —— 只清 ``box._rows``（那个 Map）的话，上一个
      //    作业的行会**留在屏幕上** ⇒ 新作业还没出句子时，用户看到的是
      //    **上一次的旧结果**（实测踩到）。
      if (box._rows) box._rows.clear();
      while (box.firstChild) box.removeChild(box.firstChild);
      this._syncResultBoxes();
      return;
    }
    wrap.classList.remove("hidden");

    const srcLang = this._srcLangLabel(this._dub.sourceLang);
    const tgtLang = this._dubTgtLangLabel(this._dub.targetLang);
    const cap = document.getElementById("asr-dub-out-cap");
    if (cap) {
      const head = "配音导出结果 · 一句一句";
      const pair = (srcLang || tgtLang)
        ? "（源语言：" + (srcLang || "未知") +
          " → 导出语言：" + (tgtLang || "未知") + "）"
        : "";
      cap.textContent = head + pair;
    }

    if (!box._rows) box._rows = new Map();
    list.forEach((s, i) => {
      let row = box._rows.get(i);
      if (!row) {
        row = document.createElement("div");
        row.className = "asr-sent";
        const src = document.createElement("div");
        src.className = "asr-sent-src";
        const dst = document.createElement("div");
        dst.className = "asr-sent-dst";
        row.appendChild(src);
        row.appendChild(dst);
        box.appendChild(row);
        box._rows.set(i, row);
      }
      const src = row.querySelector(".asr-sent-src");
      const dst = row.querySelector(".asr-sent-dst");
      this._dubSetSentText(src, srcLang, s.orig || "");
      this._dubSetSentText(dst, tgtLang, s.trans || "");
    });
    // ⚠️ **新作业比旧的句子少** ⇒ 多出来的那些行要**删掉**
    //    （留着就是上一轮的旧结果）。
    for (const [i, row] of Array.from(box._rows.entries())) {
      if (i < list.length) continue;
      box._rows.delete(i);
      if (row.parentNode) row.parentNode.removeChild(row);
    }
    // ⚠️ **结果框的显隐跟着走** —— 「配音导出有内容 ⇒ 藏掉重复的实时结果」。
    this._syncResultBoxes();
  },

  /** 一行的内容 = 「语言标签 + 文字」。 */
  _dubSetSentText(el, lang, text) {
    if (!el) return;
    // ⚠️ **只用 textContent** —— 识别出来的文字是**外部内容** ⇒ 别拼
    //    innerHTML（万一原文里带尖括号就会被当成标签解析）。
    let tag = el.querySelector(".asr-lang-tag");
    if (lang) {
      if (!tag) {
        tag = document.createElement("span");
        tag.className = "asr-lang-tag";
        el.insertBefore(tag, el.firstChild);
      }
      if (tag.textContent !== lang) tag.textContent = lang;
      tag.classList.remove("hidden");
    } else if (tag) {
      tag.classList.add("hidden");
    }
    let body = el.querySelector(".asr-lang-body");
    if (!body) {
      body = document.createElement("span");
      body.className = "asr-lang-body";
      el.appendChild(body);
    }
    if (body.textContent !== text) body.textContent = text;
  },

  /** 重新列一次源文件同级目录里的产物。 */
  async _dubRefreshFiles() {
    // ⚠️⚠️ **优先用作业自己的源路径**（``/dub/status`` 里的
    // ``source_path``）—— ``this._path`` 会被「恢复会话」改写（ASR 会话
    // 报的可能是**临时音轨**路径，作业结束就没了）⇒ 拿它列产物会 400
    // （临时目录在资源根之外）。
    const path = this._dub.sourcePath || this._dubSourcePath();
    if (!path) return;
    let items = [];
    try {
      const r = await fetch("/api/qwen3-asr/dub/files?path=" +
                            encodeURIComponent(path), { cache: "no-store" });
      const d = await r.json();
      items = (d && d.items) || [];
    } catch (e) {
      items = [];
    }
    this._dub.files = items;
    const sel = document.getElementById("asr-dub-files");
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = "";
      if (!items.length) {
        const o = document.createElement("option");
        o.value = "";
        o.textContent = "（还没有生成的文件）";
        sel.appendChild(o);
      } else {
        items.forEach((it) => {
          const o = document.createElement("option");
          o.value = it.url;
          o.textContent = it.name + "（" +
            (it.kind === "video" ? "视频" : "音频") + "）";
          sel.appendChild(o);
        });
        // ⚠️ 保持用户当前的选择（刷新列表别把选择冲掉）。
        if (items.some((it) => it.url === prev)) sel.value = prev;
      }
    }
    this._dubSyncPlayer();
  },

  /** 下拉里选中的那个文件 ⇒ 播给用户听 / 给他看。 */
  _dubSyncPlayer() {
    const $ = (id) => document.getElementById(id);
    const sel = $("asr-dub-files");
    const audio = $("asr-dub-player");
    const video = $("asr-dub-video");
    if (!sel) return;
    const url = sel.value || "";
    const item = this._dub.files.find((it) => it.url === url);
    const isVideo = item && item.kind === "video";
    if (video) {
      video.classList.toggle("hidden", !isVideo);
      if (isVideo && video.getAttribute("src") !== url) {
        video.setAttribute("src", url);
        try { video.load(); } catch (e) { /* noop */ }
      } else if (!isVideo) {
        video.removeAttribute("src");
      }
    }
    if (audio) {
      audio.classList.toggle("hidden", !!isVideo);
      if (!isVideo && audio.getAttribute("src") !== url) {
        if (url) audio.setAttribute("src", url);
        else audio.removeAttribute("src");
        try { audio.load(); } catch (e) { /* noop */ }
      }
    }
  },
};

/**
 * 卡片 JS 由 framework 注入；暴露给向导（applyWizardSnapshot）。
 *
 * 向导给的是**设备端路径**（向导「上传…」落到设备端、或「浏览…」选出来的），
 * 所以这里把 source 写进路径框并**直接发起识别** —— 向导走完就该有结果，
 * 而不是「向导完成了但还得自己去页面点开始」。
 */
window.ImApp_qwen3_asr = window.ImApp_qwen3_asr || {};

/**
 * 向导 finish 前的钩子：**提前发起**翻译模型的加载。
 *
 *  框架（`wizard/state.js::_finish`）会问卡片要「额外要加载的模型」，
 *  拿到后把它们**并进** finish 步的等待 ⇒ 翻译模型与 ASR 模型
 *  **并行加载**、**同一块进度条**里两行。
 *
 *  ⚠️ 以前是串行：先等 ASR 模型加载完（进度条走完），再由
 *  `applyWizardSnapshot` 单独加载翻译模型（另一条进度条）—— 用户要
 *  等两轮。
 *
 *  ⚠️ 这里**只发起**、不 await —— await 的话框架的等待被推迟 30 s，
 *  变回串行。失败时 `notifyFailed` ⇒ 框架那一行标 error（软失败），
 *  识别照常启动。
 */
window.ImApp_qwen3_asr.preloadExtraModels = async function (opts) {
  const o = opts || {};
  const targets = [];
  const soft = [];
  const lang = String(o.trTarget || "").trim();
  const trOn = !!lang && lang !== "off";
  // ⚠️ **同传开着吗**（2026-11-25 新增）—— 向导第 5 步的开关。
  const interpOn = !!o.interp;
  // 没有源就别白加载模型（2.2 GB 的翻译 + 1.9 GB 的语音合成）。
  if (!String(o.source || "").trim()) return { targets: targets, soft: soft };

  // ---- 翻译模型 ----
  // ⚠️ **同传开着 ⇒ MT 一定要起来** —— 同传**自己**调 MT 翻译，不依赖
  //    「实时翻译」那个开关 ⇒ 即便翻译目标语言选了「不翻译」，同传也要
  //    MT（否则同传没译文可念）。
  if (trOn || interpOn) {
    // 进度条要按 model_id 找实例 ⇒ 先确保知道它（后端 status 里带）
    if (!Qwen3Asr._tr.modelId) await Qwen3Asr._trRefresh();
    const modelId = Qwen3Asr._tr.modelId;
    if (modelId) {
      // 发起加载（**不等**）—— 框架的进度条会并行追踪它
      Qwen3Asr._trEnablePromise = Qwen3Asr._trStartModel(
        trOn ? lang : Qwen3Asr._interpTargetLang());
      Qwen3Asr._trEnablePromise.catch((e) => {
        const msg = (e && (e.message || e)) || "启动失败";
        try {
          if (window.WizardProgress && window.WizardProgress.notifyFailed) {
            window.WizardProgress.notifyFailed(modelId, msg);
          }
        } catch (_) { /* 忽略 */ }
      });
      targets.push({
        // npu_index: null = 任意一张卡起来即可（落哪张卡由后端卡位策略定）
        model_id: modelId, npu_index: null,
      });
      // 软失败：翻译模型起不来只把这行标 error，**不**让整个向导失败
      soft.push(modelId + "@null");
    }
  }

  // ---- 语音合成模型 ----
  // ⚠️ **发声链必须独占一张卡**（卡位策略：TTS 与 ASR 同卡会把识别轮
  //    间隔顶到 > 1 s ⇒ 客户端上行队列溢出 ⇒ 丢块）⇒ npu_index 传
  //    **null**，让后端按策略挑一张**没被感知链占用**的卡。
  if (interpOn) {
    const ttsId = await Qwen3Asr._interpTtsModelId();
    if (ttsId) {
      targets.push({ model_id: ttsId, npu_index: null });
      // 软失败：TTS 起不来 ⇒ 那一行标 error ⇒ 同传降级为**仅字幕**，
      // 识别与翻译照常。
      soft.push(ttsId + "@null");
      // ⚠️⚠️ **必须真的把它拉起来** —— 进度条是「按 model_id **等实例
      //    出现**」的（框架轮询 ``GET /api/compute``）⇒ 只把 model_id
      //    报上去**不会有人去起它** ⇒ 进度条会一直等到超时。
      //    ⇒ 这里**发起**（不等）—— 卡位由后端卡位策略决定
      //    （发声链独占 ⇒ 落一张没被感知链占用的卡）。
      Qwen3Asr._interpEnsureTts().catch(() => {});
    }
  }

  return { targets: targets, soft: soft };
};

window.ImApp_qwen3_asr.applyWizardSnapshot = async function (opts) {
  Qwen3Asr.init();
  const o = opts || {};

  // 1) 来源 —— 设备端路径
  if (o.source) {
    const path = String(o.source).trim();
    if (path) {
      Qwen3Asr._path = path;
      Qwen3Asr._file = null;
      const el = document.getElementById("asr-source");
      if (el) el.value = path;
      const fileInput = document.getElementById("asr-file");
      if (fileInput) fileInput.value = "";
      const nameEl = document.getElementById("asr-file-name");
      if (nameEl) nameEl.textContent = "";
      // ⚠️ **必须按路径推断来源类型并切页签** —— 向导里**没有**「输入来源」
      //   页签（那是功能页的东西），用户在向导里选了个含音轨的视频，
      //   完成配置回到功能页时页签还停在默认的「音频文件」⇒ 视频播放器
      //   不出现、视频也不播（用户实测）。
      //   后端 ``api.py::_source_kind`` 同样按后缀判定 ⇒ 前后端口径一致。
      Qwen3Asr._applyKindFromPath(path);
    }
  }

  // 2) 显示模式
  if (o.mode) {
    const sel = document.getElementById("asr-mode");
    if (sel) sel.value = o.mode;
    Qwen3Asr._applyMode();
  }

  // 3) 流式参数 —— **不传**。
  //    分块长度 / token 上限 / 回退策略等 = C++ demo 的 ONLINE_* 常量，
  //    属于内部契约，不暴露给用户；不传即用 SDK 的 ONLINE_*_DEFAULT。

  if (!Qwen3Asr._hasSource()) {
    throw new Error("WizardSnapshotError:start_failed:没有音频来源，请在向导里填写或上传音频文件");
  }

  // 4) 翻译目标语言 —— 选了非「不翻译」⇒ 把翻译模型拉起来 + 开实时翻译。
  //    ⚠️ 翻译是**旁路**：模型起不来 / 加载超时都**不能**拖垮识别主链路
  //    ⇒ _trEnableForWizard 内部软失败，这里照样往下走启动识别。
  //    放在「有没有源」之后：源都没有就别白加载一个 2.2 GB 的模型了。
  const trTarget = String(o.trTarget || "").trim();
  const trOn = !!trTarget && trTarget !== "off";
  // ⚠️ **同传（2026-11-26 起只对麦克风开放）**——
  // 向导的流程是**文件类**的（它要一个设备端路径）⇒ 文件类来源**不做**
  // 同传 ⇒ 这里**不再**按 ``o.interp`` 起同传引擎。
  const interpOn = false;
  const interpLang = "";
  if (trOn) {
    await Qwen3Asr._trEnableForWizard(trTarget);
  }

  // 4.4) **配音导出的音色**（2026-11-26）—— 向导选的音色回填到卡片页
  //      的配音下拉 ⇒ 用户在向导里选一次，功能页里就是现成的。
  const dubVoice = String(o.dubVoice || "").trim();
  if (dubVoice) {
    Qwen3Asr._dub.wizardVoice = dubVoice;
    Qwen3Asr._dubSetVoice(dubVoice);
    const sel = document.getElementById("asr-dub-voice");
    if (sel) sel.value = dubVoice;
  }
  // 导出语言：向导选的那个语言**同时**作为「显示语言」和「导出语言」的初始值
  // （卡片页里两者可以再分别改 —— 2026-11-27 起两个下拉**不再同名**）。
  if (trOn) {
    Qwen3Asr._dub.targetLang = trTarget;
    const dsel = document.getElementById("asr-dub-target");
    if (dsel) dsel.value = trTarget;
  }

  // 4.5) 同传的参数 —— **先设好，等识别起来之后再起引擎**。
  if (interpOn) {
    const vw = String(o.interpVoice || "").trim();
    if (vw) {
      // ⚠️ 值是 ``kind:id`` —— 参考音色 / 内置音色是**两个不同的 SDK
      //    参数** ⇒ 必须拆开传，混用 ⇒ 音色不生效。
      const idx = vw.indexOf(":");
      const kind = idx >= 0 ? vw.slice(0, idx) : "ref";
      const vid = idx >= 0 ? vw.slice(idx + 1) : vw;
      if (kind === "builtin") {
        Qwen3Asr._interp.speaker = vid;
        Qwen3Asr._interp.refSpeaker = "";
      } else {
        Qwen3Asr._interp.refSpeaker = vid;
        Qwen3Asr._interp.speaker = "";
      }
    }
    if (interpLang) Qwen3Asr._interp.targetLang = interpLang;
  }

  // 5) 等能力就绪 → 真启动。
  //    **不能 btn.click()**：启动按钮的 disabled 由快照的 busy 决定，
  //    disabled 按钮的 click() 会被浏览器**静默丢弃** —— 向导显示"完成配置"、
  //    识别却没跑起来（与视频卡同一个坑，见 core/wizard-start.js）。
  if (window.WizardStart) {
    await window.WizardStart.run({
      label: "开始识别",
      probe: () => Qwen3Asr._probeCap(),
      start: async () => {
        const ok = await Qwen3Asr.start();
        if (!ok) throw new Error("start_failed:识别请求没被接受");
      },
      // 能力就绪 = 有 model_id（模型在 registry 里且会话建好了）
      isReady: (s) => !!(s && s.model_id),
      // 已经在跑 = busy，或者已经有轮次到达（极短音频可能跑得比探测还快）
      isStarted: (s) => !!(s && (s.busy || (s.round_count || 0) > 0)),
    });
  } else {
    const ok = await Qwen3Asr.start();
    if (!ok) throw new Error("WizardSnapshotError:start_failed:识别请求没被接受");
  }

  // 6) 同传 —— **识别起来之后**再起引擎。
  //    ⚠️ 顺序有讲究：识别先起来 ⇒ 引擎才有句子可翻；反过来（引擎先起）
  //    第一批句子会在 MT 还没就绪时被标成「未翻译」。
  //    ⚠️ **自动播放的用户手势到这里已经没了**（框架等了 ~30 s 的模型
  //    加载才走到这一步）⇒ 视频源的「跟播放」要等用户点一下播放 ⇒
  //    分支自带的**兜底放行**会接管（译文照样出声）。
  if (interpOn) {
    await Qwen3Asr._interpStart();
  }
};

// 向导里「翻译目标语言」的选项是**运行时**才有的（语言表在后端 provider
// 里）⇒ 卡片自己填。框架只负责发事件，不认识任何卡片字段。
window.addEventListener("wizard:step-rendered", (ev) => {
  const d = ev && ev.detail;
  Qwen3Asr.onWizardStepRendered(d && d.appId).catch(() => {});
});
// 向导里改了目标语言 → 记住。换步会重建 body，select 会被清空，没有这份
// 记忆用户在向导里选的语言会被无声地冲回默认值。
window.addEventListener("wizard:field-change", (ev) => {
  const d = ev && ev.detail;
  if (!d || d.appId !== "qwen3-asr") return;
  const vals = d.values || {};
  // ⚠️ 用 `!== undefined` 而不是真值判断 —— "off"（用户明确选了「不翻译」）
  //    也必须记下来，否则「不翻译」会被卡片当前状态盖掉。
  const v = vals["asr-tr-target"];
  if (v !== undefined) Qwen3Asr._tr.wizardTarget = v;

  // ---- 同传那一步的选择（2026-11-25）----
  // ⚠️ **换步会重建 body** ⇒ select 会被清空 ⇒ 不记下来的话，
  //    用户在向导里选的朗读语言 / 音色会被无声地冲回默认值。
  const iv = vals["asr-interp-enabled"];
  if (iv !== undefined) Qwen3Asr._interp.wizardOn = !!iv;
  const il = vals["asr-interp-lang"];
  if (il !== undefined) Qwen3Asr._interp.wizardLang = String(il || "");
  const iw = vals["asr-interp-voice"];
  if (iw !== undefined) Qwen3Asr._interp.wizardVoice = String(iw || "");
});

// 暴露给控制台 / 回归脚本（别的卡片也这么做，比如 VdFsModal）——
// 节奏字幕的时间轴在 ``Qwen3Asr._buildSubTimeline()`` 里，回归要直接读它。
window.Qwen3Asr = Qwen3Asr;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => Qwen3Asr.init());
} else {
  Qwen3Asr.init();
}
})();
