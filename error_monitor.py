"""运行异常记录与按日分析。"""
import json
import os
import re
from collections import Counter
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ERROR_DIR = os.path.join(BASE_DIR, "history", "errors")


def _path(date_str=None, suffix=".jsonl"):
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    return os.path.join(ERROR_DIR, f"{date_str}{suffix}")


def log_exception(source, exc, context=None, retry_count=None):
    """记录最终失败，避免把每次重试都写成一条异常。"""
    os.makedirs(ERROR_DIR, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "context": context or {},
    }
    if retry_count is not None:
        record["retry_count"] = retry_count
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def _normalise_message(message):
    message = re.sub(r"https?://\S+", "<url>", str(message))
    message = re.sub(r"\d+", "<n>", message)
    return message[:300]


def analyze_error_logs(date_str=None):
    """分析指定日期异常，并保存 YYYYMMDD_summary.json。"""
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    records = []
    path = _path(date_str)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    by_source = Counter(r.get("source", "unknown") for r in records)
    by_type = Counter(r.get("exception_type", "unknown") for r in records)
    by_pattern = Counter(
        f"{r.get('source', 'unknown')}: {_normalise_message(r.get('message', ''))}"
        for r in records
    )
    report = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(records),
        "by_source": dict(by_source),
        "by_exception_type": dict(by_type),
        "top_patterns": [
            {"pattern": pattern, "count": count}
            for pattern, count in by_pattern.most_common(20)
        ],
        "latest": records[-20:],
    }
    os.makedirs(ERROR_DIR, exist_ok=True)
    with open(_path(date_str, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="分析 233 战法运行异常日志")
    parser.add_argument("--date", help="日期 YYYYMMDD，默认今天")
    args = parser.parse_args()
    print(json.dumps(analyze_error_logs(args.date), ensure_ascii=False, indent=2))
