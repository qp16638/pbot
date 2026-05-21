"""Bot snipe Polymarket BTC Up/Down 5m — canh đặt lệnh trong X giây cuối round."""

import csv
import pathlib
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import config as cfg_module
import logger as log_module
import strategy
from polymarket import PolymarketClient

_running     = True
_session_pnl = 0.0

_trade_stats = {"placed": 0, "filled": 0, "unfilled": 0, "win": 0, "lose": 0}
_rev_stats   = {"win": 0, "lose": 0}

REVERSAL_CSV = pathlib.Path("reversals.csv")
_HIGH_ASK    = Decimal("0.99")


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

    log.info("=" * 60)
    log.info("  Polymarket BTC Up/Down 5m — SNIPE MODE")
    log.info("  DRY_RUN       = %s %s", cfg.dry_run,
             "(chi log)" if cfg.dry_run else "SE DAT LENH THAT!")
    log.info("  TARGET_PRICE  = %.2f (nguong ask hop le)", cfg.target_price)
    log.info("  ORDER_PRICE   = %.2f (gia dat lenh)", cfg.order_price)
    log.info("  ORDER_SIZE    = %d shares", cfg.order_size)
    log.info("  SNIPE_SECONDS = %ds cuoi round", cfg.snipe_seconds)
    log.info("  SNIPE_MIN_DIFF= $%.1f", cfg.snipe_min_diff)
    log.info("=" * 60)

    if not cfg.private_key:
        log.error("PRIVATE_KEY chua duoc set trong .env!")
        sys.exit(1)

    pm = PolymarketClient(cfg, log)

    while _running:
        _run_one_round(pm, cfg, log)

    log.info("Bot da dung.")


def _run_one_round(pm: PolymarketClient, cfg, log) -> None:
    # ── 1. Tìm market ─────────────────────────────────────────────────────────
    log.info("[ROUND] Tim market BTC Up/Down 5m...")
    market = _retry_find_market(pm, log)
    if market is None:
        log.error("[ROUND] Khong tim duoc market. Nghi 60s.")
        time.sleep(60)
        return

    question    = (market.get("question") or market.get("event_question")
                   or market.get("event_title") or market.get("title") or "(unknown)")
    end_time    = pm.get_end_time(market)
    tokens      = pm.get_tokens(market)
    market_slug = f"btc-updown-5m-{int(end_time.timestamp()) - 300}" if end_time else None

    balance: Optional[float] = None
    try:
        balance = pm.get_usdc_balance()
        log.info("[BALANCE] USDC: $%.2f", balance)
    except Exception as e:
        log.debug("[BALANCE] Loi: %s", e)

    log.info("[ROUND] %s", question)
    log.info("[ROUND] Ket thuc: %s", end_time.strftime("%H:%M:%S UTC") if end_time else "?")

    if not tokens:
        log.error("[ROUND] Khong lay duoc token IDs — bo qua, doi 10s")
        time.sleep(10)
        return

    # ── 2. Init orderbook + fetch BTC open ────────────────────────────────────
    pm.init_orderbooks(list(tokens.values()))

    btc_open: Optional[float] = None
    if end_time:
        round_start_ts = int(end_time.timestamp()) - 300
        btc_open = pm.get_btc_open_binance(round_start_ts)
        log.info("[SNIPE] BTC open (Binance 5m) = %s", btc_open)

    # ── 3. Vòng lặp check ─────────────────────────────────────────────────────
    traded_this_round               = False
    traded_side: str                = ""
    _pending_result: Optional[dict] = None
    btc_current: Optional[float]    = None
    last_ob_fetch                   = 0.0
    last_high_ask_obs: dict[str, Optional[Decimal]] = {}

    while _running:
        now = datetime.now(timezone.utc)

        if end_time and now >= end_time:
            sys.stdout.write("\n")
            sys.stdout.flush()
            log.info("[ROUND] Round ket thuc.")
            break

        if traded_this_round:
            time.sleep(1)
            continue

        # Fetch OB: 10s cuoi moi 200ms, ngoai 10s moi 5s (tu dau round)
        if end_time:
            secs_now = (end_time - now).total_seconds()
            now_ts   = time.time()
            if 0 < secs_now <= 10:
                for tid in tokens.values():
                    pm.fetch_orderbook_rest(tid)
                btc_current = pm.get_btc_tick_binance()
                if btc_current and btc_open:
                    log.debug("[SNIPE] open=%.2f cur=%.2f diff=%+.2f",
                              btc_open, btc_current, btc_current - btc_open)
            elif secs_now > 0 and now_ts - last_ob_fetch >= 5.0:
                for tid in tokens.values():
                    pm.fetch_orderbook_rest(tid)
                last_ob_fetch = now_ts
                btc_current   = None
            else:
                btc_current = None

        # Check điều kiện từng side
        side_results: dict[str, strategy.CheckResult] = {}
        for side, token_id in tokens.items():
            ob = pm.get_orderbook_snapshot(token_id)
            side_results[side] = strategy.check_snipe(
                side, ob, end_time, btc_current, btc_open, cfg
            )

        # Terminal status
        secs_left = (end_time - now).total_seconds() if end_time else 0
        diff_str  = f"{btc_current - btc_open:+.1f}" if (btc_current and btc_open) else "waiting"
        side_parts = []
        for side, result in side_results.items():
            ask_str = str(result.best_ask) if result.best_ask else "--"
            flag    = " *** ENTER" if result.should_enter else ""
            side_parts.append(f"[{side:>4}] ask={ask_str:<5}{flag}")
        sys.stdout.write(
            f"\r[SNIPE] con={secs_left:.0f}s | diff={diff_str} | {'  |  '.join(side_parts)}    "
        )
        sys.stdout.flush()

        # Cap nhat high-ask observation cho reversal tracking
        for side, res in side_results.items():
            if res.best_ask is None or res.best_ask >= _HIGH_ASK:
                last_high_ask_obs[side] = res.best_ask
            else:
                last_high_ask_obs.pop(side, None)

        # Đặt lệnh nếu đủ điều kiện
        for side, token_id in tokens.items():
            result = side_results[side]
            if not result.should_enter:
                continue

            sys.stdout.write("\n")
            sys.stdout.flush()

            min_cost = cfg.order_size * cfg.order_price
            if not cfg.dry_run and (balance is None or balance < min_cost):
                log.error("[ORDER] Balance $%.2f < chi phi $%.2f — bo qua!", balance or 0.0, min_cost)
                break

            if cfg.dry_run:
                log.info("[DRY RUN] BUY %d shares @ %.2f | %s | token=%s...",
                         cfg.order_size, cfg.order_price, side, token_id[:14])
            else:
                log.info("[ORDER] BUY %d shares @ %.2f | %s",
                         cfg.order_size, cfg.order_price, side)
                order_id = _place_order_safe(pm, token_id, cfg, log)
                if order_id:
                    _pending_result = {
                        "token_id": token_id, "side": side,
                        "slug": market_slug, "order_id": order_id,
                    }
                    _trade_stats["placed"] += 1

            traded_this_round = True
            traded_side = side
            break

        time.sleep(0.2)

    # ── 4. Sau round: kiểm tra kết quả ────────────────────────────────────────
    if _pending_result:
        log.info("[RESULT] Doi 60s de Polymarket resolve...")
        time.sleep(60)

        order_id    = _pending_result.get("order_id", "")
        filled_size = 0.0
        if order_id and order_id != "unknown":
            filled = pm.get_order_filled(order_id)
            if filled is not None:
                filled_size = filled
                log.info("[RESULT] Fill: %.0f / %d shares", filled_size, cfg.order_size)
            else:
                log.warning("[RESULT] Khong query duoc fill status")
        else:
            log.warning("[RESULT] Khong co order_id — bo qua P&L")

        if filled_size == 0.0 and order_id:
            log.info("[RESULT] Chua fill — huy order")
            pm.cancel_order(order_id)
            _trade_stats["unfilled"] += 1
        else:
            _trade_stats["filled"] += 1
            actual_size = int(filled_size) if filled_size > 0 else cfg.order_size
            result = pm.check_round_result(
                _pending_result["token_id"],
                _pending_result["side"],
                _pending_result.get("slug") or "",
            )
            if result:
                log.info("[RESULT] %s (side=%s, fill=%.0f)",
                         result, _pending_result["side"], filled_size)
                if result == "WIN":
                    _trade_stats["win"] += 1
                elif result == "LOSE":
                    _trade_stats["lose"] += 1
                _update_pnl(result, actual_size, cfg, log)
            else:
                log.info("[RESULT] Chua xac dinh duoc ket qua")

        log.info("[STATS] Lenh: %d dat | %d khop | %d ko khop | W/L: %d/%d",
                 _trade_stats["placed"], _trade_stats["filled"],
                 _trade_stats["unfilled"], _trade_stats["win"], _trade_stats["lose"])

    # Reversal tracking (chay moi round co high-ask obs)
    if last_high_ask_obs:
        if not _pending_result:
            log.info("[TRACK] Doi 30s de Polymarket resolve...")
            time.sleep(30)
        _check_reversal(pm, tokens, last_high_ask_obs, market_slug, end_time, log)

    time.sleep(3)


def _check_reversal(pm, tokens, obs, slug, round_end, log) -> None:
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
            _rev_stats["win"] += 1
        elif result == "LOSE":
            _rev_stats["lose"] += 1
        log.info("[TRACK] %s ask=%s -> %s%s",
                 side, ask_str, result or "?",
                 "  *** DAO CHIEU!" if is_rev else "")

    log.info("[TRACK] W/L: %d/%d (dao chieu: %d)",
             _rev_stats["win"], _rev_stats["lose"], _rev_stats["lose"])

    _save_reversal_csv(obs, results, slug, round_end, log)


def _save_reversal_csv(obs, results, slug, round_end, log) -> None:
    exists = REVERSAL_CSV.exists()
    try:
        with open(REVERSAL_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["round_end_utc", "slug", "side", "ask", "result", "is_reversal"])
            for side, ask_val in obs.items():
                result = results.get(side)
                w.writerow([
                    round_end.strftime("%Y-%m-%d %H:%M:%S") if round_end else "",
                    slug or "",
                    side,
                    str(ask_val) if ask_val is not None else "N/A",
                    result or "?",
                    "YES" if result == "LOSE" else "NO",
                ])
    except Exception as e:
        log.debug("[TRACK] Loi ghi CSV: %s", e)


def _place_order_safe(pm: PolymarketClient, token_id: str, cfg, log) -> Optional[str]:
    for attempt in range(1, 3):
        try:
            resp     = pm.place_buy_limit(token_id, cfg.order_price, cfg.order_size)
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id") or ""
            log.info("[ORDER] OK attempt=%d order_id=%s", attempt, order_id[:12] if order_id else "?")
            return order_id or "unknown"
        except Exception as exc:
            log.error("[ORDER] Fail attempt=%d: %s", attempt, exc)
            if attempt < 2:
                time.sleep(0.1)
    return None


def _retry_find_market(pm: PolymarketClient, log) -> dict | None:
    for i in range(1, 11):
        market = pm.find_btc_5m_market()
        if market:
            return market
        log.warning("[MARKET] Khong tim thay (lan %d/10), thu lai sau 15s...", i)
        time.sleep(15)
    return None


def _update_pnl(result: str, size: int, cfg, log) -> None:
    global _session_pnl
    if result == "WIN":
        pnl = size * (1.0 - cfg.order_price)
    elif result == "LOSE":
        pnl = -(size * cfg.order_price)
    else:
        return
    _session_pnl += pnl
    sign = "+" if pnl >= 0 else ""
    log.info("[PNL] Phien: %s$%.2f  (vua: %s$%.2f)",
             "+" if _session_pnl >= 0 else "", _session_pnl, sign, pnl)
    _check_profit_limit(cfg, log)


if __name__ == "__main__":
    run()
