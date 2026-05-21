"""
Wrapper cho Polymarket CLOB API.
Cung cấp: tìm market, fetch orderbook (REST inline), đặt lệnh.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import requests

CLOB_HOST    = "https://clob.polymarket.com"
GAMMA_HOST   = "https://gamma-api.polymarket.com"
CHAIN_ID     = 137
REST_TIMEOUT = 6
MAX_RETRIES  = 3


# ── Orderbook state ─────────────────────────────────────────────────────────────

class OrderbookState:
    """Thread-safe local snapshot của orderbook cho một token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self._bids: dict[Decimal, Decimal] = {}  # price → size
        self._asks: dict[Decimal, Decimal] = {}
        self._lock = threading.Lock()
        self.updated_at: float = 0.0

    # ── Mutations (gọi từ WS thread) ──────────────────────────────────────────

    def apply_snapshot(self, bids: list, asks: list) -> None:
        with self._lock:
            self._bids = {
                Decimal(str(b["price"])): Decimal(str(b["size"]))
                for b in bids if b.get("price") and b.get("size")
            }
            self._asks = {
                Decimal(str(a["price"])): Decimal(str(a["size"]))
                for a in asks if a.get("price") and a.get("size")
            }
            self.updated_at = time.time()

    def apply_level_update(self, side: str, price: Decimal, size: Decimal) -> None:
        """Cập nhật một mức giá đơn lẻ. size=0 → xóa mức giá đó."""
        with self._lock:
            book = self._asks if side.upper() in ("ASK", "SELL") else self._bids
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
            self.updated_at = time.time()

    # ── Queries (gọi từ main thread) ──────────────────────────────────────────

    def best_ask(self) -> Optional[Decimal]:
        with self._lock:
            return min(self._asks.keys()) if self._asks else None

    def best_bid(self) -> Optional[Decimal]:
        with self._lock:
            return max(self._bids.keys()) if self._bids else None

    def shares_at_ask_price(self, price: Decimal) -> Decimal:
        with self._lock:
            return self._asks.get(price, Decimal("0"))

    def shares_at_bid_price(self, price: Decimal) -> Decimal:
        with self._lock:
            return self._bids.get(price, Decimal("0"))

    def asks_summary(self) -> list[tuple[Decimal, Decimal]]:
        """Trả về [(price, size)] sorted ascending — dùng để debug."""
        with self._lock:
            return sorted(self._asks.items())

    @property
    def is_fresh(self) -> bool:
        """True nếu dữ liệu được cập nhật trong vòng 2 giây."""
        return (time.time() - self.updated_at) < 2.0

    @property
    def has_data(self) -> bool:
        return self.updated_at > 0.0


# ── Polymarket client ───────────────────────────────────────────────────────────

class PolymarketClient:

    def __init__(self, config, log: logging.Logger):
        self.cfg = config
        self.log = log

        self._orderbooks: dict[str, OrderbookState] = {}
        self._client = None
        self._eoa_address: str = ""

        self._setup_clob_client()

    # ── Auth setup ─────────────────────────────────────────────────────────────

    def _setup_clob_client(self) -> None:
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
        except ImportError:
            self.log.error("Thiếu py-clob-client-v2. Chạy: pip install py-clob-client-v2")
            raise

        # Tính deposit wallet (CREATE2) từ EOA
        try:
            from py_builder_relayer_client.builder.derive import derive
            from py_builder_relayer_client.config import get_contract_config as get_relayer_cfg
            from py_clob_client_v2.signer import Signer as _Signer
            _eoa = _Signer(self.cfg.private_key, CHAIN_ID).address()
            self._eoa_address = _eoa
            _factory = get_relayer_cfg(CHAIN_ID).safe_factory
            _deposit_wallet = derive(_eoa, _factory)
            self.log.info("[AUTH] EOA          = %s", _eoa)
            self.log.info("[AUTH] DepositWallet= %s (tính từ CREATE2)", _deposit_wallet)
            self.log.info("[AUTH] FunderEnv    = %s", self.cfg.funder or "(trống)")
        except Exception as _e:
            self.log.debug("[AUTH] Không tính được deposit wallet: %s", _e)

        if self.cfg.api_key:
            creds = ApiCreds(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                api_passphrase=self.cfg.api_passphrase,
            )
            sig_type = 1 if self.cfg.funder else 0
            self._client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=self.cfg.private_key,
                creds=creds,
                signature_type=sig_type,
                funder=self.cfg.funder if self.cfg.funder else None,
            )
            self.log.info(
                "[AUTH] Dùng API key từ .env ✓ (sig_type=%d, funder=%s)",
                sig_type, self.cfg.funder or "EOA",
            )
        else:
            self.log.info("[AUTH] Chưa có API key — đang tạo từ private key...")
            client_l1 = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=self.cfg.private_key,
                signature_type=0,
            )
            try:
                creds = client_l1.create_or_derive_api_key()
                sig_type = 1 if self.cfg.funder else 0
                self._client = ClobClient(
                    host=CLOB_HOST,
                    chain_id=CHAIN_ID,
                    key=self.cfg.private_key,
                    creds=creds,
                    signature_type=sig_type,
                    funder=self.cfg.funder if self.cfg.funder else None,
                )
                self.log.warning(
                    "[AUTH] Đã tạo API key mới (sig_type=%d, funder=%s). Thêm vào .env rồi restart:\n"
                    "  POLYMARKET_API_KEY=%s\n"
                    "  POLYMARKET_API_SECRET=%s\n"
                    "  POLYMARKET_API_PASSPHRASE=%s",
                    sig_type, self.cfg.funder or "EOA",
                    creds.api_key, creds.api_secret, creds.api_passphrase,
                )
            except Exception as exc:
                self.log.error("[AUTH] Tạo API key thất bại: %s", exc)
                raise

    # ── Market discovery ───────────────────────────────────────────────────────

    def find_btc_5m_market(self) -> Optional[dict]:
        """
        Tìm BTC Up/Down 5m market active, kết thúc sớm nhất trong tương lai.
        Trả về market dict (markets[0] từ event) hoặc None.
        Slug pattern: btc-updown-5m-{(unix_now // 300) * 300}
        """
        now  = int(time.time())
        base = (now // 300) * 300

        # Thử round hiện tại và vài round lân cận
        for offset in (0, -300, 300, -600, 600):
            ts   = base + offset
            slug = f"btc-updown-5m-{ts}"
            m    = self._fetch_market_by_event_slug(slug)
            if m is None:
                continue
            end = self._parse_end_time(m)
            if end and end > datetime.now(timezone.utc):
                self.log.debug("[MARKET] Dùng slug %s (end=%s)", slug, end)
                return m

        return None

    def _fetch_market_by_event_slug(self, slug: str) -> Optional[dict]:
        """Fetch markets[0] từ Gamma /events?slug=..., đính kèm event-level title/description."""
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/events",
                params={"slug": slug},
                timeout=REST_TIMEOUT,
            )
            resp.raise_for_status()
            data   = resp.json()
            events = data if isinstance(data, list) else data.get("events", [])
            if not events:
                return None
            event      = events[0]
            markets_in = event.get("markets", [])
            if not markets_in:
                return None
            market = dict(markets_in[0])
            # Gắn thêm event-level fields để parse price_to_beat
            for key in ("title", "description", "question"):
                ev_val = event.get(key)
                if ev_val and not market.get(f"event_{key}"):
                    market[f"event_{key}"] = ev_val
            return market
        except Exception:
            return None

    def _parse_end_time(self, market: dict) -> Optional[datetime]:
        for key in ("endDate", "end_date_iso", "endDateIso", "end_date", "endTime"):
            val = market.get(key)
            if not val:
                continue
            if isinstance(val, (int, float)):
                return datetime.fromtimestamp(float(val), tz=timezone.utc)
            s = str(val).strip()
            # Bỏ qua giá trị chỉ có ngày (YYYY-MM-DD) — không đủ chính xác
            if len(s) == 10:
                continue
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                try:
                    return datetime.fromtimestamp(float(s), tz=timezone.utc)
                except ValueError:
                    pass
        return None

    def get_tokens(self, market: dict) -> dict[str, str]:
        """Trả về {"UP": token_id, "DOWN": token_id}."""
        # Format mới: clobTokenIds + outcomes (JSON strings từ Gamma events API)
        clob_raw     = market.get("clobTokenIds")
        outcomes_raw = market.get("outcomes")
        if clob_raw and outcomes_raw:
            try:
                token_ids = json.loads(clob_raw)     if isinstance(clob_raw, str)     else clob_raw
                outcomes  = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                result = {}
                for outcome, tid in zip(outcomes, token_ids):
                    key = outcome.upper()  # "UP" hoặc "DOWN"
                    result[key] = str(tid)
                if result:
                    return result
            except Exception as exc:
                self.log.debug("[MARKET] Parse clobTokenIds lỗi: %s", exc)

        # Fallback: tokens array (format cũ)
        result: dict[str, str] = {}
        for token in market.get("tokens", []):
            outcome = (token.get("outcome") or "").upper().strip()
            tid = str(token.get("token_id") or token.get("tokenId") or token.get("id") or "")
            if not tid:
                continue
            if outcome in ("UP", "HIGHER", "YES"):
                result.setdefault("UP", tid)
            elif outcome in ("DOWN", "LOWER", "NO"):
                result.setdefault("DOWN", tid)
        return result

    def get_end_time(self, market: dict) -> Optional[datetime]:
        return self._parse_end_time(market)

    # ── Orderbook ──────────────────────────────────────────────────────────────

    def init_orderbooks(self, token_ids: list[str]) -> None:
        self._orderbooks = {tid: OrderbookState(tid) for tid in token_ids}

    def fetch_orderbook_rest(self, token_id: str) -> bool:
        """Fetch orderbook qua REST. Returns True nếu thành công."""
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(
                    f"{CLOB_HOST}/book",
                    params={"token_id": token_id},
                    timeout=REST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                ob = self._orderbooks.get(token_id)
                if ob:
                    ob.apply_snapshot(data.get("bids", []), data.get("asks", []))
                    self.log.debug(
                        "[OB-REST] %s... best_ask=%s",
                        token_id[:14], ob.best_ask(),
                    )
                return True
            except Exception as exc:
                wait = 0.5 * (attempt + 1)
                self.log.debug("[OB-REST] Attempt %d lỗi (%s), retry sau %.1fs", attempt + 1, exc, wait)
                time.sleep(wait)
        return False

    def get_orderbook_snapshot(self, token_id: str) -> Optional["OrderbookState"]:
        """Trả về orderbook hiện tại — không auto-fetch."""
        return self._orderbooks.get(token_id)

    # ── Order placement ────────────────────────────────────────────────────────

    def get_usdc_balance(self) -> float:
        """Lấy USDC balance từ Polymarket CLOB API (số dư thật trong exchange)."""
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self.log.info("[BALANCE] raw CLOB response: %s", resp)
            raw = resp.get("balance") or "0"
            usdc = int(raw) / 1e6
            if usdc == 0.0:
                self._log_onchain_usdc()
                self._find_real_proxy()
            return usdc
        except Exception as e:
            self.log.warning("[BALANCE] CLOB balance lỗi: %s", e)
            return 0.0

    def _find_real_proxy(self) -> None:
        """Tìm proxy wallet thật qua Exchange V1 getPolyProxyWalletAddress."""
        _RPC  = "https://polygon-rpc.com"
        _EX1  = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
        if not self._eoa_address:
            return
        try:
            from eth_utils import keccak
            sel  = keccak(text="getPolyProxyWalletAddress(address)")[:4]
            padded = self._eoa_address[2:].lower().zfill(64)
            data = "0x" + sel.hex() + padded
            r = requests.post(_RPC, json={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": _EX1, "data": data}, "latest"], "id": 1,
            }, timeout=6)
            raw = r.json().get("result", "0x")
            if raw and len(raw) >= 42:
                proxy = "0x" + raw[-40:]
                if proxy != "0x" + "0" * 40:
                    self.log.info("[BALANCE] *** Proxy thật (Exchange V1) = %s", proxy)
                    if self.cfg.funder and proxy.lower() != self.cfg.funder.lower():
                        self.log.warning(
                            "[BALANCE] *** FUNDER trong .env (%s) KHÁC proxy thật (%s)!",
                            self.cfg.funder[:12], proxy[:12],
                        )
                        self.log.warning("[BALANCE] *** Hãy đặt POLYMARKET_FUNDER=%s", proxy)
        except Exception as e:
            self.log.debug("[BALANCE] _find_real_proxy lỗi: %s", e)

        # Thử sig_type=0 (EOA trực tiếp, không proxy)
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
            creds = ApiCreds(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                api_passphrase=self.cfg.api_passphrase,
            )
            c0 = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID,
                            key=self.cfg.private_key, creds=creds, signature_type=0)
            r0 = c0.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            bal0 = int(r0.get("balance") or "0") / 1e6
            self.log.info("[BALANCE] sig_type=0 (EOA, no proxy): $%.2f", bal0)
        except Exception as e:
            self.log.debug("[BALANCE] sig_type=0 check lỗi: %s", e)

    def _log_onchain_usdc(self) -> None:
        """Log USDC on-chain balance của proxy và EOA để debug."""
        _RPC  = "https://polygon-rpc.com"
        _USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        sel   = bytes.fromhex("70a08231")  # balanceOf(address)
        import struct
        addrs = {}
        if self.cfg.funder:
            addrs["proxy"] = self.cfg.funder
        if self._eoa_address:
            addrs["EOA"] = self._eoa_address
        for label, addr in addrs.items():
            try:
                padded = addr[2:].lower().zfill(64)
                data   = "0x" + sel.hex() + padded
                r = requests.post(_RPC, json={
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": _USDC, "data": data}, "latest"], "id": 1,
                }, timeout=6)
                raw = r.json().get("result", "0x")
                bal = int(raw, 16) / 1e6 if raw and raw != "0x" else 0.0
                self.log.info("[BALANCE] on-chain USDC %s (%s): $%.2f", label, addr[:12], bal)
            except Exception as e:
                self.log.debug("[BALANCE] on-chain check %s lỗi: %s", label, e)

    def get_btc_price(self) -> Optional[float]:
        """Lấy giá BTC từ Pyth Network oracle — cùng nguồn Polymarket dùng để settle."""
        # BTC/USD feed ID trên Pyth
        FEED_ID = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
        try:
            r = requests.get(
                "https://hermes.pyth.network/v2/updates/price/latest",
                params={"ids[]": FEED_ID},
                timeout=4,
            )
            parsed = r.json()["parsed"][0]["price"]
            return float(parsed["price"]) * (10 ** int(parsed["expo"]))
        except Exception:
            return None

    def check_round_result(self, token_id: str, bet_side: str, slug: str) -> Optional[str]:
        """Sau khi round kết thúc, trả về 'WIN', 'LOSE', hoặc None (chưa resolve)."""
        # Thử Gamma API trước
        try:
            resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=6)
            events = resp.json()
            events = events if isinstance(events, list) else events.get("events", [])
            if events:
                market = (events[0].get("markets") or [{}])[0]
                winner = market.get("winner") or market.get("winnerOutcome")
                if winner:
                    return "WIN" if winner.upper() == bet_side.upper() else "LOSE"
        except Exception:
            pass
        # Fallback: CLOB last trade price (> 0.9 → token resolve WIN)
        try:
            price = float(self._client.get_last_trade_price(token_id).get("price", 0.5))
            if price > 0.9:
                return "WIN"
            if price < 0.1:
                return "LOSE"
        except Exception:
            pass
        return None

    def get_order_filled(self, order_id: str) -> Optional[float]:
        """Trả về số shares đã fill. None nếu lỗi."""
        try:
            order = self._client.get_order(order_id)
            return float(order.get("size_matched") or order.get("matched_amount") or 0)
        except Exception as e:
            self.log.debug("[ORDER] get_order_filled lỗi: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Hủy order. Trả về True nếu thành công."""
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            self.log.debug("[ORDER] cancel_order lỗi: %s", e)
            return False

    def redeem_position(self, token_id: str) -> bool:
        """
        Redeem winning CTF position on-chain qua Polygon RPC.
        Tự động dùng POLY_PROXY execute nếu funder được set.
        """
        _RPC         = "https://polygon-rpc.com"
        _EXCHANGE_V1 = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
        _CTF         = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

        def _rpc(method, params):
            r = requests.post(_RPC, json={
                "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
            }, timeout=15)
            return r.json()

        def _eth_call(to, data: bytes) -> bytes:
            res = _rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])
            raw = res.get("result", "0x")
            return bytes.fromhex(raw[2:]) if len(raw) > 2 else b""

        try:
            from eth_abi import encode as _enc
            from eth_account import Account
            from eth_utils import keccak

            # ── 1. Lấy conditionId từ Exchange V1 ────────────────────────────
            sel_cid = keccak(text="getConditionId(uint256)")[:4]
            raw_cid = _eth_call(_EXCHANGE_V1, sel_cid + _enc(["uint256"], [int(token_id)]))
            cid     = raw_cid[:32] if len(raw_cid) >= 32 else b'\x00' * 32
            if cid == b'\x00' * 32:
                self.log.error("[REDEEM] conditionId = 0 — token chưa register hoặc sai exchange")
                return False
            self.log.info("[REDEEM] conditionId = 0x%s", cid.hex())

            # ── 2. Lấy collateral token từ Exchange V1 ────────────────────────
            sel_col    = keccak(text="getCollateral()")[:4]
            raw_col    = _eth_call(_EXCHANGE_V1, sel_col)
            collateral = "0x" + raw_col[-20:].hex() if len(raw_col) >= 20 else \
                         "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

            # ── 3. Encode redeemPositions calldata ────────────────────────────
            sel_rdm  = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
            rdm_data = sel_rdm + _enc(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [collateral, b'\x00' * 32, cid, [1, 2]],
            )

            # ── 4. POLY_PROXY hay EOA ─────────────────────────────────────────
            if self.cfg.funder:
                sel_ex  = keccak(text="execute(address,uint256,bytes)")[:4]
                tx_data = sel_ex + _enc(["address", "uint256", "bytes"], [_CTF, 0, rdm_data])
                tx_to   = self.cfg.funder
                self.log.info("[REDEEM] Gọi qua POLY_PROXY %s...", self.cfg.funder[:12])
            else:
                tx_data = rdm_data
                tx_to   = _CTF
                self.log.info("[REDEEM] Gọi trực tiếp từ EOA")

            # ── 5. Lấy nonce + gas price ──────────────────────────────────────
            nonce   = int(_rpc("eth_getTransactionCount",
                               [self._eoa_address, "pending"])["result"], 16)
            gprice  = int(int(_rpc("eth_gasPrice", [])["result"], 16) * 1.5)

            tx = {
                "to":       tx_to,
                "value":    0,
                "data":     "0x" + tx_data.hex(),
                "nonce":    nonce,
                "gas":      300_000,
                "gasPrice": gprice,
                "chainId":  137,
            }
            signed  = Account.sign_transaction(tx, private_key=self.cfg.private_key)
            res     = _rpc("eth_sendRawTransaction",
                           ["0x" + signed.raw_transaction.hex()])
            tx_hash = res.get("result")
            if not tx_hash:
                self.log.error("[REDEEM] Gửi tx thất bại: %s", res.get("error", "?"))
                return False

            self.log.info("[REDEEM] tx: %s", tx_hash)

            # ── 6. Chờ receipt (tối đa 90s) ───────────────────────────────────
            for _ in range(30):
                time.sleep(3)
                receipt = _rpc("eth_getTransactionReceipt", [tx_hash]).get("result")
                if receipt:
                    ok = int(receipt.get("status", "0x0"), 16) == 1
                    self.log.info("[REDEEM] %s  tx=%s...",
                                  "✓ Thành công" if ok else "✗ Revert", tx_hash[:20])
                    return ok
            self.log.warning("[REDEEM] Timeout chờ receipt tx=%s", tx_hash[:20])
            return False

        except Exception as exc:
            self.log.error("[REDEEM] Lỗi: %s", exc)
            return False

    def get_btc_open_binance(self, round_start_ts: int) -> Optional[float]:
        """5m candle open price tại đầu round. Retry tối đa 10 lần (candle có thể chưa xuất hiện ngay)."""
        for attempt in range(10):
            try:
                r = requests.get(
                    "https://fapi.binance.com/fapi/v1/klines",
                    params={"symbol": "BTCUSDT", "interval": "5m",
                            "startTime": round_start_ts * 1000, "limit": 1},
                    timeout=5,
                )
                data = r.json()
                if data and isinstance(data, list) and len(data) > 0:
                    open_time_ms = int(data[0][0])
                    # Xác nhận candle đúng round (chênh lệch < 60s)
                    if abs(open_time_ms / 1000 - round_start_ts) < 60:
                        price = float(data[0][1])
                        self.log.debug("[SNIPE] btc_open attempt %d: %.2f", attempt + 1, price)
                        return price
            except Exception as e:
                self.log.debug("[SNIPE] get_btc_open_binance attempt %d loi: %s", attempt + 1, e)
            time.sleep(1)
        self.log.warning("[SNIPE] Khong lay duoc btc_open sau 10 lan thu")
        return None

    def get_btc_tick_binance(self) -> Optional[float]:
        """Latest tick price — gọi mỗi 200ms trong 10s cuối round."""
        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=2,
            )
            return float(r.json()["price"])
        except Exception as e:
            self.log.debug("[SNIPE] get_btc_tick_binance lỗi: %s", e)
            return None

    def place_buy_limit(self, token_id: str, price: float, size: int) -> dict:
        """Đặt lệnh LIMIT BUY GTC. Raises nếu API trả lỗi."""
        from py_clob_client_v2.clob_types import OrderArgs, OrderType

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="BUY",
        )
        return self._client.create_and_post_order(order_args, order_type=OrderType.GTC)

