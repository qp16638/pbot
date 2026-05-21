# Polymarket BTC Up/Down 5m Bot

## Mô tả
Bot tự động đặt lệnh BUY limit trên Polymarket cho market "Bitcoin Up or Down 5m".
Chiến lược: khi ask price == 0.99 (99¢), supply thấp, còn đủ thời gian → mua 100 shares với kỳ vọng thắng $1.00.

## Stack
- Python 3.11+
- `py-clob-client-v2` — Polymarket CLOB v2 SDK (KHÔNG dùng `py-clob-client` v1)
- `websocket-client` — real-time orderbook feed
- `Flask` — localhost dashboard tại http://localhost:5050
- `python-dotenv` — config từ `.env`
- `eth-abi`, `eth-account` — ký và gửi tx on-chain (redeem winnings)

## Cấu trúc file
```
main.py          # Vòng lặp chính: tìm market → subscribe orderbook → check → đặt lệnh
config.py        # Đọc .env → Config dataclass
strategy.py      # Logic check 4 điều kiện vào lệnh (pure function)
polymarket.py    # PolymarketClient: CLOB API, WebSocket, order placement, redeem
dashboard.py     # Flask dashboard (background thread)
dashboard.html   # UI dashboard — edit file này + F5 browser để update UI không cần restart bot
logger.py        # Setup logging
diagnose_wallet.py  # Script chẩn đoán ví / balance (chạy độc lập)
```

## Kiến trúc Polymarket CLOB v2

### Auth / Wallet
- **EOA**: địa chỉ ví Ethereum (từ private key)
- **FUNDER (proxy)**: contract proxy của Polymarket, lấy từ **Polymarket.com → Profile → API section**
  - KHÔNG tính từ CREATE2 (factory v1 cho kết quả sai với v2)
  - Đây là địa chỉ thực sự nơi USDC được held
- **signature_type=1** (POLY_PROXY) khi có FUNDER, **signature_type=0** (EOA) khi không có
- API key derive từ private key qua `create_or_derive_api_key()` — lỗi 400 "Could not create" là bình thường, key vẫn được derive thành công

### CLOB v2 SDK usage
```python
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams, AssetType

# Init client
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,  # Polygon mainnet
    key=private_key,
    creds=ApiCreds(api_key=..., api_secret=..., api_passphrase=...),
    signature_type=1,   # POLY_PROXY
    funder=funder_address,
)

# Lấy balance (CLOB internal, không phải on-chain)
resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
usdc = int(resp["balance"]) / 1e6

# Đặt lệnh BUY limit GTC
order_args = OrderArgs(token_id=token_id, price=0.99, size=100.0, side="BUY")
result = client.create_and_post_order(order_args, order_type=OrderType.GTC)
order_id = result.get("orderID") or result.get("order_id") or result.get("id")
```

### Market data
- **Gamma API** (`https://gamma-api.polymarket.com`) — tìm market, lấy token IDs, end time
- Market slug pattern: `btc-updown-5m-{(unix_now // 300) * 300}`
- Token IDs: từ `market["clobTokenIds"]` + `market["outcomes"]` (JSON strings)
- Orderbook: REST `/book?token_id=...` hoặc WebSocket `wss://ws-subscriptions-clob.polymarket.com/ws/`

### Balance
- **LUÔN dùng CLOB API** `get_balance_allowance(COLLATERAL)` — đây là số dư thật để trade
- Không dùng on-chain RPC để check balance (chỉ xem USDC trên chain, không phản ánh CLOB balance)
- Polymarket deposit qua website bridge hỗ trợ nhiều chain (Solana, BSC...) về Polygon
- Polymarket dùng USDC native Polygon (`0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`) — không phải USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)

## Chiến lược vào lệnh (4 điều kiện)
```
C1: best ask == TARGET_PRICE (0.99)        # Không trade khi orderbook trống
C2: shares tại giá đó < MAX_SHARES_AT_PRICE (4000)
C3: 0 < thời gian còn lại < TIME_WINDOW_SECONDS (90s)
C4: bid tại giá target < MAX_BID_VOLUME (30000)   # Tránh bất thường
```
Tất cả 4 điều kiện → đặt lệnh BUY `ORDER_SIZE` shares @ `TARGET_PRICE`.

## Config (.env)
```env
PRIVATE_KEY=0x...           # Private key EOA
POLYMARKET_FUNDER=0x...     # Proxy address từ Polymarket profile → API (KHÔNG tính CREATE2)

POLYMARKET_API_KEY=...      # Derive tự động nếu để trống (restart sau khi có)
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

TARGET_PRICE=0.99
MAX_SHARES_AT_PRICE=4000
MAX_BID_VOLUME=30000
ORDER_SIZE=100
TIME_WINDOW_SECONDS=90
SKIP_BELOW_SECONDS=5        # Bỏ qua round nếu còn < Xs mà chưa đặt lệnh
DRY_RUN=false
PROFIT_LIMIT=100            # Dừng bot khi session P&L >= $X (0 = không giới hạn)
```

## Đổi ví — checklist
Khi thay ví mới, phải update **cả 3**:
1. `PRIVATE_KEY` = private key EOA mới
2. `POLYMARKET_FUNDER` = proxy address của ví đó (lấy từ Polymarket.com → Profile → API)
3. Xóa `POLYMARKET_API_KEY/SECRET/PASSPHRASE` → bot tự derive lại khi restart

Nếu chỉ thay 1-2 thứ → mismatch → 401 hoặc balance $0.

## Redeem winnings
`polymarket.py` có method `redeem_position(token_id)` — gọi on-chain `redeemPositions()` qua Polygon RPC.
- Dùng `eth_abi` + `eth_account` (không cần web3.py)
- Route qua proxy `execute(address,uint256,bytes)` nếu có FUNDER
- Hiện chưa được gọi tự động trong main loop (TODO)

## Dashboard
- URL: http://localhost:5050
- Edit `dashboard.html` + F5 browser để update UI **không cần restart bot**
- API endpoints: `/api/state` (200ms update), `/api/trades`

## Known issues / gotchas
- `py_builder_relayer_client.derive()` tính proxy địa chỉ sai cho CLOB v2 (dùng factory v1) — bỏ qua log `DepositWallet=`
- Lỗi 400 "Could not create api key" khi derive là **bình thường** — SDK fallback sang derive mode
- Lỗi 401 = API key không khớp EOA → xóa API key trong .env, restart để derive lại
- WebSocket đôi khi không kết nối → bot tự fallback sang REST polling mỗi 500ms
- `price_to_beat` luôn None vì question format "Bitcoin Up or Down - May 20, 9:25AM-9:30AM ET" không chứa giá $
