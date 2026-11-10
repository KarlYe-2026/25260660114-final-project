# 验收核验记录

> 本文所有命令输出均粘贴自 2026-11-10 22:24（本地时间）本会话中的实际运行结果，未做任何改写。

## 1. 防未来数据拦截

命令：`python tests/checker.py --asof 2026-11-06`（exit=0）。checker 从原始快照独立重算三个信号，并核验加工产物与计算实际使用的序列中无晚于 asof 的观测。

```text
[checker] 提示: raw 快照含 1 条晚于 asof=2026-11-06 的原始观测(抓数时刻完整留档,不视为违规);消费端闸门核实通过,继续校验加工产物无越界。
[checker] asof=2026-11-06 审查通过:三个信号及降级规则独立重算一致,加工产物与计算实际使用的 PE 月度序列无晚于 asof 的观测。
exit=0
```

证明：留档中晚于 asof 的 1 条原始观测（2026-11-08 PE）被正确识别为"抓数时刻完整留档"，消费端闸门（`src/fetch_data.py`、`src/calculate.py` 的 `date <= asof` 截断）经静态核实存在，且产物无任何越界数据，审查通过。

`.state/issues.json` 中 pit-001 的关闭记录（原文引用）：

```json
{
  "id": "pit-001",
  "asof": "2026-11-06",
  "category": "未来数据",
  "severity": "error",
  "status": "resolved",
  "resolution": "raw 按政策保留原样,泄露风险已由消费端闸门覆盖",
  "resolved_at": "2026-11-10",
  "verified_facts": [
    "raw PE 快照含 1 条晚于 asof 的观测:2026-11-08(PE 26.9687),系抓数时刻完整留档,按政策不再视为违规",
    "2026-11 月度 PE 实际取自 2026-11-01 观测 26.9833(最后一个 <= asof 的观测),11-08 观测未进入任何产物",
    "data/processed/qqq_daily.csv 最大日期 2026-11-06,无晚于 asof 的行;11 月各交易日 pe_ttm=26.9833",
    "output/tables/valuation-asof-2026-11-06.csv 当前 PE=26.98,与截断后序列一致,产物未受污染"
  ]
}
```

## 2. 两轮 Critic 审查

Critic-1（确定性硬断言，`tests/checker.py`）本轮输出即 §1 代码块（同一命令、同一次运行，exit=0）：三个信号及降级规则独立重算一致，产物与独立重算逐项相符。

命令：`git log --oneline`（节选与 Critic/审查机制相关的提交行，原文粘贴）：

```text
083fbc3 fix: checker改为检查消费端序列，关闭pit-001/002/003（含负向测试验证）
ca312ef feat: Controller状态机、哈希校验、断点恢复与Hook验证
2e1dd05 feat: 多层级Subagent架构、参数集中配置与Hook
```

说明：git 提交信息中没有字面含 "Critic" 的提交；以上三行是 Critic 审查机制（checker 独立重算、pit 关闭、Controller 编排）对应的真实提交。Critic-2（LLM 语义终审）的审查记录见 `agents/critic_lead.md` 与每次运行的人读报告。

## 3. 断点恢复

命令：`python src/main.py --asof 2026-11-04 --resume`（exit=0）。main.py 先重算已完成阶段产物文件的 SHA256 并与 `.state/state.json` 记录比对：

```text
[main] 阶段 fetch_data 已核验，跳过
[main] 阶段 calculate 已核验，跳过
[main] 阶段 checker 已核验，跳过
[main] 全部阶段此前均已完成且核验一致,无需补做: case_id=b8e282dd6a4f6f03, asof=2026-11-04
exit=0
```

证明：三个阶段的产物哈希与状态记录完全一致，断点恢复按设计"已核验，跳过"，未重复执行任何阶段。

## 4. 基线对比与回滚

命令：`git log --oneline`（与回滚相关的提交行，原文粘贴）：

```text
00cc9a7 rollback: 经人工批准回滚至v1.0并恢复基线产物
5ba8c8b docs: 补充参数演进审批记录与回滚测试证据
aa28fba demo: 三日连续回跑与信号历史
```

说明：git 提交信息中没有字面含 "test" 的提交；`00cc9a7` 为 rollback 提交，`5ba8c8b` 为回滚测试证据提交（`docs/notes/recovery.md`），如实列出。

基线对比产物 `docs/notes/evolution-threshold3-2026-11-04.csv`（参数演进实验：`min_signals_required` 由 2 改为 3，原文引用）：

```csv
信号,数值,阈值,判定条件,是否满足,备注
收盘价 < MA200(ma_window_1),691.95,680.56,收盘价 < 200 日均线,否,
收盘价 < MA50(ma_window_2),691.95,723.56,收盘价 < 50 日均线,是,
PE(TTM) < 历史 PE 中位数,26.98,30.58,当前 PE(TTM) < 截至 asof 历史月度 PE 中位数,是,
最终信号,观望,min_signals_required=3,满足信号数 ≥ 3,,
```

同一交易日（2026-11-04）在基线参数 `min_signals_required=2` 下的信号为"建议关注"（见 `output/tables/valuation-asof-2026-11-04.csv` 与 `output/signal-history.md`）；参数改为 3 后满足信号数 2 < 3，信号变为"观望"，与 `docs/notes/approval.md` 的人工审批记录一致，回滚提交 `00cc9a7` 已恢复基线产物。

## 5. Hook 审计日志

命令：`cat .state/audit.log`（实际输出，文件不存在）：

```text
cat: .state/audit.log: No such file or directory
exit=1
```

Hook 配置真实存在（`hooks.json` 原文）：

```json
{"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python audit_event.py"}]}]}}
```

Hook 已配置但宿主未加载，审计由 state.json 与 git 历史代替。
