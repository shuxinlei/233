"""
233战法 - 盘前筛选模块
流程: 涨停基因 → 量价结构 → 辨识度评分 → 核心股池

模块分层:
  compute_pool()      纯计算，只读数据不落盘 —— 回测按日重建股池走这里
  build_stock_pool()  compute_pool + 落盘 + 历史存档 —— 实盘/Web 走这里
"""
from datetime import datetime
from tabulate import tabulate
import config
import data_source
import indicators
import history_store
from models import StockInfo

RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'


def _log(msg='', verbose=True):
    if verbose:
        print(msg)


def _scan_zt_gene(trading_dates, verbose=True):
    """扫描涨停基因: 统计回看期内每只股票的涨停数据"""
    zt_gene = {}
    total = len(trading_dates)
    for i, date in enumerate(trading_dates):
        if (i + 1) % 5 == 0 or i == 0:
            _log(f"  [{i+1}/{total}] 涨停池扫描中...", verbose)
        df = data_source.get_zt_pool(date)
        if df.empty:
            continue
        for _, row in df.iterrows():
            code = str(row.get('代码', '')).zfill(6)
            if not code:
                continue
            name = str(row.get('名称', ''))
            consecutive = int(row.get('连板数', 1) or 1)
            amount = float(row.get('成交额', 0) or 0)
            if code not in zt_gene:
                sector = str(row.get('所属板块', '') or '')
                zt_gene[code] = {
                    'name': name, 'count': 0,
                    'max_consecutive': 0, 'last_date': date,
                    'amounts': [], 'sector': sector
                }
            zt_gene[code]['count'] += 1
            zt_gene[code]['max_consecutive'] = max(zt_gene[code]['max_consecutive'], consecutive)
            zt_gene[code]['last_date'] = date
            zt_gene[code]['amounts'].append(amount)
            if not zt_gene[code].get('sector'):
                zt_gene[code]['sector'] = str(row.get('所属板块', '') or '')
    _log('', verbose)
    return zt_gene


def _filter_zt_gene(zt_gene):
    """涨停基因筛选: 保留涨停次数 >= 阈值的股票"""
    result = []
    for code, d in zt_gene.items():
        if d['count'] >= config.ZT_MIN_COUNT:
            result.append({
                'code': code, 'name': d['name'],
                'zt_count': d['count'],
                'max_consecutive': d['max_consecutive'],
                'last_zt_date': d['last_date'],
                'avg_amount': sum(d['amounts']) / len(d['amounts']) if d['amounts'] else 0,
                'sector': d.get('sector', '')
            })
    result.sort(key=lambda x: x['zt_count'], reverse=True)
    return result


def _filter_price_volume(stocks, end_date=None, verbose=True):
    """量价结构筛选: 红肥绿瘦 + 放量 + 活跃换手"""
    total = len(stocks)
    results = []
    for i, s in enumerate(stocks):
        if (i + 1) % 20 == 0 or i == 0:
            _log(f"  [{i+1}/{total}] K线扫描中...", verbose)
        kline = data_source.get_daily_kline(s['code'], config.KLINE_DAYS, end_date=end_date)
        if kline.empty:
            continue
        rg = indicators.calculate_red_green_ratio(kline)
        vr = indicators.calculate_volume_ratio(kline)
        tr = indicators.get_latest_turnover(kline)
        amt = indicators.get_latest_amount(kline)
        if (rg >= config.RED_GREEN_RATIO and vr >= config.VOLUME_RATIO
                and tr >= config.TURNOVER_MIN):
            s['red_green_ratio'] = rg
            s['volume_ratio'] = vr
            s['turnover_rate'] = tr
            s['amount'] = amt
            s['avg_amount_5d'] = float(kline['成交额'].tail(5).mean())
            results.append(s)
    _log('', verbose)
    return results


def _score_stocks(stocks):
    """辨识度评分: 涨停基因 + 流动性 + 量价结构 加权"""
    if not stocks:
        return []
    max_zt = max(s['zt_count'] for s in stocks) or 1
    max_amt = max(s.get('amount', 0) for s in stocks) or 1
    for s in stocks:
        zt_s = min(s['zt_count'] / max_zt, 1.0) * 100
        liq_s = min(s.get('amount', 0) / max_amt, 1.0) * 100
        pv_s = (min(s.get('red_green_ratio', 0) / 3.0, 1.0) * 50 +
                min(s.get('volume_ratio', 0) / 2.0, 1.0) * 50)
        s['score'] = (config.W_ZT_COUNT * zt_s +
                      config.W_LIQUIDITY * liq_s +
                      config.W_PRICE_VOLUME * pv_s)
    stocks.sort(key=lambda x: x['score'], reverse=True)
    return stocks[:config.POOL_SIZE]


def _fmt_amount(amt):
    if amt >= 1e8:
        return f"{amt/1e8:.1f}亿"
    elif amt >= 1e4:
        return f"{amt/1e4:.1f}万"
    return f"{amt:.0f}"


def _print_report(stocks, zt_count, pv_count):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  233战法 - 盘前筛选报告 {datetime.now().strftime('%Y-%m-%d')}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"\n{CYAN}[涨停基因]{RESET} 近{config.ZT_HISTORY_DAYS}交易日涨停统计 | 次数>={config.ZT_MIN_COUNT}: {zt_count}只")
    print(f"{CYAN}[量价结构]{RESET} 红绿>{config.RED_GREEN_RATIO} 量比>{config.VOLUME_RATIO} 换手>{config.TURNOVER_MIN}% | 通过: {pv_count}只")
    print(f"\n{CYAN}[核心股池] Top{len(stocks)}:{RESET}")
    if not stocks:
        print(f"  {YELLOW}今日无符合条件的标的{RESET}")
        return
    headers = ['排名', '代码', '名称', '板块', '涨停', '连板', '红绿比', '量比', '换手%', '成交额', '评分']
    rows = []
    for i, s in enumerate(stocks):
        rows.append([
            i + 1, s['code'], s['name'][:6], s.get('sector', '')[:10],
            s['zt_count'],
            s.get('max_consecutive', 0),
            f"{s.get('red_green_ratio', 0):.1f}",
            f"{s.get('volume_ratio', 0):.2f}",
            f"{s.get('turnover_rate', 0):.1f}",
            _fmt_amount(s.get('amount', 0)),
            f"{s['score']:.1f}"
        ])
    print(tabulate(rows, headers=headers, tablefmt='simple'))
    print(f"\n{GREEN}核心股池已保存 → stock_pool.json{RESET}\n")


# ==================== 纯计算 ====================

def resolve_source_dates(as_of_date=None):
    """
    解析盘前可用的数据日期。

    盘前只能使用目标日之前已经完成的交易日，禁止把当天行情带入股池，
    否则回测会用到当天收盘结果(未来函数)。
    返回 (target_date, dates)
    """
    target_date = str(as_of_date or datetime.now().strftime('%Y%m%d'))
    target_dates = data_source.get_trading_dates(
        config.ZT_HISTORY_DAYS + 1, end_date=target_date
    )
    dates = [d for d in target_dates if d < target_date][-config.ZT_HISTORY_DAYS:]
    return target_date, dates


def candidate_codes(as_of_date=None):
    """
    只做涨停基因筛选，返回进入量价筛选的候选代码。
    回测用它预先算出全区间需要的股票，一次性批量预取日K。
    """
    _, dates = resolve_source_dates(as_of_date)
    if not dates:
        return []
    zt_stocks = _filter_zt_gene(_scan_zt_gene(dates, verbose=False))
    return [s['code'] for s in zt_stocks[:config.PV_SCAN_MAX]]


def compute_pool(as_of_date=None, verbose=True):
    """
    盘前筛选纯计算: 涨停基因 → 量价结构 → 辨识度评分。
    只读数据，不写 stock_pool.json、不写历史存档。
    返回 (scored_stocks, stats)
    """
    target_date, dates = resolve_source_dates(as_of_date)
    if not dates:
        _log(f"  {RED}获取交易日历失败{RESET}", verbose)
        return [], {'target_date': target_date, 'source_date_end': '',
                    'zt_gene_count': 0, 'pv_pass_count': 0}
    _log(f"  {len(dates)}个交易日: {dates[0]} ~ {dates[-1]}", verbose)

    _log(f"\n{CYAN}Step 2: 涨停基因扫描...{RESET}", verbose)
    zt_stocks = _filter_zt_gene(_scan_zt_gene(dates, verbose))
    _log(f"  涨停基因池: {len(zt_stocks)}只", verbose)
    stats = {'target_date': target_date, 'source_date_end': dates[-1],
             'zt_gene_count': len(zt_stocks), 'pv_pass_count': 0}
    if not zt_stocks:
        _log(f"  {YELLOW}无涨停基因股票{RESET}", verbose)
        return [], stats

    if len(zt_stocks) > config.PV_SCAN_MAX:
        zt_stocks = zt_stocks[:config.PV_SCAN_MAX]
        _log(f"  池过大，取Top{config.PV_SCAN_MAX}进行量价筛选", verbose)

    _log(f"\n{CYAN}Step 3: 量价结构筛选...{RESET}", verbose)
    pv_stocks = _filter_price_volume(zt_stocks, end_date=dates[-1], verbose=verbose)
    _log(f"  通过量价筛选: {len(pv_stocks)}只（换手率 >= {config.TURNOVER_MIN}%）", verbose)
    stats['pv_pass_count'] = len(pv_stocks)

    _log(f"\n{CYAN}Step 4: 辨识度评分...{RESET}", verbose)
    scored = _score_stocks(pv_stocks)
    _log(f"  评分完成，Top{len(scored)}只入选核心股池", verbose)
    return scored, stats


def to_stock_info(s) -> StockInfo:
    """筛选结果 dict → StockInfo"""
    return StockInfo(
        code=s['code'], name=s['name'],
        zt_count=s['zt_count'],
        max_consecutive=s.get('max_consecutive', 0),
        last_zt_date=s.get('last_zt_date', ''),
        turnover_rate=s.get('turnover_rate', 0),
        red_green_ratio=s.get('red_green_ratio', 0),
        volume_ratio=s.get('volume_ratio', 0),
        amount=s.get('amount', 0),
        avg_amount_5d=s.get('avg_amount_5d', 0),
        score=s['score'],
        sector=s.get('sector', '')
    )


# ==================== 实盘入口(计算 + 落盘) ====================

def build_stock_pool(as_of_date=None):
    """盘前筛选主流程: 计算 + 保存股池 + 历史存档"""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  盘前筛选启动 {datetime.now().strftime('%H:%M:%S')}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    print(f"\n{CYAN}Step 1: 获取交易日历...{RESET}")
    scored, stats = compute_pool(as_of_date=as_of_date, verbose=True)
    # 空池也照样落盘: 否则盘中扫描会读到上一交易日的旧股池，比读到空池更危险
    pool = [to_stock_info(s) for s in scored]

    data_source.save_pool(pool)
    hist_path = data_source.save_pool_history(pool)
    history_store.save_strategy_result(
        "pre_market", stats['target_date'],
        {
            "as_of_date": stats['target_date'],
            "source_date_end": stats['source_date_end'],
            "pool_size": len(pool),
            "stocks": [vars(s) for s in pool],
            "parameters": config.snapshot(),
        },
    )
    _print_report(scored, stats['zt_gene_count'], stats['pv_pass_count'])
    print(f"  {GREEN}历史存档: {hist_path}{RESET}")
    return pool
