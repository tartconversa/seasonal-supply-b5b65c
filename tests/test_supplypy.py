"""supplypy 核心口径单元测试。

覆盖：资料载入与可知时点、季节/轮伐资格、分龄库存与库容、
共同天气相关性、保底不可撤销与共享通道不重复计数、风险界限与冲突定位、
滚动重估不偷看未来。运行：python3 -I -m unittest 或 discover。
"""
from pathlib import Path
import datetime as dt
import unittest

import numpy as np

from supplypy.dataio import (
    COMMIT_FIRST,
    load_bundle,
    parse_stamp,
    route_capacity_view,
)
from supplypy.calendar import harvest_open, eligible_sources, week_month
from supplypy.history import calibrate, planned_obs, no_plan_obs
from supplypy.storage import Warehouse, Cohort
from supplypy.scenarios import make_paths
from supplypy.contracts import build_commitment_index, total_floor_spend
from supplypy.engine import simulate_scenario, as_of_default
from supplypy.analysis import build_report
from supplypy import backtest

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)

    def test_loads_all_tables(self):
        b = self.bundle
        self.assertEqual(len(b.sources), 8)
        self.assertEqual(len(b.coop_routes), 4)
        self.assertEqual(len(b.demand), 52)
        self.assertTrue(all(s.dry_mj_per_t > 0 for s in b.sources.values()))

    def test_as_of_filters_future(self):
        # NT1 在 2027-01-05 发布，影响 W106 BRIDGE
        before = parse_stamp("2027-01-04T08:00:00+08:00")
        after = parse_stamp("2027-01-06T08:00:00+08:00")
        view0 = route_capacity_view(self.bundle.route_caps, self.bundle.notices, before)
        view1 = route_capacity_view(self.bundle.route_caps, self.bundle.notices, after)
        # 发布前 W106 BRIDGE 只有额定 65；发布后被通知覆盖为 0
        self.assertEqual(view0[(106, "BRIDGE")][0], 65.0)
        self.assertEqual(view1[(106, "BRIDGE")][0], 0.0)
        self.assertTrue(view1[(106, "BRIDGE")][1].startswith("notice:NT1"))

    def test_week_month_boundary(self):
        self.assertEqual(week_month(104), 1)   # 2027-01-04
        self.assertEqual(week_month(117), 4)   # 进入四月
        self.assertEqual(week_month(121), 5)   # 进入五月关闭段
        self.assertEqual(week_month(139), 9)   # 九月恢复


class EligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)

    def test_rotation_offset_semantics(self):
        # last_harvest_week 是相对 W104 的偏移；P8(-8)=绝对W96，W104 间隔8 可采
        last = {sid: COMMIT_FIRST + src.last_harvest_week
                for sid, src in self.bundle.sources.items()}
        eligible104 = eligible_sources(self.bundle.sources, 104, last)
        self.assertIn("P8", eligible104)
        self.assertNotIn("P1", eligible104)  # P1 最后采于 W103，间隔不足8

    def test_closed_season_no_source(self):
        b = self.bundle
        last = {sid: COMMIT_FIRST + s.last_harvest_week for sid, s in b.sources.items()}
        # W121 落在五月，全部地块 harvest_months 不含 5-9
        self.assertEqual(eligible_sources(b.sources, 121, last), [])
        # W139 虽进九月，但轮伐间隔未到；W143 起季节与轮伐同时满足
        self.assertEqual(eligible_sources(b.sources, 139, last), [])
        self.assertTrue(eligible_sources(b.sources, 143, last))

    def test_harvest_month_rule(self):
        src = next(iter(self.bundle.sources.values()))
        self.assertTrue(harvest_open(src, 104))   # 一月
        self.assertFalse(harvest_open(src, 125))  # 六月


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)
        cls.cal = calibrate(cls.bundle)

    def test_no_plan_distinct_from_failure(self):
        # 历史上无计划周远多于有计划周；二者必须可区分
        planned = planned_obs(self.bundle.weekly)
        no_plan = no_plan_obs(self.bundle.weekly)
        self.assertTrue(len(no_plan) > len(planned))
        # 所有有计划观测都有 actual（即便为0也是失败样本）
        self.assertTrue(all(o.actual_wet_t is not None for o in planned))

    def test_weather_is_common_and_rain_penalizes(self):
        self.assertAlmostEqual(self.cal.rain_freq, 23 / 104, places=6)
        self.assertLess(self.cal.exec_rain_mean, self.cal.exec_dry_mean)
        self.assertGreater(self.cal.moisture_rain_mean, self.cal.moisture_dry_mean)

    def test_bridge_outage_only_in_rain(self):
        # 标定的停运条件概率来自 10 个雨周停运 / 23 个雨周
        self.assertAlmostEqual(self.cal.bridge_outage_given_rain, 10 / 23, places=6)


class StorageTests(unittest.TestCase):
    def setUp(self):
        b = load_bundle(ROOT)
        self.house = Warehouse.from_opening(
            b.storage_rules, b.sources, b.opening_stock)

    def test_capacity_and_loss(self):
        self.assertEqual(self.house.capacity_t, 800.0)
        opening = self.house.stock_wet_t()
        self.assertAlmostEqual(opening, 65.0)
        lost = self.house.age_and_loss()
        self.assertGreater(lost, 0.0)
        self.assertLess(self.house.stock_wet_t(), opening)

    def test_intake_respects_capacity(self):
        accepted = self.house.intake("P1", 1000.0, 22.0)
        self.assertLessEqual(self.house.stock_wet_t(), 800.0 + 1e-6)
        self.assertLess(accepted, 1000.0)  # 超额拒收，不凭空生成

    def test_fifo_withdraw_and_heat_conversion(self):
        b = load_bundle(ROOT)
        house = Warehouse(b.storage_rules, b.sources)
        house.intake("P1", 10.0, 20.0)
        heat_per_t = (1 - 0.20) * b.sources["P1"].dry_mj_per_t
        heat, wet = house.withdraw_heat(5 * heat_per_t)
        self.assertAlmostEqual(wet, 5.0, places=4)
        self.assertAlmostEqual(heat, 5 * heat_per_t, places=2)


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)
        cls.cal = calibrate(cls.bundle)

    def test_paths_reproducible_and_correlated(self):
        p1 = make_paths(self.cal, 7, 5, 104, 155, "dry")
        p2 = make_paths(self.cal, 7, 5, 104, 155, "dry")
        self.assertEqual(
            [p.weeks[w].weather for p in p1 for w in (104, 120)],
            [p.weeks[w].weather for p in p2 for w in (104, 120)])
        # 同一条路径内天气是单一共同过程（南北区共享），停运只在雨周发生
        for p in p1:
            for ws in p.weeks.values():
                if ws.bridge_outage:
                    self.assertEqual(ws.weather, "rain")


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)
        cls.cal = calibrate(cls.bundle)

    def _run(self, n=200, include_proposals=True, as_of=None):
        b = self.bundle
        paths = make_paths(self.cal, 11, n, 104, 155, "dry")
        commits = build_commitment_index(b.contracts_of("base"), include_proposals)
        cap = route_capacity_view(b.route_caps, b.notices, as_of or as_of_default())
        floor = total_floor_spend(commits)
        return [simulate_scenario(b, self.cal, p, commits, cap, floor) for p in paths], floor

    def test_floor_spend_irrevocable(self):
        results, floor = self._run(50)
        # 保底支出固定，不随情景变化
        self.assertTrue(all(r.total_floor_spend == floor for r in results))
        self.assertGreater(floor, 0)

    def test_shared_route_not_double_counted(self):
        results, _ = self._run(30)
        for r in results:
            for w in r.weeks.values():
                for route, used in w.route_usage.items():
                    self.assertLessEqual(used, w.route_capacity[route] + 1e-6)

    def test_storage_never_exceeds_capacity(self):
        results, _ = self._run(30)
        for r in results:
            for w in r.weeks.values():
                self.assertLessEqual(w.closing_stock_t, 800.0 + 1e-6)

    def test_closed_season_zero_harvest(self):
        results, _ = self._run(20)
        for r in results:
            for week in range(121, 139):
                self.assertEqual(r.weeks[week].harvested_wet_t, 0.0)

    def test_conflicts_pin_resource_and_week(self):
        results, _ = self._run(40)
        resources = set()
        for r in results:
            for w in r.weeks.values():
                for c in w.conflicts:
                    resources.add(c.resource)
                    self.assertGreaterEqual(c.week, 104)
                    self.assertGreater(c.shortfall_t, 0.0)
        # 至少应出现季节/轮伐与雨损两类根因
        self.assertIn("season_rotation", resources)
        self.assertTrue({"yield", "route"} & resources)

    def test_report_flags_limit_breaches(self):
        results, _ = self._run(200)
        limits = {w: self.bundle.demand[w].risk_limit for w in range(104, 156)}
        report = build_report(results, limits)
        self.assertEqual(report.scenario_count, 200)
        # base 组合物理上限不足，必然有越界周，且不能被全年总量掩盖
        self.assertTrue(report.weeks_outside_limit)
        self.assertTrue(report.shortage_intervals)


class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT)
        cls.cal = calibrate(cls.bundle)

    def test_attribution_decomposes(self):
        bench = float(np.mean([d.heat_mj for d in self.bundle.demand.values()]))
        attr = backtest.attribution(self.bundle, 39, 68, self.cal, bench)
        # 数量效应（雨损执行不足）应显著为正
        self.assertGreater(attr["quantity_effect_mj"], 0.0)
        # 该窗口停运只影响替补通道，实际无停运损失
        self.assertEqual(attr["correlated_outage_effect_mj"], 0.0)

    def test_rolling_does_not_use_future_notices(self):
        rows = backtest.rolling_reliability(self.bundle, [104, 106], 6, 100, 99)
        self.assertEqual(rows[0]["applied_notices"], [])
        self.assertIn("NT1", rows[1]["applied_notices"])
        for row in rows:
            self.assertTrue(0.0 <= row["predicted_rain_share"] <= 1.0)


if __name__ == "__main__":
    unittest.main()
