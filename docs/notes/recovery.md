# 参数演进与回滚测试记录
[main] 阶段 fetch_data: 运行 src/fetch_data.py --asof 2026-11-04(第 1 次尝试)
[fetch_data] asof=2026-11-04, 原始数据目录: data\raw\asof-2026-11-04
[fetch_data] 价格 OK: 2023-11-07 ~ 2026-11-04, 共 751 个交易日(来源 kimi-datasource 插件 / stock_finance_data / stock_finance_data_get_price, 请求于 2026-11-10T13:56:08Z)
[fetch_data] 请求 PE(TTM) 历史: https://danjuanfunds.com/djapi/index_eva/pe_history/NDX?day=all
[fetch_data] PE(TTM) OK: 2016-11-01 ~ 2026-11-01, 共 121 个月
[fetch_data] 完成: data\processed\qqq_daily.csv (751 行), meta: data\raw\asof-2026-11-04\_meta.json
[main] 阶段 fetch_data 完成,产物 SHA256 已写入 .state/state.json
[main] 阶段 calculate: 运行 src/calculate.py --asof 2026-11-04(第 1 次尝试)
[calculate] asof=2026-11-04(评估日 2026-11-04), 收盘价 691.95, MA200=680.56, MA50=723.56
[calculate] PE(TTM) 当前=26.98, 历史中位数=30.58, 满足信号数 2/3
[calculate] 最终信号: 观望
[calculate] 结果已写入 output\tables\valuation-asof-2026-11-04.csv
[main] 阶段 calculate 完成,产物 SHA256 已写入 .state/state.json
[main] 阶段 checker: 运行 tests/checker.py --asof 2026-11-04(第 1 次尝试)
[checker] 提示: raw 快照含 1 条晚于 asof=2026-11-04 的原始观测(抓数时刻完整留档,不视为违规);消费端闸门核实通过,继续校验加工产物无越界。
[checker] asof=2026-11-04 审查通过:三个信号及降级规则独立重算一致,加工产物与计算实际使用的 PE 月度序列无晚于 asof 的观测。
[main] 阶段 checker 完成,产物 SHA256 已写入 .state/state.json
[main] 流水线完成: case_id=4288793659951088, asof=2026-11-04,状态 DONE