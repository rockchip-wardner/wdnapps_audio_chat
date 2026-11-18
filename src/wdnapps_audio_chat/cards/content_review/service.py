"""图文内容审查卡片的后端 —— 启停决策模型 + 跑一次结构化决策。

这个模型（Intern-Decision-4B）**不是对话模型**：它不生成文本，而是对任务里
的每个「决策字段」输出 62 个候选符号的 logits，由 CPU 侧换算成选项概率。
所以本卡的「概率估计」= 把用户维护的那张**可增删列表**翻译成一组
``choice``（单选）字段 —— 列表里的每一项是它的一个选项。

为什么是**一个** choice 字段而不是 N 个 noul 字段（2026-11-18 改）
--------------------------------------------------
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

#: 本卡用的模型 id（算力页上显示为「[JEV]图像文字决策模型4B」）。
DEFAULT_MODEL = "intern-decision-4b"
DEFAULT_MODEL_LABEL = "[JEV]图像文字决策模型4B"

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
    def status(self) -> Dict[str, Any]:
        """当前状态快照（界面每次刷新拉一次）。"""
        reg = self._registry()
        instances = self._instances(reg)
        mine = [
            i for i in instances
            if i.get("model_id") == DEFAULT_MODEL
            and CAPABILITY in (i.get("capability_types") or [])
        ]
        inst = mine[0] if mine else None
        inst_status = (inst or {}).get("status") or "not_loaded"
        npu_index = None
        if inst is not None and isinstance(inst.get("npu_index"), int):
            npu_index = int(inst["npu_index"])
        enabled = inst_status == "running"
        installed = self._installed(reg)

        detail = "决策模型已就绪 —— 可以提交图文做内容审查。"
        if not installed:
            detail = ("本机未安装决策模型 %s —— 装好后才能启用。"
                      % DEFAULT_MODEL)
        elif inst_status == "loading":
            detail = "决策模型正在加载（约 40 秒），稍候即可提交。"
        elif inst_status == "error":
            detail = "决策模型启动失败：%s" % ((inst or {}).get("error")
                                              or "未知原因")
        elif not enabled:
            detail = "决策模型未启用 —— 启用后即可对图文做内容审查。"

        return {
            "enabled": enabled,
            "model_id": DEFAULT_MODEL,
            "model_label": DEFAULT_MODEL_LABEL,
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
            "conflict": self._conflict(
                reg, npu_index if npu_index is not None else DEFAULT_NPU_INDEX
            ),
        }

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def enable(self, npu_index: Optional[int] = None) -> Dict[str, Any]:
        """启用：起决策模型的一个实例，并把**这张卡清空**。

        决策模型要**独占**一整张卡：它的 LLM + 视觉编码器常驻，实测
        ~3100 MB，而一张 1828 协卡只有 5120 MB —— 同卡再塞一个翻译模型
        （2266 MB）就是 5366 MB，**顶爆**。

        ⇒ 所以这里**先清场**：把这张卡上的**所有其它模型实例**停掉，再起
        决策模型。用户点一下「启用」就该拿到一个独占的卡，而不是自己
        去算力页一格格取消勾选。

        决策模型一份要 ~50 秒加载，所以这是**慢端点** —— 界面上按钮转圈。
        """
        card = DEFAULT_NPU_INDEX if npu_index is None else int(npu_index)
        with self._op_lock:
            reg = self._registry()
            if not self._installed(reg):
                raise RuntimeError(
                    "本机没有安装决策模型 %s（目录 %s）"
                    % (DEFAULT_MODEL, self._model_dir())
                )
            bad = reg.npu_card_error(card)
            if bad:
                raise RuntimeError(bad)

            # ---- 清场：停掉这张卡上的所有其它模型 --------------------------
            stopped = self._clear_card(reg, card)

            conflict = self._conflict(reg, card)
            if conflict:
                raise RuntimeError(conflict)

            inst = self._find_instance(reg)
            if not (inst and inst.get("status") == "running"
                    and inst.get("npu_index") == card):
                with reg.model_op_lock(DEFAULT_MODEL):
                    inst = reg.ensure_worker_for_model(
                        DEFAULT_MODEL, allow_adopt=False, npu_index=card,
                        eager_load=False, replica=0,
                    )
                    reg.bind_instance(inst, card)
                if not self._wait_ready(reg, card):
                    err = self._find_instance(reg) or {}
                    raise RuntimeError(
                        "决策模型启动失败（卡 #%d）：%s"
                        % (card, err.get("error") or "未知原因")
                        + "（该模型一份约需 3100 MB 显存 —— 视觉编码器是"
                          "常驻的，同卡别的模型放不下）"
                    )
            out = self.status()
            # 告诉用户「为了腾出这张卡，我停掉了什么」
            out["stopped"] = stopped
            if stopped:
                out["detail"] = (
                    "决策模型已就绪（独占卡 #%d）—— 启用时已自动停掉：%s。"
                    % (card, "、".join(s["label"] for s in stopped))
                )
            return out

    def disable(self) -> Dict[str, Any]:
        """禁用：停掉决策模型实例。

        走 :meth:`stop_instance_local`（= 算力页「禁用」的同一套语义：
        unload → RPC shutdown → SIGTERM → SIGKILL），不是只 unload ——
        只 unload 会把显存还回去却把 worker 进程留着。
        """
        with self._op_lock:
            reg = self._registry()
            for inst in self._find_all(reg):
                npu = inst.get("npu_index")
                if not isinstance(npu, int) or npu < 0:
                    npu = None
                replica = int(inst.get("replica") or 0)
                try:
                    with reg.model_op_lock(DEFAULT_MODEL):
                        reg.stop_instance_local(DEFAULT_MODEL, npu, replica)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("content_review: 卸载实例失败: %s", exc)
            return self.status()

    # ------------------------------------------------------------------ #
    # 审查
    # ------------------------------------------------------------------ #
    def review(self, text: str = "",
               items: Optional[Sequence[Dict[str, Any]]] = None,
               image_b64: Optional[str] = None) -> Dict[str, Any]:
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
        try:
            session = reg.get_session_for_model(CAPABILITY, DEFAULT_MODEL)
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
            "model_id": DEFAULT_MODEL,
            "model_label": DEFAULT_MODEL_LABEL,
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
    def _find_instance(reg) -> Optional[Dict[str, Any]]:
        for i in ContentReviewManager._instances(reg):
            if i.get("model_id") == DEFAULT_MODEL:
                return i
        return None

    @staticmethod
    def _find_all(reg) -> List[Dict[str, Any]]:
        """该模型的**全部**实例。"""
        return [
            i for i in ContentReviewManager._instances(reg)
            if i.get("model_id") == DEFAULT_MODEL
            and CAPABILITY in (i.get("capability_types") or [])
        ]

    @staticmethod
    def _installed(reg) -> bool:
        """模型装了没（算力卡片列表里有它 = 装了）。"""
        try:
            for m in reg.list_supported_models():
                if m.get("id") == DEFAULT_MODEL:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

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
    def _model_dir() -> str:
        try:
            from app.core.compute.workers import load_one
            m = load_one(DEFAULT_MODEL)
            if m is not None and m.model_dir:
                return str(m.model_dir)
        except Exception:  # noqa: BLE001
            pass
        return "(未知)"

    @staticmethod
    def _clear_card(reg, card: int) -> List[Dict[str, Any]]:
        """停掉**这张卡上的所有其它模型实例**，让决策模型独占它。

        返回停掉了什么（给前端显示「已为你停掉 …」）。

        ⚠️ **必须逐实例停**：``stop_instance_local(model, npu, replica)`` 停的是
        **一个**实例 —— 翻译模型每卡 2 份，只停「卡 0 的第 0 份」的话第 1 份
        还占着显存，清场等于没清。

        ⚠️ 跨卡模型（``npu_indices`` 有多张）的实例要按**它自己的主卡**
        (``npu_index``) 去停，不能拿目标卡号去停 —— 那可能命中别的实例。
        """
        stopped: List[Dict[str, Any]] = []
        for inst in ContentReviewManager._instances(reg):
            mid = inst.get("model_id")
            if not mid or mid == DEFAULT_MODEL:
                continue
            if inst.get("status") not in ("running", "loading"):
                continue
            used = inst.get("npu_indices")
            if not used:
                used = [inst.get("npu_index")]
            used = [int(u) for u in (used or []) if u is not None]
            if int(card) not in used:
                continue
            npu = inst.get("npu_index")
            npu = int(npu) if isinstance(npu, int) and npu >= 0 else None
            replica = int(inst.get("replica") or 0)
            label = inst.get("label") or mid
            try:
                with reg.model_op_lock(mid):
                    reg.stop_instance_local(mid, npu, replica)
                stopped.append({"model_id": mid, "label": label,
                                "card": int(card), "replica": replica})
            except Exception as exc:  # noqa: BLE001
                LOG.warning("content_review: 清场停掉 %s 失败: %s", label, exc)
                raise RuntimeError(
                    "无法腾出卡 #%d：停掉「%s」失败（%s）"
                    % (card, label, exc)
                ) from exc
        if stopped:
            LOG.info("content_review: 清场卡 #%d，已停掉 %s",
                     card, "、".join(s["label"] for s in stopped))
        return stopped

    @staticmethod
    def _conflict(reg, card: Optional[int] = None) -> str:
        """目标卡上是不是已经被别的模型占着。

        决策模型一份要 ~3200 MB 显存 —— 同一张卡上再塞一个模型就挤不下
        （翻译模型每份 ~2200 MB，卡上两份额译 = 4400 MB，再加决策就爆）。
        与其让用户对着「启动失败」发呆，不如提前说清楚。

        ⚠️ **只看目标卡**（``card``）—— 早期版本扫所有卡，于是「卡 0 跑着
        翻译」会把「我想把决策模型放到卡 1」也一起拦掉（卡 1 明明是空的）。
        """
        if card is None:
            return ""
        try:
            # ⚠️ ``model_cards()`` 返回的是 **List[int]**（卡号列表），不是
            # dict 列表 —— 写成 ``c["npu_index"]`` 会 TypeError，被 except
            # 吞掉 ⇒ 冲突检测**永远返回空**（症状：明明卡上有别的模型，
            # 点启用却一句提示都没有）。
            cards = set(int(c) for c in reg.model_cards())
        except Exception:  # noqa: BLE001
            cards = set()
        # 取不到卡位就**不**据此判定冲突（宁可少提示，也别把检测整段跳过）
        if cards and int(card) not in cards:
            return ""
        for i in ContentReviewManager._instances(reg):
            if i.get("model_id") == DEFAULT_MODEL:
                continue
            if i.get("status") not in ("running", "loading"):
                continue
            used = i.get("npu_indices")
            if not used:
                used = [i.get("npu_index")]
            used = [int(u) for u in (used or []) if u is not None]
            if int(card) in used:
                return (
                    "卡 #%d 上正跑着「%s」，决策模型每份约需 3200 MB，"
                    "放不下。请先在「算力配置」页停掉它，或换一张空卡。"
                    % (card, i.get("label") or i.get("model_id"))
                )
        return ""

    @staticmethod
    def _wait_ready(reg, card: int, timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            inst = ContentReviewManager._find_instance(reg)
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
