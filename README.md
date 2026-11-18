# 季节性竹料供给的承诺可靠性分析

判断竹料年度/阶段采购承诺在**连续用汽最紧张的周**能否兑现，而不是只看全年总吨数。
卖方报湿吨、锅炉要可用热量；同一场持续降雨会同时打击多个乡镇的采收、道路与干燥，
因此本产品把共同天气、共享路线、共享库容和分龄库存显式建模，输出各周可交付热量的
联合分布、连续缺供区间、应急煤需求与保底支出。

## 目录

- `data/`、`docs/资料口径.md`：合成业务资料与字段契约（只读输入）
- `sourcepack/`：原有资料文件/字段/类型自检工具，不含业务计算
- `supplypy/`：分析产品
- `tests/`：资料检查器测试与 `supplypy` 口径单元测试

## 分析口径（与 docs/资料口径.md 对齐）

- **共同天气，不是独立供应商**：历史中南北区天气完全同步，按一条 Markov 共同天气过程
  生成情景；雨周同时带来执行率下降、含水率上升和 BRIDGE 条件停运，相关性被保留。
- **本来无计划 ≠ 零产能**：`planned=0 且 actual 为空` 不进入执行率样本；只有 `planned>0`
  才是计划观测，其 `actual=0` 属于计划执行失败。
- **季节与轮伐**：地块只在允许月份采收，并受最小轮伐间隔约束；`last_harvest_week` 是
  相对承诺首周（W104）的偏移。5–8 月关闭期零采收，靠跨周库存桥接。
- **湿吨到热量**：每湿吨可用热量 = `(1 - 含水率) × 干基热值`，无隐含转换系数。
- **分龄库存**：按来源/年龄/含水率分龄，逐周计提损耗、先进先出取料，全部批次共用
  800 湿吨库容，超额拒收。
- **承诺三分**：`min_t` 不可撤销保底（坏情景也不能取消，take-or-pay 计费）；
  `option_t` 仅为可选上限；`confirmed_t` 已履行，不能再买或撤销。`signed` 不可撤回，
  `proposal` 仍可变。
- **共享容量不重复计数**：同名路线（如 BRIDGE 同时是 C1/C2 替补与 C3 正常）在所有
  合作社间共享同一余量；加工能力按合作社按周共享。
- **可知时点**：预测只使用 `available_at` 不晚于评审时点的资料；承诺期路线表是预登记
  额定能力，不是未来路况；`new_information` 通知在发布后才覆盖额定值。

## 命令行用法

隔离模式运行（防止工作目录植入模块），数据根目录显式传入：

```bash
# 单方案蒙特卡洛评估（默认 2000 条情景）
python3 -I supplypy/cli.py --root . assess --scenario base --paths 2000

# 比较"仅已签"与"含可选 proposal"两套组合的风险与费用
python3 -I supplypy/cli.py --root . compare --scenario base --paths 2000

# 签约后按新可知时点重估（通知在该时点之后发布的不纳入）
python3 -I supplypy/cli.py --root . review --scenario base \
    --as-of 2027-01-13T08:00:00+08:00

# 历史归因（数量/质量/相关中断）+ 签约后滚动重估
python3 -I supplypy/cli.py --root . backtest --reviews 104,106,108

# 承诺台账：已履行、剩余保底义务、可变量、是否可撤销
python3 -I supplypy/cli.py --root . ledger --scenario base
```

也可用模块方式（非隔离环境）：`python3 -m supplypy assess --scenario base`。

## 输出解读

- **逐周风险**：可交付热量的均值/P05/P50/P95、缺供概率与该周 `risk_limit` 对照，
  越界周明确标出——全年总量足够但某周断供不会被掩盖。
- **连续缺供区间**：相邻周同时低于需求的联合发生概率与平均缺口，对应"连续用汽紧张"。
- **冲突定位**：每个保底缺口带唯一根因（`season_rotation` / `yield` / `route` /
  `processing` / `storage`）、周、合作社与缺口量，定位冲突的时间与资源。
- **费用**：固定保底支出、期望/P95 应急煤费用与总费用。
- **运行归档**：每次运行写入 `runs/<名称>-<时间戳>/`，含 manifest（参数、随机种子、
  Python 版本、9 张输入表指纹）与结果 JSON，可按原信息重现签约依据。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `dataio.py` | 资料载入、类型转换、可知时点过滤、容量视图 |
| `calendar.py` | 周索引/日历、采收月份与轮伐资格 |
| `history.py` | 仅用历史窗口标定天气、执行率、含水率、停运 |
| `scenarios.py` | 共同天气 Markov、条件停运、经验自助情景生成 |
| `storage.py` | 分龄库存、损耗、共享库容、FIFO 取料 |
| `contracts.py` | 保底/可选/已履行的约束解释与保底支出 |
| `engine.py` | 按周物理平衡与根因冲突记录 |
| `analysis.py` | 联合分布、连续缺供、风险界限、冲突汇总 |
| `backtest.py` | 历史回放归因与签约后滚动重估 |
| `review.py` | 已履行/约束/可变承诺分账与容量视图 |
| `persistence.py` | 输入指纹、参数与结果的可重现归档 |
| `app.py` / `cli.py` | 流程编排与命令行入口 |

## 测试

```bash
python3 -I -m compileall -q supplypy tests
python3 -I -c "import sys,unittest; sys.path.insert(0,'.'); \
  unittest.TextTestRunner().run(unittest.TestLoader().loadTestsFromName('tests.test_supplypy'))"
```

> 说明：`data/catalog.json` 登记的 sha256 与当前数据文件（与 git HEAD 字节一致）
> 全部不符，属资料包既有状况，因此 `sourcepack` 的摘要校验 `test_pack` 会报错；
> 字段、类型、记录键与行数等结构校验，以及 `supplypy` 的 21 项口径测试均通过。
> 分析不依赖该摘要，运行时按文件实际内容读取。

## 已知建模边界

- 不执行真实运输调度，只依据交付周期、质量与容量契约判断承诺能否成立。
- 历史仅 12 个雨周执行样本，执行率采用经验自助法保留真实分布形状，样本量限制需在
  决策时知悉。
- 情景内南北区天气按历史观测的完全同步处理；若未来出现区域分歧，需扩展为多区域天气过程。
