"""季节性竹料供给的承诺可靠性分析。

模块划分：
- dataio：资料载入、类型转换与可知时点过滤
- calendar：周索引与日历、采收资格
- history：历史标定（天气、执行率、含水率、停运、计划量）
- storage：分龄库存演化与库容约束
- engine：情景生成与按周物理平衡（采收、加工、路线、库存、热量）
- contracts：保底/可选/已履行承诺的约束解释与费用
- analysis：联合分布、连续缺供、应急煤、风险界限与冲突定位
- backtest：滚动历史检验与归因分解
- review：签约后新信息下的重估
- persistence：可重现的运行归档
"""

__all__ = [
    "dataio",
    "calendar",
    "history",
    "storage",
    "engine",
    "contracts",
    "analysis",
    "backtest",
    "review",
    "persistence",
]
