"""资料载入与可知时点过滤。

只依赖标准库读取 CSV（pandas 在标定与模拟阶段使用），空单元保留为 None，
严格按 catalog 的字段顺序与类型解析。所有时间戳带 +08:00。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

WEEK0_MONDAY = dt.datetime(2025, 1, 6, 0, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
HISTORY_FIRST, HISTORY_LAST = 0, 103
COMMIT_FIRST, COMMIT_LAST = 104, 155


def parse_stamp(text: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise ValueError("时间戳必须带时区：" + text)
    return stamp


def _convert(value: str, kind: str) -> Any:
    if value == "":
        return None
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if kind == "datetime":
        return parse_stamp(value)
    return value


@dataclass(frozen=True)
class Source:
    source_id: str
    coop_id: str
    region: str
    harvest_months: tuple[int, ...]
    rotation_weeks: int
    last_harvest_week: int
    dry_mj_per_t: float


@dataclass(frozen=True)
class WeeklyObs:
    record_id: str
    week: int
    source_id: str
    planned_wet_t: float | None
    actual_wet_t: float | None
    moisture_pct: float | None
    weather: str
    available_at: dt.datetime


@dataclass(frozen=True)
class RouteCap:
    record_id: str
    week: int
    route_id: str
    capacity_t: float
    available_at: dt.datetime


@dataclass(frozen=True)
class CoopRoute:
    coop_id: str
    normal: str
    backup: str
    processing_t: float


@dataclass(frozen=True)
class StorageRule:
    age_week: int
    loss_fraction: float
    capacity_t: float


@dataclass(frozen=True)
class Contract:
    contract_id: str
    scenario: str
    coop_id: str
    week: int
    min_t: float
    option_t: float
    confirmed_t: float
    price_yuan_t: float
    status: str


@dataclass(frozen=True)
class DemandWeek:
    week: int
    heat_mj: float
    risk_limit: float
    emergency_cost: float


@dataclass(frozen=True)
class Notice:
    notice_id: str
    week: int
    route_id: str
    capacity_t: float
    available_at: dt.datetime


@dataclass(frozen=True)
class OpeningStock:
    stock_id: str
    source_id: str
    age_week: int
    wet_t: float
    moisture_pct: float
    as_of: dt.datetime


@dataclass
class DataBundle:
    root: Path
    sources: dict[str, Source]
    weekly: list[WeeklyObs]
    route_caps: list[RouteCap]
    coop_routes: dict[str, CoopRoute]
    storage_rules: list[StorageRule]
    contracts: list[Contract]
    demand: dict[int, DemandWeek]
    notices: list[Notice]
    opening_stock: list[OpeningStock]
    catalog: dict[str, Any] = field(repr=False, default_factory=dict)

    def contracts_of(self, scenario: str) -> list[Contract]:
        return [c for c in self.contracts if c.scenario == scenario]

    def scenarios(self) -> list[str]:
        return sorted({c.scenario for c in self.contracts})


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def load_bundle(root: str | Path) -> DataBundle:
    """按 catalog 声明读取全部资料表并转换类型。"""
    root = Path(root).resolve()
    catalog = json.loads((root / "data" / "catalog.json").read_text(encoding="utf-8"))

    sources: dict[str, Source] = {}
    for row in _read_csv(root / "data" / "sources.csv"):
        sources[row["source_id"]] = Source(
            source_id=row["source_id"],
            coop_id=row["coop_id"],
            region=row["region"],
            harvest_months=tuple(int(x) for x in row["harvest_months"].split(";")),
            rotation_weeks=int(row["rotation_weeks"]),
            last_harvest_week=int(row["last_harvest_week"]),
            dry_mj_per_t=float(row["dry_mj_per_t"]),
        )

    weekly = [
        WeeklyObs(
            record_id=r["record_id"],
            week=int(r["week"]),
            source_id=r["source_id"],
            planned_wet_t=_convert(r["planned_wet_t"], "number"),
            actual_wet_t=_convert(r["actual_wet_t"], "number"),
            moisture_pct=_convert(r["moisture_pct"], "number"),
            weather=r["weather"],
            available_at=parse_stamp(r["available_at"]),
        )
        for r in _read_csv(root / "data" / "weekly.csv")
    ]

    route_caps = [
        RouteCap(
            record_id=r["record_id"],
            week=int(r["week"]),
            route_id=r["route_id"],
            capacity_t=float(r["capacity_t"]),
            available_at=parse_stamp(r["available_at"]),
        )
        for r in _read_csv(root / "data" / "route_capacity.csv")
    ]

    coop_routes = {
        r["coop_id"]: CoopRoute(
            coop_id=r["coop_id"],
            normal=r["normal"],
            backup=r["backup"],
            processing_t=float(r["processing_t"]),
        )
        for r in _read_csv(root / "data" / "coop_routes.csv")
    }

    storage_rules = [
        StorageRule(
            age_week=int(r["age_week"]),
            loss_fraction=float(r["loss_fraction"]),
            capacity_t=float(r["capacity_t"]),
        )
        for r in _read_csv(root / "data" / "storage.csv")
    ]
    storage_rules.sort(key=lambda r: r.age_week)

    contracts = [
        Contract(
            contract_id=r["contract_id"],
            scenario=r["scenario"],
            coop_id=r["coop_id"],
            week=int(r["week"]),
            min_t=float(r["min_t"]),
            option_t=float(r["option_t"]),
            confirmed_t=float(r["confirmed_t"]),
            price_yuan_t=float(r["price_yuan_t"]),
            status=r["status"],
        )
        for r in _read_csv(root / "data" / "contracts.csv")
    ]

    demand = {
        int(r["week"]): DemandWeek(
            week=int(r["week"]),
            heat_mj=float(r["heat_mj"]),
            risk_limit=float(r["risk_limit"]),
            emergency_cost=float(r["emergency_cost"]),
        )
        for r in _read_csv(root / "data" / "demand.csv")
    }

    notices = [
        Notice(
            notice_id=r["notice_id"],
            week=int(r["week"]),
            route_id=r["route_id"],
            capacity_t=float(r["capacity_t"]),
            available_at=parse_stamp(r["available_at"]),
        )
        for r in _read_csv(root / "data" / "new_information.csv")
    ]

    opening_stock = [
        OpeningStock(
            stock_id=r["stock_id"],
            source_id=r["source_id"],
            age_week=int(r["age_week"]),
            wet_t=float(r["wet_t"]),
            moisture_pct=float(r["moisture_pct"]),
            as_of=parse_stamp(r["as_of"]),
        )
        for r in _read_csv(root / "data" / "opening_stock.csv")
    ]

    return DataBundle(
        root=root,
        sources=sources,
        weekly=weekly,
        route_caps=route_caps,
        coop_routes=coop_routes,
        storage_rules=storage_rules,
        contracts=contracts,
        demand=demand,
        notices=notices,
        opening_stock=opening_stock,
        catalog=catalog,
    )


def filter_as_of(rows: Iterable[Any], as_of: dt.datetime, attr: str = "available_at") -> list[Any]:
    """只保留可知时刻不晚于 as_of 的记录。"""
    return [row for row in rows if getattr(row, attr) <= as_of]


def route_capacity_view(
    route_caps: list[RouteCap],
    notices: list[Notice],
    as_of: dt.datetime,
) -> dict[tuple[int, str], tuple[float, str]]:
    """构造 (周, 通道) -> (容量, 依据) 的可知视图。

    预登记额定能力与事后通知都按 available_at 过滤；同一键若有可知的新信息，
    通知覆盖额定值。没有可知记录的键不出现，调用方按无能力处理而不是补零假设。
    """
    view: dict[tuple[int, str], tuple[float, str]] = {}
    for cap in route_caps:
        if cap.available_at <= as_of:
            view[(cap.week, cap.route_id)] = (cap.capacity_t, "rated:" + cap.record_id)
    for notice in notices:
        if notice.available_at <= as_of:
            view[(notice.week, notice.route_id)] = (notice.capacity_t, "notice:" + notice.notice_id)
    return view
