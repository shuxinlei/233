"""
233战法 - AkShare数据源封装
统一处理API调用、重试、错误兜底
"""
import time
import json
import os
import threading
from collections import deque
import numpy as np
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import history_store
import error_monitor


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

# 进程内缓存。回测会对同一批日期/代码反复取数，仅靠 SQLite 仍有大量重复反序列化。
_ZT_POOL_MEM = {}
# 回测批量预取的整段日K: code -> DataFrame(日期已转 Timestamp)
_BULK_KLINE = {}
# 预取覆盖的区间 (start, end)。只有请求日期落在区间内才用预取数据，
# 否则回落到正常取数路径 —— 避免回测残留的缓存把过期切片喂给之后的实盘筛选。
_BULK_RANGE = None
# 交易日历(整个进程只取一次): 回测按日重建股池会反复查日历
_TRADE_CAL = None
_API_RATE_LOCK = threading.Lock()
_API_CALL_TIMES = deque()
_API_LAST_CALL = 0.0


def _cache_sina_sector_map(df: pd.DataFrame, indicator: str):
    """缓存新浪板块名称到 label 的映射，供成分股接口复用。"""
    if not df.empty and "板块" in df.columns and "label" in df.columns:
        _SINA_SECTOR_MAP[indicator] = dict(zip(
            df["板块"].astype(str), df["label"].astype(str)
        ))


def _wait_for_api_slot():
    """全进程限频，所有 AkShare 请求共享一个节流器。"""
    global _API_LAST_CALL
    while True:
        with _API_RATE_LOCK:
            now = time.monotonic()
            while _API_CALL_TIMES and now - _API_CALL_TIMES[0] >= 60:
                _API_CALL_TIMES.popleft()
            interval_wait = max(0.0, config.API_MIN_INTERVAL - (now - _API_LAST_CALL))
            window_wait = 0.0
            if len(_API_CALL_TIMES) >= config.API_MAX_CALLS_PER_MINUTE:
                window_wait = max(0.0, 60 - (now - _API_CALL_TIMES[0]))
            wait = max(interval_wait, window_wait)
            if wait <= 0:
                current = time.monotonic()
                _API_LAST_CALL = current
                _API_CALL_TIMES.append(current)
                return
        time.sleep(wait)


def api_rate_status():
    """返回限频器当前状态，供 Web 监控使用。"""
    with _API_RATE_LOCK:
        now = time.monotonic()
        while _API_CALL_TIMES and now - _API_CALL_TIMES[0] >= 60:
            _API_CALL_TIMES.popleft()
        return {
            "calls_last_minute": len(_API_CALL_TIMES),
            "max_calls_per_minute": config.API_MAX_CALLS_PER_MINUTE,
            "min_interval_seconds": config.API_MIN_INTERVAL,
        }


def _retry(func, *args, log_errors=True, attempts=None, **kwargs):
    """带重试的API调用"""
    retry_count = config.API_RETRY if attempts is None else attempts
    for i in range(retry_count):
        try:
            _wait_for_api_slot()
            result = func(*args, **kwargs)
            time.sleep(config.API_DELAY)
            if result is None:
                return pd.DataFrame()
            return result
        except Exception as e:
            if i < retry_count - 1:
                time.sleep(config.API_DELAY * (i + 1) * 2)
            else:
                error_monitor.log_exception(
                    getattr(func, "__name__", repr(func)), e,
                    {"args": [str(x)[:100] for x in args],
                     "kwargs": {k: str(v)[:100] for k, v in kwargs.items()}},
                    retry_count=retry_count,
                )
                if log_errors:
                    print(f"  [API错误] {e}")
                return pd.DataFrame()
    return pd.DataFrame()


def _trade_calendar() -> pd.DatetimeIndex:
    """交易日历，进程内缓存 + 快照按天失效。"""
    global _TRADE_CAL
    if _TRADE_CAL is not None:
        return _TRADE_CAL
    cache_key = datetime.now().strftime('%Y%m%d')
    df = history_store.load_dataframe("trade_calendar", cache_key)
    if df.empty:
        try:
            df = ak.tool_trade_date_hist_sina()
        except Exception as e:
            error_monitor.log_exception("trade_calendar", e)
            print(f"  [错误] 获取交易日历失败: {e}")
            return pd.DatetimeIndex([])
        history_store.save_dataframe("trade_calendar", cache_key, df)
    _TRADE_CAL = pd.DatetimeIndex(pd.to_datetime(df['trade_date'])).sort_values()
    return _TRADE_CAL


def get_trading_dates(n: int = 30, end_date=None) -> list:
    """获取截止 end_date(含)的最近n个交易日列表(YYYYMMDD格式)"""
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    end = pd.Timestamp(str(end_date)).normalize()
    cal = _trade_calendar()
    if len(cal) == 0:
        return []
    return [d.strftime('%Y%m%d') for d in cal[cal <= end][-n:]]


def get_trading_dates_between(start, end) -> list:
    """获取 [start, end] 闭区间内的交易日列表(YYYYMMDD格式)"""
    cal = _trade_calendar()
    if len(cal) == 0:
        return []
    lo = pd.Timestamp(str(start)).normalize()
    hi = pd.Timestamp(str(end)).normalize()
    return [d.strftime('%Y%m%d') for d in cal[(cal >= lo) & (cal <= hi)]]


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
    if key in _ZT_POOL_MEM:
        return _ZT_POOL_MEM[key]
    cached = history_store.load_dataframe("zt_pool", key)
    if not cached.empty:
        _ZT_POOL_MEM[key] = cached
        return cached
    df = _retry(ak.stock_zt_pool_em, date=date)
    history_store.save_dataframe("zt_pool", key, df)
    _ZT_POOL_MEM[key] = df
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


# 新浪源英文列名 → 统一中文
_KLINE_COL_MAP = {
    'date': '日期', 'open': '开盘', 'close': '收盘',
    'high': '最高', 'low': '最低', 'volume': '成交量',
    'amount': '成交额', 'turnover': '换手率'
}


def _snapshot_is_final(data_kind: str, cache_key: str, end: str) -> bool:
    """
    判断日K快照是否已覆盖到请求区间的末尾。

    缓存键里的 end 只是"请求"的结束日。若快照是在 end 当天(或更早)抓的，
    那天的日K可能还没发布，帧的最后一根其实更早 —— 键承诺了它没有的覆盖范围。
    只有抓取日晚于 end，数据才算最终版。
    """
    fetched_at = history_store.snapshot_fetched_at(data_kind, cache_key)
    if not fetched_at:
        return False
    fetched_date = str(fetched_at)[:10].replace('-', '')
    return fetched_date > str(end)


def _normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名，并把新浪的小数换手率(0.12=12%)转成百分比。"""
    if df.empty:
        return df
    df = df.rename(columns={k: v for k, v in _KLINE_COL_MAP.items() if k in df.columns})
    if '换手率' in df.columns:
        df['换手率'] = df['换手率'] * 100
    return df


def get_daily_kline(symbol: str, days: int = 20, end_date=None) -> pd.DataFrame:
    """
    获取个股日K线(前复权) - 使用新浪数据源
    返回列(统一中文): 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率

    若该代码已被 prefetch_klines 预取，直接从内存整段切片，不再请求接口。
    """
    code = str(symbol).zfill(6)
    if code in _BULK_KLINE and _bulk_covers(end_date):
        return slice_bulk_kline(code, days, end_date)

    end = str(end_date or datetime.now().strftime('%Y%m%d'))
    end_dt = datetime.strptime(end, '%Y%m%d')
    start = (end_dt - timedelta(days=days * 2 + 10)).strftime('%Y%m%d')
    cache_key = f"{code}:{end}:{days}"
    if _snapshot_is_final("daily_kline", cache_key, end):
        cached = history_store.load_dataframe("daily_kline", cache_key)
        if not cached.empty:
            return cached
    df = _retry(
        ak.stock_zh_a_daily,
        symbol=_to_sina_symbol(code), start_date=start, end_date=end, adjust="qfq"
    )
    if df.empty:
        return df
    result = _normalize_kline(df).tail(days).reset_index(drop=True)
    history_store.save_dataframe("daily_kline", cache_key, result)
    return result


# ==================== 回测: 整段日K预取 ====================

def _fetch_kline_range(code: str, start: str, end: str) -> pd.DataFrame:
    """取单只股票 [start, end] 整段日K，快照缓存按代码+区间存。"""
    cache_key = f"{code}:{start}:{end}"
    df = pd.DataFrame()
    if _snapshot_is_final("kline_range", cache_key, end):
        df = history_store.load_dataframe("kline_range", cache_key)
    if df.empty:
        raw = _retry(
            ak.stock_zh_a_daily,
            symbol=_to_sina_symbol(code), start_date=start, end_date=end, adjust="qfq"
        )
        df = _normalize_kline(raw)
        if df.empty:
            return df
        history_store.save_dataframe("kline_range", cache_key, df)
    if '日期' in df.columns:
        df = df.copy()
        df['日期'] = pd.to_datetime(df['日期'])
        df = df.sort_values('日期').reset_index(drop=True)
    return df


def _bulk_covers(end_date) -> bool:
    """请求的截止日期是否落在预取区间内。"""
    if _BULK_RANGE is None:
        return False
    end = pd.Timestamp(str(end_date or datetime.now().strftime('%Y%m%d')))
    return pd.Timestamp(_BULK_RANGE[0]) <= end <= pd.Timestamp(_BULK_RANGE[1])


def prefetch_klines(codes, start: str, end: str, progress_every: int = 50) -> int:
    """
    批量预取整段日K到内存，供回测按日切片。

    回测要对每个交易日重建股池，若逐日调 get_daily_kline，同一只股票会被
    不同 end_date 反复请求几十次。这里每只股票只取一次整段，之后全部走内存切片。
    返回成功取到数据的股票数。
    """
    global _BULK_RANGE
    if _BULK_RANGE != (start, end):
        # 换区间就重取，否则旧区间的窄帧会被当成新区间的数据用
        _BULK_KLINE.clear()
        _BULK_RANGE = (start, end)
    uniq = list(dict.fromkeys(str(c).zfill(6) for c in codes))
    todo = [c for c in uniq if c not in _BULK_KLINE]
    total = len(todo)
    for i, code in enumerate(todo):
        if progress_every and (i == 0 or (i + 1) % progress_every == 0):
            print(f"  [{i+1}/{total}] 预取日K...")
        _BULK_KLINE[code] = _fetch_kline_range(code, start, end)
    return sum(1 for c in uniq if not _BULK_KLINE.get(c, pd.DataFrame()).empty)


def slice_bulk_kline(code: str, days: int = 20, end_date=None) -> pd.DataFrame:
    """从预取的整段日K中截取截止 end_date(含)的最后 days 根。"""
    df = _BULK_KLINE.get(str(code).zfill(6))
    if df is None or df.empty:
        return pd.DataFrame()
    if end_date is None:
        sub = df
    else:
        sub = df[df['日期'] <= pd.Timestamp(str(end_date))]
    return sub.tail(days).reset_index(drop=True)


def get_bulk_kline(code: str) -> pd.DataFrame:
    """取预取的整段日K原始帧(回测模拟买卖时按日期定位用)。"""
    return _BULK_KLINE.get(str(code).zfill(6), pd.DataFrame())


def clear_bulk_klines():
    """清空预取缓存。日K与策略参数无关，参数扫描跨轮可复用，不必每轮清。"""
    global _BULK_RANGE
    _BULK_KLINE.clear()
    _BULK_RANGE = None


def get_index_daily(symbol: str = "sh000300") -> pd.DataFrame:
    """取指数日线，用作回测基准。"""
    cache_key = f"{symbol}:{datetime.now().strftime('%Y%m%d')}"
    cached = history_store.load_dataframe("index_daily", cache_key)
    if not cached.empty:
        df = cached
    else:
        df = _retry(ak.stock_zh_index_daily, symbol=symbol)
        if df.empty:
            return df
        history_store.save_dataframe("index_daily", cache_key, df)
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values('date').reset_index(drop=True)


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


def save_pool(pool: list, filepath: str = None):
    """保存核心股池到JSON。filepath 默认在调用时解析，便于测试重定向。"""
    filepath = filepath or POOL_FILE
    data = []
    for s in pool:
        d = {k: v for k, v in vars(s).items()}
        data.append(d)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_NumpyJSONEncoder)


def load_pool(filepath: str = None) -> list:
    """加载核心股池，原样返回存档内容。filepath 默认在调用时解析。"""
    filepath = filepath or POOL_FILE
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    from models import StockInfo
    return [StockInfo(**d) for d in data]


def apply_pool_constraints(pool: list):
    """
    用当前配置约束股池，返回 (保留, 剔除)。

    存档可能超过当前核心池大小，直接使用会让盘中扫描超出人工复核范围。
    但这一步刻意不放在 load_pool 里: 加载器静默改数据会让
    stock_pool.json / history 存档 与 实际使用的股池 长期不一致且无从察觉。
    独立成函数，调用方才能把剔除情况打出来。
    股池按评分降序存储，所以截断保留的是评分最高的部分。
    """
    kept, dropped = list(pool), []
    if len(kept) > config.POOL_SIZE:
        dropped = kept[config.POOL_SIZE:]
        kept = kept[:config.POOL_SIZE]
    return kept, dropped


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
