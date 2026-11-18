"""采购承诺的约束解释与费用。

口径（docs/资料口径.md）：
- min_t 是不可撤销保底量：坏情景中也不能取消，按 take-or-pay 计费，
  卖方无法足额交付时记为承诺违约（物理断供），买方仍承担保底支出义务。
- option_t 是可选上限：买方可在有物理来料时加购，不产生义务支出。
- confirmed_t 已履行：不能再次购买，也不能撤销；重估时从剩余义务中扣除。
- status=signed 不可撤回；proposal 仍可变，风险评估可分别给出。
"""
from __future__ import annotations

from dataclasses import dataclass

from .dataio import Contract


@dataclass
class WeekCommitment:
    week: int
    coop_id: str
    min_t: float
    option_t: float
    confirmed_t: float
    price: float
    signed: bool
    contract_ids: tuple[str, ...]

    @property
    def open_min_t(self) -> float:
        """仍有约束力的保底（扣除已履行）。"""
        return max(0.0, self.min_t - self.confirmed_t)

    @property
    def open_option_cap_t(self) -> float:
        """仍可加购的上限（扣除已履行）。"""
        return max(0.0, self.min_t + self.option_t - self.confirmed_t)

    @property
    def floor_spend(self) -> float:
        """take-or-pay 保底支出：已履行按实，其余按保底义务。"""
        return self.min_t * self.price


def build_commitment_index(
    contracts: list[Contract],
    include_proposals: bool = True,
) -> dict[tuple[int, str], WeekCommitment]:
    """按 (周, 合作社) 聚合承诺。proposal 可被排除以比较签约前后风险。"""
    index: dict[tuple[int, str], list[Contract]] = {}
    for c in contracts:
        if c.status == "proposal" and not include_proposals:
            continue
        index.setdefault((c.week, c.coop_id), []).append(c)

    result: dict[tuple[int, str], WeekCommitment] = {}
    for key, rows in index.items():
        prices = {r.price_yuan_t for r in rows}
        if len(prices) > 1:
            raise ValueError(f"同一周同一合作社出现多个价格：{key}")
        result[key] = WeekCommitment(
            week=key[0],
            coop_id=key[1],
            min_t=sum(r.min_t for r in rows),
            option_t=sum(r.option_t for r in rows),
            confirmed_t=sum(r.confirmed_t for r in rows),
            price=next(iter(prices)),
            signed=any(r.status == "signed" for r in rows),
            contract_ids=tuple(r.contract_id for r in rows),
        )
    return result


def total_floor_spend(commitments: dict[tuple[int, str], WeekCommitment]) -> float:
    return sum(c.floor_spend for c in commitments.values())


def min_demand_by_week(
    commitments: dict[tuple[int, str], WeekCommitment],
) -> dict[int, float]:
    out: dict[int, float] = {}
    for (week, _coop), c in commitments.items():
        out[week] = out.get(week, 0.0) + c.open_min_t
    return out
