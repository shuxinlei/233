"""
233战法 - 技术指标计算
"""
import pandas as pd
import numpy as np


def calculate_ma(closes: pd.Series, period: int) -> float:
    """计算移动平均线"""
    if len(closes) < period:
        return float('nan')
    return closes.tail(period).mean()


def calculate_red_green_ratio(kline: pd.DataFrame) -> float:
    """
    红肥绿瘦比值 = 平均阳线实体 / 平均阴线实体
    阳线: 收盘 > 开盘, 实体 = 收盘 - 开盘
    阴线: 收盘 < 开盘, 实体 = 开盘 - 收盘
    比值 > 1.5 视为红肥绿瘦
    """
    if kline.empty:
        return 0.0

    up = kline[kline['收盘'] > kline['开盘']]
    down = kline[kline['收盘'] < kline['开盘']]

    if down.empty or up.empty:
        return 0.0

    avg_up = (up['收盘'] - up['开盘']).mean()
    avg_down = (down['开盘'] - down['收盘']).mean()

    if avg_down == 0:
        return 0.0

    return float(avg_up / avg_down)


def calculate_volume_ratio(kline: pd.DataFrame, short: int = 5, long: int = 20) -> float:
    """
    量比 = 近short日均量 / 近long日均量
    > 1.2 视为放量
    """
    if len(kline) < long:
        return 0.0

    short_avg = kline['成交量'].tail(short).mean()
    long_avg = kline['成交量'].tail(long).mean()

    if long_avg == 0:
        return 0.0

    return float(short_avg / long_avg)


def is_above_ma5(price: float, closes: pd.Series) -> bool:
    """判断当前价格是否突破5日线"""
    ma5 = calculate_ma(closes, 5)
    if np.isnan(ma5):
        return False
    return price > ma5


def get_latest_turnover(kline: pd.DataFrame) -> float:
    """获取最新换手率"""
    if kline.empty:
        return 0.0
    return float(kline.iloc[-1].get('换手率', 0))


def get_latest_amount(kline: pd.DataFrame) -> float:
    """获取最新成交额"""
    if kline.empty:
        return 0.0
    return float(kline.iloc[-1].get('成交额', 0))
