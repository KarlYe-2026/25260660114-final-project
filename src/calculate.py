#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calculate.py — calc-agent 入口脚本。

用法:
    python src/calculate.py --asof 2026-11-06

职责(只计算,绝不抓数据,绝不心算):
    1. 读取 config/params.json 与 data/raw/asof-<日期>/ 下的原始数据
       (raw_sfd_qqq_price.csv、raw_danjuan_ndx_pe.json,均为 data-agent 原样留档);
    2. 计算三个低估信号:
         a) 当日收盘价 < MA(ma_window_1) 日均线;
         b) 当日收盘价 < MA(ma_window_2) 日均线;
         c) 当前 PE(TTM) < 历史 PE(TTM) 中位数;
    3. 满足信号数 >= min_signals_required → "建议关注",否则 "观望";
    4. 降级规则:当前 PE(TTM) 为 n/a 时,改为"两个均线信号同时满足才输出建议关注",
       并在输出中标注 "PE维度缺失，已启用降级规则";
    5. 结果写入 output/tables/valuation-asof-<日期>.csv(含各信号数值/阈值/是否满足/最终信号)。

口径说明:
    - 均线为收盘价简单移动平均,窗口含当日(rolling(window).mean(),需满窗才取值);
    - 若 asof 非交易日,取 asof 之前最近一个交易日的收盘价作为"当日收盘价";
    - PE(TTM) 沿用 fetch_data.py 的月度聚合口径:周度序列截断至 asof 后,
      取每月最后一个观测作为该月 PE;当前 PE 取所评估交易日所属月份的月度值,
      该月无观测即为 n/a;历史中位数 = 截至 asof 全部月度 PE 值的中位数。
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from statistics import median
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PARAMS_JSON = "config/params.json"
PRICE_RAW = "raw_sfd_qqq_price.csv"  # kimi-datasource 插件 stock_finance_data_get_price 原始返回
PE_RAW = "raw_danjuan_ndx_pe.json"
OUT_DIR = "output/tables"
OUT_TMPL = "valuation-asof-{asof}.csv"

WATCH = "建议关注"
WAIT = "观望"
DEGRADED_NOTE = "PE维度缺失，已启用降级规则"


def fail(msg: str) -> None:
    """任何环节失败都显式报错退出,绝不降级、绝不造假。"""
    raise RuntimeError(f"[calculate] 失败: {msg}")


def parse_asof(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--asof 必须是 YYYY-MM-DD 格式,收到: {s!r}")


def load_params() -> dict:
    p = PROJECT_ROOT / PARAMS_JSON
    if not p.exists():
        fail(f"缺少参数文件 {PARAMS_JSON}(请先创建)")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for k in ("ma_window_1", "ma_window_2", "min_signals_required"):
        if k not in cfg:
            fail(f"{PARAMS_JSON} 缺少字段 {k!r}")
        if int(cfg[k]) <= 0:
            fail(f"{PARAMS_JSON} 字段 {k!r} 必须为正整数,收到 {cfg[k]!r}")
    return cfg


def load_prices(asof: date, raw_dir: Path) -> pd.DataFrame:
    """读取插件 stock_finance_data 原始价格 CSV,返回按日期升序、仅含 asof 及之前的 date/close。"""
    f = raw_dir / PRICE_RAW
    if not f.exists():
        fail(f"缺少原始价格文件 {f.relative_to(PROJECT_ROOT)}(请先通过 MCP 插件抓数落盘)")
    df = pd.read_csv(f)
    for col in ("time", "close", "thscode"):
        if col not in df.columns:
            fail(f"{PRICE_RAW} 缺少字段 {col!r},插件返回格式可能已变化")
    bad = df[df["thscode"].astype(str) != "QQQ.O"]
    if len(bad):
        fail(f"{PRICE_RAW} 含非 QQQ.O 的行: {len(bad)} 行,请人工核查")

    df["date"] = pd.to_datetime(df["time"].astype(str), format="%Y%m%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")
    df = df.drop_duplicates(subset="date", keep="last")
    df = df[df["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
    if df.empty:
        fail(f"{PRICE_RAW} 在 asof={asof} 之前没有可用收盘价")
    return df[["date", "close"]]


def load_pe_monthly(asof: date, raw_dir: Path) -> pd.DataFrame:
    """从蛋卷原始 JSON 解析 PE(TTM),按 fetch_data.py 口径聚合为月度序列(仅含 asof 及之前)。"""
    f = raw_dir / PE_RAW
    if not f.exists():
        fail(f"缺少原始 PE 文件 {f.relative_to(PROJECT_ROOT)}(请先运行 src/fetch_data.py)")
    payload = json.loads(f.read_text(encoding="utf-8"))
    rows = (payload.get("data") or {}).get("index_eva_pe_growths") or []

    recs = []
    for r in rows:
        ts, pe = r.get("ts"), r.get("pe")
        if ts is None or pe is None:
            continue  # 个别观测缺 PE,跳过(与 fetch_data.py 行为一致)
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        recs.append((pd.Timestamp(d), float(pe)))
    if not recs:
        return pd.DataFrame(columns=["month", "pe_ttm"])

    df = pd.DataFrame(recs, columns=["date", "pe_ttm"]).sort_values("date").reset_index(drop=True)
    df = df[df["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=["month", "pe_ttm"])

    df["month"] = df["date"].values.astype("datetime64[M]")
    monthly = df.groupby("month", as_index=False)["pe_ttm"].last()
    return monthly[["month", "pe_ttm"]].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="计算 QQQ 低估信号并输出结论(calc-agent)")
    ap.add_argument("--asof", required=True, help="评估截止日,YYYY-MM-DD(含当天)")
    args = ap.parse_args()

    asof = parse_asof(args.asof)
    cfg = load_params()
    w1 = int(cfg["ma_window_1"])
    w2 = int(cfg["ma_window_2"])
    min_req = int(cfg["min_signals_required"])

    raw_dir = PROJECT_ROOT / "data" / "raw" / f"asof-{asof}"
    if not raw_dir.is_dir():
        fail(f"原始数据目录 {raw_dir.relative_to(PROJECT_ROOT)} 不存在(请先运行 src/fetch_data.py --asof {asof})")

    # ---- 价格与均线(窗口含当日,需满窗) ----
    px = load_prices(asof, raw_dir)
    need = max(w1, w2)
    if len(px) < need:
        fail(f"asof={asof} 之前仅 {len(px)} 个交易日收盘价,不足最长均线窗口 {need} 日")
    px[f"ma_{w1}"] = px["close"].rolling(w1).mean()
    px[f"ma_{w2}"] = px["close"].rolling(w2).mean()

    last = px.iloc[-1]  # asof 之前最近一个交易日
    eval_date = last["date"].date()
    close = float(last["close"])
    ma1 = float(last[f"ma_{w1}"])
    ma2 = float(last[f"ma_{w2}"])

    # ---- PE(TTM):当前值与历史中位数 ----
    monthly = load_pe_monthly(asof, raw_dir)
    eval_month = pd.Timestamp(eval_date).replace(day=1)  # 归一到月初,与月度序列的 month 对齐
    month_hit = monthly[monthly["month"] == eval_month]
    pe_current = float(month_hit["pe_ttm"].iloc[0]) if len(month_hit) else None  # None = n/a
    pe_median = float(median(monthly["pe_ttm"].tolist())) if len(monthly) else None

    # ---- 信号判定 ----
    sig_ma1 = close < ma1
    sig_ma2 = close < ma2

    if pe_current is None:
        degraded = True
        sig_pe = None
        n_met = int(sig_ma1) + int(sig_ma2)
        final = WATCH if (sig_ma1 and sig_ma2) else WAIT
        cond = f"降级规则:两个均线信号需同时满足(本应为 ≥{min_req}/{3} 个信号)"
    else:
        if pe_median is None:
            fail("PE 存在当前值但无历史中位数,数据异常,请人工核查")
        degraded = False
        sig_pe = pe_current < pe_median
        n_met = int(sig_ma1) + int(sig_ma2) + int(sig_pe)
        final = WATCH if n_met >= min_req else WAIT
        cond = f"满足信号数 ≥ {min_req}"

    note_final = DEGRADED_NOTE if degraded else ""
    if eval_date != asof:
        note_final = (note_final + "; " if note_final else "") + f"asof 非交易日,实际评估日 {eval_date}"

    # ---- 输出 ----
    pe_value = f"{pe_current:.2f}" if pe_current is not None else "n/a"
    pe_threshold = f"{pe_median:.2f}" if pe_median is not None else "n/a"
    pe_satisfied = ("是" if sig_pe else "否") if sig_pe is not None else "n/a"
    pe_note = DEGRADED_NOTE if degraded else ""

    rows = [
        {
            "信号": f"收盘价 < MA{w1}(ma_window_1)",
            "数值": f"{close:.2f}",
            "阈值": f"{ma1:.2f}",
            "判定条件": f"收盘价 < {w1} 日均线",
            "是否满足": "是" if sig_ma1 else "否",
            "备注": "",
        },
        {
            "信号": f"收盘价 < MA{w2}(ma_window_2)",
            "数值": f"{close:.2f}",
            "阈值": f"{ma2:.2f}",
            "判定条件": f"收盘价 < {w2} 日均线",
            "是否满足": "是" if sig_ma2 else "否",
            "备注": "",
        },
        {
            "信号": "PE(TTM) < 历史 PE 中位数",
            "数值": pe_value,
            "阈值": pe_threshold,
            "判定条件": "当前 PE(TTM) < 截至 asof 历史月度 PE 中位数",
            "是否满足": pe_satisfied,
            "备注": pe_note,
        },
        {
            "信号": "最终信号",
            "数值": final,
            "阈值": f"min_signals_required={min_req}",
            "判定条件": cond,
            "是否满足": "",
            "备注": note_final,
        },
    ]
    out = pd.DataFrame(rows, columns=["信号", "数值", "阈值", "判定条件", "是否满足", "备注"])

    out_dir = PROJECT_ROOT / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / OUT_TMPL.format(asof=asof)
    out.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"[calculate] asof={asof}(评估日 {eval_date}), 收盘价 {close:.2f}, "
          f"MA{w1}={ma1:.2f}, MA{w2}={ma2:.2f}")
    print(f"[calculate] PE(TTM) 当前={pe_value}, 历史中位数={pe_threshold}"
          + (f", 满足信号数 {n_met}/3" if not degraded else ", PE 缺失已启用降级规则"))
    print(f"[calculate] 最终信号: {final}"
          + (f"({DEGRADED_NOTE})" if degraded else ""))
    print(f"[calculate] 结果已写入 {out_csv.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
