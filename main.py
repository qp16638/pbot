"""Bot snipe Polymarket BTC/ETH Up/Down — chạy song song nhiều markets."""

import csv
import pathlib
import signal
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import config as cfg_module
import dashboard
import logger as log_module
import strategy
from config import MarketSpec
from polymarket import PolymarketClient

_running     = True
_session_pnl = 0.0
_pnl_lock    = threading.Lock()

# Per-market stats (keyed by spec.name, populated in run())
_trade_stats: dict[str, dict] = {}
_rev_stats:   dict[str, dict] = {}

REVERSAL_CSV  = pathlib.Path("reversals.csv")
_csv_lock     = threading.Lock()
_HIGH_ASK     = Decimal("0.99")


def _on_signal(sig, frame):
    global _running
    print("\n[!] Dung bot...")
    _running = False

signal.signal(signal.SIGINT,  _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


def _check_profit_limit(cfg, log) -> None:
    global _running, _session_pnl
    if cfg.profit_limit > 0 and _session_pnl >= cfg.profit_limit:
        log.info("=" * 60)
        log.info("  DAT MUC TIEU LOI NHUAN: $%.2f / $%.2f", _session_pnl, cfg.profit_limit)
        log.info("  Bot tu dong dung.")
        log.info("=" * 60)
        _running = False


def run() -> None:
    cfg = cfg_module.load()
    log = log_module.setup()
    dashboard.start(cfg.dry_run)

    if not cfg.markets:
        log.error("Khong co market nao duoc cau hinh trong MARKETS= (.env)")
        sys.exit(1)

    log.info("=" * 60)
    log.info("  Polymarket Snipe Bot — MULTI-MARKET")
    log.info("  DRY_RUN    = %s %s", cfg.dry_run,
             "(chi log)" if cfg.dry_run else "SE DAT LENH THAT!")
    log.info("  Markets    = %s", ", ".join(s.name for s in cfg.markets))
    for spec in cfg.markets:
        log.info("    [%s] slug=%s | symbol=%s | min_diff=%.2f | order_size=%d",
                 spec.name, spec.slug_prefix, spec.symbol, spec.min_diff, spec.order_size)
    log.info("  ORDER_PRICE= %.2f | SNIPE_SECONDS= %ds",
             cfg.order_price, cfg.snipe_seconds)
    log.info("=" * 60)

    if not cfg.private_key:
        log.error("PRIVATE_KEY chua duoc set trong .env!")
        sys.exit(1)

    # Init per-market stats
    for spec in cfg.markets:
        _trade_stats[spec.name] = {"placed": 0, "filled": 0, "unfilled": 0, "win": 0, "lose": 0}
        _rev_stats[spec.name]   = {"win": 0, "lose": 0}

    pm = PolymarketClient(cfg, log)

    threads = []
    for spec in cfg.markets:
        t = threading.Thread(
            target=_market_loop,
            args=(spec, pm, cfg, log),
            name=spec.name,
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("[START] Thread: %s", spec.name)

    for t in threads:
        t.join()

    log.info("Bot da dung.")


def _market_loop(spec: MarketSpec, pm: PolymarketClient, cfg, log) -> None:
    while _running:
        _run_one_round(spec, pm, cfg, log)
    log.info("[%s] Da dung.", spec.name)


def _run_one_round(spec: MarketSpec, pm: PolymarketClient, cfg, log) -> None:
    tag = spec.name

    # ── 1. Tìm market ─────────────────────────────────────────────────────────
    log.info("[%s] Tim market...", tag)
    market = _retry_find_market(spec, pm, log)
    if market is None:
        log.error("[%s] Khong tim duoc market. Nghi 60s.", tag)
        time.sleep(60)
        return

    question     = (market.get("question") or market.get("event_question")
                    or market.get("event_title") or market.get("title") or "(unknown)")
    end_time     = pm.get_end_time(market)
    tokens       = pm.get_tokens(market)
    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    market_slug  = (f"{spec.slug_prefix}-{int(end_time.timestamp()) - spec.interval_secs}"
                    if end_time else None)

    balance: Optional[float] = None
    try:
        balance = pm.get_usdc_balance()
        log.info("[%s][BALANCE] USDC: $%.2f", tag, balance)
        dashboard.update_global(balance=balance)
    except Exception as e:
        log.debug("[%s][BALANCE] Loi: %s", tag, e)

    log.info("[%s] %s", tag, question)
    log.info("[%s] Ket thuc: %s", tag,
             end_time.strftime("%H:%M:%S UTC") if end_time else "?")

    if not tokens:
        log.error("[%s] Khong lay duoc token IDs — bo qua, doi 10s", tag)
        time.sleep(10)
        return

    # ── 2. Init orderbook + fetch price open ──────────────────────────────────
    pm.init_orderbooks(list(tokens.values()))

    price_open: Optional[float] = None
    if end_time:
        round_start_ts = int(end_time.timestamp()) - spec.interval_secs
        price_open = pm.get_price_open_binance(
            spec.symbol, spec.interval, round_start_ts, spec.interval_secs
        )
        log.info("[%s] Open (%s %s) = %s", tag, spec.symbol, spec.interval, price_open)

    # Dùng effective_cfg với min_diff của market này
    effective_cfg = replace(cfg, snipe_min_diff=spec.min_diff)

    # ── 3. Vòng lặp check ─────────────────────────────────────────────────────
    traded_this_round               = False
    _pending_result: Optional[dict] = None
    price_current: Optional[float]  = None
    last_ob_fetch                   = 0.0
    last_status_log                 = 0.0
    last_high_ask_obs: dict[str, Optional[Decimal]] = {}

    while _running:
        now = datetime.now(timezone.utc)

        if end_time and now >= end_time:
            log.info("[%s] Round ket thuc.", tag)
            break

        if traded_this_round:
            time.sleep(1)
            continue

        # Fetch OB: 10s cuoi moi 200ms, ngoai 10s moi 2s
        if end_time:
            secs_now = (end_time - now).total_seconds()
            now_ts   = time.time()
            if 0 < secs_now <= 10:
                for tid in tokens.values():
                    pm.fetch_orderbook_rest(tid)
                price_current = pm.get_price_tick_binance(spec.symbol)
                if price_current and price_open:
                    log.debug("[%s] open=%.2f cur=%.2f diff=%+.2f",
                              tag, price_open, price_current, price_current - price_open)
            elif secs_now > 0 and now_ts - last_ob_fetch >= 2.0:
                for tid in tokens.values():
                    pm.fetch_orderbook_rest(tid)
                last_ob_fetch = now_ts
                price_current = None
            else:
                price_current = None

        # Check điều kiện từng side
        side_results: dict[str, strategy.CheckResult] = {}
        for side, token_id in tokens.items():
            ob = pm.get_orderbook_snapshot(token_id)
            side_results[side] = strategy.check_snipe(
                side, ob, end_time, price_current, price_open, effective_cfg
            )

        # Log status: ngoai 10s cuoi moi 60s, trong 10s cuoi moi 2s
        secs_left    = (end_time - now).total_seconds() if end_time else 0
        now_ts2      = time.time()
        status_freq  = 2.0 if secs_left <= 10 else 60.0
        if now_ts2 - last_status_log >= status_freq:
            diff_str   = (f"{price_current - price_open:+.1f}"
                          if (price_current and price_open) else "waiting")
            side_parts = []
            for side, res in side_results.items():
                ask_str = str(res.best_ask) if res.best_ask else "--"
                flag    = " ENTER" if res.should_enter else ""
                side_parts.append(f"[{side}] ask={ask_str}{flag}")
            log.info("[%s] con=%.0fs | diff=%s | %s",
                     tag, secs_left, diff_str, " | ".join(side_parts))
            last_status_log = now_ts2
            dashboard.update_market(
                tag,
                question=question,
                secs_left=secs_left,
                price_open=price_open,
                price_current=price_current,
                diff=((price_current - price_open) if (price_current and price_open) else None),
                sides={
                    s: {"ask": str(r.best_ask) if r.best_ask else None,
                        "should_enter": r.should_enter}
                    for s, r in side_results.items()
                },
                stats=dict(_trade_stats[tag]),
                rev=dict(_rev_stats[tag]),
            )

        # Cap nhat high-ask observation cho reversal tracking
        for side, res in side_results.items():
            if res.best_ask is None or res.best_ask >= _HIGH_ASK:
                last_high_ask_obs[side] = res.best_ask
            else:
                last_high_ask_obs.pop(side, None)

        # Đặt lệnh nếu đủ điều kiện
        for side, token_id in tokens.items():
            res = side_results[side]
            if not res.should_enter:
                continue

            min_cost = spec.order_size * cfg.order_price
            if not cfg.dry_run and (balance is None or balance < min_cost):
                log.error("[%s] Balance $%.2f < chi phi $%.2f — bo qua!",
                          tag, balance or 0.0, min_cost)
                break

            if cfg.dry_run:
                log.info("[%s][DRY RUN] BUY %d @ %.2f | %s | token=%s...",
                         tag, spec.order_size, cfg.order_price, side, token_id[:14])
                dashboard.record_trade(tag, side, spec.order_size, cfg.order_price)
            else:
                log.info("[%s][ORDER] BUY %d @ %.2f | %s",
                         tag, spec.order_size, cfg.order_price, side)
                order_id = _place_order_safe(spec, pm, token_id, cfg, log)
                if order_id:
                    _pending_result = {
                        "token_id": token_id, "side": side,
                        "slug": market_slug, "order_id": order_id,
                        "condition_id": condition_id,
                    }
                    _trade_stats[tag]["placed"] += 1
                    dashboard.record_trade(tag, side, spec.order_size, cfg.order_price)

            traded_this_round = True
            break

        time.sleep(0.2)

    # ── 4. Sau round: kiểm tra kết quả ────────────────────────────────────────
    if _pending_result:
        log.info("[%s] Doi resolve... 60s", tag)
        time.sleep(60)

        order_id    = _pending_result.get("order_id", "")
        filled_size = 0.0
        if order_id and order_id != "unknown":
            filled = pm.get_order_filled(order_id)
            if filled is not None:
                filled_size = filled
                log.info("[%s] Fill: %.0f / %d shares", tag, filled_size, spec.order_size)
            else:
                log.warning("[%s] Khong query duoc fill status", tag)
        else:
            log.warning("[%s] Khong co order_id — bo qua P&L", tag)

        if filled_size == 0.0 and order_id:
            log.info("[%s] Chua fill — huy order", tag)
            pm.cancel_order(order_id)
            _trade_stats[tag]["unfilled"] += 1
        else:
            _trade_stats[tag]["filled"] += 1
            actual_size = int(filled_size) if filled_size > 0 else spec.order_size
            result = pm.check_round_result(
                _pending_result["token_id"],
                _pending_result["side"],
                _pending_result.get("slug") or "",
            )
            if result:
                log.info("[%s] %s (side=%s, fill=%.0f)",
                         tag, result, _pending_result["side"], filled_size)
                if result == "WIN":
                    _trade_stats[tag]["win"] += 1
                elif result == "LOSE":
                    _trade_stats[tag]["lose"] += 1
                _update_pnl(result, actual_size, cfg, log)
                dashboard.update_trade_result(tag, result)
                if result == "WIN" and not cfg.dry_run:
                    _do_redeem(tag, _pending_result["token_id"],
                               _pending_result.get("condition_id", ""), pm, log)
            else:
                log.info("[%s] Chua xac dinh duoc ket qua", tag)

        ts = _trade_stats[tag]
        log.info("[%s][STATS] %d dat | %d khop | %d ko khop | W/L: %d/%d",
                 tag, ts["placed"], ts["filled"], ts["unfilled"], ts["win"], ts["lose"])

    # Reversal tracking — chỉ BTC-5M
    if last_high_ask_obs and spec.name == "BTC-5M":
        if not _pending_result:
            log.info("[%s] Doi resolve... 30s", tag)
            time.sleep(30)
        _check_reversal(spec, pm, tokens, last_high_ask_obs, market_slug, end_time, log)

    time.sleep(3)


def _check_reversal(spec: MarketSpec, pm, tokens, obs, slug, round_end, log) -> None:
    tag = spec.name
    results: dict[str, Optional[str]] = {}
    for side, ask_val in obs.items():
        token_id = tokens.get(side)
        if token_id:
            results[side] = pm.check_round_result(token_id, side, slug or "")
        else:
            results[side] = None

    for side, ask_val in obs.items():
        result  = results.get(side)
        ask_str = str(ask_val) if ask_val is not None else "N/A"
        is_rev  = result == "LOSE"
        if result == "WIN":
            _rev_stats[tag]["win"] += 1
        elif result == "LOSE":
            _rev_stats[tag]["lose"] += 1
        log.info("[%s][TRACK] %s ask=%s -> %s%s",
                 tag, side, ask_str, result or "?",
                 "  *** DAO CHIEU!" if is_rev else "")

    rs = _rev_stats[tag]
    log.info("[%s][TRACK] W/L: %d/%d (dao chieu: %d)",
             tag, rs["win"], rs["lose"], rs["lose"])

    _save_reversal_csv(spec, obs, results, slug, round_end, log)


def _save_reversal_csv(spec: MarketSpec, obs, results, slug, round_end, log) -> None:
    exists = REVERSAL_CSV.exists()
    try:
        with _csv_lock:
            with open(REVERSAL_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow(["round_end_utc", "market", "slug", "side",
                                "ask", "result", "is_reversal"])
                for side, ask_val in obs.items():
                    result = results.get(side)
                    w.writerow([
                        round_end.strftime("%Y-%m-%d %H:%M:%S") if round_end else "",
                        spec.name,
                        slug or "",
                        side,
                        str(ask_val) if ask_val is not None else "N/A",
                        result or "?",
                        "YES" if result == "LOSE" else "NO",
                    ])
    except Exception as e:
        log.debug("[%s][TRACK] Loi ghi CSV: %s", spec.name, e)


def _place_order_safe(spec: MarketSpec, pm: PolymarketClient,
                      token_id: str, cfg, log) -> Optional[str]:
    tag = spec.name
    for attempt in range(1, 3):
        try:
            resp     = pm.place_buy_limit(token_id, cfg.order_price, spec.order_size)
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id") or ""
            log.info("[%s][ORDER] OK attempt=%d order_id=%s",
                     tag, attempt, order_id[:12] if order_id else "?")
            return order_id or "unknown"
        except Exception as exc:
            log.error("[%s][ORDER] Fail attempt=%d: %s", tag, attempt, exc)
            if attempt < 2:
                time.sleep(0.1)
    return None


def _retry_find_market(spec: MarketSpec, pm: PolymarketClient, log) -> dict | None:
    tag = spec.name
    for i in range(1, 11):
        market = pm.find_market(spec.slug_prefix, spec.interval_secs)
        if market:
            return market
        log.warning("[%s] Khong tim thay (lan %d/10), thu lai sau 15s...", tag, i)
        time.sleep(15)
    return None


def _do_redeem(tag: str, token_id: str, condition_id: str,
               pm: PolymarketClient, log) -> None:
    """Retry redeem tối đa 4 lần, mỗi lần cách nhau 60s.
    Polymarket cần ~2-3 phút settle on-chain sau khi round kết thúc.
    """
    for attempt in range(1, 5):
        log.info("[%s] Redeem win token %s... (lan %d/4)", tag, token_id[:14], attempt)
        ok = pm.redeem_position(token_id, condition_id)
        if ok:
            log.info("[%s] Redeem thanh cong.", tag)
            try:
                new_bal = pm.get_usdc_balance()
                log.info("[%s][BALANCE] Sau redeem: $%.2f", tag, new_bal)
                dashboard.update_global(balance=new_bal)
            except Exception as e:
                log.debug("[%s] Loi refresh balance: %s", tag, e)
            return
        if attempt < 4:
            log.info("[%s] Redeem chua duoc (settle chua xong), thu lai sau 60s...", tag)
            time.sleep(60)
    log.warning("[%s] Redeem that bai sau 4 lan thu.", tag)


def _update_pnl(result: str, size: int, cfg, log) -> None:
    global _session_pnl
    if result == "WIN":
        pnl = size * (1.0 - cfg.order_price)
    elif result == "LOSE":
        pnl = -(size * cfg.order_price)
    else:
        return
    with _pnl_lock:
        _session_pnl += pnl
        session = _session_pnl
    sign = "+" if pnl >= 0 else ""
    log.info("[PNL] Phien: %s$%.2f  (vua: %s$%.2f)",
             "+" if session >= 0 else "", session, sign, pnl)
    _check_profit_limit(cfg, log)


if __name__ == "__main__":
    run()
