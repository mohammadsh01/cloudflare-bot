from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import os
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ALLOWED_USER_ID = int(os.getenv("CHAT_ID"))

# خودکار خوندن اکانت‌ها از متغیرهای محیطی
def load_accounts():
    accounts = []
    i = 1
    while True:
        if i == 1:
            token = os.getenv("CLOUDFLARE_TOKEN")
            account_id = os.getenv("ACCOUNT_ID")
        else:
            token = os.getenv(f"CLOUDFLARE_TOKEN_{i}")
            account_id = os.getenv(f"ACCOUNT_ID_{i}")

        if not token or not account_id:
            break

        accounts.append({
            "token": token,
            "id": account_id,
            "label": f"اکانت {i}"
        })
        i += 1
    return accounts

ACCOUNTS = load_accounts()

REQUEST_THRESHOLD = 100000
ERROR_RATE_THRESHOLD = 40
alerted_accounts = set()
silence_until = None

COST_PER_MILLION_REQUESTS = 0.30
COST_PER_MILLION_CPU_MS = 0.02


def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


def is_silenced():
    return silence_until and datetime.utcnow() < silence_until


def health_score(requests, errors):
    if requests == 0:
        return "⚪️", 100
    rate = (errors / requests) * 100
    score = round(100 - rate, 1)
    if rate < 5:
        emoji = "🟢"
    elif rate < 20:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return emoji, score


def calculate_cost(total_requests, cpu_p50_us):
    req_cost = (total_requests / 1_000_000) * COST_PER_MILLION_REQUESTS
    cpu_ms = (cpu_p50_us / 1000) * total_requests
    cpu_cost = (cpu_ms / 1_000_000) * COST_PER_MILLION_CPU_MS
    return round(req_cost + cpu_cost, 4)


def progress_bar(percent, length=12):
    filled = int(min(percent, 100) / 100 * length)
    return "▓" * filled + "░" * (length - filled)


def get_workers_stats(api_token, account_id, yesterday=False):
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    r = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts", headers=headers)
    workers = r.json().get("result", []) or []
    results = []

    if yesterday:
        now = datetime.utcnow()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        start = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for w in workers:
        name = w.get("id", "unknown")
        query = """
        { viewer { accounts(filter: {accountTag: "%s"}) {
            workersInvocationsAdaptive(filter: {scriptName: "%s", datetime_geq: "%s", datetime_leq: "%s"}, limit: 100) {
                sum { requests errors } quantiles { cpuTimeP50 cpuTimeP99 }
            }}}}""" % (account_id, name, start, end)

        gql = requests.post("https://api.cloudflare.com/client/v4/graphql", headers=headers, json={"query": query}).json()
        try:
            inv = gql["data"]["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
            if inv:
                reqs = sum(i["sum"].get("requests", 0) for i in inv)
                errors = sum(i["sum"].get("errors", 0) for i in inv)
                cpu_p50 = inv[0]["quantiles"].get("cpuTimeP50", 0)
                cpu_p99 = max(i["quantiles"].get("cpuTimeP99", 0) for i in inv)
            else:
                reqs = errors = cpu_p50 = cpu_p99 = 0
        except:
            reqs = errors = cpu_p50 = cpu_p99 = 0

        results.append({"name": name, "created": w.get("created_on", ""), "usage_model": w.get("usage_model", ""), "requests": reqs, "errors": errors, "cpu_p50": cpu_p50, "cpu_p99": cpu_p99})
    return results


def get_workers_stats_range(api_token, account_id, days=7):
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    r = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts", headers=headers)
    workers = r.json().get("result", []) or []
    results = []
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for w in workers:
        name = w.get("id", "unknown")
        query = """
        { viewer { accounts(filter: {accountTag: "%s"}) {
            workersInvocationsAdaptive(filter: {scriptName: "%s", datetime_geq: "%s", datetime_leq: "%s"}, limit: 100) {
                sum { requests errors } quantiles { cpuTimeP50 cpuTimeP99 }
            }}}}""" % (account_id, name, start, end)

        gql = requests.post("https://api.cloudflare.com/client/v4/graphql", headers=headers, json={"query": query}).json()
        try:
            inv = gql["data"]["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
            if inv:
                reqs = sum(i["sum"].get("requests", 0) for i in inv)
                errors = sum(i["sum"].get("errors", 0) for i in inv)
                cpu_p50 = inv[0]["quantiles"].get("cpuTimeP50", 0)
                cpu_p99 = max(i["quantiles"].get("cpuTimeP99", 0) for i in inv)
            else:
                reqs = errors = cpu_p50 = cpu_p99 = 0
        except:
            reqs = errors = cpu_p50 = cpu_p99 = 0

        results.append({"name": name, "requests": reqs, "errors": errors, "cpu_p50": cpu_p50, "cpu_p99": cpu_p99})
    return results


def get_account_usage(api_token, account_id):
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    today = datetime.utcnow().strftime("%Y-%m-%d")
    query = """
    { viewer { accounts(filter: {accountTag: "%s"}) {
        workersInvocationsAdaptive(filter: {datetime_geq: "%sT00:00:00Z", datetime_leq: "%sT23:59:59Z"}, limit: 100) {
            sum { requests errors }
        }}}}""" % (account_id, today, today)

    gql = requests.post("https://api.cloudflare.com/client/v4/graphql", headers=headers, json={"query": query}).json()
    try:
        inv = gql["data"]["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
        return (sum(i["sum"].get("requests", 0) for i in inv), sum(i["sum"].get("errors", 0) for i in inv)) if inv else (0, 0)
    except:
        return 0, 0


def build_stats_message(workers, title="📊 آمار ورکر", prev_workers=None):
    msg = f"╔{'═' * 23}╗\n║  {title}\n╚{'═' * 23}╝\n\n"
    msg += f"📦 تعداد ورکرها: {len(workers)}\n\n"

    for w in workers:
        emoji, score = health_score(w["requests"], w["errors"])
        trend = ""
        if prev_workers:
            prev = next((p for p in prev_workers if p["name"] == w["name"]), None)
            if prev:
                trend = " 📈" if w["requests"] > prev["requests"] else " 📉" if w["requests"] < prev["requests"] else ""

        cost = calculate_cost(w["requests"], w["cpu_p50"])
        bar = progress_bar(score)

        msg += f"┌──────────────────────\n"
        msg += f"│ 🔧 {w['name']}\n"
        msg += f"├──────────────────────\n"
        msg += f"│ {emoji} [{bar}] {score}%\n"
        msg += f"│ 🕒 {w['created']}\n"
        msg += f"│ ⚙️  مدل: {w['usage_model']}\n"
        msg += f"│ 📨 Requests: {w['requests']:,}{trend}\n"
        msg += f"│ ❌ Errors: {w['errors']:,}\n"
        msg += f"│ 🖥 CPU p50: {w['cpu_p50']} µs\n"
        msg += f"│ 🖥 CPU p99: {w['cpu_p99']} µs\n"
        msg += f"└ 💰 هزینه: ${cost}\n\n"

    return msg


def compare_inline_keyboard():
    keyboard = []
    row = []
    for i, acc in enumerate(ACCOUNTS):
        row.append(InlineKeyboardButton(f"💎 {acc['label']}", callback_data=f"cmp_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔀 همه اکانت‌ها با هم", callback_data="cmp_all")])
    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard():
    silence_text = "🔔 هشدارها روشن" if not is_silenced() else f"🔕 سکوت تا {silence_until.strftime('%H:%M')}"
    keyboard = []
    row = []
    for i, acc in enumerate(ACCOUNTS):
        row.append(KeyboardButton(f"📊 {acc['label']}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([KeyboardButton("🔀 مقایسه"), KeyboardButton("📅 گزارش هفتگی")])
    keyboard.append([KeyboardButton("🌅 گزارش دیروز"), KeyboardButton("🏅 امتیاز ماهانه")])
    keyboard.append([KeyboardButton(silence_text)])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        f"👋 سلام! {len(ACCOUNTS)} اکانت پیدا شد.\nیه گزینه انتخاب کن:",
        reply_markup=main_menu_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmp_all":
        msg = f"╔{'═' * 23}╗\n║  🔀 مقایسه همه اکانت‌ها\n╚{'═' * 23}╝\n\n"
        msg += f"🗓 {datetime.utcnow().strftime('%Y-%m-%d')} (UTC)\n\n"
        for acc in ACCOUNTS:
            reqs, errors = get_account_usage(acc["token"], acc["id"])
            limit = 100_000
            percent = round((reqs / limit) * 100, 1)
            remaining = limit - reqs
            st = "🟢" if percent < 50 else "🟡" if percent < 80 else "🔴"
            bar = progress_bar(percent)
            msg += f"┌──────────────────────\n"
            msg += f"│ 🔹 {acc['label']}\n"
            msg += f"├──────────────────────\n"
            msg += f"│ {st} [{bar}] {percent}%\n"
            msg += f"│ 📨 امروز: {reqs:,} / {limit:,}\n"
            msg += f"│ ❌ خطا: {errors:,}\n"
            msg += f"└ ✅ باقیمانده: {remaining:,}\n\n"
        await query.edit_message_text(msg, reply_markup=compare_inline_keyboard())

    elif data.startswith("cmp_"):
        index = int(data.split("_")[1])
        acc = ACCOUNTS[index]
        reqs, errors = get_account_usage(acc["token"], acc["id"])
        limit = 100_000
        percent = round((reqs / limit) * 100, 1)
        remaining = limit - reqs
        st = "🟢" if percent < 50 else "🟡" if percent < 80 else "🔴"
        bar = progress_bar(percent)
        msg = f"╔{'═' * 23}╗\n║  📊 {acc['label']}\n╚{'═' * 23}╝\n\n"
        msg += f"🗓 {datetime.utcnow().strftime('%Y-%m-%d')} (UTC)\n\n"
        msg += f"┌──────────────────────\n"
        msg += f"│ 🔹 {acc['label']}\n"
        msg += f"├──────────────────────\n"
        msg += f"│ {st} [{bar}] {percent}%\n"
        msg += f"│ 📨 امروز: {reqs:,} / {limit:,}\n"
        msg += f"│ ❌ خطا: {errors:,}\n"
        msg += f"└ ✅ باقیمانده: {remaining:,}\n\n"
        await query.edit_message_text(msg, reply_markup=compare_inline_keyboard())


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global silence_until

    if not is_allowed(update):
        return

    text = update.message.text

    # چک کردن دکمه‌های اکانت
    account_map = {f"📊 {acc['label']}": i for i, acc in enumerate(ACCOUNTS)}

    try:
        if text in account_map:
            index = account_map[text]
            acc = ACCOUNTS[index]
            loading = await update.message.reply_text("⏳ در حال دریافت اطلاعات...")
            workers = get_workers_stats(acc["token"], acc["id"])
            prev = get_workers_stats(acc["token"], acc["id"], yesterday=True)
            await loading.delete()
            await update.message.reply_text(build_stats_message(workers, f"📊 آمار {acc['label']}", prev))

        elif text == "🔀 مقایسه":
            await update.message.reply_text("یه اکانت انتخاب کن:", reply_markup=compare_inline_keyboard())

        elif text == "📅 گزارش هفتگی":
            loading = await update.message.reply_text("⏳ در حال دریافت گزارش هفتگی...")
            msg = f"╔{'═' * 23}╗\n║  📅 گزارش هفتگی\n╚{'═' * 23}╝\n\n"
            for acc in ACCOUNTS:
                workers = get_workers_stats_range(acc["token"], acc["id"], days=7)
                total_reqs = sum(w["requests"] for w in workers)
                total_errors = sum(w["errors"] for w in workers)
                max_cpu = max((w["cpu_p99"] for w in workers), default=0)
                total_cpu_p50 = sum(w["cpu_p50"] for w in workers)
                emoji, score = health_score(total_reqs, total_errors)
                cost = calculate_cost(total_reqs, total_cpu_p50 / max(len(workers), 1))
                bar = progress_bar(score)
                msg += f"┌──────────────────────\n"
                msg += f"│ 🔹 {acc['label']}\n"
                msg += f"├──────────────────────\n"
                msg += f"│ {emoji} [{bar}] {score}%\n"
                msg += f"│ 📨 Requests: {total_reqs:,}\n"
                msg += f"│ ❌ Errors: {total_errors:,}\n"
                msg += f"│ 🖥 CPU p99: {max_cpu} µs\n"
                msg += f"└ 💰 هزینه: ${cost}\n\n"
            await loading.delete()
            await update.message.reply_text(msg)

        elif text == "🌅 گزارش دیروز":
            loading = await update.message.reply_text("⏳ در حال دریافت گزارش دیروز...")
            msg = f"╔{'═' * 23}╗\n║  🌅 گزارش دیروز\n╚{'═' * 23}╝\n\n"
            for acc in ACCOUNTS:
                workers = get_workers_stats(acc["token"], acc["id"], yesterday=True)
                total_reqs = sum(w["requests"] for w in workers)
                total_errors = sum(w["errors"] for w in workers)
                emoji, score = health_score(total_reqs, total_errors)
                cost = calculate_cost(total_reqs, sum(w["cpu_p50"] for w in workers) / max(len(workers), 1))
                bar = progress_bar(score)
                msg += f"┌──────────────────────\n"
                msg += f"│ 🔹 {acc['label']}\n"
                msg += f"├──────────────────────\n"
                msg += f"│ {emoji} [{bar}] {score}%\n"
                msg += f"│ 📨 Requests: {total_reqs:,}\n"
                msg += f"│ ❌ Errors: {total_errors:,}\n"
                msg += f"└ 💰 هزینه: ${cost}\n\n"
            await loading.delete()
            await update.message.reply_text(msg)

        elif text == "🏅 امتیاز ماهانه":
            loading = await update.message.reply_text("⏳ در حال محاسبه امتیاز ماهانه...")
            msg = f"╔{'═' * 23}╗\n║  🏅 امتیاز ماهانه\n║  📆 ۳۰ روز گذشته\n╚{'═' * 23}╝\n\n"
            for acc in ACCOUNTS:
                workers = get_workers_stats_range(acc["token"], acc["id"], days=30)
                total_reqs = sum(w["requests"] for w in workers)
                total_errors = sum(w["errors"] for w in workers)
                emoji, score = health_score(total_reqs, total_errors)
                cost = calculate_cost(total_reqs, sum(w["cpu_p50"] for w in workers) / max(len(workers), 1))
                bar = progress_bar(score)
                grade = "A+ 🏆" if score >= 95 else "A 🥇" if score >= 85 else "B 🥈" if score >= 70 else "C 🥉" if score >= 50 else "D ⚠️"
                msg += f"┌──────────────────────\n"
                msg += f"│ 🔹 {acc['label']}\n"
                msg += f"├──────────────────────\n"
                msg += f"│ {emoji} [{bar}] {score}%\n"
                msg += f"│ 🎓 درجه: {grade}\n"
                msg += f"│ 📨 Requests: {total_reqs:,}\n"
                msg += f"│ ❌ Errors: {total_errors:,}\n"
                msg += f"└ 💰 هزینه ماهانه: ${cost}\n\n"
            await loading.delete()
            await update.message.reply_text(msg)

        elif text.startswith("🔔") or text.startswith("🔕"):
            if is_silenced():
                silence_until = None
                await update.message.reply_text("🔔 هشدارها دوباره روشن شد!", reply_markup=main_menu_keyboard())
            else:
                silence_until = datetime.utcnow() + timedelta(hours=2)
                await update.message.reply_text(
                    f"🔕 هشدارها تا ساعت {silence_until.strftime('%H:%M')} UTC خاموش شد.",
                    reply_markup=main_menu_keyboard()
                )

    except Exception as e:
        await update.message.reply_text(f"⚠️ خطا:\n{e}")


async def send_daily_report(app):
    try:
        for acc in ACCOUNTS:
            workers = get_workers_stats(acc["token"], acc["id"], yesterday=True)
            msg = build_stats_message(workers, title=f"🌅 گزارش دیروز {acc['label']}")
            await app.bot.send_message(chat_id=CHAT_ID, text=msg)
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ خطا:\n{e}")


async def send_weekly_report(app):
    try:
        msg = f"╔{'═' * 23}╗\n║  📅 گزارش هفتگی\n╚{'═' * 23}╝\n\n"
        for acc in ACCOUNTS:
            workers = get_workers_stats_range(acc["token"], acc["id"], days=7)
            total_reqs = sum(w["requests"] for w in workers)
            total_errors = sum(w["errors"] for w in workers)
            max_cpu = max((w["cpu_p99"] for w in workers), default=0)
            total_cpu_p50 = sum(w["cpu_p50"] for w in workers)
            emoji, score = health_score(total_reqs, total_errors)
            cost = calculate_cost(total_reqs, total_cpu_p50 / max(len(workers), 1))
            bar = progress_bar(score)
            msg += f"┌──────────────────────\n"
            msg += f"│ 🔹 {acc['label']}\n"
            msg += f"├──────────────────────\n"
            msg += f"│ {emoji} [{bar}] {score}%\n"
            msg += f"│ 📨 Requests: {total_reqs:,}\n"
            msg += f"│ ❌ Errors: {total_errors:,}\n"
            msg += f"│ 🖥 CPU p99: {max_cpu} µs\n"
            msg += f"└ 💰 هزینه: ${cost}\n\n"
        await app.bot.send_message(chat_id=CHAT_ID, text=msg)
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ خطا:\n{e}")


async def check_alerts(app):
    if is_silenced():
        return

    try:
        for acc in ACCOUNTS:
            workers = get_workers_stats(acc["token"], acc["id"])
            total_reqs = sum(w["requests"] for w in workers)
            total_errors = sum(w["errors"] for w in workers)
            key = acc["label"]

            if total_reqs > 0:
                error_rate = (total_errors / total_reqs) * 100
                if error_rate >= ERROR_RATE_THRESHOLD and key not in alerted_accounts:
                    alerted_accounts.add(key)
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🚨 هشدار! {acc['label']}\n"
                             f"❌ نرخ خطا: {round(error_rate, 1)}%\n"
                             f"📨 Requests: {total_reqs:,}\n"
                             f"❌ Errors: {total_errors:,}"
                    )
                elif error_rate < ERROR_RATE_THRESHOLD and key in alerted_accounts:
                    alerted_accounts.discard(key)
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"✅ {acc['label']} برگشت به حالت عادی\n"
                             f"❌ نرخ خطا: {round(error_rate, 1)}%"
                    )

            req_key = f"req_{key}"
            if total_reqs >= REQUEST_THRESHOLD and req_key not in alerted_accounts:
                alerted_accounts.add(req_key)
                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🚨 {acc['label']} به {total_reqs:,} ریکوست رسید!"
                )
            elif total_reqs < REQUEST_THRESHOLD and req_key in alerted_accounts:
                alerted_accounts.discard(req_key)

    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ خطا در بررسی هشدارها:\n{e}")


if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    async def post_init(application):
        scheduler = AsyncIOScheduler()
        scheduler.add_job(send_daily_report, "cron", hour=8, minute=30, args=[application])
        scheduler.add_job(send_weekly_report, "cron", day_of_week="sat", hour=9, minute=0, args=[application])
        scheduler.add_job(check_alerts, "interval", minutes=30, args=[application])
        scheduler.start()

    app.post_init = post_init
    app.run_polling()
