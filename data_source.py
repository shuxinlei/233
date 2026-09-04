"""
233战法 - AkShare数据源封装
统一处理API调用、重试、错误兜底
"""
import time
import json
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import config


def _retry(func, *args, **kwargs):
    """带重试的API调用"""
    for i in range(config.API_RETRY):
        try:
            result = func(*args, **kwargs)
            time.sleep(config.API_DELAY)
            if result is None:
                return pd.DataFrame()
            return result
        except Exception as e:
            if i < config.API_RETRY - 1:
                time.sleep(config.API_DELAY * (i + 1) * 2)
            else:
                print(f"  [API错误] {e}")
                return pd.DataFrame()
    return pd.DataFrame()


def get_trading_dates(n: int = 30) -> list:
    """获取过去n个交易日列表(YYYYMMDD格式)"""
    try:
        df = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(df['trade_date']).sort_values()
        today = pd.Timestamp.now().normalize()
        past = dates[dates <= today].tail(n)
        return [d.strftime('%Y%m%d') for d in past]
    except Exception as e:
        print(f"  [错误] 获取交易日历失败: {e}")
        return []


def is_trading_day() -> bool:
    """判断今天是否为交易日"""
    today = datetime.now().strftime('%Y%m%d')
    dates = get_trading_dates(3)
    return today in dates


def get_zt_pool(date: str) -> pd.DataFrame:
    """
    获取指定日期涨停池
    返回列: 代码, 名称, 涨跌幅, 连板数, 成交额, 换手率 等
    """
    return _retry(ak.stock_zt_pool_em, date=date)


def _to_sina_symbol(code: str) -> str:
    """6位代码 → 新浪格式 sz/sh/bj + 6位"""
    code = str(code).zfill(6)
    if code.startswith(('0', '3')):
        return 'sz' + code
    elif code.startswith('6'):
        return 'sh' + code
    else:
        return 'bj' + code


def get_daily_kline(symbol: str, days: int = 20) -> pd.DataFrame:
    """
    获取个股日K线(前复权) - 使用新浪数据源
    返回列(统一中文): 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率
    """
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=days * 2 + 10)).strftime('%Y%m%d')
    sina_sym = _to_sina_symbol(symbol)
    df = _retry(
        ak.stock_zh_a_daily,
        symbol=sina_sym, start_date=start, end_date=end, adjust="qfq"
    )
    if df.empty:
        return df
    # 新浪源英文列名 → 统一中文
    col_map = {
        'date': '日期', 'open': '开盘', 'close': '收盘',
        'high': '最高', 'low': '最低', 'volume': '成交量',
        'amount': '成交额', 'turnover': '换手率'
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    # 新浪源换手率为小数(0.12=12%)，统一转为百分比
    if '换手率' in df.columns:
        df['换手率'] = df['换手率'] * 100
    return df.tail(days).reset_index(drop=True)


def get_realtime_quotes() -> pd.DataFrame:
    """
    获取全市场实时行情 - 使用新浪数据源
    返回列: 代码(6位), 名称, 最新价, 涨跌幅, 成交量, 成交额 等
    注意: 新浪源无量比和换手率，需从其他途径获取
    """
    df = _retry(ak.stock_zh_a_spot)
    if df.empty:
        return df
    # 去除交易所前缀 (sz000001 → 000001)
    df['代码'] = df['代码'].astype(str).str[-6:]
    return df


def get_industry_boards() -> pd.DataFrame:
    """获取行业板块实时行情"""
    return _retry(ak.stock_board_industry_name_em)


def get_concept_boards() -> pd.DataFrame:
    """获取概念板块(题材)实时行情"""
    return _retry(ak.stock_board_concept_name_em)


def get_board_constituents(board_name: str, board_type: str = "concept") -> pd.DataFrame:
    """
    获取板块成分股
    board_type: "concept" 概念板块 / "industry" 行业板块
    """
    if board_type == "concept":
        return _retry(ak.stock_board_concept_cons_em, symbol=board_name)
    else:
        return _retry(ak.stock_board_industry_cons_em, symbol=board_name)


# ==================== 股池持久化 ====================

POOL_FILE = "stock_pool.json"


def save_pool(pool: list, filepath: str = POOL_FILE):
    """保存核心股池到JSON"""
    data = []
    for s in pool:
        d = {k: v for k, v in vars(s).items()}
        data.append(d)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_pool(filepath: str = POOL_FILE) -> list:
    """加载核心股池"""
    import os
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    from models import StockInfo
    return [StockInfo(**d) for d in data]


# ==================== 历史数据存储 ====================

HISTORY_DIR = "history"


def _ensure_history_dir():
    """确保历史目录存在"""
    os.makedirs(HISTORY_DIR, exist_ok=True)


def save_pool_history(pool: list, date_str: str = None):
    """存档当日股池到 history/YYYYMMDD_pool.json"""
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    _ensure_history_dir()
    filepath = os.path.join(HISTORY_DIR, f"{date_str}_pool.json")
    data = {
        'date': date_str,
        'time': datetime.now().strftime('%H:%M:%S'),
        'count': len(pool),
        'stocks': [{k: v for k, v in vars(s).items()} for s in pool],
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return filepath


def save_scan_history(results: list, scan_time_label: str, date_str: str = None):
    """存档盘中扫描结果到 history/YYYYMMDD_scan_HHMM.json"""
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    _ensure_history_dir()
    filepath = os.path.join(HISTORY_DIR, f"{date_str}_scan_{scan_time_label.replace(':', '')}.json")
    data = {
        'date': date_str,
        'scan_time': scan_time_label,
        'time': datetime.now().strftime('%H:%M:%S'),
        'count': len(results),
        'results': [vars(r) for r in results],
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return filepath


def list_history() -> list:
    """列出所有历史记录，按日期倒序"""
    _ensure_history_dir()
    files = sorted(os.listdir(HISTORY_DIR), reverse=True)
    history = []
    for fn in files:
        if not fn.endswith('.json'):
            continue
        parts = fn.replace('.json', '').split('_', 1)
        if len(parts) < 2:
            continue
        date_str = parts[0]
        typ = 'pool' if parts[1] == 'pool' else 'scan'
        scan_time = parts[1].replace('scan', '').replace('_', ':') if typ == 'scan' else ''
        time_str = ''
        count = 0
        try:
            with open(os.path.join(HISTORY_DIR, fn), 'r', encoding='utf-8') as f:
                data = json.load(f)
                time_str = data.get('time', '')
                count = data.get('count', 0)
        except Exception:
            pass
        history.append({
            'filename': fn,
            'date': date_str,
            'type': typ,
            'scan_time': scan_time,
            'time': time_str,
            'count': count,
        })
    return history


def load_history(filename: str) -> dict:
    """加载指定历史文件"""
    _ensure_history_dir()
    filepath = os.path.join(HISTORY_DIR, filename)
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)
