import os, json, asyncio, re, numpy as np, requests
from datetime import datetime, timezone, timedelta
import ccxt
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# === CONFIG ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8452633533:AAHitoH7BYaKC1lOzvETkURCraxC2N0DO8Y")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1754581939")
FRIEND_CHAT_ID = os.environ.get("FRIEND_CHAT_ID", "7362663542")
BROADCAST_IDS = [TELEGRAM_CHAT_ID, FRIEND_CHAT_ID]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
BASE_STAKE = 50.0
STATE_FILE = "martingale_state.json"
PREDICTIONS_FILE = "predictions_log.json"
SETTINGS_FILE = "settings.json"
JOURNAL_FILE = "trade_journal.json"

# === GROQ CLIENT ===
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"

# === SETTINGS ===
def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f: return json.load(f)
    except:
        return {
            "stake": 50.0,
            "confidence_filter": 60,
            "alert_mode": False,
            "alert_min_grade": "A+"
        }

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f: json.dump(settings, f, indent=2)

# === TRADE JOURNAL ===
def load_journal():
    try:
        with open(JOURNAL_FILE, "r") as f: return json.load(f)
    except: return []

def save_journal(journal):
    with open(JOURNAL_FILE, "w") as f: json.dump(journal, f, indent=2)

def log_journal_entry(window_ts, mode, lean, confidence, stake, grade, session, strategy):
    journal = load_journal()
    journal.append({
        "window_ts": window_ts,
        "mode": mode,
        "lean": lean,
        "confidence": confidence,
        "stake": stake,
        "grade": grade,
        "session": session,
        "strategy": strategy,
        "result": None,
        "pnl": None,
        "logged_at": datetime.now(timezone.utc).isoformat()
    })
    save_journal(journal)

# === TECHNICAL INDICATORS ===
def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum()/period
    down = -seed[seed < 0].sum()/period
    if down == 0:
        return 100.0
    rs = up/down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100./(1. + rs)
    for i in range(period, len(prices)):
        delta = deltas[i-1]
        if delta > 0: upval, downval = delta, 0.
        else: upval, downval = 0., -delta
        up = (up*(period-1) + upval)/period
        down = (down*(period-1) + downval)/period
        if down == 0:
            rsi[i] = 100.0
        else:
            rs = up/down
            rsi[i] = 100. - 100./(1. + rs)
    return rsi[-1]

def calculate_bollinger(prices, period=20):
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    return sma + (std * 2), sma - (std * 2)

def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = float(prices[0])
    for price in prices[1:]:
        ema = float(price) * k + ema * (1 - k)
    return ema

def calculate_macd(prices):
    macd_values = []
    for i in range(26, len(prices)):
        e12 = calculate_ema(prices[:i], 12)
        e26 = calculate_ema(prices[:i], 26)
        macd_values.append(e12 - e26)
    if len(macd_values) < 9:
        return 0, 0, 0
    macd_line = macd_values[-1]
    signal = calculate_ema(np.array(macd_values), 9)
    histogram = macd_line - signal
    return macd_line, signal, histogram

def calculate_stoch_rsi(prices, period=14):
    rsi_values = []
    for i in range(period, len(prices)):
        rsi_values.append(calculate_rsi(prices[max(0,i-period*2):i+1], period))
    if len(rsi_values) < period:
        return 50.0, 50.0
    recent_rsi = rsi_values[-period:]
    min_rsi = min(recent_rsi)
    max_rsi = max(recent_rsi)
    if max_rsi == min_rsi:
        return 50.0, 50.0
    stoch_k = ((rsi_values[-1] - min_rsi) / (max_rsi - min_rsi)) * 100
    return round(stoch_k, 2), round(stoch_k, 2)

def calculate_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return np.mean(trs[-period:])

def calculate_vwap(highs, lows, closes, volumes):
    typical_prices = (highs + lows + closes) / 3
    vwap = np.sum(typical_prices * volumes) / np.sum(volumes)
    return vwap

def calculate_support_resistance(highs, lows, period=20):
    return min(lows[-period:]), max(highs[-period:])

def calculate_volume_trend(volumes):
    avg_vol = np.mean(volumes[-20:])
    current_vol = volumes[-1]
    if current_vol > avg_vol * 1.5: return "ABOVE AVERAGE 🔥"
    elif current_vol < avg_vol * 0.7: return "BELOW AVERAGE ❄️"
    else: return "NORMAL 📊"

def detect_candle_pattern(opens, closes, highs, lows):
    o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
    po, pc = opens[-2], closes[-2]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    total_range = h - l
    if total_range == 0: return "DOJI ➖"
    if body / total_range < 0.1: return "DOJI ➖"
    if c > o and pc < po and c > po and o < pc: return "BULLISH ENGULFING 🟢"
    if c < o and pc > po and c < po and o > pc: return "BEARISH ENGULFING 🔴"
    if lower_wick > body * 2 and upper_wick < body * 0.5: return "HAMMER 🔨 (BULLISH)"
    if upper_wick > body * 2 and lower_wick < body * 0.5: return "SHOOTING STAR ⭐ (BEARISH)"
    if c > o: return "BULLISH CANDLE 🟢"
    return "BEARISH CANDLE 🔴"

def detect_structure(closes):
    recent = closes[-10:]
    highs = [max(recent[i:i+3]) for i in range(len(recent)-2)]
    lows = [min(recent[i:i+3]) for i in range(len(recent)-2)]
    if len(highs) < 2 or len(lows) < 2: return "RANGING ↔️"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]: return "BULLISH 📈 (HH/HL)"
    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]: return "BEARISH 📉 (LH/LL)"
    else: return "RANGING ↔️"

def detect_session():
    hour = datetime.now(timezone.utc).hour
    if 0 <= hour < 8: return "ASIAN 🌏"
    elif 8 <= hour < 12: return "LONDON 🇬🇧"
    elif 12 <= hour < 20: return "NEW YORK 🗽"
    else: return "OFF-HOURS 🌙"

def detect_zone(current_price, bb_upper, bb_lower):
    mid = (bb_upper + bb_lower) / 2
    if current_price > mid: return "PREMIUM 🔴 (SELL AREA)"
    else: return "DISCOUNT 💚 (BUY AREA)"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()
        value = int(data['data'][0]['value'])
        label = data['data'][0]['value_classification']
        if value >= 75: emoji = "🤑"
        elif value >= 55: emoji = "😤"
        elif value >= 45: emoji = "😐"
        elif value >= 25: emoji = "😨"
        else: emoji = "😱"
        return f"{value} — {label.upper()} {emoji}"
    except:
        return "N/A"

def confluence_score(rsi, macd_hist, ema9, ema21, ema50, current_price, stoch_k, bb_upper, bb_lower, lean):
    score = 0
    if lean == "UP":
        if 40 < rsi < 70: score += 15
        if macd_hist > 0: score += 20
        if ema9 > ema21 > ema50: score += 20
        if current_price > ema21: score += 15
        if stoch_k < 80: score += 10
        if current_price < (bb_upper + bb_lower) / 2: score += 20
    else:
        if 30 < rsi < 60: score += 15
        if macd_hist < 0: score += 20
        if ema9 < ema21 < ema50: score += 20
        if current_price < ema21: score += 15
        if stoch_k > 20: score += 10
        if current_price > (bb_upper + bb_lower) / 2: score += 20
    if score >= 85: grade = "S 🏆"
    elif score >= 70: grade = "A+ ⭐"
    elif score >= 55: grade = "A 🔥"
    elif score >= 40: grade = "B 📊"
    else: grade = "C ⚠️"
    return score, grade

def grade_qualifies_for_alert(grade, min_grade):
    order = ["C ⚠️", "B 📊", "A 🔥", "A+ ⭐", "S 🏆"]
    grade_clean = grade.split()[0] + " " + grade.split()[1]
    try:
        return order.index(grade) >= order.index(next(g for g in order if g.startswith(min_grade)))
    except:
        return True

# === PREDICTION LOG ===
def load_predictions():
    try:
        with open(PREDICTIONS_FILE, "r") as f: return json.load(f)
    except: return []

def save_predictions(preds):
    with open(PREDICTIONS_FILE, "w") as f: json.dump(preds, f, indent=2)

def log_prediction(window_start_ts, mode, lean, confidence, open_price):
    preds = load_predictions()
    preds.append({
        "window_start_ts": window_start_ts,
        "mode": mode,
        "lean": lean,
        "confidence": confidence,
        "open_price": open_price,
        "result": None,
        "close_price": None,
        "correct": None,
        "logged_at": datetime.now(timezone.utc).isoformat()
    })
    save_predictions(preds)

# === UTILITIES ===
def get_state():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except: return {"step": 0, "bankroll": 1000.0}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def get_et_now():
    return datetime.now(timezone(timedelta(hours=-4)))

def snap_to_5min(dt):
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

def get_time_windows():
    now = get_et_now()
    current_start = snap_to_5min(now)
    current_end = current_start + timedelta(minutes=5)
    past_start = current_start - timedelta(minutes=5)
    future_end = current_end + timedelta(minutes=5)
    return {
        "past": {
            "label": f"{past_start.strftime('%I:%M')}-{current_start.strftime('%I:%M %p')} ET",
            "start_ts": int(past_start.timestamp()),
            "end_ts": int(current_start.timestamp())
        },
        "current": {
            "label": f"{current_start.strftime('%I:%M')}-{current_end.strftime('%I:%M %p')} ET",
            "start_ts": int(current_start.timestamp()),
            "end_ts": int(current_end.timestamp())
        },
        "future": {
            "label": f"{current_end.strftime('%I:%M')}-{future_end.strftime('%I:%M %p')} ET",
            "start_ts": int(current_end.timestamp()),
            "end_ts": int(future_end.timestamp())
        }
    }

def get_market_urls():
    now = get_et_now()
    current_start = snap_to_5min(now)
    past_start = current_start - timedelta(minutes=5)
    future_start = current_start + timedelta(minutes=5)
    def make_entry(dt):
        end = dt + timedelta(minutes=5)
        return {
            "label": f"{dt.strftime('%I:%M')}-{end.strftime('%I:%M %p')} ET",
            "url": f"https://polyfundr.com/event/btc-updown-5m-{int(dt.timestamp())}"
        }
    return {
        "past": make_entry(past_start),
        "current": make_entry(current_start),
        "future": make_entry(future_start)
    }

# === AUTO EVALUATOR ===
async def evaluate_past_predictions(bot):
    try:
        preds = load_predictions()
        if not preds: return
        state = get_state()
        settings = load_settings()
        ex = ccxt.kraken()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        candle_map = {int(c[0] / 1000): c for c in ohlcv}
        updated = False
        journal = load_journal()

        for pred in preds:
            if pred["result"] is not None: continue
            utc_ts = pred["window_start_ts"] + (4 * 3600)
            candle = candle_map.get(utc_ts)
            if not candle: continue
            if int(datetime.now(timezone.utc).timestamp()) < utc_ts + 300: continue

            open_p = candle[1]
            close_p = candle[4]
            actual = "UP" if close_p >= open_p else "DOWN"
            correct = (pred["lean"] == actual)
            pred["close_price"] = close_p
            pred["result"] = actual
            pred["correct"] = correct
            updated = True

            # Update bankroll
            stake = settings["stake"] * (2 ** state.get("step", 0))
            if correct:
                state["bankroll"] = state.get("bankroll", 1000.0) + stake
                pnl = f"+${stake:.2f}"
            else:
                state["bankroll"] = state.get("bankroll", 1000.0) - stake
                pnl = f"-${stake:.2f}"
            save_state(state)

            # Update journal
            for j in journal:
                if j.get("window_ts") == pred["window_start_ts"] and j.get("result") is None:
                    j["result"] = actual
                    j["pnl"] = pnl
            save_journal(journal)

            emoji = "✅" if correct else "❌"
            for chat_id in BROADCAST_IDS:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📊 *AUTO EVAL*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🕐 Window: {pred['mode'].upper()} | Conf: {pred['confidence']}%\n"
                        f"🤖 Predicted: {pred['lean']} | Actual: {actual}\n"
                        f"Open: ${open_p:.2f} → Close: ${close_p:.2f}\n"
                        f"💰 P&L: {pnl}\n"
                        f"🏦 Bankroll: ${state.get('bankroll', 1000.0):.2f}\n"
                        f"{emoji} {'CORRECT' if correct else 'WRONG'}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
        if updated:
            save_predictions(preds)
    except Exception as e:
        print(f"Eval Error: {e}")

# === DAILY SUMMARY ===
async def send_daily_summary(bot):
    try:
        preds = load_predictions()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_preds = [p for p in preds if p.get("logged_at", "").startswith(today) and p["result"] is not None]
        if not today_preds:
            return
        total = len(today_preds)
        correct = sum(1 for p in today_preds if p["correct"])
        win_rate = (correct / total) * 100
        settings = load_settings()
        state = get_state()
        stake = settings["stake"]
        wins_pnl = correct * stake
        losses_pnl = (total - correct) * stake
        net_pnl = wins_pnl - losses_pnl

        # Streaks
        best_streak = cur_streak = 0
        worst_streak = cur_loss = 0
        for p in today_preds:
            if p["correct"]:
                cur_streak += 1
                cur_loss = 0
                best_streak = max(best_streak, cur_streak)
            else:
                cur_loss += 1
                cur_streak = 0
                worst_streak = max(worst_streak, cur_loss)

        # By grade
        journal = load_journal()
        today_journal = [j for j in journal if j.get("logged_at", "").startswith(today) and j.get("result")]

        grade_stats = {}
        for j in today_journal:
            g = j.get("grade", "?")
            if g not in grade_stats:
                grade_stats[g] = {"total": 0, "correct": 0}
            grade_stats[g]["total"] += 1
            if j.get("result") == j.get("lean"):
                grade_stats[g]["correct"] += 1

        grade_lines = ""
        for g, s in grade_stats.items():
            acc = (s["correct"] / s["total"]) * 100
            grade_lines += f"{g}: {s['correct']}/{s['total']} ({acc:.0f}%)\n"

        pnl_emoji = "💰" if net_pnl >= 0 else "📉"
        msg = (
            f"📅 *DAILY SUMMARY — {datetime.now(get_et_now().tzinfo).strftime('%b %d')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Total Signals: {total}\n"
            f"✅ Wins: {correct} | ❌ Losses: {total - correct}\n"
            f"🎯 Win Rate: {win_rate:.1f}%\n"
            f"{pnl_emoji} P&L: {'+'if net_pnl>=0 else ''}${net_pnl:.2f}\n"
            f"🏦 Bankroll: ${state.get('bankroll', 1000.0):.2f}\n\n"
            f"🔥 Best Streak: {best_streak} wins\n"
            f"😓 Worst Streak: {worst_streak} losses\n\n"
            f"📊 *By Confluence:*\n"
            f"{grade_lines}"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )
        for chat_id in BROADCAST_IDS:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Daily Summary Error: {e}")

# === STATS ===
async def send_stats(bot, chat_id):
    preds = load_predictions()
    evaluated = [p for p in preds if p["result"] is not None]
    if not evaluated:
        await bot.send_message(chat_id=chat_id, text="📊 No evaluated predictions yet. Check back after a few candles.")
        return
    total = len(evaluated)
    correct = sum(1 for p in evaluated if p["correct"])
    win_rate = (correct / total) * 100
    high_conf = [p for p in evaluated if p["confidence"] >= 80]
    mid_conf = [p for p in evaluated if 65 <= p["confidence"] < 80]
    high_acc = (sum(1 for p in high_conf if p["correct"]) / len(high_conf) * 100) if high_conf else 0
    mid_acc = (sum(1 for p in mid_conf if p["correct"]) / len(mid_conf) * 100) if mid_conf else 0
    streak = 0
    for p in reversed(evaluated):
        if p["correct"]: streak += 1
        else: break
    state = get_state()
    msg = (
        f"📊 *POLYFUNDR STATS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Total Predictions: {total}\n"
        f"✅ Correct: {correct} | ❌ Wrong: {total - correct}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n"
        f"🏦 Bankroll: ${state.get('bankroll', 1000.0):.2f}\n\n"
        f"🔥 High Conf (80%+): {high_acc:.1f}% ({len(high_conf)} trades)\n"
        f"📊 Mid Conf (65-79%): {mid_acc:.1f}% ({len(mid_conf)} trades)\n\n"
        f"⚡ Current Win Streak: {streak}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

# === MARKET LINKS ===
async def send_market_links(bot, chat_id):
    urls = get_market_urls()
    msg = (
        f"🔗 *BTC 5-MIN MARKET LINKS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏮️ *Past* ({urls['past']['label']})\n"
        f"[Open on PolyFundr]({urls['past']['url']})\n\n"
        f"▶️ *Current* ({urls['current']['label']})\n"
        f"[Open on PolyFundr]({urls['current']['url']})\n\n"
        f"⏭️ *Next* ({urls['future']['label']})\n"
        f"[Open on PolyFundr]({urls['future']['url']})\n\n"
        f"_Tap any link to open directly on PolyFundr_"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           disable_web_page_preview=True)

# === SETTINGS MENU ===
async def send_settings(bot, chat_id):
    settings = load_settings()
    keyboard = [
        [InlineKeyboardButton("💰 Change Stake", callback_data='set_stake')],
        [InlineKeyboardButton("🎯 Change Confidence Filter", callback_data='set_confidence')],
        [InlineKeyboardButton(f"🔔 Alert Mode: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}", callback_data='toggle_alert')],
        [InlineKeyboardButton("📋 Export Trade Journal", callback_data='export_journal')],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data='back_menu')]
    ]
    msg = (
        f"⚙️ *SETTINGS*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 Current Stake: ${settings['stake']:.0f}\n"
        f"🎯 Confidence Filter: {settings['confidence_filter']}%\n"
        f"🔔 Alert Mode: {'ON ✅ (A+ & S only)' if settings['alert_mode'] else 'OFF ❌ (all signals)'}\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           reply_markup=InlineKeyboardMarkup(keyboard))

# === EXPORT JOURNAL ===
async def export_journal(bot, chat_id):
    journal = load_journal()
    if not journal:
        await bot.send_message(chat_id=chat_id, text="📋 No journal entries yet.")
        return
    lines = ["WINDOW | MODE | LEAN | CONF | GRADE | RESULT | PNL"]
    for j in journal:
        dt = datetime.fromisoformat(j['logged_at']).strftime('%m/%d %H:%M')
        lines.append(f"{dt} | {j.get('mode','?').upper()} | {j.get('lean','?')} | {j.get('confidence','?')}% | {j.get('grade','?')} | {j.get('result','PENDING')} | {j.get('pnl','N/A')}")
    text = "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN)

# === ANALYSIS ENGINE ===
async def run_analysis(bot, mode="current", chat_id=TELEGRAM_CHAT_ID):
    try:
        settings = load_settings()
        state = get_state()
        ex = ccxt.kraken()

        # 5M data
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        closes = np.array([x[4] for x in ohlcv])
        opens = np.array([x[1] for x in ohlcv])
        highs = np.array([x[2] for x in ohlcv])
        lows = np.array([x[3] for x in ohlcv])
        volumes = np.array([x[5] for x in ohlcv])

        # 15M HTF
        ohlcv_15m = ex.fetch_ohlcv("BTC/USDT", "15m", limit=50)
        closes_15m = np.array([x[4] for x in ohlcv_15m])
        htf_rsi = calculate_rsi(closes_15m)
        htf_ema21 = calculate_ema(closes_15m, 21)
        htf_price = closes_15m[-1]
        if htf_price > htf_ema21 and htf_rsi > 50: htf_bias = "📈 BULLISH ✅"
        elif htf_price < htf_ema21 and htf_rsi < 50: htf_bias = "📉 BEARISH ✅"
        else: htf_bias = "↔️ NEUTRAL ⚠️"

        # Indicators
        rsi = calculate_rsi(closes)
        bb_upper, bb_lower = calculate_bollinger(closes)
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        ema50 = calculate_ema(closes, 50)
        macd_line, signal_line, macd_hist = calculate_macd(closes)
        stoch_k, _ = calculate_stoch_rsi(closes)
        atr = calculate_atr(highs, lows, closes)
        vwap = calculate_vwap(highs, lows, closes, volumes)
        support, resistance = calculate_support_resistance(highs, lows)
        volume_trend = calculate_volume_trend(volumes)
        candle_pattern = detect_candle_pattern(opens, closes, highs, lows)
        structure = detect_structure(closes)
        session = detect_session()
        current_price = closes[-1]
        current_open = opens[-1]
        zone = detect_zone(current_price, bb_upper, bb_lower)
        fear_greed = get_fear_greed()
        vwap_pos = "✅ ABOVE" if current_price > vwap else "❌ BELOW"

        step = state.get("step", 0)
        stake = settings["stake"]
        current_stake = stake * (2 ** step)
        total_risk = sum([stake * (2 ** i) for i in range(step + 1)])
        bankroll = state.get("bankroll", 1000.0)

        windows = get_time_windows()
        window = windows[mode]
        target_win = window["label"]
        window_start_ts = window["start_ts"]
        mode_label = {"past": "PAST ⏮️", "current": "CURRENT ▶️", "future": "FUTURE ⏭️"}[mode]

        if mode == "past":
            preds = load_predictions()
            existing = next((p for p in preds if p["window_start_ts"] == window_start_ts and p["result"] is not None), None)
            if existing:
                emoji = "✅" if existing["correct"] else "❌"
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏮️ *PAST WINDOW RESULT*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"⏱️ Window: {target_win}\n"
                        f"🤖 Predicted: {existing['lean']} ({existing['confidence']}%)\n"
                        f"📊 Actual: {existing['result']}\n"
                        f"Open: ${existing['open_price']:.2f} → Close: ${existing['close_price']:.2f}\n"
                        f"{emoji} {'CORRECT' if existing['correct'] else 'WRONG'}\n"
                        f"━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        future_context = "You are predicting the NEXT candle after the current one. Project momentum forward." if mode == "future" else ""

        prompt = f"""
        You are an expert BTC 5-minute candle predictor.
        {future_context}

        MARKET DATA:
        Price: ${current_price:.2f} | VWAP: ${vwap:.2f}
        RSI14: {rsi:.2f} | Stoch RSI: {stoch_k:.2f}
        EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f} | EMA50: ${ema50:.2f}
        MACD Histogram: {macd_hist:.4f}
        BB Upper: ${bb_upper:.2f} | Lower: ${bb_lower:.2f}
        ATR: ${atr:.2f}
        Support: ${support:.2f} | Resistance: ${resistance:.2f}
        Volume: {volume_trend}
        Candle Pattern: {candle_pattern}
        Structure: {structure}
        Zone: {zone}
        Session: {session}
        HTF 15M: {htf_bias}
        Fear/Greed: {fear_greed}
        Mode: {mode} candle ({target_win})

        Return JSON ONLY:
        {{"lean": "UP/DOWN/SKIP", "confidence": 0-100, "reasoning": "2-3 sentence explanation", "strategy": "TREND CONTINUATION/TREND PULLBACK/REVERSAL/BREAKOUT/RANGING SKIP"}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)

        conf_filter = settings.get("confidence_filter", 60)
        if data['lean'] == "SKIP" or data['confidence'] < conf_filter:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🟡 *SKIP* | {mode_label}\n"
                    f"Conf: {data.get('confidence', 0)}% | RSI: {rsi:.1f}\n"
                    f"_{data.get('reasoning', 'No clear signal')}_"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        c_score, c_grade = confluence_score(rsi, macd_hist, ema9, ema21, ema50,
                                            current_price, stoch_k, bb_upper, bb_lower, data['lean'])

        # Alert mode check
        if settings.get("alert_mode") and not grade_qualifies_for_alert(c_grade, settings.get("alert_min_grade", "A+")):
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔕 *ALERT MODE* — Signal skipped (Grade: {c_grade}, needs A+ or S)\nConf: {data['confidence']}%",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        log_prediction(window_start_ts, mode, data['lean'], data['confidence'], current_open)
        log_journal_entry(window_start_ts, mode, data['lean'], data['confidence'],
                         current_stake, c_grade, session, data.get('strategy', 'N/A'))

        bias_emoji = "📈" if data['lean'] == "UP" else "📉"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        invalidation = f"Breaks below ${support:.2f}" if data['lean'] == "UP" else f"Breaks above ${resistance:.2f}"
        market_url = f"https://polyfundr.com/event/btc-updown-5m-{window_start_ts}"

        msg = (
            f"🎯 *POLYFUNDR PRO* | BTC/USDT 5M\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{bias_emoji} *BIAS:* {data['lean']}\n"
            f"🎯 *CONFIDENCE:* {data['confidence']}%\n"
            f"⚡ *STRATEGY:* {data.get('strategy', 'N/A')}\n"
            f"🏆 *CONFLUENCE:* {c_grade} ({c_score}/100)\n"
            f"🌍 *SESSION:* {session}\n"
            f"🕐 *MODE:* {mode_label}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 *STRUCTURE & ZONE*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔷 *STRUCTURE:* {structure}\n"
            f"🗺️ *ZONE:* {zone}\n"
            f"💵 *PRICE:* ${current_price:.2f}\n"
            f"🟢 *SUPPORT:* ${support:.2f}\n"
            f"🔴 *RESISTANCE:* ${resistance:.2f}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📉 *INDICATORS*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 EMA9:  ${ema9:.2f}\n"
            f"📈 EMA21: ${ema21:.2f}\n"
            f"📈 EMA50: ${ema50:.2f}\n"
            f"💹 RSI14: {rsi:.2f}\n"
            f"⚡ STOCH RSI: {stoch_k:.2f}\n"
            f"📊 MACD HIST: {macd_hist:.4f} {macd_emoji}\n"
            f"🌊 ATR: ${atr:.2f}\n"
            f"📐 VWAP: ${vwap:.2f} {vwap_pos}\n"
            f"🕯️ CANDLE: {candle_pattern}\n"
            f"📦 VOLUME: {volume_trend}\n"
            f"🌡️ FEAR/GREED: {fear_greed}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 *HIGHER TIMEFRAME*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"15M BIAS: {htf_bias}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🧠 *WHY THIS TRADE*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"_{data['reasoning']}_\n\n"
            f"⛔ *INVALIDATION:* {invalidation}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *WINDOW:* {target_win}\n"
            f"💰 *STAKE:* ${current_stake:.0f} (Step {step})\n"
            f"📉 *RISK:* ${total_risk:.0f}\n"
            f"🏦 *BANKROLL:* ${bankroll:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"[🔗 Open on PolyFundr]({market_url})"
        )

        keyboard = [[
            InlineKeyboardButton("✅ WIN", callback_data='win'),
            InlineKeyboardButton("❌ LOSS", callback_data='loss')
        ]]
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                               reply_markup=InlineKeyboardMarkup(keyboard),
                               disable_web_page_preview=True)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Signal pushed ({mode_label}).")

    except Exception as e:
        print(f"Signal Error: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Error: {str(e)[:200]}")

# === DASHBOARD ===
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    windows = get_time_windows()
    keyboard = [
        [InlineKeyboardButton(f"⏮️ Past ({windows['past']['label']})", callback_data='past')],
        [InlineKeyboardButton(f"▶️ Current ({windows['current']['label']})", callback_data='current')],
        [InlineKeyboardButton(f"⏭️ Future ({windows['future']['label']})", callback_data='future')],
        [InlineKeyboardButton("🔗 Market Links", callback_data='links'),
         InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
         InlineKeyboardButton("📅 Daily Summary", callback_data='daily')],
        [InlineKeyboardButton("🔄 Reset Martingale ($50)", callback_data='reset')]
    ]
    await update.message.reply_text(
        "🎛️ *PolyFundr Workstation* 👑",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = get_state()
    settings = load_settings()

    if query.data in ['past', 'current', 'future']:
        await run_analysis(context.bot, mode=query.data, chat_id=query.message.chat_id)
    elif query.data == 'links':
        await send_market_links(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'stats':
        await send_stats(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'settings':
        await send_settings(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'daily':
        await send_daily_summary(context.bot)
    elif query.data == 'toggle_alert':
        settings['alert_mode'] = not settings.get('alert_mode', False)
        save_settings(settings)
        status = "ON ✅" if settings['alert_mode'] else "OFF ❌"
        await query.message.reply_text(f"🔔 Alert Mode: {status}")
    elif query.data == 'set_stake':
        await query.message.reply_text(
            "💰 Reply with your new stake amount (e.g. 100):\n_Send /stake 100_",
            parse_mode=ParseMode.MARKDOWN
        )
    elif query.data == 'set_confidence':
        await query.message.reply_text(
            "🎯 Reply with your new confidence filter (e.g. 70):\n_Send /confidence 70_",
            parse_mode=ParseMode.MARKDOWN
        )
    elif query.data == 'export_journal':
        await export_journal(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'back_menu':
        windows = get_time_windows()
        keyboard = [
            [InlineKeyboardButton(f"⏮️ Past ({windows['past']['label']})", callback_data='past')],
            [InlineKeyboardButton(f"▶️ Current ({windows['current']['label']})", callback_data='current')],
            [InlineKeyboardButton(f"⏭️ Future ({windows['future']['label']})", callback_data='future')],
            [InlineKeyboardButton("🔗 Market Links", callback_data='links'),
             InlineKeyboardButton("📊 Stats", callback_data='stats')],
            [InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
             InlineKeyboardButton("📅 Daily Summary", callback_data='daily')],
            [InlineKeyboardButton("🔄 Reset Martingale ($50)", callback_data='reset')]
        ]
        await query.message.reply_text(
            "🎛️ *PolyFundr Workstation* 👑",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == 'reset':
        state['step'] = 0
        save_state(state)
        await query.message.reply_text("♻️ Martingale reset to Step 0.")
    elif query.data == 'win':
        state['step'] = 0
        stake = settings['stake']
        state['bankroll'] = state.get('bankroll', 1000.0) + stake
        save_state(state)
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✅ WIN! Back to ${stake:.0f}. 🏦 Bankroll: ${state['bankroll']:.2f}"
        )
    elif query.data == 'loss':
        state['step'] = min(state['step'] + 1, 5)
        stake = settings['stake']
        state['bankroll'] = state.get('bankroll', 1000.0) - (stake * (2 ** (state['step'] - 1)))
        save_state(state)
        next_stake = stake * (2 ** state['step'])
        await query.edit_message_text(
            text=f"{query.message.text}\n\n❌ LOSS. Next: ${next_stake:.0f}. 🏦 Bankroll: ${state['bankroll']:.2f}"
        )

# === COMMAND HANDLERS ===
async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_stake = float(context.args[0])
        settings = load_settings()
        settings['stake'] = new_stake
        save_settings(settings)
        await update.message.reply_text(f"💰 Stake updated to ${new_stake:.0f}")
    except:
        await update.message.reply_text("❌ Usage: /stake 100")

async def confidence_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_conf = int(context.args[0])
        if not 1 <= new_conf <= 100:
            raise ValueError
        settings = load_settings()
        settings['confidence_filter'] = new_conf
        save_settings(settings)
        await update.message.reply_text(f"🎯 Confidence filter updated to {new_conf}%")
    except:
        await update.message.reply_text("❌ Usage: /confidence 70")

# === MAIN ===
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("start", menu_command))
    app.add_handler(CommandHandler("stake", stake_command))
    app.add_handler(CommandHandler("confidence", confidence_command))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    print("🚀 Pro Workstation LIVE (Groq/Llama-3.3) 👑")
    await app.initialize()
    await app.start()

    async def timer():
        last_summary_date = None
        while True:
            now = datetime.now(timezone.utc)
            # Daily summary at midnight UTC
            if now.hour == 0 and now.minute == 0 and now.date() != last_summary_date:
                await send_daily_summary(app.bot)
                last_summary_date = now.date()
            wait = 300 - ((now.minute % 5) * 60 + now.second)
            await asyncio.sleep(wait)
            for chat_id in BROADCAST_IDS:
                await run_analysis(app.bot, mode="current", chat_id=chat_id)
            await asyncio.sleep(30)
            await evaluate_past_predictions(app.bot)

    asyncio.create_task(timer())
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    await asyncio.Event().wait()

asyncio.run(main())
