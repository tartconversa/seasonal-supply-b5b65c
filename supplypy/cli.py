"""命令行入口（无包内相对导入，便于 python3 -I 直接执行脚本）。

python3 -I /path/to/supplypy/cli.py --root /path assess --scenario base
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 直接以脚本方式运行时，把包父目录加入 sys.path（-I 不自动加入 cwd）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supplypy import app, persistence  # noqa: E402
from supplypy.dataio import load_bundle, parse_stamp  # noqa: E402
from supplypy.review import commitment_ledger  # noqa: E402


def _print_summary(report) -> None:
    print(f"情景数: {report.scenario_count}")
    print(f"保底支出(元): {report.floor_spend:,.0f}")
    print(f"期望应急煤费用(元): {report.expected_emergency_cost:,.0f} "
          f"| P95: {report.p95_emergency_cost:,.0f}")
    print(f"期望总费用(元): {report.expected_total_cost:,.0f} "
          f"| P95: {report.p95_total_cost:,.0f}")
    print(f"保底违约发生概率: {report.any_breach_prob:.3f}")
    print(f"超出风险界限的周: {report.weeks_outside_limit or '无'}")
    print("\n逐周风险（仅列缺供概率>0 或越界周）:")
    for w in report.weeks:
        if w.shortfall_prob > 1e-9 or not w.within_limit:
            flag = "OK " if w.within_limit else "越界"
            print(f"  W{w.week} [{flag}] 雨概率={w.rain_prob:.2f} "
                  f"缺供P={w.shortfall_prob:.3f}(限{w.risk_limit:.2f}) "
                  f"期望热量={w.mean_heat_mj:,.0f}/{w.demand_heat_mj:,.0f} "
                  f"P05={w.p05_heat_mj:,.0f} 期末库存={w.mean_closing_stock_t:.0f}t")
    print("\n连续缺供区间(按出现频率):")
    for iv in report.shortage_intervals[:10]:
        print(f"  W{iv.start_week}-W{iv.end_week} ({iv.length_weeks}周) "
              f"发生概率={iv.occurrence_prob:.3f} 平均缺口={iv.mean_total_shortfall_mj:,.0f}MJ")
    print("\n冲突定位(前10):")
    for c in report.conflicts[:10]:
        print(f"  W{c.week} {c.coop_id} {c.resource}: 频率={c.prob:.3f} "
              f"平均缺口={c.mean_shortfall_t:.1f}t | {c.sample_detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="竹料供给承诺可靠性分析")
    parser.add_argument("--root", default=".", help="资料包根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_assess = sub.add_parser("assess", help="单方案蒙特卡洛评估")
    p_assess.add_argument("--scenario", required=True)
    p_assess.add_argument("--paths", type=int, default=2000)
    p_assess.add_argument("--seed", type=int, default=20270104)
    p_assess.add_argument("--no-proposals", action="store_true")
    p_assess.add_argument("--as-of", default=None)
    p_assess.add_argument("--out", default="runs")

    p_cmp = sub.add_parser("compare", help="比较只签signed与含proposal两套组合")
    p_cmp.add_argument("--scenario", required=True)
    p_cmp.add_argument("--paths", type=int, default=2000)
    p_cmp.add_argument("--out", default="runs")

    p_rev = sub.add_parser("review", help="签约后按新可知时点重估")
    p_rev.add_argument("--scenario", required=True)
    p_rev.add_argument("--as-of", required=True)
    p_rev.add_argument("--paths", type=int, default=2000)
    p_rev.add_argument("--out", default="runs")

    p_bt = sub.add_parser("backtest", help="滚动检验与归因")
    p_bt.add_argument("--reviews", default="104,106,108",
                      help="承诺期评审周（逗号分隔，104起）")
    p_bt.add_argument("--horizon", type=int, default=8)
    p_bt.add_argument("--paths", type=int, default=500)

    p_led = sub.add_parser("ledger", help="输出承诺台账")
    p_led.add_argument("--scenario", required=True)

    args = parser.parse_args(argv)
    bundle = load_bundle(args.root)

    if args.cmd == "assess":
        as_of = parse_stamp(args.as_of) if args.as_of else None
        cal, _res, report, params = app.run_assessment(
            bundle, args.scenario, n_paths=args.paths, seed=args.seed,
            include_proposals=not args.no_proposals, as_of=as_of)
        _print_summary(report)
        run_dir = persistence.save_run(args.out, f"assess-{args.scenario}", params,
                                       bundle, report, extra={"calibration": cal})
        print(f"\n运行已归档: {run_dir}")

    elif args.cmd == "compare":
        _, _, full, p1 = app.run_assessment(bundle, args.scenario, args.paths)
        _, _, signed_only, p2 = app.run_assessment(
            bundle, args.scenario, args.paths, include_proposals=False)
        print(f"{'指标':<26}{'仅已签':>18}{'含可选方案':>18}")
        for label, a, b in [
            ("保底支出(元)", signed_only.floor_spend, full.floor_spend),
            ("期望应急煤费用", signed_only.expected_emergency_cost, full.expected_emergency_cost),
            ("P95应急煤费用", signed_only.p95_emergency_cost, full.p95_emergency_cost),
            ("期望总费用", signed_only.expected_total_cost, full.expected_total_cost),
            ("保底违约概率", signed_only.any_breach_prob, full.any_breach_prob),
        ]:
            print(f"{label:<26}{a:>18,.1f}{b:>18,.1f}")
        print(f"{'越界周数':<26}{len(signed_only.weeks_outside_limit):>18}"
              f"{len(full.weeks_outside_limit):>18}")
        persistence.save_run(args.out, f"compare-{args.scenario}",
                             {"paths": args.paths}, bundle,
                             extra={"signed_only": signed_only, "with_proposals": full})

    elif args.cmd == "review":
        as_of = parse_stamp(args.as_of)
        cal, _res, report, params = app.run_review(
            bundle, args.scenario, as_of, n_paths=args.paths)
        _print_summary(report)
        persistence.save_run(args.out, f"review-{args.scenario}", params, bundle, report)

    elif args.cmd == "backtest":
        reviews = [int(x) for x in args.reviews.split(",")]
        cal, attribution, rolling = app.run_backtest(bundle, reviews, args.horizon, args.paths)
        print("归因分解（历史窗口 W39-W68，累计兆焦）:")
        for k, v in attribution.items():
            if isinstance(v, str):
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {v:,.0f}")
        print("\n签约后滚动重估（仅用评审时点可知信息）:")
        for row in rolling:
            print(f"  评审W{row['review_week']} 预测窗口W{row['window'][0]}-W{row['window'][1]} "
                  f"通知={row['applied_notices'] or '无'} "
                  f"预测雨占比={row['predicted_rain_share']:.3f} "
                  f"预测缺供P={row['predicted_mean_shortfall_prob']:.3f} "
                  f"越界周={row['weeks_outside_limit'] or '无'}")
        persistence.save_run("runs", "backtest",
                             {"reviews": reviews, "horizon": args.horizon, "paths": args.paths},
                             bundle, extra={"attribution": attribution, "rolling": rolling,
                                            "calibration": cal})

    elif args.cmd == "ledger":
        rows = [r for r in commitment_ledger(bundle) if r["scenario"] == args.scenario]
        print(json.dumps(rows[:12], ensure_ascii=False, indent=2))
        print(f"... 共 {len(rows)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
