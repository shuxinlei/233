"""
233战法 - 主程序入口
====================
定时调度: 盘前筛选(08:30) + 盘中4次扫描(09:25/10:30/13:00/14:30)

用法:
  python main.py              # 自动调度模式(全天运行)
  python main.py --mode pre   # 手动运行盘前筛选
  python main.py --mode intraday  # 手动运行盘中扫描
"""
import warnings
warnings.filterwarnings('ignore')

import time
import sys
from datetime import datetime, time as dt_time
import config
import data_source
import error_monitor

RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'


def _parse_time(s):
    h, m = map(int, s.split(':'))
    return dt_time(h, m)


def _check_trigger(now_str, executed):
    """检查当前时间是否匹配调度点"""
    if now_str == config.PRE_MARKET_TIME and 'pre' not in executed:
        return 'pre'
    for st in config.SCAN_TIMES:
        if now_str == st and st not in executed:
            return ('scan', st)
    return None


def run_auto():
    """自动调度模式"""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  233战法自动化扫描系统{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"  盘前筛选: {config.PRE_MARKET_TIME}")
    print(f"  盘中扫描: {', '.join(config.SCAN_TIMES)}")
    print(f"  数据源:   AkShare")

    print(f"\n{CYAN}检查交易日...{RESET}")
    if not data_source.is_trading_day():
        print(f"  {YELLOW}今日非交易日，程序退出{RESET}")
        return

    print(f"  {GREEN}今日为交易日，启动调度{RESET}")

    executed = set()
    now = datetime.now()

    # 跳过已过的时间点
    if now.time() > _parse_time(config.PRE_MARKET_TIME):
        executed.add('pre')
        print(f"  {YELLOW}已过盘前时间({config.PRE_MARKET_TIME})，跳过盘前筛选{RESET}")
        pool, _dropped = data_source.apply_pool_constraints(data_source.load_pool())
        if pool:
            print(f"  {GREEN}检测到已有股池({len(pool)}只)，继续盘中扫描{RESET}")
    for st in config.SCAN_TIMES:
        if now.time() > _parse_time(st):
            executed.add(st)
            print(f"  {YELLOW}已过 {st} 扫描时间，跳过{RESET}")

    print(f"\n{CYAN}调度启动，等待触发... (Ctrl+C 退出){RESET}\n")

    try:
        while True:
            now = datetime.now()
            now_str = now.strftime('%H:%M')
            task = _check_trigger(now_str, executed)

            if task == 'pre':
                executed.add('pre')
                from pre_market import build_stock_pool
                build_stock_pool()
            elif task and task[0] == 'scan':
                st = task[1]
                executed.add(st)
                from intraday import scan
                scan(st)

            if now.time() > dt_time(15, 30):
                print(f"\n{BOLD}{'='*60}{RESET}")
                print(f"{BOLD}  今日扫描结束 {now.strftime('%H:%M:%S')}{RESET}")
                print(f"{BOLD}{'='*60}{RESET}\n")
                break

            time.sleep(30)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}程序已退出{RESET}\n")
    except Exception as e:
        error_monitor.log_exception("scheduler", e)
        print(f"\n{RED}调度异常: {e}{RESET}\n")


def run_manual(mode):
    """手动运行一次"""
    try:
        if mode == 'pre':
            from pre_market import build_stock_pool
            build_stock_pool()
        else:
            from intraday import scan
            scan(datetime.now().strftime('%H:%M'))
    except Exception as e:
        error_monitor.log_exception(f"manual_{mode}", e)
        raise


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--mode':
        mode = sys.argv[2] if len(sys.argv) > 2 else 'auto'
    else:
        mode = 'auto'

    if mode == 'auto':
        run_auto()
    elif mode == 'pre':
        run_manual('pre')
    elif mode == 'intraday':
        run_manual('intraday')
    else:
        print(f"{RED}未知模式: {mode}{RESET}")
        print(f"用法: python main.py [--mode auto|pre|intraday]")
        sys.exit(1)
