# -*- coding: utf-8 -*-
"""Binance Observer 3.0 — educational market research; no trading."""
import concurrent.futures
from collections import deque
import os
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://fapi.binance.com"
SCAN_SECONDS = 900
SUMMARY_SECONDS = 3600
MIN_VOLUME = 10_000_000
WORKERS = 6
MAX_SUMMARY = 5
MAX_IMPULSE_ALERTS = 3
EVENT_COOLDOWN = 12 * 3600
ALERT_COOLDOWN = 6 * 3600
DB = Path(os.getenv("OBSERVER_DB_PATH", str(Path(__file__).with_name("observer_history_v30.sqlite3"))))


def fetch(path, params=None):
    response = requests.get(BASE + path, params=params, timeout=18)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "code" in data:
        raise RuntimeError(str(data))
    return data


def telegram(token, chat_id, message):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=18,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))


def percent(current, previous):
    return (current / previous - 1) * 100 if previous else 0.0


def rsi(values, n=14):
    if len(values) < n + 1:
        return 50.0
    diffs = [b - a for a, b in zip(values[-n-1:-1], values[-n:])]
    gain = sum(max(0, d) for d in diffs) / n
    loss = sum(max(0, -d) for d in diffs) / n
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100 - 100 / (1 + gain / loss)


def ema(values, n=20):
    alpha = 2 / (n + 1)
    value = values[0]
    for close in values[1:]:
        value = close * alpha + value * (1 - alpha)
    return value


def indicators(closes, highs, lows, volumes):
    """Indicators on closed hourly candles only. Descriptive, not predictive."""
    e12, e26 = closes[0], closes[0]
    macd_series = []
    for c in closes:
        e12 += (c - e12) * (2 / 13)
        e26 += (c - e26) * (2 / 27)
        macd_series.append(e12 - e26)
    signal = macd_series[0]
    histogram = []
    for m in macd_series:
        signal += (m - signal) * (2 / 10)
        histogram.append(m - signal)
    window = closes[-20:]
    mean = sum(window) / len(window)
    std = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window))
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
              abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
    atr = sum(tr[-14:]) / min(14, len(tr))
    baseline = sum(volumes[-21:-1]) / 20
    return dict(
        ema20=ema(closes, 20), ema50=ema(closes, 50), ema200=ema(closes, 200),
        macd_hist=histogram[-1], macd_hist_prev=histogram[-2],
        bb_upper=mean + 2 * std, bb_lower=mean - 2 * std,
        atr_pct=100 * atr / closes[-1] if closes[-1] else 0,
        rvol=volumes[-1] / baseline if baseline else 0,
    )


def strongest_rise(bars, count):
    """Largest low-to-later-high move within last `count` closed bars."""
    window = bars[-count:]
    lowest = float(window[0][3])
    best = 0.0
    peak_time = int(window[0][0])
    for bar in window[1:]:
        high = float(bar[2])
        rise = percent(high, lowest)
        if rise > best:
            best = rise
            peak_time = int(bar[0])
        lowest = min(lowest, float(bar[3]))
    return best, peak_time


def movement_phase(g1, g4, d24, rsi_down, below_ema, lower_highs):
    if d24 >= 10 and g1 < 0 and (below_ema or lower_highs):
        return "откат"
    if d24 >= 10 and g1 > 0 and not rsi_down:
        return "восстановление"
    if g1 > 0 and g4 > 0 and d24 <= 8 and not rsi_down:
        return "ускорение"
    if rsi_down or lower_highs:
        return "замедление"
    return "неопределённая фаза"


def analyze(symbol, volume):
    try:
        # 700 candles, including the unfinished one, cover 174.75 hours.
        bars = fetch("/fapi/v1/klines", {"symbol": symbol, "interval": "15m", "limit": 700})
        if len(bars) < 675:
            return None
        bars = bars[:-1]  # only closed candles
        close = [float(x[4]) for x in bars]
        high = [float(x[2]) for x in bars]
        low = [float(x[3]) for x in bars]
        vol = [float(x[5]) for x in bars]
        last = close[-1]

        def growth(n):
            return percent(last, close[-n-1])

        def retreat(n):
            top = max(high[-n:])
            return (top - last) / top * 100 if top else 0

        g15, g1, g4, g24, g3, g7 = (
            growth(n) for n in (1, 4, 16, 96, 288, 672)
        )
        d24, d3, d7 = (retreat(n) for n in (96, 288, 672))
        # Historical impulse remains visible even after a price pullback.
        hist4, peak4 = strongest_rise(bars, 16)
        hist12, peak12 = strongest_rise(bars, 48)
        hist24, peak24 = strongest_rise(bars, 96)
        impulse = []
        for name, value, threshold in (
            ("15м", g15, 20), ("1ч", g1, 20),
            ("4ч", g4, 40), ("24ч", g24, 100)
        ):
            if value >= threshold:
                impulse.append(f"{name} +{value:.1f}%")

        # Derive closed hourly candles from closed 15m bars,
        # keeping only complete clock-hour groups.
        hourly = {}
        for bar in bars:
            hour = int(bar[0]) // 3_600_000
            hourly.setdefault(hour, []).append(bar)
        hours = [hourly[k] for k in sorted(hourly) if len(hourly[k]) == 4]
        if len(hours) < 25:
            return None
        hc = [float(group[-1][4]) for group in hours]
        hh = [max(float(x[2]) for x in group) for group in hours]
        hl = [min(float(x[3]) for x in group) for group in hours]
        hv = [sum(float(x[5]) for x in group) for group in hours]
        extra_ind = indicators(hc, hh, hl, hv)
        current_rsi = rsi(hc)
        previous_rsi = rsi(hc[:-1])
        rsi_down = current_rsi < previous_rsi - 3
        candle_down = hc[-1] < hc[-2]
        below_ema = hc[-1] < ema(hc[-40:])
        lower_highs = hh[-1] < hh[-2] < hh[-3]
        volume_spike = hv[-1] > 2 * (sum(hv[-21:-1]) / 20)
        # Upper wick of the last closed hourly candle.
        last_hour = hours[-1]
        hour_open = float(last_hour[0][1])
        hour_close = float(last_hour[-1][4])
        hour_high = max(float(x[2]) for x in last_hour)
        hour_low = min(float(x[3]) for x in last_hour)
        wick_ratio = (
            (hour_high - max(hour_open, hour_close)) / (hour_high - hour_low)
            if hour_high > hour_low else 0
        )
        upper_wick = wick_ratio >= 0.45

        evidence = []
        if rsi_down:
            evidence.append("RSI снижается")
        if candle_down:
            evidence.append("часовая свеча вниз")
        if below_ema:
            evidence.append("ниже EMA20")
        if lower_highs:
            evidence.append("максимумы снижаются")
        # Volume spike and upper wick are context, not independent reversal proof.
        context = []
        if volume_spike:
            context.append("повышенный часовой объём")
        if upper_wick:
            context.append("длинная верхняя тень")

        phase = movement_phase(g1, g4, d24, rsi_down, below_ema, lower_highs)
        if d24 >= 35:
            phase = "глубокий откат" + ("; краткосрочное восстановление" if g1 > 0 else "")
        eligible = g24 >= 20 or g3 >= 50 or g7 >= 100 or hist4 >= 40 or hist24 >= 100
        if d24 <= 8 and (g3 >= 50 or g7 >= 100):
            group = "A"
        elif d24 >= 10 and (g3 >= 50 or g7 >= 100):
            group = "B"
        else:
            group = "C"
        score = len(evidence) + (1 if g3 >= 50 else 0) + (1 if g7 >= 100 else 0)
        return dict(
            symbol=symbol, volume=volume, price=last,
            g15=g15, g1=g1, g4=g4, g24=g24, g3=g3, g7=g7,
            d24=d24, d3=d3, d7=d7, rsi=current_rsi,
            old_rsi=previous_rsi, group=group, score=score,
            impulse=impulse, evidence=evidence, context=context,
            eligible=eligible, candle_time=int(bars[-1][0]),
            hist4=hist4, hist12=hist12, hist24=hist24,
            peak4=peak4, peak12=peak12, peak24=peak24,
            phase=phase, **extra_ind,
        )
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def init_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        symbol TEXT NOT NULL, candle_time INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, price REAL NOT NULL,
        g15 REAL, g1 REAL, g4 REAL, g24 REAL, g3 REAL, g7 REAL,
        d24 REAL, rsi REAL, evidence INTEGER, impulse TEXT,
        PRIMARY KEY(symbol, candle_time)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        symbol TEXT PRIMARY KEY, last_sent REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        symbol TEXT NOT NULL, event_time INTEGER NOT NULL,
        detected_at INTEGER NOT NULL, base_price REAL NOT NULL,
        event_type TEXT NOT NULL, rise REAL NOT NULL,
        PRIMARY KEY(symbol, event_time, event_type)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        symbol TEXT NOT NULL, event_time INTEGER NOT NULL,
        event_type TEXT NOT NULL, hours INTEGER NOT NULL,
        observed_at INTEGER NOT NULL, change_pct REAL NOT NULL,
        PRIMARY KEY(symbol, event_time, event_type, hours)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS indicators (
        symbol TEXT NOT NULL, candle_time INTEGER NOT NULL,
        macd_hist REAL, rvol REAL, atr_pct REAL, ema20 REAL, ema50 REAL,
        ema200 REAL, bb_upper REAL, bb_lower REAL,
        PRIMARY KEY(symbol, candle_time))""")
    conn.commit()


def get_setting(conn, key, default=0):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return float(row[0]) if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
        (key, str(value))
    )
    conn.commit()


def record(conn, items):
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """INSERT OR IGNORE INTO observations
        (symbol,candle_time,recorded_at,price,g15,g1,g4,g24,g3,g7,d24,rsi,evidence,impulse)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (x["symbol"], x["candle_time"], now, x["price"],
             x["g15"], x["g1"], x["g4"], x["g24"], x["g3"], x["g7"],
             x["d24"], x["rsi"], len(x["evidence"]), ", ".join(x["impulse"]))
            for x in items
        ]
    )
    conn.commit()


def record_indicators(conn, items):
    conn.executemany("""INSERT OR IGNORE INTO indicators
        (symbol,candle_time,macd_hist,rvol,atr_pct,ema20,ema50,ema200,bb_upper,bb_lower)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", [
        (x["symbol"],x["candle_time"],x["macd_hist"],x["rvol"],x["atr_pct"],
         x["ema20"],x["ema50"],x["ema200"],x["bb_upper"],x["bb_lower"])
        for x in items])
    conn.commit()


def record_events_and_outcomes(conn, items):
    # Register past 4-hour impulse events. 12h and 24h are context,
    # not separate duplicate events. Price at first detection is the baseline.
    now_ms = int(time.time() * 1000)
    for x in items:
        if x["hist4"] >= 40:
            conn.execute(
                """INSERT OR IGNORE INTO events
                (symbol,event_time,detected_at,base_price,event_type,rise)
                VALUES (?,?,?,?,?,?)""",
                (x["symbol"], x["peak4"], now_ms, x["price"],
                 "4h_impulse", x["hist4"]),
            )
    conn.commit()
    prices = {x["symbol"]: x for x in items}
    pending = conn.execute("""
        SELECT e.symbol,e.event_time,e.detected_at,e.base_price,e.event_type,h.hours
        FROM events e CROSS JOIN
        (SELECT 1 AS hours UNION ALL SELECT 4 UNION ALL SELECT 12 UNION ALL SELECT 24) h
        LEFT JOIN outcomes o ON o.symbol=e.symbol
            AND o.event_time=e.event_time AND o.event_type=e.event_type
            AND o.hours=h.hours
        WHERE o.symbol IS NULL
    """).fetchall()
    for sym, event_time, detected, base, event_type, hours_after in pending:
        x = prices.get(sym)
        # Horizons measured after detection, not after the earlier peak.
        if x and x["candle_time"] >= detected + hours_after * 3600 * 1000:
            conn.execute(
                """INSERT OR IGNORE INTO outcomes
                (symbol,event_time,event_type,hours,observed_at,change_pct)
                VALUES (?,?,?,?,?,?)""",
                (sym, event_time, event_type, hours_after,
                 x["candle_time"], percent(x["price"], base)),
            )
    conn.commit()


def research_line(x):
    m = "ослабевает" if x["macd_hist"] < x["macd_hist_prev"] else "усиливается"
    bb = "выше верхней" if x["price"] > x["bb_upper"] else ("ниже нижней" if x["price"] < x["bb_lower"] else "внутри полос")
    return (f'MACD: {m} | RVOL: {x["rvol"]:.2f}x | ATR(1ч): {x["atr_pct"]:.1f}%\n'
            f'EMA20/50/200: {x["ema20"]:.6g} / {x["ema50"]:.6g} / {x["ema200"]:.6g}\n'
            f'Bollinger: {bb} | OI: {x.get("open_interest", "не запрашивался")}')


def line(x):
    signals = ", ".join(x["evidence"]) or "нет подтверждающих признаков"
    extra = ", ".join(x["context"]) or "без дополнительных особенностей"
    return (
        f'{x["symbol"]} | группа {x["group"]}\n'
        f'15м {x["g15"]:+.1f}% | 1ч {x["g1"]:+.1f}% | 4ч {x["g4"]:+.1f}%\n'
        f'24ч {x["g24"]:+.1f}% | 3д {x["g3"]:+.1f}% | 7д {x["g7"]:+.1f}%\n'
        f'Откат: 24ч {x["d24"]:.1f}% | 3д {x["d3"]:.1f}% | 7д {x["d7"]:.1f}%\n'
        f'Исторический импульс: 4ч +{x["hist4"]:.1f}% | '
        f'12ч +{x["hist12"]:.1f}% | 24ч +{x["hist24"]:.1f}%\n'
        f'Фаза: {x["phase"]}\n'
        f'RSI 1ч {x["rsi"]:.1f} (было {x["old_rsi"]:.1f})\n' 
        f'Ослабление: {len(x["evidence"])}/4 — {signals}\n'
        f'Контекст: {extra}\n'
        f'Оборот 24ч: {x["volume"]:,.0f} USDT\n'
        + research_line(x)
    )


def run():
    """Single scan for GitHub Actions; no persistent state between runs."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions secrets")
    info = fetch("/fapi/v1/exchangeInfo")
    tickers = {x["symbol"]: x for x in fetch("/fapi/v1/ticker/24hr")}
    symbols = []
    for item in info["symbols"]:
        sym = item["symbol"]
        t = tickers.get(sym)
        if (item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
                and t is not None
                and float(t.get("quoteVolume", 0)) >= MIN_VOLUME):
            symbols.append((sym, float(t["quoteVolume"])))
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda pair: analyze(*pair), symbols))
    good = [x for x in results if x and "error" not in x]
    errors = [x for x in results if x and "error" in x]
    candidates = [x for x in good if x["eligible"]]
    rank = sorted(candidates, key=lambda x: (
        {"B": 0, "A": 1, "C": 2}[x["group"]],
        -len(x["evidence"]), -x["score"]
    ))[:MAX_SUMMARY]
    message = (
        "🔎 BINANCE OBSERVER 3.0 — обзор рынка\n"
        + datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
        + f"\nПроверено: {len(good)} | Ошибок: {len(errors)}\n\n"
        + ("\n\n".join(line(x) for x in rank) if rank
           else "Монет по заданным фильтрам не найдено.")
        + "\n\nЭто статистические наблюдения, не прогноз и не торговый сигнал."
    )
    telegram(token, chat_id, message[:4000])
    print(f"Scanned {len(good)} symbols; candidates {len(candidates)}; errors {len(errors)}")
    if errors:
        print("First error:", errors[0]["symbol"], errors[0]["error"])


if __name__ == "__main__":
    run()
