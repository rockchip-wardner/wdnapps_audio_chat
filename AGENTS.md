# AGENTS.md

Quick reference for agent sessions in this repo.

Plugin package for `wdnwebui`. Installing it makes these cards appear on
the host UI: `qwen3-asr`（流式语音识别）、`tts`（语音生成）、`text-chat`
（文本对话 —— **暂存**，见下）。

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

