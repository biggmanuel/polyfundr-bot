import os, json, asyncio, numpy as np, requests
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
        return {"stake": 50.0, "confidence_filter": 60, "alert_mode": False}

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f: json.dump(s, f, indent=2)

# === STATE ===
def get_state():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except:
        return {"step": 0, "bankroll": 1000.0, "locked_direction": None, "trend_lock_time": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

# === JOURNAL ===
def load_journal():
    try:
        with open(JOURNAL_FILE, "r") as f: return json.load(f)
    except: return []

def save_journal(j):
    with open(JOURNAL_FILE, "w") as f: json.dump(j, f, indent=2)

# === PREDICTIONS ===
def load_predictions():
    try:
        with open(PREDICTIONS_FILE, "r") as f: return json.load(f)
    except: return []

def save_predictions(p):
    with open(PREDICTIONS_FILE, "w") as f: json.dump(p, f, indent=2)

def log_prediction(window_start_ts, mode, lean, confidence, open_price, grade, strategy, session):
    preds = load_predictions()
    preds.append({
        "window_start_ts": window_start_ts,
        "mode": mode,
        "lean": lean,
        "confidence": confidence,
        "open_price": open_price,
        "grade": grade,
        "strategy": strategy,
        "session": session,
        "result": None,
        "close_price": None,
        "correct": None,
        "logged_at": datetime.now(timezone.utc).isoformat()
    })
    save_predictions(preds)

# === INDICATORS ===
def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum()/period
    down = -seed[seed < 0].sum()/period
    if down == 0: return 100.0
    rs = up/down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100./(1. + rs)
    for i in range(period, len(prices)):
        delta = deltas[i-1]
        upval, downval = (delta, 0.) if delta > 0 else (0., -delta)
        up = (up*(period-1) + upval)/period
        down = (down*(period-1) + downval)/period
        rsi[i] = 100.0 if down == 0 else 100. - 100./(1. + up/down)
    return rsi[-1]

def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = float(prices[0])
    for price in prices[1:]:
        ema = float(price) * k + ema * (1 - k)
    return ema

def calculate_bollinger(prices, period=20):
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    return sma + std*2, sma - std*2

def calculate_macd(prices):
    macd_values = []
    for i in range(26, len(prices)):
        macd_values.append(calculate_ema(prices[:i], 12) - calculate_ema(prices[:i], 26))
    if len(macd_values) < 9: return 0, 0, 0
    signal = calculate_ema(np.array(macd_values), 9)
    return macd_values[-1], signal, macd_values[-1] - signal

def calculate_stoch_rsi(prices, period=14):
    rsi_values = [calculate_rsi(prices[max(0,i-period*2):i+1], period) for i in range(period, len(prices))]
    if len(rsi_values) < period: return 50.0, 50.0
    recent = rsi_values[-period:]
    mn, mx = min(recent), max(recent)
    if mx == mn: return 50.0, 50.0
    return round(((rsi_values[-1] - mn) / (mx - mn)) * 100, 2), 50.0

def calculate_atr(highs, lows, closes, period=14):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    return np.mean(trs[-period:])

def calculate_vwap(highs, lows, closes, volumes):
    tp = (highs + lows + closes) / 3
    return np.sum(tp * volumes) / np.sum(volumes)

def calculate_support_resistance(highs, lows, period=20):
    return min(lows[-period:]), max(highs[-period:])

def calculate_volume_trend(volumes):
    avg = np.mean(volumes[-20:])
    cur = volumes[-1]
    if cur > avg * 1.5: return "ABOVE AVERAGE 🔥"
    elif cur < avg * 0.7: return "BELOW AVERAGE ❄️"
    return "NORMAL 📊"

def detect_candle_pattern(opens, closes, highs, lows):
    o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
    po, pc = opens[-2], closes[-2]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    total_range = h - l
    if total_range == 0 or body/total_range < 0.1: return "DOJI ➖"
    if c > o and pc < po and c > po and o < pc: return "BULLISH ENGULFING 🟢"
    if c < o and pc > po and c < po and o > pc: return "BEARISH ENGULFING 🔴"
    if lower_wick > body*2 and upper_wick < body*0.5: return "HAMMER 🔨 (BULLISH)"
    if upper_wick > body*2 and lower_wick < body*0.5: return "SHOOTING STAR ⭐ (BEARISH)"
    return "BULLISH CANDLE 🟢" if c > o else "BEARISH CANDLE 🔴"

def detect_structure(closes):
    r = closes[-10:]
    highs = [max(r[i:i+3]) for i in range(len(r)-2)]
    lows = [min(r[i:i+3]) for i in range(len(r)-2)]
    if len(highs) < 2: return "RANGING ↔️"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]: return "BULLISH 📈 (HH/HL)"
    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]: return "BEARISH 📉 (LH/LL)"
    return "RANGING ↔️"

def detect_session():
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 8: return "ASIAN 🌏"
    elif 8 <= h < 12: return "LONDON 🇬🇧"
    elif 12 <= h < 20: return "NEW YORK 🗽"
    return "OFF-HOURS 🌙"

def detect_zone(price, bb_upper, bb_lower):
    mid = (bb_upper + bb_lower) / 2
    return "PREMIUM 🔴 (SELL AREA)" if price > mid else "DISCOUNT 💚 (BUY AREA)"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()['data'][0]
        v = int(d['value'])
        label = d['value_classification'].upper()
        emoji = "🤑" if v >= 75 else "😤" if v >= 55 else "😐" if v >= 45 else "😨" if v >= 25 else "😱"
        return f"{v} — {label} {emoji}"
    except: return "N/A"

def confluence_score(rsi, macd_hist, ema9, ema21, ema50, price, stoch_k, bb_upper, bb_lower, lean):
    score = 0
    if lean == "UP":
        if 40 < rsi < 70: score += 15
        if macd_hist > 0: score += 20
        if ema9 > ema21 > ema50: score += 20
        if price > ema21: score += 15
        if stoch_k < 80: score += 10
        if price < (bb_upper + bb_lower) / 2: score += 20
    else:
        if 30 < rsi < 60: score += 15
        if macd_hist < 0: score += 20
        if ema9 < ema21 < ema50: score += 20
        if price < ema21: score += 15
        if stoch_k > 20: score += 10
        if price > (bb_upper + bb_lower) / 2: score += 20
    if score >= 75: grade = "S 🏆"
    elif score >= 60: grade = "A+ ⭐"
    elif score >= 45: grade = "A 🔥"
    elif score >= 30: grade = "B 📊"
    else: grade = "C ⚠️"
    return score, grade

# === TREND ANALYSIS ===
def get_trend(closes_1m, closes_3m, closes_5m):
    def trend_dir(closes):
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        rsi = calculate_rsi(closes)
        if ema9 > ema21 and rsi > 50: return "UP"
        elif ema9 < ema21 and rsi < 50: return "DOWN"
        return "NEUTRAL"

    t1 = trend_dir(closes_1m)
    t3 = trend_dir(closes_3m)
    t5 = trend_dir(closes_5m)

    if t1 == t3 == t5 == "UP": overall = "STRONG UP 📈🟢"
    elif t1 == t3 == t5 == "DOWN": overall = "STRONG DOWN 📉🔴"
    elif t1 == t3 == "UP" or t3 == t5 == "UP": overall = "WEAK UP 📈⚠️"
    elif t1 == t3 == "DOWN" or t3 == t5 == "DOWN": overall = "WEAK DOWN 📉⚠️"
    else: overall = "MIXED ↔️ NO TRADE"

    return t1, t3, t5, overall

def detect_reversal(closes_1m, closes_5m, locked_direction):
    if not locked_direction: return False
    ema9_1m = calculate_ema(closes_1m, 9)
    ema21_1m = calculate_ema(closes_1m, 21)
    rsi_1m = calculate_rsi(closes_1m)
    if locked_direction == "UP":
        if ema9_1m < ema21_1m and rsi_1m < 45: return True
    elif locked_direction == "DOWN":
        if ema9_1m > ema21_1m and rsi_1m > 55: return True
    return False

# === ET TIME ===
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
        },
        "current": {
            "label": f"{current_start.strftime('%I:%M')}-{current_end.strftime('%I:%M %p')} ET",
            "start_ts": int(current_start.timestamp()),
        },
        "future": {
            "label": f"{current_end.strftime('%I:%M')}-{future_end.strftime('%I:%M %p')} ET",
            "start_ts": int(current_end.timestamp()),
        }
    }

def get_market_urls():
    now = get_et_now()
    cs = snap_to_5min(now)
    def make_entry(dt):
        end = dt + timedelta(minutes=5)
        return {
            "label": f"{dt.strftime('%I:%M')}-{end.strftime('%I:%M %p')} ET",
            "url": f"https://polyfundr.com/event/btc-updown-5m-{int(dt.timestamp())}"
        }
    return {
        "past": make_entry(cs - timedelta(minutes=5)),
        "current": make_entry(cs),
        "future": make_entry(cs + timedelta(minutes=5))
    }

# === AUTO EVALUATOR (FIXED) ===
async def evaluate_past_predictions(bot):
    try:
        preds = load_predictions()
        if not preds: return
        state = get_state()
        settings = load_settings()
        ex = ccxt.kraken()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        candle_map = {int(c[0]/1000): c for c in ohlcv}
        updated = False

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
            candle_emoji = "🟢" if actual == "UP" else "🔴"
            pred["close_price"] = close_p
            pred["result"] = actual
            pred["correct"] = correct
            updated = True

            stake = settings["stake"] * (2 ** state.get("step", 0))
            pnl = f"+${stake:.2f}" if correct else f"-${stake:.2f}"
            if correct:
                state["bankroll"] = state.get("bankroll", 1000.0) + stake
                state["step"] = 0
            else:
                state["bankroll"] = state.get("bankroll", 1000.0) - stake
                state["step"] = min(state.get("step", 0) + 1, 5)
            save_state(state)

            result_emoji = "✅" if correct else "❌"
            for chat_id in BROADCAST_IDS:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📊 *CANDLE RESULT*\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"⏱️ Window: {pred.get('mode','?').upper()}\n"
                        f"🤖 Predicted: {pred['lean']} | Candle: {candle_emoji} {actual}\n"
                        f"Open: ${open_p:.2f} → Close: ${close_p:.2f}\n"
                        f"{'📈 Closed GREEN ✅' if actual == 'UP' else '📉 Closed RED ❌'}\n"
                        f"{result_emoji} Prediction: {'CORRECT' if correct else 'WRONG'}\n"
                        f"💰 P&L: {pnl}\n"
                        f"🏦 Bankroll: ${state['bankroll']:.2f}\n"
                        f"📶 Next Step: {state['step']}\n"
                        f"━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )

        if updated: save_predictions(preds)

    except Exception as e:
        print(f"Eval Error: {e}")

# === 30 MIN REVIEW ===
async def send_30min_review(bot):
    try:
        preds = load_predictions()
        now_utc = int(datetime.now(timezone.utc).timestamp())
        cutoff = now_utc - 1800  # 30 mins ago
        recent = [p for p in preds if p.get("logged_at") and
                  int(datetime.fromisoformat(p["logged_at"]).timestamp()) >= cutoff]

        if not recent:
            return

        lines = []
        wins = 0
        losses = 0
        pending = 0
        for p in recent:
            if p["result"] is None:
                status = "⏳ PENDING"
                pending += 1
            elif p["correct"]:
                candle_emoji = "🟢" if p["result"] == "UP" else "🔴"
                status = f"✅ {p['lean']} → Candle {candle_emoji} {p['result']} CORRECT"
                wins += 1
            else:
                candle_emoji = "🟢" if p["result"] == "UP" else "🔴"
                status = f"❌ {p['lean']} → Candle {candle_emoji} {p['result']} WRONG"
                losses += 1
            lines.append(f"• {p.get('mode','?').upper()} | {status} | Conf: {p['confidence']}%")

        total = wins + losses
        rate = (wins/total*100) if total > 0 else 0
        msg = (
            f"🕐 *30-MIN SIGNAL REVIEW*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Correct: {wins} | ❌ Wrong: {losses} | ⏳ Pending: {pending}\n"
            f"🎯 Win Rate: {rate:.1f}%\n\n"
            + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━━"
        )
        for chat_id in BROADCAST_IDS:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        print(f"30min Review Error: {e}")

# === STATS ===
async def send_stats(bot, chat_id):
    preds = load_predictions()
    evaluated = [p for p in preds if p["result"] is not None]
    if not evaluated:
        await bot.send_message(chat_id=chat_id, text="📊 No evaluated predictions yet.")
        return
    total = len(evaluated)
    correct = sum(1 for p in evaluated if p["correct"])
    win_rate = (correct/total)*100
    streak = 0
    for p in reversed(evaluated):
        if p["correct"]: streak += 1
        else: break
    state = get_state()
    msg = (
        f"📊 *POLYFUNDR STATS*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Total: {total} | ✅ {correct} | ❌ {total-correct}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n"
        f"🏦 Bankroll: ${state.get('bankroll',1000.0):.2f}\n"
        f"⚡ Win Streak: {streak}\n"
        f"📶 Martingale Step: {state.get('step',0)}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

# === MARKET LINKS ===
async def send_market_links(bot, chat_id):
    urls = get_market_urls()
    msg = (
        f"🔗 *BTC 5-MIN MARKET LINKS*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⏮️ *Past* ({urls['past']['label']})\n[Open on PolyFundr]({urls['past']['url']})\n\n"
        f"▶️ *Current* ({urls['current']['label']})\n[Open on PolyFundr]({urls['current']['url']})\n\n"
        f"⏭️ *Next* ({urls['future']['label']})\n[Open on PolyFundr]({urls['future']['url']})\n\n"
        f"_Tap to open directly on PolyFundr_"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           disable_web_page_preview=True)

# === SETTINGS ===
async def send_settings(bot, chat_id):
    settings = load_settings()
    keyboard = [
        [InlineKeyboardButton("💰 Change Stake", callback_data='set_stake')],
        [InlineKeyboardButton("🎯 Change Confidence Filter", callback_data='set_confidence')],
        [InlineKeyboardButton(f"🔔 Alert Mode: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}", callback_data='toggle_alert')],
        [InlineKeyboardButton("📋 Export Journal", callback_data='export_journal')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_menu')]
    ]
    msg = (
        f"⚙️ *SETTINGS*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Stake: ${settings['stake']:.0f}\n"
        f"🎯 Confidence Filter: {settings['confidence_filter']}%\n"
        f"🔔 Alert Mode: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           reply_markup=InlineKeyboardMarkup(keyboard))

async def export_journal(bot, chat_id):
    journal = load_journal()
    if not journal:
        await bot.send_message(chat_id=chat_id, text="📋 No journal entries yet.")
        return
    lines = ["TIME | MODE | LEAN | CONF | GRADE | RESULT | PNL"]
    for j in journal:
        dt = datetime.fromisoformat(j['logged_at']).strftime('%m/%d %H:%M')
        lines.append(f"{dt}|{j.get('mode','?').upper()}|{j.get('lean','?')}|{j.get('confidence','?')}%|{j.get('grade','?')}|{j.get('result','PENDING')}|{j.get('pnl','N/A')}")
    await bot.send_message(chat_id=chat_id,
                           text=f"```\n{chr(10).join(lines)}\n```",
                           parse_mode=ParseMode.MARKDOWN)

# === PAST RESULT LOOKUP ===
async def send_past_result(bot, chat_id, window_start_ts, target_win):
    try:
        ex = ccxt.kraken()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        candle_map = {int(c[0]/1000): c for c in ohlcv}
        utc_ts = window_start_ts + (4 * 3600)
        candle = candle_map.get(utc_ts)

        preds = load_predictions()
        pred = next((p for p in preds if p["window_start_ts"] == window_start_ts), None)

        if not candle:
            await bot.send_message(chat_id=chat_id,
                                   text=f"⏮️ *PAST WINDOW*: {target_win}\n⏳ Candle data not available yet.",
                                   parse_mode=ParseMode.MARKDOWN)
            return

        open_p = candle[1]
        close_p = candle[4]
        actual = "UP" if close_p >= open_p else "DOWN"
        candle_emoji = "🟢" if actual == "UP" else "🔴"
        candle_result = "📈 Closed GREEN" if actual == "UP" else "📉 Closed RED"

        if pred:
            correct = pred["lean"] == actual
            result_emoji = "✅" if correct else "❌"
            pred_line = (
                f"🤖 Predicted: {pred['lean']} ({pred['confidence']}%)\n"
                f"{result_emoji} Prediction: {'CORRECT' if correct else 'WRONG'}\n"
            )
        else:
            pred_line = "🤖 No prediction was made for this window\n"

        msg = (
            f"⏮️ *PAST WINDOW RESULT*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ Window: {target_win}\n"
            f"{pred_line}"
            f"📊 Actual: {candle_emoji} {actual}\n"
            f"{candle_result}\n"
            f"Open: ${open_p:.2f} → Close: ${close_p:.2f}\n"
            f"{'📈' if close_p > open_p else '📉'} Move: ${abs(close_p-open_p):.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Error fetching past result: {str(e)[:100]}")

# === MAIN ANALYSIS ENGINE ===
async def run_analysis(bot, mode="future", chat_id=TELEGRAM_CHAT_ID):
    try:
        settings = load_settings()
        state = get_state()
        ex = ccxt.kraken()

        # Fetch all timeframes
        ohlcv_1m = ex.fetch_ohlcv("BTC/USDT", "1m", limit=100)
        ohlcv_3m = ex.fetch_ohlcv("BTC/USDT", "3m", limit=60)
        ohlcv_5m = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        ohlcv_15m = ex.fetch_ohlcv("BTC/USDT", "15m", limit=50)

        closes_1m = np.array([x[4] for x in ohlcv_1m])
        closes_3m = np.array([x[4] for x in ohlcv_3m])
        closes_5m = np.array([x[4] for x in ohlcv_5m])
        closes_15m = np.array([x[4] for x in ohlcv_15m])

        opens = np.array([x[1] for x in ohlcv_5m])
        highs = np.array([x[2] for x in ohlcv_5m])
        lows = np.array([x[3] for x in ohlcv_5m])
        volumes = np.array([x[5] for x in ohlcv_5m])

        # Trend analysis
        t1, t3, t5, overall_trend = get_trend(closes_1m, closes_3m, closes_5m)

        # Reversal check
        locked_dir = state.get("locked_direction")
        reversal_detected = detect_reversal(closes_1m, closes_5m, locked_dir)
        if reversal_detected:
            new_dir = "DOWN" if locked_dir == "UP" else "UP"
            state["locked_direction"] = new_dir
            state["step"] = 0  # reset martingale on reversal
            save_state(state)
            for cid in BROADCAST_IDS:
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        f"🚨 *TREND REVERSAL DETECTED!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚠️ Was: {locked_dir} | Now: {new_dir}\n"
                        f"🔄 Martingale reset to Step 0\n"
                        f"📌 New locked direction: *{new_dir}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            locked_dir = new_dir

        # 15M HTF bias
        htf_rsi = calculate_rsi(closes_15m)
        htf_ema21 = calculate_ema(closes_15m, 21)
        htf_price = closes_15m[-1]
        if htf_price > htf_ema21 and htf_rsi > 50: htf_bias = "📈 BULLISH ✅"
        elif htf_price < htf_ema21 and htf_rsi < 50: htf_bias = "📉 BEARISH ✅"
        else: htf_bias = "↔️ NEUTRAL ⚠️"

        # 5M indicators
        rsi = calculate_rsi(closes_5m)
        bb_upper, bb_lower = calculate_bollinger(closes_5m)
        ema9 = calculate_ema(closes_5m, 9)
        ema21 = calculate_ema(closes_5m, 21)
        ema50 = calculate_ema(closes_5m, 50)
        macd_line, signal_line, macd_hist = calculate_macd(closes_5m)
        stoch_k, _ = calculate_stoch_rsi(closes_5m)
        atr = calculate_atr(highs, lows, closes_5m)
        vwap = calculate_vwap(highs, lows, closes_5m, volumes)
        support, resistance = calculate_support_resistance(highs, lows)
        volume_trend = calculate_volume_trend(volumes)
        candle_pattern = detect_candle_pattern(opens, closes_5m, highs, lows)
        structure = detect_structure(closes_5m)
        session = detect_session()
        current_price = closes_5m[-1]
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

        # Past mode → show actual result
        if mode == "past":
            await send_past_result(bot, chat_id, window_start_ts, target_win)
            return

        # Determine trade direction from trend
        if "STRONG UP" in overall_trend:
            forced_lean = "UP"
        elif "STRONG DOWN" in overall_trend:
            forced_lean = "DOWN"
        elif "WEAK UP" in overall_trend:
            forced_lean = "UP"
        elif "WEAK DOWN" in overall_trend:
            forced_lean = "DOWN"
        else:
            forced_lean = None

        # Lock direction for martingale consistency
        if forced_lean and not locked_dir:
            state["locked_direction"] = forced_lean
            locked_dir = forced_lean
            save_state(state)

        # Use locked direction if set
        effective_lean = locked_dir if locked_dir else forced_lean

        future_context = "Predict the NEXT 5-minute candle after current. Project momentum forward." if mode == "future" else ""

        prompt = f"""
        You are an expert BTC 5-minute candle predictor.
        {future_context}

        TREND ANALYSIS:
        1M Trend: {t1} | 3M Trend: {t3} | 5M Trend: {t5}
        Overall: {overall_trend}
        Locked Direction: {locked_dir or 'NONE'}

        MARKET DATA:
        Price: ${current_price:.2f} | VWAP: ${vwap:.2f} ({vwap_pos})
        RSI14: {rsi:.2f} | Stoch RSI: {stoch_k:.2f}
        EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f} | EMA50: ${ema50:.2f}
        MACD Hist: {macd_hist:.4f}
        BB Upper: ${bb_upper:.2f} | Lower: ${bb_lower:.2f}
        ATR: ${atr:.2f}
        Support: ${support:.2f} | Resistance: ${resistance:.2f}
        Volume: {volume_trend}
        Candle Pattern: {candle_pattern}
        Structure: {structure}
        Zone: {zone}
        Session: {session}
        15M HTF Bias: {htf_bias}
        Fear/Greed: {fear_greed}

        The trend analysis suggests {effective_lean or 'NO CLEAR DIRECTION'}.
        Only trade in the direction of the trend. If trend is mixed, return SKIP.

        Return JSON ONLY:
        {{"lean": "UP/DOWN/SKIP", "confidence": 0-100, "reasoning": "2-3 sentence explanation", "strategy": "TREND CONTINUATION/TREND PULLBACK/REVERSAL/BREAKOUT/RANGING SKIP"}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=300
        )
        data = json.loads(response.choices[0].message.content)

        conf_filter = settings.get("confidence_filter", 60)
        if data['lean'] == "SKIP" or data['confidence'] < conf_filter:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🟡 *SKIP* | {mode_label}\n"
                    f"Trend: {overall_trend}\n"
                    f"Conf: {data.get('confidence', 0)}% | RSI: {rsi:.1f}\n"
                    f"_{data.get('reasoning', 'No clear signal')}_"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Enforce trend direction consistency
        if effective_lean and data['lean'] != effective_lean and data['lean'] != "SKIP":
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ *SIGNAL BLOCKED*\n"
                    f"AI suggested {data['lean']} but trend is locked {effective_lean}\n"
                    f"Staying consistent with martingale direction."
                ),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        c_score, c_grade = confluence_score(rsi, macd_hist, ema9, ema21, ema50,
                                            current_price, stoch_k, bb_upper, bb_lower, data['lean'])

        log_prediction(window_start_ts, mode, data['lean'], data['confidence'],
                      current_open, c_grade, data.get('strategy', 'N/A'), session)

        bias_emoji = "📈" if data['lean'] == "UP" else "📉"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        invalidation = f"Breaks below ${support:.2f}" if data['lean'] == "UP" else f"Breaks above ${resistance:.2f}"
        market_url = f"https://polyfundr.com/event/btc-updown-5m-{window_start_ts}"

        msg = (
            f"🎯 *POLYFUNDR PRO* | BTC/USDT 5M\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{bias_emoji} *BIAS:* {data['lean']}\n"
            f"🎯 *CONFIDENCE:* {data['confidence']}%\n"
            f"⚡ *STRATEGY:* {data.get('strategy','N/A')}\n"
            f"🏆 *CONFLUENCE:* {c_grade} ({c_score}/100)\n"
            f"🌍 *SESSION:* {session}\n"
            f"🕐 *MODE:* {mode_label}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *TREND ANALYSIS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"1M: {t1} | 3M: {t3} | 5M: {t5}\n"
            f"🔒 Overall: {overall_trend}\n"
            f"📌 Locked Direction: {locked_dir or 'NONE'}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *STRUCTURE & ZONE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔷 *STRUCTURE:* {structure}\n"
            f"🗺️ *ZONE:* {zone}\n"
            f"💵 *PRICE:* ${current_price:.2f}\n"
            f"🟢 *SUPPORT:* ${support:.2f}\n"
            f"🔴 *RESISTANCE:* ${resistance:.2f}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 *INDICATORS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f} | EMA50: ${ema50:.2f}\n"
            f"💹 RSI14: {rsi:.2f} | ⚡ STOCH: {stoch_k:.2f}\n"
            f"📊 MACD: {macd_hist:.4f} {macd_emoji}\n"
            f"🌊 ATR: ${atr:.2f} | 📐 VWAP: ${vwap:.2f} {vwap_pos}\n"
            f"🕯️ CANDLE: {candle_pattern}\n"
            f"📦 VOLUME: {volume_trend}\n"
            f"🌡️ FEAR/GREED: {fear_greed}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 *HIGHER TIMEFRAME*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"15M BIAS: {htf_bias}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 *WHY THIS TRADE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_{data['reasoning']}_\n\n"
            f"⛔ *INVALIDATION:* {invalidation}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *WINDOW:* {target_win}\n"
            f"💰 *STAKE:* ${current_stake:.0f} (Step {step})\n"
            f"📉 *RISK:* ${total_risk:.0f}\n"
            f"🏦 *BANKROLL:* ${bankroll:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"[🔗 Open on PolyFundr]({market_url})"
        )

        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Signal pushed ({mode_label}).")

    except Exception as e:
        print(f"Signal Error: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Error: {str(e)[:200]}")

# === DAILY SUMMARY ===
async def send_daily_summary(bot):
    try:
        preds = load_predictions()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_preds = [p for p in preds if p.get("logged_at","").startswith(today) and p["result"] is not None]
        if not today_preds: return
        total = len(today_preds)
        correct = sum(1 for p in today_preds if p["correct"])
        win_rate = (correct/total)*100
        settings = load_settings()
        stake = settings["stake"]
        net_pnl = (correct * stake) - ((total-correct) * stake)
        state = get_state()
        pnl_emoji = "💰" if net_pnl >= 0 else "📉"
        msg = (
            f"📅 *DAILY SUMMARY — {get_et_now().strftime('%b %d')}*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📈 Signals: {total} | ✅ {correct} | ❌ {total-correct}\n"
            f"🎯 Win Rate: {win_rate:.1f}%\n"
            f"{pnl_emoji} P&L: {'+'if net_pnl>=0 else ''}${net_pnl:.2f}\n"
            f"🏦 Bankroll: ${state.get('bankroll',1000.0):.2f}\n"
            f"━━━━━━━━━━━━━━━━━"
        )
        for chat_id in BROADCAST_IDS:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Daily Summary Error: {e}")

# === DASHBOARD ===
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    windows = get_time_windows()
    keyboard = [
        [InlineKeyboardButton(f"⏮️ Past Result ({windows['past']['label']})", callback_data='past')],
        [InlineKeyboardButton(f"▶️ Current ({windows['current']['label']})", callback_data='current')],
        [InlineKeyboardButton(f"⏭️ Future Signal ({windows['future']['label']})", callback_data='future')],
        [InlineKeyboardButton("🕐 30-Min Review", callback_data='review30'),
         InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🔗 Market Links", callback_data='links'),
         InlineKeyboardButton("📅 Daily Summary", callback_data='daily')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("🔄 Reset Martingale", callback_data='reset')]
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
    elif query.data == 'review30':
        await send_30min_review(context.bot)
    elif query.data == 'links':
        await send_market_links(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'stats':
        await send_stats(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'daily':
        await send_daily_summary(context.bot)
    elif query.data == 'settings':
        await send_settings(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'toggle_alert':
        settings['alert_mode'] = not settings.get('alert_mode', False)
        save_settings(settings)
        await query.message.reply_text(f"🔔 Alert Mode: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}")
    elif query.data == 'set_stake':
        await query.message.reply_text("💰 Send: /stake 100")
    elif query.data == 'set_confidence':
        await query.message.reply_text("🎯 Send: /confidence 70")
    elif query.data == 'export_journal':
        await export_journal(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'back_menu':
        windows = get_time_windows()
        keyboard = [
            [InlineKeyboardButton(f"⏮️ Past Result ({windows['past']['label']})", callback_data='past')],
            [InlineKeyboardButton(f"▶️ Current ({windows['current']['label']})", callback_data='current')],
            [InlineKeyboardButton(f"⏭️ Future Signal ({windows['future']['label']})", callback_data='future')],
            [InlineKeyboardButton("🕐 30-Min Review", callback_data='review30'),
             InlineKeyboardButton("📊 Stats", callback_data='stats')],
            [InlineKeyboardButton("🔗 Market Links", callback_data='links'),
             InlineKeyboardButton("📅 Daily Summary", callback_data='daily')],
            [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
            [InlineKeyboardButton("🔄 Reset Martingale", callback_data='reset')]
        ]
        await query.message.reply_text(
            "🎛️ *PolyFundr Workstation* 👑",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif query.data == 'reset':
        state['step'] = 0
        state['locked_direction'] = None
        save_state(state)
        await query.message.reply_text("♻️ Martingale reset. Trend lock cleared.")

# === COMMANDS ===
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
        if not 1 <= new_conf <= 100: raise ValueError
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

    print("🚀 PolyFundr Pro LIVE 👑")
    await app.initialize()
    await app.start()

    async def timer():
        last_summary_date = None
        last_review_time = None
        while True:
            now = datetime.now(timezone.utc)

            # Daily summary at midnight UTC
            if now.hour == 0 and now.minute == 0 and now.date() != last_summary_date:
                await send_daily_summary(app.bot)
                last_summary_date = now.date()

            # 30-min review every 30 mins
            if last_review_time is None or (now - last_review_time).seconds >= 1800:
                await send_30min_review(app.bot)
                last_review_time = now

            # Wait for next 5-min window
            wait = 300 - ((now.minute % 5) * 60 + now.second)
            await asyncio.sleep(wait)

            # Auto broadcast future signal only (to save Groq tokens)
            signal_data = await get_signal_data()
            if signal_data:
                for chat_id in BROADCAST_IDS:
                    await bot_send_signal(app.bot, signal_data, chat_id)

            await asyncio.sleep(30)
            await evaluate_past_predictions(app.bot)

    asyncio.create_task(timer())
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    await asyncio.Event().wait()

# Generate signal once, broadcast to all
async def get_signal_data():
    try:
        ex = ccxt.kraken()
        ohlcv_1m = ex.fetch_ohlcv("BTC/USDT", "1m", limit=100)
        ohlcv_3m = ex.fetch_ohlcv("BTC/USDT", "3m", limit=60)
        ohlcv_5m = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        ohlcv_15m = ex.fetch_ohlcv("BTC/USDT", "15m", limit=50)

        closes_1m = np.array([x[4] for x in ohlcv_1m])
        closes_3m = np.array([x[4] for x in ohlcv_3m])
        closes_5m = np.array([x[4] for x in ohlcv_5m])
        closes_15m = np.array([x[4] for x in ohlcv_15m])
        opens = np.array([x[1] for x in ohlcv_5m])
        highs = np.array([x[2] for x in ohlcv_5m])
        lows = np.array([x[3] for x in ohlcv_5m])
        volumes = np.array([x[5] for x in ohlcv_5m])

        t1, t3, t5, overall_trend = get_trend(closes_1m, closes_3m, closes_5m)
        htf_rsi = calculate_rsi(closes_15m)
        htf_ema21 = calculate_ema(closes_15m, 21)
        htf_price = closes_15m[-1]
        if htf_price > htf_ema21 and htf_rsi > 50: htf_bias = "📈 BULLISH ✅"
        elif htf_price < htf_ema21 and htf_rsi < 50: htf_bias = "📉 BEARISH ✅"
        else: htf_bias = "↔️ NEUTRAL ⚠️"

        rsi = calculate_rsi(closes_5m)
        bb_upper, bb_lower = calculate_bollinger(closes_5m)
        ema9 = calculate_ema(closes_5m, 9)
        ema21 = calculate_ema(closes_5m, 21)
        ema50 = calculate_ema(closes_5m, 50)
        _, _, macd_hist = calculate_macd(closes_5m)
        stoch_k, _ = calculate_stoch_rsi(closes_5m)
        atr = calculate_atr(highs, lows, closes_5m)
        vwap = calculate_vwap(highs, lows, closes_5m, volumes)
        support, resistance = calculate_support_resistance(highs, lows)
        volume_trend = calculate_volume_trend(volumes)
        candle_pattern = detect_candle_pattern(opens, closes_5m, highs, lows)
        structure = detect_structure(closes_5m)
        session = detect_session()
        current_price = closes_5m[-1]
        zone = detect_zone(current_price, bb_upper, bb_lower)
        fear_greed = get_fear_greed()
        vwap_pos = "✅ ABOVE" if current_price > vwap else "❌ BELOW"

        state = get_state()
        settings = load_settings()
        locked_dir = state.get("locked_direction")

        if "STRONG UP" in overall_trend: forced_lean = "UP"
        elif "STRONG DOWN" in overall_trend: forced_lean = "DOWN"
        elif "WEAK UP" in overall_trend: forced_lean = "UP"
        elif "WEAK DOWN" in overall_trend: forced_lean = "DOWN"
        else: forced_lean = None

        effective_lean = locked_dir if locked_dir else forced_lean

        windows = get_time_windows()
        window = windows["future"]
        target_win = window["label"]
        window_start_ts = window["start_ts"]

        prompt = f"""
        You are an expert BTC 5-minute candle predictor.
        Predict the NEXT 5-minute candle. Project momentum forward.

        TREND: 1M={t1} | 3M={t3} | 5M={t5} | Overall={overall_trend}
        Locked Direction: {locked_dir or 'NONE'}
        Price: ${current_price:.2f} | VWAP: ${vwap:.2f}
        RSI: {rsi:.2f} | Stoch: {stoch_k:.2f}
        EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f} | EMA50: ${ema50:.2f}
        MACD Hist: {macd_hist:.4f}
        BB: ${bb_upper:.2f}/${bb_lower:.2f}
        Support: ${support:.2f} | Resistance: ${resistance:.2f}
        Volume: {volume_trend} | Pattern: {candle_pattern}
        Structure: {structure} | Zone: {zone}
        Session: {session} | 15M: {htf_bias}
        Fear/Greed: {fear_greed}
        Suggested direction: {effective_lean or 'NONE'}
        Only trade in trend direction. Mixed trend = SKIP.

        JSON ONLY: {{"lean":"UP/DOWN/SKIP","confidence":0-100,"reasoning":"2 sentences","strategy":"TREND CONTINUATION/PULLBACK/REVERSAL/BREAKOUT/SKIP"}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=200
        )
        data = json.loads(response.choices[0].message.content)

        conf_filter = settings.get("confidence_filter", 60)
        if data['lean'] == "SKIP" or data['confidence'] < conf_filter:
            return None

        if effective_lean and data['lean'] != effective_lean:
            return None

        c_score, c_grade = confluence_score(rsi, macd_hist, ema9, ema21, ema50,
                                            current_price, stoch_k, bb_upper, bb_lower, data['lean'])

        log_prediction(window_start_ts, "future", data['lean'], data['confidence'],
                      closes_5m[-1], c_grade, data.get('strategy','N/A'), session)

        step = state.get("step", 0)
        stake = settings["stake"]
        current_stake = stake * (2 ** step)
        total_risk = sum([stake * (2 ** i) for i in range(step + 1)])
        bankroll = state.get("bankroll", 1000.0)
        bias_emoji = "📈" if data['lean'] == "UP" else "📉"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        invalidation = f"Breaks below ${support:.2f}" if data['lean'] == "UP" else f"Breaks above ${resistance:.2f}"
        market_url = f"https://polyfundr.com/event/btc-updown-5m-{window_start_ts}"

        msg = (
            f"🎯 *POLYFUNDR PRO* | BTC/USDT 5M\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"{bias_emoji} *BIAS:* {data['lean']}\n"
            f"🎯 *CONFIDENCE:* {data['confidence']}%\n"
            f"⚡ *STRATEGY:* {data.get('strategy','N/A')}\n"
            f"🏆 *CONFLUENCE:* {c_grade} ({c_score}/100)\n"
            f"🌍 *SESSION:* {session}\n"
            f"🕐 *MODE:* FUTURE ⏭️\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 *TREND ANALYSIS*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"1M: {t1} | 3M: {t3} | 5M: {t5}\n"
            f"🔒 Overall: {overall_trend}\n"
            f"📌 Locked: {locked_dir or 'NONE'}\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 *STRUCTURE & ZONE*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🔷 {structure} | 🗺️ {zone}\n"
            f"💵 ${current_price:.2f} | 🟢 ${support:.2f} | 🔴 ${resistance:.2f}\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📉 *INDICATORS*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f} | EMA50: ${ema50:.2f}\n"
            f"💹 RSI: {rsi:.2f} | ⚡ STOCH: {stoch_k:.2f}\n"
            f"📊 MACD: {macd_hist:.4f} {macd_emoji}\n"
            f"🌊 ATR: ${atr:.2f} | 📐 VWAP: ${vwap:.2f} {vwap_pos}\n"
            f"🕯️ {candle_pattern} | 📦 {volume_trend}\n"
            f"🌡️ FEAR/GREED: {fear_greed}\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🕐 *15M HTF:* {htf_bias}\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🧠 _{data['reasoning']}_\n"
            f"⛔ *INVALIDATION:* {invalidation}\n\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *WINDOW:* {target_win}\n"
            f"💰 *STAKE:* ${current_stake:.0f} (Step {step})\n"
            f"📉 *RISK:* ${total_risk:.0f}\n"
            f"🏦 *BANKROLL:* ${bankroll:.2f}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"[🔗 Open on PolyFundr]({market_url})"
        )
        return msg

    except Exception as e:
        print(f"Signal Data Error: {e}")
        return None

async def bot_send_signal(bot, msg, chat_id):
    try:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True)
    except Exception as e:
        print(f"Send Error: {e}")

asyncio.run(main())
