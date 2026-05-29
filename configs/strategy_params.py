"""
策略参数配置

⚠️ 与 backtester/config/current_params.json 对应关系:
  - bollinger.period         → BB_CLIMB_PERIOD
  - bollinger.std            → BB_CLIMB_STD_MULT
  - strategy1.min_hours      → BB_CLIMB_MIN_HOURS
  - strategy1.min_volume_24h → VOLUME_24H_FILTER
请保持两处数值一致，否则回测与实盘行为将不一致。
"""

STRATEGY_PARAMS = {
    'strategy1': {
        'name': '稳步抬升',
        'min_hours': 2,  # matches current_params.json BB_CLIMB_MIN_HOURS: 2
        'min_range': 0.005,
        'max_range': 0.05,
        'scan_hours_before': 6,
        'scan_hours_after': 0,
        'top_n': 600,
        'min_volume_24h': 3_000_000,  # matches current_params.json VOLUME_24H_FILTER: 3000000
        'description': '连续N小时震幅0.5%~5%，最低价逐步抬高'
    },

    'bollinger': {
        'name': '布林收敛通道',
        'period': 20,
        'std': 2.0,
        'converge_threshold': 0.05,
        'min_between_k': 5,
        'closeness_threshold': 0.05,
        'description': '布林带收敛(宽度<5%) + 价格在下轨中轨间运行5K+'
    },

    'coin_quality': {
        'name': '币种质量评分',
        'min_score': 40,
        'max_single_day_gain': 5.0,
        'history_days': 19,
        'klines_per_day': 24,
        'description': '两连阳 + 质量评分>=40'
    },

    'deepseek': {
        'name': 'DeepSeek 3.2分析',
        'history_days': 19,
        'description': 'AI分析19天日线数据，推荐做多币种'
    },

    'volume': {
        'name': '合约趋势',
        'history_days': 19,
        'exclude_symbols': ['BTC', 'ETH'],
        'description': '排除BTC/ETH，近19天成交量趋势'
    }
}

BIG_COINS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT',
    'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'LTCUSDT', 'UNIUSDT',
    'TRXUSDT', 'MATICUSDT', 'SHIBUSDT', 'APEUSDT', 'LUNCUSDT', 'FLOKIUSDT',
    'BONKUSDT', 'XAUUSDT', 'XAGUSDT', 'TAOUSDT', 'STOUSDT', 'RIVERUSDT',
    'WLDUSDT', 'TRUMPUSDT', 'PIPPINUSDT', 'SUIUSDT', 'XMRUSDT', 'ZECUSDT',
    'BCHUSDT', 'HYPEUSDT', 'MSTRUSDT', '1000BONKUSDT', 'CRCLUSDT',
    'BEATUSDT', 'CFGUSDT'
]

SCORE_WEIGHTS = {
    'liquidity': {
        'max_score': 30,
        'tiers': [
            (10_000_000_000, 30),
            (5_000_000_000, 25),
            (1_000_000_000, 20),
            (500_000_000, 15),
            (100_000_000, 10),
            (0, 0)
        ]
    },
    'integrity': {
        'max_score': 15,
        'data_age_penalty': 5,
        'completeness_threshold': 0.9
    },
    'volatility': {
        'max_score': 25,
        'ideal_range': (1, 5),
        'acceptable_range': [(0.5, 1), (5, 8)]
    },
    'trend': {
        'max_score': 20,
        'ma_trend_score': 10,
        'slope_score': 10
    },
    'heat': {
        'max_score': 10,
        'high_threshold': 2.0,
        'medium_threshold': 1.5
    }
}
