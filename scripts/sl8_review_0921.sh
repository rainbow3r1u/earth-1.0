#!/bin/bash
# SL-8% 两周复查线 — 2026-09-21 一次性触发后自删 (2026-09-07 用户要求)
# 背景: 9/7 用户拍板 SL 5%→8% (依据TOP10模拟器1585笔网格: 胜率+6pp/右尾截杀96→56/净+2645%名义)
#       复查线: 新口径批次(9/8起)两周内到期胜率 <60% → 吸血期摩擦吞掉脉冲市红利, 届时再议回5%或其他
MARK=/tmp/sl8_review_0921.flag
if [ -f "$MARK" ]; then exit 0; fi
TODAY=$(date +%Y-%m-%d)
if [ "$TODAY" != "2026-09-21" ]; then exit 0; fi
touch "$MARK"
cd /home/myuser/websocket_new && /usr/bin/python3 - << 'EOF'
from alert_monitor import send_email
body_html = """
<div style="font-family:'Microsoft YaHei';max-width:720px;">
<h2 style="color:#e65100;">SL-8% 调参两周复查线 (9/21)</h2>
<p><b>今天是 2026-09-21, SL 5%→8% (9/7) 满两周, 执行复查。</b></p>
<h3>复查数据 (自动可取)</h3>
<ul>
<li>新口径批次: 9/8 起开的批次全部到期结算 (residual_live_state.json + 晨报3.9c生存表)</li>
<li>核心指标: <b>到期胜率</b> — 回测预期 69%→75%, 实盘复查线 60%</li>
<li>辅助: 止损笔均 (预期≈-3.2U, 原-2.0U) / 止损笔数占比 / 右尾(≥+20%)到期单数</li>
</ul>
<h3>判定</h3>
<ol>
<li><b>到期胜率 ≥60%</b> → SL-8% 转正, 观察哨关闭, 纳入常规晨报监控</li>
<li><b>到期胜率 <60%</b> → 吸血期摩擦在吞脉冲市红利 (回测是脉冲市加权), 拿数据找用户再议(回5%/维持/其他)</li>
</ol>
<p style="color:#888;">本邮件为一次性提醒 (2026-09-07 设置), 触发后自动失效。执行时说"晨报SKILL"+要求SL-8%复查即可。</p>
</div>
"""
send_email('SL-8% 调参两周复查线 (9/21)', '', body_html=body_html, priority='high')
print('SL8 review reminder sent')
EOF
(crontab -l | grep -v 'sl8_review_0921.sh') | crontab -
