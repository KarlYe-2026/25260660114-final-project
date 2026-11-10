#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_data.py — data-agent 入口脚本。

用法:
    python src/fetch_data.py --asof 2026-11-06

职责（只做抓取与落盘，不做任何指标计算）:
    1. 价格: 读取 data/raw/asof-<日期>/raw_sfd_qqq_price.csv
       (kimi-datasource 插件 stock_finance_data_get_price 的原始返回)并校验;
    2. PE: 抓取 Nasdaq-100 的 PE(TTM) 月度历史(蛋卷基金周度序列,作为 QQQ 的 PE 代理);
    3. 防未来数据泄露:任何进入样本的数据日期晚于 asof 即 raise ValueError;
    4. 原始响应原样保存到 data/raw/asof-<日期>/,并写 _meta.json;
    5. 个别日期缺 PE 标 n/a;任何抓取/解析失败直接报错退出,严禁模拟数据。

数据源说明:
    - 价格: kimi-datasource 插件 · stock_finance_data_get_price
      (ticker QQQ.O, 美股默认/显式 adjust=none 未复权, 单次最长 3 年, 日线)。
      独立脚本无法直接调用 MCP 工具,故实际请求由 generator-lead 通过 MCP 发起、
      原始 CSV 落盘 data/raw/,同时写 _fetch_request.json 留档(真实请求参数与时间);
      本脚本只读取并校验该原始文件,缺失即报错,绝不自行造数。
      变更原因(2026-11-10 实测): 本机直连 Yahoo Chart API(query1/query2)全部 403,
      cookie+crumb 流程亦被边缘节点封禁;插件服务端抓取不受本机出口 IP 影响,且
      未复权口径与 task.md 一致(探测记录见 docs/notes/data-availability.md)。
    - PE(TTM): 蛋卷基金(danjuanfunds.com)纳指100 指数估值 PE 历史(周度序列,
      取每月最后一个观测作为月度值;kimi-datasource 各源均无 PE 历史序列;
      multpl.com 原页面已下架,经项目负责人确认改用蛋卷,见 _meta.json)。
      PE 采用 NDX 指数口径,作为 QQQ 的代理。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_TRADING_DAYS = 250

PRICE_RAW_FILE = "raw_sfd_qqq_price.csv"
FETCH_REQ_JSON = "_fetch_request.json"  # 插件抓数的真实请求留档(由 MCP 调用方写入)
PE_URL = "https://danjuanfunds.com/djapi/index_eva/pe_history/NDX?day=all"
PE_RAW_FILE = "raw_danjuan_ndx_pe.json"

RAW_DIR_TMPL = "data/raw/asof-{asof}"
DAILY_CSV = "data/processed/qqq_daily.csv"
META_JSON = "_meta.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# 蛋卷为公开接口:5xx 为边缘节点临时故障,重试;其余状态码(如 404)重试无意义,直接失败。
HTTP_RETRY_STATUSES = {500, 502, 503, 504}
HTTP_MAX_ATTEMPTS = 6
HTTP_RETRY_BASE_DELAY = 15  # 秒;退避序列 15/30/60/120/120,累计约 6 分钟


def fail(msg: str) -> None:
    """任何环节失败都显式报错退出,绝不降级、绝不造假。"""
    raise RuntimeError(f"[fetch_data] 失败: {msg}")


def http_get(url: str, binary: bool = False):
    last_err = None
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            return body if binary else body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in HTTP_RETRY_STATUSES or attempt == HTTP_MAX_ATTEMPTS:
                fail(f"请求失败 {url}: HTTP {e.code} {e.reason}(已重试 {attempt - 1} 次)")
            delay = min(HTTP_RETRY_BASE_DELAY * 2 ** (attempt - 1), 120)
            print(f"[fetch_data] HTTP {e.code}(疑似临时故障),{delay}s 后重试 "
                  f"({attempt}/{HTTP_MAX_ATTEMPTS - 1})...")
            time.sleep(delay)
        except urllib.error.URLError as e:
            last_err = e
            if attempt == HTTP_MAX_ATTEMPTS:
                fail(f"请求失败 {url}: {e}(已重试 {attempt - 1} 次)")
            delay = min(HTTP_RETRY_BASE_DELAY * 2 ** (attempt - 1), 120)
            print(f"[fetch_data] 网络错误 {e},{delay}s 后重试 "
                  f"({attempt}/{HTTP_MAX_ATTEMPTS - 1})...")
            time.sleep(delay)
    fail(f"请求失败 {url}: {last_err}")  # 循环内已 return/fail,兜底防漏


def parse_asof(s: str) -> date:
    try:
        asof = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--asof 必须是 YYYY-MM-DD 格式,收到: {s!r}")
    today = datetime.now(timezone.utc).date()
    if asof > today:
        raise ValueError(f"--asof ({asof}) 晚于今天 ({today}),不存在可用的历史数据")
    return asof


def assert_no_future_dates(dates: pd.Series, asof: date, what: str) -> None:
    """防未来数据泄露闸门:样本中任何日期晚于 asof 立即阻断。

    若数据源因接口行为异常(如忽略 end_date、返回盘后补充 bar)把 asof 之后
    的数据混进来,后续所有均线/信号都会吃到"未来",回测结果不可信且难以察觉。
    因此这里宁可让整个流水线停掉,也不放行一条越界数据。
    """
    future = dates[dates > pd.Timestamp(asof)]
    if len(future):
        raise ValueError(
            f"[fetch_data] 防泄露闸门触发: {what} 中出现 {len(future)} 条晚于 "
            f"asof={asof} 的数据,首个越界日期 {future.iloc[0].date()};已阻断,请人工核查数据源。"
        )


def fetch_prices(asof: date, raw_dir: Path):
    """读取插件 stock_finance_data 的原始价格 CSV 并校验,返回 (DataFrame, 请求留档 dict)。

    DataFrame 按日期升序、仅含 asof 及之前的 date/close 两列。
    """
    req_file = raw_dir / FETCH_REQ_JSON
    raw_file = raw_dir / PRICE_RAW_FILE
    if not req_file.exists():
        fail(f"缺少请求留档 {req_file.relative_to(PROJECT_ROOT)}:价格须先由 MCP 插件抓取"
             f"(stock_finance_data_get_price, ticker QQQ.O)并落盘原始 CSV 与该留档文件")
    if not raw_file.exists():
        fail(f"缺少原始价格文件 {raw_file.relative_to(PROJECT_ROOT)}(请先通过 MCP 插件抓数)")
    req = json.loads(req_file.read_text(encoding="utf-8"))

    df = pd.read_csv(raw_file)
    for col in ("time", "close", "thscode"):
        if col not in df.columns:
            fail(f"{PRICE_RAW_FILE} 缺少字段 {col!r},插件返回格式可能已变化")
    bad = df[df["thscode"].astype(str) != "QQQ.O"]
    if len(bad):
        fail(f"{PRICE_RAW_FILE} 含非 QQQ.O 的行: {len(bad)} 行,请人工核查")

    df["date"] = pd.to_datetime(df["time"].astype(str), format="%Y%m%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")
    df = df.drop_duplicates(subset="date", keep="last")
    df = df[df["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
    if df.empty:
        fail(f"{PRICE_RAW_FILE} 在 asof={asof} 之前没有可用收盘价")

    assert_no_future_dates(df["date"], asof, "QQQ 日线收盘价")

    n = len(df)
    if n < MIN_TRADING_DAYS:
        fail(f"asof={asof} 之前仅 {n} 个交易日收盘价,不足 {MIN_TRADING_DAYS} 个")
    print(f"[fetch_data] 价格 OK: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}, 共 {n} 个交易日"
          f"(来源 {req.get('data_source')} / {req.get('api')}, 请求于 {req.get('fetched_at_utc')})")
    return df[["date", "close"]].reset_index(drop=True), req


def fetch_pe_history(asof: date, raw_dir: Path) -> pd.DataFrame:
    """抓取 Nasdaq-100 PE(TTM) 历史(蛋卷周度序列),聚合为月度,返回 month(月初 Timestamp) + pe_ttm。

    聚合口径:取每个自然月内最后一个 <= asof 的观测,近似月末 PE(与 multpl 月度表口径一致);
    当月尚未走完时,取 asof 之前最后一个周度观测。
    """
    print(f"[fetch_data] 请求 PE(TTM) 历史: {PE_URL}")
    raw_text = http_get(PE_URL)
    fetched_at = datetime.now(timezone.utc)  # 真实请求完成时刻,留档进 _meta.json
    (raw_dir / PE_RAW_FILE).write_text(raw_text, encoding="utf-8")

    try:
        rows = (json.loads(raw_text).get("data") or {}).get("index_eva_pe_growths") or []
    except json.JSONDecodeError as e:
        fail(f"蛋卷 PE 接口返回非 JSON(接口可能已变化): {e}")
    if not rows:
        fail("蛋卷 PE 接口返回空序列")

    recs = []
    for r in rows:
        ts, pe = r.get("ts"), r.get("pe")
        if ts is None or pe is None:
            continue
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        recs.append((pd.Timestamp(d), float(pe)))
    if not recs:
        fail("蛋卷 PE 序列解析后为空")

    df = pd.DataFrame(recs, columns=["date", "pe_ttm"]).sort_values("date").reset_index(drop=True)
    # 周度序列必然包含 asof 之后的观测:先按日截断丢弃(原始 JSON 仍原样留档),再聚合到月
    df = df[df["date"] <= pd.Timestamp(asof)].reset_index(drop=True)
    if df.empty:
        fail(f"蛋卷 PE 序列在 asof={asof} 之前无任何观测")

    df["month"] = df["date"].values.astype("datetime64[M]")
    monthly = df.groupby("month", as_index=False)["pe_ttm"].last()
    assert_no_future_dates(monthly["month"], asof, "PE(TTM) 月度序列")
    print(f"[fetch_data] PE(TTM) OK: {monthly['month'].iloc[0].date()} ~ {monthly['month'].iloc[-1].date()}, 共 {len(monthly)} 个月")
    return monthly[["month", "pe_ttm"]].reset_index(drop=True), fetched_at


def build_daily(price: pd.DataFrame, pe: pd.DataFrame) -> pd.DataFrame:
    """按月份把 PE(TTM) 挂到每个交易日上,缺 PE 的月份标 n/a(不做任何计算)。"""
    daily = price.copy()
    daily["month"] = daily["date"].values.astype("datetime64[M]")
    daily = daily.merge(pe, on="month", how="left")
    daily["pe_ttm"] = daily["pe_ttm"].where(daily["pe_ttm"].notna(), "n/a")
    n_na = int((daily["pe_ttm"] == "n/a").sum())
    if n_na:
        print(f"[fetch_data] 提示: {n_na}/{len(daily)} 个交易日缺 PE(TTM),已标 n/a")
    return daily[["date", "close", "pe_ttm"]]


def write_meta(raw_dir: Path, asof: date, price_req: dict,
               pe_fetched_at: datetime, daily_csv: Path) -> None:
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    meta = {
        "asof": str(asof),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "src/fetch_data.py (data-agent);价格原始 CSV 由 generator-lead 经 kimi-datasource MCP 抓数落盘",
        "anti_leakage": "样本内任何数据日期晚于 asof 即 raise ValueError 阻断(src/fetch_data.py: assert_no_future_dates)",
        "datasets": {
            "price": {
                "symbol": "QQQ",
                "source": "kimi-datasource 插件 · stock_finance_data(stock_finance_data_get_price)",
                "note": "ticker QQQ.O,美股 adjust=none 未复权日线,单次最长 3 年窗口;2026-11-10 本机直连 Yahoo Chart API(query1/query2)全部 403、cookie+crumb 亦被封禁,经项目负责人确认改用插件(服务端抓取不受本机出口 IP 限制),探测记录见 docs/notes/data-availability.md",
                "api": price_req.get("api"),
                "params": price_req.get("params"),
                "fetched_at_utc": price_req.get("fetched_at_utc"),
                "request_id": price_req.get("request_id"),
                "raw_file": PRICE_RAW_FILE,
                "fields": {
                    "time": "交易日(插件返回,YYYYMMDD)",
                    "close": "QQQ 当日收盘价(美元,未复权)",
                },
            },
            "pe_ttm": {
                "symbol": "纳斯达克100(作为 QQQ 的 PE(TTM) 代理)",
                "source": "蛋卷基金 danjuanfunds.com — /djapi/index_eva/pe_history/NDX?day=all(周度)",
                "note": "multpl.com Nasdaq-100 PE 页面已下架(404),经项目负责人确认改用蛋卷;周度序列取每月最后一个 <= asof 的观测作为月度值,月度值挂到当月每个交易日;口径为蛋卷计算的 PE-TTM(NDX 指数口径,作为 QQQ 代理),与 multpl 口径存在差异",
                "raw_file": PE_RAW_FILE,
                "request_url": PE_URL,
                "fetched_at_utc": iso(pe_fetched_at),
                "fields": {
                    "month": "PE 所属月份(月初),YYYY-MM-DD",
                    "pe_ttm": "Nasdaq-100 PE(TTM);该月无数据时为 n/a",
                },
            },
        },
        "output": {
            "daily_csv": str(daily_csv.relative_to(PROJECT_ROOT)),
            "columns": ["date", "close", "pe_ttm"],
        },
        "policy": "原始响应原样保存,禁止修改;抓取或解析失败即报错退出,严禁模拟/随机数据",
    }
    (raw_dir / META_JSON).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="校验 QQQ 价格原始快照、抓取 PE(TTM) 历史并落盘(data-agent)")
    ap.add_argument("--asof", required=True, help="数据截止日,YYYY-MM-DD(含当天)")
    args = ap.parse_args()

    asof = parse_asof(args.asof)
    raw_dir = PROJECT_ROOT / RAW_DIR_TMPL.format(asof=asof)
    raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"[fetch_data] asof={asof}, 原始数据目录: {raw_dir.relative_to(PROJECT_ROOT)}")

    price, price_req = fetch_prices(asof, raw_dir)
    pe, pe_fetched_at = fetch_pe_history(asof, raw_dir)
    daily = build_daily(price, pe)

    out_csv = PROJECT_ROOT / DAILY_CSV
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(out_csv, index=False)
    write_meta(raw_dir, asof, price_req, pe_fetched_at, out_csv)

    print(f"[fetch_data] 完成: {out_csv.relative_to(PROJECT_ROOT)} "
          f"({len(daily)} 行), meta: {(raw_dir / META_JSON).relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
