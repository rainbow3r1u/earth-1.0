---
name: "mi-equity"
description: "查米账户(币安#2)总权益。当用户说'米权益'/'查米权益'/'米总权益'/'米的总资金'/'米还有多少钱'时触发。只输出一行: 权益U+折合CNY+汇率。不做任何分析、不附持仓明细。"
---

# 米权益SKILL

> 创建: 2026-09-09 (用户要求: "其他都不要, 只要总权益, 那个金额就好")
> 适用目录: `/home/myuser/websocket_new/` (earth-1.0 仓库)

## 一键执行

```bash
cd /home/myuser/websocket_new && python3 .agents/skills/mi-equity/equity.py
```

## 输出规范

- **只输出一行**: 权益U + 折合CNY + 汇率, 例: `1447.69U ≈ 10,332.51 CNY (rate 7.1322)`
- 汇率: open.er-api.com 主源, frankfurter.app 备源; 双源失败则只出U并标注
- 不附持仓明细、不附浮盈分解、不附分析建议
- 用户若追问细节, 再另行执行 `python3 audit/hybrid_live.py status` 或持仓查询

## 数据口径

- 来源: HYBRID_BINANCE 凭证 → `fapi.binance.com/fapi/v2/account` → `totalMarginBalance`
- 口径 = 钱包余额(已实现) + 全部持仓未实现盈亏 = 总权益, 与 `audit/hybrid_live.py status` 的"总权益"同源
- 实时值, 无缓存
