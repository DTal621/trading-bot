import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json",
}


def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def place_order(symbol, qty, side, order_type="market", time_in_force="day", limit_price=None):
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if limit_price:
        payload["limit_price"] = str(limit_price)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def get_orders(status="open"):
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params={"status": status})
    r.raise_for_status()
    return r.json()


def cancel_order(order_id):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    r.raise_for_status()
    return r.status_code


if __name__ == "__main__":
    acct = get_account()
    print(f"Account: {acct['account_number']}")
    print(f"Portfolio value: ${float(acct['portfolio_value']):,.2f}")
    print(f"Buying power:    ${float(acct['buying_power']):,.2f}")
    print(f"Cash:            ${float(acct['cash']):,.2f}")
