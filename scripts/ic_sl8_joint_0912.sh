#!/bin/bash
# IC拐点+SL8首批到期 联合观察提醒 — 2026-09-12 一次性触发后自删 (2026-09-08 用户要求)
# 背景: ①IC_L自9/5起二次走弱(-0.133/-0.149), 按4~6天自愈标尺, 9/10~9/12应现拐点
#       ②9/8开的SL-8%首批9笔(CHIP/COTI/PUMP/BMT/MUBARAK/ONG/VVV/LIT/CHILLGUY)于9/11到期
#       ③两件事的答案一起到; 若9/10~9/12 IC未现回升拐点→非"修复中"而是"alpha衰减"信号(升级决策项)
MARK=/tmp/ic_sl8_joint_0912.flag
if [ -f "$MARK" ]; then exit 0; fi
TODAY=$(date +%Y-%m-%d)
if [ "$TODAY" != "2026-09-12" ]; then exit 0; fi
touch "$MARK"
cd /home/myuser/websocket_new && /usr/bin/python3 - << 'EOF'
from alert_monitor import send_email
body_html = """
<div style="font-family:'Microsoft YaHei';max-width:720px;">
<h2 style="color:#1565c0;">IC拐点 + SL-8%首批到期 联合观察日 (9/12)</h2>
<p><b>今天是 2026-09-12, 两个观察窗口同时到期, 联合判读。</b></p>
<h3>观察① IC_L 自愈拐点 (9/10~9/12窗口)</h3>
<ul>
<li>背景: IC_L 自 9/5 二次走弱 (-0.133 / -0.149), 5日均 -0.057, 四态链=🟠修复中断</li>
<li>本轮与8月那轮的区别: 8月IC深负但批次赚钱(脱锚), 这次9/6~9/7 IC负伴随LONG侧真实连亏(-123.9U/-93.7U) — <b>同步走弱</b></li>
<li><b>判定: 看 data/forward_ic_history_48h.json 最近3日 IC_L</b> — 出现连续回升且5日均回升 = 自愈成立, 回归常态监控</li>
<li>⚠️ 若仍在 -0.10 以下无拐点 → 不是"修复中"而是 <b style="color:#c62828;">alpha衰减信号</b>, 升级为用户决策项(残差臂去留/减仓讨论)</li>
</ul>
<h3>观察② SL-8% 首批到期 (9/8批9笔, 今日08:21应已全部平仓)</h3>
<ul>
<li>首批名单: CHIP/COTI/PUMP/BMT/MUBARAK/ONG/VVV/LIT/CHILLGUY (SL/entry≈0.92已验证)</li>
<li><b>判定: 批内到期胜率 ≥40% = 正常</b> (回测预期75%是脉冲市加权, 吸血期放宽)</li>
<li>同时看: 止损笔均是否≈-3.2U(新口径) vs 旧-2.0U; 9/11批的半程表现作辅助</li>
</ul>
<h3>联合判读矩阵</h3>
<table border="1" cellpadding="6" style="border-collapse:collapse;font-size:13px;">
<tr bgcolor="#f0f0f0"><th>IC拐点</th><th>SL8首批胜率</th><th>解读</th></tr>
<tr><td>✅回升</td><td>≥40%</td><td>双重健康 — 回归常态, 9/21复查线正常等</td></tr>
<tr><td>✅回升</td><td>&lt;40%</td><td>行情tax为主因, SL口径观察延至9/21</td></tr>
<tr><td>❌无拐点</td><td>≥40%</td><td>选币仍有效但排序退化 — 谈组合方式</td></tr>
<tr><td>❌无拐点</td><td>&lt;40%</td><td style="color:#c62828;"><b>双重警示 — alpha衰减+结构失灵, 上升为最高优先级决策(残差臂暂停/回GPU排查)</b></td></tr>
</table>
<p style="color:#888;">本邮件为一次性提醒 (2026-09-08 设置), 触发后自动失效。执行: 说"晨报SKILL"+要求IC拐点与SL8首批联合判读即可。</p>
</div>
"""
send_email('IC拐点 + SL-8%首批到期 联合观察日 (9/12)', '', body_html=body_html, priority='high')
print('joint review reminder sent')
EOF
(crontab -l | grep -v 'ic_sl8_joint_0912.sh') | crontab -
