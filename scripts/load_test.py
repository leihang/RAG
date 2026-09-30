#!/usr/bin/env python
"""并发压测 + 硬件占用采样（`02` 的性能验收口径）。

设计要点（都是被坑过才这么写的）：

* **单用户压不出容量**：`limits.per_user_qps=2`，同一个用户发 10 并发会立刻 429。
  所以按「10 名员工同时提问」建模 —— 脚本会自动准备 N 个压测用户（幂等），
  每个用户只发 1 QPS（全局 10 QPS < `global_qps=20`）。
* **缓存会把负载吃掉**：`/qa/chat` 传 `use_cache=false`；`/qa/retrieve` 没有该开关，
  只能对每个请求换问法（加轮次后缀）以绕开 `retrieve_ttl=1800` 的检索缓存。
* **硬件采样**：`top -l 2` 取第二个样本（macOS 的 `top -l 1` 第一帧 CPU 恒为 0，
  会得出"零占用"的假结论）；同时记 API 进程 RSS/线程数与系统内存/交换。

用法：
    EKBS_CONFIG=config/application.cloud.yaml \\
    .venv/bin/python scripts/load_test.py --mode retrieve --concurrency 10 --rounds 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BASE_QUESTIONS = [
    "年假有多少天",
    "年假申请要提前几天",
    "未休年假怎么补偿",
    "上下班打卡时间规定",
    "公司资产离职要归还吗",
    "入职满三年年假几天",
]


# ---------------------------------------------------------------------------
# 压测用户
# ---------------------------------------------------------------------------
def provision_users(count: int, config_path: str | None) -> list[tuple[int, str]]:
    """幂等地准备 N 个压测用户，返回 ``[(user_id, dingtalk_userid)]``。

    形态照抄演示用户 alice（L2 / 人力资源部 / end_user）：这样走的是**真实鉴权链路**
    （权限指纹、C1 分片路由、C5 门禁），而不是绕过鉴权压裸接口。
    """
    from ekbs.config import load_settings
    from ekbs.db import Database

    settings = load_settings(config_path) if config_path else load_settings()
    db = Database(settings)   # Database 收 Settings 对象（不是路径字符串）
    created: list[tuple[int, str]] = []
    with db.sync_connect() as conn:
        # 以 alice 为模板取一份权限形态
        template = conn.execute(
            "SELECT clearance, dept_chain, primary_dept_id FROM ekbs_user WHERE dingtalk_userid='demo_alice'"
        ).fetchone()
        if template is None:
            raise SystemExit("库中没有 demo_alice，无法取权限模板（先跑 seed_demo）")
        clearance, dept_chain, primary_dept = template[0], template[1], template[2]
        for index in range(1, count + 1):
            dt_id = f"loadtest_{index:02d}"
            row = conn.execute("SELECT user_id FROM ekbs_user WHERE dingtalk_userid=?", (dt_id,)).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO ekbs_user(dingtalk_userid, nick, clearance, dept_chain, primary_dept_id,"
                    " status, auth_source) VALUES(?,?,?,?,?,'active','dingtalk')",
                    (dt_id, f"压测用户{index:02d}", clearance, dept_chain, primary_dept),
                )
                user_id = int(cursor.lastrowid)
                # 部门可见性：主部门 + 祖先（与 alice 同形）
                for dept_id, is_primary in ((1, 0), (primary_dept, 1)):
                    conn.execute(
                        "INSERT OR IGNORE INTO ekbs_user_dept(user_id, dept_id, is_primary) VALUES(?,?,?)",
                        (user_id, dept_id, is_primary),
                    )
                # end_user 角色（全局范围）
                conn.execute(
                    "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id)"
                    " VALUES(?,5,'global',0)",
                    (user_id,),
                )
            else:
                user_id = int(row[0])
            created.append((user_id, dt_id))
        conn.commit()
    return created


# ---------------------------------------------------------------------------
# 硬件采样
# ---------------------------------------------------------------------------
def _to_mb(token: str) -> float:
    """把 top 的 ``22G`` / ``1182M`` / ``512K`` 统一换算成 MB。"""
    m = re.match(r"([\d.]+)([KMG]?)", token.strip())
    if not m:
        return 0.0
    factor = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "": 1.0}[m.group(2)]
    return round(float(m.group(1)) * factor, 1)


class HardwareSampler(threading.Thread):
    """后台采样 API 进程与整机资源。`top -l 2` 取第二帧（第一帧 CPU 恒 0）。"""

    def __init__(self, api_pid: int, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.api_pid = api_pid
        self.interval = interval
        # 注意：属性名不能叫 _stop —— 那会覆盖 Thread._stop，join() 会崩
        self._stop_event = threading.Event()
        self.samples: list[dict] = []
        self.baseline: dict = {}
        self._last_cpu: tuple[float, float] | None = None

    def run(self) -> None:
        self.baseline = self._sample()
        while not self._stop_event.is_set():
            self.samples.append(self._sample())
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=self.interval + 5)

    def _sample(self) -> dict:
        out: dict = {"t": time.time()}
        # --- 进程级：top 第二帧给 %CPU 与线程数（第一帧 CPU 恒 0，会得出"零占用"假结论）---
        try:
            top = subprocess.run(
                ["top", "-l", "2", "-s", "1", "-n", "1", "-pid", str(self.api_pid),
                 "-stats", "pid,cpu,mem,threads"],
                capture_output=True, text=True, timeout=25,
            ).stdout
            for line in reversed(top.splitlines()):
                m = re.match(r"\s*(\d+)\s+([\d.]+)\s+([\d.]+)([KMG]?)\s+(\d+)\s*$", line)
                if m and int(m.group(1)) == self.api_pid:
                    out["raw_row"] = line.strip()   # 留原始行：CPU 采样出 0 时能立刻判断是解析问题还是真为 0
                    out["api_cpu_pct_top"] = float(m.group(2))   # top 口径（实测恒 0，仅留作对照）
                    out["api_mem_mb_top"] = float(m.group(3))   # top 的 MEM 列（MB）
                    out["api_threads"] = int(m.group(5))
                    break
            # 整机 CPU 与内存（同一份输出里就有，省一次调用）
            cpu_lines = [ln for ln in top.splitlines() if ln.startswith("CPU usage")]
            if cpu_lines:
                m = re.search(r"([\d.]+)% user, ([\d.]+)% sys, ([\d.]+)% idle", cpu_lines[-1])
                if m:
                    out["sys_user_pct"] = float(m.group(1))
                    out["sys_sys_pct"] = float(m.group(2))
                    out["sys_idle_pct"] = float(m.group(3))
            phys = [ln for ln in top.splitlines() if ln.startswith("PhysMem")]
            if phys:
                m = re.search(r"PhysMem: (\S+) used \(([^)]*)\), (\S+) unused", phys[-1])
                if m:
                    out["mem_used_mb"] = _to_mb(m.group(1))
                    out["mem_unused_mb"] = _to_mb(m.group(3))
                    out["mem_wired"] = m.group(2)
            load = [ln for ln in top.splitlines() if ln.startswith("Load Avg")]
            if load:
                m = re.search(r"Load Avg: ([\d.]+), ([\d.]+), ([\d.]+)", load[-1])
                if m:
                    out["load1"] = float(m.group(1))
        except Exception as exc:   # 采样失败不能影响压测
            out["top_error"] = str(exc)[:80]

        # --- CPU：用 ps 的累计 CPU 时间做差（最可靠）---
        # macOS 的 `top -pid` 实测对本进程**恒报 0.0%**（负载中也是），不可信；
        # `ps -o pcpu=` 是进程生命周期内的衰减均值，短时压测会被严重稀释。
        # 累计 CPU 时间（`ps -o time=`）两次采样的差值 / 墙钟才是真值。
        try:
            raw_time = subprocess.run(["ps", "-o", "time=", "-p", str(self.api_pid)],
                                      capture_output=True, text=True, timeout=10).stdout.strip()
            if raw_time:
                parts = raw_time.split(":")
                seconds = 0.0
                for part in parts:
                    seconds = seconds * 60 + float(part)
                out["api_cpu_seconds"] = seconds
                previous = self._last_cpu
                if previous is not None:
                    cpu_delta = seconds - previous[1]
                    wall_delta = out["t"] - previous[0]
                    if wall_delta > 0:
                        out["api_cpu_pct"] = round(max(cpu_delta, 0.0) / wall_delta * 100, 1)
                self._last_cpu = (out["t"], seconds)
        except Exception as exc:
            out["cpu_error"] = str(exc)[:60]

        # --- 内存：ps 的 RSS（KB）比 top 的 MEM 更接近常说的"进程占用"---
        try:
            rss = subprocess.run(["ps", "-o", "rss=", "-p", str(self.api_pid)],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if rss:
                out["api_rss_mb"] = round(int(rss) / 1024, 1)
        except Exception:
            pass

        try:
            swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
            m = re.search(r"used = ([\d.]+)([MG])", swap)
            if m:
                out["swap_used_mb"] = round(float(m.group(1)) * (1024 if m.group(2) == "G" else 1), 1)
        except Exception:
            pass
        return out

    def summary(self) -> dict:
        def peak(key: str) -> float:
            values = [s[key] for s in self.samples if key in s]
            return round(max(values), 1) if values else 0.0

        def mean(key: str) -> float:
            values = [s[key] for s in self.samples if key in s]
            return round(statistics.fmean(values), 1) if values else 0.0

        return {
            "samples": len(self.samples),
            "api_cpu_mean_pct": mean("api_cpu_pct"),
            "api_cpu_peak_pct": peak("api_cpu_pct"),
            "api_rss_base_mb": self.baseline.get("api_rss_mb", 0.0),
            "api_rss_peak_mb": peak("api_rss_mb"),
            "api_threads_peak": int(peak("api_threads")),
            "sys_cpu_busy_mean_pct": round(100 - mean("sys_idle_pct"), 1) if self.samples else 0.0,
            "sys_cpu_busy_peak_pct": round(100 - min((s.get("sys_idle_pct", 100) for s in self.samples), default=100), 1),
            "load1_peak": peak("load1"),
            "api_mem_mb_top_peak": peak("api_mem_mb_top"),
            "mem_used_peak_mb": peak("mem_used_mb"),
            "mem_unused_min_mb": round(min((s.get("mem_unused_mb", 1e9) for s in self.samples), default=0.0), 1),
            "swap_used_peak_mb": peak("swap_used_mb"),
        }


# ---------------------------------------------------------------------------
# 压测主体
# ---------------------------------------------------------------------------
async def run_load(args: argparse.Namespace, users: list[tuple[int, str]], run_id: str) -> dict:
    import httpx

    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[dict] = []
    lock = asyncio.Lock()
    deadline = time.perf_counter() + args.duration if args.duration else None

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)

    async def one(client: httpx.AsyncClient, worker: int, round_index: int) -> None:
        user_id, dt_id = users[worker % len(users)]
        question = BASE_QUESTIONS[(worker + round_index) % len(BASE_QUESTIONS)]
        # 绕开检索缓存：每次请求换一个问法后缀
        variant = f"{question}（{run_id}-{worker}-{round_index}）"
        if args.mode == "chat":
            url, payload = "/api/v1/qa/chat", {"question": variant, "use_cache": False}
        else:
            url, payload = "/api/v1/qa/retrieve", {"query": variant}
        headers = {
            "X-Internal-Token": args.token,
            "X-Internal-User-Id": str(user_id),
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        record = {"worker": worker, "round": round_index, "user": dt_id}
        try:
            response = await client.post(args.base_url + url, json=payload, headers=headers)
            record["status"] = response.status_code
            record["ms"] = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                body = response.json()
                record["refused"] = bool(body.get("refused"))
                timings = body.get("timings_ms") or {}
                record["server_total_ms"] = timings.get("total")
                record["server_embed_ms"] = timings.get("embed")
                record["server_rerank_ms"] = timings.get("rerank")
                record["server_recall_ms"] = timings.get("recall")
                record["server_expand_ms"] = timings.get("expand")
                record["candidates"] = body.get("candidate_count")
        except Exception as exc:
            record["status"] = 0
            record["ms"] = (time.perf_counter() - started) * 1000
            record["error"] = f"{type(exc).__name__}: {exc}"[:120]
        async with lock:
            results.append(record)

    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        async def worker_loop(worker: int) -> None:
            round_index = 0
            while True:
                if deadline is not None and time.perf_counter() >= deadline:
                    return
                if deadline is None and round_index >= args.rounds:
                    return
                async with semaphore:
                    await one(client, worker, round_index)
                round_index += 1
                if args.think_time > 0:
                    await asyncio.sleep(args.think_time)

        await asyncio.gather(*(worker_loop(i) for i in range(args.concurrency)))

    return {"results": results}


def pct(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="10 并发压测 + 硬件采样")
    parser.add_argument("--mode", choices=["retrieve", "chat"], default="retrieve")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=30, help="每个并发的轮数（与 --duration 二选一）")
    parser.add_argument("--duration", type=float, default=0, help="按秒数压测（0=按轮数）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.environ.get("EKBS_INTERNAL_TOKEN", "dev-internal-token"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--think-time", type=float, default=0.5,
                        help="每轮之间的思考时间（秒）。必须 >0：workers 背靠背发请求会让单用户 QPS 超 per_user_qps=2 而假性 429")
    parser.add_argument("--config", default=os.environ.get("EKBS_CONFIG"))
    parser.add_argument("--api-pid", type=int, default=0)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--run-id", default="",
                        help="本轮唯一标识，默认取时间戳。**必须每轮不同**：问法后缀若重复，"
                             "第二轮起会命中查询嵌入缓存（query_embedding_ttl=86400），embed 会假性变成 0ms")
    args = parser.parse_args()
    run_id = args.run_id or time.strftime("%H%M%S")

    api_pid = args.api_pid
    if not api_pid:
        # ⚠️ 不能用 `pgrep -f "uvicorn ekbs.main:app"`：它会先匹配到 **bash 包装进程**
        # （nohup ... bash -c），采样到的是那个 1 线程的壳，而不是真正服务的 Python 进程
        # —— 实测踩到，报出"CPU 0% / RSS 1MB"的假数据。按监听端口取才准。
        listeners = subprocess.run(
            ["lsof", "-nP", "-iTCP:8000", "-sTCP:LISTEN", "-t"], capture_output=True, text=True
        ).stdout.split()
        if not listeners:
            print("8000 端口没有监听进程（先启动 API，或用 --api-pid 指定）", file=sys.stderr)
            return 2
        api_pid = int(listeners[0])

    users = provision_users(args.concurrency, args.config)
    total_planned = args.concurrency * args.rounds if not args.duration else None
    print(f"== 压测 ==")
    print(f"  模式={args.mode}  并发={args.concurrency}  用户={len(users)}  "
          f"{'轮数=' + str(args.rounds) + f'（共 {total_planned} 请求）' if total_planned else f'时长={args.duration}s'}")
    print(f"  API pid={api_pid}  基线采样中…")

    sampler = HardwareSampler(api_pid)
    sampler.start()
    time.sleep(2)   # 让采样器先拿到基线

    started = time.perf_counter()
    outcome = asyncio.run(run_load(args, users, run_id))
    elapsed = time.perf_counter() - started
    sampler.stop()

    results = outcome["results"]
    ok = [r for r in results if r.get("status") == 200]
    errors = [r for r in results if r.get("status") != 200]
    latencies = [r["ms"] for r in ok]
    refused = [r for r in ok if r.get("refused")]

    print(f"\n== 延迟（n={len(ok)} 成功 / {len(errors)} 失败，用时 {elapsed:.1f}s）==")
    if latencies:
        print(f"  p50={pct(latencies, 0.50)}ms  p90={pct(latencies, 0.90)}ms  "
              f"p95={pct(latencies, 0.95)}ms  p99={pct(latencies, 0.99)}ms  max={max(latencies):.0f}ms")
        print(f"  mean={statistics.fmean(latencies):.0f}ms  吞吐={len(ok) / elapsed:.1f} req/s")
        print(f"  拒答={len(refused)}/{len(ok)}（压测问法带后缀，拒答属预期）")
    if errors:
        codes: dict[str, int] = {}
        for item in errors:
            key = str(item.get("status")) if item.get("status") else item.get("error", "?")
            codes[key] = codes.get(key, 0) + 1
        print(f"  失败分类：{codes}")

    server_lat = [r["server_total_ms"] for r in ok if r.get("server_total_ms")]
    if server_lat:
        print(f"  服务端 timings.total：p50={pct(server_lat, 0.5)}ms  "
              f"p95={pct(server_lat, 0.95)}ms（网络+排队 ≈ 客户端-服务端之差）")

    hardware = sampler.summary()
    print(f"\n== 硬件占用（{hardware['samples']} 个样本）==")
    print(f"  API 进程：CPU 均值 {hardware['api_cpu_mean_pct']}%（峰值 {hardware['api_cpu_peak_pct']}%，"
          f"整机 {15} 逻辑核）")
    print(f"            RSS {hardware['api_rss_base_mb']}MB → 峰值 {hardware['api_rss_peak_mb']}MB，"
          f"线程峰值 {hardware['api_threads_peak']}")
    print(f"  整机：CPU 忙 均值 {hardware['sys_cpu_busy_mean_pct']}%（峰值 {hardware['sys_cpu_busy_peak_pct']}%），"
          f"load1 峰值 {hardware['load1_peak']}")
    print(f"       物理内存占用峰值 {hardware['mem_used_peak_mb']}MB，空闲最低 {hardware['mem_unused_min_mb']}MB，"
          f"swap 峰值 {hardware['swap_used_peak_mb']}MB")

    # 分阶段耗时（判断瓶颈在云端还是本机）：服务端 timings_ms 的 embed/recall/rerank/expand
    stages: dict[str, dict] = {}
    for key in ("embed", "recall", "rerank", "expand"):
        values = [r[f"server_{key}_ms"] for r in ok if r.get(f"server_{key}_ms") is not None]
        if values:
            stages[key] = {"mean": round(statistics.fmean(values), 1), "p95": pct(values, 0.95)}
    if stages:
        print("\n== 服务端分阶段耗时（均值 / p95）==")
        for key, item in stages.items():
            print(f"  {key:7} {item['mean']:>7.1f}ms / {item['p95']:>7.1f}ms")

    report = {
        "mode": args.mode,
        "stages_ms": stages, "concurrency": args.concurrency, "rounds": args.rounds,
        "requests": len(results), "ok": len(ok), "errors": len(errors), "elapsed_s": round(elapsed, 2),
        "throughput_rps": round(len(ok) / elapsed, 2) if elapsed else 0,
        "latency_ms": {k: pct(latencies, v) for k, v in
                       (("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99))} | {"max": round(max(latencies), 1) if latencies else 0},
        "refused": len(refused),
        "hardware": hardware,
    }
    report["samples_raw"] = sampler.samples
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n  报告已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
