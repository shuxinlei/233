"""
233战法 - 盘中扫描模块
4个时间节点: 9:25 / 10:30 / 13:00 / 14:30
流程: 板块确认 → 龙头确认 → 买点确认(四条件共振)

数据适配: 新浪实时行情无量比/换手率
- 换手率: 从板块成分股接口获取
- 量比: 用 今日成交额/5日均额 按时间折算代理值
"""
import pandas as pd
from datetime import datetime
from tabulate import tabulate
import config
import data_source
import indicators
import history_store
from models import ScanResult, SectorInfo

RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

# 各扫描时间点已过交易分钟数(用于量比折算)
SCAN_MINUTES = {
    '09:25': 5,
    '10:30': 60,
    '13:00': 120,
    '14:30': 210,
}
TOTAL_MINUTES = 240


def _is_risk_warning(name) -> bool:
    """
    识别风险警示股。

    A股同时存在 ST 与 *ST 两种前缀，且 *ST(退市风险警示)更常见，
    只判 startswith('ST') 会把 *ST 全部漏掉 —— 这类股在5%涨停封板时
    会被误标成可介入。部分数据源用全角星号，一并剥掉。
    """
    return str(name).upper().lstrip('*＊ 　').startswith('ST')


def _limit_up_change(code, name=''):
    """
    按板块规则估算涨停涨幅阈值(%)。

    先判板块再判风险警示: 创业板/科创板的风险警示股涨跌幅限制仍是20%，
    只有主板的风险警示股才是5%。
    """
    code = str(code).zfill(6)
    if code.startswith(('43', '83', '87', '92')):   # 北交所 30%
        return 29.5
    if code.startswith(('30', '68')):               # 创业板/科创板 20%(含风险警示股)
        return 19.5
    if _is_risk_warning(name):                      # 主板风险警示股 5%
        return 4.8
    return 9.5


def _tradeability(code, name, change, price, high):
    """识别封板状态；没有最高价时对触及涨停采取保守判断。"""
    is_limit_up = change >= _limit_up_change(code, name)
    if not is_limit_up:
        return False, False
    is_sealed = not high or price >= high * (1 - config.SEALED_PRICE_TOLERANCE)
    return True, is_sealed


def _calc_vol_ratio_proxy(current_amount, avg_5d_amount, scan_label):
    """
    量比代理值 = 今日成交额 / (5日平均成交额 × 已过分钟占比)
    意义: 若全天匀速成交，当前应完成 (minutes/240) 的额度
    超过1.5倍即为放量
    """
    minutes = SCAN_MINUTES.get(scan_label, 120)
    if avg_5d_amount <= 0 or minutes <= 0:
        return 0.0
    expected = avg_5d_amount * (minutes / TOTAL_MINUTES)
    if expected <= 0:
        return 0.0
    return float(current_amount / expected)


def _get_top_sectors():
    """板块确认: 概念+行业板块按涨幅排名，取前N"""
    sectors = []
    if config.USE_CONCEPT_BOARD:
        df = data_source.get_concept_boards()
        for _, row in df.iterrows() if not df.empty else []:
            sectors.append(SectorInfo(
                name=row.get('板块名称', row.get('板块', '')),
                change_pct=float(row.get('涨跌幅', 0) or 0),
                amount=float(row.get('成交额', row.get('总成交额', 0)) or 0),
                rise_count=int(row.get('上涨家数', 0) or 0),
                fall_count=int(row.get('下跌家数', 0) or 0),
                leader_stock=row.get('领涨股票', row.get('股票名称', '')),
                leader_change=float(row.get('领涨股票-涨跌幅', row.get('个股-涨跌幅', 0)) or 0),
                board_type='concept'
            ))
    if config.USE_INDUSTRY_BOARD:
        df = data_source.get_industry_boards()
        for _, row in df.iterrows() if not df.empty else []:
            sectors.append(SectorInfo(
                name=row.get('板块名称', row.get('板块', '')),
                change_pct=float(row.get('涨跌幅', 0) or 0),
                amount=float(row.get('成交额', row.get('总成交额', 0)) or 0),
                rise_count=int(row.get('上涨家数', 0) or 0),
                fall_count=int(row.get('下跌家数', 0) or 0),
                leader_stock=row.get('领涨股票', row.get('股票名称', '')),
                leader_change=float(row.get('领涨股票-涨跌幅', row.get('个股-涨跌幅', 0)) or 0),
                board_type='industry'
            ))
    sectors.sort(key=lambda x: x.change_pct, reverse=True)
    return [s for s in sectors if s.change_pct >= config.SECTOR_RISE_MIN][:config.TOP_SECTOR_COUNT]


def _find_leaders(sector, pool, quotes, scan_label):
    """
    龙头确认: 板块成分股 ∩ 核心股池
    换手率 ← 板块成分股数据
    量比 ← 代理计算(今日成交额/5日均额按时间折算)
    涨幅/成交额 ← 实时行情
    """
    cons = data_source.get_board_constituents(sector.name, sector.board_type)
    if cons.empty:
        return []

    pool_codes = {s.code for s in pool}
    pool_map = {s.code: s for s in pool}
    is_auction = (scan_label == '09:25')

    candidates = []
    for _, row in cons.iterrows():
        code = str(row.get('代码', '')).zfill(6)
        if code not in pool_codes:
            continue
        q = quotes.get(code)
        if not q:
            continue

        si = pool_map[code]
        change = float(q.get('涨跌幅', 0) or 0)
        amount = float(q.get('成交额', 0) or 0)
        price = float(q.get('最新价', 0) or 0)
        high = float(q.get('最高', 0) or 0)
        # 换手率从板块成分股获取
        turnover = float(row.get('换手率', 0) or 0)
        # 量比代理值
        vol_ratio = _calc_vol_ratio_proxy(amount, si.avg_amount_5d, scan_label)

        candidates.append({
            'code': code,
            'name': row.get('名称', '') or si.name,
            'change': change,
            'volume_ratio': vol_ratio,
            'turnover': turnover,
            'price': price,
            'high': high,
            'amount': amount,
            'zt_gene': si.zt_count
        })

    # 筛选阈值: 9:25竞价阶段放宽
    if is_auction:
        leaders = [c for c in candidates if c['change'] >= 2.0]
    else:
        leaders = [c for c in candidates
                   if c['change'] >= config.STOCK_RISE_MIN
                   and c['volume_ratio'] >= config.STOCK_VOLUME_RATIO
                   and c['turnover'] >= config.STOCK_TURNOVER_MIN]
    leaders.sort(key=lambda x: x['change'], reverse=True)
    return leaders


def _check_buy_point(leader, sector, scan_label):
    """买点确认: 板块涨+核心动+量能放+突破MA5 四条件共振"""
    kline = data_source.get_daily_kline(leader['code'], 10)
    above_ma5 = False
    if not kline.empty and len(kline) >= 5:
        above_ma5 = indicators.is_above_ma5(leader['price'], kline['收盘'])

    is_auction = (scan_label == '09:25')

    c_sec = sector.change_pct >= config.BUY_SECTOR_RISE_MIN
    c_stk = leader['change'] >= (2.0 if is_auction else config.BUY_STOCK_RISE_MIN)
    c_vol = leader['volume_ratio'] >= (0.5 if is_auction else config.BUY_VOLUME_RATIO)
    c_brk = above_ma5 if config.BUY_BREAK_MA5 else True

    is_limit_up, is_sealed = _tradeability(
        leader['code'], leader['name'], leader['change'],
        leader['price'], leader.get('high', 0)
    )
    conditions_ok = c_sec and c_stk and c_vol and c_brk
    blocked = config.EXCLUDE_SEALED_LIMIT_UP and is_sealed
    actionable = conditions_ok and not blocked
    if blocked:
        entry_status = '封板不可买'
    elif actionable:
        entry_status = '可介入'
    elif is_limit_up:
        entry_status = '涨停附近观察'
    else:
        entry_status = '条件不足'

    return ScanResult(
        scan_time=datetime.now().strftime('%H:%M'),
        stock_code=leader['code'], stock_name=leader['name'],
        sector_name=sector.name,
        stock_change=leader['change'], sector_change=sector.change_pct,
        volume_ratio=leader['volume_ratio'],
        turnover_rate=leader['turnover'],
        is_limit_up=is_limit_up,
        is_sealed_limit_up=is_sealed,
        actionable=actionable,
        entry_status=entry_status,
        above_ma5=above_ma5, zt_gene=leader['zt_gene'],
        cond_sector=c_sec, cond_stock=c_stk,
        cond_volume=c_vol, cond_breakout=c_brk,
        all_confirmed=actionable
    )


def scan(scan_time_label):
    """盘中扫描主流程"""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  盘中扫描 {scan_time_label} ({datetime.now().strftime('%H:%M:%S')}){RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    stored = data_source.load_pool()
    if not stored:
        print(f"  {YELLOW}核心股池为空，请先运行盘前筛选 (python main.py --mode pre){RESET}")
        return []
    pool, dropped = data_source.apply_pool_constraints(stored)
    if dropped:
        print(f"  {YELLOW}存档{len(stored)}只，按当前参数剔除{len(dropped)}只"
              f"(超出 POOL_SIZE={config.POOL_SIZE}){RESET}")
    if not pool:
        print(f"  {YELLOW}股池经当前参数过滤后为空，请重跑盘前筛选或放宽参数{RESET}")
        return []
    print(f"  核心股池: {len(pool)}只")

    # Step 1: 板块确认
    print(f"\n{CYAN}[板块确认] 资金方向...{RESET}")
    top_sectors = _get_top_sectors()
    if not top_sectors:
        print(f"  {YELLOW}无涨幅超过{config.SECTOR_RISE_MIN}%的板块{RESET}")
        return []
    rows = [[i+1, s.name[:10], '概念' if s.board_type == 'concept' else '行业',
             f"{s.change_pct:.2f}", s.rise_count, s.fall_count,
             f"{s.leader_stock}({s.leader_change:.1f}%)"]
            for i, s in enumerate(top_sectors)]
    print(tabulate(rows, headers=['排名', '板块', '类型', '涨幅%', '上涨', '下跌', '领涨股'], tablefmt='simple'))

    # 获取全市场实时行情
    print(f"\n{CYAN}[龙头确认] 实时行情匹配...{RESET}")
    quotes_df = data_source.get_realtime_quotes()
    if quotes_df.empty:
        print(f"  {RED}获取实时行情失败{RESET}")
        return []

    quotes = {}
    for _, row in quotes_df.iterrows():
        code = str(row.get('代码', '')).zfill(6)
        if not code:
            continue
        quotes[code] = {
            '最新价': float(row.get('最新价', 0) or 0),
            '涨跌幅': float(row.get('涨跌幅', 0) or 0),
            '成交额': float(row.get('成交额', 0) or 0),
            '成交量': float(row.get('成交量', 0) or 0),
            '最高': float(row.get('最高', 0) or 0),
        }

    # Step 2: 每个强势板块找龙头
    all_results = []
    seen_codes = set()
    for sector in top_sectors:
        leaders = _find_leaders(sector, pool, quotes, scan_time_label)
        if not leaders:
            continue
        print(f"\n  {BOLD}{sector.name}{RESET} ({sector.change_pct:.2f}%)")
        lrows = [[l['code'], l['name'][:6], f"{l['change']:.2f}",
                 f"{l['volume_ratio']:.1f}", f"{l['turnover']:.1f}",
                 f"{l['zt_gene']}次"] for l in leaders]
        print(tabulate(lrows, headers=['代码', '名称', '涨幅%', '量比*', '换手%', '涨停基因'], tablefmt='simple'))

        # Step 3: 买点确认
        for leader in leaders:
            if leader['code'] in seen_codes:
                continue
            seen_codes.add(leader['code'])
            result = _check_buy_point(leader, sector, scan_time_label)
            all_results.append(result)

    # 汇总
    if all_results:
        print(f"\n{CYAN}[买点确认] 四条件共振 + 可介入{RESET}")

        def mark(b):
            return 'Y' if b else 'N'

        brows = []
        for r in all_results:
            # 共振列已含"可介入"，所以必须同时给出状态，否则会出现
            # 四个Y却没有星号、且看不到原因的情况
            star = '★' if r.all_confirmed else ''
            brows.append([r.stock_code, r.stock_name[:6],
                         mark(r.cond_sector), mark(r.cond_stock),
                         mark(r.cond_volume), mark(r.cond_breakout),
                         r.entry_status or '-', star])
        print(tabulate(brows, headers=['代码', '名称', '板块涨', '核心动', '量能放',
                                       '突破MA5', '可介入', '共振'], tablefmt='simple'))

        confirmed = [r for r in all_results if r.all_confirmed]
        if confirmed:
            print(f"\n{GREEN}{BOLD}>>> {len(confirmed)}只标的四条件共振且可介入 <<<{RESET}")
            for r in confirmed:
                print(f"  {GREEN}{r.stock_code} {r.stock_name} | "
                      f"板块:{r.sector_name}({r.sector_change:.1f}%) | "
                      f"涨幅:{r.stock_change:.1f}% | 量比:{r.volume_ratio:.1f} | "
                      f"涨停基因:{r.zt_gene}次{RESET}")
        else:
            print(f"\n{YELLOW}当前无共振确认，继续观察{RESET}")

        # 四条件齐了但买不进的单独列出，否则这些标的在表里只是"少个星号"
        blocked = [r for r in all_results
                   if not r.all_confirmed and r.cond_sector and r.cond_stock
                   and r.cond_volume and r.cond_breakout]
        if blocked:
            print(f"\n{YELLOW}四条件已满足但当前买不进 {len(blocked)}只:{RESET}")
            for r in blocked:
                print(f"  {YELLOW}{r.stock_code} {r.stock_name} | {r.entry_status} | "
                      f"涨幅:{r.stock_change:.1f}% | 板块:{r.sector_name}{RESET}")
    else:
        print(f"\n{YELLOW}强势板块中无核心股池标的匹配{RESET}")

    print(f"\n  {CYAN}注: 量比* = 代理计算(今日成交额/5日均额按时间折算){RESET}")
    hist_path = data_source.save_scan_history(all_results, scan_time_label)
    history_store.save_strategy_result(
        "intraday", f"{datetime.now().strftime('%Y%m%d')}_{scan_time_label.replace(':', '')}",
        {
            "scan_time": scan_time_label,
            "created_at": datetime.now().isoformat(timespec='seconds'),
            "result_count": len(all_results),
            "results": [vars(r) for r in all_results],
            "parameters": config.snapshot(),
        },
    )
    print(f"  {GREEN}历史存档: {hist_path}{RESET}")
    return all_results
