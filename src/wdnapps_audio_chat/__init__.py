"""``wdnapps_audio_chat`` —— wdnwebui 的应用插件包。

提供 3 张卡片（通过 ``wdnwebui.cards`` entry point 暴露给 wdnwebui 的
manifest 加载器）：

* ``tts`` —— 语音生成（Qwen3-TTS 文本转语音；带音色选择、试听样例、
  用户音色克隆）
* ``qwen3-asr`` —— 流式语音识别（Qwen3-ASR；文件 / 含音轨的视频转写、
  浏览器麦克风实时识别、板载 ALSA 采集）
* ``text-chat`` —— 文本对话（Qwen3.5-VL；多轮流式 + 工具调用）——
  **暂存**：它是文本→文本的卡，与音频无关，最终会拆到独立的
  ``wdnapps-chat`` 包。详见 ``docs/TEXT_CHAT_MIGRATION.md``。

与宿主 wdnwebui 的关系
----------------------

本包是 **wdnwebui 的插件**，不是独立应用：卡片后端 ``from app.core.*``
import 宿主的服务（``ComputeRegistry`` / ``app.core.audio.tts_bridge``
/ ``app.core.audio.capture`` / ``app.core.audio.endpoint`` /
``app.core.uploads`` 等），这是 wdnwebui 的设计原则 4/5 要求的调用
路径，插件不得绕过它们直接调 wdnai / wdnav。因此本 wheel 必须与
wdnwebui 在同一个解释器里、且 wdnwebui 的仓库根在 ``sys.path`` 上 ——
这正是 ``pip install -e`` 到 ``.venv/src/``（与 wdnai/wdnav/wdndds 平级）
这种装法的用途。

卡片目录布局是刻意的：每个卡仍是 ``<card_dir>/{manifest.json, api.py,
service.py, static/}``，与它在 wdnwebui 仓库里时逐字一致。宿主按
``/static/apps/<id>`` → ``<card_dir>/static`` 挂载，所以**所有前端
URL、后端路由、wizard 字段 id 一个都没改**，只有 manifest.json 里的
``backend.module`` 换成了本包的模块路径。
"""
from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
