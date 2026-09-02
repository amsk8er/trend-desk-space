---
version: v1.5
effective_from: 2026-07-16
source_path: 趋势交易-开拓者一号/03-模板SOP/我的纪律.md
source_hash: 73295365f48f08ccefc39720340b65dbf91b34a38267fe9031f582427a3cce4c
updated: 2026-08-04
---

# 纪律 v1.5 机器可执行规则投影

> 本文件是《我的纪律.md》v1.5 的机器投影：只包含可执行参数与判定逻辑，供 `rules.py` 加载与审计。
> 人类真相源：`趋势交易-开拓者一号/03-模板SOP/我的纪律.md`（A股/ETF）；美股另见 `03-模板SOP/我的美股纪律.md` v1.2。
> 规则修改只允许发生在真相源文档，再通过 sync 流程更新本投影与 `rules.py`（见 `docs/rules-change-sop.md`）。

## 1. 版本与生效

- 版本：v1.5
- 生效日：2026-07-16
- 修改条件：《我的纪律.md》第九节（连续执行 ≥ 8 周且完成清仓交易 ≥ 30 笔）

## 2. 参数表

| 维度 | 参数 | 值 |
|---|---|---|
| 选股·A股个股 | 交易权限 | 仅有权交易的板块（当前无创业板/科创板权限） |
| 选股·A股个股 | 入场信号 | 当日温转热（requires_warm_to_hot） |
| 选股·A股个股 | 板块温度 | ≥ 温 |
| 选股·A股个股 | 流通市值 | ≥ 300 亿元 |
| 选股·A股个股 | 日成交额 | ≥ 5 亿元 |
| 选股·A股个股 | 右侧天数 | ≤ 10 天 |
| 选股·A股个股 | 节气 | < 大暑（大暑及以后不建仓） |
| 选股·A股个股 | 温转沸 | 排除（exclude_warm_to_boiling） |
| 选股·ETF | 入场信号 | ETF 自身当日温转热 |
| 选股·ETF | 基金规模 | ≥ 25 亿元 |
| 选股·ETF | 日成交额 | ≥ 2 亿元 |
| 选股·ETF | 趋势强度 | ≥ 80 |
| 选股·ETF | 节气 | < 大暑 |
| 选股·ETF | 温转沸 | 排除 |
| 选股·ETF | 同指数去重 | 同一基准指数只保留强度最高者；同强度按成交额→规模→代码 |
| 观察 | 强度周变化 | 只展示，不参与筛选/排序/仓位/离场；未定义值原样展示 |
| 容量 | 单新仓 | 5% × 环境系数 |
| 容量 | 环境系数 | 温/热/沸=1.0，平=0.5，凉=0.25，寒/冻=0 |
| 容量 | 常规模式 | 单日新建 ≤ 2 只，新增仓位 ≤ 10% |
| 容量 | 强共振模式 | 单日新建 ≤ 5 只，新增仓位 ≤ 25% |
| 容量 | 上限 | 无杠杆、总仓位 ≤ 100%、工具 ≤ 20 |
| 离场 | 全部离场 | 温度平/凉/寒/冻 或 危险信号，次日开盘全清 |
| 离场 | 部分止盈 | 沸、开香槟各减 25%；同日叠加按同一基数相加；连续多日连续止盈 |
| 离场 | 固定止损 | 不叠加固定百分比止损 |

## 3. 判定逻辑

- 选股：按 §2 硬条件筛选 → ETF 同指数去重 → 强度、成交额排序 → 容量生成次日唯一白名单。
- 容量：`可新建数量 = min(剩余工具名额, 模式剩余名额, floor(剩余总仓位 ÷ 单个新仓))`；不足一个完整新仓不拆小仓。
- 离场优先级：全清（危险/转平及以下）→ 当日止盈 25%×N → 持有。
- 再入场：危险或转平离场后均须等待新的温转热，并重新通过全部入场与容量条件。

## 4. 与《我的纪律.md》v1.5 的映射

- §2 参数表 ← 《我的纪律》第五/六/七节（入场/容量/离场）
- 强度周变化观察 ← 《我的纪律》v1.5 第十节第 8 条（只展示，不参与决策）
- 修改条件 ← 《我的纪律》第九节
- 每日执行顺序 ← 《我的纪律》第八节

## 5. 机器参数块（rules.py 加载，JSON 与 canonical_rules_json() 完全一致）

```json
{"capacity":{"base_new_position_pct":0.05,"environment_factors":{"冻":0.0,"凉":0.25,"寒":0.0,"平":0.5,"沸":1.0,"温":1.0,"热":1.0},"max_tools":20,"max_total_weight":1.0,"normal":{"max_added_weight":0.1,"max_new_tools":2},"resonance":{"max_added_weight":0.25,"max_new_tools":5}},"effective_from":"2026-07-16","exit":{"fraction_per_signal":0.25,"full_exit_priority":1,"full_exit_temperatures":["平","凉","寒","冻"],"hold_priority":4,"profit_signals":["champagne","boiling"],"reduce_priority":2,"round_lot":100},"observation":{"strength_change":{"applies_to":["eligible_candidates","holdings"],"decision_effect":"none","documented_values":{"":"none","↑":"moderate","↑↑":"significant"},"field":"trendStrengthLocalChange","unknown_value_policy":"display_raw_only"}},"selection":{"etf":{"benchmark_tiebreakers":["strength","amount_yi","aum_yi","code"],"deduplicate_by_benchmark":true,"exclude_warm_to_boiling":true,"min_amount_yi":2.0,"min_aum_yi":25.0,"min_strength":80.0,"requires_warm_to_hot":true},"max_entry_phase_exclusive":"大暑","phase_order":["立春","雨水","惊蛰","春分","清明","谷雨","立夏","小满","芒种","夏至","小暑","大暑","立秋","处暑","白露","秋分","寒露","霜降","立冬","小雪","大雪","冬至","小寒","大寒"],"stock":{"exclude_warm_to_boiling":true,"max_right_side_days":10,"min_amount_yi":5.0,"min_float_market_cap_yi":300.0,"min_sector_temperature":"温","requires_warm_to_hot":true}},"version":"v1.5"}
```
