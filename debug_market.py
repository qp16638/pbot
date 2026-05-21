"""Debug: lấy token IDs từ BTC 5m market hiện tại."""
import requests, json, time

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

now  = int(time.time())
base = (now // 300) * 300
slug = f"btc-updown-5m-{base}"

print(f"=== Event: {slug} ===\n")

r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10)
events = r.json()
events = events if isinstance(events, list) else events.get("events", [])
if not events:
    print("Không tìm thấy event!")
    exit()

event = events[0]
print(f"title   : {event.get('title')}")
print(f"endDate : {event.get('endDate')}")
print(f"active  : {event.get('active')}")

# In toàn bộ markets[0] để xem token fields
markets_in_event = event.get("markets", [])
if markets_in_event:
    print(f"\n=== markets[0] đầy đủ ===\n")
    print(json.dumps(markets_in_event[0], indent=2))
else:
    print("\nKhông có markets bên trong event!")

# Thử CLOB lookup bằng conditionId
if markets_in_event:
    cid = markets_in_event[0].get("conditionId") or markets_in_event[0].get("condition_id")
    if cid:
        print(f"\n=== CLOB market by conditionId ===\n")
        try:
            r2 = requests.get(f"{CLOB}/markets/{cid}", timeout=10)
            print(json.dumps(r2.json(), indent=2)[:1500])
        except Exception as ex:
            print(f"Lỗi: {ex}")
