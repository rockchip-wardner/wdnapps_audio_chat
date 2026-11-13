# text_chat 卡片迁入本包：暂存说明与执行步骤

> 状态：**暂存**（staging）。卡片已迁入 `wdnapps_audio_chat`，但**不是最终归属**。
> 评估依据：宿主仓 `wdnwebui` 的 `docs/TEXT_CHAT_PLUGIN_MIGRATION_REPORT.md`
> 执行清单：宿主仓 `docs/TEXT_CHAT_PLUGIN_MIGRATION_CHECKLIST.md`

---

## 1. 为什么 text_chat 暂存在这里

### 1.1 一句话

`text_chat` 是**纯文本 → 文本**的 LLM 对话卡，与音频 I/O 无关；按本生态
「**按模态分包**」的惯例，它最终应该独立成 `wdnapps-chat` 包。先落进
`wdnapps_audio_chat` 是**分步推进的中间态**，不是最终形态的承诺。

### 1.2 生态的分组逻辑是按模态

| 插件包 | 模态 | 卡片 |
|---|---|---|
| `wdnapps_video_analyzer` | 视频 ↔ 文本 | video-detection / video-retrieval / video-target-* / audio-analysis |
| `wdnapps_audio_chat` | 音频 ↔ 文本/语音 | tts（文本→语音）、qwen3-asr（音频→文本）、**text_chat（暂存）** |
| `wdnapps_media_generator` | 文本 → 图像 | media-generator / image-editor |
| `wdnapps_hdmi_processor` | 视频 → HID 动作 | usb-hid |
| **`wdnapps-chat`（计划中）** | **文本 → 文本** | **text_chat** |

### 1.3 能力词表也不属于音频域

宿主的能力命名轴是 `<输入模态>_<输出形态或功能>`：

| 能力 | 含义 | provider | 域 |
|---|---|---|---|
| `swcap_text_chat` | **文本→文本**（多轮 / 流式） | qwen3.5-vl-4b / 9b | **文本** |
| `swcap_text_to_speech` | 文本→语音 | qwen3-tts | 音频 |
| `swcap_audio_asr` | 音频→文本 | qwen3-asr | 音频 |

`swcap_text_chat` 的输入**和**输出都是文本 —— 它不碰任何音频路径。

### 1.4 迁移分两步走

| 步骤 | 动作 | 收益 |
|---|---|---|
| **第一步（本次）** | 把卡片从宿主仓 `app/apps/text_chat/` 搬进插件体系 | 消除宿主仓对**具体某张卡片**的源码耦合；卡片获得独立发版节奏；宿主仓只留 core + system 页 |
| **第二步（计划中）** | 把纯文本卡从音频包拆到独立的 `wdnapps-chat` 仓 | 包的语义边界与生态的「按模态分包」一致；将来任何纯文本卡（prompt 模板卡、翻译卡）有家可归 |

**第一步必须先做**：只要宿主仓里还有一张内置卡，「卡片全部插件化」就没有收口，
第二步也就没有讨论的意义。

### 1.5 为什么不一步到位新建 `wdnapps-chat`

不是不能，是**代价与收益的时序**问题：

- 新建插件包要开新的 git 仓（局域网 `wardner/wdnapps_chat.git` + GitHub 镜像 +
  Gitee 自动同步）、改宿主 `update_rkwhl.sh` 的 **4 处**硬编码包名
  （224 行 case、558-564 行 `run_pkg`、576 行完成信息、3-4 行头注释）、
  还要走一遍全新的首次装机链路验证。
- 而当时**只有一张卡**要搬 —— 独立一个包，仓库开销 > 收益。

⇒ 先借 `wdnapps_audio_chat` 落地，**把拆包当作计划内的一步**记下来，
等下面任一条件成立时执行第二步。

---

## 2. 什么时候执行第二步（拆到 `wdnapps-chat`）

满足**任一**即可：

1. 宿主仓 `app/apps/` 已清空、内置发现通道退役 —— 迁移目标达成；
2. 出现**第二张**纯文本卡（prompt 模板、文本翻译、摘要等）—— 独立包才划算；
3. `wdnapps_audio_chat` 的语义边界开始造成实际困扰（README 与内容不符、
   用户装音频插件却多出一张文本卡）。

---

## 3. 拆包步骤（第二步，执行时照做）

拆包**只换包、不换契约** —— 用户侧必须零感知。

### 3.1 必须保持不变的契约

| 契约 | 值 | 为什么不能变 |
|---|---|---|
| 卡 id | `text-chat`（连字符） | 宿主 `manifest.py` 校验 entry point 名与 manifest `"id"` **逐字相同**，不一致直接 FATAL 跳过该卡 |
| API 前缀 | `/api/text-chat` | 前端 `app.js` 里写死；改了就是破坏性变更 |
| 静态路径 | `/static/apps/text-chat/*` | 宿主按 `/static/apps/<id>` 挂载，`<id>` 就是卡 id |
| 向导全局名 | `window.ImApp_text_chat.applyWizardSnapshot` | 向导框架按 `ImApp_<app.id 去 hyphen>` 查找 |
| capability | `swcap_text_chat` | provider 声明在**宿主** `tools/worker_manifests/`，不在本包 |
| 宿主卡位占位符 | `{% card_slot "text-chat" %}` | **留在宿主** `app/static/index.html.template` —— 5 张现有插件卡也都在那儿 |

### 3.2 执行步骤

- [ ] 新建局域网仓库 `wardner/wdnapps_chat.git`（**权威源**）
- [ ] 新包骨架照抄本包：`pyproject.toml` + `src/wdnapps_chat/cards/text_chat/`
- [ ] `cards/text_chat/` 从本包**整目录拷过去**（含 `static/app.css`）
- [ ] `manifest.json` 的 `"backend.module"` → `"wdnapps_chat.cards.text_chat"`
- [ ] `pyproject.toml`：
      ```toml
      [project.entry-points."wdnwebui.cards"]
      "text-chat" = "wdnapps_chat.cards.text_chat"

      [tool.setuptools.package-data]
      "wdnapps_chat.cards.text_chat" = ["manifest.json", "static/*"]
      ```
- [ ] 宿主 `update_rkwhl.sh` 改 **4 处**（224 行 case、558-564 行 `run_pkg`、
      576 行完成信息、3-4 行头注释），加一行
      `run_pkg wdnapps_chat "$WARDNER_PKG_BASE/wdnapps_chat.git#egg=wdnapps_chat"`
- [ ] 本包（`wdnapps_audio_chat`）删掉 `cards/text_chat/`、entry point、package-data，
      并把 `pyproject.toml` description / `README*.md` / `__init__.py` docstring
      **改回 2 张卡**
- [ ] GitHub 镜像 + Gitee 自动同步（Gitee **不做任何 git 操作**）

### 3.3 拆包后的验证

宿主仓 `scripts/` 下的回归脚本**全部按卡 id 引用**，拆包后应当**一条都不用改**
—— 这正是当初用 `scripts/card_dirs.py` 做双通道解析的目的：

```bash
python -m scripts.verify_card_slots          # 卡位占位符
.venv/bin/python scripts/check_plugin_entrypoints.py   # 两个包都查
.venv/bin/python scripts/card_dirs.py        # text-chat 的 ns 应变成 wdnapps_chat.cards
python -m scripts.verify_apps_isolation      # 跨卡 import
python scripts/verify_plugin_cards.py        # 浏览器端
```

---

## 3.5 执行状态（2026-11-13）

**第一步已完成。** 卡片已迁入本包并在本板真机跑通：

| 项 | 结果 |
|---|---|
| 卡片装载 | `loaded 12 plugin card(s) from wdnwebui.cards: … text-chat …`；`apps.text_chat: API mounted at /api/text-chat` |
| 静态资源 | `/static/apps/text-chat/{app.js,app.css,template.html,wizard.json}` 全 200；`app.css` **4328 bytes**（迁移前宿主里那个 `app.css` 是 1 行空文件） |
| 真实推理 | answer=`你好，我是 Qwen3.5…`，`tool_call=TtsAlert` 成功解析成真实 `@Callable` 实例 |
| 流式 | 256 个 delta；stop → `state=stopped`，被停轮**不写历史** |
| 回归 | L1 5/5 + L2 全通 + L3（`verify_plugin_cards` OK、`smoke_wizard_all` 8/8、专项脚本 19/19）+ L4（`verify_stage3_smoke --group chat` 5/5） |

宿主侧同步改动：删掉 `app/apps/text_chat/`、把 145 行卡片 CSS 从
`framework.css` 搬进本卡的 `app.css`、删掉 `system.js` 里引用不存在全局
`TextChat` 的死代码、CI 的 `py_compile` 列表与两处过时注释同步。

> ⚠️ **两个执行时才发现的坑**（写在这里给下一个来拆包的人）：
>
> 1. 插件卡内部有**两处旧命名空间自引用**（`api.py` 的
>    `from app.apps.text_chat.service`、`service.py` 的
>    `from app.apps.text_chat.thinking_9b`）。拷目录时**必须一起改**，
>    否则卡片 import 时直接 ImportError。
> 2. 宿主侧 `scripts/verify_tool_channel_filter.py` 曾**硬编码**读
>    `app/apps/text_chat/service.py` —— 卡片一挪就 FileNotFoundError。
>    已改成走 `scripts/card_dirs.py`（双通道解析）。**拆包时同理**：
>    脚本不要硬编码卡片路径。

---

## 4. 插件侧实施步骤（第一步，本次执行）

### 4.1 拷目录

```bash
cd /userdata/wdnwebui
cp -r app/apps/text_chat \
      .venv/src/wdnapps-audio-chat/src/wdnapps_audio_chat/cards/text_chat
```

### 4.2 改 `manifest.json`

```diff
   "backend": {
-    "module": "app.apps.text_chat",
+    "module": "wdnapps_audio_chat.cards.text_chat",
     "register_api": "register_api",
     "register_shutdown": "register_shutdown"
   },
```

其余字段**一字不改**（`id` / `uploads_dir` / `preview_video` / `demo_guard` /
`wizard_config` / `display_order` 都与来源无关）。

### 4.3 改 `__init__.py`

```diff
-        from app.apps.text_chat import service as _service_mod
+        from wdnapps_audio_chat.cards.text_chat import service as _service_mod
```

**顺带清掉 5 个残留子模块探测**（`app/apps/text_chat/__init__.py:50`）：

```python
for subname in ("bot", "llm", "video_cache", "video_manager", "service_wechat"):
```

那是 **im_bridge 的遗留** —— text_chat 根本没有 `bot.py` / `llm.py` /
`video_cache.py` / `video_manager.py` / `service_wechat.py`，这段循环永远全部
`except: continue`。迁过来时一并删掉。

### 4.4 补 `static/app.css`

当前宿主 `app/apps/text_chat/static/app.css` 是 **1 行空文件** —— 卡片的真实样式
寄生在宿主 `app/static/core/framework.css` 的 **4287–4434 行**。

⚠️ **这一步最容易漏**：漏了不会报错，卡片照常能开、能发消息，只是**全是裸样式**。

把宿主那段（`#page-text-chat .imb-card` / `.text-chat-*` / `@keyframes text-chat-blink` 等）
**整段**剪切进 `cards/text_chat/static/app.css`。注意那段里**不全是 `#page-` 前缀**
—— `.text-chat-*` 是无作用域的全局类名，必须一起搬。

依赖的共享类（`.imb-card` / `.imb-conv` / `.imb-composer` / `.imb-msg--in/out` /
`var(--mono)`）**留在宿主** —— 本包的 `tts/static/app.css`（374 行）、
`qwen3_asr/static/app.css`（536 行）都已经在用这些共享类。

### 4.5 改 `pyproject.toml`

```diff
 [project.entry-points."wdnwebui.cards"]
 "tts" = "wdnapps_audio_chat.cards.tts"
 "qwen3-asr" = "wdnapps_audio_chat.cards.qwen3_asr"
+"text-chat" = "wdnapps_audio_chat.cards.text_chat"
```

```diff
 [tool.setuptools.package-data]
 "wdnapps_audio_chat.cards.tts" = ["manifest.json", "static/*", "static/previews/*"]
 "wdnapps_audio_chat.cards.qwen3_asr" = ["manifest.json", "static/*"]
+"wdnapps_audio_chat.cards.text_chat" = ["manifest.json", "static/*"]
```

> ⚠️ **`package-data` 漏了是最阴的坑**（本包 README 原话）：卡片仍会被发现、
> 后端路由正常，但前端 404 —— 「卡片在、页面空白」。`static/*` 只匹配 static 下的
> **直接子项**；text_chat 的 `static/` 里没有子目录，`static/*` 够用。

同时把 description 从「音频类应用插件：语音生成 + 流式语音识别」改成
包含文本对话的措辞，并标注 text_chat 为暂存。

### 4.6 版本与自述

- `src/wdnapps_audio_chat/__init__.py` —— `__version__ = "0.1.1"` → `"0.2.0"`；
  docstring 的「提供 2 张卡片」→ 3 张（并标注 text_chat 暂存）
- `README.md` / `README_CN.md` —— 卡片表加 `text-chat` 行 + 暂存脚注
- `AGENTS.md` —— repo map 补 `docs/`；加「text_chat 暂存」小节

---

## 5. 安装与装载

```bash
cd /userdata/wdnwebui
./update_rkwhl.sh
# 或强制全量：UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh

.venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_audio_chat
# 期望 3 行 OK（tts / qwen3-asr / text-chat），无 BAD:

sudo systemctl restart wdnwebui
journalctl -u wdnwebui -n 80 | grep -i "plugin card\|text-chat\|FATAL"
# 期望：loaded 3 plugin card(s) from wdnwebui.cards: ..., text-chat
```

⚠️ **重启会连坐杀掉 `compute_worker`**（systemd `KillMode=control-group`）——
重启后确认 `compute.autostart` 有值，否则板子没有任何算力，
需手工 `POST /api/compute/instances` 把模型拉回来。

---

## 6. 回归验证

完整清单见宿主仓 `docs/TEXT_CHAT_PLUGIN_MIGRATION_CHECKLIST.md`（第 2 节，
L1–L4 四层）。本包相关的三条：

```bash
# L1 —— entry point 注册（本包）
.venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_audio_chat

# L2 —— 静态资源真的挂上了（CSS 漏搬的探针）
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' \
     http://127.0.0.1:8000/static/apps/text-chat/app.css
# 期望 200 且 size_download > 0（当前卡片的 app.css 是 1 行空文件 ⇒ 必须 > 0）

# L3 —— 浏览器端（pageerror 判据价值最高）
python scripts/verify_plugin_cards.py

# L3 —— 卡片专项（本包迁入后新增）
.venv/bin/python scripts/verify_text_chat_plugin_card.py

# L3 —— 模型解析：首选没装 ⇒ 回退到本板装着的那个（33 条断言）
.venv/bin/python scripts/verify_wizard_model_fallback.py
```

**L4（真机 NPU）**：`python -m scripts.verify_stage3_smoke` 跑
`swcap_text_chat` 的纯文本轮与工具轮；再手测「停止」按钮能真中断并释放 NPU。

> ⚠️ `verify_stage3_smoke.py` **必须 `sudo` 跑**（worker 日志写在 root 建的
> `/tmp/wdnwebui-worker/`），且**跑前要腾空 NPU** —— 脚本自带 registry 会自己
> spawn worker，跟服务侧同模型抢卡；跑完记得把服务侧的实例拉回来。

## 7. 本包的边界（不要越界）

text_chat 迁进来之后，本包**同时**装了两类东西：

| | 卡片 | 依赖的宿主服务 |
|---|---|---|
| 音频 I/O | `tts` | `app.core.audio.tts_bridge` / `capture` / `endpoint` |
| 音频 I/O | `qwen3_asr` | `app.core.audio.*` |
| **纯文本 LLM（暂存）** | **`text_chat`** | **`app.core.chat.*` / `app.core.compute.registry` / `app.core.tools.registry` / `app.core.thinking`** |

⚠️ **text_chat 不使用任何 `app.core.audio.*`** —— 它和本包原有的两张卡**没有
代码共享**，只有「都住在同一个包里」这一层关系。

⇒ **不要**因为「它在本包里」就给 text_chat 引入音频依赖，反之亦然。
依赖方向永远是**卡片 → `app.core.*`**，core 不依赖插件。

另注：`text_chat` 的 provider（`swcap_text_chat`）声明在**宿主**
`tools/worker_manifests/qwen3.5-vl-{4b,9b}.json`，**不在本包**。
本包不负责模型安装，宿主算力页的「本地算力」栏才是它的来源。

---

## 8. 提交规范提醒

本仓发布到公开的 GitHub 镜像（国际可见），**commit message 必须用英文**
（Conventional Commits：`type(scope): subject`）。

- 例：`feat(chat): add the text-chat card`
- 例：`docs(chat): note that text-chat is staged here pending wdnapps-chat`

推送顺序**不可颠倒**：先 `git push origin master`（局域网主仓库，唯一权威源），
再 `git push github master`（GitHub 发布镜像，只出不进）。
**Gitee 一概不碰** —— 它是上游自动同步的公开只读镜像。
