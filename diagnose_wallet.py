"""
Chạy script này để kiểm tra xem POLY_PROXY address của ví mới có đúng không.
  python diagnose_wallet.py
Thay NEW_PRIVATE_KEY + NEW_FUNDER bên dưới.
"""

import os, sys, json, requests
from dotenv import load_dotenv

load_dotenv()

# ── Lấy từ .env hoặc sửa trực tiếp ──────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER      = os.getenv("POLYMARKET_FUNDER", "")
API_KEY     = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET  = os.getenv("POLYMARKET_API_SECRET", "")
API_PASS    = os.getenv("POLYMARKET_API_PASSPHRASE", "")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137

print("=" * 70)
print("DIAGNOSE WALLET")
print("=" * 70)
print(f"  PRIVATE_KEY  = {PRIVATE_KEY[:10]}...{PRIVATE_KEY[-4:]}")
print(f"  FUNDER (env) = {FUNDER or '(trống)'}")
print()

# ── 1. Tính EOA và deposit wallet từ private key ──────────────────────────────
print("── 1. Tính địa chỉ từ private key ──")
try:
    from py_clob_client_v2.signer import Signer as _Signer
    eoa = _Signer(PRIVATE_KEY, CHAIN_ID).address()
    print(f"  EOA = {eoa}")
except Exception as e:
    print(f"  [LỖI] không tính được EOA: {e}")
    eoa = ""

try:
    from py_builder_relayer_client.builder.derive import derive
    from py_builder_relayer_client.config import get_contract_config as get_relayer_cfg
    factory = get_relayer_cfg(CHAIN_ID).safe_factory
    deposit_wallet = derive(eoa, factory)
    print(f"  DepositWallet (CREATE2) = {deposit_wallet}")
    print(f"  FUNDER env              = {FUNDER or '(trống)'}")
    if FUNDER and FUNDER.lower() != deposit_wallet.lower():
        print(f"  [!] FUNDER KHÔNG KHỚP DepositWallet — đây có thể là root cause!")
    elif FUNDER and FUNDER.lower() == deposit_wallet.lower():
        print(f"  [OK] FUNDER khớp DepositWallet")
    else:
        print(f"  [WARN] FUNDER trống trong .env")
except Exception as e:
    print(f"  [LỖI] không tính được deposit wallet: {e}")
    deposit_wallet = ""

print()

# ── 2. Thử tạo / derive API key ──────────────────────────────────────────────
print("── 2. Thử API key authentication ──")
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    if API_KEY:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)
        print(f"  Dùng API key từ .env: {API_KEY[:20]}...")
    else:
        print("  API key trống, đang derive từ private key...")
        client_l1 = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY, signature_type=0)
        creds = client_l1.create_or_derive_api_key()
        print(f"  Derived API key = {creds.api_key}")

    # Thử sig_type=1 (POLY_PROXY) với funder
    effective_funder = FUNDER or deposit_wallet
    sig_type = 1 if effective_funder else 0
    print(f"  sig_type = {sig_type}, funder = {effective_funder or 'EOA'}")

    client = ClobClient(
        host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
        creds=creds, signature_type=sig_type,
        funder=effective_funder or None,
    )
    print(f"  [OK] ClobClient tạo thành công")
except Exception as e:
    print(f"  [LỖI] ClobClient: {e}")
    sys.exit(1)

print()

# ── 3. Lấy balance ─────────────────────────────────────────────────────────────
print("── 3. Get balance ──")
try:
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Raw response: {json.dumps(resp, indent=2)}")
    raw = resp.get("balance") or "0"
    usdc = int(raw) / 1e6
    print(f"  USDC balance = ${usdc:.2f}")
except Exception as e:
    print(f"  [LỖI] get_balance_allowance: {e}")

print()

# ── 4. Thử sig_type=0 (EOA) nếu sig_type=1 không có balance ─────────────────
if FUNDER:
    print("── 4. Thử sig_type=0 (EOA, bỏ qua funder) ──")
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        c0 = ClobClient(
            host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
            creds=creds, signature_type=0,
        )
        resp0 = c0.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print(f"  Raw response (sig_type=0): {json.dumps(resp0, indent=2)}")
        raw0 = resp0.get("balance") or "0"
        usdc0 = int(raw0) / 1e6
        print(f"  USDC balance (sig_type=0) = ${usdc0:.2f}")
    except Exception as e:
        print(f"  [LỖI] sig_type=0: {e}")
    print()

# ── 5. Thử deposit_wallet làm funder (nếu FUNDER khác DepositWallet) ─────────
if deposit_wallet and FUNDER and FUNDER.lower() != deposit_wallet.lower():
    print(f"── 5. Thử lại với FUNDER = deposit_wallet ({deposit_wallet[:12]}...) ──")
    try:
        c2 = ClobClient(
            host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
            creds=creds, signature_type=1,
            funder=deposit_wallet,
        )
        resp2 = c2.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print(f"  Raw response: {json.dumps(resp2, indent=2)}")
        raw2 = resp2.get("balance") or "0"
        usdc2 = int(raw2) / 1e6
        print(f"  USDC balance (funder=deposit_wallet) = ${usdc2:.2f}")
        if usdc2 > 0:
            print(f"\n  *** FUNDER ĐÚNGlà: {deposit_wallet}")
            print(f"  *** Hãy đặt POLYMARKET_FUNDER={deposit_wallet} trong .env!")
    except Exception as e:
        print(f"  [LỖI]: {e}")
    print()

# ── 6. Check via direct REST (không qua SDK) ─────────────────────────────────
print("── 6. Direct REST balance check (debug) ──")
try:
    import time, hmac, hashlib, base64
    ts      = str(int(time.time()))
    method  = "GET"
    path    = "/balance-allowance"
    qs      = "asset_type=COLLATERAL"
    msg     = ts + method + path + "?" + qs

    secret_bytes = base64.urlsafe_b64decode(creds.api_secret + "==")
    sig = base64.b64encode(
        hmac.new(secret_bytes, msg.encode(), hashlib.sha256).digest()
    ).decode()

    headers = {
        "POLY-API-KEY":         creds.api_key,
        "POLY-TIMESTAMP":       ts,
        "POLY-SIGNATURE":       sig,
        "POLY-PASSPHRASE":      creds.api_passphrase,
    }
    r = requests.get(
        f"{CLOB_HOST}/balance-allowance",
        params={"asset_type": "COLLATERAL"},
        headers=headers,
        timeout=8,
    )
    print(f"  HTTP {r.status_code}: {r.text[:300]}")
except Exception as e:
    print(f"  [LỖI] direct REST: {e}")

print()
print("=" * 70)
print("XONG. Dựa vào output trên để biết FUNDER đúng và balance thực tế.")
print("=" * 70)
