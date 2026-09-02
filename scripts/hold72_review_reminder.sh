#!/bin/bash
# 48h vs 72h 终审提醒 — 2026-10-23 一次性触发后自删 (2026-09-02 用户要求, 最高优先级)
# 触发条件: 日期到 2026-10-23 且 /tmp 未置静默标志
MARK=/tmp/hold72_review_sent.flag
if [ -f "$MARK" ]; then exit 0; fi
TODAY=$(date +%Y-%m-%d)
if [ "$TODAY" != "2026-10-23" ]; then exit 0; fi
touch "$MARK"
cd /home/myuser/websocket_new && /usr/bin/python3 - << 'EOF'
from alert_monitor import send_email
body_html = """
<div style="font-family:'Microsoft YaHei';max-width:720px;">
<h2 style="color:#c62828;">⭐⭐⭐ 48h vs 72h 持有窗口终审日 (最高优先级)</h2>
<p><b>今天是 2026-10-23，60 天验证期到期，执行 48h→72h 迁移终审。</b></p>
<h3>背景（2026-09-02 预研结论）</h3>
<ul>
<li>标签=72h终点语义, 但结算链(3.6~3.9/实盘)全是 strict 48h → 结算比标签少收24h右尾</li>
<li>29天580笔重放: 72h比48h多赚 <b style="color:#2e7d32;">+3101U (+111%)</b> (LONG +2246 / SHORT +854)</li>
<li>剔史诗日8/7后仍 <b>+1435U (+51U/天)</b>; 前3大肥日占78% (右尾放大器本质); 逐日胜率59%</li>
</ul>
<h3>今日待执行（详见 docs/优化待办-20260825 ⭐⭐⭐章节）</h3>
<ol>
<li>用<b>残差臂60天1m数据</b>重跑 docs/hold_48_72_experiment.py（改读 residual_tracker.json, 两臂互证）</li>
<li>通过标准: 双臂72h均占优 且 剔头3日后≥+30%/天量级 且 逐日胜率≥55%</li>
<li>通过 → 结算tracker+residual_live实盘+晨报口径一次性迁移72h(标签本就72h语义, 迁移=消除错位)</li>
<li>同时重算实盘资金: 持仓2批→3批(峰值20→30笔), 保证金+50%, 2x/5x爆仓距离压缩评估</li>
<li>不通过 → 维持48h, 错位已知已量化, 记录后关闭议题</li>
</ol>
<p style="color:#888;">本邮件为一次性提醒 (2026-09-02 用户要求归档时设置), 触发后自动失效。</p>
</div>
"""
send_email('⭐48h vs 72h 持有窗口终审日 (最高优先级)', '', body_html=body_html, priority='high')
print('review reminder sent')
EOF
# 发送成功后从crontab移除自身
(crontab -l | grep -v 'hold72_review_reminder.sh') | crontab -
