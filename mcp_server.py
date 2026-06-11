#!/usr/bin/env python3
"""
轻量 MCP 工具服务 — AI可调用币安/CoinGecko/新闻API获取实时数据
"""
import requests, json, os, sys

BINANCE = "https://api.binance.com/api/v3"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"

TOOLS = {
    "get_price": {
        "description": "获取币安现货当前价格",
        "params": {"symbol": "交易对，如BTCUSDT"}
    },
    "get_klines": {
        "description": "获取日线K线数据(最近N天)",
        "params": {"symbol": "交易对", "limit": "天数，默认30"}
    },
    "get_24h_ticker": {
        "description": "获取24小时行情(价格/涨跌/成交量)",
        "params": {"symbol": "交易对"}
    },
    "get_orderbook": {
        "description": "获取订单簿深度(买卖盘前10档)",
        "params": {"symbol": "交易对"}
    },
    "get_funding_rate": {
        "description": "获取合约资金费率",
        "params": {"symbol": "交易对，如BTCUSDT"}
    },
    "get_sector_data": {
        "description": "获取板块资金流向(Coingecko)",
        "params": {}
    },
    "search_news": {
        "description": "搜索历史日报新闻",
        "params": {"q": "搜索关键词"}
    },
    "scan_accumulation": {
        "description": "扫描TON式吸筹信号(量比>5x + EMA50下 + 低涨幅)",
        "params": {"symbol": "可选，指定币种；不传则返回全量TOP10"}
    },
}

def execute_tool(name: str, params: dict) -> str:
    try:
        if name == "get_price":
            sym = str(params.get("symbol", "BTCUSDT"))
            r = requests.get(f"{BINANCE}/ticker/price", params={"symbol": sym}, timeout=5)
            r.raise_for_status()
            return json.dumps(r.json())

        elif name == "get_klines":
            sym = str(params.get("symbol", "BTCUSDT"))
            try:
                limit = int(params.get("limit", 30))
            except (ValueError, TypeError):
                limit = 30
            r = requests.get(f"{BINANCE}/klines", params={"symbol": sym, "interval": "1d", "limit": min(limit, 90)}, timeout=10)
            r.raise_for_status()
            kls = r.json()
            result = []
            for k in kls[-5:]:
                result.append({"t": k[0]//1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
            return json.dumps({"recent_days": len(kls), "last_5": result})

        elif name == "get_24h_ticker":
            sym = params.get("symbol", "BTCUSDT")
            r = requests.get(f"{BINANCE}/ticker/24hr", params={"symbol": sym}, timeout=5)
            d = r.json()
            return json.dumps({"price": d.get("lastPrice"), "chg_pct": d.get("priceChangePercent"), "high": d.get("highPrice"), "low": d.get("lowPrice"), "vol": d.get("quoteVolume")})

        elif name == "get_orderbook":
            sym = params.get("symbol", "BTCUSDT")
            r = requests.get(f"{BINANCE}/depth", params={"symbol": sym, "limit": 10}, timeout=5)
            d = r.json()
            bids = d.get("bids", [])[:3]
            asks = d.get("asks", [])[:3]
            return json.dumps({"top_bids": bids, "top_asks": asks})

        elif name == "get_funding_rate":
            sym = params.get("symbol", "BTCUSDT")
            r = requests.get(f"{BINANCE_FAPI}/premiumIndex", params={"symbol": sym}, timeout=5)
            d = r.json()
            return json.dumps({"funding_rate": d.get("lastFundingRate"), "mark_price": d.get("markPrice")})

        elif name == "get_sector_data":
            try:
                with open("/tmp/sector_heatmap.json", "r") as f:
                    data = json.load(f)
                top5 = [{"name": s["name"], "chg_pct": s["mc_change_pct"]} for s in data[:5]]
                return json.dumps(top5)
            except:
                return "{}"

        elif name == "search_news":
            q = params.get("q", "")
            archive = "/home/myuser/news_monitor/archive"
            results = []
            try:
                for fname in sorted(os.listdir(archive), reverse=True)[:60]:
                    if not fname.endswith('.txt'): continue
                    with open(os.path.join(archive, fname)) as f:
                        for line in f:
                            if q.lower() in line.lower() and len(line.strip()) > 15:
                                results.append(line.strip()[:150])
                                if len(results) >= 5: break
                    if len(results) >= 5: break
            except: pass
            return json.dumps(results)

        elif name == "scan_accumulation":
            sym = params.get("symbol", "")
            try:
                with open("/tmp/accumulation_scan.json", "r") as f:
                    data = json.load(f)
                if sym:
                    filtered = [r for r in data.get("results", []) if r["symbol"].upper() == sym.upper()]
                    return json.dumps(filtered[:3])
                return json.dumps(data.get("results", [])[:10])
            except:
                return "[]"

        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})

def get_tools_schema():
    """返回OpenAI/MCP格式的工具定义"""
    schema = []
    for name, info in TOOLS.items():
        props = {}
        for pname, pdesc in info["params"].items():
            props[pname] = {"type": "string", "description": pdesc}
        schema.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": {"type": "object", "properties": props, "required": []}
            }
        })
    return schema

# ── MCP stdio 主循环 ──
def main():
    while True:
        try:
            line = sys.stdin.readline()
            if not line: break
            req = json.loads(line.strip())
            method = req.get('method', '')
            rid = req.get('id')

            if method == 'initialize':
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {
                    'protocolVersion': '2024-11-05',
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'data-mcp', 'version': '1.0'}}}
            elif method == 'notifications/initialized':
                continue
            elif method == 'tools/list':
                tools = []
                for t in get_tools_schema():
                    tools.append({
                        'name': t['function']['name'],
                        'description': t['function']['description'],
                        'inputSchema': t['function']['parameters']
                    })
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools}}
            elif method == 'tools/call':
                name = req.get('params', {}).get('name', '')
                args = req.get('params', {}).get('arguments', {})
                result = execute_tool(name, args)
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': result}]}}
            else:
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {}}

            sys.stdout.write(json.dumps(resp) + '\n')
            sys.stdout.flush()
        except Exception:
            break

if __name__ == '__main__':
    main()
