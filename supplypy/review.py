"""签约后新信息下的重估：区分已履行、仍有约束力与可变补充。"""
from __future__ import annotations

import datetime as dt

from .contracts import build_commitment_index
from .dataio import DataBundle, route_capacity_view


def split_commitments(bundle: DataBundle, include_proposals: bool):
    """signed 与 proposal 分开建索引，重估时不可撤回部分保持约束。"""
    signed = build_commitment_index(
        [c for c in bundle.contracts if c.status == "signed"], True)
    full = build_commitment_index(bundle.contracts, include_proposals)
    return signed, full


def capacity_view_at(bundle: DataBundle, as_of: dt.datetime):
    """可知容量视图：预登记额定 + 已发布通知覆盖，杜绝使用未来路况。"""
    return route_capacity_view(bundle.route_caps, bundle.notices, as_of)


def commitment_ledger(bundle: DataBundle) -> list[dict]:
    """逐合同台账：已履行、剩余保底义务、可变量、状态。"""
    rows = []
    for c in sorted(bundle.contracts, key=lambda x: (x.scenario, x.week, x.coop_id)):
        rows.append({
            "contract_id": c.contract_id,
            "scenario": c.scenario,
            "coop_id": c.coop_id,
            "week": c.week,
            "status": c.status,
            "min_t": c.min_t,
            "option_t": c.option_t,
            "confirmed_t": c.confirmed_t,
            "open_min_t": max(0.0, c.min_t - c.confirmed_t),
            "open_option_cap_t": max(0.0, c.min_t + c.option_t - c.confirmed_t),
            "floor_spend_yuan": c.min_t * c.price_yuan_t,
            "revocable": c.status != "signed",
        })
    return rows
