"""滚动历史检验与归因分解。

原则：
- 每个评审时点只使用 available_at 不晚于该时点的资料，禁止使用事后实况；
- 用当时模型对后续窗口做概率预测，输出预测雨占比、缺供概率与期望应急费用；
- 归因通过四个确定性回放实现，各只改一类输入（数量口径、质量口径、通道口径），
  在同一基准上分离来料数量、质量折算、相关中断各自的热量影响。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import numpy as np

from .analysis import build_report
from .contracts import build_commitment_index
from .dataio import (
    COMMIT_FIRST,
    COMMIT_LAST,
    WEEK0_MONDAY,
    DataBundle,
    route_capacity_view,
)
from .engine import simulate_scenario
from .history import Calibration, calibrate
from .scenarios import make_paths
from .storage import Warehouse


@dataclass
class ReplayWeek:
    week: int
    weather: str
    actual_harvest_t: float
    delivered_heat_mj: float
    demand_heat_mj: float
    shortfall_mj: float
    storage_loss_t: float
    produced_heat_mj: float = 0.0
    rejected_t: float = 0.0
    conflicts: list = field(default_factory=list)


@dataclass
class ReplayResult:
    label: str
    weeks: dict[int, ReplayWeek]
    total_shortfall_mj: float
    produced_heat_mj: float = 0.0
    outage_rejected_t: float = 0.0

    @property
    def total_heat(self) -> float:
        return sum(w.delivered_heat_mj for w in self.weeks.values())


def replay_history(
    bundle: DataBundle,
    first: int,
    last: int,
    mode: str,
    expected_moisture: dict[str, float],
    rated_routes: bool = False,
    benchmark_demand_mj: float | None = None,
    fullfillment: str = "actual",
) -> ReplayResult:
    """确定性回放历史窗口。

    mode 控制质量口径：
    - actual：含水率按真实观测；
    - expected_quality：含水率换成该天气的期望。
    fullfillment 控制数量口径：
    - actual：按实际执行量；
    - planned：按计划量满产交付（剥离执行不足，作为数量效应基准）。
    rated_routes=True 时把历史停运恢复为额定，剥离相关中断影响。

    历史窗口没有逐周需求真值，benchmark_demand_mj 给出统一基准需求；
    不传则只累计可交付热量能力、不计算缺口。历史无逐周库存真值，从空仓起算。
    """
    house = Warehouse(bundle.storage_rules, bundle.sources)
    last_harvest = {sid: 104 + src.last_harvest_week for sid, src in bundle.sources.items()}
    obs_index = {(o.week, o.source_id): o for o in bundle.weekly}
    out: dict[int, ReplayWeek] = {}

    for week in range(first, last + 1):
        loss = house.age_and_loss()
        week_obs = [o for o in bundle.weekly if o.week == week]
        weather = week_obs[0].weather
        week_lots: list[tuple[str, float, float]] = []
        # 采收：无计划周跳过（本来无计划，不是零产能）
        for sid in sorted(bundle.sources):
            src = bundle.sources[sid]
            o = obs_index.get((week, sid))
            if o is None or not o.planned_wet_t or o.planned_wet_t <= 0:
                continue
            base_qty = (o.planned_wet_t if fullfillment == "planned"
                        else (o.actual_wet_t or 0.0))
            qty = min(base_qty, bundle.coop_routes[src.coop_id].processing_t)
            moisture = (
                expected_moisture[weather] if mode == "expected_quality"
                else (o.moisture_pct or expected_moisture[weather])
            )
            last_harvest[sid] = week
            week_lots.append((sid, qty, moisture))

        # 路线：三种模式统一按正常→替补、共享通道容量建模；
        # rated_routes=True 时把历史停运容量恢复为额定，剥离相关中断影响。
        pool_cap: dict[str, float] = {}
        for cr0 in bundle.coop_routes.values():
            for route in (cr0.normal, cr0.backup):
                rated_value = 65.0 if route == "BRIDGE" else 110.0
                if rated_routes:
                    pool_cap[route] = rated_value
                else:
                    cap_row = next((c.capacity_t for c in bundle.route_caps
                                    if c.week == week and c.route_id == route), rated_value)
                    pool_cap[route] = cap_row
        remaining_cap = dict(pool_cap)
        delivered_lots: list[tuple[str, float, float]] = []
        rejected = 0.0
        for coop_id in sorted(bundle.coop_routes):
            cr0 = bundle.coop_routes[coop_id]
            coop_lots = [lot for lot in week_lots
                         if bundle.sources[lot[0]].coop_id == coop_id]
            tons = sum(lot[1] for lot in coop_lots)
            for route in (cr0.normal, cr0.backup):
                if tons <= 1e-9:
                    break
                moved = min(tons, remaining_cap.get(route, 0.0))
                remaining_cap[route] -= moved
                tons -= moved
            rejected += tons
            # 按运达比例回摊到批次
            produced = sum(lot[1] for lot in coop_lots)
            scale = (produced - tons) / produced if produced > 1e-9 else 0.0
            for sid, actual, moisture in coop_lots:
                delivered_lots.append((sid, actual * scale, moisture))
        for sid, actual, moisture in delivered_lots:
            house.intake(sid, actual, moisture)

        # 实际入仓批次的可用热量（干净的质量折算口径，不受取料时序影响）
        produced_heat = sum(
            actual * (1.0 - moisture / 100.0) * bundle.sources[sid].dry_mj_per_t
            for sid, actual, moisture in delivered_lots)

        need = benchmark_demand_mj if benchmark_demand_mj is not None else 0.0
        heat, _ = house.withdraw_heat(need)
        short = max(0.0, need - heat) if benchmark_demand_mj is not None else 0.0
        out[week] = ReplayWeek(
            week=week, weather=weather,
            actual_harvest_t=sum(
                (o.actual_wet_t or 0.0) for o in week_obs
                if o.planned_wet_t and o.planned_wet_t > 0) - rejected,
            delivered_heat_mj=heat, demand_heat_mj=need,
            shortfall_mj=short, storage_loss_t=loss,
            produced_heat_mj=produced_heat, rejected_t=rejected)
    return ReplayResult(
        label=mode, weeks=out,
        total_shortfall_mj=sum(w.shortfall_mj for w in out.values()),
        produced_heat_mj=sum(w.produced_heat_mj for w in out.values()),
        outage_rejected_t=sum(w.rejected_t for w in out.values()))


def attribution(bundle: DataBundle, first: int, last: int, cal: Calibration,
                benchmark_demand_mj: float | None = None) -> dict[str, float]:
    """三类偏差归因，四个确定性回放各只改一类输入，在同一口径上分离。

    效应为"该因素使可交付热量减少的兆焦数"，正值=造成损失，负值=净改善：
    - 数量效应 = 满产实况质量 − 全实况（执行不足造成的热量损失，恒非负）；
    - 质量效应 = 满产期望质量 − 满产实况质量（实际含水率高于期望为正损失）；
    - 相关中断效应 = 额定通道满产 − 真实通道满产（停运造成的热量损失，恒非负）。
    """
    expected = {"dry": cal.moisture_dry_mean, "rain": cal.moisture_rain_mean}
    kw = {"benchmark_demand_mj": benchmark_demand_mj}
    actual = replay_history(bundle, first, last, "actual", expected, **kw)
    full_actual_q = replay_history(
        bundle, first, last, "actual", expected, fullfillment="planned", **kw)
    full_expected_q = replay_history(
        bundle, first, last, "expected_quality", expected,
        fullfillment="planned", **kw)
    full_no_outage = replay_history(
        bundle, first, last, "actual", expected,
        rated_routes=True, fullfillment="planned", **kw)

    quantity_effect = full_actual_q.produced_heat_mj - actual.produced_heat_mj
    quality_effect = full_expected_q.produced_heat_mj - full_actual_q.produced_heat_mj
    outage_effect = full_no_outage.produced_heat_mj - full_actual_q.produced_heat_mj
    return {
        "actual_total_shortfall_mj": actual.total_shortfall_mj,
        "quantity_effect_mj": quantity_effect,
        "quality_effect_mj": quality_effect,
        "correlated_outage_effect_mj": outage_effect,
        "quality_effect_note": "正值=实际含水率高于条件期望造成的热量损失；负值为净改善",
        "actual_produced_heat_mj": actual.produced_heat_mj,
        "full_actual_quality_heat_mj": full_actual_q.produced_heat_mj,
        "full_expected_quality_heat_mj": full_expected_q.produced_heat_mj,
        "full_no_outage_heat_mj": full_no_outage.produced_heat_mj,
        "outage_rejected_wet_t": actual.outage_rejected_t,
    }


def rolling_reliability(
    bundle: DataBundle,
    review_weeks: list[int],
    horizon: int,
    n_paths: int,
    seed: int,
) -> list[dict]:
    """签约后滚动重估：在每个评审周只用当时可知信息预测后续 horizon 周。

    评审周取承诺期内周索引（104 起）。每个时点：
    - 标定只用历史窗口（weekly 周0-103 全程可知，承诺期实际天气尚不可知，绝不偷看）；
    - 容量视图只纳入 available_at 不晚于评审时点的预登记与通知；
    - 初始天气取历史末周（周103）实况，之后按 Markov 转移生成；
    - 输出预测雨周占比、预测缺供概率、期望应急煤费用，供与后续真实到货对照。
    """
    # 历史窗口对所有承诺期评审点都已完全可知，标定一次复用
    cal = calibrate(bundle)
    init_weather = next(
        o.weather for o in bundle.weekly if o.week == max(o2.week for o2 in bundle.weekly))
    rows = []
    for review in review_weeks:
        first = max(COMMIT_FIRST, review + 1)
        last = min(COMMIT_LAST, review + horizon)
        if last < first:
            continue
        # 评审时点：该周周一 08:00（资料发布均在工作日早晨）
        as_of = WEEK0_MONDAY + _dt.timedelta(days=7 * review, hours=8)
        cap_view = route_capacity_view(bundle.route_caps, bundle.notices, as_of)
        applied_notices = sorted(
            n.notice_id for n in bundle.notices if n.available_at <= as_of)
        paths = make_paths(cal, seed, n_paths, first, last, init_weather)
        commits = build_commitment_index(
            [c for c in bundle.contracts if first <= c.week <= last])
        results = [simulate_scenario(bundle, cal, p, commits, cap_view, 0.0, first, last)
                   for p in paths]
        report = build_report(
            results, {w: bundle.demand[w].risk_limit for w in range(first, last + 1)})
        rows.append({
            "review_week": review,
            "as_of": as_of.isoformat(),
            "window": [first, last],
            "applied_notices": applied_notices,
            "predicted_rain_share": float(np.mean([
                sum(1 for w in p.weeks.values() if w.weather == "rain") / (last - first + 1)
                for p in paths])),
            "predicted_mean_shortfall_prob": float(
                np.mean([w.shortfall_prob for w in report.weeks])),
            "weeks_outside_limit": report.weeks_outside_limit,
            "expected_emergency_cost": report.expected_emergency_cost,
        })
    return rows
