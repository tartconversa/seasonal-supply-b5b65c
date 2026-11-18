"""蒙特卡洛结果汇总：联合分布、连续缺供、风险界限与冲突定位。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .engine import ScenarioResult


@dataclass
class WeekRisk:
    week: int
    demand_heat_mj: float
    mean_heat_mj: float
    p05_heat_mj: float
    p50_heat_mj: float
    p95_heat_mj: float
    shortfall_prob: float
    risk_limit: float
    within_limit: bool
    mean_shortfall_mj: float
    cvar05_shortfall_mj: float  # 最差5%情景的平均缺供
    mean_closing_stock_t: float
    rain_prob: float


@dataclass
class ShortageInterval:
    start_week: int
    end_week: int
    length_weeks: int
    occurrence_prob: float
    mean_total_shortfall_mj: float


@dataclass
class ConflictSummary:
    week: int
    coop_id: str
    resource: str
    occurrences: int
    prob: float
    mean_shortfall_t: float
    sample_detail: str


@dataclass
class RiskReport:
    scenario_count: int
    weeks: list[WeekRisk]
    shortage_intervals: list[ShortageInterval]
    conflicts: list[ConflictSummary]
    expected_emergency_cost: float
    p95_emergency_cost: float
    expected_min_shortfall_t: float
    floor_spend: float
    expected_total_cost: float
    p95_total_cost: float
    any_breach_prob: float
    weeks_outside_limit: list[int]


def _percentile(x: np.ndarray, q: float) -> float:
    return float(np.percentile(x, q))


def build_report(
    results: list[ScenarioResult],
    risk_limits: dict[int, float],
) -> RiskReport:
    if not results:
        raise ValueError("情景结果为空")
    n = len(results)
    weeks = sorted(results[0].weeks)
    week_risks: list[WeekRisk] = []
    outside: list[int] = []

    for week in weeks:
        heat = np.array([r.weeks[week].delivered_heat_mj for r in results])
        demand = np.array([r.weeks[week].demand_heat_mj for r in results])
        short = np.maximum(demand - heat, 0.0)
        stock = np.array([r.weeks[week].closing_stock_t for r in results])
        rain = np.mean([1.0 if r.weeks[week].weather == "rain" else 0.0 for r in results])
        shortfall_prob = float(np.mean(short > 1e-6))
        limit = risk_limits[week]
        if shortfall_prob > limit + 1e-12:
            outside.append(week)
        worst = np.sort(short)[max(1, int(np.ceil(0.05 * n))):]  # noqa: F841
        cvar = float(np.mean(np.sort(short)[-max(1, int(np.ceil(0.05 * n))):]))
        week_risks.append(WeekRisk(
            week=week,
            demand_heat_mj=float(demand[0]),
            mean_heat_mj=float(heat.mean()),
            p05_heat_mj=_percentile(heat, 5),
            p50_heat_mj=_percentile(heat, 50),
            p95_heat_mj=_percentile(heat, 95),
            shortfall_prob=shortfall_prob,
            risk_limit=limit,
            within_limit=shortfall_prob <= limit + 1e-12,
            mean_shortfall_mj=float(short.mean()),
            cvar05_shortfall_mj=cvar,
            mean_closing_stock_t=float(stock.mean()),
            rain_prob=float(rain),
        ))

    # 连续缺供区间：对每条情景找相邻缺供周，再按 (起,止) 汇总发生频率
    interval_stats: dict[tuple[int, int], list[float]] = {}
    for r in results:
        flags = {w: r.weeks[w].heat_shortfall_mj > 1e-6 for w in weeks}
        i = 0
        while i < len(weeks):
            if flags[weeks[i]]:
                j = i
                total = 0.0
                while j < len(weeks) and flags[weeks[j]]:
                    total += r.weeks[weeks[j]].heat_shortfall_mj
                    j += 1
                key = (weeks[i], weeks[j - 1])
                interval_stats.setdefault(key, []).append(total)
                i = j
            else:
                i += 1
    intervals = [
        ShortageInterval(
            start_week=a, end_week=b, length_weeks=b - a + 1,
            occurrence_prob=len(vals) / n,
            mean_total_shortfall_mj=float(np.mean(vals)),
        )
        for (a, b), vals in sorted(interval_stats.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    # 冲突定位：按 (周, 合作社, 资源) 聚合
    bucket: dict[tuple[int, str, str], list] = {}
    for r in results:
        for w in weeks:
            for c in r.weeks[w].conflicts:
                bucket.setdefault((w, c.coop_id, c.resource), []).append(c)
    conflicts = [
        ConflictSummary(
            week=w, coop_id=coop, resource=res,
            occurrences=len(items), prob=len(items) / n,
            mean_shortfall_t=float(np.mean([i.shortfall_t for i in items])),
            sample_detail=items[0].detail,
        )
        for (w, coop, res), items in sorted(bucket.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    emergency = np.array([r.total_emergency_cost for r in results])
    min_short = np.array([r.total_min_shortfall_t for r in results])
    floor = results[0].total_floor_spend
    total_cost = emergency + floor
    any_breach = np.mean([
        1.0 if any(d.min_shortfall_t > 1e-6 for w in weeks for d in r.weeks[w].coop_deliveries.values())
        else 0.0 for r in results
    ])
    return RiskReport(
        scenario_count=n,
        weeks=week_risks,
        shortage_intervals=intervals,
        conflicts=conflicts,
        expected_emergency_cost=float(emergency.mean()),
        p95_emergency_cost=_percentile(emergency, 95),
        expected_min_shortfall_t=float(min_short.mean()),
        floor_spend=floor,
        expected_total_cost=float(total_cost.mean()),
        p95_total_cost=_percentile(total_cost, 95),
        any_breach_prob=float(any_breach),
        weeks_outside_limit=outside,
    )
