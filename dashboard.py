"""Localhost web dashboard cho bot — multi-market, hot-reload HTML."""

import copy
import os
import threading
import time

from flask import Flask, jsonify, send_file

app = Flask(__name__)

_state: dict = {
    "dry_run": False,
    "balance_start": None,
    "balance": None,
    "session_pnl": None,
    "markets": {},
}
_trade_log: list = []
_lock = threading.Lock()
_started = False

_HTML_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


def start(dry_run: bool = False, port: int = 5050) -> None:
    global _started
    if _started:
        return
    _started = True
    with _lock:
        _state["dry_run"] = dry_run
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="dashboard",
    )
    t.start()


def update_market(name: str, **fields) -> None:
    if not _started:
        return
    with _lock:
        m = _state["markets"].setdefault(name, {})
        m.update(fields)


def update_global(**fields) -> None:
    if not _started:
        return
    with _lock:
        if "balance" in fields:
            b = fields.pop("balance")
            if _state["balance_start"] is None:
                _state["balance_start"] = b
            _state["balance"] = b
            _state["session_pnl"] = b - _state["balance_start"]
        _state.update(fields)


def record_trade(market: str, side: str, size: int, price: float) -> None:
    if not _started:
        return
    with _lock:
        _trade_log.insert(0, {
            "ts":     time.strftime("%H:%M:%S"),
            "market": market,
            "side":   side,
            "size":   size,
            "price":  price,
            "result": "pending",
        })
        if len(_trade_log) > 100:
            _trade_log.pop()


def update_trade_result(market: str, result: str) -> None:
    if not _started:
        return
    with _lock:
        for entry in _trade_log:
            if entry.get("market") == market and entry.get("result") == "pending":
                entry["result"] = result
                break


@app.route("/")
def index():
    return send_file(_HTML_PATH)


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(copy.deepcopy(_state))


@app.route("/api/trades")
def api_trades():
    with _lock:
        return jsonify(list(_trade_log))
