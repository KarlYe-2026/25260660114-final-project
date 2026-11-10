# 生产调度：Windows 任务计划程序每日触发

> 本文档说明生产环境如何让流水线在每个美股交易日收盘后自动运行，以及本作业如何用手动回跑演示重复触发。

## 1. 触发时机

- 美股常规交易时段收盘为东部时间 16:00，对应北京时间 **04:00（夏令时）/ 05:00（冬令时）**；架构设计（`docs/design.md` §1）定为每日 **06:00（北京时间）** 触发，收盘后数据已可得。
- Windows 任务计划程序无法表达美股交易日历，有两种可选方案：
  1. **每日 06:00 触发**（设计口径）：周末与美股节假日也会触发，无新数据的日期由流水线显式处理——取不到当日数据即报错或标 n/a，绝不造假（`docs/plans/task.md` §4、§5）；
  2. **仅周一至周五触发**：减少无效运行，但同样无法排除美股节假日。
- `src/main.py` 必须显式传入 `--asof`，因此计划任务不直接调用 main.py，而是调用下面的包装脚本，由它计算候选日期后触发流水线。

## 2. 包装脚本 `scripts/run_daily.ps1`（生产环境入口）

```powershell
# scripts/run_daily.ps1 —— 任务计划程序的唯一入口
$ErrorActionPreference = 'Stop'
$root = 'D:\KIMI 代码练习\aicdemo\FINAL PROJECT\25260660114-final-project'
Set-Location $root

# 06:00 运行时取当天为候选 asof；周末（周六/周日触发）回退到最近一个工作日
$d = Get-Date
while ($d.DayOfWeek -in 'Saturday', 'Sunday') { $d = $d.AddDays(-1) }
$asof = $d.ToString('yyyy-MM-dd')

# 数据截止约束与重复触发安全：
#   - main.py 内部各阶段均按 date <= asof 截断（防未来数据）；
#   - 当日重复触发安全：产物按日期命名覆盖写，.state/state.json 原子覆写；
#   - 如需断点续跑语义，可改为: python src\main.py --asof $asof --resume
python src\main.py --asof $asof
exit $LASTEXITCODE
```

## 3. 注册计划任务：`schtasks /create`（PowerShell 语法）

以管理员或普通 PowerShell 会话执行（每日 06:00，覆盖已存在的同名任务）：

```powershell
schtasks /create /tn 'QQQ-Daily-Valuation' /tr 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\KIMI 代码练习\aicdemo\FINAL PROJECT\25260660114-final-project\scripts\run_daily.ps1"' /sc daily /st 06:00 /f
```

仅工作日（周一至周五）触发的变体：

```powershell
schtasks /create /tn 'QQQ-Daily-Valuation' /tr 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\KIMI 代码练习\aicdemo\FINAL PROJECT\25260660114-final-project\scripts\run_daily.ps1"' /sc weekly /d MON,TUE,WED,THU,FRI /st 06:00 /f
```

参数说明：`/tn` 任务名；`/tr` 要运行的命令（单引号内嵌双引号包住含空格的中文路径）；`/sc daily|weekly` 触发频率；`/st 06:00` 每天开始时间（北京时间）；`/d` 每周触发时的星期几；`/f` 强制覆盖同名任务。如需指定运行账户，可加 `/ru 用户名 [/rp 密码]`。

## 4. 验证与管理

```powershell
# 查看任务详情（状态、下次运行时间、触发器）
schtasks /query /tn 'QQQ-Daily-Valuation' /v /fo LIST

# 手动触发一次（对应 task.md 端到端场景：定时任务手动触发、五类产物齐备）
schtasks /run /tn 'QQQ-Daily-Valuation'

# 删除任务
schtasks /delete /tn 'QQQ-Daily-Valuation' /f
```

## 5. 本作业的演示方式：`--asof` 手动回跑历史日期

本作业不等待真实交易日逐日到来，而是对**历史交易日**用 `--asof` 多次触发同一流水线，演示"重复触发 / 历史回跑"行为：

```bash
# 对同一历史交易日重复触发：产物按日期覆盖写、状态原子覆写，重复执行安全
python src/main.py --asof 2026-11-04
python src/main.py --asof 2026-11-05
python src/main.py --asof 2026-11-06
```

每日产物落盘 `output/tables/valuation-asof-<日期>.csv`，运行状态留痕于 `.state/state.json`，多日信号汇总见 `output/signal-history.md`。回跑与生产触发走同一条代码路径，同样受信息截止约束（数据先截断至 asof 再计算），因此回跑结果与"当日真实触发"在方法上等价。
