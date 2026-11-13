[English](README.md) | [简体中文](README_CN.md)

# wdnapps_audio_chat

wdnwebui 的应用插件包。装上之后，宿主页面上会出现这三张卡：

| 卡 id | 标题 | 说明 |
|---|---|---|
| `tts` | 语音生成 | Qwen3-TTS 文本转语音；带音色选择、离线试听样例、用户音色克隆（上传/录制参考音频现场提取 x-vector） |
| `qwen3-asr` | 流式语音识别 | Qwen3-ASR 流式识别；音频文件 / 含音轨的视频转写，浏览器麦克风或板载 ALSA 实时识别 |
| `text-chat` | 文本对话 | 与本地大模型（Qwen3.5-VL 4B / 9B）多轮对话：流式出字、工具调用、上下文窗口计数。**暂存于此，见下** |

> ### `text-chat` 是**暂存**，不是最终归属 ⚠️
>
> `text-chat` 是**文本 → 文本**的 LLM 对话卡：它既不产生也不消费音频，能力
> `swcap_text_chat` 也不在音频域 —— 而本包是**音频类**应用插件包。
>
> 它待在这里是**分步推进的中间态**，不是最终决定：
>
> 1. **已完成** —— 卡片从宿主仓（`app/apps/text_chat/`）搬进插件体系，宿主
>    只留 core + system 页，不再持有任何单张卡片的源码。
> 2. **计划中** —— 文本卡独立成 `wdnapps-chat` 包，与生态**按模态分包**的
>    惯例一致（`wdnapps_video_analyzer` / `wdnapps_audio_chat` /
>    `wdnapps_media_generator` / `wdnapps_hdmi_processor`）。
>
> **什么时候拆**：宿主 `app/apps/` 清空后；或出现第二张纯文本卡时；或本包的
> 语义边界开始造成实际困扰时。
>
> 拆包**只换包、不换契约** —— 卡 id `text-chat`、API 前缀
> `/api/text-chat`、静态路径 `/static/apps/text-chat/*`、向导回调
> `window.ImApp_text_chat.applyWizardSnapshot` 全部不变，用户侧零感知。
>
> 动这张卡之前先读 [`docs/TEXT_CHAT_MIGRATION.md`](docs/TEXT_CHAT_MIGRATION.md)
> —— 里面有完整的迁入步骤、拆包步骤和回归验证命令。

## 它是什么形态

**这是 wdnwebui 的插件，不是独立应用。** 卡片后端 `from app.core.*` import 宿主
的服务——`ComputeRegistry`（LLMSession 唯一来源）、`app.core.audio.tts_bridge`
（TTSRequest / session 代理）、`app.core.audio.capture` / `app.core.audio.endpoint`
（PCM 采集 / 重采样 / 停顿检测）、`app.core.uploads`（用户音色落盘路径）等。这些是
wdnwebui 设计原则 4/5 规定的调用路径，插件绕不过去也不该绕。所以本 wheel 必须和
wdnwebui 在**同一个解释器**里，且 wdnwebui 仓库根在 `sys.path` 上。

> ⚠️ 上面的 `app.core.audio.*` 是 `tts` / `qwen3-asr` 两张**音频卡**的依赖。
> 暂存的 `text_chat` 用的是另一套：`app.core.chat.*`（agent / runner / serial）、
> `app.core.compute.registry`、`app.core.tools.registry`、`app.core.thinking`
> —— **它一行 `app.core.audio.*` 都不用**。三张卡之间没有代码共享，
> 只是住在同一个包里。依赖方向永远是**卡片 → `app.core.*`**。

## 安装

宿主仓库根有一条 `update_rkwhl.sh`，它同时驱动四个 SDK 和三个卡片插件包。
**首选跑它**，别手动 pip：

```bash
cd <wdnwebui>
./update_rkwhl.sh                       # 按 git HEAD 决定哪些要重装
UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh   # 强制全部重装
```

脚本对本包做的事：HEAD 没变就完全不碰 `.venv`（连 pip 都不跑）；HEAD 变了
才 `purge_install_traces` + `pip install -e`。装完会跑一遍卡片注册校验
（`scripts/check_plugin_entrypoints.py`），逐张卡确认「entry point 能解析 →
manifest `"id"` 与 entry point 名逐字相同 → 卡片目录有 `static/`」。这挡的
是那个静默失败：pip 全程报成功，宿主启动却把这张卡 FATAL 跳过。

手工装也可以（首次 clone 时脚本走的就是这条路）：

```bash
# 落在 wdnwebui 的 venv 里，与 wdnai/wdnav/wdndds 平级
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_audio_chat.git
pip install -e wdnapps_audio_chat
```

宿主重启时扫描 `wdnwebui.cards` entry point 组，逐张卡片读 `manifest.json` 并
挂载路由 + `/static/apps/<id>` 静态目录。装完不必改宿主任何一行代码或配置。

> `pip install` 不会因为缺依赖而静默跳过——宿主侧的
> `_discover_from_entry_points()` 对每张卡单独 try/except，某张卡 import 失败
> 只 `LOG.error` 并跳过该卡，其余卡照常加载，web UI 不会因此起不来。
> 排错直接看宿主日志里的 `[<card_id>] plugin entry point ... not importable`。

## 加一张新卡

1. 建 `src/wdnapps_audio_chat/cards/<新卡>/`，目录结构照抄现有卡：
   `__init__.py`（`register_api` / `register_shutdown`）、`api.py`、`service.py`、
   `manifest.json`、`static/{template.html,app.js,app.css,wizard.json}`。
   可以再加自己的后端模块，**不用**登记进 `package-data`（只有
   `manifest.json` 和 `static/*` 算 data）：`qwen3-asr` 卡就在
   `api.py` / `service.py` 旁边带了 `board_mic.py`（板载 ALSA 采集子进程管理）。
2. 卡内 import 一律写全 `wdnapps_audio_chat.cards.<新卡>.xxx`，**不要**写
   `app.apps.*`。
3. 在 `pyproject.toml` 注册 **两处**，缺一不可：
   - `[project.entry-points."wdnwebui.cards"]` 加 `<卡id> = "…cards.<新卡>"`
     ——左边必须与 `manifest.json` 的 `"id"` **逐字**相同，否则宿主判 FATAL 跳过。
   - `[tool.setuptools.package-data]` 加该卡的 `["manifest.json", "static/*"]`
     ——不加的话静态资源不进 wheel，卡片能被发现但前端 404。
     **静态资源有子目录时要一并列出**（本卡的试听样例在 `static/previews/`，
     所以写了 `static/previews/*`）：`static/*` 只匹配直接子项，漏掉的子目录
     在 editable 装法下完全看不出来，只有真 wheel 才暴露。

## 重新生成试听样例

自带音色的试听样例（`cards/tts/static/previews/*.wav`）是**离线产物**：
一个音色合成一段要十几秒（NPU 上跑），放到页面上现算等不得。生成脚本
在 `scripts/gen_voice_previews.py`：

```bash
# 在本包仓库根跑；.venv 是宿主 wdnwebui 的 —— 脚本走宿主的 HTTP 接口合成
cd <wdnwebui>/.venv/src/wdnapps-audio-chat
<wdnwebui>/.venv/bin/python -u scripts/gen_voice_previews.py
```

它调宿主的 `POST /api/tts/start` 合成（`play_to=browser`，批量生成不往
板载喇叭出声），再把 SDK 写的 **32bit float** WAV 转成 **16bit PCM**
（体积减半、播放器兼容性更稳），落到卡片的 `static/previews/`。

改了音色表或试听文案后要重新跑一遍，否则样例与实际音色对不上 —— 试听
按钮放的就是这些文件。

> 另两个插件包（video-analyzer / hdmi-processor）没有 `scripts/`：它们的
> 静态资源全是手写的，没有「需要离线生成的产物」。本包有，所以工具跟
> 产物放一起 —— 谁改了音色表，就地就能重新生成。

## 与宿主的边界

插件**只能** `from app.core.*`，不能 import 宿主的 `app.apps.*`（那是别的卡，
可能压根没装）。反向也不允许：core 不得依赖插件——宿主 `app/core/audio/tts_bridge.py`
里的 `get_session()` 因此走**候选路径列表**（先试本包，再试树内旧路径），
插件没装时抛 ImportError，由调用方（内置工具 `tts_alert`）按「工具不可用」
软失败处理。

> **`qwen3-asr` 卡的一个例外：板载麦克风采集脚本仍在宿主。**
> `tools/asr_mic_agent.py`（独立进程跑 ALSA 采集）import 的是
> `app.core.audio.alsa` —— 它是**宿主**的工具，所以留在宿主的 `tools/` 下，
> 没跟着卡片搬进本包。卡片通过 `board_mic.py::_host_repo_root()`（从
> `app.core.__file__` 反推宿主仓库根）定位它，**不依赖插件装在哪儿** ——
> editable 装在 `.venv/src/` 下、还是真 wheel 装进 site-packages，都指向
> 同一个宿主 `tools/`。找不到就报「找不到采集脚本」，不会让整张卡 import 失败。

## License

Apache-2.0。见 [LICENSE](LICENSE)。
