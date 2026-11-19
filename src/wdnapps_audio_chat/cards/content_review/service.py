"""图文内容审查卡片的后端 —— 启停决策模型 + 跑一次结构化决策。

这个模型（Intern-Decision 家族：4B / 0.8B）**不是对话模型**：它不生成文本，
而是对任务里的每个「决策字段」输出 62 个候选符号的 logits，由 CPU 侧换算成
选项概率。所以本卡的「概率估计」= 把用户维护的那张**可增删列表**翻译成一组
``choice``（单选）字段 —— 列表里的每一项是它的一个选项。

⚠️ **两个型号同规格**（2026-11-19）
--------------------------------
0.8B 与 4B 用**同一个 qwen35 tokenizer**、**同样的控制 token id**、同样的
prompt 协议与视觉预处理 ⇒ 调用方代码**一个都不用改**，换模型只是换个
model id。

差别只有两处：

* **embedding 宽度**（1024 vs 2560）⇒ 概率校准温度不同。温度是**运行时**
  从 RKNN 的 LLM_CONFIG 读出来再查表的，代码里没写死。
* **显存占用**：0.8B 约 900 MB，4B 约 3200 MB ⇒ 0.8B 能和别的模型同卡共居。

为什么是**一个** choice 字段而不是 N 个 noul 字段（2026-11-18 改）
------------------------------------------------------------------
Intern-Decision 在 RKNN 上是 **prefill-only** 模型 —— 一次前向只在**一个**
位置输出那 62 维符号 logits。官方 C++ 参考实现（``rknn3_model_zoo/
examples/Intern_Decision/cpp/main.cpp``）对**每个字段**各跑一次 prefill，
输入是「在该字段 ``<decision>`` 标记处截断的前缀」。

⇒ **N 个字段 = N 次前向**；而且每个字段的前缀里已经含有**全部**字段的
定义（字段定义在 user 块、``<decision>`` 骨架在 assistant 块，后者总在
后面）⇒ 总 prefill token ∝ *N*²。真机实测（图文，RK1828 单卡）：

    ==========  ==========  ==========
    维度数       N 个 noul   1 个 choice
    ==========  ==========  ==========
    1             0.89 s      1.0 s
    7             8.0 s       1.0 s
    16            26.4 s      1.0 s
    ==========  ==========  ==========

（官方原始权重在 NVIDIA 卡上其实是**一次前向**出全部字段 —— 上游
``inference.py`` 用 ``logits_to_keep=positions`` 一次取所有 ``<decision>``
位置的 logits。RKNN 这条路做不到，是因为导出时 LM head 被裁成 62 个候选
符号，且 rknn3 的 LLM 输出回调只给 prefill **最后一个**位置的 logits。）

⚠️ **语义变了**：``choice`` 的选项概率是**归一化**的（合计为 1）——
表达的是「最像哪一类」，而**不是**每维独立的肯定概率。一条内容同时命中
多个维度时，只有模型选中的那一类拿得到高概率（实测 top1 与 top2 差距
通常很明显，如 广告 68% vs 垃圾信息 9%）。

三条链路（别搞混）
------------------
1. 浏览器 → 本卡后端：**HTTP**（``POST /api/content-review/review``）。
2. 本卡后端 → 决策模型 worker：**DDS/RPC**（``wdndds/rpc/<model>/worker-<pid>``），
   走 ``session.run_prompt(..., task=...)`` —— ``task`` 是 DDS wire 的
   ``options`` 字段，能带任意可 JSON 序列化的 override。
3. worker 内部 → NPU：rknn3。

⚠️ **为什么用 ``run_prompt`` 而不是 ``respond``**
   ``respond()`` 在没有 ``generating=`` 时**只返回文本**（``_parse_output``
   直接 ``return raw_text``），把输出附件丢掉了 —— 而决策模型的**完整概率 /
   logits 就在附件里**（``TextAttachment``）。``run_prompt()`` 返回
   ``(text, attachments)``，才拿得到概率。

⚠️ **决策字段名 = 列表项名称**
   模型的 prompt 是 ``{字段名}: {instructions}`` —— 字段名直接进 prompt，
   所以让它带上语义（"广告"）比 ``item_0`` 有用得多。字段名要唯一（JSON
   key 重复会被覆盖）⇒ 重名自动加后缀。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOG = logging.getLogger("apps.content_review")

#: 决策模型的能力名（``wdnai.capability.SoftwareCapability.DECISION``）。
CAPABILITY = "swcap_decision"

#: 本卡支持的决策模型（**有序**，第一个是默认）。
#:
#: 两个型号**能力完全相同** —— 同一个 qwen35 tokenizer（vocab 248320）、
#: 同样的控制 token id（``<decision>``=248077 / ``<think>``=248068 /
#: ``</think>``=248069）、同样的 prompt 协议与视觉预处理 ⇒ 调用方代码
#: **一个都不用改**。差别在参数量：0.8B 更快更省显存，4B 判别力更强。
#: 用户在界面上下拉切换。
#:
#: ⚠️ **这里只声明 model id**：标签与显存估算从算力页的 manifest 读
#: （``list_supported_models``）—— 同一个模型在两处各写一遍标签，迟早漂移。
MODEL_IDS: Tuple[str, ...] = (
    "intern-decision-4b",
    "intern-decision-0.8b",
)

#: 默认模型（没指定 / 指定的不认识时用它）。
DEFAULT_MODEL = MODEL_IDS[0]

#: 模型**没装**时（manifest 不存在 ⇒ 算力页上根本没有这张卡）的兜底标签。
_FALLBACK_LABELS = {
    "intern-decision-4b": "[JEV]图像文字决策模型4B",
    "intern-decision-0.8b": "[JEV]图像文字决策模型0.8B",
}

#: 一张 EP 协卡的 NPU 显存（MB）。实测 RK1828 协卡 = 5120 MB。
#:
#: 与 ``web_translate`` 卡用的是同一个常量 —— 两张卡跑在同一批协卡上。
CARD_MEMORY_MB = 5120

#: 本机实例的来源标记 —— 只有这两种真的吃**本板**的 NPU 显存。
#:
#: ⚠️ 远端 peer（``adopted``）的 ``npu_index`` 是**对端板自己的卡号**，
#: 它的显存吃在**对端板**上 ⇒ 算进来会把本板一张**空卡**判成占满。
LOCAL_SOURCES = ("spawned", "adopted_local")

#: 默认落在第一张 1828 协卡上。
DEFAULT_NPU_INDEX = 0

#: 决策任务最多 16 个字段 —— 这是**模型的硬上限**
#: （``wdnai.decision.MAX_FIELDS``，参考实现里也是 16）。超了要在界面上
#: 拦下来，不能等模型报错。
MAX_ITEMS = 16

#: 用户维护的那张列表被压成**这一个**决策字段（列表项 = 它的选项）。
#:
#: ⚠️ 它会直接进 prompt（``{字段名}: {instructions}``），所以要短、要像
#: 一个类别名，不能是「item_0」那种占位符。
CHOICE_FIELD = "内容类型"

#: 文字与图片的护栏（对外端点，防止把板子当通用 OCR 机使）。
MAX_TEXT_CHARS = 4000
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 字段名里不能出现的东西（会破坏 prompt 的行结构 / JSON key）。
_FIELD_BAD_CHARS = re.compile(r"[\r\n\t]+")


# --------------------------------------------------------------------------- #
# 推理性能表（tokens/s 的原料）
# --------------------------------------------------------------------------- #
#
# ⚠️ **数字只在 worker 进程里**：SDK 把它们记在 backend 上（决策模型是
# ``backend.perf``，通用 LLM 是 ``backend.sink.perf``），worker 显式带过
# wire，client 侧收在 ``session.last_perf``（见 ``wdndds/ai_session.py``）。
#
# ⇒ 这里**只是取出来 + 用 SDK 的公式算派生量**，前端一个数都不用自己算。
def _session_perf(session: Any) -> Dict[str, float]:
    """从 session 上读最近一次推理的性能表（读不到就空字典）。"""
    if session is None:
        return {}
    perf = getattr(session, "last_perf", None)
    if not isinstance(perf, dict) or not perf:
        # 退路：本地 session（没走 RPC）—— 直接读 SDK 的 sink
        sink = getattr(getattr(session, "_backend", None), "sink", None)
        perf = getattr(sink, "perf", None)
    if not isinstance(perf, dict):
        return {}
    return {
        str(k): float(v) for k, v in perf.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _perf_table(session: Any) -> Dict[str, float]:
    """性能表 + 派生量（tokens/s 等）。

    ⚠️ 派生量用 **SDK 自己的** :func:`wdnai.perf_table` 算 —— 和 SDK 打印的
    那张 perf 表**同一份公式**，不会两边漂。

    ⚠️ **决策模型是 prefill-only**：每个字段跑一次 ``max_new_tokens=1``
    的推理 ⇒ 解码 token 恒为 0 ⇒ 它的吞吐是**预填充速率**
    （提示词处理速度），``perf_table`` 的 ``tokens_per_second`` 正是按这个
    口径给的（有解码 token 用解码速率，没有就用预填充速率）。
    """
    raw = _session_perf(session)
    if not raw:
        return {}
    from wdnai import perf_table

    out = dict(raw)
    out.update(perf_table(out))
    return out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _run_coro(coro):
    """把 session 的 async 接口桥到当前线程。

    本卡的端点是**同步** ``def``（FastAPI 丢线程池跑），所以这里没有 loop 可
    await。万一哪天有人从 async 上下文调进来，硬 ``asyncio.run`` 会炸 ——
    那就丢到一次性线程里跑完再取结果（不 deadlock）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=_runner, name="content-review-coro")
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _decode_image(data_b64: str):
    """base64 → BGR ndarray（``None`` 表示解不开）。

    ⚠️ 必须是 **BGR**：``wdnai.attachment.ImageAttachment`` 与决策模型的
    视觉预处理都按 BGR 收；RGB 传进去会整体偏色，概率会飘。
    """
    try:
        import cv2  # noqa: WPS433
        import numpy as np  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"图片解码依赖缺失: {exc}") from exc

    raw = data_b64 or ""
    # 前端可能带 data URL 前缀
    if "," in raw and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"图片 base64 解码失败: {exc}") from exc
    if not blob:
        raise RuntimeError("图片内容为空")
    if len(blob) > MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"图片太大（{len(blob) // 1024} KB > {MAX_IMAGE_BYTES // 1024} KB）"
        )
    arr = np.frombuffer(blob, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or getattr(img, "size", 0) == 0:
        raise RuntimeError("图片解码失败（格式不支持或文件损坏）")
    return img


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class ContentReviewManager:
    """决策模型的启停 + 一次审查。"""

    def __init__(self) -> None:
        # 启停是**慢操作**（要等模型加载 ~40s），别让两个请求同时进来
        self._op_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self, model_id: Optional[str] = None) -> Dict[str, Any]:
        """当前状态快照（界面每次刷新拉一次）。

        ``model_id`` = 本次要看**哪个**模型的状态（界面上拉选中的那个）。
        传空 / 不认识 ⇒ 默认模型。**响应里同时带上全部候选模型的列表**
        （``models``），界面用它渲染下拉。
        """
        reg = self._registry()
        mid = self._resolve_model(model_id)
        mine = [
            i for i in self._instances(reg)
            if i.get("model_id") == mid
            and CAPABILITY in (i.get("capability_types") or [])
        ]
        inst = mine[0] if mine else None
        inst_status = (inst or {}).get("status") or "not_loaded"
        npu_index = None
        if inst is not None and isinstance(inst.get("npu_index"), int):
            npu_index = int(inst["npu_index"])
        enabled = inst_status == "running"
        presets = self._presets(reg)
        models = self._models(reg, presets)
        preset = presets.get(mid)
        installed = bool(preset) and bool(preset.get("exists"))
        label = self._label(preset, mid)
        # ⚠️ **preset 可能是 None** —— 本板没装这个模型时，算力页的清单里
        # 压根没有这一条（manifest 随模型文件夹分发）。上面 installed /
        # label 都已经防了（label 走 _FALLBACK_LABELS），这里也要防 ——
        # 否则「模型没装」会变成 500（状态接口挂掉 ⇒ 前端每 3 s 刷一条
        # console error，整页日志全红），而正确行为是返回「未安装」。
        init_s = int((preset or {}).get("estimated_init_seconds") or 40)

        detail = "决策模型已就绪 —— 可以提交图文做内容审查。"
        if not installed:
            detail = ("本机未安装决策模型 %s —— 装好后才能启用。" % mid)
        elif inst_status == "loading":
            detail = "决策模型正在加载（约 %d 秒），稍候即可提交。" % init_s
        elif inst_status == "error":
            detail = "决策模型启动失败：%s" % ((inst or {}).get("error")
                                              or "未知原因")
        elif not enabled:
            detail = "决策模型未启用 —— 启用后即可对图文做内容审查。"

        return {
            "enabled": enabled,
            "model_id": mid,
            "model_label": label,
            "capability": CAPABILITY,
            "installed": installed,
            "npu_index": npu_index,
            "default_npu_index": DEFAULT_NPU_INDEX,
            "instance_status": inst_status,
            "instance_error": (inst or {}).get("error") or "",
            "cards": self._cards(reg),
            "max_items": MAX_ITEMS,
            "max_text_chars": MAX_TEXT_CHARS,
            "detail": detail,
            # ⚠️ **警告，不是拦截**（2026-11-19 起决策模型不再独占整卡）。
            "warning": self._memory_warning(
                reg, npu_index if npu_index is not None else DEFAULT_NPU_INDEX,
                mid),
            # 全部候选模型（界面渲染下拉用）。
            "models": models,
            "card_memory_mb": CARD_MEMORY_MB,
        }

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def enable(self, npu_index: Optional[int] = None,
               model_id: Optional[str] = None) -> Dict[str, Any]:
        """启用：起决策模型的一个实例。

        ⚠️ **不再独占整卡**（2026-11-19）—— 以前会先把卡上的其它模型全停
        掉，现在只给余量告警、不拦停：0.8B 只要 ~900 MB，和翻译模型
        （2200 MB）同卡也放得下（900 + 2200 = 3100 < 5120），替用户做决定
        反而添乱。

        决策模型一份要几十秒加载，所以这是**慢端点** —— 界面上按钮转圈。
        """
        card = DEFAULT_NPU_INDEX if npu_index is None else int(npu_index)
        mid = self._resolve_model(model_id)
        with self._op_lock:
            reg = self._registry()
            if not self._installed(reg, mid):
                raise RuntimeError(
                    "本机没有安装决策模型 %s（目录 %s）"
                    % (mid, self._model_dir(mid))
                )
            # ⚠️ **两个决策模型不叠同一张卡**（详见 _pick_card）。
            card = self._pick_card(reg, card, mid)
            bad = reg.npu_card_error(card)
            if bad:
                raise RuntimeError(bad)

            inst = self._find_instance(reg, mid)
            if not (inst and inst.get("status") == "running"
                    and inst.get("npu_index") == card):
                with reg.model_op_lock(mid):
                    inst = reg.ensure_worker_for_model(
                        mid, allow_adopt=False, npu_index=card,
                        eager_load=False, replica=0,
                    )
                    reg.bind_instance(inst, card)
                if not self._wait_ready(reg, card, mid):
                    err = self._find_instance(reg, mid) or {}
                    raise RuntimeError(
                        "决策模型启动失败（卡 #%d）：%s"
                        % (card, err.get("error") or "未知原因")
                        + "（该模型要占不少显存，显存不够时 worker 会加载"
                          "失败 —— 可在算力页看这张卡上还跑着什么）"
                    )
            return self.status(mid)

    def disable(self, model_id: Optional[str] = None) -> Dict[str, Any]:
        """禁用：停掉决策模型实例。

        走 :meth:`stop_instance_local`（= 算力页「禁用」的同一套语义：
        unload → RPC shutdown → SIGTERM → SIGKILL），不是只 unload ——
        只 unload 会把显存还回去却把 worker 进程留着。

        ⚠️ **只停指定的那个模型** —— 另一个型号（0.8B / 4B）若也在跑，
        互不相干。
        """
        mid = self._resolve_model(model_id)
        with self._op_lock:
            reg = self._registry()
            for inst in self._find_all(reg, mid):
                npu = inst.get("npu_index")
                if not isinstance(npu, int) or npu < 0:
                    npu = None
                replica = int(inst.get("replica") or 0)
                try:
                    with reg.model_op_lock(mid):
                        reg.stop_instance_local(mid, npu, replica)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("content_review: 卸载实例失败: %s", exc)
            return self.status(mid)

    # ------------------------------------------------------------------ #
    # 审查
    # ------------------------------------------------------------------ #
    def review(self, text: str = "",
               items: Optional[Sequence[Dict[str, Any]]] = None,
               image_b64: Optional[str] = None,
               model_id: Optional[str] = None) -> Dict[str, Any]:
        """对一张图 + 一段文字，按用户维护的列表给出**各维度的占比**。

        返回::

            {
              "results": [
                {"name": "广告", "probability": 0.68, "symbol": "A",
                 "label": "广告", "probabilities": {"广告": 0.68},
                 "logits": [12.3], "selected": true}
              ],
              "selected": "广告", "selected_symbol": "A",
              "temperature": 1.9924, "elapsed_sec": 1.0,
              "tokens_per_second": 320.5, "perf": {...},
              "has_image": true, "raw_answer": "{...}"
            }

        ⚠️ **概率是归一化分布里的占比**（choice 字段，合计为 1）——
        表达「最像哪一类」，不是每维独立的肯定概率。``selected`` 标出
        模型实际选中的那一类。

        ⚠️ **性能字段全部在端算好**（``elapsed_sec`` /
        ``tokens_per_second`` / ``perf``）—— 前端只负责显示，一个数都不用
        自己算。
        """
        text = (text or "").strip()
        if len(text) > MAX_TEXT_CHARS:
            raise RuntimeError(
                "文字太长（%d 字 > 上限 %d 字）" % (len(text), MAX_TEXT_CHARS)
            )
        items = self._normalize_items(items)
        if not items:
            raise RuntimeError("请至少添加一个审查项")
        if not text and not image_b64:
            raise RuntimeError("请输入待审查的文字，或上传一张图片")

        reg = self._registry()
        mid = self._resolve_model(model_id)
        try:
            session = reg.get_session_for_model(CAPABILITY, mid)
        except RuntimeError as exc:
            raise RuntimeError(
                "决策模型未启用（%s 没有正在运行的实例）" % CAPABILITY
            ) from exc

        questions, field_map = self._build_questions(items)
        task = {"questions": questions}

        attachments = []
        image_info = None
        if image_b64:
            img = _decode_image(image_b64)
            # 延迟 import：wdnai 是重依赖，卡片启动时不该被它拖住
            from wdnai.attachment import ImageAttachment  # noqa: WPS433
            attachments.append(ImageAttachment(img))
            image_info = {"width": int(img.shape[1]), "height": int(img.shape[0])}

        t0 = time.perf_counter()
        try:
            answer_text, atts = _run_coro(session.run_prompt(
                prompt=text,
                attachment=attachments or None,
                task=task,
            ))
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"决策推理失败: {exc}") from exc
        elapsed = time.perf_counter() - t0
        # ⚠️ **性能表必须在推理返回的当下取** —— ``last_perf`` 是「最近一次
        # 推理」的单槽位，晚了就被别的请求覆盖了。
        perf = _perf_table(session)

        detail = self._parse_detail(atts)
        decisions = detail.get("decisions") or {}
        results, picked, picked_symbol = self._split_choice(decisions, field_map)

        return {
            "results": results,
            # 模型选中的那一类（前端在结果区顶部显示）。
            "selected": picked,
            "selected_symbol": picked_symbol,
            "temperature": detail.get("temperature"),
            "elapsed_sec": round(elapsed, 2),
            # ⚠️ **性能**：全部在**后端**算好再发给前端 —— 前端只负责
            # 显示，一个数都不自己算（用户明确要求：不要在 JS 端计时）。
            #
            # ``tokens_per_second`` 是 SDK 算好的**主吞吐**（决策模型是
            # prefill-only ⇒ 那是预填充速率）。
            "tokens_per_second": round(
                float(perf.get("tokens_per_second") or 0.0), 1),
            "perf": perf,
            "model_id": mid,
            "model_label": self._label(self._preset(reg, mid), mid),
            "has_image": bool(image_b64),
            "image": image_info,
            "raw_answer": answer_text,
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_items(items: Optional[Sequence[Dict[str, Any]]]):
        """清洗用户维护的列表：去空、去重、限长。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        for raw in items or []:
            if isinstance(raw, str):
                raw = {"name": raw}
            if not isinstance(raw, dict):
                continue
            name = _FIELD_BAD_CHARS.sub(" ", str(raw.get("name") or "")).strip()
            if not name:
                continue
            desc = _FIELD_BAD_CHARS.sub(
                " ", str(raw.get("description") or "")).strip()
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "description": desc})
        if len(out) > MAX_ITEMS:
            raise RuntimeError(
                "审查项最多 %d 个（模型上限），当前 %d 个"
                % (MAX_ITEMS, len(out))
            )
        return out

    @staticmethod
    def _build_questions(items: Sequence[Dict[str, Any]]):
        """把列表项翻译成**一个** ``choice``（单选）决策字段。

        每个项 = 一个**选项**（选项键 = 列表项名称，选项说明 = 它的判定口径）。
        整个列表只有**一个**决策字段 ⇒ **1 次前向**就能出全部维度的分布；
        N 个 ``noul`` 字段则要 N 次前向、总 prefill ∝ N²（见模块 docstring）。

        ⚠️ **选项键 = 列表项名称**：模型的 prompt 是
        ``{字段名}: {instructions}`` + ``{符号} = {选项键}: {选项说明}``，
        选项键直接进 prompt，所以用「广告」比 ``item_0`` 有用得多。
        选项键必须唯一（JSON key 重复会被静默覆盖）—— ``_normalize_items``
        已经按名称去重了。
        """
        criteria: Dict[str, str] = {}
        field_map: List[Tuple[Dict[str, Any], str]] = []
        for item in items:
            name = item["name"]
            desc = (item.get("description") or "").strip()
            criteria[name] = desc or ("属于「%s」这一类" % name)
            field_map.append((item, name))
        questions: Dict[str, Any] = {
            CHOICE_FIELD: {
                "type": "choice",
                "instructions": "判断待审查的图片与文字属于以下哪一类",
                "criteria": criteria,
            }
        }
        return questions, field_map

    @staticmethod
    def _split_choice(decisions: Dict[str, Any],
                      field_map: Sequence[Tuple[Dict[str, Any], str]]):
        """把**一个** choice 字段的归一化分布拆回每个维度。

        ⚠️ **整张列表 = 一个 choice 字段** ⇒ 模型返回的是**一份**归一化
        分布（``{选项键: 概率}``），这里按选项键拆回每个维度。

        返回 ``(results, picked, picked_symbol)``：
        * ``results`` —— 每个维度一行（前端直接渲染）；
        * ``picked`` —— 模型实际选中的那一类（choice 只能选一个）。

        单独抽出来是为了**不碰 NPU 就能测**（见
        ``scripts/verify_content_review_card.py``）。
        """
        choice = decisions.get(CHOICE_FIELD) or {}
        probs = choice.get("probabilities") or {}
        field_logits = choice.get("logits") or []
        picked = str(choice.get("label") or "")
        # 选项符号：按 criteria 顺序 A、B、C…（与模型 prompt 里写的一致）
        try:
            from wdnai.decision import DECISION_SYMBOLS
        except Exception:  # noqa: BLE001 — 协议层是纯 CPU，理论上不会失败
            DECISION_SYMBOLS = (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")

        results: List[Dict[str, Any]] = []
        for index, (item, key) in enumerate(field_map):
            try:
                p = float(probs.get(key, 0.0))
            except (TypeError, ValueError):
                p = 0.0
            try:
                logit = float(field_logits[index])
            except (IndexError, TypeError, ValueError):
                logit = None
            results.append({
                "name": item["name"],
                "description": item.get("description", ""),
                # ⚠️ choice 的概率是**归一化**的（合计为 1）—— 这里是
                # 「该维度在这份分布里的占比」，不是独立的肯定概率。
                "probability": p,
                "symbol": (DECISION_SYMBOLS[index]
                           if index < len(DECISION_SYMBOLS) else ""),
                "label": key,
                "probabilities": {key: p},
                "logits": [logit] if logit is not None else [],
                # 模型实际选中的是哪一个（choice 只能选一个）。
                "selected": bool(picked) and key == picked,
            })
        return results, picked, str(choice.get("symbol") or "")

    @staticmethod
    def _parse_detail(atts) -> Dict[str, Any]:
        """从输出附件里抠出完整明细（每个字段的概率 / logits）。

        决策 backend 的附件是 ``TextAttachment``，内容 = ``summary`` + 换行 +
        ``json.dumps(result.to_dict())``。这里优先按「第一行之后是 JSON」
        切，失败再退回「最后一个 ``{`` 起的 JSON」。
        """
        text = ""
        for att in atts or []:
            body = getattr(att, "text", None)
            if isinstance(body, str) and body.strip():
                text = body
                break
        if not text:
            return {}
        chunk = text.split("\n", 1)
        candidate = chunk[1] if len(chunk) > 1 else ""
        for text_try in (candidate, text):
            text_try = (text_try or "").strip()
            if not text_try.startswith("{"):
                continue
            try:
                data = json.loads(text_try)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    @staticmethod
    def _registry():
        from app.core.compute.registry import get_registry
        return get_registry()

    @staticmethod
    def _instances(reg) -> List[Dict[str, Any]]:
        try:
            snap = reg.snapshot()
        except Exception:  # noqa: BLE001
            return []
        return list(snap.get("instances") or [])

    @staticmethod
    def _resolve_model(model_id: Optional[str] = None) -> str:
        """把请求里的 model_id 归一成受支持的模型 id。

        不认识 ⇒ 退回默认并记一条日志，**不抛错**：界面上的下拉可能存着
        上次的旧选择（模型被卸载了），这时让它退回默认，而不是整个状态
        接口 400。
        """
        mid = str(model_id or "").strip()
        if mid in MODEL_IDS:
            return mid
        if mid:
            LOG.warning("content_review: 不认识的模型 %r，退回默认 %s",
                        mid, DEFAULT_MODEL)
        return DEFAULT_MODEL

    @staticmethod
    def _presets(reg) -> Dict[str, Dict[str, Any]]:
        """算力页的模型清单 → ``{model_id: 条目}``（只留本卡支持的决策模型）。

        **真源 = manifest**（``list_supported_models`` 从扫描结果派生），
        模型没装 ⇒ 压根没有这条。
        """
        out: Dict[str, Dict[str, Any]] = {}
        try:
            for m in reg.list_supported_models():
                mid = str(m.get("id") or "")
                if mid not in MODEL_IDS:
                    continue
                if CAPABILITY not in (m.get("capability_types") or []):
                    continue
                out[mid] = m
        except Exception:  # noqa: BLE001
            pass
        return out

    @staticmethod
    def _preset(reg, model_id: str) -> Optional[Dict[str, Any]]:
        """这个模型的清单条目（标签 / 显存估算 / 装没装）。"""
        return ContentReviewManager._presets(reg).get(model_id)

    @staticmethod
    def _label(preset: Optional[Dict[str, Any]], model_id: str) -> str:
        """显示用的模型标签（manifest 优先，没装时用兜底表）。"""
        if preset:
            label = str(preset.get("label") or "").strip()
            if label:
                return label
        return _FALLBACK_LABELS.get(model_id, model_id)

    @staticmethod
    def _models(reg,
                presets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """全部候选模型的状态（界面渲染下拉用）。"""
        out: List[Dict[str, Any]] = []
        for mid in MODEL_IDS:
            preset = presets.get(mid)
            inst = ContentReviewManager._find_instance(reg, mid)
            out.append({
                "id": mid,
                "label": ContentReviewManager._label(preset, mid),
                "installed": bool(preset) and bool(preset.get("exists")),
                "memory_mb": int((preset or {}).get("memory_estimate_mb") or 0),
                "running": bool(inst and inst.get("status") == "running"),
                "instance_status": (inst or {}).get("status") or "not_loaded",
                "npu_index": (inst or {}).get("npu_index"),
            })
        return out

    @staticmethod
    def _find_instance(reg, model_id: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
        for i in ContentReviewManager._instances(reg):
            if i.get("model_id") == model_id:
                return i
        return None

    @staticmethod
    def _find_all(reg, model_id: str = DEFAULT_MODEL) -> List[Dict[str, Any]]:
        """该模型的**全部**实例。"""
        return [
            i for i in ContentReviewManager._instances(reg)
            if i.get("model_id") == model_id
            and CAPABILITY in (i.get("capability_types") or [])
        ]

    @staticmethod
    def _installed(reg, model_id: str = DEFAULT_MODEL) -> bool:
        """模型装了没（算力卡片列表里有它 = 装了）。"""
        preset = ContentReviewManager._preset(reg, model_id)
        return bool(preset) and bool(preset.get("exists"))

    @staticmethod
    def _cards(reg) -> List[Dict[str, Any]]:
        """本机能跑 rknn 模型的卡位（= EP 协卡，0 起连续编号）。"""
        try:
            return [
                {"npu_index": int(c["npu_index"]),
                 "device_id": c.get("device_id")}
                for c in reg.list_cards()
            ]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _model_dir(model_id: str = DEFAULT_MODEL) -> str:
        try:
            from app.core.compute.workers import load_one
            m = load_one(model_id)
            if m is not None and m.model_dir:
                return str(m.model_dir)
        except Exception:  # noqa: BLE001
            pass
        return "(未知)"

    @staticmethod
    def _card_occupancy(reg, card: int,
                        model_id: str = DEFAULT_MODEL) -> int:
        """这张卡上**除本模型外**的本机实例显存占用（MB）。

        与 ``web_translate`` 卡的 ``_local_card_occupancy`` 同一套口径：只算
        本机实例（远端 peer 的显存吃在对端板上）、不算本模型自己（我们问的
        是「还能不能再加一份」）。
        """
        used = 0
        for inst in ContentReviewManager._instances(reg):
            if inst.get("model_id") == model_id:
                continue
            if (inst.get("_source") or "spawned") not in LOCAL_SOURCES:
                continue
            if inst.get("status") not in ("running", "loading"):
                continue
            mem = int(inst.get("memory_estimate_mb") or 0)
            if mem <= 0:
                continue
            cards = inst.get("npu_indices") or [inst.get("npu_index")]
            for c in cards:
                if c is None:
                    continue
                if int(c) == int(card):
                    used += mem
        return used

    @staticmethod
    def _memory_warning(reg, card: Optional[int] = None,
                        model_id: str = DEFAULT_MODEL) -> str:
        """目标卡上的显存**还够不够**放一份决策模型（提示，不是拦截）。

        ⚠️ **2026-11-19 起决策模型不再独占整卡** —— 0.8B 只要 ~900 MB，
        和翻译模型（2200 MB）同卡也放得下（900 + 2200 = 3100 < 5120）⇒
        共居是常态，不该再拦。

        ⇒ 只在**估算确实放不下**时提示一句。估算是估算值（4B 的 3200 MB
        是实测口径，0.8B 的 900 MB 是外推），真装不下时 worker 自己会报
        加载失败 —— 那比提前拦死更准。
        """
        if card is None:
            return ""
        try:
            # ⚠️ ``model_cards()`` 返回的是 **List[int]**（卡号列表），不是
            # dict 列表 —— 写成 ``c["npu_index"]`` 会 TypeError，被 except
            # 吞掉 ⇒ 检测**永远返回空**（症状：明明卡上有别的模型，
            # 点启用却一句提示都没有）。
            cards = set(int(c) for c in reg.model_cards())
        except Exception:  # noqa: BLE001
            cards = set()
        # 取不到卡位就**不**据此判定（宁可少提示，也别把检测整段跳过）
        if cards and int(card) not in cards:
            return ""
        preset = ContentReviewManager._preset(reg, model_id)
        need = int((preset or {}).get("memory_estimate_mb") or 0)
        if need <= 0:
            return ""
        used = ContentReviewManager._card_occupancy(reg, card, model_id)
        if used + need <= CARD_MEMORY_MB:
            return ""
        name = ContentReviewManager._label(preset, model_id)
        return (
            "卡 #%d 上已有约 %d MB，%s 还要约 %d MB（卡上限 %d MB）—— "
            "可能放不下。请在「算力配置」页腾出显存，或换一张空卡。"
            % (card, used, name, need, CARD_MEMORY_MB)
        )

    @staticmethod
    def _decision_cards_in_use(reg) -> Dict[int, str]:
        """每张卡上正在跑的**决策模型** id（``{卡号: model_id}``）。"""
        out: Dict[int, str] = {}
        for inst in ContentReviewManager._instances(reg):
            if CAPABILITY not in (inst.get("capability_types") or []):
                continue
            if inst.get("status") not in ("running", "loading"):
                continue
            if (inst.get("_source") or "spawned") not in LOCAL_SOURCES:
                continue          # 远端 peer 不在本板上
            npu = inst.get("npu_index")
            if isinstance(npu, int) and npu >= 0:
                out.setdefault(int(npu), str(inst.get("model_id")))
        return out

    @staticmethod
    def _pick_card(reg, preferred: int, model_id: str) -> int:
        """挑一张能放这个决策模型的卡。

        ⚠️ **两个决策模型不叠同一张卡** —— 不是显存不够（实测 4B + 0.8B
        = 4100 < 5120，同卡跑得好好的），而是 registry 的 autostart 维护
        「一卡一模型」不变量：两个决策模型挤同一张卡，``set_active_app`` 会
        把两条 autostart 条目**互相顶掉** ⇒ config 被反复重写，且**重启后
        只剩一个自启**（实测 4B 的自启被 0.8B 顶掉）。

        ⇒ 首选卡上已经有别的决策模型时，换一张没跑的卡。与**其它**模型共居
        不受此限 —— 那是本卡「不再独占整卡」的意义。
        """
        in_use = ContentReviewManager._decision_cards_in_use(reg)
        others = {c: m for c, m in in_use.items() if m != model_id}
        if int(preferred) not in others:
            return int(preferred)
        try:
            cards = sorted(int(c) for c in reg.model_cards())
        except Exception:  # noqa: BLE001
            cards = []
        for c in cards:
            if c not in others:
                return c
        raise RuntimeError(
            "本机每张协卡上都跑着决策模型了（%s）—— 请先停掉一个再启用别的。"
            % "、".join("%s@#%d" % (m, c) for c, m in sorted(others.items()))
        )

    @staticmethod
    def _wait_ready(reg, card: int, model_id: str = DEFAULT_MODEL,
                    timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            inst = ContentReviewManager._find_instance(reg, model_id)
            if inst and inst.get("status") == "running" \
                    and inst.get("npu_index") == card:
                return True
            if inst and inst.get("status") == "error":
                return False
            time.sleep(1.0)
        return False


_MANAGER: Optional[ContentReviewManager] = None
_MANAGER_LOCK = threading.Lock()


def get_service() -> ContentReviewManager:
    """单例（卡片后端的标准入口）。"""
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = ContentReviewManager()
    return _MANAGER
