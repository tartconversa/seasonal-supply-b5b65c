"""情景随机过程：共同天气、条件停运、执行率与含水率的经验生成。

所有随机数来自单一 numpy Generator，种子固定后整条路径可重现。
南北区在历史窗口天气完全同步，因此每条情景只有一条共同天气路径，
所有合作社的雨损、高含水率和 BRIDGE 停运在同一周同时发生——这是相关性的来源，
不是把独立供应商简单相加。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .dataio import COMMIT_FIRST, COMMIT_LAST
from .history import Calibration


@dataclass
class WeekScenario:
    week: int
    weather: str
    bridge_capacity_t: float | None  # None 表示该周无 BRIDGE 可知/适用事件，按额定处理
    bridge_outage: bool


@dataclass
class ScenarioPath:
    index: int
    seed: int
    weeks: dict[int, WeekScenario]

    def weather(self, week: int) -> str:
        return self.weeks[week].weather


def _bootstrap_residual(rng: np.random.Generator, sample: tuple[float, ...]) -> float:
    """经验自助：直接从历史样本抽取值，保留非正态形状。"""
    return float(sample[int(rng.integers(len(sample)))])


def draw_weather_path(
    cal: Calibration,
    rng: np.random.Generator,
    first_week: int = COMMIT_FIRST,
    last_week: int = COMMIT_LAST,
    initial_weather: str = "dry",
) -> dict[int, WeekScenario]:
    """生成共同天气路径与雨周 BRIDGE 条件停运。

    历史最后一周（103）是雨，但承诺评审只能使用转移概率而非未来实况；
    initial_weather 显式给出，默认按旱季稳态起算，调用方可用历史末周覆盖。
    """
    out: dict[int, WeekScenario] = {}
    weather = initial_weather
    for week in range(first_week, last_week + 1):
        if weather == "dry":
            rain = rng.random() < cal.trans_dry_to_rain
        else:
            rain = rng.random() < cal.trans_rain_to_rain
        weather = "rain" if rain else "dry"
        outage = False
        bridge_cap: float | None = None
        if rain:
            # 历史雨周 BRIDGE 以经验条件概率完全停运，否则维持额定 65
            outage = rng.random() < cal.bridge_outage_given_rain
            if outage:
                bridge_cap = 0.0
        out[week] = WeekScenario(week=week, weather=weather, bridge_capacity_t=bridge_cap, bridge_outage=outage)
    return out


def draw_execution_ratio(cal: Calibration, weather: str, rng: np.random.Generator) -> float:
    """有计划采收的执行率（实际/计划），经验残差自助，不允许负值。"""
    _, _, sample = cal.exec_params(weather)
    return max(0.0, _bootstrap_residual(rng, sample))


def draw_moisture(cal: Calibration, weather: str, rng: np.random.Generator) -> float:
    """来料含水率（质量百分数），按天气状态的正态波动生成。"""
    mean, std = cal.moisture_params(weather)
    return float(np.clip(rng.normal(mean, std), 0.0, 100.0))


def draw_plan_size(cal: Calibration, rng: np.random.Generator) -> float:
    """单地块单周计划湿吨，截断正态，正值。"""
    value = rng.normal(cal.plan_mean, cal.plan_std)
    return max(1.0, float(value))


def make_paths(
    cal: Calibration,
    base_seed: int,
    n: int,
    first_week: int = COMMIT_FIRST,
    last_week: int = COMMIT_LAST,
    initial_weather: str = "dry",
) -> list[ScenarioPath]:
    paths: list[ScenarioPath] = []
    for i in range(n):
        seed = base_seed + i
        rng = np.random.default_rng(seed)
        weeks = draw_weather_path(cal, rng, first_week, last_week, initial_weather)
        paths.append(ScenarioPath(index=i, seed=seed, weeks=weeks))
    return paths
