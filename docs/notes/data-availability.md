# 数据可得性探测记录

- 探测日期：2026-11-10（北京时间）
- 探测方式：kimi-datasource 插件（yahoo_finance / sp_data / stock_finance_data），实测调用并落盘原始返回至 `temp/probe_*.csv`；生产抓数（stock_finance_data 全量窗口）落盘 `data/raw/asof-2026-11-06/`
- 探测目的：为 QQQ 每日估值工作流确认输入数据可得性。**本文件只记录探测结论，不含分析代码。** 数据来源的最终口径以 `docs/plans/task.md` 第 5 节为准。

---

## 1. QQQ 每日收盘价历史

**结论：可以获取（两个独立源均满足 ≥250 个交易日要求）。**

### 源 A：yahoo_finance — `get_historical_stock_prices`

| 项目 | 结果 |
|---|---|
| 能否获取 | ✅ 可以 |
| 可用字段名 | `Date, Open, High, Low, Close, Volume, Dividends, Stock Splits, Capital Gains, thscode, thsname_cn, thsname_en, currency`（ticker 直接传 `QQQ`） |
| 历史覆盖范围 | 单次请求最长 2 年（`period` 或 `start_date+end_date`，区间 >1 年时 `interval` 须 ≥1d）。实测 `period=2y, interval=1d` 返回 **500 个交易日：2024-11-11 → 2026-11-09**，落盘 `temp/probe_qqq_px_2y.csv` |
| 更新频率 | 美股收盘后日线更新（16:00 ET，北京时间 04:00/05:00 后可用） |

**⚠️ 口径注意（重要，且仅限本接口）：** 该接口返回的 OHLC 为**后复权价，非未复权 Close**。交叉验证：2024-11-11 同一交易日，yahoo 返回 Close=508.69，而 stock_finance_data（美股默认不复权）返回 Close=513.84，比值稳定约 0.990（两段日期一致），说明全序列被后续分红/资本利得分配统一缩放。task.md 要求未复权 `Close` 口径，**直接用该序列会与口径要求冲突**，使用前必须处理（改取原始价或还原复权因子）。同表内 `Dividends` / `Capital Gains` 列可用于还原校验。

> 上述"后复权"问题**是 yahoo_finance 接口特有的口径行为，与 stock_finance_data 无关**：后者为独立上游，美股默认即不复权（可 `adjust=none` 显式锁定），返回的即为未复权原始价（同交易日 Close=513.84，与 task.md 口径一致）。因此该问题只影响"是否采用 yahoo_finance 作为价格源"，不构成对 stock_finance_data 的顾虑。

### 源 B：stock_finance_data — `stock_finance_data_get_price`（**生产主源**，见 task.md 第 5 节）

| 项目 | 结果 |
|---|---|
| 能否获取 | ✅ 可以 |
| 可用字段名 | `open, high, low, close, volume, thscode, time, thsname_cn, thsname_en, currency`（ticker 格式 `QQQ.O`） |
| 历史覆盖范围 | 单次请求最长 3 年，支持 `interval=D/W/M/Q/Y`。小样本实测 2024-11-06→2024-11-15 返回未复权日线（2024-11-11 Close=513.84），落盘 `temp/probe_qqq_sfd_202411.csv` |
| **全量窗口实测（2026-11-10 生产抓数）** | `ticker=QQQ.O, start_date=2023-11-07, end_date=2026-11-06, adjust=none, interval=D` 返回 **753 个交易日（2023-11-07 → 2026-11-06）**，落盘 `data/raw/asof-2026-11-06/raw_sfd_qqq_price.csv`，请求留档 `_fetch_request.json`（fetched_at_utc 2026-11-10T12:30:27Z，request_id `707d0fbaca92569e47a3cba76b7c5f36`） |
| 窗口上限实测 | 单次区间上限 **1095 天（3 年）**：`2023-11-06 → 2026-11-06`（1096 天）被接口拒绝（`PARAMETER_ERROR: Date range too large`），改 2023-11-07 起（1095 天）通过 |
| 更新频率 | 日线，美股收盘后更新；另有 `stock_finance_data_get_stock_realtime_price`（ticker 用 `.US` 后缀）可取美股实时快照 |
| 价格调整 | **美股默认不复权**，已用 `adjust=none` 显式锁定（与 task.md 未复权口径一致） |

### 曾评估并弃用的候选：Yahoo Chart API（非插件直连）

2026-11-10 实测：本机直连 `query1/query2.finance.yahoo.com/v8/finance/chart/QQQ` 全部返回 **HTTP 403**（6 次指数退避重试仍失败）；补做 cookie+crumb 流程（guce/fc.yahoo.com 取 A1/A3 等 cookie 后请求 getcrumb）**同样 403**，属边缘节点对本机出口 IP 的封禁，短期内重试无意义。经项目负责人确认弃用，价格改用插件 `stock_finance_data`（服务端抓取不受本机出口 IP 限制）。原始重试日志见会话后台任务 `bash-p8thibr0`。

### 小结（第 1 项）

- **价格生产主源 = 插件 stock_finance_data**（`QQQ.O`, `adjust=none` 未复权，单次 3 年窗口，753 个交易日 ≫ 250 要求），与 task.md 第 5 节口径一致。
- yahoo_finance（后复权口径）不与未复权价格混算，仅可作交叉验证；Yahoo Chart API 直连已被本机网络 403 封禁，弃用。

---

## 2. QQQ / 纳斯达克100指数 PE(TTM)

**结论：仅当前值快照可得；历史序列在插件全部数据源中均不可得。**

### 当前值快照：yahoo_finance — `get_stock_info`

| 项目 | 结果 |
|---|---|
| 能否获取 | ✅ 当前值可以（单点快照，无历史） |
| 可用字段名 | `trailingPE`（实测 27.98），另有 `epsTrailingTwelveMonths`（24.2776）、`navPrice`（678.86）、`priceToBook`（1.899） |
| 覆盖范围 | 仅"当前时刻"一个值，无时间序列；实测于 2026-11-10 早盘前（marketState=PRE，最近完整交易日 2026-11-09） |
| 更新频率 | 随行情快照刷新（盘中约 15 分钟延迟，`exchangeDataDelayedBy=15`），属时点数据 |
| 落盘 | `temp/probe_qqq_info.csv` |

### 历史序列：三个候选源全部不可行

| 源 | 结果 |
|---|---|
| `sp_data` | ❌ `sp_get_financials(module=valuation)` 报错：`ticker 'QQQ' was not found in current S&P listings`——S&P Capital IQ 只覆盖上市公司，ETF 无估值模块 |
| `stock_finance_data` | ❌ 无任何美股估值/PE 接口（财务指标、报表、价格、股东接口均不含 PE） |
| 纳斯达克100指数（^NDX / XNDX）层面 PE | ❌ 插件无指数估值数据源 |

**结论：插件内历史 PE 序列不可得；生产来源改用蛋卷基金公开接口（NDX 指数口径，作为 QQQ 的代理），与 task.md 第 5 节一致。** 蛋卷侧的可得性与口径见 `data/raw/asof-*/_meta.json`。

---

## 附：探测产物清单

| 文件 | 内容 |
|---|---|
| `temp/probe_qqq_px_2y.csv` | yahoo_finance QQQ 2 年日线（500 行，含复权口径问题） |
| `temp/probe_qqq_sfd_202411.csv` | stock_finance_data QQQ.O 未复权日线小样本验证（8 行） |
| `temp/probe_qqq_info.csv` | yahoo_finance QQQ 信息快照（含 trailingPE 等） |
| `data/raw/asof-2026-11-06/raw_sfd_qqq_price.csv` | **生产抓数**：stock_finance_data QQQ.O 全量窗口日线（753 行，2023-11-07 → 2026-11-06，adjust=none 未复权） |
| `data/raw/asof-2026-11-06/_fetch_request.json` | 生产抓数请求留档（API、参数、fetched_at_utc、request_id） |
| `data/raw/asof-2026-11-06/_meta.json` | 数据源溯源 meta（每数据集含来源、URL/参数、真实请求时间） |
