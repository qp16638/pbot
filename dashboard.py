"""
Localhost web dashboard cho bot.
Flask chạy trong background thread; bot ghi state mỗi 200ms, frontend poll mỗi 300ms.
"""

import os
import threading
from flask import Flask, jsonify, send_file

app = Flask(__name__)

_state: dict = {}
_trade_log: list = []
_lock = threading.Lock()


def update_state(data: dict) -> None:
    global _state
    with _lock:
        _state = data


def record_trade(entry: dict) -> None:
    with _lock:
        _trade_log.insert(0, entry)
        if len(_trade_log) > 50:
            _trade_log.pop()


def update_trade_result(idx: int, result: str) -> None:
    with _lock:
        if 0 <= idx < len(_trade_log):
            _trade_log[idx]["result"] = result


def update_state_field(key: str, value) -> None:
    """Cập nhật một field đơn lẻ trong state mà không ghi đè toàn bộ."""
    global _state
    with _lock:
        _state[key] = value


_HTML_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")

@app.route("/")
def index():
    return send_file(_HTML_PATH)


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_state)


@app.route("/api/trades")
def api_trades():
    with _lock:
        return jsonify(_trade_log)


def start(port: int = 5050) -> None:
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="dashboard",
    )
    t.start()


