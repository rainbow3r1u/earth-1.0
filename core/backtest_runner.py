"""
Rust回测引擎调用封装
运行 hybrid 模式，返回解析后的结果
"""
import subprocess, json, os, tempfile, time
from pathlib import Path

BACKTESTER_DIR = Path("/home/myuser/backtester-rs")
BINARY = BACKTESTER_DIR / "target" / "release" / "backtester-rs"
CARGO_ENV = os.path.expanduser("~/.cargo/env")

def run_hybrid_search(trials: int = 100, symbols: int = 200,
                      vs_ratio_min: float = 1.0, vs_ratio_max: float = 10.0) -> dict:
    """运行Rust回测引擎 hybrid 搜索 (纯BB绑定模式)"""
    if not BINARY.exists():
        return {"error": f"回测二进制不存在: {BINARY}。请先编译: cd {BACKTESTER_DIR} && cargo build --release"}

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        output_path = f.name

    try:
        cmd = f"source {CARGO_ENV} && cd {BACKTESTER_DIR} && {BINARY} search --trials {trials} --symbols {symbols} --vs-ratio-min {vs_ratio_min} --vs-ratio-max {vs_ratio_max} --output {output_path}"
        start = time.time()
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=600
        )
        elapsed = time.time() - start

        if result.returncode != 0:
            return {"error": f"回测失败 (exit={result.returncode})", "stderr": result.stderr[-500:]}

        # Parse output JSON
        with open(output_path) as f:
            results = json.load(f)

        # Sort and take top 20
        results.sort(key=lambda x: x.get("combined_return", -999), reverse=True)
        top = results[:20]

        return {
            "top_results": top,
            "elapsed_sec": round(elapsed, 1),
            "trials": trials,
            "symbols": symbols,
            "stdout_tail": result.stdout[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": "回测超时（>10分钟）"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        try: os.unlink(output_path)
        except: pass

def get_current_params() -> dict:
    """获取当前 sim_trade.py 中的 V7 参数"""
    with open("/home/myuser/websocket_new/sim_trade.py") as f:
        content = f.read()

    params = {}
    for key in ["SPOT_BB_PERIOD", "SPOT_BB_STD_MULT", "SPOT_MIN_HOURS",
                "SPOT_HL_WINDOW", "SPOT_HL_MIN", "SPOT_GAIN_FILTER_PCT",
                "SPOT_VOL_FILTER", "SPOT_PER_TRADE", "SPOT_MAX_POSITIONS",
                "FUT_MARGIN", "FUT_LEVERAGE", "FUT_TP_PCT", "FUT_SL_PCT",
                "FUT_MIN_RATIO", "FUT_VOL_FILTER", "FUT_MAX_DAILY_TP"]:
        import re
        m = re.search(rf'{key}\s*=\s*([0-9_.]+)', content)
        if m:
            v = m.group(1)
            params[key] = float(v) if '.' in v else int(v)
    return params

def deploy_params(bb_period: int, bb_std: float, bb_min_hours: int,
                  bb_hl_window: int, bb_hl_min: int, daily_gain: float,
                  fut_max_daily_tp: int, **kwargs) -> dict:
    """部署参数到 sim_trade.py 并重启交易服务"""
    filepath = "/home/myuser/websocket_new/sim_trade.py"
    with open(filepath) as f:
        content = f.read()

    # Backup
    backup_path = f"/tmp/sim_trade.py.bak.{int(time.time())}"
    with open(backup_path, 'w') as f:
        f.write(content)

    import re
    replacements = {
        r'SPOT_BB_PERIOD\s*=\s*\d+': f'SPOT_BB_PERIOD = {bb_period}',
        r'SPOT_BB_STD_MULT\s*=\s*[0-9.]+': f'SPOT_BB_STD_MULT = {bb_std}',
        r'SPOT_MIN_HOURS\s*=\s*\d+': f'SPOT_MIN_HOURS = {bb_min_hours}',
        r'SPOT_HL_WINDOW\s*=\s*\d+': f'SPOT_HL_WINDOW = {bb_hl_window}',
        r'SPOT_HL_MIN\s*=\s*\d+': f'SPOT_HL_MIN = {bb_hl_min}',
        r'SPOT_GAIN_FILTER_PCT\s*=\s*[0-9.]+': f'SPOT_GAIN_FILTER_PCT = {daily_gain}',
        r'FUT_MAX_DAILY_TP\s*=\s*\d+': f'FUT_MAX_DAILY_TP = {fut_max_daily_tp}',
    }
    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content)

    with open(filepath, 'w') as f:
        f.write(content)

    # Also update hybrid.rs defaults
    _update_hybrid_defaults(bb_period, bb_std, bb_min_hours, bb_hl_window, bb_hl_min, daily_gain, fut_max_daily_tp)

    # Restart trade service
    subprocess.run(["screen", "-S", "trade", "-X", "quit"], capture_output=True)
    subprocess.run(["screen", "-S", "trade", "-X", "quit"], capture_output=True)  # ensure
    time.sleep(1)
    subprocess.run(
        ["screen", "-dmS", "trade", "bash", "-c",
         "cd /home/myuser/websocket_new && python3 -u sim_trade.py > /tmp/trade.log 2>&1"],
        capture_output=True
    )

    return {
        "deployed": True,
        "backup": backup_path,
        "params": {
            "SPOT_BB_PERIOD": bb_period, "SPOT_BB_STD_MULT": bb_std,
            "SPOT_MIN_HOURS": bb_min_hours, "SPOT_HL_WINDOW": bb_hl_window,
            "SPOT_HL_MIN": bb_hl_min, "SPOT_GAIN_FILTER_PCT": daily_gain,
            "FUT_MAX_DAILY_TP": fut_max_daily_tp,
        }
    }

def _update_hybrid_defaults(bb_period, bb_std, bb_min_hours, bb_hl_window, bb_hl_min, daily_gain, fut_max_daily_tp):
    """同步更新 hybrid.rs 默认参数"""
    # 参数校验 (HIGH-055 fix)
    if bb_period < 2 or bb_std <= 0 or bb_min_hours < 1:
        print(f"ERROR: 非法参数 period={bb_period} std={bb_std} min_hours={bb_min_hours}, 拒绝写入")
        return
    if bb_hl_window < 1 or bb_hl_min < 1 or bb_hl_min > bb_hl_window:
        print(f"ERROR: 非法参数 hl_window={bb_hl_window} hl_min={bb_hl_min}, 拒绝写入")
        return

    filepath = "/home/myuser/backtester-rs/src/hybrid.rs"
    with open(filepath) as f:
        content = f.read()

    import re
    # BBParams struct内替换 (HIGH-054 fix: 限定在BBParams块内)
    bb_block = re.search(r'struct BBParams\s*\{[^}]*\}', content)
    if bb_block:
        old_block = bb_block.group(0)
        new_block = old_block
        new_block = re.sub(r'period:\s*\d+', f'period: {bb_period}', new_block)
        new_block = re.sub(r'std_mult:\s*[0-9.]+', f'std_mult: {bb_std}', new_block)
        new_block = re.sub(r'min_hours:\s*\d+', f'min_hours: {bb_min_hours}', new_block)
        new_block = re.sub(r'hl_window:\s*\d+', f'hl_window: {bb_hl_window}', new_block)
        new_block = re.sub(r'hl_min:\s*\d+', f'hl_min: {bb_hl_min}', new_block)
        new_block = re.sub(r'daily_gain_pct:\s*[0-9.]+', f'daily_gain_pct: {daily_gain}', new_block)
        new_block = re.sub(r'max_daily_tp:\s*\d+', f'max_daily_tp: {fut_max_daily_tp}', new_block)
        content = content.replace(old_block, new_block)
    else:
        print("WARNING: 未找到BBParams struct, 回退到全局替换")
        content = re.sub(r'period:\s*\d+', f'period: {bb_period}', content)
        content = re.sub(r'std_mult:\s*[0-9.]+', f'std_mult: {bb_std}', content)
        content = re.sub(r'min_hours:\s*\d+', f'min_hours: {bb_min_hours}', content)
        content = re.sub(r'hl_window:\s*\d+', f'hl_window: {bb_hl_window}', content)
        content = re.sub(r'hl_min:\s*\d+', f'hl_min: {bb_hl_min}', content)
        content = re.sub(r'daily_gain_pct:\s*[0-9.]+', f'daily_gain_pct: {daily_gain}', content)
        content = re.sub(r'max_daily_tp:\s*\d+', f'max_daily_tp: {fut_max_daily_tp}', content)

    with open(filepath, 'w') as f:
        f.write(content)

    # Recompile Rust (async, non-blocking)
    subprocess.Popen(
        ["bash", "-c", f"source {CARGO_ENV} && cd {BACKTESTER_DIR} && cargo build --release 2>&1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
