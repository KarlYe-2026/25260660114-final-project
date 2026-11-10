# 数据可得性探测记录

- 探测日期：2026-11-10（北京时间）
- 探测方式：kimi-datasource 插件（yahoo_finance / sp_data / stock_finance_data），实测调用并落盘原始返回至 `temp/probe_*.csv`
- 探测目的：为 QQQ 每日估值工作流确认输入数据可得性。**本文件只记录探测结论，不含分析代码。**

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

**⚠️ 口径注意（重要）：** 该接口返回的 OHLC 为**后复权价，非未复权 Close**。交叉验证：2024-11-11 同一交易日，yahoo 返回 Close=508.69，而 stock_finance_data（美股默认不复权）返回 Close=513.84，比值稳定约 0.990（两段日期一致），说明全序列被后续分红/资本利得分配统一缩放。task.md 要求未复权 `Close` 口径，**直接用该序列会与口径要求冲突**，使用前必须处理（改取原始价或还原复权因子）。同表内 `Dividends` / `Capital Gains` 列可用于还原校验。

### 源 B：stock_finance_data — `stock_finance_data_get_price`（备选/交叉验证源）

| 项目 | 结果 |
|---|---|
| 能否获取 | ✅ 可以 |
| 可用字段名 | `open, high, low, close, volume, thscode, time, thsname_cn, thsname_en, currency`（ticker 格式 `QQQ.O`） |
| 历史覆盖范围 | 单次请求最长 3 年，支持 `interval=D/W/M/Q/Y`。实测 2024-11-06→2024-11-15 返回未复权日线（2024-11-11 Close=513.84），落盘 `temp/probe_qqq_sfd_202411.csv` |
| 更新频率 | 日线，美股收盘后更新；另有 `stock_finance_data_get_stock_realtime_price`（ticker 用 `.US` 后缀）可取美股实时快照 |
| 价格调整 | **美股默认不复权**，可用 `adjust=none` 显式锁定 |

### 小结（第 1 项）

两个源均覆盖 ≥250 个交易日；若严格按 task.md 未复权口径，stock_finance_data（`QQQ.O`, 默认不复权）口径最直接，yahoo_finance 需先解决复权问题。

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

**PE 维度将按 task.md 的降级规则处理**

---

## 附：探测产物清单

| 文件 | 内容 |
|---|---|
| `temp/probe_qqq_px_2y.csv` | yahoo_finance QQQ 2 年日线（500 行，含复权口径问题） |
| `temp/probe_qqq_sfd_202411.csv` | stock_finance_data QQQ.O 未复权日线交叉验证（8 行） |
| `temp/probe_qqq_info.csv` | yahoo_finance QQQ 信息快照（含 trailingPE 等） |
