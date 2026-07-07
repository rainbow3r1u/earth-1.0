#!/usr/bin/env python3
"""
Cron任务失败检测+自动重试脚本
功能:
  1. 检查daily_data_collection.py是否在指定时间内成功运行
  2. 检查auto_dual_trade.py是否在指定时间内成功运行
  3. 若失败, 自动重试一次
  4. 重试仍失败, 发送邮件告警

Cron配置 (每小时检查):
  30 6 * * * cd /home/myuser/websocket_new && python3 cron_monitor.py >> logs/cron_monitor.log 2>&1
  30 8 * * * cd /home/myuser/websocket_new && python3 cron_monitor.py >> logs/cron_monitor.log 2>&1
"""
import os
import sys
import time
import json
import subprocess
from datetime import datetime, timezone, timedelta

# 复用alert_monitor的邮件发送
sys.path.insert(0, '/home/myuser/websocket_new')
from alert_monitor import send_email, should_alert, load_alert_state, save_alert_state

CST = timezone(timedelta(hours=8))
LOG_DIR = '/home/myuser/websocket_new/logs'
RETRY_STATE = f'{LOG_DIR}/cron_retry_state.json'

# 任务定义: (任务名, 日志文件, 期望小时, 超时分钟, 重试命令)
TASKS = [
    {
        'name': '数据采集',
        'log_file': f'{LOG_DIR}/collect.log',
        'expected_hour': 6,        # 06:00 CST
        'timeout_minutes': 30,     # 06:30后检查
        'retry_cmd': 'cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 daily_data_collection.py',
        'success_marker': '采集完成',
    },
    {
        'name': '交易预测',
        'log_file': f'{LOG_DIR}/auto_dual.log',
        'expected_hour': 8,        # 08:05 CST
        'timeout_minutes': 35,     # 08:40后检查
        'retry_cmd': 'cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 auto_dual_trade.py',
        'success_marker': '训练数据已保存',  # 训练完成的标志
    },
]


def check_task_ran_today(task):
    """检查任务今日是否成功运行 (日志中有success_marker且时间戳为今日)

    注意: 不同任务日志时区不一致 — daily_data_collection.py 用 UTC 打印时间戳,
    auto_dual_trade.py 用 CST (服务器本地时区)。这里同时匹配 UTC 和 CST 两个
    日期字符串, 兼容两种日志。数据采集 06:00 CST = 前一天 22:00 UTC, 故需取
    两个时区的"今日"才能覆盖跨天场景。
    """
    if not os.path.exists(task['log_file']):
        return False, '日志文件不存在'

    # 同时取 UTC 和 CST 的今日日期, 兼容两种时区的日志时间戳
    today_strs = {
        datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        datetime.now(CST).strftime('%Y-%m-%d'),
    }
    try:
        with open(task['log_file'], 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32768))  # 读最后32KB
            content = f.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return False, f'读取日志失败: {e}'

    # 查找今日的success_marker (匹配任一时区日期)
    lines = content.split('\n')
    for line in reversed(lines):
        if any(ts in line for ts in today_strs) and task['success_marker'] in line:
            return True, f'今日已成功运行 (匹配: {line[:80]})'

    # 检查今日是否有任何输出 (说明运行了但可能失败)
    today_lines = [l for l in lines if any(ts in l for ts in today_strs)]
    if today_lines:
        last_line = today_lines[-1][:100]
        return False, f'今日有运行但未找到成功标志, 最后输出: {last_line}'

    return False, '今日无运行记录'


def retry_task(task):
    """重试任务, 返回是否成功"""
    print(f"[RETRY] 重试任务: {task['name']}")
    try:
        result = subprocess.run(
            task['retry_cmd'],
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,  # 10分钟超时
        )
        # 追加重试输出到日志
        with open(task['log_file'], 'a') as f:
            f.write(f'\n[RETRY {datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}]\n')
            f.write(result.stdout[-4096:] if result.stdout else '')
            if result.stderr:
                f.write(f'\n[STDERR]\n{result.stderr[-2048:]}')

        # 检查重试后是否成功
        time.sleep(2)
        ok, msg = check_task_ran_today(task)
        return ok, msg
    except subprocess.TimeoutExpired:
        return False, '重试超时(10分钟)'
    except Exception as e:
        return False, f'重试异常: {e}'


def main():
    now = datetime.now(CST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Cron监控启动")

    state = load_alert_state()
    any_failure = False

    for task in TASKS:
        # 只在任务预期完成时间后检查
        expected_time = now.replace(hour=task['expected_hour'], minute=task['timeout_minutes'], second=0, microsecond=0)
        if now < expected_time:
            print(f"  [{task['name']}] 未到检查时间 (预期 {expected_time.strftime('%H:%M')})")
            continue

        # 检查任务是否成功
        ok, msg = check_task_ran_today(task)
        if ok:
            print(f"  [{task['name']}] ✅ {msg}")
            continue

        print(f"  [{task['name']}] ❌ 失败: {msg}")

        # 检查今日是否已重试过
        retry_key = f"cron_retry_{task['name']}_{now.strftime('%Y-%m-%d')}"
        if state.get(retry_key):
            # 已重试过, 检查重试后是否成功
            ok2, msg2 = check_task_ran_today(task)
            if ok2:
                print(f"  [{task['name']}] ✅ 重试后成功")
            else:
                print(f"  [{task['name']}] ❌ 重试后仍失败: {msg2}")
                # 发送告警 (带冷却)
                alert_key = f"cron_fail_{task['name']}_{now.strftime('%Y-%m-%d')}"
                if should_alert(alert_key, state):
                    send_email(
                        f'Cron任务失败: {task["name"]}',
                        f'任务: {task["name"]}\n'
                        f'预期时间: {task["expected_hour"]:02d}:00 CST\n'
                        f'失败原因: {msg}\n'
                        f'已自动重试, 仍然失败\n\n'
                        f'请手动检查:\n'
                        f'  1. 日志: {task["log_file"]}\n'
                        f'  2. 手动运行: {task["retry_cmd"]}\n'
                        f'  3. 检查Python环境: python3 -c "import xgboost"',
                        priority='high'
                    )
                    state[alert_key] = time.time()
                any_failure = True
            continue

        # 首次失败, 执行重试
        print(f"  [{task['name']}] 🔄 启动自动重试...")
        state[retry_key] = time.time()
        save_alert_state(state)

        ok_retry, msg_retry = retry_task(task)
        if ok_retry:
            print(f"  [{task['name']}] ✅ 重试成功")
            # 重试成功也发个info邮件
            alert_key = f"cron_retry_ok_{task['name']}_{now.strftime('%Y-%m-%d')}"
            if should_alert(alert_key, state):
                send_email(
                    f'Cron任务重试成功: {task["name"]}',
                    f'任务: {task["name"]}\n'
                    f'首次运行失败, 自动重试成功\n'
                    f'失败原因: {msg}\n'
                    f'重试结果: {msg_retry}',
                    priority='normal'
                )
                state[alert_key] = time.time()
        else:
            print(f"  [{task['name']}] ❌ 重试失败: {msg_retry}")
            alert_key = f"cron_fail_{task['name']}_{now.strftime('%Y-%m-%d')}"
            if should_alert(alert_key, state):
                send_email(
                    f'Cron任务失败: {task["name"]}',
                    f'任务: {task["name"]}\n'
                    f'预期时间: {task["expected_hour"]:02d}:00 CST\n'
                    f'首次失败: {msg}\n'
                    f'重试失败: {msg_retry}\n\n'
                    f'请手动检查:\n'
                    f'  1. 日志: {task["log_file"]}\n'
                    f'  2. 手动运行: {task["retry_cmd"]}',
                    priority='high'
                )
                state[alert_key] = time.time()
            any_failure = True

    save_alert_state(state)
    print(f"[{now.strftime('%H:%M:%S')}] Cron监控完成, {'有失败' if any_failure else '全部正常'}")
    sys.exit(1 if any_failure else 0)


if __name__ == '__main__':
    main()
