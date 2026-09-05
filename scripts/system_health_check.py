#!/usr/bin/env python3
"""系统健康检查 — 数据采集→特征→训练→预测→公证→下单/挂止损→影子结算→晨报 全链路体检.

设计(2026-09-06): 配套 .agents/skills/health-check/SKILL.md 使用。
时间感知: ref_day = 今天(>=08:00后) 或 昨天(凌晨跑时查昨日产物);
每个环节有 expected_by 时间, 未到时间=NOT_DUE 不计入失败。
用法:
  python3 scripts/system_health_check.py            # 体检, 打印表格, 有FAIL退出码1
  python3 scripts/system_health_check.py --notify   # 有FAIL/WARN时用 alert_monitor 发邮件
  python3 scripts/system_health_check.py --json     # 机器可读输出
"""
import os, sys, json, glob, time, socket, subprocess, shutil, datetime

BASE = '/home/myuser/websocket_new'
DATA = os.path.join(BASE, 'data')
LOGS = os.path.join(BASE, 'logs')
AT = '/home/myuser/.local/share/auto_trade'
CST = datetime.timezone(datetime.timedelta(hours=8))

OK, WARN, FAIL, NOT_DUE = 'OK', 'WARN', 'FAIL', 'NOT_DUE'
_rank = {OK: 0, NOT_DUE: 0, WARN: 1, FAIL: 2}


def now_cst():
    return datetime.datetime.now(CST)


def ref_day():
    n = now_cst()
    return n.date().isoformat() if n.hour >= 8 else (n.date() - datetime.timedelta(days=1)).isoformat()


def minutes_today(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def due(now_min, hhmm, grace=10, pre8=False):
    """环节是否已到判定时间(含grace分钟缓冲). 返回False=NOT_DUE.
    pre8=True(凌晨跑, 基准日=昨天): 昨日全流水线均已到期, 全部可检."""
    if pre8:
        return True
    return now_min >= minutes_today(hhmm) + grace


def mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def age_min(p):
    return (time.time() - mtime(p)) / 60


def tail(p, n=200):
    try:
        with open(p, errors='replace') as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def check(name, status, detail):
    return {'name': name, 'status': status, 'detail': detail}


# ---------------- 各环节检查 ----------------

def chk_cron():
    """CRON注册表: 关键任务必须在 crontab 里(与2026-09-06基线比对)."""
    try:
        tab = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return check('CRON注册表', FAIL, f'crontab -l 失败: {e}')
    expected = [
        ('guardian.py', '进程守护(每分钟)'),
        ('daily_data_collection.py', '06:00 主采集'),
        ('update_klines_oi', '07:30 K线+OI补采'),
        ('daily_universe_snapshot.py', '07:10 宇宙快照'),
        ('coingecko_mcap_collector', '06:10 市值采集'),
        ('binance_exchange_info_collector', '06:12 交易所信息'),
        ('oi_snapshot.py', '06:20 OI快照'),
        ('fetch_etf.py', '06:05 ETF数据'),
        ('sector_fetcher.py', '04:10 板块数据'),
        ('audit_snapshot.py', '08:04 审计快照'),
        ('data_versions_snapshot.py', '08:02 数据版本快照'),
        ('auto_dual_trade.py', '08:05 训练+预测'),
        ('residual_live.py trade', '08:21 实盘开仓'),
        ('residual_live.py reconcile', '每小时:31 对账'),
        ('audit_verify.py', '08:25 审计校验'),
        ('notarize_pred.sh', '08:30 预测公证'),
        ('data_drift_monitor.py', '08:30 数据漂移'),
        ('replay_verify.py', '08:30 重放校验'),
        ('cron_monitor.py', '08:40 失败重试'),
        ('hybrid_tracker.py', '08:45 主臂影子'),
        ('trading_system_github_sync.py', '08:50 GitHub同步'),
        ('forward_ic_check.py', '08:50 前向IC'),
        ('hybrid_s5.py', '08:50 S5对照臂'),
        ('residual_tracker.py', '08:55 残差影子'),
        ('digest_guard.sh', '09:00 晨报保险丝'),
        ('forward_tracker.py', '09:10 前向tracker'),
        ('telegram_group_bot.py signal', '08:20 TG信号'),
        ('telegram_group_bot.py pnl', '09:02 TG盈亏'),
        ('altcoin_volume_alert.py', '09:05 放量警报'),
    ]
    missing = [f'{c}({d})' for c, d in expected if c not in tab]
    if missing:
        return check('CRON注册表', FAIL, f'缺失{len(missing)}条: ' + '; '.join(missing))
    return check('CRON注册表', OK, f'{len(expected)}/{len(expected)} 条关键任务在册')


def chk_processes():
    out = []
    glog = '/tmp/guardian.log'
    if not os.path.exists(glog):
        out.append('guardian.log 不存在')
    elif age_min(glog) > 90:
        out.append(f'guardian.log 已 {age_min(glog):.0f} 分钟未更新(每小时热力图任务应保证≤60min)')
    # signal_api 端口
    port_ok = False
    try:
        s = socket.create_connection(('127.0.0.1', 8080), timeout=2)
        s.close()
        port_ok = True
    except OSError:
        pass
    if not port_ok:
        out.append('signal_api 端口8080未监听(@reboot自启, 需 bash scripts/start_signal_api.sh)')
    if out:
        return check('进程/服务', WARN, '; '.join(out))
    return check('进程/服务', OK, 'guardian日志新鲜(<90min), signal_api:8080监听中')


def chk_disk():
    try:
        u = shutil.disk_usage('/')
        free_gb = u.free / 1e9
        if free_gb < 1:
            return check('磁盘空间', FAIL, f'/ 剩余 {free_gb:.1f}GB')
        if free_gb < 2:
            return check('磁盘空间', WARN, f'/ 剩余 {free_gb:.1f}GB (<2GB)')
        return check('磁盘空间', OK, f'/ 剩余 {free_gb:.1f}GB')
    except Exception as e:
        return check('磁盘空间', WARN, f'检查失败: {e}')


def chk_collect(now_min, rd, pre8=False):
    """数据采集层: K线缓存/宇宙快照/外部数据 新鲜度."""
    if not due(now_min, '07:30', 15, pre8):
        return check('数据采集', NOT_DUE, '07:30后判定')
    issues, infos = [], []
    kl = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    a = age_min(kl)
    if a > 60 * 26:
        issues.append(f'K线缓存 {a/60:.0f}h 未更新(07:30任务失效?)')
    else:
        try:
            n = len(json.load(open(kl))['klines'])
            infos.append(f'K线宇宙 {n} 币')
        except Exception as e:
            issues.append(f'K线缓存损坏: {e}')
    u = os.path.join(DATA, 'universe', f'{rd}.json')
    if not os.path.exists(u):
        issues.append(f'universe/{rd}.json 缺失(07:10任务)')
    # 外部数据(漂移监控覆盖的那批) 26h 内有更新
    for f in ('etf_data/etf_flow.json', 'fear_greed_history.json', 'macro_assets.json',
              'stablecoin_exchange_netflow.json', 'liq_daily.json'):
        p = os.path.join(DATA, f)
        if os.path.exists(p) and age_min(p) > 60 * 26:
            issues.append(f'{f} {age_min(p)/60:.0f}h 未更新')
    if issues:
        return check('数据采集', FAIL, '; '.join(issues))
    return check('数据采集', OK, '; '.join(infos) + '; 外部数据均26h内有更新')


def chk_train_pred(now_min, rd, pre8=False):
    """训练+预测层: pred文件/npz/模型/SAMPLECHK/PERM."""
    if not due(now_min, '08:05', 25, pre8):
        return check('训练预测', NOT_DUE, '08:30后判定')
    issues, infos = [], []
    pf = os.path.join(DATA, f'pred_{rd}.json')
    if not os.path.exists(pf):
        issues.append(f'pred_{rd}.json 缺失(08:05训练未产出? 查 logs/auto_dual.log)')
    else:
        try:
            d = json.load(open(pf))
            tl = len(d.get('top10_long') or [])
            tr = len(d.get('top10_long_residual') or [])
            ts = len(d.get('top10_short') or [])
            if tl == 0:
                issues.append('top10_long 为空')
            if tr == 0:
                infos.append('⚠️ top10_long_residual 为空(残差臂今天无候选)')
            if ts == 0:
                infos.append('top10_short 为空(SHORT被阻断或无信号)')
            infos.append(f'LONG {tl}/残差 {tr}/SHORT {ts}')
        except Exception as e:
            issues.append(f'pred 文件损坏: {e}')
    a_npz = age_min(os.path.join(AT, 'train_data_latest.npz'))
    if a_npz > 60 * 26:
        issues.append(f'训练数据 npz {a_npz/60:.0f}h 未更新')
    else:
        infos.append(f'npz {a_npz/60:.0f}h前更新')
    pkl = os.path.join(AT, 'models', f'xgb_daily_long_{rd.replace("-", "")}.pkl')
    if not os.path.exists(pkl):
        issues.append(f'当日模型 {os.path.basename(pkl)} 缺失')
    # SAMPLECHK(幽灵防复发探针): 训练日应≥1条(满配3条)
    s = [l for l in tail(os.path.join(AT, 'trade.log'), 400) if rd in l and '[SAMPLECHK]' in l]
    if not s:
        issues.append(f'{rd} 无 [SAMPLECHK] 记录(特征构建探针缺失)')
    elif len(s) < 3:
        infos.append(f'⚠️ SAMPLECHK 仅 {len(s)}/3')
    else:
        infos.append(f'SAMPLECHK {len(s)}/3')
    if issues:
        return check('训练预测', FAIL, '; '.join(issues))
    return check('训练预测', OK, '; '.join(infos))


def chk_trade(now_min, rd, pre8=False):
    """下单+挂止损层: residual_live 开仓记录 + 在持SL完整性."""
    if not due(now_min, '08:21', 15, pre8):
        return check('实盘开仓', NOT_DUE, '08:36后判定')
    sp = os.path.join(DATA, 'residual_live_state.json')
    issues, infos = [], []
    try:
        st = json.load(open(sp))
    except Exception as e:
        return check('实盘开仓', FAIL, f'state 损坏: {e}')
    day = st.get('days', {}).get(rd, {})
    opened = day.get('opened') or []
    if day.get('note'):
        infos.append(f"{rd}: 未开仓({day.get('note')})")
    elif opened:
        infos.append(f"{rd} 开{len(opened)}笔 {','.join(opened[:3])}{'...' if len(opened) > 3 else ''}")
    else:
        # 日志兜底(幂等跳过/已完成等场景 state 可能无 opened)
        lg = [l for l in tail(os.path.join(LOGS, 'residual_live.log'), 200)
              if rd in l and ('完成: 开仓' in l or '已开过仓' in l)]
        if lg:
            infos.append(f'{rd} 有开仓/幂等记录')
        else:
            issues.append(f'{rd} 无开仓记录(08:21任务失效? 查 logs/residual_live.log)')
    o = st.get('open', {})
    no_sl = [sym for sym, p in o.items() if not p.get('sl_algo_id') and not p.get('sl_price')]
    if no_sl:
        issues.append(f'在持无SL: {",".join(no_sl[:5])}')
    if len(o) > 30:
        issues.append(f'在持 {len(o)} 笔超过30上限')
    infos.append(f'在持 {len(o)} 笔')
    if issues:
        return check('实盘开仓', FAIL, '; '.join(issues))
    return check('实盘开仓', OK, '; '.join(infos))


def chk_git(now_min, rd, pre8=False):
    """公证+git层: UU冲突/公证结果/远程一致/stash残留."""
    issues, infos = [], []
    try:
        r = subprocess.run(['git', 'status', '--porcelain'], cwd=BASE,
                           capture_output=True, text=True, timeout=10)
        uu = [l for l in r.stdout.splitlines() if l[:2] in ('UU', 'AA', 'DD', 'AU', 'UA', 'DU', 'UD')]
        if uu:
            issues.append(f'git 未解决冲突: {uu[:3]}')
    except Exception as e:
        infos.append(f'git status 失败: {e}')
    if due(now_min, '08:30', 15, pre8):
        lg = [l.strip() for l in tail(os.path.join(LOGS, 'notarize.log'), 100)]
        today_lines = [l for l in lg if l.startswith(rd)]
        if any('notarize done (push OK)' in l for l in today_lines):
            infos.append(f'{rd} 公证 push OK')
        elif any('SKIP公证' in l for l in today_lines):
            infos.append(f'{rd} 公证SKIP(pred未生成)')
        elif any('ERROR' in l for l in today_lines):
            # 失败但已人工补救(git 里存在当日 pred 补提交) -> 降级 WARN
            try:
                gl = subprocess.run(['git', 'log', '--oneline', '--since',
                                     f'{rd} 12:00', '--grep', f'pred: {rd}'],
                                    cwd=BASE, capture_output=True, text=True, timeout=10).stdout
                if gl.strip():
                    infos.append(f'{rd} 08:30公证失败但已人工补提交(查 logs/notarize.log)')
                else:
                    issues.append(f'{rd} 公证失败且未补救(查 logs/notarize.log)')
            except Exception:
                issues.append(f'{rd} 公证失败(查 logs/notarize.log)')
        else:
            issues.append(f'{rd} 无公证完成记录(08:30任务失效?)')
    try:
        subprocess.run(['git', 'fetch', 'xgboot', 'main'], cwd=BASE,
                       capture_output=True, timeout=30)
        behind = subprocess.run(['git', 'rev-list', '--count', 'main..xgboot/main'],
                                cwd=BASE, capture_output=True, text=True, timeout=10).stdout.strip()
        if behind and behind != '0':
            issues.append(f'本地落后远程 {behind} 个提交(08:50同步后未拉回, 正常; 次日公证自动rebase)')
    except Exception:
        infos.append('远程一致性检查跳过(网络)')
    try:
        r = subprocess.run(['git', 'stash', 'list'], cwd=BASE,
                           capture_output=True, text=True, timeout=10)
        n = len([l for l in r.stdout.splitlines() if l.strip()])
        if n:
            infos.append(f'⚠️ {n} 条stash残留(冲突遗留, 可 git stash drop)')
    except Exception:
        pass
    if issues:
        return check('公证/git', FAIL, '; '.join(issues))
    return check('公证/git', OK, '; '.join(infos) if infos else '无UU, 公证正常')


def chk_drift(now_min, rd, pre8=False):
    if not due(now_min, '08:30', 15, pre8):
        return check('数据漂移', NOT_DUE, '08:45后判定')
    p = os.path.join(DATA, 'drift_report.json')
    try:
        dr = json.load(open(p))
    except Exception as e:
        return check('数据漂移', WARN, f'报告读取失败: {e}')
    if dr.get('date') != rd:
        return check('数据漂移', WARN, f'报告日期 {dr.get("date")} != {rd}(08:30任务未跑?)')
    if dr.get('status') == 'OK':
        return check('数据漂移', OK, '无修订/重放一致')
    return check('数据漂移', WARN, f"ALERT {len(dr.get('alerts', []))}项: " + '; '.join(dr.get('alerts', [])[:3]))


def chk_trackers(now_min, rd, pre8=False):
    """影子臂结算层: hybrid/residual/s5 当日入册."""
    if not due(now_min, '08:55', 15, pre8):
        return check('影子结算', NOT_DUE, '09:10后判定')
    issues, infos = [], []
    for f, label in (('hybrid_tracker.json', '主臂'), ('residual_tracker.json', '残差臂'),
                     ('hybrid_tracker_s5.json', 'S5臂')):
        p = os.path.join(DATA, f)
        try:
            d = json.load(open(p))
            if rd in d:
                infos.append(f'{label}✓')
            else:
                issues.append(f'{f} 无 {rd} 条目')
        except Exception as e:
            issues.append(f'{f} 读取失败: {e}')
    if issues:
        return check('影子结算', FAIL, '; '.join(issues))
    return check('影子结算', OK, ' '.join(infos))


def chk_sync(now_min, rd, pre8=False):
    if not due(now_min, '08:50', 15, pre8):
        return check('GitHub同步', NOT_DUE, '09:05后判定')
    p = os.path.join(LOGS, 'trading_system_sync_status.json')
    try:
        st = json.load(open(p))
    except Exception as e:
        return check('GitHub同步', WARN, f'状态读取失败: {e}')
    if st.get('date') != rd:
        return check('GitHub同步', WARN, f'状态日期 {st.get("date")} != {rd}')
    if st.get('status') in ('CHANGED', 'NO_CHANGE'):
        extra = f" 上传{st.get('uploaded', 0)}失败{st.get('failed', 0)}"
        return check('GitHub同步', OK, f"{st.get('status')}{extra}" + (f" ⚠️有失败项" if st.get('failed') else ''))
    return check('GitHub同步', WARN, f"状态异常: {st.get('status')}")


def chk_digest(now_min, rd, pre8=False):
    if not due(now_min, '09:00', 15, pre8):
        return check('晨报发送', NOT_DUE, '09:15后判定')
    lg = tail(os.path.join(LOGS, 'digest.log'), 300)
    txt = ''.join(lg)
    if f'晨报总览 {rd}' in txt and '邮件已发送' in txt:
        guard = [l for l in lg if 'GUARD' in l and rd in l]
        if guard:
            return check('晨报发送', WARN, f'{rd} 已发送(经保险丝备份版! 原文件损坏需修复)')
        return check('晨报发送', OK, f'{rd} 邮件已发送')
    if f'晨报总览 {rd}' in txt:
        return check('晨报发送', WARN, f'{rd} 有编译/生成记录但未见发送确认')
    return check('晨报发送', FAIL, f'{rd} 晨报未发送(09:00任务失效? 查 logs/digest.log)')


def chk_reconcile():
    """对账活性(软检查): state 26h内有落笔即可(无事件时不写日志, 不可强检)."""
    a = age_min(os.path.join(DATA, 'residual_live_state.json'))
    if a > 60 * 26:
        return check('对账活性', WARN, f'state {a/60:.0f}h 未落笔(每小时:31对账失效?)')
    return check('对账活性', OK, f'state {a:.0f}min 前有更新')


# ---------------- 主流程 ----------------

def run_all():
    n = now_cst()
    nm = n.hour * 60 + n.minute
    rd = ref_day()
    pre8 = rd != n.date().isoformat()   # 凌晨跑: 基准日=昨天, 昨日环节全部视为已到期
    results = [
        check('基准日', OK, f'{rd} (现在 {n:%m-%d %H:%M} CST)'),
        chk_cron(), chk_processes(), chk_disk(),
        chk_collect(nm, rd, pre8), chk_train_pred(nm, rd, pre8), chk_trade(nm, rd, pre8),
        chk_git(nm, rd, pre8), chk_drift(nm, rd, pre8), chk_trackers(nm, rd, pre8),
        chk_sync(nm, rd, pre8), chk_digest(nm, rd, pre8), chk_reconcile(),
    ]
    return results


def render(results):
    icon = {OK: '✅', WARN: '⚠️ ', FAIL: '❌', NOT_DUE: '⏳'}
    lines = []
    worst = OK
    for r in results:
        lines.append(f"{icon[r['status']]} {r['name']:<10} {r['detail']}")
        if _rank[r['status']] > _rank[worst]:
            worst = r['status']
    verdict = {'OK': '系统健康', 'WARN': '带警示运行', 'FAIL': '存在故障'}[worst]
    lines.append('')
    lines.append(f"== 总判定: {verdict} ({worst}) ==")
    return '\n'.join(lines), worst


def main():
    args = sys.argv[1:]
    results = run_all()
    text, worst = render(results)
    print(text)
    if '--json' in args:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    if '--notify' in args and worst in (WARN, FAIL):
        try:
            sys.path.insert(0, BASE)
            from alert_monitor import send_email
            send_email(f'系统体检-{worst}: {"存在故障" if worst == FAIL else "带警示运行"}', text)
            print('[notify] 告警邮件已发送')
        except Exception as e:
            print(f'[notify] 邮件发送失败: {e}')
    sys.exit(1 if worst == FAIL else 0)


if __name__ == '__main__':
    main()
