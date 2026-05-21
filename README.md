# Polymarket BTC Up/Down 5m Bot

Bot tự động trade thị trường **Bitcoin Up or Down - 5m** trên Polymarket.

## Chiến lược

Vào lệnh **BUY** khi thỏa mãn **đồng thời** 3 điều kiện:

| # | Điều kiện | Giá trị mặc định |
|---|-----------|-----------------|
| 1 | Best ask của UP **hoặc** DOWN == `TARGET_PRICE` | `0.99` (99¢) |
| 2 | Tổng shares tại mức giá đó `<` `MAX_SHARES_AT_PRICE` | `2500` |
| 3 | Thời gian còn lại của round `<` `TIME_WINDOW_SECONDS` | `90s` |

Chỉ đặt **1 lệnh mỗi round**. Sang round mới thì reset.

## Cài đặt

```bash
# 1. Tạo môi trường ảo
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2. Cài thư viện
pip install -r requirements.txt

# 3. Tạo .env từ template
copy .env.example .env
# Điền PRIVATE_KEY vào .env
```

> **Yêu cầu:** Ví Polygon phải có USDC và đã approve Polymarket CLOB contract tại
> `https://polymarket.com` (đăng nhập 1 lần bằng ví là đủ).

## Cấu hình `.env`

```env
PRIVATE_KEY=0x<private_key_của_bạn>

# Lần đầu để trống — bot tự tạo và in ra
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

TARGET_PRICE=0.99
MAX_SHARES_AT_PRICE=2500
ORDER_SIZE=100
TIME_WINDOW_SECONDS=90
DRY_RUN=true
```

## Chạy bot

### Bước 1 — Test DRY_RUN (không tốn tiền)

```bash
python main.py
```

Bot sẽ tự tạo API key lần đầu và in ra — **copy 3 dòng đó vào `.env`** rồi restart:

```
[AUTH] Đã tạo API key mới. Thêm vào .env rồi restart:
  POLYMARKET_API_KEY=xxxx
  POLYMARKET_API_SECRET=xxxx
  POLYMARKET_API_PASSPHRASE=xxxx
```

### Bước 2 — Xem log DRY_RUN

Khi đủ điều kiện, bot in:
```
[DRY RUN] >>> SẼ ĐẶT LỆNH: BUY 100 shares @ 0.99 | token=0x1a2b3c... | UP
```

### Bước 3 — Chạy thật

Sau khi đã xác nhận logic đúng, đổi trong `.env`:
```
DRY_RUN=false
```
rồi chạy lại `python main.py`.

## Log mẫu

```
14:02:01 [INFO ] [ROUND] Tìm market BTC Up/Down 5m đang active...
14:02:02 [INFO ] [ROUND] Will BTC be higher or lower in 5 minutes?
14:02:02 [INFO ] [ROUND] Kết thúc: 14:05:00 UTC
14:02:02 [INFO ] [OB] Fetch REST orderbook (UP)...
14:02:02 [INFO ] [OB-UP] Top 5 asks: 0.96×500 | 0.97×800 | 0.98×1200 | 0.99×400 | 1.00×9999
14:02:03 [INFO ] [WS] Connected & subscribed: ['0x1a2b...', '0x3c4d...']
14:04:28 [INFO ] [  UP] ask=0.99  | shares@99=2100   | còn=32s  | [✓ ✓ ✓] TẤT CẢ ĐỦ ĐIỀU KIỆN
14:04:28 [INFO ] [DRY RUN] >>> SẼ ĐẶT LỆNH: BUY 100 shares @ 0.99 | token=0x1a2b... | UP
```

## Cấu trúc code

```
pbot-9/
├── main.py          ← vòng lặp chính
├── config.py        ← đọc .env
├── polymarket.py    ← CLOB client, orderbook, WS, đặt lệnh
├── strategy.py      ← kiểm tra 3 điều kiện
├── logger.py        ← log console + file
├── requirements.txt
├── .env             ← config của bạn (không commit)
├── .env.example     ← template
└── logs/            ← file log theo ngày
```

## Rủi ro cần biết

- **Edge mỏng**: mua 99¢, lãi tối đa 1¢/share nếu thắng → cần volume đủ lớn
- **Market rotation**: round mới tạo mỗi 5 phút, bot tự detect nhưng có thể miss vài giây
- **Slippage**: lệnh limit có thể không khớp nếu giá dịch chuyển nhanh
- **API thay đổi**: Polymarket có thể cập nhật WS format bất kỳ lúc nào
