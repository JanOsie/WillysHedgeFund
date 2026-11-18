# Williams %R -> Telegram, wersja pod GitHub Actions (jeden przebieg na uruchomienie)
# Uruchamiane z crona co ~15 min. Stan (ostatnia strefa spółek, offset Telegrama) w state.json.
# Raport na żądanie: komenda /status w Telegramie (obsługiwana przy kolejnym przebiegu)
# albo ręczne uruchomienie workflow w GitHubie (tryb --report).

import os
import sys
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ------------------------- KONFIGURACJA -------------------------
PERIOD = 14              # okres Williams %R
PRE_OPEN_MIN = 30        # ile minut przed otwarciem wysłać raport poranny
OVERSOLD = -80
OVERBOUGHT = -20
STATE_FILE = "state.json"
MAX_COMMAND_AGE_S = 2 * 3600   # starsze komendy są ignorowane

# Rynki i spółki (tickery z Yahoo Finance) - edytuj wg potrzeb
MARKETS = {
    "USA": dict(tz="America/New_York", open=(9, 30), close=(16, 0),
                tickers=['SOFI', 'XYL', 'HOOD', 'NVDA', 'MU', 'TSM', 'GOOGL']),
}

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ------------------------- TELEGRAM -------------------------
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)


# ------------------------- DANE I WSKAŹNIK -------------------------
def get_daily(ticker: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
    df = df[["High", "Low", "Close"]].dropna()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    return df


def williams_r(high, low, close):
    if high == low:
        return float("nan")
    return -100 * (high - close) / (high - low)


def analyze(ticker: str, today: dt.date) -> dict:
    df = get_daily(ticker)
    has_live = df.index[-1].date() == today
    completed = df.iloc[:-1] if has_live else df

    w = completed.tail(PERIOD)
    wr_prev = williams_r(w.High.max(), w.Low.min(), w.Close.iloc[-1])

    base = completed.tail(PERIOD - 1)
    hh, ll = base.High.max(), base.Low.min()
    if has_live:
        live = df.iloc[-1]
        hh, ll = max(hh, live.High), min(ll, live.Low)
        price = float(live.Close)
        wr = williams_r(hh, ll, price)
    else:
        price = float(completed.Close.iloc[-1])
        wr = wr_prev

    rng = hh - ll
    p_os = hh + OVERSOLD / 100 * rng
    p_ob = hh + OVERBOUGHT / 100 * rng
    return dict(wr_prev=wr_prev, wr=wr, price=price, p_os=p_os, p_ob=p_ob, live=has_live)


def zone(wr: float) -> str:
    if wr < OVERSOLD:
        return "WYPRZEDANIE (< -80)"
    if wr > OVERBOUGHT:
        return "WYKUPIENIE (> -20)"
    return "STREFA NEUTRALNA (-80...-20)"


# ------------------------- STAN -------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("premarket", {})   # rynek -> data ostatniego raportu
    s.setdefault("zones", {})       # rynek -> {"date": ..., "z": {ticker: strefa}}
    return s


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ------------------------- RAPORTY -------------------------
def build_report(name: str, tickers: list, today: dt.date, title: str) -> str:
    lines = [f"📊 {name} – {title} ({today})", f"Williams %R({PERIOD})", ""]
    for t in tickers:
        try:
            a = analyze(t, today)
            src = "na żywo" if a["live"] else "zamknięcie"
            lines.append(
                f"{t}: %R = {a['wr']:.1f} | {zone(a['wr'])}\n"
                f"   cena ({src}): {a['price']:.2f}\n"
                f"   spadek poniżej {a['p_os']:.2f} -> %R < {OVERSOLD}\n"
                f"   wzrost powyżej {a['p_ob']:.2f} -> %R > {OVERBOUGHT}"
            )
        except Exception as e:
            lines.append(f"{t}: błąd danych ({e})")
    return "\n".join(lines)


def send_on_demand(args: list) -> None:
    """Raport na żądanie. Bez argumentów: wszystkie spółki z MARKETS.
    Z argumentami (np. /status NVDA TSLA): wskazane tickery."""
    if args:
        today = dt.datetime.now(dt.timezone.utc).date()
        send_telegram(build_report("Wybrane spółki", args, today, "raport na żądanie"))
        return
    for name, m in MARKETS.items():
        today = dt.datetime.now(ZoneInfo(m["tz"])).date()
        send_telegram(build_report(name, m["tickers"], today, "raport na żądanie"))


# ------------------------- KOMENDY TELEGRAMA -------------------------
def handle_commands(state: dict) -> None:
    params = {"timeout": 0}
    if "tg_offset" in state:
        params["offset"] = state["tg_offset"]
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params=params, timeout=15)
        updates = r.json().get("result", [])
    except Exception as e:
        print("getUpdates exception:", e)
        return

    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        # tylko wiadomości z Twojego czatu i nie starsze niż MAX_COMMAND_AGE_S
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        if time.time() - msg.get("date", 0) > MAX_COMMAND_AGE_S:
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = [a.upper() for a in parts[1:]]

        if cmd in ("/status", "/raport"):
            send_on_demand(args)
        elif cmd in ("/start", "/help"):
            send_telegram(
                "Komendy:\n"
                "/status – raport dla wszystkich spółek z listy\n"
                "/status NVDA TSLA – raport dla wskazanych tickerów\n"
                "Odpowiedź przychodzi przy najbliższym sprawdzeniu (do kilkunastu minut)."
            )


# ------------------------- SPRAWDZANIE W TRAKCIE SESJI -------------------------
def live_check(name: str, tickers: list, today: dt.date, state: dict) -> None:
    entry = state["zones"].get(name)
    if not entry or entry.get("date") != str(today):
        entry = {"date": str(today), "z": {}}
        state["zones"][name] = entry
    zones = entry["z"]

    for t in tickers:
        try:
            a = analyze(t, today)
            if not a["live"]:
                continue
            prev_zone = zones.setdefault(t, zone(a["wr_prev"]))
            cur_zone = zone(a["wr"])
            if cur_zone != prev_zone:
                send_telegram(
                    f"🔔 {t}: zmiana strefy Williams %R\n"
                    f"{prev_zone}\n   ->  {cur_zone}\n"
                    f"%R = {a['wr']:.1f} | cena: {a['price']:.2f}\n"
                    f"Progi (dla bieżącej świecy): < {a['p_os']:.2f} (-80), "
                    f"> {a['p_ob']:.2f} (-20)"
                )
                zones[t] = cur_zone
            print(f"{t}: %R={a['wr']:.1f} cena={a['price']:.2f} strefa={cur_zone}")
        except Exception as e:
            print(f"{t}: błąd ({e})")


# ------------------------- MAIN -------------------------
def main() -> None:
    if not TOKEN or not CHAT_ID:
        sys.exit("Brak TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")

    # Ręczne uruchomienie z GitHuba: od razu wyślij raport, niezależnie od godziny
    if "--report" in sys.argv:
        send_on_demand([])
        return

    state = load_state()

    # 1) obsłuż komendy wysłane do bota (np. /status)
    handle_commands(state)

    # 2) raport przed sesją i alerty w trakcie sesji
    for name, m in MARKETS.items():
        now = dt.datetime.now(ZoneInfo(m["tz"]))
        if now.weekday() >= 5:
            continue

        open_dt = now.replace(hour=m["open"][0], minute=m["open"][1], second=0, microsecond=0)
        close_dt = now.replace(hour=m["close"][0], minute=m["close"][1], second=0, microsecond=0)
        pre_dt = open_dt - dt.timedelta(minutes=PRE_OPEN_MIN)
        today = now.date()

        if pre_dt <= now < open_dt:
            if state["premarket"].get(name) != str(today):
                send_telegram(build_report(name, m["tickers"], today, "raport przed sesją"))
                state["premarket"][name] = str(today)
        elif open_dt <= now <= close_dt:
            live_check(name, m["tickers"], today, state)

    save_state(state)


if __name__ == "__main__":
    main()
