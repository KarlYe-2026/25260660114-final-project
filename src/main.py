#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 流水线 Controller(Orchestrator 角色,契约见 docs/design.md §2/§5)。

用法:
    python src/main.py --asof 2026-11-06            # 全量新跑(覆写 state.json)
    python src/main.py --asof 2026-11-06 --resume   # 断点续跑(先核验已完成阶段)

职责(只做调度与状态维护,不抓数、不计算、不审查):
    1. 依次以子进程运行三个阶段: fetch_data → calculate → checker;
    2. 每个阶段完成后,用 hashlib 计算该阶段产物文件的 SHA256,连同 case_id、
       asof、当前阶段、重试次数、状态原子写入 .state/state.json
       (先写 .state/state.json.tmp 再 os.replace;main.py 是其唯一读写者);
    3. 有界重试: 任一阶段失败最多重试 max_retries 次(config/params.json 读取;
       子进程超时上限取 timeout_minutes),超限置 FAILED、写明原因并以非零码退出;
    4. --resume: 先读 state.json,重算已完成阶段产物哈希并比对——一致打印
       "已核验,跳过",只补做未完成阶段;不一致置 BLOCKED 并提示人工处理;
    5. 案例识别: case_id = sha256(asof + params) 前 16 位。config/params.json
       内容与 state.json 记录不一致(参数变更 = 新案例,--asof 变更同理)时,
       拒绝运行并提示须清除旧状态(.state/state.json)后重跑。

阶段产物(checker 只读产物、不改写任何数据文件,其"产物"取它所复核的估值 CSV,
用于把验证结论与具体产物版本绑定):
    fetch_data → data/processed/qqq_daily.csv
    calculate  → output/tables/valuation-asof-<asof>.csv
    checker    → output/tables/valuation-asof-<asof>.csv(复核对象)

退出码: 0 = 全部完成;1 = 阶段失败且重试超限;2 = 状态/参数类错误
        (BLOCKED、参数或 asof 变更、state.json 损坏、入口参数非法)。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PARAMS_JSON = "config/params.json"
STATE_JSON = ".state/state.json"
STATE_TMP = ".state/state.json.tmp"

# (阶段名, 子脚本相对路径, 产物文件列表模板)
STAGES = [
    ("fetch_data", "src/fetch_data.py", ["data/processed/qqq_daily.csv"]),
    ("calculate", "src/calculate.py", ["output/tables/valuation-asof-{asof}.csv"]),
    ("checker", "tests/checker.py", ["output/tables/valuation-asof-{asof}.csv"]),
]

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_STATE_ERROR = 2


def log(msg: str) -> None:
    print(f"[main] {msg}")


def fail(msg: str) -> None:
    """任何环节失败都显式报错退出,绝不降级、绝不造假。"""
    raise RuntimeError(f"[main] 失败: {msg}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_asof(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"--asof 必须是 YYYY-MM-DD 格式,收到: {s!r}")
    return s


def load_params() -> dict:
    p = PROJECT_ROOT / PARAMS_JSON
    if not p.exists():
        fail(f"缺少参数文件 {PARAMS_JSON}(请先创建)")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{PARAMS_JSON} 不是合法 JSON: {e}")
    if not isinstance(cfg, dict) or "max_retries" not in cfg:
        fail(f"{PARAMS_JSON} 缺少字段 'max_retries'")
    return cfg


def compute_case_id(asof: str, params: dict) -> str:
    """案例标识 = (asof, 参数全集) 的 SHA256 前缀;参数或 asof 变即新案例。"""
    payload = asof + "\n" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_state(case_id: str, asof: str, params: dict) -> dict:
    return {
        "version": 1,
        "case_id": case_id,
        "asof": asof,
        "params": params,
        "status": "RUNNING",
        "current_stage": None,
        "stages": {
            name: {"status": "PENDING", "retries": 0, "artifacts": {}}
            for name, _, _ in STAGES
        },
        "error": None,
    }


def save_state(state: dict) -> None:
    """原子写: 先写临时文件再 os.replace,中断不会留下半个 JSON。"""
    state["updated_at"] = now_iso()
    target = PROJECT_ROOT / STATE_JSON
    tmp = PROJECT_ROOT / STATE_TMP
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def run_once(script: str, asof: str, timeout_sec: int):
    """运行一个阶段子进程,返回 (returncode, 错误说明)。"""
    cmd = [sys.executable, script, "--asof", asof]
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return -9, f"超时(>{timeout_sec // 60} 分钟)"
    except OSError as e:
        return -1, f"无法启动子进程: {e}"
    if proc.returncode != 0:
        return proc.returncode, f"退出码 {proc.returncode}"
    return 0, ""


def hash_artifacts(templates, asof: str) -> dict:
    """计算阶段产物文件的 SHA256,返回 {相对路径: 哈希};产物缺失即失败。"""
    out = {}
    for t in templates:
        rel = t.format(asof=asof)
        p = PROJECT_ROOT / rel
        if not p.is_file():
            fail(f"阶段成功退出但产物缺失: {rel}")
        out[rel] = sha256_file(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="QQQ 估值流水线 Controller(调度 + 状态机)")
    ap.add_argument("--asof", required=True, help="数据截止日,YYYY-MM-DD(含当天)")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑:核验已完成阶段产物哈希后,只补做未完成阶段")
    args = ap.parse_args()

    try:
        asof = parse_asof(args.asof)
        params = load_params()
        max_retries = int(params["max_retries"])
        timeout_sec = int(params.get("timeout_minutes", 30)) * 60
    except (ValueError, RuntimeError, TypeError) as e:
        print(f"[main] {e}", file=sys.stderr)
        return EXIT_STATE_ERROR
    if max_retries < 0:
        print(f"[main] {PARAMS_JSON} 字段 'max_retries' 不能为负", file=sys.stderr)
        return EXIT_STATE_ERROR
    case_id = compute_case_id(asof, params)

    # ---- 读取旧状态并做案例一致性检查(参数/asof 变更 = 新案例,拒绝沿用旧状态) ----
    state_path = PROJECT_ROOT / STATE_JSON
    old = None
    if state_path.exists():
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[main] {STATE_JSON} 解析失败({e}),运行状态未知。", file=sys.stderr)
            print(f"[main] 请人工核查后清除该文件再重跑(禁止静默删除任何产物)。",
                  file=sys.stderr)
            return EXIT_STATE_ERROR
        if old.get("asof") != asof or old.get("params") != params:
            print("[main] config/params.json 或 --asof 与 state.json 记录不一致,"
                  "参数变更属于新案例,不能沿用旧状态:", file=sys.stderr)
            print(f"  state.json: asof={old.get('asof')}, "
                  f"params={json.dumps(old.get('params'), ensure_ascii=False, sort_keys=True)}",
                  file=sys.stderr)
            print(f"  本次请求:   asof={asof}, "
                  f"params={json.dumps(params, ensure_ascii=False, sort_keys=True)}",
                  file=sys.stderr)
            print(f"  须先清除旧状态(删除 {STATE_JSON})后重跑。", file=sys.stderr)
            return EXIT_STATE_ERROR

    # ---- --resume: 重算已完成阶段产物哈希并比对;不一致置 BLOCKED ----
    verified = {}
    if args.resume:
        if old is None:
            log("未发现 state.json,无可恢复状态,按全量新跑。")
        else:
            for name, _, _ in STAGES:
                rec = (old.get("stages") or {}).get(name) or {}
                if rec.get("status") != "DONE":
                    continue
                bad = None
                for rel, recorded in (rec.get("artifacts") or {}).items():
                    p = PROJECT_ROOT / rel
                    if not p.is_file():
                        bad = f"产物缺失: {rel}"
                        break
                    actual = sha256_file(p)
                    if actual != recorded:
                        bad = (f"产物 {rel} 哈希不一致"
                               f"(state.json={recorded[:16]}…, 实际={actual[:16]}…)")
                        break
                if bad:
                    state = new_state(case_id, asof, params)
                    state["stages"] = old.get("stages") or {}
                    state["status"] = "BLOCKED"
                    state["error"] = {
                        "stage": name,
                        "message": f"resume 核验失败: {bad};"
                                   f"疑似产物被改动或状态过期,请人工处理",
                        "at": now_iso(),
                    }
                    save_state(state)
                    print(f"[main] 阶段 {name} 产物哈希与 state.json 记录不一致,"
                          f"已置 BLOCKED: {bad}", file=sys.stderr)
                    print("[main] 请人工核查产物与状态;确认后删除 .state/state.json"
                          "重跑,或恢复产物后重新全量执行。", file=sys.stderr)
                    return EXIT_STATE_ERROR
                verified[name] = rec
                log(f"阶段 {name} 已核验，跳过")

    # ---- 初始化本次运行状态(覆写);已核验阶段沿用其核验记录 ----
    state = new_state(case_id, asof, params)
    state["stages"].update(verified)
    save_state(state)

    # ---- 依次运行未完成阶段(有界重试) ----
    for name, script, templates in STAGES:
        if name in verified:
            continue
        state["current_stage"] = name
        state["stages"][name] = {"status": "RUNNING", "retries": 0, "artifacts": {}}
        save_state(state)

        attempts = 0
        while True:
            attempts += 1
            log(f"阶段 {name}: 运行 {script} --asof {asof}(第 {attempts} 次尝试)")
            rc, err = run_once(script, asof, timeout_sec)
            artifacts = None
            if rc == 0:
                try:
                    artifacts = hash_artifacts(templates, asof)
                except RuntimeError as e:
                    rc, err = -1, str(e)
            if rc == 0:
                state["stages"][name] = {
                    "status": "DONE",
                    "retries": attempts - 1,
                    "artifacts": artifacts,
                    "finished_at": now_iso(),
                }
                save_state(state)
                log(f"阶段 {name} 完成,产物 SHA256 已写入 {STATE_JSON}"
                    + (f"(重试 {attempts - 1} 次)" if attempts > 1 else ""))
                break
            retries_used = attempts - 1
            state["stages"][name] = {
                "status": "FAILED", "retries": retries_used, "error": err,
            }
            save_state(state)
            if retries_used >= max_retries:
                reason = (f"阶段 {name} 失败: {err};"
                          f"已达最大重试次数 max_retries={max_retries}")
                state["status"] = "FAILED"
                state["error"] = {
                    "stage": name, "message": reason,
                    "retries": retries_used, "at": now_iso(),
                }
                save_state(state)
                print(f"[main] {reason}", file=sys.stderr)
                print(f"[main] 状态已置 FAILED,原因已写入 {STATE_JSON};"
                      f"修复后可用 --resume 续跑。", file=sys.stderr)
                return EXIT_STAGE_FAILED
            log(f"阶段 {name} 失败({err}),重试 ({retries_used + 1}/{max_retries})")

    state["status"] = "DONE"
    state["current_stage"] = None
    state["error"] = None
    save_state(state)
    if len(verified) == len(STAGES):
        log(f"全部阶段此前均已完成且核验一致,无需补做: case_id={case_id}, asof={asof}")
    else:
        log(f"流水线完成: case_id={case_id}, asof={asof},状态 DONE")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
