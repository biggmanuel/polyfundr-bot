import os, json, asyncio, re, numpy as np
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


# === GROQ CLIENT ===
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"


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
    except: return {"step": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)


def get_et_now():
    et_offset = timezone(timedelta(hours=-4))
    return datetime.now(et_offset)


def snap_to_5min(dt):
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)


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
        if not preds:
            return
        ex = ccxt.kraken()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=100)
        candle_map = {}
        for c in ohlcv:
            utc_ts = int(c[0] / 1000)
            candle_map[utc_ts] = c
        updated = False
        for pred in preds:
            if pred["result"] is not None:
                continue
            utc_ts = pred["window_start_ts"] + (4 * 3600)
            candle = candle_map.get(utc_ts)
            if not candle:
                continue
            candle_close_utc = utc_ts + 300
            now_utc = int(datetime.now(timezone.utc).timestamp())
            if now_utc < candle_close_utc:
                continue
            open_p = candle[1]
            close_p = candle[4]
            actual = "UP" if close_p >= open_p else "DOWN"
            correct = (pred["lean"] == actual)
            pred["close_price"] = close_p
            pred["result"] = actual
            pred["correct"] = correct
            updated = True
            emoji = "✅" if correct else "❌"
            for chat_id in BROADCAST_IDS:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📊 *AUTO EVAL*\n"
                        f"🕐 Window: {pred['mode'].upper()} | Conf: {pred['confidence']}%\n"
                        f"🤖 Predicted: {pred['lean']} | Actual: {actual}\n"
                        f"Open: ${open_p:.2f} → Close: ${close_p:.2f}\n"
                        f"{emoji} {'CORRECT' if correct else 'WRONG'}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
        if updated:
            save_predictions(preds)
    except Exception as e:
        print(f"Eval Error: {e}")


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
    msg = (
        f"📊 *POLYFUNDR STATS*\n\n"
        f"📈 Total Predictions: {total}\n"
        f"✅ Correct: {correct} | ❌ Wrong: {total - correct}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n\n"
        f"🔥 High Conf (80%+): {high_acc:.1f}% accuracy ({len(high_conf)} trades)\n"
        f"📊 Mid Conf (65-79%): {mid_acc:.1f}% accuracy ({len(mid_conf)} trades)\n\n"
        f"⚡ Current Win Streak: {streak}"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)


# === MARKET LINKS ===
async def send_market_links(bot, chat_id):
    urls = get_market_urls()
    msg = (
        f"🔗 *BTC 5-MIN MARKET LINKS*\n\n"
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


# === ANALYSIS ENGINE ===
async def run_analysis(bot, mode="current", chat_id=TELEGRAM_CHAT_ID):
    try:
        state = get_state()
        ex = ccxt.kraken()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "5m", limit=50)
        closes = np.array([x[4] for x in ohlcv])
        opens = np.array([x[1] for x in ohlcv])
        rsi = calculate_rsi(closes)
        bb_upper, bb_lower = calculate_bollinger(closes)
        current_price = closes[-1]
        current_open = opens[-1]
        step = state['step']
        current_stake = BASE_STAKE * (2 ** step)
        total_risk = sum([BASE_STAKE * (2 ** i) for i in range(step + 1)])
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
                        f"⏱️ Window: {target_win}\n"
                        f"🤖 Predicted: {existing['lean']} ({existing['confidence']}%)\n"
                        f"📊 Actual: {existing['result']}\n"
                        f"Open: ${existing['open_price']:.2f} → Close: ${existing['close_price']:.2f}\n"
                        f"{emoji} {'CORRECT' if existing['correct'] else 'WRONG'}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
                return


        prompt = f"""
        BTC: ${current_price} | RSI: {rsi:.2f}
        Bollinger Bands: Upper ${bb_upper:.2f}, Lower ${bb_lower:.2f}
        Trend: {"BULLISH" if current_price > bb_upper else "BEARISH" if current_price < bb_lower else "NEUTRAL"}


        Predict the {mode} 5m candle ({target_win}). Return JSON ONLY with no extra text:
        {{"lean": "UP/DOWN/SKIP", "confidence": 0-100, "reasoning": "brief tech talk"}}
        """
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)


        if data['lean'] == "SKIP" or data['confidence'] < 65:
            await bot.send_message(chat_id=chat_id, text=f"🟡 *SKIP* [{mode_label}] | Conf: {data.get('confidence', 0)}% | RSI: {rsi:.1f}")
            return


        log_prediction(window_start_ts, mode, data['lean'], data['confidence'], current_open)
        market_url = f"https://polyfundr.com/event/btc-updown-5m-{window_start_ts}"


        keyboard = [[
            InlineKeyboardButton("✅ WIN", callback_data='win'),
            InlineKeyboardButton("❌ LOSS", callback_data='loss')
        ]]
        msg = (
            f"🎯 *POLYFUNDR PRO*\n"
            f"🕐 *Mode:* {mode_label}\n"
            f"⏱️ *Window:* {target_win}\n"
            f"💰 *Stake:* ${current_stake:.0f} (Step {step})\n"
            f"📉 *Risk Exposure:* ${total_risk:.0f}\n"
            f"📈 *Direction:* {data['lean']} ({data['confidence']}%)\n"
            f"📊 *RSI:* {rsi:.1f}\n"
            f"📝 *Reason:* {data['reasoning']}\n\n"
            f"[🔗 Open on PolyFundr]({market_url})"
        )
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
        [InlineKeyboardButton("🔄 Reset Martingale ($50)", callback_data='reset')]
    ]
    await update.message.reply_text("🎛️ *PolyFundr Workstation*", parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = get_state()


    if query.data in ['past', 'current', 'future']:
        await run_analysis(context.bot, mode=query.data, chat_id=query.message.chat_id)
    elif query.data == 'links':
        await send_market_links(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'stats':
        await send_stats(context.bot, chat_id=query.message.chat_id)
    elif query.data == 'reset':
        state['step'] = 0
        save_state(state)
        await query.message.reply_text("♻️ Martingale reset to Step 0 ($50).")
    elif query.data == 'win':
        state['step'] = 0
        save_state(state)
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ WIN! Back to $50.")
    elif query.data == 'loss':
        state['step'] = min(state['step'] + 1, 5)
        save_state(state)
        await query.edit_message_text(text=f"{query.message.text}\n\n❌ LOSS. Next Step: ${BASE_STAKE * (2**state['step'])}.")


# === MAIN ===
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("start", menu_command))
    app.add_handler(CallbackQueryHandler(handle_buttons))


    print("🚀 Pro Workstation LIVE (Groq/Llama-3.3)...")
    await app.initialize()
    await app.start()


    async def timer():
        while True:
            now = datetime.now()
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