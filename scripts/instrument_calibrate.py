#!/usr/bin/env python3
"""仪器标定 (2026-09-13 立) — 把 AGENTS §8.2 的 7 条 GPU 大实验规程变成可跑的裁判。

为什么需要它: GPU 回测仪器的"可信度"会**静默腐烂**。2026-09-13 实测: 缓存里 dims13-16
(r2/residual/rsi7/rsi14) 吃到样本日当天收盘价 = 一日前视 → 所有绝对水位偏高, 而它唯一的
暴露方式是"概率饱和"这个肉眼签名。规程写在文档里靠人记得执行, 本脚本把它变成一次调用。

⚠️ 连接信息(GPU 的 IP/端口/密码每次都可能变) — 本脚本**从不硬编码**, 按此顺序解析:
   ① 环境变量  GPU_HOST / GPU_PORT / GPU_USER / GPU_PASS
   ② 本地配置  ~/.gpu_conn  (JSON, chmod 600, 不入 git)
   ③ 都没有 → 报错并给出 --set-conn 用法
   更新连接: python3 scripts/instrument_calibrate.py --set-conn 1.2.3.4:22172

用法:
  python3 scripts/instrument_calibrate.py --check         # ①连接/环境 ②串行 ③传参 (约10秒, 默认)
  python3 scripts/instrument_calibrate.py --settlement    # ④结算标定(本地: 自建模拟 vs 影子档, 一致率≥90%)
  python3 scripts/instrument_calibrate.py --full          # 追加 ⑤数据同源 ⑥特征同源 ⑦一日 walk-forward 标定
  python3 scripts/instrument_calibrate.py --set-conn HOST:PORT   # 写 ~/.gpu_conn(交互式输密码)
"""
import os, sys, json, argparse, subprocess, getpass, datetime

BASE = '/home/myuser/websocket_new'
CONF = os.path.expanduser('~/.gpu_conn')
REMOTE = '~/websocket_new'


# ───────────────────────── 连接解析 ─────────────────────────
def load_conn(args=None):
    c = {}
    if args and args.set_conn:
        host, _, port = args.set_conn.partition(':')
        c = dict(host=host, port=port or '22', user=(args.user or 'linux'))
        c['pass'] = args.password or getpass.getpass(f"{c['user']}@{c['host']}:{c['port']} 密码: ")
        json.dump(c, open(CONF, 'w'))
        os.chmod(CONF, 0o600)
        print(f'✅ 连接已写入 {CONF} (chmod 600, 不入 git)')
        return c
    if os.environ.get('GPU_HOST'):
        c = dict(host=os.environ['GPU_HOST'], port=os.environ.get('GPU_PORT', '22'),
                 user=os.environ.get('GPU_USER', 'linux'), password=os.environ.get('GPU_PASS', ''))
    elif os.path.exists(CONF):
        c = json.load(open(CONF))
    else:
        return None
    c['pass'] = c.get('pass') or c.get('password', '')
    return c


def ssh(c, cmd, timeout=120):
    full = ['sshpass', '-p', c['pass'], 'ssh', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30', '-p', str(c['port']), f"{c['user']}@{c['host']}", cmd]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        txt = (r.stdout or '') + (r.stderr or '')
        txt = '\n'.join(l for l in txt.split('\n') if 'Permanently added' not in l)
        return r.returncode, txt
    except subprocess.TimeoutExpired:
        return 124, '(超时)'
    except FileNotFoundError:
        return 127, 'sshpass 未安装 (apt install sshpass)'


def verdict(ok, label, detail=''):
    print(f'   {"✅" if ok else "❌"} {label}' + (f' — {detail}' if detail else ''))
    return ok


# ───────────────────────── ①②③ 基础检查 ─────────────────────────
def check_basic(c):
    print('① 连接与环境')
    rc, out = ssh(c, 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; python3 -V; '
                     f'ls -d {REMOTE} 2>/dev/null || echo NO_REPO', timeout=60)
    if rc != 0:
        verdict(False, '无法连接', out.strip()[:160])
        print(f'   → 更新连接: python3 scripts/instrument_calibrate.py --set-conn HOST:PORT')
        return False
    ok = 'NO_REPO' not in out
    verdict(True, 'SSH 通', out.replace('\n', ' | ').strip()[:150])
    verdict(ok, f'远端仓库 {REMOTE} 存在')
    print()
    print('② GPU 严格串行 (§8.2 第7条)')
    rc, out = ssh(c, 'pgrep -af "gpu_backtest_exp|gpu_replay_prod|gpu_long_momentum|sl_grid_scan|python3 /tmp/" '
                     '| grep -vE "nvidia_gpu_exporter|pgrep|bash -c" | head -5 || true', timeout=60)
    busy = [l.strip() for l in out.strip().split('\n') if l.strip() and 'nvidia_gpu' not in l]
    ok_serial = not busy
    verdict(ok_serial, '无其他 GPU 实验在跑' if ok_serial else f'⚠️ 有 {len(busy)} 个实验在跑, 新实验请等它结束(严格串行)',
            '; '.join(busy)[:150])
    print()
    print('③ 显式传参 (§8.2 第5条) — 跑之前必须确认这几项')
    rc, out = ssh(c, f"grep -nE \"^TOP10_LONG|^SOUP|^SL_PCT|^KRONOS_ON|VOLRAW_FEATS *=\" {REMOTE}/gpu_backtest_exp.py | head -8", timeout=60)
    print('   远端引擎默认值:')
    for l in out.strip().split('\n')[:8]:
        if l.strip():
            print(f'     {l.strip()[:120]}')
    print('   实盘对照: TOP10_LONG=1 / SOUP=1 / SL_PCT=8 / VOLRAW_FEATS=1 / FUND_FEATS=1')
    print('   ⚠️ 跑结构级或标定实验时, 必须显式传上面这些; 不传就是另一套结构')
    return ok_serial


# ───────────────────────── ④ 结算标定(本地) ─────────────────────────
def check_settlement():
    print('④ 结算标定 (本地: 自建模拟 vs 影子档逐笔一致率 ≥90%)')
    coin_sim = f'{BASE}/audit/coin_sim.py'
    if not os.path.exists(coin_sim):
        verdict(False, 'audit/coin_sim.py 不存在', '无法做结算标定')
        return False
    try:
        r = subprocess.run([sys.executable, coin_sim], capture_output=True, text=True, timeout=600, cwd=BASE)
        tail = (r.stdout or '').strip().split('\n')[-12:]
        for l in tail:
            print(f'   {l[:140]}')
        return r.returncode == 0
    except Exception as e:
        verdict(False, f'运行失败: {e}')
        return False


# ───────────────────────── ⑤⑥⑦ --full ─────────────────────────
def check_full(c):
    print('⑤ 数据同源 (§8.2 第1条) — 远端外部数据加载天数 vs 本地')
    probe = ('cd ' + REMOTE + ' && python3 -c "'
             'import daily_predictor as dp;'
             'f=lambda d: len(d) if d else 0;'
             'print(f(dp._load_sent_features()), f(dp._load_chain_tvl()), '
             'f(dp._load_liquidation_features()), f(dp._load_btc_dominance_proxy()))" 2>/dev/null')
    rc, out = ssh(c, probe, timeout=180)
    remote = out.strip().split('\n')[-1] if rc == 0 else '(失败)'
    rc2, out2 = subprocess.run([sys.executable, '-c',
        'import sys; sys.path.insert(0,"."); import daily_predictor as dp;'
        'f=lambda d: len(d) if d else 0;'
        'print(f(dp._load_sent_features()), f(dp._load_chain_tvl()), '
        'f(dp._load_liquidation_features()), f(dp._load_btc_dominance_proxy()))'],
        capture_output=True, text=True, cwd=BASE)
    local = (out2.stdout or '').strip()
    print(f'   远端: {remote}')
    print(f'   本地: {local}')
    verdict(remote == local and remote != '(失败)', '四项加载天数一致' if remote == local else '⚠️ 不一致 → 特征会不同源',
            '(sent / chain_tvl / liq / dominance)')
    print()
    print('⑥⑦ 特征同源 + 一日 walk-forward 标定')
    print('   ⚠️ 这两项需要实跑(约 1~3 分钟), 且必须人工确认真空期; 本脚本只给出判据:')
    print('   ⑥ 特征同源: 取任一共同日, 重建该日特征 vs 生产 pred_feats_*.npz 逐币相关 ≈1.0, 且无整块全零维')
    print('   ⑦ 仪器标定: 概率向量 Spearman ≥0.90 (对照生产 pred 存档 all_long)')
    print('      ⚠️ 2026-09-13 修正: **不要用选币重合度**当标定指标 —— top10 是刀刃(第10/11名概率差<1pp),')
    print('         生产自身重训也只重合 5/10。合格判据 = 概率向量 Spearman≥0.9 + 概率不饱和')
    print('      ⚠️ 饱和检查(头号签名): 复现 max_prob 明显高于生产(如 96% vs 88%) → 先查"特征是否用到样本日当天bar"')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='基础检查(默认)')
    ap.add_argument('--settlement', action='store_true', help='结算标定')
    ap.add_argument('--full', action='store_true', help='全部(含远端数据同源)')
    ap.add_argument('--set-conn', metavar='HOST:PORT', help='写入/更新 GPU 连接')
    ap.add_argument('--user', help='配合 --set-conn')
    ap.add_argument('--password', help='配合 --set-conn(不传则交互输入, 不落命令历史)')
    a = ap.parse_args()

    print(f'=== 仪器标定 {datetime.date.today().isoformat()} (AGENTS §8.2 七条规程的裁判) ===')
    print(f'    ⚠️ GPU 当前是否在用由用户决定 —— 本脚本只保证"要用的时候仪器是合格的"')
    print()
    c = load_conn(a)
    if a.set_conn:
        return 0
    if c is None:
        print('❌ 无 GPU 连接信息。三选一:')
        print('   ① 环境变量: export GPU_HOST=... GPU_PORT=... GPU_USER=... GPU_PASS=...')
        print('   ② 写配置:   python3 scripts/instrument_calibrate.py --set-conn 1.2.3.4:22172')
        print('   ③ 向用户索取当前 IP/端口/密码(GPU 每次重开都会变)')
        return 1
    print(f'   连接: {c["user"]}@{c["host"]}:{c["port"]} (来源: {"环境变量" if os.environ.get("GPU_HOST") else CONF})')
    print()
    ok = True
    if a.full or not (a.settlement and not a.full):
        ok &= check_basic(c)
        print()
    if a.full:
        ok &= check_full(c)
        print()
    if a.settlement or a.full:
        ok &= check_settlement()
        print()
    print('── 结论 ──')
    print('   ' + ('✅ 基础检查通过' if ok else '❌ 有项目不通过, 见上'))
    print('   完整标定顺序(§8.2): ①数据同源 → ②特征同源 → ③仪器标定(Spearman≥0.9) → ④结算标定(≥90%)')
    print('                       → ⑤显式传参 → ⑥增量落盘 → ⑦GPU串行')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
