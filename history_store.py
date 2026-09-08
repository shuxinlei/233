"""历史行情快照与策略结果存储。

使用 SQLite 保存可回放的原始 DataFrame，避免回测依赖实时接口。
"""
import json
import os
import sqlite3
from datetime import datetime

import pandas as pd


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "history", "market_data.db")


def _connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_snapshots (
            data_kind TEXT NOT NULL,
            data_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            columns_json TEXT NOT NULL,
            records_json TEXT NOT NULL,
            PRIMARY KEY (data_kind, data_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_runs (
            run_kind TEXT NOT NULL,
            run_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_kind, run_key)
        )
    """)
    return conn


def save_dataframe(data_kind: str, data_key: str, df: pd.DataFrame):
    """保存一份接口快照；空 DataFrame 不覆盖已有有效数据。"""
    if df is None or df.empty:
        return
    payload = df.copy()
    # pandas 负责把 numpy 标量、NaN 和日期统一转换为可回放的 JSON 值。
    records = json.loads(payload.to_json(
        orient="records", force_ascii=False, date_format="iso"
    ))
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_snapshots "
            "(data_kind, data_key, fetched_at, columns_json, records_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (data_kind, data_key, datetime.now().isoformat(timespec="seconds"),
             json.dumps(list(payload.columns), ensure_ascii=False),
             json.dumps(records, ensure_ascii=False)),
        )


def load_dataframe(data_kind: str, data_key: str):
    """读取历史快照，不存在时返回空 DataFrame。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT columns_json, records_json FROM api_snapshots "
            "WHERE data_kind = ? AND data_key = ?",
            (data_kind, data_key),
        ).fetchone()
    if not row:
        return pd.DataFrame()
    columns = json.loads(row[0])
    records = json.loads(row[1])
    return pd.DataFrame(records, columns=columns)


def snapshot_fetched_at(data_kind: str, data_key: str):
    """返回快照的抓取时间(ISO字符串)，不存在返回 None。

    调用方据此判断快照是否已是最终版: 若抓取当天就是请求区间的结束日，
    那一天的K线可能还没发布，快照并不真的覆盖到区间末尾。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM api_snapshots WHERE data_kind = ? AND data_key = ?",
            (data_kind, data_key),
        ).fetchone()
    return row[0] if row else None


def save_strategy_result(run_kind: str, run_key: str, payload: dict):
    """保存一次策略运行结果，供回测评估和参数对比使用。"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategy_runs "
            "(run_kind, run_key, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (run_kind, run_key, datetime.now().isoformat(timespec="seconds"),
             json.dumps(payload, ensure_ascii=False, default=str)),
        )


def load_strategy_result(run_kind: str, run_key: str):
    """读取策略运行结果，不存在时返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM strategy_runs WHERE run_kind = ? AND run_key = ?",
            (run_kind, run_key),
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_snapshots(data_kind: str = None):
    """列出可用于回放的历史快照。"""
    query = "SELECT data_kind, data_key, fetched_at FROM api_snapshots"
    params = ()
    if data_kind:
        query += " WHERE data_kind = ?"
        params = (data_kind,)
    query += " ORDER BY data_key, data_kind"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {"data_kind": kind, "data_key": key, "fetched_at": fetched_at}
        for kind, key, fetched_at in rows
    ]
