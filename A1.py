# requirements:
#   python-telegram-bot==21.3
#   aiosqlite==0.20.0
# ------------------------------------------------------------

import asyncio, aiosqlite, datetime as dt, io, csv, os
from datetime import datetime, timezone
UTC = timezone.utc
from dataclasses import dataclass
from typing import Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut
HELP_TEXT = (
    "🧾 Финбот — быстрый учёт доходов и расходов\n\n"
    "Основные команды:\n"
    "• /add <сумма> [заметка] — записать расход\n"
    "Примеры: `/add 250 кофе`, `/add 1200 такси`\n"
    "• /inc <сумма> [заметка] — записать доход\n"
    "Пример: `/inc 20000 зарплата`\n"
    "• /sum — итоги за текущий месяц\n"
    "• /list [YYYY-MM] — последние операции (по умолчанию — текущий месяц)\n"
    "• /export [YYYY-MM] — экспорт CSV за месяц\n\n"
    "Бюджеты:\n"
    "• /budget_set <категория> <сумма> [YYYY-MM] — установить лимит на месяц\n"
    "  Пример: `/budget_set еда 15000`\n"
    "• /budget_view [YYYY-MM] — показать лимиты и траты\n\n"
    "Подсказка: категория берётся из первого слова заметки (например, `еда`, `такси`)."
)

DB = "finbot.db"
DAILY_DIGEST_HOUR_UTC = 18  # 21:00 MSK ≈ 18:00 UTC

# ---------- Time helpers ----------
def iso_utc(d: datetime) -> str:
    """ISO-строка в UTC c 'Z' на конце."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------- Schema ----------
CREATE_SQL = [
    "PRAGMA journal_mode=WAL;",
    """
    CREATE TABLE IF NOT EXISTS users(
                                        id INTEGER PRIMARY KEY,
                                        currency TEXT DEFAULT 'RUB',
                                        digest_hour_utc INTEGER DEFAULT 18,
                                        created_at TEXT DEFAULT (datetime('now'))
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories(
                                             id INTEGER PRIMARY KEY AUTOINCREMENT,
                                             user_id INTEGER,
                                             name TEXT,
                                             type TEXT CHECK(type IN ('expense','income')) DEFAULT 'expense',
        UNIQUE(user_id, name, type)
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions(
                                               id INTEGER PRIMARY KEY AUTOINCREMENT,
                                               user_id INTEGER,
                                               amount REAL,
                                               category_id INTEGER,
                                               ts TEXT,         -- ISO-UTC '...Z'
                                               note TEXT,
                                               type TEXT CHECK(type IN ('expense','income')) NOT NULL
        )
    """,
    "CREATE INDEX IF NOT EXISTS idx_txn_user_ts ON transactions(user_id, ts);",
    """
    CREATE TABLE IF NOT EXISTS budgets(
                                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                                          user_id INTEGER,
                                          category_id INTEGER,
                                          period TEXT, -- 'YYYY-MM'
                                          amount REAL,
                                          UNIQUE(user_id, category_id, period)
        )
    """,
]


async def init_db():
    async with aiosqlite.connect(DB) as db:
        for stmt in CREATE_SQL:
            await db.execute(stmt)
        await db.commit()


async def ensure_user(db, uid: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(id, digest_hour_utc) VALUES (?, ?)",
        (uid, DAILY_DIGEST_HOUR_UTC),
    )
    await db.commit()


# ---------- Parsing ----------
@dataclass
class Parsed:
    amount: Optional[float]
    note: str


def parse_amount_and_note(args) -> Parsed:
    if not args:
        return Parsed(None, "")
    raw = args[0].replace(",", ".")
    try:
        amt = float(raw)
        note = " ".join(args[1:]).strip()
        return Parsed(amt, note)
    except ValueError:
        return Parsed(None, " ".join(args).strip())


# ---------- Catalog / write ----------
async def upsert_category(db, uid: int, name: str, ttype: str) -> int:
    name = name.lower()
    await db.execute(
        """
        INSERT INTO categories(user_id, name, type)
        SELECT ?, ?, ?
            WHERE NOT EXISTS(
            SELECT 1 FROM categories WHERE user_id=? AND name=? AND type=?
        )
        """,
        (uid, name, ttype, uid, name, ttype),
    )
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM categories WHERE user_id=? AND name=? AND type=?",
        (uid, name, ttype),
    )
    row = await cur.fetchone()
    return row[0]


async def add_txn_core(uid: int, amount: float, note: str, ttype: str) -> str:
    """Записать транзакцию, вернуть имя категории."""
    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, uid)
        cat_name = (note.split()[0] if note else "прочее").lower()
        cat_id = await upsert_category(db, uid, cat_name, ttype)
        now_iso = iso_utc(datetime.now(UTC))
        amount_val = amount if ttype == "income" else -abs(amount)
        await db.execute(
            """
            INSERT INTO transactions(user_id, amount, category_id, ts, note, type)
            VALUES(?,?,?,?,?,?)
            """,
            (uid, amount_val, cat_id, now_iso, note, ttype),
        )
        await db.commit()
    return cat_name


# ---------- Period helpers ----------
async def month_bounds_utc(year: int, month: int) -> Tuple[str, str]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
    return iso_utc(start), iso_utc(end)


async def get_totals(db, uid: int, start_iso: str):
    cur = await db.execute(
        """
        SELECT
            ROUND(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END), 2) AS income,
            ROUND(ABS(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END)), 2) AS expense,
            ROUND(SUM(amount), 2) AS net
        FROM transactions WHERE user_id=? AND ts >= ?
        """,
        (uid, start_iso),
    )
    return await cur.fetchone()


# ---------- Safe reply helper ----------
async def safe_reply_text(update: Update, text: str):
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(text)


# ---------- Handlers ----------
async def cmd_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, update.effective_user.id)

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ +100 кофе", callback_data="quick:add:100:кофе")],
            [InlineKeyboardButton("📊 Итоги месяца", callback_data="dash:month")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="dash:help")],
        ]
    )

    await update.message.reply_text(HELP_TEXT, reply_markup=kb, disable_web_page_preview=True)

async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, disable_web_page_preview=True)

async def add_txn(update: Update, context: ContextTypes.DEFAULT_TYPE, ttype: str):
    uid = update.effective_user.id
    parsed = parse_amount_and_note(context.args)
    if parsed.amount is None:
        await safe_reply_text(update, "Укажите сумму: /add 450 еда или /inc 120000 зарплата")
        return
    cat = await add_txn_core(uid, parsed.amount, parsed.note, ttype)
    sign = "+" if ttype == "income" else "-"
    await safe_reply_text(update, f"Записано: {sign}{abs(parsed.amount):.2f} — {parsed.note or cat}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_txn(update, context, "expense")


async def cmd_inc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_txn(update, context, "income")


async def cmd_sum(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    first_dt = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first = iso_utc(first_dt)
    async with aiosqlite.connect(DB) as db:
        row = await get_totals(db, uid, first)
    inc, exp, net = row or (0, 0, 0)
    await safe_reply_text(update, f"За месяц: доход {inc:.2f}, расход {exp:.2f}, чистый поток {net:.2f}")


# ---- Budgets ----
async def resolve_category(db, uid: int, name: str) -> Optional[int]:
    name = name.lower()
    cur = await db.execute(
        "SELECT id FROM categories WHERE user_id=? AND name=? AND type='expense'",
        (uid, name),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def cmd_budget_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /budget_set <category> <amount> [YYYY-MM]
    """
    if len(context.args) < 2:
        await safe_reply_text(update, "Формат: /budget_set <категория> <сумма> [YYYY-MM]")
        return
    uid = update.effective_user.id
    cat_name = context.args[0].lower()
    try:
        amount = float(context.args[1].replace(",", "."))
    except ValueError:
        await safe_reply_text(update, "Укажите корректную сумму, например 15000")
        return
    period = context.args[2] if len(context.args) >= 3 else datetime.now(UTC).strftime("%Y-%m")
    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, uid)
        cat_id = await resolve_category(db, uid, cat_name)
        if not cat_id:
            cat_id = await upsert_category(db, uid, cat_name, "expense")
        await db.execute(
            """
            INSERT INTO budgets(user_id, category_id, period, amount)
            VALUES(?,?,?,?)
                ON CONFLICT(user_id, category_id, period) DO UPDATE SET amount=excluded.amount
            """,
            (uid, cat_id, period, amount),
        )
        await db.commit()
    await safe_reply_text(update, f"Бюджет по '{cat_name}' на {period}: {amount:.2f} установлен")


async def spent_in_period(db, uid: int, cat_id: int, period: str) -> float:
    start_iso, end_iso = await month_bounds_utc(int(period[:4]), int(period[5:7]))
    cur = await db.execute(
        """
        SELECT ABS(COALESCE(SUM(amount),0)) FROM transactions
        WHERE user_id=? AND category_id=? AND type='expense' AND ts >= ? AND ts < ?
        """,
        (uid, cat_id, start_iso, end_iso),
    )
    row = await cur.fetchone()
    return float(row[0] if row and row[0] is not None else 0.0)


async def cmd_budget_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    period = context.args[0] if context.args else datetime.now(UTC).strftime("%Y-%m")
    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, uid)
        cur = await db.execute(
            """
            SELECT c.name, b.amount, (
                SELECT ABS(COALESCE(SUM(t.amount),0)) FROM transactions t
                WHERE t.user_id=b.user_id AND t.category_id=b.category_id AND t.type='expense'
                  AND t.ts >= ? AND t.ts < ?
            ) as spent
            FROM budgets b
                     JOIN categories c ON c.id=b.category_id
            WHERE b.user_id=? AND b.period=?
            ORDER BY c.name
            """,
            (*(await month_bounds_utc(int(period[:4]), int(period[5:7]))), uid, period),
        )
        rows = await cur.fetchall()
    if not rows:
        await safe_reply_text(update, "Бюджеты не заданы. Используйте /budget_set")
        return
    lines = [f"Бюджеты на {period}:"]
    for name, limit, spent in rows:
        remain = float(limit) - float(spent)
        pct = (float(spent) / float(limit) * 100) if float(limit) > 0 else 0
        lines.append(f"• {name}: {spent:.2f}/{float(limit):.2f} (ост {remain:.2f}, {pct:.0f}%)")
    await safe_reply_text(update, "\n".join(lines))


# ---- List & Export ----
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # 1) Надёжно разбираем период аргументом [YYYY-MM], иначе берём текущий месяц
    if context.args:
        raw = (context.args[0] or "").strip()
        try:
            y = int(raw[:4])
            m = int(raw[5:7])
            if len(raw) == 7 and raw[4] == "-" and 1 <= m <= 12:
                period = f"{y:04d}-{m:02d}"
            else:
                period = datetime.now(UTC).strftime("%Y-%m")
        except Exception:
            period = datetime.now(UTC).strftime("%Y-%m")
    else:
        period = datetime.now(UTC).strftime("%Y-%m")

    start_iso, end_iso = await month_bounds_utc(int(period[:4]), int(period[5:7]))

    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, uid)
        # 2) Явно указываем алиасы колонок из таблицы transactions (t) — убирает 'ambiguous column name: type'
        cur = await db.execute(
            """
            SELECT t.ts,
                   t.amount,
                   c.name AS category,
                   COALESCE(t.note,'') AS note,
                   t.type
            FROM transactions t
                     JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = ? AND t.ts >= ? AND t.ts < ?
            ORDER BY t.ts DESC
                LIMIT 20
            """,
            (uid, start_iso, end_iso),
        )
        rows = await cur.fetchall()

    if not rows:
        await safe_reply_text(update, "Записей за период нет")
        return

    lines = [f"Последние операции за {period}:"]
    for ts, amt, cname, note, ttype in rows:
        sign = "+" if ttype == "income" else "-"
        ts_str = ts if isinstance(ts, str) else str(ts)
        lines.append(f"{ts_str[:16]} {sign}{abs(amt):.2f} {cname} — {note or ''}")

    await safe_reply_text(update, "\n".join(lines))



async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # 1) Надёжно разобрать период [YYYY-MM], иначе — текущий месяц
    if context.args:
        raw = (context.args[0] or "").strip()
        try:
            y = int(raw[:4]); m = int(raw[5:7])
            period = f"{y:04d}-{m:02d}" if len(raw) == 7 and raw[4] == "-" and 1 <= m <= 12 else datetime.now(UTC).strftime("%Y-%m")
        except Exception:
            period = datetime.now(UTC).strftime("%Y-%m")
    else:
        period = datetime.now(UTC).strftime("%Y-%m")

    start_iso, end_iso = await month_bounds_utc(int(period[:4]), int(period[5:7]))

    async with aiosqlite.connect(DB) as db:
        await ensure_user(db, uid)
        # 2) Явно указываем алиасы: t.* / c.*
        cur = await db.execute(
            """
            SELECT t.ts,
                   t.type,                      -- явный источник
                   ABS(t.amount)  AS amount,
                   c.name         AS category,
                   COALESCE(t.note,'') AS note
            FROM transactions t
                     JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = ? AND t.ts >= ? AND t.ts < ?
            ORDER BY t.ts ASC
            """,
            (uid, start_iso, end_iso),
        )
        rows = await cur.fetchall()

    if not rows:
        await safe_reply_text(update, f"За {period} записей нет")
        return

    # 3) Готовим CSV в памяти → BytesIO (file-like)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "type", "amount", "category", "note"])
    for r in rows:
        writer.writerow(r)

    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    filename = f"finbot_{uid}_{period}.csv"

    # Можно писать либо в чат, либо через safe_reply_text + send_document
    await update.effective_chat.send_document(
        document=InputFile(data, filename),
        caption=f"Экспорт за {period}"
    )



# ---- Inline callbacks ----
async def on_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if parts[0] == "quick" and parts[1] == "add":
        amt = float(parts[2])
        note = parts[3]
        uid = q.from_user.id
        cat = await add_txn_core(uid, amt, note, "expense")
        await q.message.reply_text(f"Записано: -{amt:.2f} — {note or cat}")
    elif parts[0] == "dash" and parts[1] == "month":
        uid = q.from_user.id
        first = iso_utc(datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        async with aiosqlite.connect(DB) as db:
            row = await get_totals(db, uid, first)
        inc, exp, net = row or (0, 0, 0)
        await q.edit_message_text(f"Месяц — Доход: {inc:.2f}\nРасход: {exp:.2f}\nЧистый поток: {net:.2f}")
    elif parts[0] == "dash" and parts[1] == "help":
        await q.edit_message_text(HELP_TEXT, disable_web_page_preview=True)


# ---- Daily digest ----
async def send_daily_digest(app: Application):
    while True:
        now = datetime.now(UTC)                 # работаем с datetime, не со строкой
        await asyncio.sleep(60 - now.second)    # шаг до следующей минуты
        now = datetime.now(UTC)
        if now.minute != 0:
            continue

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT id, digest_hour_utc FROM users")
            users = await cur.fetchall()

        for uid, hour in users:
            if hour is None:
                hour = DAILY_DIGEST_HOUR_UTC
            if now.hour == int(hour):
                first_month = iso_utc(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
                y0 = iso_utc((now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
                y1 = iso_utc(now.replace(hour=0, minute=0, second=0, microsecond=0))
                async with aiosqlite.connect(DB) as db:
                    m = await get_totals(db, uid, first_month)
                    d = await get_totals(db, uid, y0)
                inc_m, exp_m, net_m = m or (0, 0, 0)
                inc_d, exp_d, net_d = d or (0, 0, 0)
                text = (
                    "Дайджест:\n"
                    f"Сегодня: доход {inc_d:.2f}, расход {exp_d:.2f}, чистый {net_d:.2f}\n"
                    f"Месяц: доход {inc_m:.2f}, расход {exp_m:.2f}, чистый {net_m:.2f}"
                )
                try:
                    await app.bot.send_message(chat_id=uid, text=text)
                except (Forbidden, BadRequest):
                    # пользователь мог заблокировать бота / невалидный chat_id — игнорируем
                    pass
                except (RetryAfter, TimedOut):
                    # можно добавить логирование/повтор
                    pass


# ---- Bootstrap ----
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or "<TELEGRAM_BOT_TOKEN>"
    app = Application.builder().token("7898079955:AAEgZDclIirToJvWxvCCcK8yMId5e2YKhHE").build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("inc", cmd_inc))
    app.add_handler(CommandHandler("sum", cmd_sum))
    app.add_handler(CommandHandler("budget_set", cmd_budget_set))
    app.add_handler(CommandHandler("budget_view", cmd_budget_view))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.create_task(send_daily_digest(app))

    app.run_polling()  # блокирующий вызов


if __name__ == "__main__":
    main()
