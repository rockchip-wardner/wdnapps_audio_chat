# AGENTS.md

Quick reference for agent sessions in this repo.

Plugin package for `wdnwebui`. Installing it makes these cards appear on
the host UI: `qwen3-asr`（流式语音识别）、`tts`（语音生成）、`text-chat`
（文本对话 —— **暂存**，见下）、`web-translate`（网页翻译）。

## 算力页的「运行在」：**自动（按卡位策略）**

算力页在**宿主** `app/system/static/system.js` 里（`ComputeConfigPage` ——
⚠️ 本仓 `cards/tts/static/app.js` 里那份是**早期遗留的死代码**，没有
人调用，别照着改）。

宿主算力页每个模型卡片的「运行在」多了一个 **「自动（按卡位策略）」**
选项：勾上它 ⇒ 启用请求**不带** `npu_index` ⇒ 后端按宿主的卡位策略挑卡
（宿主 `app/core/compute/card_roles.py`，插件**不复制**一份）。

| 模型 | 策略 |
|---|---|
| `hy-mt-1-5`（翻译） | **跟随 ASR 所在的卡**（感知链同卡，实测争用 ≈ 0） |
| `qwen3-tts`（语音合成） | **独占剩下那张卡**（流式生成边生成边播，不能与识别共卡） |
| 其余 | 挑一张放得下的卡 |

⚠️ **用户显式勾的卡号永远优先于策略** —— 策略只在**一个卡都没勾**（= 自动）
时生效。

## `qwen3-asr` 卡向导的「翻译目标语言」步

向导第 3 步 = **实时翻译（可选）**，一个「翻译目标语言」下拉。选了非
「不翻译」⇒ 走完向导时**把翻译模型也拉起来**（`hy-mt-1-5`，跟随 ASR 的卡）。

| 事项 | 口径 |
|---|---|
| 选项来源 | **运行时**才有（语言表在后端 provider 里）⇒ `wizard.json` 只能放一个「不翻译」占位，卡片监听框架的 `wizard:step-rendered` 事件自己填（和 TTS 卡的音色下拉同一套路） |
| 默认值 | **「不翻译」**。⚠️ 不能拿卡片页的 `targetLang`（默认 `"en"`）当默认 —— 用户没碰过下拉也会被当成「选了英语」⇒ 白白加载一个 2.2 GB 的模型 |
| 「不翻译」是显式选择 | 用哨兵值 `"off"`，**不能**被卡片当前状态盖掉（卡片翻译开着时也不能把用户的「不翻译」改回去） |
| 加载 | 走 `POST /api/qwen3-asr/translate/enable`（后端算卡位 + 幂等 + 写 `compute.autostart`），**前端不自己拼** `POST /api/compute/instances` |
| **加载进度** | 与 ASR 模型**同屏两行、并行加载**（宿主 `window.WizardProgress` + 卡片钩子 `preloadExtraModels`）。⚠️ 以前是**串行**：先等 ASR 加载完，再单独加载翻译模型 ⇒ 用户等两轮。现在卡片在向导 finish 之前就**发起**翻译模型的加载，框架把它并进同一个等待 ⇒ 一屏两行进度条一起走 |
| 钩子只**发起**不 await | `preloadExtraModels` 里 await 慢端点（~30 s）的话，框架的等待被推迟 ⇒ 又变回串行。⚠️ 失败时调 `WizardProgress.notifyFailed(modelId, reason)` ⇒ 那一行立刻标 error（软失败），不用干等 15 s 宽限 |
| 开关时机 | **模型就绪之后**才开实时翻译开关。顺序反了的话，模型没起来的那些句子会被标成「翻译模型未启用」（`status=unavailable`），白噪音 |
| **失败降级** | 翻译是**旁路**，识别才是主链路 ⇒ 模型起不来 / 超时**只记警告，识别照常启动** |

⚠️ **和 `web-translate` 卡相反**：`web-translate` 的向导「直接启用」翻译
服务，失败抛 `start_failed:` 让向导报错（那张卡的全部意义就是翻译，失败
= 不可用）；`qwen3-asr` 的翻译是**附加功能**，失败不能拖垮识别。

回归（宿主仓）：`.venv/bin/python scripts/verify_asr_wizard_translate.py`
—— 断言向导有这一步、默认「不翻译」、选了语言真的调 `translate/enable`、
**翻译模型加载时进度条与 ASR 模型同屏两行**（两个用例都先
`POST /translate/disable` 把模型停掉，否则 enable 直接复用 ⇒ 没有
loading 进度可看）、以及**翻译模型启用失败时识别照常启动**（失败那一行
会显示「启动失败：<原因>」）。

## `qwen3-asr` 卡的实时翻译：**文件 / 视频路径也有一份**

实时翻译（流式同传）最早只挂在**实时路径**（麦克风）的 `endpoint` 帧上 ——
那是端点检测给出的**句级信号**。文件 / 视频路径**没有** `endpoint`
（只有 `round` 帧的 `committed` / `unfixed`，收尾只有一个 `done`）⇒
改成**自己断句**。

| 事项 | 口径 |
|---|---|
| 断句器 | `service.py` 的 `_SentenceSplitter`（纯函数，好测） |
| 主规则 | **句末标点**。中文 `。！？` 直接算；英文 `.` 走**带护栏**的判定（后面必须是空白 + 下一个词首字母大写 + 这一句 ≥ 12 字符 + 点前面不是 `Mr.`/`Dr.` 这类缩写）⇒ 小数与缩写不会误切 |
| 兜底 ① | **epoch 切换**（decoder 重置）⇒ 显式 flush 上一段的尾巴。⚠️ **必须显式**：换段后 `committed` 从头开始，上一段末尾那半句再也不会出现在任何一轮里 ⇒ 不 flush 就永远不译 |
| 兜底 ② | 攒够 60 字符还没标点 ⇒ 强制切（一段没标点的长音频也能边播边出译文） |
| 收尾 | 识别结束 flush 尾巴（最后一句通常没标点） |
| **句序号** | 文件路径 = `请求号 × 100000 + 句序号`（`_TR_FILE_INDEX_BASE`）；实时路径 = 1、2、3…。⚠️ **必须分区间** —— 前端是**按序号覆盖**同一条译文的（`app.js` 的 `translation` 帧处理）⇒ 撞了就互相顶掉 |
| **主循环** | 译文要广播给 WS 订阅者，而订阅队列属于**主循环** ⇒ API 层（async 端点）把 `asyncio.get_running_loop()` 传给 `transcribe_stream(loop=...)`。⚠️ 文件识别跑在**另一个线程的另一个循环**里（`asyncio.run`），那个循环识别一结束就关掉 ⇒ 不能拿它广播 |
| **翻译面板不在 live 块里** | live 块由 feature flag `asr.live.enabled` 门控 ⇒ 翻译控件放里面的话，文件 / 视频来源**根本看不到**。现在它是「输入来源」section 之后的独立块 ⇒ 三种来源都可见 |
| **「清空」** | 翻译服务有**代数**（`TranslationService._gen`）：`clear()` 时 +1 ⇒ **正在推理**的那一句回来时作废（不计数、不广播）⇒ 用户刚点完「清空」，页面不会又冒出一行 |

回归（宿主仓）：

* `.venv/bin/python scripts/verify_asr_translation.py` —— 单元级：断句规则
  （中文标点 / 英文句点 / 小数 / 缩写 / 长度兜底 / epoch 切换 / 收尾）、
  `round` 帧 ⇒ 切句 ⇒ 译文帧的**真实接线**、句序号区间、「清空」丢在飞的译文。
* `.venv/bin/python scripts/verify_asr_file_translation_e2e.py` —— 真机
  API 级：音频文件 ⇒ `round` 帧 ⇒ 译文帧（`stream_id` 是 `file-<请求号>`、
  序号落在文件区间、至少一句 `status=done`、每句都能在识别文本里找到）。
* `.venv/bin/python scripts/verify_asr_file_translate_ui.py` —— 真机浏览器级：
  翻译面板**不在** live 块里且**可见**、语言下拉被填满、走 UI 流程点
  「开始识别」后译文区真的出行、「清空」后译文区空了且**空态提示回得来**。

## `web-translate` 卡（网页翻译）

界面翻译的**服务开关**：启用 = 起本板 NPU 上的翻译模型（HY-MT1.5，默认
**4 个实例 = 2 张 1828 协卡 × 每卡 2 份**）**并**把 `npu` 这一路 provider
接进板端的翻译链；禁用 = 先摘链再停模型。之后网页右上角「界面语言」切换
就走本板 NPU。

| 位置 | 内容 |
|---|---|
| `src/wdnapps_audio_chat/cards/web_translate/service.py` | 启停 + 状态（`WebTranslateManager`） |
| `src/wdnapps_audio_chat/cards/web_translate/api.py` | `GET /api/web-translate/status`、`POST /enable`、`POST /disable`、`POST /translate` |
| `src/wdnapps_audio_chat/cards/web_translate/static/` | `template.html`（功能页）+ `app.js` + `app.css` + `wizard.json` |

### 4 个实例（2 卡 × 每卡 2 份）

真源 = `static/wizard.json` 的 `swcap_to_model.swcap_translate.instance_slots`
（4 条）—— **向导和卡片读的是同一份**，两处不一致会出现「向导开了 4 个、
卡片只起 1 个」。

实测（RK1828 双卡，24 条界面文案）：

| 布局 | provider 层 | 端到端¹ |
|---|---|---|
| 1 实例 / 1 卡 | 3.30s (1.00×) | 5.52s |
| 4 实例 / 2 卡×2 | 1.45s (2.31×) | 2.78s (1.99×) |

¹ 端到端 = `POST /api/translate`（10 个有效轮次的中位；同一批文案、同一串
目标语言、每次 disable→enable 清缓存后重跑）。

⚠️ **加速的单位是「卡」不是「实例」** —— 同卡加一份只 +21%，加到第二张卡
直接 +85%。

⚠️ **批间 1.2s 间隔只对「走外网的 provider」生效** —— 那是给免费外网接口
防限流用的，本板 NPU 没有外网连接，不 sleep。之前 NPU 路径也照睡，白等
1.2s（占 4 实例端到端时间的 30%），把加速比从 1.99× 压到 1.69×。判据是
provider 自己的属性 `TranslationProvider.local`，见宿主仓
`app/core/translate/`。

⚠️ **两条容易踩的**：

1. **卡号 0 是合法值** —— `i.get("npu_index") or -1` 在卡 0 时会返回 -1
   （falsy-zero），于是卡 0 上的实例永远匹配不上（表现为「明明 running 却报
   启动失败」，然后干等 120s 超时）。要用
   `if not isinstance(npu, int) or npu != int(card): continue`。
2. **禁用要停全部份数**，不只是第一个 —— 同卡多份时漏停会留下还在占显存的
   僵尸 worker。

⚠️ **三条**：

1. **端点必须是同步 `def`** —— 启用要等模型加载完（~20~30s）、禁用要等
   unload + shutdown + SIGTERM 宽限（~3~5s）。写成 `async def` 的话这
   30 秒里整个 Web UI 都没响应（FastAPI 只把 `async def` 跑在事件循环里）。
2. **禁用要走 `reg.stop_instance_local(model_id, npu_index, replica)`** —— 那是
   算力页「禁用」的同一套语义（unload → RPC shutdown → SIGTERM → 3s → SIGKILL）。
   只 unload 会把 NPU 显存还回去但把 worker 进程留着。
   ⚠️ `reg.snapshot()` 给的是**字典**，不是 `ModelInstance` 对象 ——
   拿快照字典去调 `unload_one_instance()` 会报
   `'dict' object has no attribute '_probe_gen'`（而且被 except 吞掉，
   表现是「禁用成功但模型还在跑」）。
3. **默认卡位 = `npu_index=0`** —— 本机 EP 协卡按 PCI BDF 排序编号，
   0 号就是第一张 1828。翻译模型只要一张卡；图像生成（`qwenimage21`）要
   两张，同占 0 号卡会抢显存起不来 —— 卡片会提前把这个冲突说出来。

⚠️ **测吞吐时会被两个东西骗到**（写基准脚本时踩过）：

* **缓存** —— `TranslateService` 按 `(source, target, text)` 记账，命中缓存
  根本不调 provider（24 条全命中只要 0.01s）。绕缓存要**轮换目标语言**，
  **不能**给文案加后缀（`（1）` / `#1`）—— 那会让 HY-MT 把短文案复读回来，
  整批回退外网。
* **整批回退** —— 一条翻不出来整批都走外网。所以基准的文案集必须是
  「NPU 能整批翻通」的（宿主仓 `scripts/probe_npu_items.py` 逐条试出来）。

向导走完（`ImApp_web_translate.applyWizardSnapshot`）→ **直接启用**翻译
服务；失败抛 `start_failed:`，向导会显示错误并给「重新开启模型」的重试入口。

## 仓库地址与同步策略（2026-11-10）

| 角色 | 地址 | 允许的操作 |
|---|---|---|
| **局域网主仓库** | http://10.10.10.10:8080/wardner | **pull + push** —— 唯一权威源，所有更新的第一站 |
| **GitHub** | https://github.com/rockchip-wardner | **只允许 push** —— 发布镜像，不从它 pull |
| **Gitee 国内镜像** | https://gitee.com/rockchip-wardner | **不允许任何 git 操作** —— 公开只读镜像，内容由上游自动同步 |

远端命名：`origin` = `git@10.10.10.10:wardner/wdnapps_audio_chat.git`（局域网），
`github` = `git@github.com:rockchip-wardner/wdnapps_audio_chat.git`（只 push）。

**提交顺序不可颠倒：先 `git push origin master`（局域网主仓库），再
`git push github master`（GitHub）。Gitee 一概不碰。** `master` 的跟踪分支指向
`origin`，裸 `git pull` / `git push` 即为对主仓库操作。拉取最新代码只走局域网
主仓库 —— 从 GitHub 拉的是「已发布出去的历史」，会让本地与主仓库分叉。

🚦 **推服务器前必须先征得用户同意（2026-11-12）。**

本地 `git commit` 不用问；**`git push` 一律先问**，用户明确点头再推：

> 「要把 wdnapps_audio_chat 的这 N 个提交推到局域网主仓库（10.10.10.10）和 GitHub 吗？」

**不要顺手就推。** 理由：

* 推送是**对外**动作 —— GitHub 是公开镜像，推上去国际可见，撤不回来；
* 用户本地可能还有正在做的改动，先推会把半成品推成「最新」；
* 一次授权只覆盖当次问过的那个仓和那些提交，别顺手推广到别的仓。

顺序本身不变（先局域网、后 GitHub），只是**多了「先问」这一步**。

🚦 **「新卡片 / 新功能」在用户验收之前不推（2026-11-17 补充）。**

一次授权只覆盖**当次问过的那批仓、那批提交**。特别是**新卡片**：

> 用户在浏览器里看过、点过、确认能用之前 —— **本地 commit 就行，不要推**。

踩过的坑（这次真踩了）：用户上一轮说「提交10和github」，那轮授权覆盖的是
**当次已验证的改动**；下一轮做**新卡片**（content-review）时，我把那句授权
当成了持续授权，把新卡连同它的 bug 修复直接推到了局域网与 GitHub —— 用户还
没在浏览器里验收。推送是**对外**动作，撤不回来。

⇒ 判断标准：**这次改动用户验收过吗？** 没验收过 ⇒ 停在本地，问一句再推。

## 提交信息规范（2026-11-11）

**commit message 必须用英文。**

- 格式：Conventional Commits —— `type(scope): subject`，正文（若有）也用英文。
  例：`feat(asr): add a streaming ASR card`
- 原因：本仓发布到公开的 GitHub 镜像（发布镜像 = 国际可见），提交历史是给国际读者
  看的 —— 中文提交信息让公开历史没法读。
- **只约束提交信息**：代码注释、docstring、仓内文档（AGENTS.md / README）仍按各仓
  现有的写法（中文没问题）。README 是中英双语，AGENTS.md 允许中文。


## Repo map (one-line each)

- `src/wdnapps_audio_chat/cards/<card_id>/` — one directory per card: `manifest.json` + `api.py` / `service.py` + `static/`（`capture-worklet.js` 是 ASR 卡的采集 worklet）。
- The host discovers the cards through the `wdnwebui.cards` entry point group —— `manifest.json` 是宿主装载的唯一契约。
- `docs/TEXT_CHAT_MIGRATION.md` —— **text_chat 卡片的暂存说明 + 迁入步骤 + 未来拆包到 `wdnapps-chat` 的步骤**。
- `scripts/` — helper scripts.
- `src/wdnapps_audio_chat/__init__.py` — the package version.

## text_chat 是**暂存**，不是最终归属 ⚠️

`text-chat`（纯文本 → 文本的 LLM 对话卡）**不属于本包的语义边界**。本包是
「音频类应用插件包」：`tts`（文本→语音）、`qwen3-asr`（音频→文本）。text_chat
既不产生也不消费音频，能力词表 `swcap_text_chat` 也不在音频域。

它暂存在这里是**分步推进的中间态**：

1. **第一步（已做）**：把卡片从宿主仓 `app/apps/text_chat/` 搬进插件体系 ——
   消除宿主仓对具体某张卡片的源码耦合，宿主只留 core + system 页。
2. **第二步（计划中）**：把纯文本卡拆到独立的 `wdnapps-chat` 插件包 —— 与生态
   「**按模态分包**」的惯例一致（video-analyzer / audio-chat / media-generator /
   hdmi-processor 各自按模态成包）。

**第二步什么时候做**：宿主仓 `app/apps/` 清空后；或出现第二张纯文本卡时；
或本包的语义边界开始造成实际困扰时。

**拆包只换包、不换契约** —— 卡 id `text-chat`、API 前缀 `/api/text-chat`、
静态路径 `/static/apps/text-chat/*`、向导全局名 `window.ImApp_text_chat.*`
**全部不变**，用户侧零感知。宿主仓的 `{% card_slot "text-chat" %}` 占位符
**留在宿主**（现有 5 张插件卡也都在那儿）。

**做改动前先读** `docs/TEXT_CHAT_MIGRATION.md` —— 里面有完整的迁入步骤、
拆包步骤、回归验证命令，以及本包的边界提醒。

> ⚠️ **text_chat 不使用任何 `app.core.audio.*`** —— 它和本包原有的两张卡没有
> 代码共享，只有「住在同一个包里」这一层关系。别因为它在本包里就给音频卡
> 引入文本依赖，反之亦然。依赖方向永远是**卡片 → `app.core.*`**。

## text_chat 的模型解析：**候选列表** + 动态 provider

本卡**不认 model_id**，只认 capability `swcap_text_chat`（卡片按 capability
取 session）。所以「用哪个模型」只能表达成**顺序**，不能表达成「唯一那个」。

**单一真源 = `cards/text_chat/static/wizard.json`**：

```jsonc
"swcap_to_model": {
  "swcap_text_chat": {
    "model_id": "qwen3.5-vl-9b",                    // = 首选（兼容旧字段）
    "models": ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],    // 候选列表（有序，首选在前）
    "npu_index": [0, 1]                             // 首选的卡位
  }
}
```

运行期（`service.py`）的候选顺序 = **① 声明的偏好** → **② 本板已安装的
provider**（后端 `list_supported_models()` 动态列）→ **③ 正在跑的 provider**
（兜住远端 peer：peer 上的模型本板没装，第 ② 段里没有）。

⇒ **新增模型不必改代码**：装完模型、算力页点「刷新模型」，它就进候选池。
宿主向导读的是**同一份** `wizard.json`（`swcap_to_model.models`）⇒ 向导
「一键开启」启哪个模型、运行期先用哪个模型，永远一致。

⚠️ **`service.py` 里仍有一处模型 id 字面量**：`if model_id ==
"qwen3.5-vl-9b"` —— 那是 9B **特有的思考段输出格式**（专用解析器），不是
模型偏好。换模型要换解析器是另一件事，不要跟「偏好写死」混为一谈。

回归（宿主仓）：`.venv/bin/python scripts/verify_wizard_model_fallback.py`
（运行期解析 + 前端解析 + env 步提示与按钮，共 33 条断言）。

