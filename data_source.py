"""
233战法 - AkShare数据源封装
统一处理API调用、重试、错误兜底
"""
import time
import json
import os
import numpy as np
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import history_store


class _NumpyJSONEncoder(json.JSONEncoder):
    """处理numpy类型的JSON编码器"""
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        return super().default(obj)
from typing import Optional
import config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_EASTMONEY_BOARD_AVAILABLE = True
_SINA_SECTOR_MAP = {}


def _cache_sina_sector_map(df: pd.DataFrame, indicator: str):
    """缓存新浪板块名称到 label 的映射，供成分股接口复用。"""
    if not df.empty and "板块" in df.columns and "label" in df.columns:
        _SINA_SECTOR_MAP[indicator] = dict(zip(
            df["板块"].astype(str), df["label"].astype(str)
        ))


def _retry(func, *args, log_errors=True, attempts=None, **kwargs):
    """带重试的API调用"""
    retry_count = config.API_RETRY if attempts is None else attempts
    for i in range(retry_count):
        try:
            result = func(*args, **kwargs)
            time.sleep(config.API_DELAY)
            if result is None:
                return pd.DataFrame()
            return result
        except Exception as e:
            if i < retry_count - 1:
                time.sleep(config.API_DELAY * (i + 1) * 2)
            else:
                if log_errors:
                    print(f"  [API错误] {e}")
                return pd.DataFrame()
    return pd.DataFrame()


def get_trading_dates(n: int = 30, end_date=None) -> list:
    """获取过去n个交易日列表(YYYYMMDD格式)"""
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    end_date = pd.Timestamp(str(end_date)).normalize()
    try:
        df = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(df['trade_date']).sort_values()
        past = dates[dates <= end_date].tail(n)
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
    key = str(date)
    cached = history_store.load_dataframe("zt_pool", key)
    if not cached.empty:
        return cached
    df = _retry(ak.stock_zt_pool_em, date=date)
    history_store.save_dataframe("zt_pool", key, df)
    return df


def _to_sina_symbol(code: str) -> str:
    """6位代码 → 新浪格式 sz/sh/bj + 6位"""
    code = str(code).zfill(6)
    if code.startswith(('0', '3')):
        return 'sz' + code
    elif code.startswith('6'):
        return 'sh' + code
    else:
        return 'bj' + code


def get_daily_kline(symbol: str, days: int = 20, end_date=None) -> pd.DataFrame:
    """
    获取个股日K线(前复权) - 使用新浪数据源
    返回列(统一中文): 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率
    """
    end = str(end_date or datetime.now().strftime('%Y%m%d'))
    end_dt = datetime.strptime(end, '%Y%m%d')
    start = (end_dt - timedelta(days=days * 2 + 10)).strftime('%Y%m%d')
    cache_key = f"{str(symbol).zfill(6)}:{end}:{days}"
    cached = history_store.load_dataframe("daily_kline", cache_key)
    if not cached.empty:
        return cached
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
    result = df.tail(days).reset_index(drop=True)
    history_store.save_dataframe("daily_kline", cache_key, result)
    return result


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
    history_store.save_dataframe(
        "realtime_quotes", datetime.now().strftime('%Y%m%d_%H%M%S'), df
    )
    return df


def get_industry_boards() -> pd.DataFrame:
    """获取行业板块实时行情"""
    global _EASTMONEY_BOARD_AVAILABLE
    if _EASTMONEY_BOARD_AVAILABLE:
        df = _retry(ak.stock_board_industry_name_em, log_errors=False, attempts=1)
        if not df.empty:
            return df
        _EASTMONEY_BOARD_AVAILABLE = False
        print("  [数据源切换] 东方财富板块接口不可用，本次运行改用新浪板块数据")
    df = _retry(ak.stock_sector_spot, indicator="行业")
    _cache_sina_sector_map(df, "行业")
    history_store.save_dataframe(
        "industry_boards", datetime.now().strftime('%Y%m%d_%H%M%S'), df
    )
    return df


def get_concept_boards() -> pd.DataFrame:
    """获取概念板块(题材)实时行情"""
    global _EASTMONEY_BOARD_AVAILABLE
    if _EASTMONEY_BOARD_AVAILABLE:
        df = _retry(ak.stock_board_concept_name_em, log_errors=False, attempts=1)
        if not df.empty:
            return df
        _EASTMONEY_BOARD_AVAILABLE = False
        print("  [数据源切换] 东方财富板块接口不可用，本次运行改用新浪板块数据")
    df = _retry(ak.stock_sector_spot, indicator="概念")
    _cache_sina_sector_map(df, "概念")
    history_store.save_dataframe(
        "concept_boards", datetime.now().strftime('%Y%m%d_%H%M%S'), df
    )
    return df


def _get_sina_sector_label(board_name: str, board_type: str) -> str:
    """将板块名称映射为新浪成分股接口需要的 label。"""
    indicator = "概念" if board_type == "concept" else "行业"
    if indicator not in _SINA_SECTOR_MAP:
        df = _retry(ak.stock_sector_spot, indicator=indicator)
        if df.empty or "板块" not in df.columns or "label" not in df.columns:
            _SINA_SECTOR_MAP[indicator] = {}
        else:
            _cache_sina_sector_map(df, indicator)
    if not _SINA_SECTOR_MAP.get(indicator):
        return ""
    return _SINA_SECTOR_MAP[indicator].get(str(board_name), "")


def _get_sina_constituents(board_name: str, board_type: str) -> pd.DataFrame:
    """获取新浪板块成分股，并统一成盘中扫描使用的字段。"""
    label = _get_sina_sector_label(board_name, board_type)
    if not label:
        return pd.DataFrame()
    df = _retry(ak.stock_sector_detail, sector=label)
    if df.empty:
        return df
    result = df.rename(columns={
        "code": "代码", "name": "名称", "changepercent": "涨跌幅",
        "turnoverratio": "换手率", "amount": "成交额",
    })
    history_store.save_dataframe(
        "board_constituents", f"{board_type}:{board_name}:{datetime.now().strftime('%Y%m%d_%H%M%S')}", result
    )
    return result


def get_board_constituents(board_name: str, board_type: str = "concept") -> pd.DataFrame:
    """
    获取板块成分股
    board_type: "concept" 概念板块 / "industry" 行业板块
    """
    global _EASTMONEY_BOARD_AVAILABLE
    if _EASTMONEY_BOARD_AVAILABLE:
        if board_type == "concept":
            df = _retry(ak.stock_board_concept_cons_em, symbol=board_name, log_errors=False, attempts=1)
        else:
            df = _retry(ak.stock_board_industry_cons_em, symbol=board_name, log_errors=False, attempts=1)
        if not df.empty:
            return df
        _EASTMONEY_BOARD_AVAILABLE = False
    return _get_sina_constituents(board_name, board_type)


# ==================== 股池持久化 ====================

POOL_FILE = os.path.join(BASE_DIR, "stock_pool.json")


def save_pool(pool: list, filepath: str = POOL_FILE):
    """保存核心股池到JSON"""
    data = []
    for s in pool:
        d = {k: v for k, v in vars(s).items()}
        data.append(d)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_NumpyJSONEncoder)


def load_pool(filepath: str = POOL_FILE) -> list:
    """加载核心股池"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    from models import StockInfo
    return [StockInfo(**d) for d in data]


# ==================== 历史数据存储 ====================

HISTORY_DIR = os.path.join(BASE_DIR, "history")


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
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_NumpyJSONEncoder)
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
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_NumpyJSONEncoder)
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
    # 仅允许访问 history 目录下的 JSON 文件，避免路径遍历。
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith('.json'):
        return {}
    filepath = os.path.join(HISTORY_DIR, safe_name)
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)
