# QQQ 每日估值观察 — 三角色架构与状态恢复设计

> 本文档是 `docs/plans/task.md` 的架构补充：把流水线拆成 Orchestrator / Generator / Critic 三个角色，定义各自的职责边界、交互契约，以及围绕 `state.json` 的中断恢复机制。实现以 task.md 的指标公式、信号规则和验收测试为准，本文档定义"谁来做什么、中断后怎么接着做"。

## 1. 角色总览

```
Windows 任务计划程序 (每日 06:00 北京时间)
              │
              ▼
      ┌───────────────┐   返回值/退出码   ┌──────────────────┐
      │ Orchestrator  │ ◄─────────────── │     Generator     │
      │ (调度+状态机)  │ ── 入参/指令 ───► │ (取数+计算参考价)  │
      └──────┬────────┘                  └────────┬─────────┘
             │                                  │ 原始快照/指标/信号
             │   ┌──────────────────────────────┼──────────────┐
             │   ▼                              ▼              ▼
             │ ┌─────────┐  verdict1    ┌──────────┐  verdict2
             └►│Critic-1 │              │ Critic-2 │
               │(硬断言)  │              │(LLM终审) │
               └─────────┘              └──────────┘
             ▲
             │ 唯一读写者
      .state/state.json
```

| 角色 | 一句话职责 | 实现载体 | 是否写 state.json |
|------|-----------|---------|------------------|
| **Orchestrator** | 调度触发、按阶段推进流水线、维护 state.json、失败恢复 | `src/orchestrate.py` + `src/run_daily.bat` | **唯一读写者** |
| **Generator** | 抓取 QQQ 价格与 NAV，计算两个参考价与信号，落盘产物 | `src/qqq_valuation.py`（取数+计算部分） | 否 |
| **Critic** | 审查数据来源与截止日期：数据缺失、前视泄露、口径一致性 | `src/validate.py`（Critic-1）+ LLM 终审（Critic-2） | 否 |

核心原则：**Critic 不信任 Generator 的输出**。Critic-1 从原始快照独立重算指标；Critic-2 在全新上下文中只读输入快照、代码、产物和 Critic-1 报告。

## 2. Orchestrator

### 2.1 职责

1. **调度接入**：被任务计划程序（或手动）触发，是每次运行的入口。
2. **确定 asof 日期 T**：运行时刻之前最近的美股交易日；非交易日触发时记 `not_a_trading_day` 正常退出。
3. **编排执行**：按状态机依次调用 Generator、Critic-1、Critic-2，收集退出码与产物路径。
4. **维护 state.json**：在每个阶段边界更新状态（见 §4、§5）；除它之外任何组件不得写 state.json。
5. **恢复决策**：启动时先读 state.json 决定是正常新跑、断点续跑还是补跑（见 §6）。

### 2.2 状态机

```
scheduled ──► fetching ──► computing ──► validating ──► reviewing ──► done
                 │            │              │              │
                 └────────────┴──────┬───────┴──────┬───────┘
                                     ▼              ▼
                                   failed ◄────── Critic 任一层判 fail
```

- `fetching`：Generator 拉取价格 + NAV，**完成后立即把原始快照落盘**（`data/raw/{T}.csv`）——这是断点可恢复的关键，见 §6。
- `computing`：计算 `ma200` / `fair_nav` / 信号，写日志行与当日报告。
- `validating` / `reviewing`：分别跑 Critic-1 / Critic-2。
- `failed`：任一阶段抛异常或 Critic 判 fail；`error` 字段记录失败阶段与信息，产物保留供人工复查。

### 2.3 职责边界（不做什么）

- 不直接抓取或计算市场数据——数据逻辑全部属于 Generator。
- 不做审查判断——审查结论以 Critic 的 verdict 为准，Orchestrator 只搬运与记录。
- 不修改历史产物——所有 `data/`、`report/` 文件只追加或按当日键覆盖，Orchestrator 只负责触发这些写入。

## 3. Generator

### 3.1 职责

1. **取数**
   - 价格：`yfinance` 下载 QQQ 日线 ≥ 1 年（未复权 `Close`，美元，交易所日期）。
   - NAV：`fetch_nav()` 从 Invesco 官网抓取最新公布 NAV 及其公布日期；抓取器可替换。
2. **计算**（公式见 task.md §5）：`ma200_T`、`fair_nav_T = nav_T × (1 + mean(premium_{T-19..T}))`、信号。
3. **落盘产物**：原始快照、NAV 历史追加行、信号日志行、当日报告草稿（路径见 task.md §7）。

### 3.2 硬性约束

- **只用 `date ≤ T` 的数据**；历史回放模式（`--date`）先把数据截断到 T 再计算。这条由 Critic 复核（task.md §6 三道防线之一）。
- **缺失必须显式标记**：收盘价/NAV/窗口不足分别记 `missing_close` / `missing_nav` / `insufficient_history`，当日信号为 `数据缺失`。禁止用旧 NAV 顶替、插值、静默跳过。
- **幂等写入**：信号日志以 `asof_date` 为唯一键（回滚当日旧行后重写）；报告与快照按日期命名，覆盖写安全；所有写操作先写临时文件再原子替换（`os.replace`），避免中断留下半个文件。
- **不碰 state.json**：只通过返回值向 Orchestrator 汇报 `{产物路径, 数据状态, 指标数值}`。

### 3.3 失败行为

取数失败（网络、解析）→ 立即返回结构化错误（失败对象 + 已部分完成的产物路径），由 Orchestrator 记 `failed`；**不允许**降级为"用昨天的数据先算着"。

## 4. Critic

Critic 审查首先覆盖用户指定的两项——**数据来源**与**截止日期**，以及 task.md §6 的缺失处理与前视泄露。分两层实现，互补不替代。

### 4.1 Critic-1：确定性硬断言（`src/validate.py`）

从 `data/raw/{T}.csv` 与 `data/processed/nav_history.csv` **独立重算**全部指标（使用与 Generator 独立的计算代码），然后执行 task.md §4.2 的 6 条断言。与主题的直接对应：

- **数据来源**：NAV 日期 == T 且无重复；行数 ≥ 220；价格数值在合理范围（> 0，单日涨跌幅超 ±20% 时告警供人工复核）。
- **截止日期**：`max(date) ≤ T`；重算指标与 Generator 输出一致（误差 < 1e-6 × 价格）；信号复算一致且严格小于。

输出：退出码（0 = pass）+ `report/{T}.critic1.md`。

### 4.2 Critic-2：LLM 语义终审

全新上下文，只读材料：当日原始快照、两份脚本源码、Generator 产物、Critic-1 报告。审查清单（task.md §4.3）：

- 缺失是否被如实标记，有无用旧值/填充值冒充当日数据；
- 有无未来函数、rolling 窗口越界、merge 日期错位、`--date` 回放时用到 T 之后的数据；
- 口径一致性（均线、当前价、比较同为未复权 `Close`；单位美元）；
- 报告文字与数值、信号是否相符。

输出：verdict（pass / fail + 问题清单）写入 `report/{T}.critic2.md`。

### 4.3 失败行为与可用性

- 任一层判 fail → Orchestrator 置 `failed`，当日信号作废（`validation_failed`），产物全部保留。
- Critic-2（LLM）不可用（无会话、配额耗尽）：状态停在 `reviewing`，当日报告标注"LLM 终审未执行，信号仅供参考"，待人工在会话中补审；**不伪造通过**。

## 5. state.json 契约

### 5.1 Schema

```json
{
  "version": 1,
  "run_id": "2026-06-20T06:00:01+08:00",
  "asof_date": "2026-06-19",
  "status": "fetching",
  "checkpoint": {
    "raw_snapshot_written": true,
    "log_written": false,
    "report_written": false,
    "critic1": "pending",
    "critic2": "pending"
  },
  "inputs": {
    "price_source": "yfinance",
    "price_retrieved_at": "2026-06-20T06:00:02+08:00",
    "price_rows": 252,
    "price_max_date": "2026-06-19",
    "nav_source": "invesco.com",
    "nav_retrieved_at": "2026-06-20T06:00:03+08:00",
    "nav_date": "2026-06-19",
    "nav_value": 512.34
  },
  "outputs": {
    "raw_snapshot": "data/raw/2026-06-19.csv",
    "report": null,
    "critic1_report": null,
    "critic2_report": null
  },
  "validation": { "critic1": "pending", "critic2": "pending" },
  "last_success": {
    "run_id": "2026-06-19T06:00:00+08:00",
    "asof_date": "2026-06-18",
    "report": "report/2026-06-18.md"
  },
  "error": null,
  "updated_at": "2026-06-20T06:00:03+08:00"
}
```

### 5.2 字段说明

| 字段 | 用途 |
|------|------|
| `run_id` | 带时区的时间戳，全局唯一，日志行与之关联 |
| `asof_date` | 本次信号日 T（ET 交易日） |
| `status` | 状态机当前阶段；恢复逻辑的第一判据 |
| `checkpoint` | 细粒度断点：产物写到了哪、哪层 Critic 还没跑 |
| `inputs` | **数据来源与抓取时间戳的审计记录**，Critic 审查"来源与截止日期"的对照依据 |
| `outputs` | 各产物路径；`null` 表示尚未产出 |
| `validation` | 两层 Critic 各自 verdict（pending / pass / fail） |
| `last_success` | 上次完整通过的运行指针；本次失败时它保留不动，人工取数仍可用 |
| `error` | `failed` 时填写：`{phase, message, at}` |

### 5.3 写入协议

1. **独占写**：只有 Orchestrator 写 state.json；Generator/Critic 通过返回值汇报。
2. **阶段边界更新**：进入每个阶段时更新 `status`，阶段内关键产物落盘后立即更新对应 `checkpoint` 与 `outputs`。
3. **原子写**：先写 `.state/state.json.tmp` 再 `os.replace`，中断不会留下半个 JSON。
4. **元数据定位**：state.json 只存元数据与产物指针，**市场数据真相在 `data/` 文件里**——state.json 可从产物重建（见 §6.4）。

## 6. 中断与恢复

### 6.1 中断场景

| 场景 | 典型断点 | 后果 |
|------|---------|------|
| 网络失败 / yfinance、Invesco 不可达 | `fetching` | 无产物或快照未写完 |
| 进程崩溃 / 机器断电 / 任务计划被杀 | 任意 | 以 state.json 最后落盘阶段为准 |
| Critic 判 fail | `validating` / `reviewing` | 产物齐，信号作废 |
| 磁盘满 / 权限错误 | 任意 | 写入抛异常 |
| 任务计划错过触发（关机）| — | 缺一个或多个交易日的运行 |

### 6.2 恢复决策表（Orchestrator 启动时先读 state.json）

| 读到的状态 | 判定 | 动作 |
|-----------|------|------|
| 无 state.json | 首次运行 | 全量新跑 |
| `status=done` 且 `asof_date` = 最新交易日 | 今日已完成 | 幂等退出（不重复写日志） |
| `status=done` 但 `asof_date` < 最新交易日 | 漏跑 | 进入补跑（§6.3） |
| `fetching` / `computing` | Generator 中断 | 快照已落盘（`checkpoint.raw_snapshot_written=true`）→ **复用快照重算**，保证复现性、不重抓（源数据可能已变化）；快照不在 → 从取数重来 |
| `validating` / `reviewing` | Critic 中断 | Generator 产物已齐，**不重跑 Generator**，只重跑 `validation` 中为 `pending` 的 Critic 层 |
| `failed` | 上次失败 | `--resume` 按 `checkpoint` 续跑；对同一 `asof_date` 重跑前先回滚当日日志行（幂等键） |
| state.json 损坏（解析失败） | 未知状态 | 对账模式（§6.4），禁止静默删除任何产物 |

### 6.3 幂等与补跑（catch-up）

- **日志幂等**：`data/processed/qqq_daily_log.csv` 以 `asof_date` 唯一；重跑某日前先删该日旧行再追加，中断重复执行不会产生重复行。
- **快照与报告按日命名**：覆盖写天然幂等。
- **补跑**：对比交易日历与日志中已有的 `asof_date` 集合，找出缺失交易日逐个补跑；每个补跑日都是独立 `run_id`、走完整流水线。防止节假日误判：脚本内嵌交易所日历校验，非交易日不生成任务。
- **Orchestrator 自身中断**：因为它只在阶段边界写 state，最坏情况是某阶段重复执行一次——由于上述幂等机制，重复执行是安全的。

### 6.4 对账模式（state.json 损坏或丢失）

state.json 是恢复真相，但不是数据真相。损坏时：

1. 从 `data/processed/qqq_daily_log.csv` 推导出已完成的 `asof_date` 集合与对应信号；
2. 从 `data/raw/` 与 `report/` 核对产物完整性；
3. 重建一份最小 state.json（`status` 置为需要人工确认的状态，标记 `rebuilt: true`）；
4. 人工确认后按 §6.2 决策表继续。**全程不删除、不修改任何历史产物。**

### 6.5 恢复后的验证清单

- [ ] `data/processed/qqq_daily_log.csv` 中每个 `asof_date` 唯一且与交易日历对齐、无缺口
- [ ] 当日（或补跑日）`report/{T}.md`、`{T}.critic1.md`、`{T}.critic2.md` 齐备
- [ ] `state.json` 的 `status=done`、`validation` 两层均为 `pass`、`last_success` 指向最新成功报告
- [ ] 若曾判 `failed`：`error` 字段的问题已修复，重跑后该日信号已更新

### 6.6 手动操作命令

```bash
python src/orchestrate.py                  # 日常：自动读 state.json 决策
python src/orchestrate.py --resume         # 断点续跑
python src/orchestrate.py --redo 2026-06-19  # 强制重跑某日（先回滚当日日志行）
python src/orchestrate.py --catch-up       # 补跑所有缺失交易日
python src/orchestrate.py --status         # 打印当前 state.json
```

## 7. 防泄露与降级

### 7.1 原始快照全量留档,防泄露校验在加工与计算环节执行

- `data/raw/asof-<日期>/` 是抓数时刻的完整留档,原样保存、禁止修改。数据源返回的原始序列允许包含晚于 asof 的观测——典型如蛋卷 PE 周度序列,抓数时刻必然已含 asof 之后的周度观测。这类记录属于留档的一部分,**不视为违规**:checker 不再据此记"未来数据"issue,也不得为"消除违规"而删改快照。
- 防泄露闸门设在消费端,共两道,均在聚合/计算之前执行:
  1. **加工环节**(`src/fetch_data.py`):价格与 PE 序列一律先按 `date <= asof` 截断,PE 截断后才做月度聚合(取每月最后一个 ≤ asof 的观测);`assert_no_future_dates` 断言兜底,触发即 raise,宁可停线不放行。
  2. **计算环节**(`src/calculate.py`):加载原始快照时同样按 `date <= asof` 截断,截断后才参与均线与 PE 月度聚合。
- 因此 checker 的防泄露校验对象是**加工产物与计算实际使用的序列**,而非留档本身:
  - `data/processed/qqq_daily.csv` 不得含晚于 asof 的交易日,其 PE 列须与"截断至 asof 后独立重算"的月度序列逐月一致(不一致即说明有越界观测混入产物);
  - 计算实际使用的 PE 月度序列不得含晚于 asof 的月份。
- 仅当 raw 快照中发现晚于 asof 的记录时,checker 须额外静态核实上述两道闸门仍然存在(`tests/checker.py` 的 `find_missing_consumer_gates`);闸门缺失而 raw 有越界记录时,仍按"未来数据"记 issue。

### 7.2 降级规则

PE(TTM) 为 n/a 时,最终信号降级为"两个均线信号同时满足才输出建议关注",并须在估值 CSV 等处明确标注降级原因,严禁静默降级(口径见 task.md §1、§5)。降级是否如实执行由 checker 独立重算复核(检查项 c)。

## 8. 与 task.md 的对应关系

| 设计元素 | task.md 出处 |
|---------|-------------|
| 指标公式与信号规则 | §5 |
| 数据缺失与前视三道防线 | §6 |
| 目录结构与只追加约定 | §7 |
| Critic-1 六条断言、Critic-2 审查清单 | §4.2、§4.3 |
| 调度（任务计划程序 06:00）| §4.4 |
| 验收测试 | §9 |

本文档中的 `.state/state.json` 即 task.md §8 定义的"运行状态文件"（task.md 中曾用名 `last_run.json`，以本文档的 `state.json` 为准）。
