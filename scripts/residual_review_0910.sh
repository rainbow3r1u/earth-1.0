#!/bin/bash
# 残差臂观察期结论 — 2026-09-10 一次性触发后自删 (2026-09-07 用户要求)
# 背景: 残差双轨9/2上线, 用户定的观察期结论节点是9/10 (框架: 明显跑赢主臂→谈转正/加仓;
#       ≈主臂→转P1(OI截断)/组合臂复盘; 更差→止损实盘回GPU排查)
MARK=/tmp/residual_review_0910.flag
if [ -f "$MARK" ]; then exit 0; fi
TODAY=$(date +%Y-%m-%d)
if [ "$TODAY" != "2026-09-10" ]; then exit 0; fi
touch "$MARK"
cd /home/myuser/websocket_new && /usr/bin/python3 - << 'EOF'
from alert_monitor import send_email
body_html = """
<div style="font-family:'Microsoft YaHei';max-width:720px;">
<h2 style="color:#1565c0;">残差臂一周观察期结论日 (9/10)</h2>
<p><b>今天是 2026-09-10, 残差双轨(9/2上线)观察期到期, 出结论。</b></p>
<h3>对账口径 (实盘 vs 主臂影子, 同窗口同费用)</h3>
<ul>
<li>实盘: residual_live.py 9/2~9/10 全部已结算批次 (排除链路测试), 注意 9/7 后是 SL-8% 新口径</li>
<li>主臂对照: hybrid_tracker.json LONG侧同窗口 (48h口径, 差24h窗口注意标注)</li>
<li>辅助: residual_tracker.json 影子臂 vs 主臂 (纯结算对照, 无执行滑点)</li>
</ul>
<h3>结论框架 (用户 9/4 已定)</h3>
<ol>
<li><b>明显跑赢主臂</b> → 谈转正/加仓方案</li>
<li><b>≈主臂</b> → 转 P1(OI截断放开)/组合臂复盘</li>
<li><b>更差</b> → 止损实盘回 GPU 排查</li>
</ol>
<p style="color:#b8860b;">⚠️ 附加背景: 9/7 已调 SL 5%→8% (当天到期的仍是旧口径), 结论需按新旧口径分段解读, 不可混算。</p>
<p style="color:#888;">本邮件为一次性提醒 (2026-09-07 设置), 触发后自动失效。执行时直接说"晨报SKILL"+要求残差臂对账即可, 两个SKILL已沉淀全部口径。</p>
</div>
"""
send_email('残差臂观察期结论日 (9/10)', '', body_html=body_html, priority='high')
print('residual review reminder sent')
EOF
(crontab -l | grep -v 'residual_review_0910.sh') | crontab -
