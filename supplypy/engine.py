"""按周物理平衡引擎。

不执行真实运输调度，只依据契约容量判断承诺能否成立，每周顺序：
1. 仓内分龄损耗与年龄推进；
2. 按共同天气情景生成各地块计划、执行率、含水率；
3. 在季节/轮伐/合作社加工能力约束内安排采收（保底优先于可选）；
4. 路线按"正常优先、替补兜底"在共享通道容量内分配，同名通道不重复计数；
5. 到货入共享库容，超出部分拒收（不凭空生成）；
6. FIFO 取料折算可用热量，对照锅炉需求记账，缺口转应急煤。

每个未满足的保底都留下带原因码的冲突记录，便于定位时间与资源。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from .calendar import eligible_sources
from .contracts import WeekCommitment
from .dataio import (
    COMMIT_FIRST,
    COMMIT_LAST,
    DataBundle,
)
from .history import Calibration
from .scenarios import (
    ScenarioPath,
    draw_execution_ratio,
    draw_moisture,
    draw_plan_size,
)
from .storage import Warehouse

# 单地块单次采收计划上限：取历史观测最大值，避免模型安排出从未出现过的超大批次
PLAN_HARD_CAP_T = 79.0


@dataclass
class Conflict:
    week: int
    coop_id: str
    resource: str  # season | rotation | processing | route | storage | no_source
    detail: str
    shortfall_t: float


@dataclass
class CoopDelivery:
    coop_id: str
    requested_min_t: float
    requested_option_t: float
    delivered_t: float
    min_shortfall_t: float
    option_delivered_t: float
    route_used: dict[str, float] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)


@dataclass
class WeekResult:
    week: int
    weather: str
    planned_t: float
    harvested_wet_t: float
    route_rejected_t: float
    storage_rejected_t: float
    storage_loss_t: float
    opening_stock_t: float
    closing_stock_t: float
    delivered_heat_mj: float
    demand_heat_mj: float
    heat_shortfall_mj: float
    emergency_coal_mj: float
    emergency_cost_yuan: float
    coop_deliveries: dict[str, CoopDelivery]
    route_usage: dict[str, float]
    route_capacity: dict[str, float]
    conflicts: list[Conflict]
    harvest_log: list[dict]


@dataclass
class ScenarioResult:
    index: int
    seed: int
    weeks: dict[int, WeekResult]
    total_floor_spend: float
    total_emergency_cost: float
    total_min_shortfall_t: float
    total_storage_loss_t: float

    def heat_series(self) -> np.ndarray:
        return np.array([self.weeks[w].delivered_heat_mj for w in sorted(self.weeks)])


class _RoutePool:
    """共享通道池：同名通道的容量被所有合作社共用，绝不重复计数。"""

    def __init__(self, capacity: dict[str, float]):
        self.capacity = dict(capacity)
        self.remaining = dict(capacity)
        self.usage: dict[str, float] = {r: 0.0 for r in capacity}

    def allocate(self, route: str, amount: float) -> float:
        if route not in self.remaining:
            return 0.0
        accepted = min(max(amount, 0.0), self.remaining[route])
        self.remaining[route] -= accepted
        self.usage[route] += accepted
        return accepted


def _effective_route_capacity(
    bundle: DataBundle,
    cap_view: dict[tuple[int, str], tuple[float, str]],
    path: ScenarioPath,
    week: int,
) -> dict[str, float]:
    """合并额定/通知容量与情景停运。情景停运只可能压低容量，并记录依据。"""
    routes = sorted({r for cr in bundle.coop_routes.values() for r in (cr.normal, cr.backup)})
    out: dict[str, float] = {}
    for route in routes:
        rated = cap_view.get((week, route))
        value = rated[0] if rated is not None else 0.0
        if route == "BRIDGE" and path.weeks[week].bridge_outage:
            value = min(value, path.weeks[week].bridge_capacity_t or 0.0)
        out[route] = value
    return out


def simulate_week(
    week: int,
    bundle: DataBundle,
    cal: Calibration,
    path: ScenarioPath,
    rng: np.random.Generator,
    house: Warehouse,
    commitments: dict[tuple[int, str], WeekCommitment],
    cap_view: dict[tuple[int, str], tuple[float, str]],
    last_harvest: dict[str, int],
    demand_heat: float,
    emergency_cost: float,
) -> WeekResult:
    weather = path.weather(week)
    opening_stock = house.stock_wet_t()
    storage_loss = house.age_and_loss()

    conflicts: list[Conflict] = []
    harvest_log: list[dict] = []
    planned_total = 0.0
    harvested_total = 0.0

    # ---- 采购义务（保底不可撤销；可选只是上限） ----
    coop_min: dict[str, float] = {}
    coop_option_extra: dict[str, float] = {}
    for coop_id in sorted(bundle.coop_routes):
        commitment = commitments.get((week, coop_id))
        if commitment is None:
            coop_min[coop_id] = 0.0
            coop_option_extra[coop_id] = 0.0
        else:
            coop_min[coop_id] = commitment.open_min_t
            coop_option_extra[coop_id] = max(
                0.0, commitment.open_option_cap_t - commitment.open_min_t)

    # ---- 第一步：自然采收批次（与采购量解耦） ----
    # 历史节奏是每个合格地块每周一次、批次约 65t；合同限制的是采购归属，不是地块产出。
    # 季节+轮伐决定哪些地块当周可采，加工能力按合作社封顶；无合格地块=本来无计划而非零产能。
    eligible = eligible_sources(bundle.sources, week, last_harvest)
    eligible_by_coop: dict[str, list[str]] = {}
    for sid in eligible:
        eligible_by_coop.setdefault(bundle.sources[sid].coop_id, []).append(sid)

    # lots_by_coop: [(source_id, actual_wet_t, moisture_pct, plan_t, ratio)]
    lots_by_coop: dict[str, list[tuple[str, float, float, float, float]]] = {
        c: [] for c in bundle.coop_routes}
    processing_left = {c: cr.processing_t for c, cr in bundle.coop_routes.items()}
    no_eligible: set[str] = set()
    for coop_id in sorted(bundle.coop_routes):
        cands = eligible_by_coop.get(coop_id, [])
        if not cands:
            no_eligible.add(coop_id)
        for sid in cands:
            if processing_left[coop_id] <= 1e-9:
                # 加工能力是合作社按周硬上限；剩余合格地块本周无法加工，不再排产
                break
            plan = min(draw_plan_size(cal, rng), PLAN_HARD_CAP_T, processing_left[coop_id])
            if plan <= 1e-9:
                break
            ratio = draw_execution_ratio(cal, weather, rng)
            moisture = draw_moisture(cal, weather, rng)
            actual = plan * ratio
            processing_left[coop_id] -= plan
            last_harvest[sid] = week
            planned_total += plan
            harvested_total += actual
            lots_by_coop[coop_id].append((sid, actual, moisture, plan, ratio))
            harvest_log.append({
                "source_id": sid, "coop_id": coop_id, "plan_t": plan,
                "actual_t": actual, "moisture_pct": moisture,
                "exec_ratio": ratio, "phase": "natural",
            })

    # ---- 第二步：路线共享容量，保底优先、可选其次的两趟分配 ----
    capacity = _effective_route_capacity(bundle, cap_view, path, week)
    pool = _RoutePool(capacity)
    route_rejected = 0.0
    # 每个合作社当周每趟的已运量
    shipped_min: dict[str, float] = {c: 0.0 for c in bundle.coop_routes}
    shipped_option: dict[str, float] = {c: 0.0 for c in bundle.coop_routes}
    route_used: dict[str, float] = {}
    # 按合作社记账的通道用量（共享通道总量仍由 pool 统一约束，不重复计数）
    coop_route_used: dict[str, dict[str, float]] = {
        c: {} for c in bundle.coop_routes}
    # 各合作社待运湿吨（采收产出）
    waiting: dict[str, float] = {
        c: sum(lot[1] for lot in lots) for c, lots in lots_by_coop.items()}

    def send(coop_id: str, route: str, amount: float) -> float:
        got = pool.allocate(route, amount)
        if got > 0:
            route_used[route] = route_used.get(route, 0.0) + got
            coop_route_used[coop_id][route] = coop_route_used[coop_id].get(route, 0.0) + got
        return got

    def ship_pass(target: dict[str, float], limit: dict[str, float]) -> None:
        """一趟运输：各合作社按正常→替补通道装货，受共享通道余量约束。"""
        for coop_id in sorted(bundle.coop_routes):
            need = max(0.0, limit[coop_id] - target[coop_id])
            if need <= 1e-9 or waiting[coop_id] <= 1e-9:
                continue
            cr = bundle.coop_routes[coop_id]
            for route in (cr.normal, cr.backup):
                amount = min(need, waiting[coop_id])
                if amount <= 1e-9:
                    break
                moved = send(coop_id, route, amount)
                waiting[coop_id] -= moved
                target[coop_id] += moved
                need -= moved

    ship_pass(shipped_min, coop_min)
    # 可选趟：上限为 min + option_extra，已运保底量不重复计算
    option_cap = {c: coop_min[c] + coop_option_extra[c] for c in bundle.coop_routes}
    shipped_option = dict(shipped_min)  # 可选趟从已运量起算
    ship_pass(shipped_option, option_cap)

    # 运不出的采收量（共享路线容量不足），不凭空入仓
    route_rejected = sum(waiting.values())

    # ---- 第三步：保底缺口的唯一根因归属 ----
    deliveries: dict[str, CoopDelivery] = {}
    for coop_id in sorted(bundle.coop_routes):
        need_min = coop_min[coop_id]
        min_got = min(shipped_min[coop_id], need_min)
        min_short = max(0.0, need_min - min_got)
        delivered = shipped_option[coop_id]
        option_delivered = max(0.0, delivered - need_min)
        local: list[Conflict] = []
        if min_short > 1e-9:
            produced = sum(lot[1] for lot in lots_by_coop[coop_id])
            if coop_id in no_eligible:
                resource, detail = "season_rotation", \
                    "当周无满足采收月份与轮伐间隔的地块（本来无计划，不等于零产能）"
            elif produced + 1e-9 < need_min:
                resource, detail = "yield", \
                    f"雨损/执行率使采收产出 {produced:.1f}t 低于保底 {need_min:.1f}t"
            elif shipped_min[coop_id] + 1e-9 < min(produced, need_min):
                resource, detail = "route", \
                    f"正常与替补通道共享容量不足，保底 {min_short:.1f}t 运不出"
            else:
                resource, detail = "yield", "采收产出不足以覆盖保底"
            local.append(Conflict(week, coop_id, resource, detail, min_short))
        if waiting[coop_id] > 1e-9:
            local.append(Conflict(
                week, coop_id, "route",
                f"共享通道余量不足，{waiting[coop_id]:.1f}t 可选/超产料滞留产地",
                waiting[coop_id]))
        conflicts.extend(local)
        cr = bundle.coop_routes[coop_id]
        used_routes = {r: round(v, 6) for r, v in coop_route_used[coop_id].items()}
        deliveries[coop_id] = CoopDelivery(
            coop_id=coop_id,
            requested_min_t=need_min,
            requested_option_t=coop_option_extra[coop_id],
            delivered_t=delivered,
            min_shortfall_t=min_short,
            option_delivered_t=option_delivered,
            route_used=used_routes,
            conflicts=local,
        )

    # ---- 第四步：入共享库容；超额拒收记 storage 冲突 ----
    storage_rejected = 0.0
    for coop_id, lots in lots_by_coop.items():
        produced = sum(lot[1] for lot in lots)
        delivered_t = deliveries[coop_id].delivered_t
        scale = delivered_t / produced if produced > 1e-9 else 0.0
        for sid, actual, moisture, _plan, _ratio in lots:
            arrived = actual * scale
            accepted = house.intake(sid, arrived, moisture)
            storage_rejected += arrived - accepted
            if arrived - accepted > 1e-9:
                conflicts.append(Conflict(
                    week, coop_id, "storage",
                    f"共享库容 {house.capacity_t:.0f}t 不足，拒收 {arrived - accepted:.2f}t",
                    arrived - accepted))

    # ---- 锅炉取料与缺供 ----
    heat, _taken_wet = house.withdraw_heat(demand_heat)
    shortfall = max(0.0, demand_heat - heat)
    emergency = shortfall  # 应急煤按缺供热量一对一替补
    return WeekResult(
        week=week,
        weather=weather,
        planned_t=planned_total,
        harvested_wet_t=harvested_total,
        route_rejected_t=route_rejected,
        storage_rejected_t=storage_rejected,
        storage_loss_t=storage_loss,
        opening_stock_t=opening_stock,
        closing_stock_t=house.stock_wet_t(),
        delivered_heat_mj=heat,
        demand_heat_mj=demand_heat,
        heat_shortfall_mj=shortfall,
        emergency_coal_mj=emergency,
        emergency_cost_yuan=emergency * emergency_cost,
        coop_deliveries=deliveries,
        route_usage=pool.usage,
        route_capacity=capacity,
        conflicts=conflicts,
        harvest_log=harvest_log,
    )


def simulate_scenario(
    bundle: DataBundle,
    cal: Calibration,
    path: ScenarioPath,
    commitments: dict[tuple[int, str], WeekCommitment],
    cap_view: dict[tuple[int, str], tuple[float, str]],
    floor_spend: float,
    first_week: int = COMMIT_FIRST,
    last_week: int = COMMIT_LAST,
) -> ScenarioResult:
    rng = np.random.default_rng(path.seed * 7919 + 17)
    house = Warehouse.from_opening(bundle.storage_rules, bundle.sources, bundle.opening_stock)
    # sources.last_harvest_week 是相对承诺首周（104）的偏移，需换算为绝对周索引
    last_harvest = {sid: COMMIT_FIRST + src.last_harvest_week
                    for sid, src in bundle.sources.items()}
    results: dict[int, WeekResult] = {}
    for week in range(first_week, last_week + 1):
        demand = bundle.demand[week]
        results[week] = simulate_week(
            week, bundle, cal, path, rng, house, commitments, cap_view,
            last_harvest, demand.heat_mj, demand.emergency_cost)
    return ScenarioResult(
        index=path.index,
        seed=path.seed,
        weeks=results,
        total_floor_spend=floor_spend,
        total_emergency_cost=sum(w.emergency_cost_yuan for w in results.values()),
        total_min_shortfall_t=sum(
            d.min_shortfall_t for w in results.values() for d in w.coop_deliveries.values()),
        total_storage_loss_t=sum(w.storage_loss_t for w in results.values()),
    )


def as_of_default() -> dt.datetime:
    """承诺评审基准时点：承诺期额定能力预登记时刻。"""
    return dt.datetime(2026, 12, 28, 8, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
