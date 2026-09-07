"""
233战法 - Web仪表盘
Flask本地服务，浏览器访问查看筛选结果和触发扫描
"""
import warnings
warnings.filterwarnings('ignore')

import os
import threading
from contextlib import redirect_stdout
from datetime import datetime
from flask import Flask, request, jsonify, render_template

import config
import data_source

app = Flask(__name__, template_folder='templates')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_LOCK = threading.Lock()

# 全局状态
TASK_LABELS = {
    'pre_market': '盘前筛选',
    'intraday': '盘中扫描',
    'backtest': '回测',
}
task_status = {
    name: {'running': False, 'result': None, 'error': None, 'log': []}
    for name in TASK_LABELS
}


def _run_task(name, fn):
    """
    在后台线程里跑一个任务: 捕获 stdout 进日志、串行化、异常带 traceback 落日志。
    fn 返回值作为该任务的 result。
    """
    st = task_status[name]
    st['running'] = True
    st['result'] = None
    st['error'] = None
    st['log'] = []
    log = st['log']

    class LogCapture:
        def write(self, msg):
            if msg.strip():
                log.append(msg.strip())

        def flush(self):
            pass

    try:
        with TASK_LOCK, redirect_stdout(LogCapture()):
            st['result'] = fn()
    except Exception as e:
        import traceback
        log.append(f'[错误] {e}')
        log.append(traceback.format_exc().strip())
        st['error'] = str(e)
    finally:
        st['running'] = False


def _start_task(name, fn):
    """已在运行则拒绝，否则起后台线程。"""
    if task_status[name]['running']:
        return jsonify({'ok': False, 'msg': f'{TASK_LABELS[name]}正在运行中'}), 409
    threading.Thread(target=_run_task, args=(name, fn), daemon=True).start()
    return jsonify({'ok': True, 'msg': f'{TASK_LABELS[name]}已启动'})


def _pre_market_task():
    from pre_market import build_stock_pool
    pool = build_stock_pool()
    return {
        'count': len(pool),
        'stocks': [vars(s) for s in pool],
        'time': datetime.now().strftime('%H:%M:%S'),
    }


def _intraday_task():
    from intraday import scan
    results = scan(datetime.now().strftime('%H:%M'))
    return {
        'count': len(results),
        'results': [vars(r) for r in results],
        'time': datetime.now().strftime('%H:%M:%S'),
    }


def _backtest_task(params):
    """回测结果里 trades 可能上千行，状态接口只回摘要，明细去存档接口取。"""
    import backtest
    res = backtest.run_backtest(**params)
    if not res:
        return {'ok': False, 'msg': '区间内无交易日或无可回测标的',
                'time': datetime.now().strftime('%H:%M:%S')}
    return {
        'ok': True,
        'summary': res['summary'],
        'metrics': {k: v for k, v in (res.get('metrics') or {}).items()
                    if k != 'equity_curve'},
        'equity_curve': (res.get('metrics') or {}).get('equity_curve', []),
        'rank_buckets': res.get('rank_buckets', []),
        'benchmark': res.get('benchmark'),
        'skipped': res.get('skipped', {}),
        'trade_count': len(res.get('trades', [])),
        'time': datetime.now().strftime('%H:%M:%S'),
    }


# ==================== 路由 ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/pool')
def get_pool():
    """获取核心股池"""
    pool = data_source.load_pool()
    return jsonify({
        'count': len(pool),
        'stocks': [vars(s) for s in pool],
        'time': datetime.now().strftime('%H:%M:%S'),
    })


@app.route('/api/status')
def get_status():
    """获取所有任务状态"""
    payload = {
        name: {
            'running': st['running'],
            'result': st['result'],
            'error': st['error'],
            'log_tail': st['log'][-50:],
        }
        for name, st in task_status.items()
    }
    payload['now'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return jsonify(payload)


@app.route('/api/run/pre', methods=['POST'])
def run_pre():
    """触发盘前筛选"""
    return _start_task('pre_market', _pre_market_task)


@app.route('/api/run/intraday', methods=['POST'])
def run_intraday():
    """触发盘中扫描"""
    return _start_task('intraday', _intraday_task)


@app.route('/api/run/backtest', methods=['POST'])
def run_backtest_route():
    """触发盘前股池回测"""
    import backtest
    body = request.json or {}
    start, end = body.get('start'), body.get('end')
    if not start or not end:
        return jsonify({'ok': False, 'msg': '需要 start 和 end (YYYYMMDD)'}), 400

    params = {'start': str(start), 'end': str(end), 'verbose': True, 'save': True}
    if body.get('exit_mode'):
        if body['exit_mode'] not in backtest.EXIT_MODES:
            return jsonify({'ok': False,
                            'msg': f"exit_mode 需为 {list(backtest.EXIT_MODES)} 之一"}), 400
        params['exit_mode'] = body['exit_mode']
    for key, cast in (('hold_days', int), ('top_n', int),
                      ('cost_pct', float), ('max_open_gap', float)):
        if body.get(key) not in (None, ''):
            try:
                params[key] = cast(body[key])
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'msg': f'{key} 取值非法'}), 400
    if body.get('benchmark'):
        params['benchmark'] = str(body['benchmark'])

    return _start_task('backtest', lambda: _backtest_task(params))


@app.route('/api/config', methods=['GET', 'POST'])
def manage_config():
    """获取或修改配置"""
    if request.method == 'GET':
        attrs = {}
        for attr in dir(config):
            if attr.isupper() and not attr.startswith('_'):
                val = getattr(config, attr)
                if isinstance(val, (int, float, str, bool, list)):
                    attrs[attr] = val
        return jsonify(attrs)

    data = request.json
    updated = []
    for key, val in data.items():
        if hasattr(config, key) and key.isupper():
            old_val = getattr(config, key)
            if isinstance(old_val, bool):
                val = str(val).lower() in ('true', '1', 'yes')
            elif isinstance(old_val, int) and not isinstance(old_val, bool):
                val = int(val)
            elif isinstance(old_val, float):
                val = float(val)
            elif isinstance(old_val, list):
                val = val if isinstance(val, list) else [val]
            setattr(config, key, val)
            updated.append(f'{key}: {val}')

    # 持久化到 config_local.py
    with open(os.path.join(BASE_DIR, 'config_local.py'), 'w', encoding='utf-8') as f:
        f.write('# 自动生成 - 勿手动编辑\n')
        for attr in dir(config):
            if attr.isupper() and not attr.startswith('_'):
                val = getattr(config, attr)
                if isinstance(val, (int, float, str, bool)):
                    f.write(f'{attr} = {repr(val)}\n')
                elif isinstance(val, list):
                    f.write(f'{attr} = {val}\n')
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/trading_day')
def check_trading_day():
    """检查是否为交易日"""
    return jsonify({
        'is_trading_day': data_source.is_trading_day(),
        'now': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scan_times': config.SCAN_TIMES,
        'pre_market_time': config.PRE_MARKET_TIME,
    })


@app.route('/api/history')
def get_history_list():
    """列出所有历史记录"""
    return jsonify(data_source.list_history())


@app.route('/api/history/<filename>')
def get_history_detail(filename):
    """加载指定历史记录详情"""
    data = data_source.load_history(filename)
    return jsonify(data)


@app.route('/api/backtest')
def get_backtest_list():
    """列出所有回测存档"""
    import backtest
    return jsonify(backtest.list_results())


@app.route('/api/backtest/<filename>')
def get_backtest_detail(filename):
    """加载指定回测存档(含逐笔交易)"""
    import backtest
    return jsonify(backtest.load_result(filename))


if __name__ == '__main__':
    # 加载本地配置覆盖
    config_path = os.path.join(BASE_DIR, 'config_local.py')
    if os.path.exists(config_path):
        import importlib
        spec = importlib.util.spec_from_file_location('config_local', config_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in dir(mod):
            if attr.isupper() and not attr.startswith('_'):
                setattr(config, attr, getattr(mod, attr))

    print(f"\n  233战法 Web仪表盘")
    print(f"  浏览器访问: http://127.0.0.1:5555")
    print(f"  按 Ctrl+C 退出\n")
    app.run(host='0.0.0.0', port=5555, debug=False)
