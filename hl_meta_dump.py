import json
import os
from hyperliquid.utils import constants
from hyperliquid.info import Info

# Initialize public info client
info = Info(constants.MAINNET_API_URL)

# Coins to inspect — you can pull these directly from your environment
COINS = [
    "ETH", "BTC", "SOL", "LINK", "BNB", "AVAX", "BIO", "PAXG", "STBL",
    "SNX", "TAO", "ZORA", "ETHFI", "MNT", "CRV", "ZEREBRO"
]

def main():
    meta = info.meta()
    coin_meta = {c["name"]: c for c in meta["universe"] if c["name"] in COINS}

    result = {}
    print("\n=== Hyperliquid Tick / Size Info ===")
    for coin in COINS:
        data = coin_meta.get(coin)
        if not data:
            print(f"{coin}: ⚠️ Not found")
            continue
        px_tick = data["szDecimals"]  # backup field sometimes used
        price_tick = data.get("priceTick", "unknown")
        size_step = data.get("szStep", "unknown")
        min_sz = data.get("minSz", "unknown")

        result[coin] = {
            "price_tick": price_tick,
            "size_step": size_step,
            "min_sz": min_sz,
        }

        print(f"{coin}: price_tick={price_tick}, size_step={size_step}, min_sz={min_sz}")

    print("\nJSON for environment override (copy this):\n")
    overrides = ",".join(f"{k}={v['price_tick']}" for k,v in result.items() if v["price_tick"] != "unknown")
    print(f"HYPER_PX_TICK_OVERRIDES=\"{overrides}\"")

    print("\nFull data:")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
