#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checker.py — checker-agent 独立重算审查脚本。

用法:
    python tests/checker.py --asof 2026-11-06

职责(只读产物,绝不修改 data/ 与 output/ 下任何文件):
    不引用 src/calculate.py,独立读取 config/params.json、
    data/raw/asof-<日期>/ 原始快照、data/processed/qqq_daily.csv
    与 output/tables/valuation-asof-<日期>.csv,
    独立重算三个低估信号及降级规则,并与产物逐项比对。
    发现以下问题即将其写入 .state/issues.json(按 pit-001 起递增编号,
    注明受影响文件)并 sys.exit(1):
      a) 产物数值/阈值/是否满足/最终信号与独立重算对不上(数值不符);
      b) 加工产物 data/processed/qqq_daily.csv 存在晚于 asof 的交易日、
         其 PE 列与"截断至 asof 后独立重算"的月度序列不一致,
         或计算实际使用的 PE 月度序列含晚于 asof 的月份(未来数据);
      c) 产物 PE 为 n/a 却未按降级规则执行,或独立重算 PE 为 n/a 而产物未降级。
    全部通过则打印审查结论并 exit(0)。

防泄露政策(2026-11-10 起):
    原始快照 data/raw/ 是抓数时刻的完整留档,允许含晚于 asof 的原始观测
    (如蛋卷 PE 周度序列必然包含 asof 之后的数据),不再视为违规、不得修改;
    但发现越界记录时,须静态核实消费端闸门存在(src/fetch_data.py 加工前、
    src/calculate.py 加载时均按 <= asof 截断),且加工产物与计算实际使用的
    序列无越界。防泄露校验的执行点在加工与计算环节,而非留档环节。

口径(与 calc-agent 的约定一致,此处为独立实现,不复用其代码):
    - 均线为收盘价简单移动平均,窗口含当日,需满窗才取值;
    - asof 非交易日时,取 asof 之前最近一个交易日的收盘价为"当日收盘价";
    - PE(TTM):周度序列截断至 asof,取每月最后一个观测为该月 PE;
      当前 PE 取评估交易日所属月份的月度值,该月无观测即为 n/a;
      历史中位数 = 截至 asof 全部月度 PE 值的中位数;
    - 降级规则:当前 PE(TTM) 为 n/a 时,两个均线信号同时满足才输出
      "建议关注",并标注 "PE维度缺失，已启用降级规则";
    - 最终信号:满足信号数 >= min_signals_required 为 "建议关注",否则 "观望"。
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PARAMS_JSON = "config/params.json"
PRICE_RAW = "raw_sfd_qqq_price.csv"
PE_RAW = "raw_danjuan_ndx_pe.json"
DAILY_CSV = "data/processed/qqq_daily.csv"
OUT_DIR = "output/tables"
OUT_TMPL = "valuation-asof-{asof}.csv"
ISSUES_JSON = ".state/issues.json"

# 消费端防泄露闸门的静态核实要求:加工(fetch_data.py)与计算(calculate.py)
# 环节均须在聚合/使用前按 <= asof 截断价格与 PE;fetch_data.py 另保留断言闸门兜底。
GATE_REQUIREMENTS = {
    "src/fetch_data.py": [("assert_no_future_dates", 1), ("<= pd.Timestamp(asof)", 2)],
    "src/calculate.py": [("<= pd.Timestamp(asof)", 2)],
}

WATCH = "建议关注"
WAIT = "观望"
DEGRADED_NOTE = "PE维度缺失，已启用降级规则"

# 产物数值保留两位小数,允许半个末位单位的舍入差
TOL = 0.005 + 1e-9

SATISFIED_MAP = {"是": True, "否": False, "n/a": None}


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


class CheckerError(Exception):
    """输入/环境层面错误(缺文件、格式不符),同样记 issue 并退出。"""

    def __init__(self, message: str, files=None):
        super().__init__(message)
        self.files = list(files or [])


class Issues:
    """问题收集器:写入 .state/issues.json,id 按 pit-001 起递增,不覆盖历史记录。"""

    def __init__(self, asof_str: str):
        self.asof = asof_str
        self.existing = []
        p = PROJECT_ROOT / ISSUES_JSON
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("issues"), list):
                    self.existing = data["issues"]
            except json.JSONDecodeError:
                self.existing = []
        nums = []
        for i in self.existing:
            m = re.fullmatch(r"pit-(\d+)", str(i.get("id", "")))
            if m:
                nums.append(int(m.group(1)))
        self.next_n = (max(nums) if nums else 0) + 1
        self.new = []

    def add(self, category: str, message: str, files) -> None:
        iid = f"pit-{self.next_n:03d}"
        self.next_n += 1
        self.new.append({
            "id": iid,
            "asof": self.asof,
            "category": category,
            "severity": "error",
            "message": message,
            "affected_files": [str(f) for f in files],
        })

    def save(self) -> None:
        p = PROJECT_ROOT / ISSUES_JSON
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"issues": self.existing + self.new}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_asof(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise CheckerError(f"--asof 必须是 YYYY-MM-DD 格式,收到: {s!r}")


def load_params() -> dict:
    p = PROJECT_ROOT / PARAMS_JSON
    if not p.exists():
        raise CheckerError(f"缺少参数文件 {PARAMS_JSON}", [rel(p)])
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for k in ("ma_window_1", "ma_window_2", "min_signals_required"):
        if k not in cfg:
            raise CheckerError(f"{PARAMS_JSON} 缺少字段 {k!r}", [rel(p)])
    return cfg


def read_prices(raw_dir: Path) -> pd.DataFrame:
    """读取原始价格 CSV 全部行(含 asof 之后)。

    raw 快照是抓数时刻的完整留档,晚于 asof 的记录不单独记 issue,
    仅用于统计越界观测并触发消费端闸门核实;独立重算时再截断至 asof。
    """
    f = raw_dir / PRICE_RAW
    if not f.exists():
        raise CheckerError(f"缺少原始价格文件 {rel(f)}", [rel(f)])
    df = pd.read_csv(f)
    for col in ("time", "close"):
        if col not in df.columns:
            raise CheckerError(f"{rel(f)} 缺少字段 {col!r},插件返回格式可能已变化", [rel(f)])
    df["date"] = pd.to_datetime(df["time"].astype(str), format="%Y%m%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")
    df = df.drop_duplicates(subset="date", keep="last")
    return df[["date", "close"]].reset_index(drop=True)


def read_pe(raw_dir: Path) -> pd.DataFrame:
    """读取蛋卷原始 PE JSON 全部观测(含 asof 之后)。

    与 read_prices 同理:raw 为完整留档,越界观测只用于统计与闸门核实。
    """
    f = raw_dir / PE_RAW
    if not f.exists():
        raise CheckerError(f"缺少原始 PE 文件 {rel(f)}", [rel(f)])
    payload = json.loads(f.read_text(encoding="utf-8"))
    rows = (payload.get("data") or {}).get("index_eva_pe_growths") or []
    recs = []
    for r in rows:
        ts, pe = r.get("ts"), r.get("pe")
        if ts is None or pe is None:
            continue
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        recs.append((pd.Timestamp(d), float(pe)))
    df = pd.DataFrame(recs, columns=["date", "pe_ttm"])
    return df.sort_values("date").reset_index(drop=True)


def find_missing_consumer_gates() -> list:
    """静态核实消费端防泄露闸门,返回缺失项列表(空列表 = 闸门齐备)。

    raw 快照允许含晚于 asof 的观测(抓数时刻完整留档);只要 fetch_data.py
    加工前、calculate.py 加载时均按 <= asof 截断价格与 PE,越界记录就到不了
    加工产物与计算。
    """
    missing = []
    for fname, pats in GATE_REQUIREMENTS.items():
        p = PROJECT_ROOT / fname
        if not p.exists():
            missing.append(f"{fname} 不存在")
            continue
        text = p.read_text(encoding="utf-8")
        for pat, min_count in pats:
            n = text.count(pat)
            if n < min_count:
                missing.append(f"{fname} 中 {pat!r} 出现 {n} 次(应 ≥ {min_count})")
    return missing


def check_output(out_csv: Path, issues: Issues, expect: dict) -> None:
    """读取产物 CSV,逐项与独立重算结果比对。"""
    if not out_csv.exists():
        raise CheckerError(f"缺少产物文件 {rel(out_csv)}", [rel(out_csv)])
    out = pd.read_csv(out_csv, dtype=str, encoding="utf-8-sig").fillna("")
    for col in ("信号", "数值", "阈值", "判定条件", "是否满足", "备注"):
        if col not in out.columns:
            raise CheckerError(f"{rel(out_csv)} 缺少列 {col!r}", [rel(out_csv)])

    out_rel = rel(out_csv)

    def find_row(key: str):
        hit = out[out["信号"].str.contains(key, regex=False, na=False)]
        if len(hit) != 1:
            issues.add("产物格式异常",
                       f"{out_rel} 中匹配 {key!r} 的行数为 {len(hit)}(应为 1)",
                       [out_csv])
            return None
        return hit.iloc[0]

    def cmp_num(row, field: str, expected: float, label: str) -> None:
        got = str(row[field]).strip()
        try:
            gv = float(got)
        except ValueError:
            issues.add("数值不符",
                       f"{label}:产物{field}={got!r} 非数值,独立重算为 {expected:.4f}",
                       [out_csv])
            return
        if abs(gv - expected) > TOL:
            issues.add("数值不符",
                       f"{label}:产物{field}={gv},独立重算 {expected:.4f}(差 {gv - expected:+.4f})",
                       [out_csv])

    def cmp_na_or_num(row, field: str, expected, label: str) -> None:
        """expected 为 None 时产物应写作 n/a,否则按数值比对。"""
        got = str(row[field]).strip()
        if expected is None:
            if got.lower() != "n/a":
                issues.add("数值不符",
                           f"{label}:独立重算为 n/a,产物{field}={got!r}",
                           [out_csv])
        else:
            cmp_num(row, field, expected, label)

    def cmp_satisfied(row, expected, label: str) -> None:
        got = str(row["是否满足"]).strip()
        if SATISFIED_MAP.get(got, "<?>") is not expected:
            issues.add("数值不符",
                       f"{label}:产物是否满足={got!r},独立重算应为 "
                       f"{'n/a' if expected is None else ('是' if expected else '否')}",
                       [out_csv])

    # ---- 三个信号行 ----
    for key, tag, sig in ((f"MA{expect['w1']}", f"MA{expect['w1']}", "sig_ma1"),
                          (f"MA{expect['w2']}", f"MA{expect['w2']}", "sig_ma2")):
        row = find_row(key)
        if row is None:
            continue
        cmp_num(row, "数值", expect["close"], f"{tag} 信号(收盘价)")
        cmp_num(row, "阈值", expect[f"ma_{tag}"], f"{tag} 信号(阈值)")
        cmp_satisfied(row, expect[sig], tag)

    row_pe = find_row("PE(TTM)")
    if row_pe is not None:
        cmp_na_or_num(row_pe, "数值", expect["pe_current"], "PE(TTM) 信号(当前 PE)")
        cmp_na_or_num(row_pe, "阈值", expect["pe_median"], "PE(TTM) 信号(历史中位数)")
        cmp_satisfied(row_pe, expect["sig_pe"], "PE(TTM) 信号")

    # ---- 降级规则检查(c) ----
    out_pe_na = row_pe is not None and str(row_pe["数值"]).strip().lower() in ("n/a", "na", "")
    if expect["degraded"] and not out_pe_na:
        issues.add("降级规则未执行",
                   f"独立重算当前 PE(TTM) 为 n/a,产物却给出数值 "
                   f"{str(row_pe['数值']).strip()!r},未按降级规则执行",
                   [out_csv])
    if out_pe_na and not expect["degraded"]:
        issues.add("降级规则未执行",
                   f"产物 PE(TTM) 标为 n/a,但独立重算当前 PE={expect['pe_current']:.4f},"
                   f"降级标注与数值矛盾",
                   [out_csv])
    if out_pe_na:
        # 产物自称 PE 为 n/a,必须完整执行降级规则
        if row_pe is not None and DEGRADED_NOTE not in str(row_pe["备注"]):
            issues.add("降级规则未执行",
                       f"产物 PE 为 n/a,但 PE 行备注缺少 {DEGRADED_NOTE!r} 标注",
                       [out_csv])
        if row_pe is not None and str(row_pe["是否满足"]).strip() != "n/a":
            issues.add("降级规则未执行",
                       f"产物 PE 为 n/a,但 PE 行是否满足={str(row_pe['是否满足']).strip()!r}(应为 n/a)",
                       [out_csv])

    # ---- 最终信号行 ----
    row_fin = find_row("最终信号")
    if row_fin is not None:
        got_final = str(row_fin["数值"]).strip()
        if got_final != expect["final"]:
            cat = "降级规则未执行" if out_pe_na else "数值不符"
            issues.add(cat,
                       f"最终信号:产物={got_final!r},独立重算={expect['final']!r}",
                       [out_csv])
        if expect["degraded"] or out_pe_na:
            if "降级" not in str(row_fin["判定条件"]):
                issues.add("降级规则未执行",
                           "PE 为 n/a,但最终信号行判定条件未说明降级规则",
                           [out_csv])
            if DEGRADED_NOTE not in str(row_fin["备注"]):
                issues.add("降级规则未执行",
                           f"PE 为 n/a,但最终信号行备注缺少 {DEGRADED_NOTE!r} 标注",
                           [out_csv])


def main() -> None:
    ap = argparse.ArgumentParser(description="checker-agent 独立重算审查(只读产物)")
    ap.add_argument("--asof", required=True, help="评估截止日,YYYY-MM-DD(含当天)")
    args = ap.parse_args()

    issues = Issues(args.asof)

    try:
        asof = parse_asof(args.asof)
        cfg = load_params()
        w1 = int(cfg["ma_window_1"])
        w2 = int(cfg["ma_window_2"])
        min_req = int(cfg["min_signals_required"])

        raw_dir = PROJECT_ROOT / "data" / "raw" / f"asof-{asof}"
        if not raw_dir.is_dir():
            raise CheckerError(f"原始数据目录 {rel(raw_dir)} 不存在", [rel(raw_dir)])
        price_file = raw_dir / PRICE_RAW
        pe_file = raw_dir / PE_RAW
        out_csv = PROJECT_ROOT / OUT_DIR / OUT_TMPL.format(asof=asof)

        # ---- 未来数据检查(b) ----
        # raw 快照(data/raw/)是抓数时刻的完整留档,允许含晚于 asof 的原始观测
        # (如蛋卷 PE 周度序列必然包含 asof 之后的数据),不视为违规;
        # 但发现越界记录时,须确认消费端闸门存在、加工产物无越界。
        prices_all = read_prices(raw_dir)
        pe_all = read_pe(raw_dir)
        n_future_raw = int((prices_all["date"] > pd.Timestamp(asof)).sum()) \
            + int((pe_all["date"] > pd.Timestamp(asof)).sum())
        if n_future_raw:
            missing_gates = find_missing_consumer_gates()
            if missing_gates:
                issues.add("未来数据",
                           f"raw 快照存在 {n_future_raw} 条晚于 asof={asof} 的原始观测,且消费端闸门核实失败: "
                           f"{'; '.join(missing_gates)};无法保证加工与计算环节已截断,请人工核查",
                           [price_file, pe_file])
            else:
                print(f"[checker] 提示: raw 快照含 {n_future_raw} 条晚于 asof={asof} 的原始观测"
                      f"(抓数时刻完整留档,不视为违规);消费端闸门核实通过,继续校验加工产物无越界。")

        # 加工产物:data/processed/qqq_daily.csv 不得含晚于 asof 的交易日
        daily_csv = PROJECT_ROOT / DAILY_CSV
        if not daily_csv.exists():
            raise CheckerError(f"缺少加工产物 {rel(daily_csv)}(请先运行 src/fetch_data.py)",
                               [rel(daily_csv)])
        daily = pd.read_csv(daily_csv)
        for col in ("date", "close", "pe_ttm"):
            if col not in daily.columns:
                raise CheckerError(f"{rel(daily_csv)} 缺少字段 {col!r}", [rel(daily_csv)])
        daily["date"] = pd.to_datetime(daily["date"])
        future_daily = daily[daily["date"] > pd.Timestamp(asof)]
        if len(future_daily):
            issues.add("未来数据",
                       f"{rel(daily_csv)} 存在 {len(future_daily)} 条晚于 asof={asof} 的交易日"
                       f"(最大日期 {future_daily['date'].max().date()}),加工产物越界",
                       [daily_csv])

        # ---- 独立重算:价格与均线(窗口含当日,需满窗) ----
        px = prices_all[prices_all["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
        if len(px) < max(w1, w2):
            raise CheckerError(
                f"asof={asof} 之前仅 {len(px)} 个交易日收盘价,不足最长均线窗口 {max(w1, w2)} 日",
                [price_file])
        close = float(px["close"].iloc[-1])
        eval_date = px["date"].iloc[-1].date()
        ma1 = float(px["close"].rolling(w1).mean().iloc[-1])
        ma2 = float(px["close"].rolling(w2).mean().iloc[-1])
        sig_ma1 = close < ma1
        sig_ma2 = close < ma2

        # ---- 独立重算:PE(TTM) 月度聚合、当前值与历史中位数 ----
        pe_df = pe_all[pe_all["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
        if len(pe_df):
            monthly = (pe_df.assign(month=pe_df["date"].values.astype("datetime64[M]"))
                            .groupby("month", as_index=False)["pe_ttm"].last())
        else:
            monthly = pd.DataFrame(columns=["month", "pe_ttm"])

        # 计算实际使用的 PE 月度序列不得含晚于 asof 的月份
        if len(monthly) and monthly["month"].max() > pd.Timestamp(asof).replace(day=1):
            issues.add("未来数据",
                       f"计算实际使用的 PE 月度序列含晚于 asof={asof} 的月份"
                       f"(最大月份 {monthly['month'].max().date()})",
                       [pe_file])

        # 加工产物 PE 列与独立重算(截断至 asof)的月度序列逐月比对:
        # 若产物混入晚于 asof 的观测(如未截断就聚合),数值必然对不上
        if len(daily):
            recomputed = ({pd.Timestamp(m): float(v)
                           for m, v in zip(monthly["month"], monthly["pe_ttm"])}
                          if len(monthly) else {})
            bad = []
            for m, grp in daily.groupby(daily["date"].values.astype("datetime64[M]")):
                exp = recomputed.get(pd.Timestamp(m))
                for v in grp["pe_ttm"].astype(str).str.strip().unique():
                    if exp is None:
                        ok = v.lower() == "n/a"
                    else:
                        try:
                            ok = abs(float(v) - exp) <= TOL
                        except ValueError:
                            ok = False
                    if not ok:
                        bad.append((pd.Timestamp(m).date(), v, exp))
            if bad:
                details = "; ".join(
                    f"{m} 产物={v} 重算={'n/a' if e is None else format(e, '.4f')}"
                    for m, v, e in bad[:5])
                issues.add("未来数据",
                           f"{rel(daily_csv)} 的 PE(TTM) 列与独立重算(截断至 asof)不一致,"
                           f"疑似混入晚于 asof 的观测: {details}",
                           [daily_csv])

        eval_month = pd.Timestamp(eval_date).replace(day=1)
        hit = monthly[monthly["month"] == eval_month]
        pe_current = float(hit["pe_ttm"].iloc[0]) if len(hit) else None
        pe_median = float(median(monthly["pe_ttm"].tolist())) if len(monthly) else None

        # ---- 信号判定与降级规则 ----
        if pe_current is None:
            degraded = True
            sig_pe = None
            final = WATCH if (sig_ma1 and sig_ma2) else WAIT
        else:
            if pe_median is None:
                raise CheckerError("PE 存在当前值但无历史中位数,数据异常,请人工核查", [pe_file])
            degraded = False
            sig_pe = pe_current < pe_median
            n_met = int(sig_ma1) + int(sig_ma2) + int(sig_pe)
            final = WATCH if n_met >= min_req else WAIT

        expect = {
            "w1": w1, "w2": w2,
            "close": close, f"ma_MA{w1}": ma1, f"ma_MA{w2}": ma2,
            "sig_ma1": sig_ma1, "sig_ma2": sig_ma2,
            "pe_current": pe_current, "pe_median": pe_median, "sig_pe": sig_pe,
            "degraded": degraded, "final": final,
        }

        # ---- 与产物逐项比对(a)(c) ----
        check_output(out_csv, issues, expect)

    except CheckerError as e:
        issues.add("输入错误", str(e), e.files)

    if issues.new:
        issues.save()
        print(f"[checker] 发现 {len(issues.new)} 个问题,已写入 {ISSUES_JSON}:", file=sys.stderr)
        for i in issues.new:
            print(f"  {i['id']} [{i['category']}] {i['message']}", file=sys.stderr)
            print(f"        受影响文件: {', '.join(i['affected_files'])}", file=sys.stderr)
        sys.exit(1)

    print(f"[checker] asof={args.asof} 审查通过:三个信号及降级规则独立重算一致,"
          f"加工产物与计算实际使用的 PE 月度序列无晚于 asof 的观测。")
    sys.exit(0)


if __name__ == "__main__":
    main()
