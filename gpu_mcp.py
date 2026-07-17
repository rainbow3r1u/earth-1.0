#!/usr/bin/env python3
"""
GPU MCP Server — 在 GPU 服务器上运行，接受老服务器 Reasonix 的调用
暴露: 跑回测 / 跑 sweep / 查看结果
"""
import os, sys, json, subprocess, time

WS = '/root/reasonix-projects/websocket_new'

TOOLS = {
    "gpu_run_sweep": {
        "description": "运行 Kronos Top-N 爬坡实验 (Top10/20/50/100/200)",
        "params": {}
    },
    "gpu_run_backtest": {
        "description": "运行单次 dual_backtest 回测",
        "params": {"days": "回测天数, 默认30", "stride": "重训练间隔, 默认1"}
    },
    "gpu_get_results": {
        "description": "查看最近回测结果",
        "params": {}
    },
    "gpu_status": {
        "description": "GPU 使用情况",
        "params": {}
    },
}

def handle(method, req_id, params=None):
    name = params.get('name', '') if params else ''
    args = (params.get('arguments', {}) if params else {}) or {}

    if name == 'gpu_status':
        try:
            r = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=10)
            return {'content': [{'type': 'text', 'text': f'GPU: {r.stdout.strip()}'}]}
        except Exception as e:
            return {'content': [{'type': 'text', 'text': f'nvidia-smi failed: {e}'}]}

    elif name == 'gpu_run_sweep':
        subprocess.Popen(
            ['python3', '-u', f'{WS}/kronos_sweep.py'],
            cwd=WS, stdout=open('/tmp/sweep.log', 'w'), stderr=subprocess.STDOUT
        )
        return {'content': [{'type': 'text', 'text': 'Sweep 已启动, 查看: tail -f /tmp/sweep.log'}]}

    elif name == 'gpu_run_backtest':
        days = str(args.get('days', 30))
        stride = str(args.get('stride', 1))
        subprocess.Popen(
            ['python3', '-u', f'{WS}/gpu_backtest.py', days, stride, '0'],  # kronos=0 (104D)
            cwd=WS, stdout=open('/tmp/gpu_bt.log', 'w'), stderr=subprocess.STDOUT
        )
        return {'content': [{'type': 'text', 'text': f'回测已启动 (days={days}, stride={stride}), 查看: tail -f /tmp/backtest.log'}]}

    elif name == 'gpu_get_results':
        results = []
        data_dir = f'{WS}/data'
        if os.path.isdir(data_dir):
            for f in sorted(os.listdir(data_dir), reverse=True):
                if 'dual_backtest' in f or 'kronos_sweep' in f or 'ablate' in f:
                    try:
                        with open(f'{data_dir}/{f}') as fh:
                            d = json.load(fh)
                        s = d.get('summary', d.get('result', {}))
                        results.append(f"{f}: PnL={s.get('total_pnl','?')}, Sharpe={s.get('sharpe','?')}, Win={s.get('win_rate','?')}%")
                    except:
                        results.append(f)
        return {'content': [{'type': 'text', 'text': '\n'.join(results[:20]) or '无结果文件'}]}

    return {'content': [{'type': 'text', 'text': f'Unknown tool: {name}'}]}

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
                    'serverInfo': {'name': 'gpu-mcp', 'version': '1.0'}}}
            elif method == 'notifications/initialized':
                continue
            elif method == 'tools/list':
                tools = []
                for tname, tinfo in TOOLS.items():
                    tools.append({'name': tname, 'description': tinfo['description'],
                                  'inputSchema': {'type': 'object', 'properties': {
                                      k: {'type': 'string', 'description': v}
                                      for k, v in tinfo.get('params', {}).items()
                                  }}})
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools}}
            elif method == 'tools/call':
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': handle(method, rid, req.get('params', {}))}
            else:
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': {}}

            sys.stdout.write(json.dumps(resp) + '\n')
            sys.stdout.flush()
        except Exception:
            break

if __name__ == '__main__':
    main()
