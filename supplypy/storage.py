"""分龄库存演化。

库存按 (来源, 入库年龄, 含水率) 分龄批次保留：
- 每周先对所有批次计提该年龄的损耗（质量损失比例按 storage 表，超龄按最后一段）；
- 新到货以年龄 0 批次入仓，全部批次湿吨之和受同一库容约束；
- 锅炉取料按先进先出（最老批次优先），避免可缓冲的热量继续劣化；
- 每湿吨可用热量 = (1 - 含水率) × 干基热值，不引入任何隐含转换系数。
"""
from __future__ import annotations

from dataclasses import dataclass

from .dataio import OpeningStock, Source, StorageRule

EPS = 1e-9


@dataclass
class Cohort:
    source_id: str
    age_week: int
    wet_t: float
    moisture_pct: float

    def heat_per_wet_t(self, sources: dict[str, Source]) -> float:
        dry = sources[self.source_id].dry_mj_per_t
        return (1.0 - self.moisture_pct / 100.0) * dry


class Warehouse:
    """按周推进的共享库容。"""

    def __init__(self, rules: list[StorageRule], sources: dict[str, Source]):
        if not rules:
            raise ValueError("storage 规则为空")
        self.rules = sorted(rules, key=lambda r: r.age_week)
        self.capacity_t = rules[0].capacity_t
        if any(r.capacity_t != self.capacity_t for r in rules):
            raise ValueError("库容必须是所有年龄共同上限")
        self.sources = sources
        self.cohorts: list[Cohort] = []
        self.lost_wet_t = 0.0
        self.discarded_wet_t = 0.0

    @classmethod
    def from_opening(
        cls,
        rules: list[StorageRule],
        sources: dict[str, Source],
        opening: list[OpeningStock],
    ) -> "Warehouse":
        house = cls(rules, sources)
        for stock in opening:
            house.cohorts.append(
                Cohort(
                    source_id=stock.source_id,
                    age_week=stock.age_week,
                    wet_t=stock.wet_t,
                    moisture_pct=stock.moisture_pct,
                )
            )
        if house.stock_wet_t() > house.capacity_t + EPS:
            raise ValueError("期初库存已超过共同库容")
        return house

    def _loss(self, age_week: int) -> float:
        if age_week <= self.rules[-1].age_week:
            for rule in self.rules:
                if rule.age_week == age_week:
                    return rule.loss_fraction
        return self.rules[-1].loss_fraction

    def stock_wet_t(self) -> float:
        return sum(c.wet_t for c in self.cohorts)

    def stock_heat_mj(self) -> float:
        return sum(c.wet_t * c.heat_per_wet_t(self.sources) for c in self.cohorts)

    def age_and_loss(self) -> float:
        """周初计提损耗并令全部批次长一周。返回本周损失湿吨。"""
        lost = 0.0
        for cohort in self.cohorts:
            fraction = self._loss(cohort.age_week)
            removed = cohort.wet_t * fraction
            cohort.wet_t -= removed
            lost += removed
            cohort.age_week += 1
        self.cohorts = [c for c in self.cohorts if c.wet_t > EPS]
        self.lost_wet_t += lost
        return lost

    def free_space(self) -> float:
        return max(0.0, self.capacity_t - self.stock_wet_t())

    def intake(self, source_id: str, wet_t: float, moisture_pct: float) -> float:
        """入仓，受共同库容限制；返回实际入库湿吨（超额部分不凭空生成）。"""
        accepted = min(max(wet_t, 0.0), self.free_space())
        if accepted > EPS:
            self.cohorts.append(Cohort(source_id, 0, accepted, moisture_pct))
        self.discarded_wet_t += max(0.0, wet_t - accepted)
        return accepted

    def withdraw_heat(self, heat_need_mj: float) -> tuple[float, float]:
        """按 FIFO 取料满足热量需求，返回 (取用热量, 取用湿吨)。"""
        remaining = heat_need_mj
        taken_heat = 0.0
        taken_wet = 0.0
        for cohort in sorted(self.cohorts, key=lambda c: (-c.age_week, c.source_id)):
            if remaining <= EPS:
                break
            per_t = cohort.heat_per_wet_t(self.sources)
            need_t = remaining / per_t
            used_t = min(need_t, cohort.wet_t)
            cohort.wet_t -= used_t
            taken_wet += used_t
            heat = used_t * per_t
            taken_heat += heat
            remaining -= heat
        self.cohorts = [c for c in self.cohorts if c.wet_t > EPS]
        return taken_heat, taken_wet

    def snapshot(self) -> list[Cohort]:
        return [Cohort(c.source_id, c.age_week, c.wet_t, c.moisture_pct) for c in self.cohorts]
