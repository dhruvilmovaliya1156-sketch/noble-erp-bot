import os
import asyncio
import logging
import sqlite3
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError

# ================= CONFIG =================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Optional: for AI assistant
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SESSION_TIMEOUT_MINUTES = 30
ALERT_CHECK_INTERVAL = 3600  # seconds between scheduled alert checks
user_sessions: Dict[int, Dict] = {}

# ================= STATES =================

class LoginStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()

class AskStates(StatesGroup):
    waiting_for_question = State()

# ================= DATABASE =================

DB_PATH = "/tmp/erp_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id     INTEGER PRIMARY KEY,
            username    TEXT,
            password    TEXT,
            created_at  TEXT,
            last_login  TEXT,
            alerts_on   INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER,
            page_name   TEXT,
            data_json   TEXT,
            captured_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER,
            alert_type  TEXT,
            message     TEXT,
            sent_at     TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def save_credentials(chat_id: int, username: str, password: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO users (chat_id, username, password, created_at, last_login, alerts_on)
        VALUES (?, ?, ?, ?, ?, COALESCE((SELECT alerts_on FROM users WHERE chat_id=?), 1))
    """, (chat_id, username, password, datetime.now().isoformat(), datetime.now().isoformat(), chat_id))
    conn.commit()
    conn.close()

def get_credentials(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row  # (username, password) or None

def get_all_users_with_alerts():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT chat_id, username, password FROM users WHERE alerts_on=1").fetchall()
    conn.close()
    return rows

def save_snapshot(chat_id: int, page_name: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO snapshots (chat_id, page_name, data_json, captured_at)
        VALUES (?, ?, ?, ?)
    """, (chat_id, page_name, json.dumps(data), datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_last_snapshot(chat_id: int, page_name: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT data_json FROM snapshots
        WHERE chat_id=? AND page_name=?
        ORDER BY captured_at DESC LIMIT 1 OFFSET 1
    """, (chat_id, page_name)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def toggle_alerts(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    current = conn.execute("SELECT alerts_on FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    new_val = 0 if (current and current[0] == 1) else 1
    conn.execute("UPDATE users SET alerts_on=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    return bool(new_val)

def log_alert(chat_id: int, alert_type: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO alert_log (chat_id, alert_type, message, sent_at) VALUES (?, ?, ?, ?)",
                 (chat_id, alert_type, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ================= BROWSER =================

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.screenshot_counter = 0

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("Browser started")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped")

    async def new_context(self):
        return await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )

    async def save_screenshot(self, page, prefix="shot"):
        self.screenshot_counter += 1
        path = f"/tmp/{prefix}_{self.screenshot_counter}.png"
        await page.screenshot(path=path, full_page=True)
        return path


browser_manager = BrowserManager()

# ================= ERP PAGES =================

PAGES = {
    "🏠 Dashboard":    "https://noble.icrp.in/academic/Student-cp/Home_student.aspx",
    "📋 Attendance":   "https://noble.icrp.in/academic/Student-cp/Form_Students_Lecture_Wise_Attendance.aspx",
    "👤 Profile":      "https://noble.icrp.in/academic/Student-cp/Students_profile.aspx",
    "📚 Academics":    "https://noble.icrp.in/academic/Student-cp/Form_Display_Division_TimeTableS.aspx",
    "💰 Fees":         "https://noble.icrp.in/academic/Student-cp/Form_students_pay_fees.aspx",
    "📝 Exam":         "https://noble.icrp.in/academic/Student-cp/Form_Students_Exam_Result_Login.aspx",
    "📅 Holidays":     "https://noble.icrp.in/academic/Student-cp/List_Students_College_Wise_Holidays.aspx",
    "🎓 Convocation":  "https://noble.icrp.in/academic/Student-cp/Form_student_Convocation_Registration.aspx",
}

PAGE_KEYS = list(PAGES.keys())
PAGE_VALS = list(PAGES.values())


def get_menu():
    rows = []
    for i in range(0, len(PAGE_KEYS), 4):
        row = [
            InlineKeyboardButton(text=PAGE_KEYS[j], callback_data=f"page_{j}")
            for j in range(i, min(i + 4, len(PAGE_KEYS)))
        ]
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="📸 Screenshot",  callback_data="screenshot"),
        InlineKeyboardButton(text="📊 Smart Data",  callback_data="smartdata"),
        InlineKeyboardButton(text="🤖 Ask AI",      callback_data="ask_ai"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔔 Toggle Alerts", callback_data="toggle_alerts"),
        InlineKeyboardButton(text="🚪 Logout",         callback_data="logout"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back to Menu", callback_data="show_menu")]
    ])

# ================= SESSION HELPERS =================

def is_expired(session):
    return datetime.now() > session["expires"]

def refresh_session(chat_id):
    if chat_id in user_sessions:
        user_sessions[chat_id]["expires"] = datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES)

async def close_session(chat_id):
    session = user_sessions.pop(chat_id, None)
    if session:
        try:
            await session["page"].close()
            await session["context"].close()
        except Exception:
            pass
        logger.info(f"Session closed for {chat_id}")

async def verify_logged_in(page) -> bool:
    try:
        return await page.query_selector("a:has-text('Logout')") is not None
    except Exception:
        return False

# ================= ERP DATA EXTRACTORS =================

async def extract_attendance(page) -> dict:
    """Parse attendance table into structured data."""
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("📋 Attendance")], wait_until="networkidle")
        rows = await page.query_selector_all("table tr")
        subjects = []
        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if len(cells) >= 4:
                texts = [await c.inner_text() for c in cells]
                subjects.append({
                    "subject": texts[0].strip(),
                    "total":   texts[1].strip(),
                    "present": texts[2].strip(),
                    "percent": texts[3].strip(),
                })
        return {"subjects": subjects, "extracted_at": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}

async def extract_profile(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("👤 Profile")], wait_until="networkidle")
        data = {}
        labels = await page.query_selector_all("td.label, th")
        for label in labels:
            text = (await label.inner_text()).strip()
            if ":" in text:
                k, _, v = text.partition(":")
                data[k.strip()] = v.strip()
        return {"profile": data, "extracted_at": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}

async def extract_fees(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("💰 Fees")], wait_until="networkidle")
        rows = await page.query_selector_all("table tr")
        fees = []
        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if len(cells) >= 3:
                texts = [await c.inner_text() for c in cells]
                fees.append({
                    "description": texts[0].strip(),
                    "amount":      texts[1].strip() if len(texts) > 1 else "",
                    "status":      texts[2].strip() if len(texts) > 2 else "",
                })
        return {"fees": fees, "extracted_at": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}

async def extract_exam(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("📝 Exam")], wait_until="networkidle")
        rows = await page.query_selector_all("table tr")
        results = []
        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if len(cells) >= 3:
                texts = [await c.inner_text() for c in cells]
                results.append({
                    "subject": texts[0].strip(),
                    "marks":   texts[1].strip() if len(texts) > 1 else "",
                    "grade":   texts[2].strip() if len(texts) > 2 else "",
                })
        return {"results": results, "extracted_at": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}

def format_attendance_message(data: dict) -> str:
    if "error" in data:
        return f"❌ Could not extract attendance: {data['error']}"
    subjects = data.get("subjects", [])
    if not subjects:
        return "📋 No attendance data found."
    lines = ["📋 *Attendance Summary*\n"]
    for s in subjects:
        pct = s.get("percent", "?")
        try:
            pct_val = float(re.sub(r"[^\d.]", "", pct))
            emoji = "✅" if pct_val >= 75 else "⚠️" if pct_val >= 60 else "❌"
        except Exception:
            emoji = "📌"
        lines.append(f"{emoji} *{s['subject']}*\n   {s['present']}/{s['total']} — {pct}")
    return "\n".join(lines)

def format_fees_message(data: dict) -> str:
    if "error" in data:
        return f"❌ Could not extract fees: {data['error']}"
    fees = data.get("fees", [])
    if not fees:
        return "💰 No fee data found."
    lines = ["💰 *Fee Details*\n"]
    for f in fees:
        status_emoji = "✅" if "paid" in f["status"].lower() else "❗"
        lines.append(f"{status_emoji} {f['description']}: ₹{f['amount']} — {f['status']}")
    return "\n".join(lines)

def format_exam_message(data: dict) -> str:
    if "error" in data:
        return f"❌ Could not extract results: {data['error']}"
    results = data.get("results", [])
    if not results:
        return "📝 No exam results found."
    lines = ["📝 *Exam Results*\n"]
    for r in results:
        lines.append(f"📌 *{r['subject']}* — {r['marks']} ({r['grade']})")
    return "\n".join(lines)

# ================= AI ASSISTANT =================

async def ask_erp_ai(question: str, context_data: dict) -> str:
    """Use OpenAI to answer questions about extracted ERP data."""
    if not OPENAI_API_KEY:
        return "🤖 AI assistant not configured. Set OPENAI_API_KEY in .env to enable this feature."

    try:
        import aiohttp
        context_str = json.dumps(context_data, indent=2)
        prompt = f"""You are an ERP assistant for a college student portal.
Here is the student's current data:
{context_str}

Answer this question concisely and helpfully:
{question}"""

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                }
            ) as resp:
                result = await resp.json()
                return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"❌ AI error: {str(e)}"

# ================= AUTO-LOGIN HELPER =================

async def auto_login(chat_id: int) -> Optional[dict]:
    """Restore session using saved credentials."""
    creds = get_credentials(chat_id)
    if not creds:
        return None
    username, password = creds
    try:
        context = await browser_manager.new_context()
        page = await context.new_page()
        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        await page.type('input[name="txt_uname"]', username, delay=30)
        await page.type('input[name="txt_password"]', password, delay=30)
        await page.click('input[type="submit"]')
        await page.wait_for_load_state("networkidle")

        if "Home_student" not in page.url:
            await context.close()
            return None

        try:
            await page.click("span[onclick='hide_popup();']", timeout=3000)
        except Exception:
            pass

        session = {
            "context": context,
            "page": page,
            "expires": datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES),
            "cache": {},
        }
        user_sessions[chat_id] = session
        logger.info(f"Auto-login success for {chat_id}")
        return session
    except Exception as e:
        logger.error(f"Auto-login failed for {chat_id}: {e}")
        return None

# ================= SCHEDULED ALERTS =================

async def run_scheduled_alerts():
    """Background task: check attendance daily and alert if low."""
    while True:
        await asyncio.sleep(ALERT_CHECK_INTERVAL)
        logger.info("Running scheduled alert check...")
        users = get_all_users_with_alerts()
        for (chat_id, username, password) in users:
            try:
                session = user_sessions.get(chat_id)
                if not session or is_expired(session):
                    session = await auto_login(chat_id)
                if not session:
                    continue

                page = session["page"]
                att_data = await extract_attendance(page)
                save_snapshot(chat_id, "attendance", att_data)

                # Find low attendance subjects
                low = []
                for s in att_data.get("subjects", []):
                    try:
                        pct = float(re.sub(r"[^\d.]", "", s.get("percent", "0")))
                        if pct < 75:
                            low.append(f"⚠️ {s['subject']}: {pct:.1f}%")
                    except Exception:
                        pass

                if low:
                    msg = "🔔 *Daily Attendance Alert*\nSubjects below 75%:\n" + "\n".join(low)
                    await bot.send_message(chat_id, msg, parse_mode="Markdown")
                    log_alert(chat_id, "attendance", msg)

            except Exception as e:
                logger.error(f"Alert check failed for {chat_id}: {e}")

# ================= BOT COMMANDS =================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    creds = get_credentials(message.chat.id)
    if creds:
        await message.answer(
            "👋 Welcome back! Restoring your session...\n_(Your credentials are saved)_",
            parse_mode="Markdown"
        )
        session = await auto_login(message.chat.id)
        if session:
            await message.answer("✅ Auto-login successful!", reply_markup=get_menu())
            return
        await message.answer("⚠️ Auto-login failed. Please re-enter credentials.")

    await message.answer(
        "🎓 *ERP Bot — Next Level*\n\nPlease enter your *Username*:",
        parse_mode="Markdown"
    )
    await state.set_state(LoginStates.waiting_for_username)

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    refresh_session(chat_id)
    await message.answer("📱 Main Menu:", reply_markup=get_menu())

@dp.message(Command("attendance"))
async def cmd_attendance(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    msg = await message.answer("⏳ Fetching attendance data...")
    att = await extract_attendance(session["page"])
    save_snapshot(chat_id, "attendance", att)
    await msg.edit_text(format_attendance_message(att), parse_mode="Markdown")

@dp.message(Command("fees"))
async def cmd_fees(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    msg = await message.answer("⏳ Fetching fee data...")
    fees = await extract_fees(session["page"])
    save_snapshot(chat_id, "fees", fees)
    await msg.edit_text(format_fees_message(fees), parse_mode="Markdown")

@dp.message(Command("exam"))
async def cmd_exam(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    msg = await message.answer("⏳ Fetching exam results...")
    exam = await extract_exam(session["page"])
    save_snapshot(chat_id, "exam", exam)
    await msg.edit_text(format_exam_message(exam), parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    creds = get_credentials(chat_id)
    lines = [
        "📊 *Bot Status*",
        f"👤 Saved credentials: {'✅' if creds else '❌'}",
        f"🔗 Active session: {'✅' if session and not is_expired(session) else '❌'}",
    ]
    if session and not is_expired(session):
        remaining = session["expires"] - datetime.now()
        lines.append(f"⏱ Session expires in: {int(remaining.total_seconds() // 60)}m")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    chat_id = message.chat.id
    if not get_credentials(chat_id):
        await message.answer("❌ You must log in first.")
        return
    new_state = toggle_alerts(chat_id)
    status = "🔔 *Alerts enabled*" if new_state else "🔕 *Alerts disabled*"
    await message.answer(status, parse_mode="Markdown")

@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    await close_session(message.chat.id)
    await message.answer("🔓 Logged out. Your saved credentials remain for auto-login.\nUse /start to log in again.")

# ================= LOGIN FLOW =================

@dp.message(LoginStates.waiting_for_username)
async def get_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await message.answer("🔑 Enter your *Password*:", parse_mode="Markdown")
    await state.set_state(LoginStates.waiting_for_password)

@dp.message(LoginStates.waiting_for_password)
async def get_password(message: Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text.strip()
    await state.clear()

    # Delete password message for security
    try:
        await message.delete()
    except Exception:
        pass

    msg = await message.answer("🔄 Logging in, please wait...")

    try:
        context = await browser_manager.new_context()
        page = await context.new_page()

        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        await page.type('input[name="txt_uname"]', username, delay=50)
        await page.type('input[name="txt_password"]', password, delay=50)
        await page.click('input[type="submit"]')
        await page.wait_for_load_state("networkidle")

        if "Home_student" not in page.url:
            screenshot = await browser_manager.save_screenshot(page, "login_failed")
            await message.answer_photo(FSInputFile(screenshot), caption="❌ Login Failed. Please try /start again.")
            await context.close()
            await msg.delete()
            return

        try:
            await page.click("span[onclick='hide_popup();']", timeout=5000)
        except Exception:
            pass

        # Save credentials for auto-login
        save_credentials(message.chat.id, username, password)

        user_sessions[message.chat.id] = {
            "context": context,
            "page": page,
            "expires": datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES),
            "cache": {},
        }

        await msg.delete()
        await message.answer(
            "✅ *Login Successful!*\n\nTip: Use /attendance, /fees, /exam for quick data, or the menu below.",
            parse_mode="Markdown",
            reply_markup=get_menu(),
        )

    except Exception as e:
        logger.error(f"Login error: {e}")
        await msg.delete()
        await message.answer(f"❌ Error: {str(e)}")

# ================= AI QUESTION FLOW =================

@dp.message(AskStates.waiting_for_question)
async def handle_ai_question(message: Message, state: FSMContext):
    question = message.text.strip()
    await state.clear()

    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Session lost. Please /start again.")
        return

    msg = await message.answer("🤖 Fetching data & asking AI...")

    # Gather fresh data
    page = session["page"]
    att = await extract_attendance(page)
    fees = await extract_fees(page)
    exam = await extract_exam(page)
    context_data = {"attendance": att, "fees": fees, "exam": exam}

    answer = await ask_erp_ai(question, context_data)
    await msg.delete()
    await message.answer(f"🤖 *AI Answer:*\n\n{answer}", parse_mode="Markdown", reply_markup=get_back_menu())

# ================= CALLBACK HANDLER =================

@dp.callback_query()
async def menu_handler(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    data = callback.data

    # Show menu without session check
    if data == "show_menu":
        await callback.message.answer("📱 Main Menu:", reply_markup=get_menu())
        await callback.answer()
        return

    # Session check (with auto-login)
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        await callback.answer("⏳ Restoring session...", show_alert=False)
        session = await auto_login(chat_id)
        if not session:
            await callback.message.answer("❌ Session expired. Use /start to log in.")
            await callback.answer()
            return

    if not await verify_logged_in(session["page"]):
        await close_session(chat_id)
        session = await auto_login(chat_id)
        if not session:
            await callback.message.answer("❌ Session lost. Use /start")
            await callback.answer()
            return

    refresh_session(chat_id)
    page = session["page"]

    if data.startswith("page_"):
        idx = int(data.split("_")[1])
        page_name = PAGE_KEYS[idx]
        page_url = PAGE_VALS[idx]

        await callback.answer(f"Loading {page_name}...")
        await page.goto(page_url, wait_until="networkidle")
        screenshot = await browser_manager.save_screenshot(page, "page")
        await callback.message.answer_photo(FSInputFile(screenshot), caption=f"📸 {page_name}", reply_markup=get_back_menu())

    elif data == "screenshot":
        await callback.answer("Taking screenshot...")
        screenshot = await browser_manager.save_screenshot(page, "manual")
        await callback.message.answer_photo(FSInputFile(screenshot), caption="📸 Current Page", reply_markup=get_back_menu())

    elif data == "smartdata":
        await callback.answer("Extracting data...")
        loading = await callback.message.answer("⏳ Extracting all ERP data (attendance, fees, exams)...")
        att  = await extract_attendance(page)
        fees = await extract_fees(page)
        exam = await extract_exam(page)

        save_snapshot(chat_id, "attendance", att)
        save_snapshot(chat_id, "fees", fees)
        save_snapshot(chat_id, "exam", exam)

        await loading.delete()
        await callback.message.answer(format_attendance_message(att), parse_mode="Markdown")
        await callback.message.answer(format_fees_message(fees), parse_mode="Markdown")
        await callback.message.answer(format_exam_message(exam), parse_mode="Markdown", reply_markup=get_back_menu())

    elif data == "ask_ai":
        await callback.answer("Ask anything!")
        await state.set_state(AskStates.waiting_for_question)
        await callback.message.answer(
            "🤖 *Ask the AI anything about your ERP data!*\n\n"
            "Examples:\n"
            "• _Which subject has the lowest attendance?_\n"
            "• _Do I have any pending fees?_\n"
            "• _What's my best exam result?_",
            parse_mode="Markdown"
        )

    elif data == "toggle_alerts":
        new_state = toggle_alerts(chat_id)
        icon = "🔔" if new_state else "🔕"
        await callback.answer(f"{icon} Alerts {'enabled' if new_state else 'disabled'}!", show_alert=True)

    elif data == "logout":
        await close_session(chat_id)
        await callback.message.answer("🔓 Logged out. Credentials saved for next auto-login.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer()

# ================= HEALTH SERVER =================

async def health(request):
    active = sum(1 for s in user_sessions.values() if not is_expired(s))
    return web.Response(
        content_type="application/json",
        text=json.dumps({"status": "ok", "active_sessions": active, "time": datetime.now().isoformat()})
    )

async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server running on port {PORT}")

# ================= STARTUP / SHUTDOWN =================

async def on_startup():
    init_db()
    await browser_manager.start()
    asyncio.create_task(start_health())
    asyncio.create_task(run_scheduled_alerts())

    # Register bot commands
    await bot.set_my_commands([
        BotCommand(command="start",      description="Start / Auto-login"),
        BotCommand(command="menu",       description="Show main menu"),
        BotCommand(command="attendance", description="Check attendance"),
        BotCommand(command="fees",       description="Check fee status"),
        BotCommand(command="exam",       description="Check exam results"),
        BotCommand(command="status",     description="Bot & session status"),
        BotCommand(command="alerts",     description="Toggle daily alerts"),
        BotCommand(command="logout",     description="Logout"),
    ])
    logger.info("Bot started successfully")

async def on_shutdown():
    for chat_id in list(user_sessions.keys()):
        await close_session(chat_id)
    await browser_manager.stop()
    logger.info("Bot shut down cleanly")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
