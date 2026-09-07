"""
233战法 - 配置参数
所有可调参数集中管理，方便后续优化
"""

# ==================== 定时任务 ====================
PRE_MARKET_TIME = "08:30"
SCAN_TIMES = ["09:25", "10:30", "13:00", "14:30"]

# ==================== 涨停基因筛选 ====================
ZT_HISTORY_DAYS = 30       # 统计涨停基因的回看交易日数
ZT_MIN_COUNT = 1            # 最少涨停次数（回看期内）

# ==================== 量价结构筛选 ====================
KLINE_DAYS = 20             # 量价结构回看天数
RED_GREEN_RATIO = 1.5      # 阳线实体均值 / 阴线实体均值 的最小比值
VOLUME_RATIO = 1.2          # 近5日均量 / 近20日均量 的最小比值
TURNOVER_MIN = 3.0          # 最低换手率(%)

# ==================== 辨识度评分权重 ====================
W_ZT_COUNT = 0.4            # 涨停次数
W_LIQUIDITY = 0.3           # 流动性(成交额)
W_PRICE_VOLUME = 0.3        # 量价结构

# 核心股池大小
POOL_SIZE = 50

# 涨停基因池过大时，取评分前N只再做量价筛选(控制K线请求量)
PV_SCAN_MAX = 200

# ==================== 盘中 - 板块确认 ====================
TOP_SECTOR_COUNT = 8        # 取前N个板块(概念+行业混合排名)
SECTOR_RISE_MIN = 2.0       # 板块最低涨幅(%)
USE_CONCEPT_BOARD = True    # 是否扫描概念板块(题材)
USE_INDUSTRY_BOARD = True   # 是否扫描行业板块

# ==================== 盘中 - 龙头确认 ====================
STOCK_RISE_MIN = 5.0        # 个股最低涨幅(%)
STOCK_VOLUME_RATIO = 1.5    # 最低量比
STOCK_TURNOVER_MIN = 5.0    # 最低换手率(%)

# ==================== 盘中 - 买点确认 ====================
# 四条件共振: 板块涨 + 核心动 + 量放 + 突破MA5
BUY_SECTOR_RISE_MIN = 2.0   # 板块最低涨幅(%)
BUY_STOCK_RISE_MIN = 5.0    # 个股最低涨幅(%)
BUY_VOLUME_RATIO = 1.5      # 最低量比
BUY_BREAK_MA5 = True       # 是否要求突破5日线

# ==================== API调用 ====================
API_RETRY = 3               # 失败重试次数
API_DELAY = 0.3             # 调用间隔(秒)，防止频率过快被限制

# ==================== 回测 ====================
# 只回测盘前股池: 按日重建股池(仅用当日之前数据) → 模拟买卖 → 评估
BT_EXIT_MODE = "next_open"   # same_close(T+0,仅参考) / next_open / next_close / nday_close
BT_HOLD_DAYS = 1             # nday_close 模式下的持有交易日数
BT_COST_PCT = 0.2            # 双边交易成本(%): 佣金+印花税+过户费+滑点
BT_MAX_OPEN_GAP = 9.5        # 开盘涨幅超过该值视为一字板买不进，跳过(%)
BT_TOP_N = 0                 # 只取评分前N只; 0 = 用整个核心股池
BT_BENCHMARK = "sh000300"    # 基准指数(新浪代码)
BT_SCORE_BUCKETS = 5         # 评分分位分组数，用于检验评分单调性


def snapshot() -> dict:
    """当前生效参数的快照，随每次运行一起存档，便于回溯与对比。"""
    import sys
    mod = sys.modules[__name__]
    return {
        key: getattr(mod, key) for key in dir(mod)
        if key.isupper() and isinstance(getattr(mod, key), (int, float, str, bool, list))
    }
