"""网页翻译卡片的服务端 —— 翻译服务的**启停**与状态。

这张卡是「网页翻译」这个**应用**的门面，但真正干活的翻译模型
（``swcap_translate`` / HY-MT1.5）跑在 compute worker 里。卡片负责三件事:

1. **启停翻译服务** —— 起/停模型实例,并把 provider 链里的 ``npu`` 那一路
   打开/摘掉（见 ``app.core.translate.set_npu_translate_enabled``）。
2. **选卡** —— 默认走**第一张 1828 协卡**（``npu_index=0``）。本机的 EP 协卡
   按 PCI BDF 排序编号（见 ``app.core.compute.cards``），所以 0 号就是
   「第一张 1828」。
3. **把状态摊给界面** —— 界面要能显示「已启用 / 未启用 / 加载中 / 启动失败」
   以及当前用的是哪张卡。

为什么启停要在这里做，而不是让算力页代劳
------------------------------------------
算力页的开关是**按模型**的（``/api/compute/instances``），没有「启用后还要
把 NPU 接进翻译链」这一步；而翻译链是**运行期**状态（``config.jsonc`` 只是
默认值）。把两件事绑在一张卡上，用户点一次「启用」就得到「模型起来了 +
界面翻译走 NPU」，不用再去算力页点一次、再改一次配置。

⚠️ **卡位冲突**：图像生成（``qwenimage21``）要**两张**协卡，翻译模型只要
一张 —— 同占 0 号卡时会抢显存，起不来（实测 ``MODEL_SETUP fail``）。卡片
在启停前会检查冲突并把原因说清楚，而不是只报一句「启动失败」。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("apps.web_translate")

#: 本卡提供的 capability（= ``static/wizard.json`` 的 ``swcap_to_model`` 键名）
CAPABILITY = "swcap_translate"

#: 首选翻译模型
DEFAULT_MODEL = "hy-mt-1-5"

#: 默认卡位 —— 第一张 1828 协卡
DEFAULT_NPU_INDEX = 0

#: 默认启用的**槽位**（卡号, 本卡第几份）。
#:
#: 4 个 = 2 张 1828 协卡 × 每卡 2 份。实测（RK1828 双卡，24 条界面文案）：
#:
#:     provider 层（只跑推理）：
#:     1 实例            3.30s   1.00×
#:     2 实例（同卡）    2.74s   1.21×
#:     2 实例（两张卡）  1.76s   1.85×
#:     4 实例（2 卡×2）  1.45s   2.31×   ← 默认
#:
#:     端到端（POST /api/translate）：
#:     1 实例            5.52s
#:     4 实例（2 卡×2）  2.78s   1.99×
#:
#: ⚠️ **加速的单位是「卡」不是「实例」** —— 同卡加一份只 +21%（两个
#: rknn3 context 抢同一张卡的算力），加到第二张卡直接 +85%。
#:
#: 真源 = 同目录 ``static/wizard.json`` 的
#: ``swcap_to_model.swcap_translate.instance_slots`` —— 向导和卡片读的是
#: 同一份，两处不一致会出现「向导开了 4 个、卡片只起 1 个」。
DEFAULT_SLOTS = ((0, 0), (0, 1), (1, 0), (1, 1))

#: 起实例后等它 ready 的上限（模型加载约 20~30s，留一倍余量）
_LOAD_TIMEOUT_S = 120.0
_LOAD_POLL_S = 2.0

#: 预热用的最小翻译句 —— 让**第一次**真实翻译不用等 DDS 的首次握手
_WARMUP_PROMPT = "Translate the following text from zh to en:\nok"


#: 一张 EP 协卡的 NPU 显存（MB）。实测 RK1828 协卡 = 5120 MB。
CARD_MEMORY_MB = 5120

#: 翻译模型每份的显存估算（manifest 的 ``memory_estimate_mb``）。
TRANSLATE_MEMORY_MB = 2200


def _slots(reg=None) -> List[tuple]:
    """本卡默认启用的槽位表 [(卡号, 本卡第几份), ...]。

    读同目录 ``static/wizard.json`` 的 ``instance_slots``（向导读的是同一
    份）；读不到就退回 :data:`DEFAULT_SLOTS`。

    ⚠️ **给了 ``reg`` 就会避开放不下的卡**：一张 1828 协卡只有 5120 MB，
    而决策模型（LLM + 视觉常驻）一份就 ~3200 MB ⇒ 它占着的卡**塞不下**
    翻译模型（3200 + 2200 = 5400 > 5120）。

    ⇒ 所以「决策模型独占卡 0」时，翻译自动只在**另一张卡**上跑 —— 两边
    都不用用户去算力页一格格取消勾选。翻译自己的实例**不计入**占用
    （否则第二次 enable 会把自己起的份也算成「占着」）。
    """
    import json as _json
    from pathlib import Path as _Path

    out: List[tuple] = []
    try:
        cfg = _json.loads(
            (_Path(__file__).resolve().parent / "static" / "wizard.json")
            .read_text(encoding="utf-8"))
        raw = (((cfg.get("swcap_to_model") or {})
                .get(CAPABILITY) or {}).get("instance_slots"))
        for sl in raw or []:
            n = int(sl.get("npu_index"))
            r = int(sl.get("replica") or 0)
            if n >= 0:
                out.append((n, r))
        if not out:
            out = list(DEFAULT_SLOTS)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("web_translate: 读 wizard.json 的 instance_slots 失败: %s", exc)
        out = list(DEFAULT_SLOTS)

    if reg is None:
        return out
    free = _cards_with_room_for_one_copy(reg)
    if not free:
        raise RuntimeError(
            "没有一张协卡放得下翻译模型（每份约 %d MB，一张卡 %d MB）"
            % (TRANSLATE_MEMORY_MB, CARD_MEMORY_MB)
        )
    kept = [(n, r) for (n, r) in out if n in free]
    if not kept:
        raise RuntimeError(
            "没有一张协卡放得下翻译模型 —— 已被别的模型占满了"
        )
    skipped = sorted({n for (n, _r) in out} - free)
    if skipped:
        # ⚠️ 把**占着它的本机模型**说出来 —— 「被别的模型占着」太笼统，
        # 用户会以为卡上真跑着东西（本板 3 张卡全空时这句话最误导）。
        blockers = _local_blockers(reg)
        who = ("（%s）" % blockers) if blockers else ""
        LOG.info("web_translate: 卡 %s 放不下翻译模型%s（被本机模型占着），"
                 "只在卡 %s 上起", skipped, who,
                 sorted({n for (n, _r) in kept}))
    return kept


def _local_blockers(reg) -> str:
    """本机占着卡的那些模型名（给日志/人话用）。

    只列**本机**实例（远端 peer 不占本板卡位）；同卡多份会合并。
    """
    names: List[str] = []
    try:
        snap = reg.snapshot()
    except Exception:  # noqa: BLE001
        return ""
    for inst in snap.get("instances") or []:
        if inst.get("model_id") == DEFAULT_MODEL:
            continue
        if (inst.get("_source") or "spawned") not in LOCAL_SOURCES:
            continue
        if inst.get("status") not in ("running", "loading"):
            continue
        label = inst.get("label") or inst.get("model_id")
        if label and label not in names:
            names.append(str(label))
    return "、".join(names)


#: **本机**实例的来源标记 —— 只有这两种真的吃本板的 NPU 显存。
#:
#: ``spawned``        = 本进程 spawn 出来的 worker；
#: ``adopted_local``  = 本机的孤儿 worker（上一次 uvicorn 重载留下的），
#:                       本板 /proc 里能看到它的进程 ⇒ 它的显存吃在**本板**。
#:
#: ⚠️ ``adopted`` **不算** —— 那是**别的板子**通过 DDS 暴露的 service。
LOCAL_SOURCES = ("spawned", "adopted_local")


def _local_card_occupancy(reg) -> dict:
    """每张**本板**协卡当前被本地模型占了多少 MB。

    ⚠️ **只统计本机实例** —— 这是本模块最容易踩的坑（2026-11-18 实测）：

    ``_source == "adopted"`` 是**另一块板子**通过 DDS 暴露的 service：

    * 它的 ``npu_index`` 是**对端板自己的卡号**，不是本板的；
    * 它的显存吃在**对端板**上，本板一字节都没用。

    把它算进本板卡位 ⇒ 一张**空卡**被判成「放不下翻译模型」。
    真机现场（2026-11-18）：peer 板 ``172.16.9.147`` 的
    ``LocateAnything-3b`` 报 ``npu=1 / 5500MB`` ⇒ 本板卡 1 被误判占满
    （5500 + 2200 > 5120）⇒ 翻译模型只起卡 0 的 2 份（而不是 4 份），
    日志还打「卡 [1] 放不下翻译模型（被别的模型占着）」—— 而本板
    **3 张 1828 全空**、一个本地 worker 都没有。

    ⇒ 同一份数据在 ``registry.snapshot()`` 的 ``per_card_memory_mb``
    里也有同样的毛病（peer 的 5500MB 被记到本板卡 1），那边要一起修。
    """
    used: dict = {}
    try:
        snap = reg.snapshot()
    except Exception:  # noqa: BLE001
        return used
    for inst in snap.get("instances") or []:
        # 翻译模型自己的实例不算 —— 我们问的是「还能不能再加一份」
        if inst.get("model_id") == DEFAULT_MODEL:
            continue
        # ⚠️ 远端 peer 的实例**不占本板显存**，一律跳过（见上面注释）
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
            used[int(c)] = used.get(int(c), 0) + mem
    return used


def _cards_with_room_for_one_copy(reg) -> set:
    """还能再塞**一份**翻译模型的卡号集合。

    按实例的 ``memory_estimate_mb`` 估算每张卡的占用 —— 决策模型那个
    3200 MB 就是「LLM + 视觉常驻」的峰值估算，正好表达了「它要独占一卡」。

    ⚠️ **只算本机实例**（远端 peer 不占本板显存）—— 见
    :func:`_local_card_occupancy` 的注释。
    """
    try:
        all_cards = set(int(c) for c in reg.model_cards())
    except Exception:  # noqa: BLE001
        return set()
    if not all_cards:
        return set()
    used = _local_card_occupancy(reg)
    return {
        c for c in all_cards
        if used.get(c, 0) + TRANSLATE_MEMORY_MB <= CARD_MEMORY_MB
    }


class WebTranslateManager:
    """网页翻译服务管理器；单例（``get_service()``）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 启停是**慢操作**（要等模型加载），别让两个请求同时进来把卡搞乱
        self._op_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        """当前状态快照（界面每次刷新都拉一次）。"""
        reg = self._registry()
        instances = self._instances(reg)
        model_id = DEFAULT_MODEL
        mine = [i for i in instances
                if i.get("model_id") == model_id
                and CAPABILITY in (i.get("capability_types") or [])]
        inst = mine[0] if mine else None
        npu_index = int(inst.get("npu_index")) if inst and inst.get("npu_index") is not None else None
        inst_status = (inst or {}).get("status") or "not_loaded"
        installed = self._installed(reg, model_id)
        chain_has_npu = self._chain_has_npu()
        # 「计划几份」要和**实际能起几份**一致 —— 决策模型独占一张卡时，
        # 翻译只能起另一张卡的 2 份，说「计划 4 份」是误导。
        # （取不到就退回全量，status 不该因为这个炸掉。）
        try:
            slots = _slots(reg)
        except RuntimeError:
            slots = _slots()

        # 正在跑的份数（每个实例一个 worker 进程，能同时跑一个推理）
        running = [i for i in mine if i.get("status") == "running"]
        enabled = bool(running and chain_has_npu)
        return {
            "enabled": enabled,
            "model_id": model_id,
            "capability": CAPABILITY,
            "npu_index": npu_index,
            "default_npu_index": DEFAULT_NPU_INDEX,
            "instance_status": inst_status,
            "instance_error": (inst or {}).get("error") or "",
            "installed": installed,
            "provider_in_chain": chain_has_npu,
            "cards": self._cards(reg),
            "conflict": self._conflict(reg, model_id),
            # 槽位 / 已跑的份 —— 界面要显示「4 份（卡0 ×2 + 卡1 ×2）」
            "slots": [{"npu_index": n, "replica": r} for (n, r) in slots],
            "running_count": len(running),
            "expected_count": len(slots),
            "instances": [
                {"npu_index": i.get("npu_index"),
                 "replica": int(i.get("replica") or 0),
                 "status": i.get("status"),
                 "pid": i.get("pid")}
                for i in mine
            ],
            "detail": self._detail(enabled, inst_status, installed,
                                    chain_has_npu, len(running), len(slots)),
        }

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def enable(self, npu_index: Optional[int] = None) -> Dict[str, Any]:
        """启用翻译服务：起模型实例 + 把 ``npu`` 接进 provider 链。

        默认起 **4 个实例**（2 张 1828 协卡 × 每卡 2 份，见
        :data:`DEFAULT_SLOTS`）：每份是独立的 worker 进程，能同时跑一个
        推理 —— 这是界面翻译能并行的**唯一**单位（一个 session 的 RPC
        执行器是 ``max_workers=1``）。

        ⚠️ 但**放不下的卡会被自动跳过**：决策模型独占一张卡时（LLM+视觉
        常驻 ~3200 MB），那张卡塞不下翻译模型（3200+2200 > 5120）⇒ 翻译
        只在另一张卡上起（2 份而不是 4 份）。

        ``npu_index`` 给了就只起**那一张卡的 1 份**（老调用方 / 手工排查用）。
        """
        with self._op_lock:
            reg = self._registry()
            slots = ([(int(npu_index), 0)] if npu_index is not None
                     else _slots(reg))
            if not self._installed(reg, DEFAULT_MODEL):
                raise RuntimeError(
                    f"本机没有安装翻译模型 {DEFAULT_MODEL}"
                    f"（目录 {self._model_dir()}）"
                )
            for card, _rep in slots:
                bad = reg.npu_card_error(card)
                if bad:
                    raise RuntimeError(bad)
            conflict = self._conflict(reg, DEFAULT_MODEL)
            if conflict:
                raise RuntimeError(conflict)

            for (card, replica) in slots:
                # 已经在跑就直接复用，别重复 load。
                # ⚠️ 判据必须带上 **replica** —— 同卡的第 1 份在跑，不代表
                # 第 2 份也起来了。
                inst = self._find_slot(reg, DEFAULT_MODEL, card, replica)
                if inst and inst.get("status") == "running" \
                        and inst.get("npu_index") == card:
                    continue
                # model_op_lock：同模型的启停串行化 —— 和算力页点「启用」
                # 同时发生时，两个 init 线程抢同一份 NPU 内存会 rc=-6。
                with reg.model_op_lock(DEFAULT_MODEL):
                    inst = reg.ensure_worker_for_model(
                        DEFAULT_MODEL, allow_adopt=False, npu_index=card,
                        eager_load=False, replica=replica,
                    )
                    reg.bind_instance(inst, card)
                if not self._wait_ready(reg, DEFAULT_MODEL, card, replica):
                    err = self._find_slot(reg, DEFAULT_MODEL, card,
                                          replica) or {}
                    raise RuntimeError(
                        "翻译模型启动失败（卡 #%d 第 %d 份）："
                        % (card, replica + 1)
                        + (err.get("error") or "未知原因")
                        + "（常见原因：该卡显存被别的模型占着 —— "
                          "同卡开 %d 份需要约 %d MB）"
                        % (len(slots),
                           len(slots) * 2200 // max(1, len({c for c, _ in slots})))
                    )
            # 模型起来了 → 把 NPU 接进翻译链（放在最前，当主力）
            self._set_chain(True)
            self._warmup()
            return self.status()

    def disable(self) -> Dict[str, Any]:
        """禁用翻译服务：把 ``npu`` 摘出 provider 链 + 停掉模型实例。

        先摘链再停模型 —— 反过来的话会有一个窗口期：链里还留着 npu，
        而模型已经在卸载，翻译请求会一头撞在超时上。

        停模型走 :meth:`stop_instance_local`（= 算力页「禁用」的同一套
        语义：unload → RPC shutdown → SIGTERM → 3s → SIGKILL），**不是**
        只 unload —— 只 unload 会把 NPU 显存还回去但把 worker 进程留着，
        卡片「禁用」了却有个僵尸进程占着内存。
        """
        with self._op_lock:
            self._set_chain(False)
            reg = self._registry()
            # ⚠️ **停全部实例**，不只是第一个 —— 同卡多份时漏停会留下
            # 还在占显存的僵尸 worker。
            for inst in self._find_all(reg, DEFAULT_MODEL):
                npu = inst.get("npu_index")
                if not isinstance(npu, int) or npu < 0:
                    npu = None
                replica = int(inst.get("replica") or 0)
                try:
                    with reg.model_op_lock(DEFAULT_MODEL):
                        reg.stop_instance_local(DEFAULT_MODEL, npu, replica)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("web_translate: 卸载实例失败（卡 %s 第 %d 份）: %s",
                                npu, replica + 1, exc)
            return self.status()

    # ------------------------------------------------------------------ #
    # 翻译（给界面上那个「试一下」用）
    # ------------------------------------------------------------------ #
    def translate(self, segments: List[str], source: str = "zh-CN",
                  target: str = "en") -> Dict[str, Any]:
        """翻一批文案（走的是**和界面右上角完全同一条**路径）。"""
        from app.core.translate import (
            QuotaExceeded,
            TranslationError,
            get_translate_service,
        )
        service = get_translate_service()
        try:
            translations, stats = service.translate_segments(segments, source, target)
        except QuotaExceeded as exc:
            raise RuntimeError(str(exc)) from exc
        except TranslationError as exc:
            raise RuntimeError(str(exc)) from exc
        return {"translations": translations, **stats}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
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
        out = []
        for i in snap.get("instances", []):
            out.append(i)
        return out

    @staticmethod
    def _find_instance(reg, model_id: str) -> Optional[Dict[str, Any]]:
        for i in WebTranslateManager._instances(reg):
            if i.get("model_id") == model_id:
                return i
        return None

    @staticmethod
    def _find_slot(reg, model_id: str, card: int,
                   replica: int = 0) -> Optional[Dict[str, Any]]:
        """找**某个槽位**上的实例（卡号 + 本卡第几份）。"""
        for i in WebTranslateManager._instances(reg):
            if i.get("model_id") != model_id:
                continue
            npu = i.get("npu_index")
            # ⚠️ **不能用 ``i.get("npu_index") or -1``** —— 卡号 **0** 是
            # 合法值，`0 or -1` 会变成 -1，于是卡 0 上的实例永远匹配不上
            # （表现为「明明 loading→running 了，启用却报启动失败」）。
            if not isinstance(npu, int) or npu != int(card):
                continue
            if int(i.get("replica") or 0) != int(replica):
                continue
            return i
        return None

    @staticmethod
    def _find_all(reg, model_id: str) -> List[Dict[str, Any]]:
        """该模型的**全部**实例（每份一个）。"""
        return [i for i in WebTranslateManager._instances(reg)
                if i.get("model_id") == model_id]

    @staticmethod
    def _installed(reg, model_id: str) -> bool:
        """模型装了没（算力卡片列表里有它 = 装了）。"""
        try:
            for m in reg.list_supported_models():
                if m.get("id") == model_id:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _cards(reg) -> List[Dict[str, Any]]:
        """本机能跑 rknn 模型的卡位（= EP 协卡，0 起连续编号）。"""
        try:
            return [
                {"npu_index": int(c["npu_index"]), "device_id": c.get("device_id")}
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

    def _conflict(self, reg, model_id: str) -> str:
        """这张卡上是不是已经有别的模型占着。

        图像生成（``qwenimage21``）要**两张**协卡，和翻译模型同占 0 号卡
        会抢显存 —— 实测起不来时报 ``MODEL_SETUP fail``。与其让用户对着
        「启动失败」发呆，不如提前说清楚。

        ⚠️ **只查本机实例** —— 远端 peer 的 ``npu_index`` 是**对端板**的
        卡号，拿它判本板冲突 ⇒ 一张空卡被误报成「卡位冲突」。
        """
        try:
            # ⚠️ ``model_cards()`` 返回 **List[int]**（卡号列表），不是 dict
            # 列表 —— 写成 ``c["npu_index"]`` 会 TypeError，被 except 吞掉 ⇒
            # 冲突检测**永远返回空**（卡位冲突的提示从来没生效过）。
            cards = set(int(c) for c in reg.model_cards())
        except Exception:  # noqa: BLE001
            return ""
        for i in self._instances(reg):
            if i.get("model_id") == model_id:
                continue
            # ⚠️ 远端 peer 不占本板卡位（见 _local_card_occupancy 的注释）
            if (i.get("_source") or "spawned") not in LOCAL_SOURCES:
                continue
            if i.get("status") not in ("running", "loading"):
                continue
            used = i.get("npu_indices")
            if not used:
                used = [i.get("npu_index")]
            used = [int(u) for u in (used or []) if u is not None]
            if not used:
                continue
            need = int(i.get("requires_cards") or 1)
            if need > 1 and set(used) & cards:
                return (
                    f"卡位冲突：「{i.get('label') or i.get('model_id')}」"
                    f"占用 {used} 号协卡（它需要 {need} 张），"
                    f"翻译模型放不下。请先在「算力配置」页停掉它。"
                )
        return ""

    @staticmethod
    def _chain_has_npu() -> bool:
        try:
            from app.core.translate import npu_translate_enabled
            return bool(npu_translate_enabled())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _set_chain(enabled: bool) -> None:
        try:
            from app.core.translate import set_npu_translate_enabled
            set_npu_translate_enabled(bool(enabled))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("web_translate: 切换翻译链失败: %s", exc)

    def _wait_ready(self, reg, model_id: str, card: Optional[int] = None,
                    replica: Optional[int] = None) -> bool:
        """等某个槽位就绪（``card``/``replica`` 不给 = 任意一个就行）。"""
        deadline = time.time() + _LOAD_TIMEOUT_S
        while time.time() < deadline:
            if card is None:
                inst = self._find_instance(reg, model_id)
            else:
                inst = self._find_slot(reg, model_id, card,
                                        replica or 0)
            if inst:
                st = inst.get("status")
                if st == "running":
                    return True
                if st == "error":
                    return False
            time.sleep(_LOAD_POLL_S)
        return False

    def _warmup(self) -> None:
        """预热一次 —— 第一次 RPC 调用要等 DDS 首次握手（实测 ~15s）。

        不预热的话，用户第一次切界面语言就要干等十几秒。

        ⚠️ **在后台线程里跑**，不阻塞 :meth:`enable` 返回 —— 预热是「优化」
        不是「前提」，而它自己可能要 15s。让它挡住返回的话，点「启用」
        的按钮要转 45s 才出结果（模型加载 30s + 预热 15s），用户会以为
        卡死了。服务已经接进 provider 链了，预热在后面跑完即可。
        """
        def _run() -> None:
            try:
                from app.core.translate.npu_provider import NpuTranslateProvider
                prov = NpuTranslateProvider()
                prov._infer(prov._session(), _WARMUP_PROMPT)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("web_translate: 预热失败（不影响功能）: %s", exc)

        threading.Thread(target=_run, name="web-translate-warmup",
                         daemon=True).start()

    @staticmethod
    def _detail(enabled: bool, inst_status: str, installed: bool,
                chain_has_npu: bool, running: int = 0,
                expected: int = 0) -> str:
        if not installed:
            return f"本机未安装翻译模型 {DEFAULT_MODEL}。"
        if enabled:
            tail = ("（%d 份并行翻译）" % running) if running > 1 else ""
            return ("翻译服务已启用 —— 右上角「界面语言」切换将使用本板 NPU 翻译"
                    + tail + "。")
        if inst_status == "loading":
            return "翻译模型正在加载，请稍候（约 20~30 秒）。"
        if inst_status == "error":
            return "翻译模型启动失败，请查看下方错误信息。"
        if not chain_has_npu:
            return "翻译服务已停止 —— 界面翻译会回退到外网翻译站。"
        if expected and running < expected:
            return ("翻译服务未完全启用（%d/%d 份在跑）—— 界面翻译会走已就绪的"
                    "那些实例。" % (running, expected))
        return "翻译服务未启用。"


_manager: Optional[WebTranslateManager] = None
_manager_lock = threading.Lock()


def get_service() -> WebTranslateManager:
    """拿到全局管理器（单例）。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WebTranslateManager()
        return _manager
