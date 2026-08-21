#!/usr/bin/env python3
"""进程守护 — 每分钟检查关键服务，挂了自动重启"""
import subprocess, time, os, sys, re, shlex, json

ENV_FILE = '/home/myuser/websocket_new/.env'
ENV_PREFIX = f'. {ENV_FILE} 2>/dev/null; export $(grep -v "^#" {ENV_FILE} | cut -d= -f1 2>/dev/null); '
RESTART_COUNT_FILE = '/tmp/guardian_restart_count.json'

CHECKS = [
    # ── 常驻进程 ──
    # sim_trade已停 (改用auto_dual_trade.py cron每天0点运行)
    {
        "name": "OI采集",
        "check": "process", "pattern": "oi_collector.py",
        "start": "screen -dmS oi_collector bash -c 'source /home/myuser/websocket_new/.env 2>/dev/null; export $(grep -v \"^#\" /home/myuser/websocket_new/.env | cut -d= -f1); cd /home/myuser/backtester/cos_service && python3 -u oi_collector.py > /tmp/oi_collector.log 2>&1'",
    },
    {
        "name": "CDD链上",
        "check": "process", "pattern": "blockchair_collector.py",
        "start": "screen -dmS bc_collector bash -c 'source /home/myuser/websocket_new/.env 2>/dev/null; export $(grep -v \"^#\" /home/myuser/websocket_new/.env | cut -d= -f1); python3 -u /home/myuser/blockchair_collector.py > /tmp/bc_collector.log 2>&1'",
    },
    {
        "name": "情绪采集",
        "check": "process", "pattern": "sentiment_collector.py",
        "start": "screen -dmS sentiment bash -c 'source /home/myuser/websocket_new/.env 2>/dev/null; export $(grep -v \"^#\" /home/myuser/websocket_new/.env | cut -d= -f1); python3 -u /home/myuser/websocket_new/sentiment_collector.py > /tmp/sentiment.log 2>&1'",
    },
    # ── cron定时任务 (检查输出文件新鲜度) ──
    {
        "name": "板块热力图(每小时)",
        "check": "file_age", "path": "/tmp/sector_heatmap.json", "max_age": 7200,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/sector_heatmap.py > /tmp/heatmap_cron.log 2>&1 &",
    },
    {
        "name": "清算热力图(每小时)",
        "check": "file_age", "path": "/tmp/liquidation_heatmap.json", "max_age": 7200,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/liquidation_heatmap.py > /tmp/liq_heatmap_cron.log 2>&1 &",
    },
    {
        "name": "恐慌贪婪(每天)",
        "check": "file_age", "path": "/tmp/fear_greed_history.json", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/fear_greed_collector.py > /tmp/fear_greed_cron.log 2>&1 &",
    },
    {
        "name": "稳定币监控(每天)",
        "check": "file_age", "path": "/home/myuser/stablecoin_data/daily_monitor.csv", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/stablecoin_data/monitor.py > /tmp/stablecoin_monitor.log 2>&1 &",
    },
    {
        "name": "算力采集(每天)",
        "check": "file_age", "path": "/home/myuser/hashrate_data/hashrate_daily.csv", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/hashrate_data/collector.py > /tmp/hashrate_cron.log 2>&1 &",
    },
    {
        "name": "ETF采集(每天)",
        "check": "file_age", "path": "/home/myuser/websocket_new/data/etf_data/etf_flow.json", "max_age": 72000,
        "start": "cd /home/myuser/websocket_new/data/etf_data && /usr/bin/python3 fetch_etf.py > /tmp/etf_cron.log 2>&1 &",
    },
    {
        "name": "板块标签(每天)",
        "check": "file_age", "path": "/tmp/crypto_sectors.json", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/sector_fetcher.py > /tmp/sector_fetcher.log 2>&1 &",
    },
    {
        "name": "BTC市值(每天)",
        "check": "file_age", "path": "/home/myuser/coingecko_data/btc_mcap.json", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/collect_btc_mcap.py > /tmp/btc_mcap_cron.log 2>&1 &",
    },
    {
        "name": "TVL采集(每天)",
        "check": "file_age", "path": "/home/myuser/defillama_data/ethereum_tvl.json", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/collect_tvl.py > /tmp/tvl_cron.log 2>&1 &",
    },
    {
        # 2026-08-19: 快照cron 07:10 CST若失败, 次日~08:10发现当日文件缺失(>25h)自动补跑
        "name": "宇宙快照(每天)",
        "check": "file_age", "path": "/home/myuser/websocket_new/data/universe/%F.json", "max_age": 90000,
        "start": "/usr/bin/python3 /home/myuser/websocket_new/daily_universe_snapshot.py >> /home/myuser/websocket_new/logs/universe.log 2>&1 &",
    },
    {
        "name": "auto_dual_trade(每天)",
        "check": "crash_file", "path": "/tmp/auto_dual_trade_crash.json", "alert_window": 90000,
        # start 只做告警记录，不重跑交易脚本（交易脚本由 cron 调度）
        "start": "/usr/bin/python3 -c \"import datetime; print(datetime.datetime.now().isoformat(), 'auto_dual_trade crashed - check /tmp/auto_dual_trade_crash.json')\" >> /tmp/auto_dual_trade_alert.log",
    },
    # 2026-08-05 停用: 旧监控面板 market_monitor_app.py(5003) 无人使用, 从守护列表移除
    # {
    #     "name": "Web服务(5003)",
    #     "check": "process", "pattern": "market_monitor_app.py",
    #     "start": "cd /home/myuser/websocket_new && screen -dmS web python3 -u market_monitor_app.py",
    # },
    {
        "name": "MCP服务",
        "check": "process", "pattern": "mcp_server.py",
        "start": "cd /home/myuser/websocket_new && screen -dmS mcp python3 -u mcp_server.py",
    },
    {
        # 2026-08-19: DSH(DEEPSEEK HARNESS) web端, nginx 8443→3080; 进程死了自动拉起
        # --trusted-host: /api信任围栏白名单(不带端口=匹配任意端口), 否则经nginx访问报403
        "name": "DSH Web(3080)",
        "check": "process", "pattern": "dsh web",
        "start": "setsid nohup /home/myuser/.npm-global/bin/dsh web --trusted-host 43.133.253.208 --trusted-host 10.8.0.16 --trusted-host VM-0-16-ubuntu --trusted-host localhost.localdomain >> /tmp/dsh_web.log 2>&1 < /dev/null &",
    },
]

# check_port() monitors TCP port liveness for CHECKS entries with "check": "port"
# To monitor the web service (port 5003), add a CHECKS entry:
#   {"name": "Web服务", "check": "port", "port": 5003, "start": "..."}
def check_port(port):
    import socket
    try:
        s = socket.socket()
        s.settimeout(2)
        s.connect(('localhost', port))
        s.close()
        return True
    except Exception:
        return False

def check_process(pattern):
    try:
        result = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False

def check_file_age(path, max_age):
    """检查文件是否存在且最近修改时间在max_age秒内 (path支持strftime模板, 如 %F=当日北京日期)"""
    try:
        path = time.strftime(path)
        mtime = os.path.getmtime(path)
        return (time.time() - mtime) < max_age
    except Exception:
        return False

def check_crash_file(path, alert_window=3600):
    """检查崩溃标记文件：存在且 crashed=true 且在 alert_window 内 → 不健康"""
    try:
        if not os.path.exists(path):
            return True
        mtime = os.path.getmtime(path)
        if (time.time() - mtime) > alert_window:
            return True  # 旧的崩溃已过期，不继续告警
        with open(path) as f:
            data = json.load(f)
        return not data.get('crashed', False)
    except Exception:
        return True

def _pid_file(name):
    """生成PID文件路径"""
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return f"/tmp/guardian_{safe}.pid"

def _task_running(name, start_cmd):
    """检查file_age任务是否已有实例在跑，用pgrep匹配完整脚本路径"""
    # 从start命令提取脚本路径 (如 /home/myuser/websocket_new/sector_fetcher.py)
    match = re.search(r'(/[^\s]+\.py)', start_cmd)
    if match:
        pattern = match.group(1)
        try:
            result = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            pass
    # 退化为PID文件检查
    pf = _pid_file(name)
    try:
        with open(pf) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def _load_restart_count():
    """从文件加载重启计数，使circuit breaker在cron调用间持续生效"""
    try:
        if os.path.exists(RESTART_COUNT_FILE):
            with open(RESTART_COUNT_FILE) as f:
                data = json.load(f)
            now = time.time()
            result = {}
            for name, timestamps in data.items():
                result[name] = [t for t in timestamps if now - t < 3600]
            return result
    except Exception:
        pass
    return {}

def _save_restart_count(counts):
    try:
        with open(RESTART_COUNT_FILE, 'w') as f:
            json.dump(counts, f)
    except Exception:
        pass

def _stop_dsh():
    """通过3080端口精确找到DSH进程并停止（pkill -f会误杀命令行含该串的wrapper）"""
    try:
        out = subprocess.run("ss -ltnp 2>/dev/null | grep ':3080 '", shell=True,
                             capture_output=True, text=True).stdout
        m = re.search(r'pid=(\d+)', out)
        if not m:
            return True  # 本来就没在跑
        os.kill(int(m.group(1)), 15)
        for _ in range(20):
            time.sleep(0.5)
            if not check_port(3080):
                return True
        return False
    except Exception:
        return False

def dsh_session_maintenance():
    """每天04:00-04:59自动体检DSH会话日志，发现超阈值会话则停服→瘦身→重启。
    预防流式碎片堆积导致的历史加载30秒超时（'The user aborted a request'）。"""
    if int(time.strftime('%H')) != 4:
        return
    marker = '/tmp/guardian_dsh_slim_done_' + time.strftime('%Y%m%d')
    if os.path.exists(marker):
        return
    tool = '/home/myuser/websocket_new/dsh_session_slim.mjs'
    try:
        # 1.体检：无超阈值会话则打标记收工
        r = subprocess.run(['/usr/bin/node', tool, 'check'], capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            open(marker, 'w').close()
            return
        ts = time.strftime('%m-%d %H:%M')
        print(f"[{ts}] DSH会话超阈值，开始凌晨维护: {r.stdout.strip()}")
        # 2.停DSH（瘦身期间不能有写入，否则seq错乱）
        if not _stop_dsh():
            print(f"  ⚠️ DSH停止失败，本轮跳过瘦身")
            return
        # 3.瘦身
        r2 = subprocess.run(['/usr/bin/node', tool, 'slim'], capture_output=True, text=True, timeout=600)
        print((r2.stdout or r2.stderr).strip())
        # 4.重启DSH（复用CHECKS里的启动命令，保证--trusted-host白名单一致）
        dsh_svc = next(s for s in CHECKS if s["name"] == "DSH Web(3080)")
        subprocess.run(ENV_PREFIX + dsh_svc["start"], shell=True)
        time.sleep(2)
        if check_port(3080):
            open(marker, 'w').close()
            print(f"[{time.strftime('%m-%d %H:%M')}] DSH维护完成，服务已恢复")
        else:
            print(f"  ⚠️ DSH维护后未检测到服务，等待下轮拉起")
    except Exception as e:
        print(f"  ⚠️ DSH会话维护异常: {e}")

def main():
    log_file = "/tmp/guardian.log"
    restart_count = _load_restart_count()
    status = []
    restarted_any = False
    dsh_session_maintenance()
    for svc in CHECKS:
        check_type = svc["check"]
        if check_type == "port":
            alive = check_port(svc["port"])
        elif check_type == "file_age":
            alive = check_file_age(svc["path"], svc["max_age"])
        elif check_type == "crash_file":
            alive = check_crash_file(svc["path"], svc.get("alert_window", 3600))
        else:
            alive = check_process(svc["pattern"])

        status.append({"name": svc["name"], "alive": alive})

        if not alive:
            # file_age任务：检查是否已有一个实例在跑
            if check_type == "file_age" and _task_running(svc["name"], svc["start"]):
                continue

            # Circuit breaker: stop restarting if more than 5 restarts in 1 hour
            now_ts = time.time()
            if svc["name"] not in restart_count:
                restart_count[svc["name"]] = []
            restart_count[svc["name"]] = [t for t in restart_count[svc["name"]] if now_ts - t < 3600]
            if len(restart_count[svc["name"]]) >= 5:
                msg = f"[{time.strftime('%m-%d %H:%M')}] CRITICAL: {svc['name']} restart limit exceeded (5+ restarts in 1hr) — giving up"
                print(msg)
                continue
            restart_count[svc["name"]].append(now_ts)
            restarted_any = True

            if check_type == "file_age":
                msg = f"[{time.strftime('%m-%d %H:%M')}] {svc['name']} 文件过期，启动更新..."
            elif check_type == "crash_file":
                msg = f"[{time.strftime('%m-%d %H:%M')}] {svc['name']} 发生崩溃，记录告警..."
            else:
                msg = f"[{time.strftime('%m-%d %H:%M')}] {svc['name']} 挂了，重启..."
            print(msg)
            cmd = svc["start"]

            # 如果是screen命令，先杀掉同名旧session防止孤儿进程泄漏
            screen_match = re.search(r'screen -dmS (\S+)', cmd)
            if screen_match:
                old_name = screen_match.group(1)
                subprocess.run(['screen', '-S', old_name, '-X', 'quit'], capture_output=True)
                time.sleep(0.3)

            # 所有命令前追加 .env 加载，确保子进程有API密钥
            cmd = ENV_PREFIX + cmd

            # 包含shell重定向时使用shell=True，否则使用shlex拆分 (SEC-007 fix)
            if any(c in cmd for c in ('>', '2>&1', '&')):
                subprocess.run(cmd, shell=True)
            else:
                subprocess.run(shlex.split(cmd), shell=False)
            
            # 验证是否真的启动了
            time.sleep(0.5)
            started = False
            if screen_match:
                result = subprocess.run(['screen', '-ls'], capture_output=True, text=True)
                started = screen_match.group(1) in result.stdout
            elif svc["check"] == "process":
                result = subprocess.run(['pgrep', '-f', svc["pattern"]], capture_output=True)
                started = result.returncode == 0
            else:
                started = True  # file_age/crash_file 为一次性任务，无需进程存活验证
            if not started:
                print(f"  ⚠️  {svc['name']} 重启命令已执行但未检测到进程!")
    if restarted_any:
        _save_restart_count(restart_count)
    # 写状态文件供网站读取
    with open("/tmp/guardian_status.json", "w") as f:
        json.dump({"services": status, "updated": time.time()}, f)

if __name__ == "__main__":
    main()
