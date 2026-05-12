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

SEP = "━━━━━━━━━━━━━━━"

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
        return {"step": 0, "bankroll": 1000.0, "locked_direction": None}

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
    if cur > avg * 1.5: return "ABOVE AVG 🔥"
    elif cur < avg * 0.7: return "BELOW AVG ❄️"
    return "NORMAL 📊"

def detect_candle_pattern(opens, closes, highs, lows):
    o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
    po, pc = opens[-2], closes[-2]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    total_range = h - l
    if total_range == 0 or body/total_range < 0.1: return "DOJI ➖"
    if c > o and pc < po and c > po and o < pc: return "BULL ENGULFING 🟢"
    if c < o and pc > po and c < po and o > pc: return "BEAR ENGULFING 🔴"
    if lower_wick > body*2 and upper_wick < body*0.5: return "HAMMER 🔨"
    if upper_wick > body*2 and lower_wick < body*0.5: return "SHOOT STAR ⭐"
    return "BULL CANDLE 🟢" if c > o else "BEAR CANDLE 🔴"

def detect_structure(closes):
    r = closes[-10:]
    highs = [max(r[i:i+3]) for i in range(len(r)-2)]
    lows = [min(r[i:i+3]) for i in range(len(r)-2)]
    if len(highs) < 2: return "RANGING ↔️"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]: return "BULLISH 📈 HH/HL"
    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]: return "BEARISH 📉 LH/LL"
    return "RANGING ↔️"

def detect_session():
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 8: return "ASIAN 🌏"
    elif 8 <= h < 12: return "LONDON 🇬🇧"
    elif 12 <= h < 20: return "NEW YORK 🗽"
    return "OFF-HOURS 🌙"

def detect_zone(price, bb_upper, bb_lower):
    mid = (bb_upper + bb_lower) / 2
    return "PREMIUM 🔴" if price > mid else "DISCOUNT 💚"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()['data'][0]
        v = int(d['value'])
        label = d['value_classification'].upper()
        emoji = "🤑" if v >= 75 else "😤" if v >= 55 else "😐" if v >= 45 else "😨" if v >= 25 else "😱"
        return f"{v} {label} {emoji}"
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
        if len(closes) < 22: return "NEUTRAL"
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
    else: overall = "MIXED ↔️"
    return t1, t3, t5, overall

def detect_reversal(closes_1m, locked_direction):
    if not locked_direction or len(closes_1m) < 22: return False
    ema9 = calculate_ema(closes_1m, 9)
    ema21 = calculate_ema(closes_1m, 21)
    rsi = calculate_rsi(closes_1m)
    if locked_direction == "UP" and ema9 < ema21 and rsi < 45: return True
    if locked_direction == "DOWN" and ema9 > ema21 and rsi > 55: return True
    return False

# === ET TIME ===
def get_et_now():
    return datetime.now(timezone(timedelta(hours=-4)))

def snap_to_5min(dt):
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

def get_time_windows():
    now = get_et_now()
    cs = snap_to_5min(now)
    ce = cs + timedelta(minutes=5)
    ps = cs - timedelta(minutes=5)
    fe = ce + timedelta(minutes=5)
    return {
        "past": {"label": f"{ps.strftime('%I:%M')}-{cs.strftime('%I:%M %p')} ET", "start_ts": int(ps.timestamp())},
        "current": {"label": f"{cs.strftime('%I:%M')}-{ce.strftime('%I:%M %p')} ET", "start_ts": int(cs.timestamp())},
        "future": {"label": f"{ce.strftime('%I:%M')}-{fe.strftime('%I:%M %p')} ET", "start_ts": int(ce.timestamp())}
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

# === FETCH MARKET DATA ===
def fetch_market_data():
    ex = ccxt.binance()
    ohlcv_1m = ex.fetch_ohlcv("BTC/USDT", "1m", limit=60)
    ohlcv_3m = ex.fetch_ohlcv("BTC/USDT", "3m", limit=40)
    ohlcv_5m = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
    ohlcv_15m = ex.fetch_ohlcv("BTC/USDT", "15m", limit=50)
    return ohlcv_1m, ohlcv_3m, ohlcv_5m, ohlcv_15m

# === AUTO EVALUATOR ===
async def evaluate_past_predictions(bot):
    try:
        preds = load_predictions()
        if not preds: 
            return

        state = get_state()
        settings = load_settings()
        ex = ccxt.binance()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=150)
        
        # Key Fix: Use timestamp directly (no +4 hours)
        candle_map = {int(c[0]/1000): c for c in ohlcv}

        updated = False
        now_utc = int(datetime.now(timezone.utc).timestamp())

        for pred in preds:
            if pred.get("result") is not None:
                continue

            ts = pred["window_start_ts"]
            candle = candle_map.get(ts)

            if not candle:
                continue
            if now_utc < ts + 300:   # Candle not closed yet
                continue

            open_p = candle[1]
            close_p = candle[4]
            actual = "UP" if close_p >= open_p else "DOWN"
            correct = (pred["lean"] == actual)

            # Update prediction
            pred["close_price"] = close_p
            pred["result"] = actual
            pred["correct"] = correct
            updated = True

            # Martingale logic
            stake = settings["stake"] * (2 ** state.get("step", 0))
            pnl = f"+\( {stake:.2f}" if correct else f"- \){stake:.2f}"

            if correct:
                state["bankroll"] = state.get("bankroll", 1000.0) + stake
                state["step"] = 0
            else:
                state["bankroll"] = max(50.0, state.get("bankroll", 1000.0) - stake)
                state["step"] = min(state.get("step", 0) + 1, 5)

            save_state(state)

            # Broadcast result
            result_emoji = "✅" if correct else "❌"
            candle_emoji = "🟢" if actual == "UP" else "🔴"

            for chat_id in BROADCAST_IDS:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📊 *CANDLE RESULT*\n"
                        f"{SEP}\n"
                        f"🤖 Predicted: {pred['lean']} | {candle_emoji} Actual: {actual}\n"
                        f"Open: ${open_p:.2f} → Close: ${close_p:.2f}\n"
                        f"{'📈 GREEN ✅' if actual == 'UP' else '📉 RED ❌'}\n"
                        f"{result_emoji} {'CORRECT' if correct else 'WRONG'}\n"
                        f"💰 P&L: {pnl}\n"
                        f"🏦 Bankroll: ${state['bankroll']:.2f}\n"
                        f"📶 Step: {state['step']}\n"
                        f"{SEP}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )

        if updated:
            save_predictions(preds)

    except Exception as e:
        print(f"Eval Error: {e}")

# === 30 MIN REVIEW ===
async def send_30min_review(bot):
    try:
        preds = load_predictions()
        now_utc = int(datetime.now(timezone.utc).timestamp())
        cutoff = now_utc - 1800
        recent = [p for p in preds if p.get("logged_at") and
                  int(datetime.fromisoformat(p["logged_at"]).timestamp()) >= cutoff]
        if not recent: return
        wins = sum(1 for p in recent if p.get("correct") == True)
        losses = sum(1 for p in recent if p.get("correct") == False)
        pending = sum(1 for p in recent if p.get("result") is None)
        total = wins + losses
        rate = (wins/total*100) if total > 0 else 0
        lines = []
        for p in recent:
            if p["result"] is None: status = "⏳ PENDING"
            elif p["correct"]:
                ce = "🟢" if p["result"] == "UP" else "🔴"
                status = f"✅ {p['lean']} → {ce} {p['result']}"
            else:
                ce = "🟢" if p["result"] == "UP" else "🔴"
                status = f"❌ {p['lean']} → {ce} {p['result']}"
            lines.append(f"• {p.get('mode','?').upper()} {status} {p['confidence']}%")
        msg = (
            f"🕐 *30-MIN REVIEW*\n"
            f"{SEP}\n"
            f"✅ {wins} | ❌ {losses} | ⏳ {pending}\n"
            f"🎯 Win Rate: {rate:.1f}%\n\n"
            + "\n".join(lines) +
            f"\n{SEP}"
        )
        for chat_id in BROADCAST_IDS:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"30min Review Error: {e}")

# === PAST RESULT ===
async def send_past_result(bot, chat_id, window_start_ts, target_win):
    try:
        ex = ccxt.binance()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        candle_map = {int(c[0]/1000): c for c in ohlcv}

        # Key Fix: No +4 hours
        candle = candle_map.get(window_start_ts)

        if not candle:
            await bot.send_message(chat_id=chat_id,
                text=f"⏮️ *PAST*: {target_win}\n⏳ Candle not available yet.",
                parse_mode=ParseMode.MARKDOWN)
            return

        open_p = candle[1]
        close_p = candle[4]
        actual = "UP" if close_p >= open_p else "DOWN"
        candle_emoji = "🟢" if actual == "UP" else "🔴"

        preds = load_predictions()
        pred = next((p for p in preds if p["window_start_ts"] == window_start_ts), None)

        if pred:
            correct = pred["lean"] == actual
            result_emoji = "✅" if correct else "❌"
            pred_line = f"🤖 Predicted: {pred['lean']} ({pred['confidence']}%)\n{result_emoji} {'CORRECT' if correct else 'WRONG'}\n"
        else:
            pred_line = "🤖 No prediction for this window\n"

        msg = (
            f"⏮️ *PAST RESULT*\n"
            f"{SEP}\n"
            f"⏱️ {target_win}\n"
            f"{pred_line}"
            f"📊 Actual: {candle_emoji} {actual}\n"
            f"{'📈 GREEN' if actual == 'UP' else '📉 RED'}\n"
            f"${open_p:.2f} → ${close_p:.2f}\n"
            f"Move: ${abs(close_p-open_p):.2f}\n"
            f"{SEP}"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Error: {str(e)[:100]}")

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
        f"📊 *STATS*\n"
        f"{SEP}\n"
        f"📈 Total: {total} | ✅ {correct} | ❌ {total-correct}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n"
        f"🏦 Bankroll: ${state.get('bankroll',1000.0):.2f}\n"
        f"⚡ Streak: {streak}\n"
        f"📶 Step: {state.get('step',0)}\n"
        f"{SEP}"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

# === MARKET LINKS ===
async def send_market_links(bot, chat_id):
    urls = get_market_urls()
    msg = (
        f"🔗 *MARKET LINKS*\n"
        f"{SEP}\n"
        f"⏮️ *Past* ({urls['past']['label']})\n[Open on PolyFundr]({urls['past']['url']})\n\n"
        f"▶️ *Current* ({urls['current']['label']})\n[Open on PolyFundr]({urls['current']['url']})\n\n"
        f"⏭️ *Next* ({urls['future']['label']})\n[Open on PolyFundr]({urls['future']['url']})\n"
        f"{SEP}"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           disable_web_page_preview=True)

# === SETTINGS ===
async def send_settings(bot, chat_id):
    settings = load_settings()
    keyboard = [
        [InlineKeyboardButton("💰 Change Stake", callback_data='set_stake')],
        [InlineKeyboardButton("🎯 Change Confidence", callback_data='set_confidence')],
        [InlineKeyboardButton(f"🔔 Alert Mode: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}", callback_data='toggle_alert')],
        [InlineKeyboardButton("📋 Export Journal", callback_data='export_journal')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_menu')]
    ]
    msg = (
        f"⚙️ *SETTINGS*\n"
        f"{SEP}\n"
        f"💰 Stake: ${settings['stake']:.0f}\n"
        f"🎯 Confidence: {settings['confidence_filter']}%\n"
        f"🔔 Alert: {'ON ✅' if settings['alert_mode'] else 'OFF ❌'}\n"
        f"{SEP}"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN,
                           reply_markup=InlineKeyboardMarkup(keyboard))

async def export_journal(bot, chat_id):
    journal = load_journal()
    if not journal:
        await bot.send_message(chat_id=chat_id, text="📋 No journal entries yet.")
        return
    lines = ["TIME|MODE|LEAN|CONF|GRADE|RESULT|PNL"]
    for j in journal:
        dt = datetime.fromisoformat(j['logged_at']).strftime('%m/%d %H:%M')
        lines.append(f"{dt}|{j.get('mode','?').upper()}|{j.get('lean','?')}|{j.get('confidence','?')}%|{j.get('grade','?')}|{j.get('result','PENDING')}|{j.get('pnl','N/A')}")
    await bot.send_message(chat_id=chat_id,
                           text=f"```\n{chr(10).join(lines)}\n```",
                           parse_mode=ParseMode.MARKDOWN)

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
            f"📅 *DAILY — {get_et_now().strftime('%b %d')}*\n"
            f"{SEP}\n"
            f"📈 {total} signals | ✅ {correct} | ❌ {total-correct}\n"
            f"🎯 Win Rate: {win_rate:.1f}%\n"
            f"{pnl_emoji} P&L: {'+'if net_pnl>=0 else ''}${net_pnl:.2f}\n"
            f"🏦 Bankroll: ${state.get('bankroll',1000.0):.2f}\n"
            f"{SEP}"
        )
        for chat_id in BROADCAST_IDS:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Daily Summary Error: {e}")

# === SIGNAL BUILDER ===
async def build_and_send_signal(bot, mode="future", chat_id=None):
    try:
        settings = load_settings()
        state = get_state()

        ohlcv_1m, ohlcv_3m, ohlcv_5m, ohlcv_15m = fetch_market_data()

        closes_1m = np.array([x[4] for x in ohlcv_1m])
        closes_3m = np.array([x[4] for x in ohlcv_3m])
        closes_5m = np.array([x[4] for x in ohlcv_5m])
        closes_15m = np.array([x[4] for x in ohlcv_15m])
        opens = np.array([x[1] for x in ohlcv_5m])
        highs = np.array([x[2] for x in ohlcv_5m])
        lows = np.array([x[3] for x in ohlcv_5m])
        volumes = np.array([x[5] for x in ohlcv_5m])

        # Trend
        t1, t3, t5, overall_trend = get_trend(closes_1m, closes_3m, closes_5m)
        locked_dir = state.get("locked_direction")

        # Reversal check
        if detect_reversal(closes_1m, locked_dir):
            new_dir = "DOWN" if locked_dir == "UP" else "UP"
            state["locked_direction"] = new_dir
            state["step"] = 0
            save_state(state)
            targets = [chat_id] if chat_id else BROADCAST_IDS
            for cid in targets:
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        f"🚨 *REVERSAL DETECTED!*\n"
                        f"{SEP}\n"
                        f"Was: {locked_dir} → Now: {new_dir}\n"
                        f"🔄 Martingale reset Step 0\n"
                        f"📌 New lock: *{new_dir}*\n"
                        f"{SEP}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            locked_dir = new_dir

        # HTF 15M
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
        vwap_pos = "✅" if current_price > vwap else "❌"

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

        # Determine effective direction
        if "STRONG UP" in overall_trend: forced_lean = "UP"
        elif "STRONG DOWN" in overall_trend: forced_lean = "DOWN"
        elif "WEAK UP" in overall_trend: forced_lean = "UP"
        elif "WEAK DOWN" in overall_trend: forced_lean = "DOWN"
        else: forced_lean = None

        if forced_lean and not locked_dir:
            state["locked_direction"] = forced_lean
            locked_dir = forced_lean
            save_state(state)

        effective_lean = locked_dir if locked_dir else forced_lean
        future_ctx = "Predict the NEXT candle. Project momentum forward." if mode == "future" else ""

        prompt = f"""
        BTC 5M predictor. {future_ctx}
        TREND: 1M={t1} 3M={t3} 5M={t5} Overall={overall_trend}
        Locked={locked_dir or 'NONE'} Suggested={effective_lean or 'NONE'}
        Price=${current_price:.2f} VWAP=${vwap:.2f}
        RSI={rsi:.1f} Stoch={stoch_k:.1f}
        EMA9=${ema9:.2f} EMA21=${ema21:.2f} EMA50=${ema50:.2f}
        MACD={macd_hist:.4f} ATR=${atr:.2f}
        Support=${support:.2f} Resistance=${resistance:.2f}
        BB=${bb_upper:.2f}/${bb_lower:.2f}
        Vol={volume_trend} Pattern={candle_pattern}
        Structure={structure} Zone={zone}
        Session={session} 15M={htf_bias}
        FearGreed={fear_greed}
        Trade ONLY in trend direction. Mixed=SKIP.
        JSON ONLY: {{"lean":"UP/DOWN/SKIP","confidence":0-100,"reasoning":"2 sentences","strategy":"CONTINUATION/PULLBACK/REVERSAL/BREAKOUT/SKIP"}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=200
        )
        data = json.loads(response.choices[0].message.content)

        conf_filter = settings.get("confidence_filter", 60)
        targets = [chat_id] if chat_id else BROADCAST_IDS

        if data['lean'] == "SKIP" or data['confidence'] < conf_filter:
            for cid in targets:
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        f"🟡 *SKIP* | {mode_label}\n"
                        f"Trend: {overall_trend}\n"
                        f"Conf: {data.get('confidence',0)}%\n"
                        f"_{data.get('reasoning','No clear signal')}_"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            return

        if effective_lean and data['lean'] != effective_lean:
            for cid in targets:
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        f"⚠️ *BLOCKED*\n"
                        f"AI: {data['lean']} | Lock: {effective_lean}\n"
                        f"Staying with trend direction."
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            return

        c_score, c_grade = confluence_score(rsi, macd_hist, ema9, ema21, ema50,
                                            current_price, stoch_k, bb_upper, bb_lower, data['lean'])
        log_prediction(window_start_ts, mode, data['lean'], data['confidence'],
                      current_price, c_grade, data.get('strategy','N/A'), session)

        bias_emoji = "📈" if data['lean'] == "UP" else "📉"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        inv = f"< ${support:.2f}" if data['lean'] == "UP" else f"> ${resistance:.2f}"
        market_url = f"https://polyfundr.com/event/btc-updown-5m-{window_start_ts}"

        msg = (
            f"🎯 *POLYFUNDR PRO* BTC/USDT\n"
            f"{SEP}\n"
            f"{bias_emoji} *{data['lean']}* | {data['confidence']}% | {c_grade}\n"
            f"⚡ {data.get('strategy','N/A')}\n"
            f"🌍 {session} | {mode_label}\n"
            f"{SEP}\n"
            f"📊 *TREND*\n"
            f"1M: {t1} | 3M: {t3} | 5M: {t5}\n"
            f"🔒 {overall_trend}\n"
            f"📌 Locked: {locked_dir or 'NONE'}\n"
            f"{SEP}\n"
            f"📉 *INDICATORS*\n"
            f"RSI: {rsi:.1f} | STOCH: {stoch_k:.1f}\n"
            f"EMA9: ${ema9:.0f} | 21: ${ema21:.0f} | 50: ${ema50:.0f}\n"
            f"MACD: {macd_hist:.4f} {macd_emoji}\n"
            f"ATR: ${atr:.2f} | VWAP: ${vwap:.0f} {vwap_pos}\n"
            f"🕯️ {candle_pattern}\n"
            f"📦 {volume_trend} | 🌡️ {fear_greed}\n"
            f"{SEP}\n"
            f"🏗️ {structure} | {zone}\n"
            f"💵 ${current_price:.2f}\n"
            f"🟢 S: ${support:.2f} | 🔴 R: ${resistance:.2f}\n"
            f"🕐 15M: {htf_bias}\n"
            f"{SEP}\n"
            f"🧠 _{data['reasoning']}_\n"
            f"⛔ Fails if price {inv}\n"
            f"{SEP}\n"
            f"⏱️ {target_win}\n"
            f"💰 ${current_stake:.0f} Step {step}\n"
            f"📉 Risk: ${total_risk:.0f}\n"
            f"🏦 Bank: ${bankroll:.2f}\n"
            f"{SEP}\n"
            f"[🔗 PolyFundr]({market_url})"
        )

        for cid in targets:
            await bot.send_message(chat_id=cid, text=msg, parse_mode=ParseMode.MARKDOWN,
                                   disable_web_page_preview=True)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Signal sent ({mode_label})")

    except Exception as e:
        print(f"Signal Error: {e}")
        targets = [chat_id] if chat_id else BROADCAST_IDS
        for cid in targets:
            await bot.send_message(chat_id=cid, text=f"❌ Error: {str(e)[:150]}")

# === DASHBOARD ===
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    windows = get_time_windows()
    keyboard = [
        [InlineKeyboardButton(f"⏮️ Past Result ({windows['past']['label']})", callback_data='past')],
        [InlineKeyboardButton(f"▶️ Current ({windows['current']['label']})", callback_data='current')],
        [InlineKeyboardButton(f"⏭️ Future ({windows['future']['label']})", callback_data='future')],
        [InlineKeyboardButton("🕐 30-Min Review", callback_data='review30'),
         InlineKeyboardButton("📊 Stats", callback_data='stats')],
        [InlineKeyboardButton("🔗 Links", callback_data='links'),
         InlineKeyboardButton("📅 Daily", callback_data='daily')],
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

    if query.data == 'past':
        windows = get_time_windows()
        await send_past_result(context.bot, query.message.chat_id,
                               windows['past']['start_ts'], windows['past']['label'])
    elif query.data in ['current', 'future']:
        await build_and_send_signal(context.bot, mode=query.data, chat_id=query.message.chat_id)
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
            [InlineKeyboardButton(f"⏭️ Future ({windows['future']['label']})", callback_data='future')],
            [InlineKeyboardButton("🕐 30-Min Review", callback_data='review30'),
             InlineKeyboardButton("📊 Stats", callback_data='stats')],
            [InlineKeyboardButton("🔗 Links", callback_data='links'),
             InlineKeyboardButton("📅 Daily", callback_data='daily')],
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
        await query.message.reply_text("♻️ Reset. Step 0. Trend lock cleared.")

# === COMMANDS ===
async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_stake = float(context.args[0])
        settings = load_settings()
        settings['stake'] = new_stake
        save_settings(settings)
        await update.message.reply_text(f"💰 Stake: ${new_stake:.0f}")
    except:
        await update.message.reply_text("❌ Usage: /stake 100")

async def confidence_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_conf = int(context.args[0])
        if not 1 <= new_conf <= 100: raise ValueError
        settings = load_settings()
        settings['confidence_filter'] = new_conf
        save_settings(settings)
        await update.message.reply_text(f"🎯 Confidence: {new_conf}%")
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
            if now.hour == 0 and now.minute == 0 and now.date() != last_summary_date:
                await send_daily_summary(app.bot)
                last_summary_date = now.date()
            if last_review_time is None or (now - last_review_time).total_seconds() >= 1800:
                await send_30min_review(app.bot)
                last_review_time = now
            wait = 300 - ((now.minute % 5) * 60 + now.second)
            await asyncio.sleep(max(wait, 1))
            await build_and_send_signal(app.bot, mode="future")
            await asyncio.sleep(30)
            await evaluate_past_predictions(app.bot)

    asyncio.create_task(timer())
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    await asyncio.Event().wait()

asyncio.run(main())
