"""只用历史窗口（周0-103）可知资料标定随机模型。

口径要点：
- 天气是区域共同事件；本资料中南北区天气完全同步，按单一共同天气过程标定，
  绝不把各合作社当作独立供应商事件。
- planned=0 且 actual 为空是"本来无计划"，不进入执行率样本；
  planned>0 才是计划观测，其 actual 即使为 0 也属于执行失败样本。
- BRIDGE 停运只在历史雨周出现，条件概率按雨周经验频率标定。
- 含水率、计划湿吨、执行率使用经验残差自助法，保留真实分布形状。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .dataio import DataBundle, HISTORY_LAST, WeeklyObs


@dataclass
class Calibration:
    rain_freq: float
    trans_dry_to_rain: float
    trans_rain_to_rain: float
    rain_run_lengths: tuple[int, ...]
    bridge_outage_given_rain: float
    bridge_rain_capacity: tuple[float, ...]
    exec_dry_mean: float
    exec_dry_std: float
    exec_rain_mean: float
    exec_rain_std: float
    exec_dry_sample: tuple[float, ...]
    exec_rain_sample: tuple[float, ...]
    moisture_dry_mean: float
    moisture_dry_std: float
    moisture_rain_mean: float
    moisture_rain_std: float
    plan_mean: float
    plan_std: float
    plan_sample: tuple[float, ...]
    history_weeks: tuple[int, ...]
    rain_weeks: tuple[int, ...]
    bridge_zero_weeks: tuple[int, ...]

    def moisture_params(self, weather: str) -> tuple[float, float]:
        if weather == "rain":
            return self.moisture_rain_mean, self.moisture_rain_std
        return self.moisture_dry_mean, self.moisture_dry_std

    def exec_params(self, weather: str) -> tuple[float, float, tuple[float, ...]]:
        if weather == "rain":
            return self.exec_rain_mean, self.exec_rain_std, self.exec_rain_sample
        return self.exec_dry_mean, self.exec_dry_std, self.exec_dry_sample


def _safe_std(values: list[float]) -> float:
    """小样本（不足2个观测）时标准差记0，避免除零污染下游。"""
    return float(np.std(values, ddof=1)) if len(values) >= 2 else 0.0


def calibrate(bundle: DataBundle) -> Calibration:
    weekly = [o for o in bundle.weekly if o.week <= HISTORY_LAST]

    # 共同天气：每周一条区域天气（资料中各区域同步）
    weather_by_week: dict[int, str] = {}
    for obs in weekly:
        prev = weather_by_week.get(obs.week)
        if prev is not None and prev != obs.weather:
            raise ValueError("历史资料出现区域间天气分歧，模型需扩展为多区域过程")
        weather_by_week[obs.week] = obs.weather
    weeks = sorted(weather_by_week)
    seq = [weather_by_week[k] for k in weeks]

    rain_weeks = tuple(k for k in weeks if weather_by_week[k] == "rain")
    dry_to_rain = sum(1 for a, b in zip(seq, seq[1:]) if a == "dry" and b == "rain")
    dry_total = sum(1 for a in seq[:-1] if a == "dry")
    rain_to_rain = sum(1 for a, b in zip(seq, seq[1:]) if a == "rain" and b == "rain")
    rain_total = sum(1 for a in seq[:-1] if a == "rain")

    runs: list[int] = []
    run = 0
    for state in seq:
        if state == "rain":
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)

    # BRIDGE 历史停运
    bridge = [c for c in bundle.route_caps if c.route_id == "BRIDGE" and c.week <= HISTORY_LAST]
    rain_set = set(rain_weeks)
    bridge_rain_caps = tuple(sorted(c.capacity_t for c in bridge if c.week in rain_set))
    bridge_zero = tuple(sorted(c.week for c in bridge if c.capacity_t == 0.0))
    if any(c.week not in rain_set for c in bridge if c.capacity_t == 0.0):
        raise ValueError("BRIDGE 在非雨周停运，条件停运假设不成立")
    bridge_outage_given_rain = len(bridge_zero) / len(rain_set)

    # 执行率：只取有计划的观测
    planned = [o for o in weekly if o.planned_wet_t is not None and o.planned_wet_t > 0]
    for o in planned:
        if o.actual_wet_t is None:
            raise ValueError(f"有计划却缺执行观测：{o.record_id}")
    ratios: dict[str, list[float]] = {"dry": [], "rain": []}
    plan_sizes: list[float] = []
    for o in planned:
        ratios[o.weather].append(o.actual_wet_t / o.planned_wet_t)  # type: ignore[operator]
        plan_sizes.append(o.planned_wet_t)  # type: ignore[arg-type]

    # 含水率：全部历史观测（无计划周也有来料质量读数）
    moisture: dict[str, list[float]] = {"dry": [], "rain": []}
    for o in weekly:
        if o.moisture_pct is not None:
            moisture[o.weather].append(o.moisture_pct)

    return Calibration(
        rain_freq=len(rain_set) / len(weeks),
        trans_dry_to_rain=dry_to_rain / dry_total if dry_total else 0.0,
        trans_rain_to_rain=rain_to_rain / rain_total if rain_total else 0.0,
        rain_run_lengths=tuple(runs),
        bridge_outage_given_rain=bridge_outage_given_rain,
        bridge_rain_capacity=bridge_rain_caps,
        exec_dry_mean=float(np.mean(ratios["dry"])) if ratios["dry"] else 1.0,
        exec_dry_std=_safe_std(ratios["dry"]),
        exec_rain_mean=float(np.mean(ratios["rain"])) if ratios["rain"] else 1.0,
        exec_rain_std=_safe_std(ratios["rain"]),
        exec_dry_sample=tuple(ratios["dry"]) or (1.0,),
        exec_rain_sample=tuple(ratios["rain"]) or (1.0,),
        moisture_dry_mean=float(np.mean(moisture["dry"])) if moisture["dry"] else 0.0,
        moisture_dry_std=_safe_std(moisture["dry"]),
        moisture_rain_mean=float(np.mean(moisture["rain"])) if moisture["rain"] else 0.0,
        moisture_rain_std=_safe_std(moisture["rain"]),
        plan_mean=float(np.mean(plan_sizes)) if plan_sizes else 65.0,
        plan_std=_safe_std(plan_sizes),
        plan_sample=tuple(sorted(plan_sizes)) or (65.0,),
        history_weeks=tuple(weeks),
        rain_weeks=rain_weeks,
        bridge_zero_weeks=bridge_zero,
    )


def planned_obs(weekly: list[WeeklyObs]) -> list[WeeklyObs]:
    """有计划的执行观测（区别于本来无计划）。"""
    return [o for o in weekly if o.planned_wet_t is not None and o.planned_wet_t > 0]


def no_plan_obs(weekly: list[WeeklyObs]) -> list[WeeklyObs]:
    """本来无计划的周：计划量为0，实际为空属正常，不是零产能证据。"""
    return [o for o in weekly if o.planned_wet_t == 0]
