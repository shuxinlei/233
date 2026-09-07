"""
233战法 - 盘前股池回测模块
================================
按日重建盘前核心股池(只用当日之前已收盘的数据)，模拟买卖，评估规则质量。

为什么只回测盘前:
  盘中扫描依赖板块的"盘中实时涨幅"和"当时的板块成分股名单"，这两项历史数据
  在公开接口里取不到(板块接口只给日线，成分股只给当前名单)。要回放盘中，只能
  等实盘运行把 history/market_data.db 的快照攒起来。盘前这一层则完全可回测，
  且所有可调参数(涨停基因/红绿比/量比/换手/评分权重/池大小)都在这一层。

交易假设:
  入场   一律当日开盘买入(盘前选股，开盘执行)
  出场   由 exit_mode 决定
           same_close  当日收盘卖   (T+0，A股做不到，仅作信号质量参考)
           next_open   次日开盘卖   (默认，T+1 最短合法持有，日期不重叠)
           next_close  次日收盘卖
           nday_close  第N个交易日收盘卖
  买不进 开盘涨幅 >= BT_MAX_OPEN_GAP 视为一字板，跳过该笔
  成本   BT_COST_PCT 双边一次性扣除(佣金+印花税+过户费+滑点)

用法:
  python backtest.py --start 20260601 --end 20260904
  python backtest.py --start 20260601 --end 20260904 --exit next_close --top-n 10
  python backtest.py --start 20260601 --end 20260904 --sweep RED_GREEN_RATIO=1.2,1.5,2.0
"""
import warnings
warnings.filterwarnings('ignore')

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
from tabulate import tabulate

import config
import data_source
import history_store
import pre_market

RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_DIR = os.path.join(BASE_DIR, "history", "backtest")

EXIT_MODES = ('same_close', 'next_open', 'next_close', 'nday_close')
TRADING_DAYS_PER_YEAR = 242
MIN_DAYS_FOR_ANNUAL = 20      # 少于该天数不展示年化(样本太短，年化无意义)

SKIP_REASONS = {
    'no_bar': '当日无K线(停牌/未上市)',
    'gap_limit': '开盘接近涨停，买不进',
    'no_exit_bar': '区间末尾缺卖出K线',
    'bad_price': '价格数据异常',
}


def _f(v) -> float:
    """安全转 float: None/NaN/非数 一律当 0。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


# ==================== 单笔交易模拟 ====================

def _simulate(code, entry_date, exit_mode, hold_days, cost_pct, max_open_gap):
    """
    在预取的整段日K上模拟一笔交易。
    返回 (trade_dict, None) 或 (None, skip_reason)
    """
    df = data_source.get_bulk_kline(code)
    if df.empty or '日期' not in df.columns:
        return None, 'no_bar'

    idx = df.index[df['日期'] == pd.Timestamp(entry_date)]
    if len(idx) == 0:
        return None, 'no_bar'
    i = int(idx[0])

    entry = _f(df.at[i, '开盘'])
    if entry <= 0:
        return None, 'bad_price'

    # 开盘涨幅要用前一交易日收盘价算；区间首根K线拿不到前收，则不做涨停过滤
    gap = None
    if i >= 1:
        prev_close = _f(df.at[i - 1, '收盘'])
        if prev_close > 0:
            gap = (entry / prev_close - 1) * 100
            if gap >= max_open_gap:
                return None, 'gap_limit'

    if exit_mode == 'same_close':
        j, col = i, '收盘'
    elif exit_mode == 'next_open':
        j, col = i + 1, '开盘'
    elif exit_mode == 'next_close':
        j, col = i + 1, '收盘'
    else:  # nday_close
        j, col = i + max(1, hold_days), '收盘'

    if j >= len(df):
        return None, 'no_exit_bar'
    exit_px = _f(df.at[j, col])
    if exit_px <= 0:
        return None, 'bad_price'

    gross = (exit_px / entry - 1) * 100
    return {
        'exit_date': df.at[j, '日期'].strftime('%Y%m%d'),
        'entry_price': round(entry, 3),
        'exit_price': round(exit_px, 3),
        'open_gap_pct': round(gap, 2) if gap is not None else None,
        'gross_pct': round(gross, 3),
        'net_pct': round(gross - cost_pct, 3),
    }, None


# ==================== 指标计算 ====================

def _equity_curve(daily_returns):
    nav, curve = 1.0, []
    for r in daily_returns:
        nav *= (1 + r / 100.0)
        curve.append(nav)
    return curve


def _max_drawdown_pct(curve):
    peak, mdd = float('-inf'), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd * 100


def _metrics(trades):
    """按入场日等权聚合成组合日收益，再算组合级指标。"""
    rets = [t['net_pct'] for t in trades]
    if not rets:
        return {}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]

    by_date = defaultdict(list)
    for t in trades:
        by_date[t['entry_date']].append(t['net_pct'])
    dates = sorted(by_date)
    daily = [statistics.mean(by_date[d]) for d in dates]
    curve = _equity_curve(daily)
    nav = curve[-1]
    # 回撤基准要含初始资金 1.0，否则首日即亏时首点自成峰值、回撤被算成 0
    mdd = _max_drawdown_pct([1.0] + curve)

    ann = (nav ** (TRADING_DAYS_PER_YEAR / len(daily)) - 1) * 100 if nav > 0 else -100.0
    sharpe = None
    if len(daily) > 1:
        sd = statistics.stdev(daily)
        if sd > 0:
            sharpe = statistics.mean(daily) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)

    return {
        'trade_count': len(rets),
        'trading_days': len(daily),
        'avg_trades_per_day': round(len(rets) / len(daily), 1),
        'win_rate_pct': round(len(wins) / len(rets) * 100, 2),
        'avg_return_pct': round(statistics.mean(rets), 3),
        'median_return_pct': round(statistics.median(rets), 3),
        'avg_win_pct': round(statistics.mean(wins), 3) if wins else 0.0,
        'avg_loss_pct': round(statistics.mean(losses), 3) if losses else 0.0,
        'profit_factor': (round(statistics.mean(wins) / abs(statistics.mean(losses)), 2)
                          if wins and losses else None),
        'best_pct': round(max(rets), 2),
        'worst_pct': round(min(rets), 2),
        'total_return_pct': round((nav - 1) * 100, 2),
        'annualized_pct': round(ann, 2),
        'max_drawdown_pct': round(mdd, 2),
        'sharpe': round(sharpe, 2) if sharpe is not None else None,
        'daily_win_rate_pct': round(sum(1 for r in daily if r > 0) / len(daily) * 100, 2),
        'equity_curve': [
            {'date': d, 'ret_pct': round(r, 3), 'nav': round(v, 4)}
            for d, r, v in zip(dates, daily, curve)
        ],
    }


def _rank_buckets(trades, buckets):
    """
    按标的在当日股池中的排名分位分组。
    评分若真有区分度，靠前的组胜率/平均收益应明显更高 —— 这是检验评分权重的核心视图。
    """
    groups = defaultdict(list)
    for t in trades:
        pct = (t['rank'] - 1) / max(t['pool_size'], 1)
        b = min(int(pct * buckets), buckets - 1)
        groups[b].append(t['net_pct'])
    out = []
    for b in range(buckets):
        rs = groups.get(b, [])
        out.append({
            'bucket': f"前{int(b / buckets * 100)}-{int((b + 1) / buckets * 100)}%",
            'count': len(rs),
            'win_rate_pct': round(sum(1 for r in rs if r > 0) / len(rs) * 100, 2) if rs else None,
            'avg_return_pct': round(statistics.mean(rs), 3) if rs else None,
        })
    return out


def _zt_coverage(bt_dates):
    """
    统计每个回测日的涨停池数据覆盖度。

    stock_zt_pool_em 只提供最近约3周的涨停池，更早的日期一律返回空。
    涨停基因是第一层筛选，数据缺失会直接让股池为空 —— 若不显式暴露，
    "没数据"会被当成"策略没信号"，把一段极短的样本误读成长周期回测结果。
    覆盖度依赖 get_zt_pool 的进程内缓存，汇总候选标的之后调用几乎不额外发请求。
    """
    per_date = {}
    for d in bt_dates:
        _, src_dates = pre_market.resolve_source_dates(d)
        have = sum(1 for x in src_dates if not data_source.get_zt_pool(x).empty)
        per_date[d] = {'required': len(src_dates), 'available': have}
    total = len(bt_dates)
    zero = [d for d, c in per_date.items() if c['available'] == 0]
    full = [d for d, c in per_date.items()
            if c['required'] > 0 and c['available'] >= c['required']]
    covered = [d for d, c in per_date.items() if c['available'] > 0]
    return {
        'total_days': total,
        'no_data_days': len(zero),
        'partial_days': total - len(zero) - len(full),
        'full_days': len(full),
        'first_covered': covered[0] if covered else None,
        'last_covered': covered[-1] if covered else None,
        'required_per_day': config.ZT_HISTORY_DAYS,
        'per_date': per_date,
    }


def _benchmark(dates, symbol):
    """基准指数同期表现，用收盘价算。"""
    if not dates:
        return None
    df = data_source.get_index_daily(symbol)
    if df.empty or 'close' not in df.columns:
        return None
    lo, hi = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
    sub = df[(df['date'] >= lo) & (df['date'] <= hi)]
    if len(sub) < 2:
        return None
    closes = sub['close'].astype(float).tolist()
    # 曲线首点即 1.0，本身就是起始峰值，不必额外补初始资金点
    curve = [c / closes[0] for c in closes]
    return {
        'symbol': symbol,
        'bars': len(closes),
        'total_return_pct': round((closes[-1] / closes[0] - 1) * 100, 2),
        'max_drawdown_pct': round(_max_drawdown_pct(curve), 2),
    }


# ==================== 主流程 ====================

def run_backtest(start, end, exit_mode=None, hold_days=None, cost_pct=None,
                 max_open_gap=None, top_n=None, benchmark=None,
                 buckets=None, verbose=True, save=True):
    """
    回测盘前股池。返回结果 dict(含 summary / metrics / trades / rank_buckets)。
    """
    exit_mode = exit_mode or config.BT_EXIT_MODE
    if exit_mode not in EXIT_MODES:
        raise ValueError(f"exit_mode 必须是 {EXIT_MODES} 之一，收到 {exit_mode!r}")
    hold_days = config.BT_HOLD_DAYS if hold_days is None else hold_days
    cost_pct = config.BT_COST_PCT if cost_pct is None else cost_pct
    max_open_gap = config.BT_MAX_OPEN_GAP if max_open_gap is None else max_open_gap
    top_n = config.BT_TOP_N if top_n is None else top_n
    benchmark = benchmark or config.BT_BENCHMARK
    buckets = config.BT_SCORE_BUCKETS if buckets is None else buckets

    def log(msg=''):
        if verbose:
            print(msg)

    log(f"\n{BOLD}{'='*64}{RESET}")
    log(f"{BOLD}  233战法 - 盘前股池回测  {start} ~ {end}{RESET}")
    log(f"{BOLD}{'='*64}{RESET}")
    log(f"  出场方式: {exit_mode}" + (f" (N={hold_days})" if exit_mode == 'nday_close' else ""))
    log(f"  交易成本: {cost_pct}% 双边 | 涨停跳过阈值: {max_open_gap}%")
    log(f"  每日取样: {'评分前' + str(top_n) + '只' if top_n else f'整个核心股池(POOL_SIZE={config.POOL_SIZE})'}")
    if exit_mode == 'same_close':
        log(f"  {YELLOW}注意: same_close 是 T+0，A股无法实盘，结果仅作信号质量参考{RESET}")

    bt_dates = data_source.get_trading_dates_between(start, end)
    if not bt_dates:
        log(f"  {RED}区间内无交易日{RESET}")
        return None
    log(f"\n{CYAN}Step 1: 区间内 {len(bt_dates)} 个交易日 ({bt_dates[0]} ~ {bt_dates[-1]}){RESET}")

    # Pass 1: 先把全区间会用到的股票算出来，一次性批量预取日K。
    # 否则每个交易日都要为同一批股票重新请求一遍K线，请求量是这里的几十倍。
    log(f"\n{CYAN}Step 2: 汇总候选标的...{RESET}")
    codes = set()
    for i, d in enumerate(bt_dates):
        if verbose and (i == 0 or (i + 1) % 10 == 0):
            log(f"  [{i+1}/{len(bt_dates)}] 涨停基因池汇总中...")
        codes.update(pre_market.candidate_codes(d))
    if not codes:
        log(f"  {RED}区间内无涨停基因标的，无法回测{RESET}")
        return None
    log(f"  候选标的: {len(codes)}只")

    coverage = _zt_coverage(bt_dates)
    log(f"  涨停池覆盖: 完整{coverage['full_days']}日 / 部分{coverage['partial_days']}日 "
        f"/ 无数据{coverage['no_data_days']}日")
    if coverage['no_data_days']:
        log(f"  {YELLOW}提示: 涨停池接口只回溯最近约3周，{coverage['no_data_days']}个回测日"
            f"因缺涨停池数据必然出空池，不计入有效样本{RESET}")

    pre_dates = data_source.get_trading_dates(config.KLINE_DAYS + 10, end_date=bt_dates[0])
    fetch_start = pre_dates[0] if pre_dates else bt_dates[0]
    # 卖出可能落在区间之后；上限压到今天，避免把"尚不存在的未来K线"写进快照缓存
    tail_buffer = (max(1, hold_days) + 5) * 3
    fetch_end_dt = min(
        datetime.strptime(bt_dates[-1], '%Y%m%d') + timedelta(days=tail_buffer),
        datetime.now(),
    )
    fetch_end = fetch_end_dt.strftime('%Y%m%d')
    log(f"\n{CYAN}Step 3: 预取日K {fetch_start} ~ {fetch_end}...{RESET}")
    ok = data_source.prefetch_klines(sorted(codes), fetch_start, fetch_end,
                                     progress_every=50 if verbose else 0)
    log(f"  取到日K: {ok}/{len(codes)}只")

    # Pass 2: 按日重建股池并模拟
    log(f"\n{CYAN}Step 4: 按日重建股池并模拟交易...{RESET}")
    trades = []
    skipped = defaultdict(int)
    daily_pool = {}
    for i, d in enumerate(bt_dates):
        if verbose and (i == 0 or (i + 1) % 10 == 0):
            log(f"  [{i+1}/{len(bt_dates)}] {d} 回放中...")
        scored, _ = pre_market.compute_pool(as_of_date=d, verbose=False)
        if top_n:
            scored = scored[:top_n]
        daily_pool[d] = len(scored)
        for rank, s in enumerate(scored, 1):
            res, reason = _simulate(s['code'], d, exit_mode, hold_days,
                                    cost_pct, max_open_gap)
            if reason:
                skipped[reason] += 1
                continue
            trades.append({
                'entry_date': d,
                'code': s['code'],
                'name': s['name'],
                'sector': s.get('sector', ''),
                'rank': rank,
                'pool_size': len(scored),
                'score': round(s['score'], 2),
                'zt_count': s['zt_count'],
                **res,
            })

    empty_days = [d for d, n in daily_pool.items() if n == 0]
    log(f"  股池非空日: {len(bt_dates) - len(empty_days)}/{len(bt_dates)} | 成交笔数: {len(trades)}")

    result = {
        'summary': {
            'start': bt_dates[0],
            'end': bt_dates[-1],
            'exit_mode': exit_mode,
            'hold_days': hold_days,
            'cost_pct': cost_pct,
            'max_open_gap': max_open_gap,
            'top_n': top_n,
            'candidate_count': len(codes),
            'kline_ok_count': ok,
            'total_trading_days': len(bt_dates),
            'empty_pool_days': len(empty_days),
            'avg_pool_size': round(statistics.mean(daily_pool.values()), 1) if daily_pool else 0,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        },
        'coverage': coverage,
        'metrics': _metrics(trades),
        'rank_buckets': _rank_buckets(trades, buckets) if trades else [],
        'benchmark': _benchmark(bt_dates, benchmark),
        'skipped': {SKIP_REASONS.get(k, k): v for k, v in sorted(skipped.items())},
        'daily_pool_size': daily_pool,
        'parameters': config.snapshot(),
        'trades': trades,
    }

    if verbose:
        print_report(result)
    if save:
        path = save_result(result)
        result['summary']['saved_to'] = path
        log(f"\n  {GREEN}回测存档: {path}{RESET}")
    return result


# ==================== 报告输出 ====================

def print_report(result):
    m = result.get('metrics') or {}
    s = result['summary']

    print(f"\n{BOLD}{'='*64}{RESET}")
    print(f"{BOLD}  回测报告  {s['start']} ~ {s['end']}  ({s['exit_mode']}){RESET}")
    print(f"{BOLD}{'='*64}{RESET}")

    cov = result.get('coverage')
    if cov:
        print(f"\n{CYAN}[数据覆盖]{RESET} 涨停基因需每个回测日回看{cov['required_per_day']}个交易日")
        print(tabulate([
            ['回测区间交易日', cov['total_days']],
            ['涨停池数据完整', cov['full_days']],
            ['涨停池数据不全', cov['partial_days']],
            ['涨停池完全无数据', cov['no_data_days']],
            ['实际有数据区间', f"{cov['first_covered']} ~ {cov['last_covered']}"
                              if cov['first_covered'] else '无'],
        ], headers=['项', '值'], tablefmt='simple'))
        if cov['no_data_days'] or cov['partial_days']:
            print(f"  {YELLOW}stock_zt_pool_em 只回溯最近约3周。数据不全的日期股池会偏小或为空，"
                  f"下面的指标只反映有数据的那几天，别当成整段区间的表现{RESET}")

    if not m:
        print(f"\n  {YELLOW}区间内没有产生任何交易{RESET}")
        if result.get('skipped'):
            print(f"\n{CYAN}[跳过原因]{RESET}")
            print(tabulate([[k, v] for k, v in result['skipped'].items()],
                           headers=['原因', '笔数'], tablefmt='simple'))
        return

    print(f"\n{CYAN}[交易统计]{RESET}")
    print(tabulate([
        ['交易笔数', m['trade_count']],
        ['有信号天数', f"{m['trading_days']} / {s['total_trading_days']}"],
        ['日均笔数', m['avg_trades_per_day']],
        ['日均股池', s['avg_pool_size']],
        ['胜率', f"{m['win_rate_pct']}%"],
        ['平均收益', f"{m['avg_return_pct']}%"],
        ['收益中位数', f"{m['median_return_pct']}%"],
        ['平均盈利 / 平均亏损', f"{m['avg_win_pct']}% / {m['avg_loss_pct']}%"],
        ['盈亏比', m['profit_factor'] if m['profit_factor'] is not None else '-'],
        ['最好 / 最差单笔', f"{m['best_pct']}% / {m['worst_pct']}%"],
    ], headers=['指标', '值'], tablefmt='simple'))

    print(f"\n{CYAN}[组合表现]{RESET} (每日等权买入当日股池)")
    # 不足一个月的样本年化出来纯是噪声，不给数字免得误读
    ann = f"{m['annualized_pct']}%" if m['trading_days'] >= MIN_DAYS_FOR_ANNUAL else f"- (样本不足{MIN_DAYS_FOR_ANNUAL}日)"
    rows = [
        ['累计收益', f"{m['total_return_pct']}%"],
        ['年化收益', ann],
        ['最大回撤', f"{m['max_drawdown_pct']}%"],
        ['夏普比率', m['sharpe'] if m['sharpe'] is not None else '-'],
        ['日胜率', f"{m['daily_win_rate_pct']}%"],
    ]
    bm = result.get('benchmark')
    if bm:
        rows.append([f"基准 {bm['symbol']} 同期", f"{bm['total_return_pct']}% (回撤 {bm['max_drawdown_pct']}%)"])
        rows.append(['超额收益', f"{round(m['total_return_pct'] - bm['total_return_pct'], 2)}%"])
    print(tabulate(rows, headers=['指标', '值'], tablefmt='simple'))

    if result.get('rank_buckets'):
        print(f"\n{CYAN}[评分区分度]{RESET} 按标的在当日股池中的排名分组")
        print(tabulate(
            [[b['bucket'], b['count'],
              f"{b['win_rate_pct']}%" if b['win_rate_pct'] is not None else '-',
              f"{b['avg_return_pct']}%" if b['avg_return_pct'] is not None else '-']
             for b in result['rank_buckets']],
            headers=['排名分位', '笔数', '胜率', '平均收益'], tablefmt='simple'))
        print(f"  {YELLOW}靠前分位收益应明显更高；若各组差不多，说明评分权重没起作用{RESET}")

    if result.get('skipped'):
        print(f"\n{CYAN}[跳过的信号]{RESET}")
        print(tabulate([[k, v] for k, v in result['skipped'].items()],
                       headers=['原因', '笔数'], tablefmt='simple'))

    curve = m.get('equity_curve') or []
    if curve:
        print(f"\n{CYAN}[净值曲线]{RESET} 首尾各5日")
        head = curve[:5]
        tail = curve[-5:] if len(curve) > 10 else []
        crows = [[c['date'], f"{c['ret_pct']}%", round(c['nav'], 4)] for c in head]
        if tail:
            crows.append(['...', '...', '...'])
            crows += [[c['date'], f"{c['ret_pct']}%", round(c['nav'], 4)] for c in tail]
        print(tabulate(crows, headers=['日期', '当日收益', '净值'], tablefmt='simple'))

    if s['exit_mode'] in ('next_close', 'nday_close'):
        print(f"\n  {YELLOW}注: {s['exit_mode']} 持仓与次日新信号重叠，"
              f"净值曲线按每日满仓独立资金计算，衡量信号质量而非可直接实盘的资金曲线{RESET}")


# ==================== 结果存档 ====================

def save_result(result) -> str:
    os.makedirs(RESULT_DIR, exist_ok=True)
    s = result['summary']
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    fn = f"bt_{s['start']}_{s['end']}_{s['exit_mode']}_{stamp}.json"
    path = os.path.join(RESULT_DIR, fn)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    history_store.save_strategy_result(
        "backtest", fn.replace('.json', ''),
        {k: v for k, v in result.items() if k != 'trades'} | {'trade_count': len(result['trades'])},
    )
    return path


def list_results() -> list:
    """列出所有回测存档，按时间倒序。"""
    if not os.path.isdir(RESULT_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(RESULT_DIR), reverse=True):
        if not fn.endswith('.json'):
            continue
        item = {'filename': fn}
        try:
            with open(os.path.join(RESULT_DIR, fn), 'r', encoding='utf-8') as f:
                data = json.load(f)
            item.update({
                'summary': data.get('summary', {}),
                'metrics': {k: v for k, v in (data.get('metrics') or {}).items()
                            if k != 'equity_curve'},
            })
        except Exception:
            pass
        out.append(item)
    return out


def load_result(filename: str) -> dict:
    """读取指定回测存档(限定 history/backtest 目录，防路径遍历)。"""
    safe = os.path.basename(filename)
    if safe != filename or not safe.endswith('.json'):
        return {}
    path = os.path.join(RESULT_DIR, safe)
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ==================== 参数扫描 ====================

def _coerce(param, raw):
    cur = getattr(config, param)
    if isinstance(cur, bool):
        return str(raw).lower() in ('1', 'true', 'yes', 'y')
    if isinstance(cur, int):
        return int(float(raw))
    if isinstance(cur, float):
        return float(raw)
    return raw


def run_sweep(param, values, start, end, verbose=True, **kw):
    """
    在同一段样本上跑多组参数取值，横向对比 —— 用来验证每次规则调整。
    日K与参数无关，预取缓存跨轮复用，所以后续几轮基本不再发请求。
    """
    if not hasattr(config, param) or not param.isupper():
        raise ValueError(f"未知参数: {param}")
    original = getattr(config, param)
    rows, results = [], []
    try:
        for raw in values:
            val = _coerce(param, raw)
            setattr(config, param, val)
            print(f"\n{BOLD}{'-'*64}{RESET}")
            print(f"{BOLD}  {param} = {val}{RESET}")
            print(f"{BOLD}{'-'*64}{RESET}")
            res = run_backtest(start, end, verbose=False, save=False, **kw)
            if not res or not res.get('metrics'):
                print(f"  {YELLOW}该取值下无交易{RESET}")
                rows.append([val, 0, '-', '-', '-', '-', '-'])
                continue
            m = res['metrics']
            results.append((val, res))
            rows.append([
                val, m['trade_count'], f"{m['win_rate_pct']}%",
                f"{m['avg_return_pct']}%", f"{m['total_return_pct']}%",
                f"{m['max_drawdown_pct']}%",
                m['sharpe'] if m['sharpe'] is not None else '-',
            ])
            print(f"  笔数{m['trade_count']} 胜率{m['win_rate_pct']}% "
                  f"平均{m['avg_return_pct']}% 累计{m['total_return_pct']}% "
                  f"回撤{m['max_drawdown_pct']}%")
    finally:
        setattr(config, param, original)

    print(f"\n{BOLD}{'='*64}{RESET}")
    print(f"{BOLD}  参数扫描对比: {param}  ({start} ~ {end}){RESET}")
    print(f"{BOLD}{'='*64}{RESET}")
    print(tabulate(rows, headers=[param, '笔数', '胜率', '平均收益', '累计收益', '最大回撤', '夏普'],
                   tablefmt='simple'))
    print(f"\n  {YELLOW}参数已恢复为 {param}={original}{RESET}")
    return results


# ==================== CLI ====================

def main():
    ap = argparse.ArgumentParser(
        description='233战法盘前股池回测',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--start', required=True, help='回测起始日 YYYYMMDD')
    ap.add_argument('--end', required=True, help='回测结束日 YYYYMMDD')
    ap.add_argument('--exit', dest='exit_mode', choices=EXIT_MODES,
                    default=None, help=f'出场方式，默认 {config.BT_EXIT_MODE}')
    ap.add_argument('--hold', dest='hold_days', type=int, default=None,
                    help='nday_close 模式的持有交易日数')
    ap.add_argument('--cost', dest='cost_pct', type=float, default=None,
                    help='双边交易成本(%%)')
    ap.add_argument('--max-gap', dest='max_open_gap', type=float, default=None,
                    help='开盘涨幅超过该值视为买不进(%%)')
    ap.add_argument('--top-n', dest='top_n', type=int, default=None,
                    help='每日只取评分前N只，0=整个股池')
    ap.add_argument('--benchmark', default=None, help='基准指数，如 sh000300')
    ap.add_argument('--no-save', action='store_true', help='不写回测存档')
    ap.add_argument('--sweep', default=None,
                    help='参数扫描，格式 PARAM=v1,v2,v3 例如 RED_GREEN_RATIO=1.2,1.5,2.0')
    args = ap.parse_args()

    kw = dict(exit_mode=args.exit_mode, hold_days=args.hold_days,
              cost_pct=args.cost_pct, max_open_gap=args.max_open_gap,
              top_n=args.top_n, benchmark=args.benchmark)

    if args.sweep:
        if '=' not in args.sweep:
            print(f"{RED}--sweep 格式应为 PARAM=v1,v2,v3{RESET}")
            sys.exit(1)
        param, vals = args.sweep.split('=', 1)
        values = [v.strip() for v in vals.split(',') if v.strip()]
        if not values:
            print(f"{RED}--sweep 未提供取值{RESET}")
            sys.exit(1)
        run_sweep(param.strip(), values, args.start, args.end, **kw)
    else:
        run_backtest(args.start, args.end, save=not args.no_save, **kw)


if __name__ == '__main__':
    main()
