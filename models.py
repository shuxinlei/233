"""
233战法 - 数据模型
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StockInfo:
    """个股信息"""
    code: str
    name: str
    zt_count: int = 0             # 涨停次数
    max_consecutive: int = 0      # 最大连板数
    last_zt_date: str = ""        # 最近涨停日
    turnover_rate: float = 0.0   # 换手率
    red_green_ratio: float = 0.0  # 红肥绿瘦比值
    volume_ratio: float = 0.0     # 量比(近5日/近20日)
    amount: float = 0.0           # 成交额(元)
    avg_amount_5d: float = 0.0   # 5日平均成交额(用于盘中量比代理计算)
    score: float = 0.0            # 综合评分
    sector: str = ""              # 所属板块


@dataclass
class SectorInfo:
    """板块信息"""
    name: str
    change_pct: float = 0.0       # 涨跌幅
    amount: float = 0.0           # 成交额
    rise_count: int = 0           # 上涨家数
    fall_count: int = 0           # 下跌家数
    leader_stock: str = ""        # 领涨股
    leader_change: float = 0.0    # 领涨股涨幅
    board_type: str = ""          # concept / industry


@dataclass
class ScanResult:
    """盘中扫描结果"""
    scan_time: str = ""
    stock_code: str = ""
    stock_name: str = ""
    sector_name: str = ""
    stock_change: float = 0.0     # 个股涨幅
    sector_change: float = 0.0     # 板块涨幅
    volume_ratio: float = 0.0      # 量比
    turnover_rate: float = 0.0     # 换手率
    above_ma5: bool = False       # 是否突破5日线
    zt_gene: int = 0              # 涨停基因次数
    # 买点四条件
    cond_sector: bool = False     # 板块在涨
    cond_stock: bool = False      # 核心在动
    cond_volume: bool = False     # 量能在放
    cond_breakout: bool = False   # 股价在突破
    all_confirmed: bool = False   # 四条件共振
