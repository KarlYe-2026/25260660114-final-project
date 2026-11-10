# QQQ 每日"趋势 + 估值"复合观察流水线

- 姓名：叶剑舟
- 学号：25260660114

## 任务概述

每个美股交易日收盘后，对纳斯达克 100 ETF（QQQ）执行"趋势 + 估值"复合判断，输出当日研究信号。三个低估信号：

| 信号 | 定义 | 类型 |
|------|------|------|
| S1 | 收盘价 < MA200（200 日均线） | 趋势 |
| S2 | 收盘价 < MA50（50 日均线） | 趋势 |
| S3 | 当前 PE(TTM) < 历史 PE(TTM) 中位数 | 估值 |

判定规则：满足信号数 `n_signals ≥ min_signals_required`（默认 2，见 `config/params.json`）→ 信号为"建议关注"，否则为"观望"；所有比较一律采用严格小于。PE(TTM) 缺失（n/a）时降级为"S1、S2 同时满足才输出建议关注"，并在产物中明确标注降级原因，严禁静默降级。

流水线固定为四个阶段，由 `src/main.py` 统一编排：① 取数 → ② 计算 → ③ 检查 → ④ 信号。数据来源：价格取自 kimi-datasource 插件 `stock_finance_data`（`stock_finance_data_get_price`，ticker `QQQ.O`，`adjust=none` 未复权日线）；PE(TTM) 取蛋卷基金纳指 100 指数周度序列（NDX 口径，作为 QQQ 代理），聚合口径见 `data/raw/asof-*/_meta.json`。本工作流输出为研究信号，不构成投资建议。

## 目录与各文件说明

| 路径 | 说明 |
|------|------|
| `docs/plans/task.md` | 任务定义：指标公式、信号规则、验收标准（实现与验收的唯一准绳） |
| `docs/design.md` | 三角色架构设计：Orchestrator / Generator / Critic 职责边界与状态恢复机制 |
| `docs/verification.md` | 验收核验记录（防未来数据、两轮 Critic、断点恢复、基线回滚、Hook 审计） |
| `docs/notes/scheduling.md` | 生产环境 Windows 任务计划程序调度配置说明 |
| `docs/notes/data-availability.md` | 数据可得性探测记录（候选数据源实测与取舍原因） |
| `docs/notes/recovery.md` | 参数演进与回滚测试记录 |
| `docs/notes/approval.md` | 参数变更人工审批记录 |
| `src/main.py` | 流水线 Controller（Orchestrator）：调度三阶段、维护 `.state/state.json`、有界重试、`--resume` 断点恢复 |
| `src/fetch_data.py` | data-agent 入口：校验价格原始快照、抓取蛋卷 PE 周度序列、按 asof 截断防泄露，落盘 `data/raw/` 与 `data/processed/qqq_daily.csv` |
| `src/calculate.py` | calc-agent 入口：读取原始快照计算 S1/S2/S3 与最终信号，写 `output/tables/valuation-asof-<日期>.csv` |
| `tests/checker.py` | checker-agent（Critic-1 硬断言）：独立重算并与产物比对，问题写入 `.state/issues.json` |
| `agents/` | 子代理定义：`data-agent` / `calc-agent` / `checker-agent` 及 `generator_lead` / `critic_lead` |
| `config/params.json` | 可调参数：均线窗口、`min_signals_required`、最大重试次数、子进程超时 |
| `data/raw/asof-<日期>/` | 原始数据快照（插件原始返回、`_meta.json`、`_fetch_request.json`），只留档、永不修改 |
| `data/processed/qqq_daily.csv` | 加工产物：截断至 asof 后的日线价格 + 月度 PE 列 |
| `output/tables/` | 每日估值产物 `valuation-asof-<日期>.csv`（各信号数值/阈值/是否满足/最终信号） |
| `output/signal-history.md` | 信号历史汇总（人读） |
| `output/figures/`、`report/`、`slides/` | 图表、人读报告、演示材料 |
| `.state/state.json` | 运行状态文件（`main.py` 唯一读写者，原子写入） |
| `.state/audit.log` | Hook 审计日志（`hooks.json` 配置的 PostToolUse 事件） |
| `.state/issues.json` | checker 审查问题清单（按 pit-001 起递增编号） |
| `hooks.json` + `audit_event.py` | PostToolUse Hook 配置与审计脚本 |
| `.logs/`、`temp/`、`tests/` | 执行日志、一次性暂存文件、检查脚本 |

## 环境配置

- **Python**：3.10 及以上（开发机实测 Python 3.15.1）。
- **依赖库**：pandas（开发机实测 3.1.0），其余均为 Python 标准库。安装命令：

  ```bash
  pip install pandas
  ```

- **kimi-datasource 插件**：Kimi Code 托管插件，在会话中作为 MCP 工具使用；价格数据通过其 `stock_finance_data` 数据源抓取，原始返回由插件调用方落盘 `data/raw/asof-<日期>/`。
- **PE(TTM)**：由 `src/fetch_data.py` 直连蛋卷基金公开接口抓取，无需密钥。
- **API 密钥**：本项目无需任何 API 密钥。

## 运行方法

运行前请确认 `data/raw/asof-<日期>/` 目录下已存在该日期的价格原始快照（`raw_sfd_qqq_price.csv` 及 `_fetch_request.json`，由 kimi-datasource 插件调用落盘）；若缺失，`fetch_data.py` 会显式报错退出，绝不自行造数。

- **单日全量新跑**（覆写 `.state/state.json`）：

  ```bash
  python src/main.py --asof 2026-11-06
  ```

- **`--asof` 历史回跑**（回跑任意历史交易日，用于调试、验收与补数；回跑同样受信息截止约束，数据先截断至 asof 再计算）：

  ```bash
  python src/main.py --asof 2026-11-04
  ```

- **`--resume` 断点恢复**（先核验已完成阶段产物 SHA256，一致则"已核验，跳过"，只补做未完成阶段；不一致置 BLOCKED 并提示人工处理）：

  ```bash
  python src/main.py --asof 2026-11-06 --resume
  ```

退出码：`0` = 全部完成；`1` = 阶段失败且重试超限；`2` = 状态/参数类错误。注意：`--asof` 或 `config/params.json` 变更属于新案例，须先删除 `.state/state.json` 再全量重跑。生产环境的定时自动触发见 `docs/notes/scheduling.md`。

## 证据索引

- 任务定义与验收标准：`docs/plans/task.md`
- 架构与恢复设计：`docs/design.md`
- 验收核验记录：`docs/verification.md`
- 生产调度配置：`docs/notes/scheduling.md`
- 数据源探测与取舍：`docs/notes/data-availability.md`
- 参数演进、回滚与审批：`docs/notes/recovery.md`、`docs/notes/approval.md`
- 信号产物与汇总：`output/tables/`、`output/signal-history.md`
- 运行与审计留痕：`.state/state.json`、`.state/audit.log`、`.state/issues.json`、`.logs/`

## 本人贡献说明

- **选题与规则设计**：选定"趋势 + 估值"复合观察方向，设计 S1/S2/S3 三信号及"满足信号数 ≥ min_signals_required"的判定规则（含严格小于口径）。
- **降级规则决策**：确定 PE(TTM) 缺失（n/a）时的降级规则——S1、S2 同时满足才输出"建议关注"，并要求在产物中显式标注降级原因，禁止静默降级。
- **数据源选型拍板**：拍板价格走 kimi-datasource 插件、PE(TTM) 采用蛋卷基金 NDX 周度序列作为 QQQ 代理；决定弃用 Yahoo（本机直连被 403 封禁），取舍过程见 `docs/notes/data-availability.md`。
- **人工核对**：人工核对收盘价（批注见 `output/tables/valuation-asof-2026-11-06.csv`），并手算复核均线结果与产物一致。
- **pit-001 处置决策**：决定原始快照原样留档、不删改越界观测，防泄露闸门设在消费端（加工与计算环节执行截断），处置记录见 `.state/issues.json` 中 pit-001。
- **参数演进测试的审批与否决**：审批了 `min_signals_required` 由 2 改为 3 的参数演进测试（见 `docs/notes/approval.md`）；测试后否决该变更，回滚至基线参数并恢复基线产物（见 `docs/notes/recovery.md`）。
