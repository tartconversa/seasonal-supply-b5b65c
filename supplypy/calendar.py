"""周索引与日历、采收资格规则。"""
from __future__ import annotations

import datetime as dt

from .dataio import WEEK0_MONDAY, Source


def week_monday(week: int) -> dt.date:
    return (WEEK0_MONDAY + dt.timedelta(days=7 * week)).date()


def week_month(week: int) -> int:
    """该周周一所在月份；资料口径以周为最小时间单位，采收资格按此判定。"""
    return week_monday(week).month


def harvest_open(source: Source, week: int) -> bool:
    """地块在该周是否处于允许采收月份。"""
    return week_month(week) in source.harvest_months


def rotation_ok(source: Source, week: int, last_harvest_week: int) -> bool:
    """距最近一次采收是否满足最小轮伐间隔。"""
    return week - last_harvest_week >= source.rotation_weeks


def eligible_sources(
    sources: dict[str, Source],
    week: int,
    last_harvest: dict[str, int],
) -> list[str]:
    """返回该周同时满足季节与轮伐约束的地块。"""
    result = []
    for sid, src in sources.items():
        last = last_harvest.get(sid, src.last_harvest_week)
        if harvest_open(src, week) and rotation_ok(src, week, last):
            result.append(sid)
    return sorted(result)


def iso_label(week: int) -> str:
    monday = week_monday(week)
    return f"W{week}({monday.isoformat()})"
