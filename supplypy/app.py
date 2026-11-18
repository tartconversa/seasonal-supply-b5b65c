"""应用门面：把标定、情景模拟、风险报告、重估、回测编排成可调用流程。"""
from __future__ import annotations

import datetime as dt

import numpy as np

from . import analysis, backtest
from .contracts import build_commitment_index, total_floor_spend
from .dataio import DataBundle, COMMIT_FIRST, COMMIT_LAST, route_capacity_view
from .engine import as_of_default, simulate_scenario
from .history import calibrate
from .scenarios import make_paths


def run_assessment(
    bundle: DataBundle,
    scenario: str,
    n_paths: int = 2000,
    seed: int = 20270104,
    include_proposals: bool = True,
    as_of: dt.datetime | None = None,
    first_week: int = COMMIT_FIRST,
    last_week: int = COMMIT_LAST,
    initial_weather: str = "dry",
):
    """对一套方案执行蒙特卡洛评估，返回 (标定, 情景结果, 风险报告, 参数)。"""
    as_of = as_of or as_of_default()
    cal = calibrate(bundle)
    contracts = [
        c for c in bundle.contracts_of(scenario)
        if first_week <= c.week <= last_week
    ]
    commitments = build_commitment_index(contracts, include_proposals)
    cap_view = route_capacity_view(bundle.route_caps, bundle.notices, as_of)
    paths = make_paths(cal, seed, n_paths, first_week, last_week, initial_weather)
    floor = total_floor_spend(commitments)
    results = [
        simulate_scenario(bundle, cal, p, commitments, cap_view, floor, first_week, last_week)
        for p in paths
    ]
    limits = {w: bundle.demand[w].risk_limit for w in range(first_week, last_week + 1)}
    report = analysis.build_report(results, limits)
    params = {
        "scenario": scenario,
        "n_paths": n_paths,
        "seed": seed,
        "include_proposals": include_proposals,
        "as_of": as_of,
        "first_week": first_week,
        "last_week": last_week,
        "initial_weather": initial_weather,
    }
    return cal, results, report, params


def run_review(
    bundle: DataBundle,
    scenario: str,
    as_of: dt.datetime,
    n_paths: int = 2000,
    seed: int = 20270104,
):
    """签约后重估：只使用 as_of 可知信息，signed 保持约束，proposal 仍可变。"""
    return run_assessment(
        bundle, scenario, n_paths=n_paths, seed=seed,
        include_proposals=True, as_of=as_of)


def run_backtest(bundle: DataBundle, review_weeks: list[int], horizon: int = 8,
                 n_paths: int = 500, seed: int = 424242):
    """历史归因用历史窗口；滚动可靠性在承诺期评审时点上重估。

    历史窗口无逐周需求真值，归因以承诺期平均热量需求作为统一基准。
    """
    cal = calibrate(bundle)
    benchmark = float(np.mean([d.heat_mj for d in bundle.demand.values()]))
    attribution = backtest.attribution(bundle, 39, 68, cal, benchmark)
    rolling = backtest.rolling_reliability(bundle, review_weeks, horizon, n_paths, seed)
    return cal, attribution, rolling
