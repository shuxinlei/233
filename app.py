"""
233战法 - Web仪表盘
Flask本地服务，浏览器访问查看筛选结果和触发扫描
"""
import warnings
warnings.filterwarnings('ignore')

import os
import json
import threading
import time
import re
from datetime import datetime
from flask import Flask, request, jsonify, render_template

import config
import data_source

app = Flask(__name__, template_folder='templates')

# 全局状态
task_status = {
    'pre_market': {'running': False, 'result': None, 'error': None, 'log': []},
    'intraday': {'running': False, 'result': None, 'error': None, 'log': []},
}


def _run_pre_market():
    """后台运行盘前筛选"""
    task_status['pre_market']['running'] = True
    task_status['pre_market']['result'] = None
    task_status['pre_market']['error'] = None
    task_status['pre_market']['log'] = []
    log = task_status['pre_market']['log']

    class LogCapture:
        def write(self, msg):
            if msg.strip():
                log.append(msg.strip())
        def flush(self):
            pass

    old_stdout = os.dup(1)
    import sys
    sys.stdout = LogCapture()

    try:
        from pre_market import build_stock_pool
        pool = build_stock_pool()
        task_status['pre_market']['result'] = {
            'count': len(pool),
            'stocks': [vars(s) for s in pool],
            'time': datetime.now().strftime('%H:%M:%S'),
        }
    except Exception as e:
        task_status['pre_market']['error'] = str(e)
    finally:
        sys.stdout = sys.__stdout__
        task_status['pre_market']['running'] = False


def _run_intraday():
    """后台运行盘中扫描"""
    task_status['intraday']['running'] = True
    task_status['intraday']['result'] = None
    task_status['intraday']['error'] = None
    task_status['intraday']['log'] = []
    log = task_status['intraday']['log']

    class LogCapture:
        def write(self, msg):
            if msg.strip():
                log.append(msg.strip())
        def flush(self):
            pass

    import sys
    sys.stdout = LogCapture()

    try:
        from intraday import scan
        results = scan(datetime.now().strftime('%H:%M'))
        task_status['intraday']['result'] = {
            'count': len(results),
            'results': [vars(r) for r in results],
            'time': datetime.now().strftime('%H:%M:%S'),
        }
    except Exception as e:
        task_status['intraday']['error'] = str(e)
    finally:
        sys.stdout = sys.__stdout__
        task_status['intraday']['running'] = False


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
    """获取任务状态"""
    return jsonify({
        'pre_market': {
            'running': task_status['pre_market']['running'],
            'result': task_status['pre_market']['result'],
            'error': task_status['pre_market']['error'],
            'log_tail': task_status['pre_market']['log'][-20:] if task_status['pre_market']['log'] else [],
        },
        'intraday': {
            'running': task_status['intraday']['running'],
            'result': task_status['intraday']['result'],
            'error': task_status['intraday']['error'],
            'log_tail': task_status['intraday']['log'][-20:] if task_status['intraday']['log'] else [],
        },
        'now': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/api/run/pre', methods=['POST'])
def run_pre():
    """触发盘前筛选"""
    if task_status['pre_market']['running']:
        return jsonify({'ok': False, 'msg': '盘前筛选正在运行中'}), 409
    t = threading.Thread(target=_run_pre_market, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '盘前筛选已启动'})


@app.route('/api/run/intraday', methods=['POST'])
def run_intraday():
    """触发盘中扫描"""
    if task_status['intraday']['running']:
        return jsonify({'ok': False, 'msg': '盘中扫描正在运行中'}), 409
    t = threading.Thread(target=_run_intraday, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '盘中扫描已启动'})


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
    with open('config_local.py', 'w', encoding='utf-8') as f:
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


if __name__ == '__main__':
    # 加载本地配置覆盖
    if os.path.exists('config_local.py'):
        import importlib
        spec = importlib.util.spec_from_file_location('config_local', 'config_local.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in dir(mod):
            if attr.isupper() and not attr.startswith('_'):
                setattr(config, attr, getattr(mod, attr))

    print(f"\n  233战法 Web仪表盘")
    print(f"  浏览器访问: http://127.0.0.1:5555")
    print(f"  按 Ctrl+C 退出\n")
    app.run(host='0.0.0.0', port=5555, debug=False)
