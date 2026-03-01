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
        InlineKeyboardButton(text="🏅 My Result",     callback_data="view_result"),
        InlineKeyboardButton(text="🔔 Toggle Alerts", callback_data="toggle_alerts"),
        InlineKeyboardButton(text="🚪 Logout",         callback_data="logout"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_attendance_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 View Daily Log", callback_data="att_daily")],
        [InlineKeyboardButton(text="🔙 Back to Menu",   callback_data="show_menu")],
    ])

def get_fees_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 All Transactions", callback_data="fees_detail")],
        [InlineKeyboardButton(text="🔙 Back to Menu",      callback_data="show_menu")],
    ])

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

def _clean(text: str) -> str:
    """Strip whitespace and normalize spaces."""
    return re.sub(r"\s+", " ", text.strip())

def _is_junk(text: str) -> bool:
    """Detect Angular un-rendered template literals or empty cells."""
    return (
        not text
        or "{{" in text
        or "}}" in text
        or text in ("-", "—", "/", "P", "H", "A", "S")
    )

async def _wait_for_angular(page, timeout: int = 10000):
    """Wait until Angular template placeholders are gone from the DOM."""
    try:
        await page.wait_for_function(
            "() => !document.body.innerText.includes('{{') && !document.body.innerText.includes('}}')",
            timeout=timeout,
        )
    except Exception:
        pass  # proceed anyway; we'll filter junk rows ourselves


# ─────────────────────────────────────────────────────────────
#  PROFILE  ─  Extract from ASP.NET label elements directly
# ─────────────────────────────────────────────────────────────
async def extract_profile(page) -> dict:
    """
    Extract full student profile from the ASP.NET profile page.
    Uses specific label IDs known from the page source.
    """
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("👤 Profile")], wait_until="networkidle")
        await asyncio.sleep(1)
        await _wait_for_angular(page)

        profile = await page.evaluate("""
        () => {
            const g = (id) => {
                const el = document.getElementById(id);
                return el ? el.innerText.trim() : '';
            };

            return {
                // Personal
                full_name:        g('ctl00_ContentPlaceHolder1_lbl_name'),
                marksheet_name:   g('ctl00_ContentPlaceHolder1_lbl_as_per_marksheet_name'),
                father_name:      g('ctl00_ContentPlaceHolder1_lbl_fathername'),
                mother_name:      g('ctl00_ContentPlaceHolder1_lbl_mothername'),
                gender:           g('ctl00_ContentPlaceHolder1_lbl_gen'),
                dob:              g('ctl00_ContentPlaceHolder1_lbl_dob'),
                aadhar:           g('ctl00_ContentPlaceHolder1_lbl_adhar'),
                blood_group:      g('ctl00_ContentPlaceHolder1_lbl_blood'),
                email:            g('ctl00_ContentPlaceHolder1_lbl_email'),
                mobile:           g('ctl00_ContentPlaceHolder1_lbl_mob_no'),
                category:         g('ctl00_ContentPlaceHolder1_lbl_category'),
                // Academic
                college:          g('ctl00_ContentPlaceHolder1_lbl_col'),
                department:       g('ctl00_ContentPlaceHolder1_lbl_batch'),
                program:          g('ctl00_ContentPlaceHolder1_lbl_course'),
                semester:         g('ctl00_ContentPlaceHolder1_lbl_sem'),
                division:         g('ctl00_ContentPlaceHolder1_lbl_division'),
                roll_no:          g('ctl00_ContentPlaceHolder1_lbl_rollno'),
                admission_no:     g('ctl00_ContentPlaceHolder1_lbl_adm_no'),
                enrollment_no:    g('ctl00_ContentPlaceHolder1_lbl_enroll'),
                admission_year:   g('ctl00_ContentPlaceHolder1_lbl_adm_yr'),
                admission_type:   g('ctl00_ContentPlaceHolder1_lbl_adm_type'),
                abc_id:           g('ctl00_ContentPlaceHolder1_lbl_abc_id'),
                // Contact
                address:          g('ctl00_ContentPlaceHolder1_lbl_add'),
                address2:         g('ctl00_ContentPlaceHolder1_lbl_add1'),
                city:             g('ctl00_ContentPlaceHolder1_lbl_city'),
                state:            g('ctl00_ContentPlaceHolder1_lbl_state'),
                pincode:          g('ctl00_ContentPlaceHolder1_lbl_pincode'),
                father_mobile:    g('ctl00_ContentPlaceHolder1_lbl_father_no'),
            };
        }
        """)

        profile["extracted_at"] = datetime.now().isoformat()
        return {"profile": profile, "extracted_at": profile["extracted_at"]}
    except Exception as e:
        logger.error(f"extract_profile error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
#  RESULT  ─  Call Angular API endpoint like the page does
# ─────────────────────────────────────────────────────────────
async def extract_result(page) -> dict:
    """
    Extract exam results by calling the Angular/ASP.NET API endpoint
    that the Student_Result.aspx page uses internally.
    """
    try:
        # Navigate to result page first to establish session cookies
        await page.goto(
            "https://noble.icrp.in/academic/Student-cp/Student_Result.aspx",
            wait_until="networkidle"
        )
        await asyncio.sleep(1)

        # Grab cookies for authenticated API call
        import aiohttp as _aiohttp
        cookies_list = await page.context.cookies()
        cookie_jar = {c["name"]: c["value"] for c in cookies_list}

        results = []
        backlog_data = []

        # 1. Get list of exam results
        api_url = "https://noble.icrp.in/academic/Student-cp/Student_Result.aspx/ListStudentResult"
        async with _aiohttp.ClientSession(cookies=cookie_jar) as sess:
            async with sess.post(
                api_url,
                json={"filter_mode": 0},
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://noble.icrp.in/academic/Student-cp/Student_Result.aspx",
                },
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                raw = await resp.json(content_type=None)
                d = raw.get("d", [])
                if isinstance(d, str):
                    d = json.loads(d)
                if isinstance(d, list):
                    for item in d:
                        results.append({
                            "enrollment":        item.get("Student_Code", ""),
                            "name":              item.get("Student_Name", ""),
                            "program":           item.get("Degree_Name", ""),
                            "semester":          item.get("Semester_Name", ""),
                            "exam":              item.get("exam_name", ""),
                            "exam_type":         item.get("student_exam_type", ""),
                            "result_declared":   item.get("is_result_declare", 0),
                            "swd_sem_id":        item.get("swd_sem_id"),
                            "swd_term_id":       item.get("swd_term_id"),
                            "swd_year_id":       item.get("swd_year_id"),
                            "swd_id":            item.get("swd_id"),
                            "swd_college_id":    item.get("swd_college_id"),
                            "degree_id":         item.get("Degree_id"),
                            "student_id":        item.get("Student_Id"),
                        })

        # 2. Get consolidated performance / SGPA / backlogs
        backlog_url = "https://noble.icrp.in/academic/Student-cp/Student_Result.aspx/Get_student_total_backlog_and_attempt"
        async with _aiohttp.ClientSession(cookies=cookie_jar) as sess:
            async with sess.post(
                backlog_url,
                json={},
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://noble.icrp.in/academic/Student-cp/Student_Result.aspx",
                },
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                raw = await resp.json(content_type=None)
                d = raw.get("d", [])
                if isinstance(d, str):
                    d = json.loads(d)
                if isinstance(d, list):
                    for item in d:
                        try:
                            sgpa = float(str(item.get("ssrd_SGPA", 0)).replace(",", "").strip())
                        except Exception:
                            sgpa = 0.0
                        backlog_data.append({
                            "semester":      item.get("semester_name", ""),
                            "sgpa":          sgpa,
                            "backlogs":      item.get("Total_backlog", 0),
                            "attempts":      item.get("Total_Attempt", 0),
                            "enrollment_no": item.get("enrollment_no", ""),
                            "student_name":  item.get("student_name", ""),
                            "degree_name":   item.get("Degree_Name", ""),
                        })

        return {
            "results":  results,
            "performance": backlog_data,
            "extracted_at": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"extract_result error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
#  FEES  ─  The ERP table repeats cell text across columns due
#            to nested <span> / Angular bindings. We use JS to
#            read each row by fixed column index (0=Sr, 1=Type,
#            2=Amount, 3=Payment Mode) and skip duplicate rows.
# ─────────────────────────────────────────────────────────────
async def extract_fees(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("\U0001f4b0 Fees")], wait_until="networkidle")
        await asyncio.sleep(1)

        fees = await page.evaluate("""
        () => {
            const results = [];
            const table = document.querySelector('[id*="grd_inst_fee"]');
            if (!table) return { fees: [], total_paid: 0, error: "Fee table not found" };

            const rows = Array.from(document.querySelectorAll('tr.tabe_12'));
            for (const row of rows) {
                const getSpan = (suffix) => {
                    const spans = row.querySelectorAll(`span[id*="${suffix}"]`);
                    for (const s of spans) {
                        const t = s.innerText.trim();
                        if (t) return t;
                    }
                    return '';
                };

                const tds = row.querySelectorAll('td.item_pading');
                const sr_td = tds[0];
                const sr = sr_td ? sr_td.innerText.trim() : '';
                if (!sr || isNaN(parseInt(sr))) continue;

                const amount_raw   = getSpan('lbl_fee_type');
                const pay_type     = getSpan('lbl_pay_type');
                const account_head = getSpan('lbl_account_head');
                const pay_date     = getSpan('lbl_pay_date');
                const receipt_no   = getSpan('lbl_receipt_no');
                const status       = getSpan('lbl_status');
                const amount_num   = parseFloat(amount_raw.replace(/[^0-9.]/g, '')) || 0;

                results.push({
                    sr: parseInt(sr),
                    amount_display: amount_raw,
                    amount: amount_num,
                    pay_type,
                    account_head,
                    pay_date,
                    receipt_no,
                    status,
                });
            }

            const totalEl = document.querySelector('[id*="lblTotal"]');
            const total_raw = totalEl ? totalEl.innerText.trim() : '0';
            const total_paid = parseFloat(total_raw.replace(/[^0-9.]/g, '')) || 0;
            return { fees: results, total_paid };
        }
        """)

        fees["extracted_at"] = datetime.now().isoformat()
        return fees

    except Exception as e:
        logger.error(f"extract_fees error: {e}")
        return {"error": str(e)}


async def extract_attendance(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("📋 Attendance")], wait_until="networkidle")
        await asyncio.sleep(1)

        monthly = []
        try:
            import aiohttp as _aiohttp
            cookies_list = await page.context.cookies()
            cookie_jar = {c["name"]: c["value"] for c in cookies_list}

            api_url = "https://noble.icrp.in/academic/Student-cp/Form_Students_Lecture_Wise_Attendance.aspx/ListAttendanceStudent"
            async with _aiohttp.ClientSession(cookies=cookie_jar) as sess:
                async with sess.post(
                    api_url,
                    json={},
                    headers={
                        "Content-Type": "application/json",
                        "Referer": PAGE_VALS[PAGE_KEYS.index("📋 Attendance")],
                    },
                    timeout=_aiohttp.ClientTimeout(total=15),
                ) as resp:
                    raw = await resp.json(content_type=None)
                    d = raw.get("d", [])
                    if isinstance(d, str):
                        d = json.loads(d)
                    if isinstance(d, list):
                        for i, c in enumerate(d):
                            monthly.append({
                                "sr":             i + 1,
                                "month":          c.get("month", ""),
                                "total_arranged": c.get("total_arrange_lect", 0),
                                "remaining":      c.get("remaning", 0),
                                "total_lectures": c.get("total_lecture_for_stud", 0),
                                "absent":         c.get("absent_lecture", 0),
                                "present":        c.get("present_lecture", 0),
                                "percentage":     c.get("persentage", 0),
                            })
        except Exception as e:
            logger.warning(f"Monthly API call failed: {e}")
            monthly = []

        _js_att = (
            "() => {"
            "  const lectures = [];"
            "  const lecDiv = document.querySelector(\"[id*='div_lec_att']\");"
            "  if (!lecDiv) return { lectures: [], student: {}, headers: [] };"
            "  const table = lecDiv.querySelector(\"table\");"
            "  if (!table) return { lectures: [], student: {}, headers: [] };"
            "  const headerThs = Array.from(table.querySelectorAll(\"th\"));"
            "  const headers = headerThs.map(th => th.innerText.trim().replace(/ +/g, \" \").replace(/\\n/g, \" \"));"
            "  const rows = Array.from(table.querySelectorAll(\"tr\")).slice(1);"
            "  for (const row of rows) {"
            "    const cells = Array.from(row.querySelectorAll(\"td\"));"
            "    if (cells.length < 2) continue;"
            "    const slotNum = cells[0].innerText.trim();"
            "    if (!slotNum || isNaN(parseInt(slotNum))) continue;"
            "    const days = [];"
            "    for (let i = 1; i < cells.length; i++) {"
            "      const cell = cells[i];"
            "      const header = headers[i] || \"\";"
            "      const statusDiv = cell.querySelector(\"div\");"
            "      let status = statusDiv ? statusDiv.innerText.trim() : cell.innerText.trim();"
            "      if (!status) status = \"-\";"
            "      const tooltip = cell.querySelector(\".tooltiptext\");"
            "      let faculty = \"\", topic = \"\", reason = \"\";"
            "      if (tooltip) {"
            "        const h = tooltip.innerHTML;"
            "        const fm = h.match(/Faculty:[^>]*>([^<]+)/);"
            "        const tm = h.match(/Topic:[^>]*>([^<]+)/);"
            "        const rm = h.match(/Reason:[^>]*>([^<]*)/);"
            "        faculty = fm ? fm[1].trim() : \"\";"
            "        topic   = tm ? tm[1].trim() : \"\";"
            "        reason  = rm ? rm[1].trim() : \"\";"
            "      }"
            "      days.push({ date: header, status: status, faculty: faculty, topic: topic, reason: reason });"
            "    }"
            "    lectures.push({ slot: parseInt(slotNum), days: days });"
            "  }"
            "  const g = function(id) { const e = document.getElementById(id); return e ? e.innerText.trim() : \"\"; };"
            "  const student = {"
            "    name:       g(\"ctl00_ContentPlaceHolder1_lbl_name\"),"
            "    enrollment: g(\"ctl00_ContentPlaceHolder1_lbl_enroll\"),"
            "    college:    g(\"ctl00_ContentPlaceHolder1_lbl_coll\"),"
            "    department: g(\"ctl00_ContentPlaceHolder1_lbl_dept\"),"
            "    course:     g(\"ctl00_ContentPlaceHolder1_lbl_course\"),"
            "    semester:   g(\"ctl00_ContentPlaceHolder1_lbl_sm\"),"
            "    division:   g(\"ctl00_ContentPlaceHolder1_lbl_div\"),"
            "    batch:      g(\"ctl00_ContentPlaceHolder1_lbl_batch\"),"
            "    term:       g(\"ctl00_ContentPlaceHolder1_lbl_term\")"
            "  };"
            "  return { lectures: lectures, student: student, headers: headers };"
            "}"
        )
        dom_data = await page.evaluate(_js_att)

        return {
            "monthly":   monthly,
            "lectures":  dom_data.get("lectures", []),
            "student":   dom_data.get("student", {}),
            "headers":   dom_data.get("headers", []),
            "extracted_at": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"extract_attendance error: {e}")
        return {"error": str(e)}


async def extract_exam(page) -> dict:
    try:
        await page.goto(PAGE_VALS[PAGE_KEYS.index("📝 Exam")], wait_until="networkidle")
        await _wait_for_angular(page)

        results = await page.evaluate("""
        () => {
            const results = [];
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const rows = Array.from(table.querySelectorAll('tr'));
                if (rows.length < 2) continue;
                const headers = Array.from(rows[0].querySelectorAll('th,td'))
                    .map(c => c.innerText.trim().toLowerCase());
                const looksLikeExam = headers.some(h =>
                    h.includes('subject') || h.includes('mark') || h.includes('grade') || h.includes('result')
                );
                if (!looksLikeExam) continue;
                for (const row of rows.slice(1)) {
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (cells.length < 2) continue;
                    const texts = cells.map(c => c.innerText.trim().replace(/\\s+/g,' '));
                    if (texts.join('').includes('{{')) continue;
                    if (!texts[0] || texts[0].match(/^\\d+$/) && texts.length < 3) continue;
                    results.push({
                        subject: texts[0],
                        marks:   texts[1] || '',
                        grade:   texts[2] || '',
                        result:  texts[3] || '',
                    });
                }
                if (results.length > 0) break;
            }
            return results;
        }
        """)

        return {"results": results, "extracted_at": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────────────────────

def format_profile_message(data: dict) -> str:
    """Format student profile into a clean Telegram message."""
    if "error" in data:
        return f"❌ Could not extract profile: {data['error']}"

    p = data.get("profile", {})
    if not p:
        return "👤 No profile data found."

    lines = [
        "👤 *Student Profile*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "📌 *Personal Details*",
    ]

    def row(label, value):
        if value and value.strip():
            lines.append(f"  `{label}:` {value}")

    row("Full Name",     p.get("full_name", ""))
    row("Father",        p.get("father_name", ""))
    row("Mother",        p.get("mother_name", ""))
    row("Gender",        p.get("gender", ""))
    row("Date of Birth", p.get("dob", ""))
    row("Blood Group",   p.get("blood_group", ""))
    row("Category",      p.get("category", ""))
    row("Aadhar",        p.get("aadhar", ""))
    row("ABC / APAAR",   p.get("abc_id", ""))

    lines += ["", "📞 *Contact*"]
    row("Mobile",        p.get("mobile", ""))
    row("Email",         p.get("email", ""))
    row("Address",       p.get("address", ""))
    row("City",          p.get("city", ""))
    row("State",         p.get("state", ""))
    row("Pin Code",      p.get("pincode", ""))
    row("Father Mobile", p.get("father_mobile", ""))

    lines += ["", "🎓 *Academic Details*"]
    row("College/Faculty",  p.get("college", ""))
    row("Department",       p.get("department", ""))
    row("Program",          p.get("program", ""))
    row("Semester",         p.get("semester", ""))
    row("Division",         p.get("division", ""))
    row("Roll No",          p.get("roll_no", ""))
    row("Enrollment No",    p.get("enrollment_no", ""))
    row("Admission No",     p.get("admission_no", ""))
    row("Admission Year",   p.get("admission_year", ""))
    row("Admission Type",   p.get("admission_type", ""))

    lines += ["", f"🕐 _Updated: {datetime.now().strftime('%d %b %Y, %H:%M')}_"]
    return "\n".join(lines)


def format_result_message(data: dict) -> str:
    """Format exam result list into Telegram message."""
    if "error" in data:
        return f"❌ Could not fetch results: {data['error']}"

    results = data.get("results", [])
    performance = data.get("performance", [])

    if not results and not performance:
        return "🏅 No result data found."

    lines = ["🏅 *Exam Results*", "━━━━━━━━━━━━━━━━━━━━━━"]

    # Show semester-wise results list
    if results:
        lines.append("")
        lines.append("📋 *Registered Exams*")
        for r in results:
            declared = r.get("result_declared", 0)
            status_icon = "✅" if declared else "⏳"
            lines.append(
                f"\n{status_icon} *{r.get('semester', 'N/A')}* — {r.get('exam', 'N/A')}\n"
                f"   📚 {r.get('program', '')}\n"
                f"   🔖 Type: {r.get('exam_type', 'N/A')}\n"
                f"   {'🟢 Result Declared' if declared else '🔴 Result Not Declared Yet'}"
            )

    # Show SGPA / consolidated performance
    if performance:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━", "📊 *Consolidated Performance*", ""]

        total_backlogs = 0
        for p in performance:
            sgpa = p.get("sgpa", 0)
            backlogs = p.get("backlogs", 0)
            total_backlogs += int(backlogs) if str(backlogs).isdigit() else 0

            if sgpa >= 9.0:
                grade_emoji = "🏆"
            elif sgpa >= 8.0:
                grade_emoji = "🥇"
            elif sgpa >= 7.0:
                grade_emoji = "🥈"
            elif sgpa >= 6.0:
                grade_emoji = "🥉"
            elif sgpa > 0:
                grade_emoji = "📌"
            else:
                grade_emoji = "⏳"

            lines.append(
                f"{grade_emoji} *{p.get('semester', 'N/A')}*\n"
                f"   SGPA: `{sgpa:.2f}` | Backlogs: `{backlogs}` | Attempts: `{p.get('attempts', 0)}`"
            )

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📌 *Total Backlogs: {total_backlogs}*",
        ]

    lines.append(f"\n🕐 _Updated: {datetime.now().strftime('%d %b %Y, %H:%M')}_")
    return "\n".join(lines)


def format_fees_message(data: dict) -> str:
    """Summary grouped by account head with totals."""
    if "error" in data:
        return f"\u274c Could not extract fees: {data['error']}"

    fees = data.get("fees", [])
    if not fees:
        return "\U0001f4b0 No fee records found."

    grouped: dict = {}
    for f in fees:
        head = f.get("account_head", "Other")
        if head not in grouped:
            grouped[head] = {"total": 0.0, "count": 0, "modes": set(), "dates": []}
        grouped[head]["total"]  += f.get("amount", 0)
        grouped[head]["count"]  += 1
        m = f.get("pay_type", "")
        if m: grouped[head]["modes"].add(m)
        d = f.get("pay_date", "")
        if d: grouped[head]["dates"].append(d)

    total_paid = data.get("total_paid", 0)
    lines = [
        "\U0001f4b0 *Fee Payment Summary*",
        f"\U0001f4c5 As of: {datetime.now().strftime('%d %b %Y')}",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    ]
    for head, info in grouped.items():
        modes_str = " & ".join(sorted(info["modes"])) if info["modes"] else "—"
        count_str = f"\u00d7{info['count']}" if info["count"] > 1 else "1 payment"
        lines.append(
            f"\u2705 *{head}*\n"
            f"   \U0001f4b5 \u20b9{info['total']:,.2f} ({count_str})\n"
            f"   \U0001f3e6 {modes_str}"
        )
    lines += [
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        f"\U0001f4b3 *Total Paid: \u20b9{total_paid:,.2f}*",
        f"\U0001f4ca *{len(fees)} transaction(s)*",
    ]
    return "\n".join(lines)


def format_fees_detail_message(data: dict) -> str:
    if "error" in data:
        return f"\u274c {data['error']}"
    fees = data.get("fees", [])
    if not fees:
        return "No transactions found."

    lines = ["\U0001f4cb *All Fee Transactions*\n"]
    for f in fees:
        status_icon = "\u2705" if "success" in f.get("status", "").lower() else "\u23f3"
        lines.append(
            f"{status_icon} *#{f['sr']} — {f['account_head']}*\n"
            f"   \u20b9{f['amount']:,.2f}  \u2022  {f['pay_type']}\n"
            f"   \U0001f4c5 {f['pay_date']}  \u2022  Receipt: {f['receipt_no']}"
        )
    total = data.get("total_paid", 0)
    lines.append(f"\n\U0001f4b3 *Total: \u20b9{total:,.2f}*")
    return "\n".join(lines)


def format_attendance_message(data: dict) -> str:
    if "error" in data:
        return f"\u274c Could not extract attendance: {data['error']}"

    monthly = data.get("monthly", [])
    student = data.get("student", {})

    if not monthly:
        return (
            "\U0001f4cb *Attendance*\n\n"
            "\u26a0\ufe0f No monthly data found.\n"
            "Try tapping *\U0001f4cb Attendance* again after a moment."
        )

    name = student.get("name", "")
    course = student.get("course", "")
    sem = student.get("semester", "")
    term = student.get("term", "")

    lines = ["\U0001f4cb *Month-wise Attendance*"]
    if name:
        lines.append(f"\U0001f393 {name} | {course} {sem}")
    if term:
        lines.append(f"\U0001f4c5 Term: {term}")
    lines.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")

    all_pcts = []
    for m in monthly:
        try:
            pct = float(str(m.get("percentage", 0)).replace(",", "").strip())
        except Exception:
            pct = 0.0

        all_pcts.append(pct)
        present = m.get("present", 0)
        absent  = m.get("absent", 0)
        total   = m.get("total_lectures", 0)
        arranged = m.get("total_arranged", 0)
        month_name = m.get("month", f"Month {m.get('sr','')}")

        if pct >= 85:   emoji, status = "\U0001f7e2", "Good"
        elif pct >= 75: emoji, status = "\U0001f7e1", "OK"
        elif pct >= 60: emoji, status = "\U0001f7e0", "\u26a0\ufe0f Low"
        else:           emoji, status = "\U0001f534", "\u274c Critical"

        filled = int(pct / 10)
        bar = "\u2588" * filled + "\u2591" * (10 - filled)

        lines.append(
            f"\n{emoji} *{month_name}*\n"
            f"   `{bar}` {pct:.1f}%\n"
            f"   \u2705 Present: {present}  \u274c Absent: {absent}  \U0001f4da Total: {total}\n"
            f"   \U0001f4dd Arranged: {arranged}  — {status}"
        )

    lines.append("\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
    if all_pcts:
        avg = sum(all_pcts) / len(all_pcts)
        low = sum(1 for p in all_pcts if p < 75)
        lines.append(f"\U0001f4ca *Overall Avg: {avg:.1f}%*")
        if low:
            lines.append(f"\u26a0\ufe0f *{low} month(s) below 75%*")
        else:
            lines.append("\u2705 All months above 75%")

    return "\n".join(lines)


def format_attendance_daily(data: dict) -> str:
    lectures = data.get("lectures", [])
    headers  = data.get("headers", [])
    student  = data.get("student", {})

    if not lectures:
        return "\U0001f4c5 No lecture-wise data available."

    name = student.get("name", "")
    course = student.get("course", "")
    sem = student.get("semester", "")

    lines = ["\U0001f4c5 *Lecture-wise Daily Attendance*"]
    if name:
        lines.append(f"\U0001f393 {name} | {course} {sem}")
    lines.append("")

    status_map = {
        "P": "\u2705", "A": "\u274c", "H": "\U0001f3d6",
        "S": "\u26d4", "L": "\U0001f4dd", "R": "\u23f3", "-": "\u2796"
    }

    for lec in lectures:
        slot = lec.get("slot", "?")
        days = lec.get("days", [])
        lines.append(f"\U0001f4da *Lecture {slot}*")

        present_count = sum(1 for d in days if d.get("status") == "P")
        absent_count  = sum(1 for d in days if d.get("status") == "A")

        day_parts = []
        for d in days:
            st = d.get("status", "-")
            date = d.get("date", "")
            if st == "-": continue
            em = status_map.get(st, "\u2753")
            day_parts.append(f"`{date}`{em}")

        for i in range(0, len(day_parts), 4):
            lines.append("  " + "  ".join(day_parts[i:i+4]))

        lines.append(f"  \u2705{present_count} \u274c{absent_count}")
        lines.append("")

    lines += [
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "\u2705P=Present  \u274cA=Absent",
        "\U0001f3d6H=Holiday  \u26d4S=Suspended  \u23f3R=Remaining",
    ]
    return "\n".join(lines)


def format_exam_message(data: dict) -> str:
    if "error" in data:
        return f"❌ Could not extract results: {data['error']}"
    results = data.get("results", [])
    if not results:
        return "📝 No exam results found."

    lines = [
        "📝 *Exam Results*",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for r in results:
        grade  = r.get("grade", "")
        marks  = r.get("marks", "")
        result = r.get("result", "")
        grade_emoji = {"A+": "🏆", "A": "🥇", "B": "🥈", "C": "🥉", "F": "❌", "PASS": "✅", "FAIL": "❌"}.get(
            result.upper() or grade.upper(), "📌"
        )
        detail = " | ".join(filter(None, [marks, grade, result]))
        lines.append(f"{grade_emoji} *{r['subject']}*\n   {detail}")

    return "\n".join(lines)

# ================= AI ASSISTANT =================

async def ask_erp_ai(question: str, context_data: dict) -> str:
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

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    msg = await message.answer("⏳ Fetching your profile...")
    profile_data = await extract_profile(session["page"])
    save_snapshot(chat_id, "profile", profile_data)
    session.setdefault("cache", {})["profile"] = profile_data
    await msg.edit_text(format_profile_message(profile_data), parse_mode="Markdown", reply_markup=get_back_menu())

@dp.message(Command("result"))
async def cmd_result(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id)
    if not session or is_expired(session):
        session = await auto_login(chat_id)
    if not session:
        await message.answer("❌ Not logged in. Use /start")
        return
    msg = await message.answer("⏳ Fetching your results...")
    result_data = await extract_result(session["page"])
    save_snapshot(chat_id, "result", result_data)
    session.setdefault("cache", {})["result"] = result_data
    text = format_result_message(result_data)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await message.answer(text[i:i+4000], parse_mode="Markdown")
    else:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=get_back_menu())

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

        save_credentials(message.chat.id, username, password)

        user_sessions[message.chat.id] = {
            "context": context,
            "page": page,
            "expires": datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES),
            "cache": {},
        }

        await msg.delete()
        await message.answer(
            "✅ *Login Successful!*\n\n"
            "Tip: Use /profile, /result, /attendance, /fees for quick data, or the menu below.",
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

    page = session["page"]
    att  = await extract_attendance(page)
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

    if data == "show_menu":
        await callback.message.answer("📱 Main Menu:", reply_markup=get_menu())
        await callback.answer()
        return

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
        page_url  = PAGE_VALS[idx]

        await callback.answer(f"Loading {page_name}...")
        loading = await callback.message.answer(f"⏳ Loading {page_name}...")

        # ── Profile ───────────────────────────────────────────
        if page_name == "👤 Profile":
            profile_data = await extract_profile(page)
            save_snapshot(chat_id, "profile", profile_data)
            session.setdefault("cache", {})["profile"] = profile_data

            screenshot = await browser_manager.save_screenshot(page, "profile")
            await loading.delete()

            await callback.message.answer_photo(
                FSInputFile(screenshot),
                caption="📸 Profile Page"
            )
            await callback.message.answer(
                format_profile_message(profile_data),
                parse_mode="Markdown",
                reply_markup=get_back_menu()
            )

        # ── Attendance ────────────────────────────────────────
        elif page_name == "📋 Attendance":
            att = await extract_attendance(page)
            save_snapshot(chat_id, "attendance", att)
            session["cache"]["att"] = att

            screenshot = await browser_manager.save_screenshot(page, "attendance")
            await loading.delete()

            await callback.message.answer_photo(
                FSInputFile(screenshot),
                caption="📸 Attendance Page"
            )
            await callback.message.answer(
                format_attendance_message(att),
                parse_mode="Markdown",
                reply_markup=get_attendance_menu()
            )

        # ── Fees ──────────────────────────────────────────────
        elif page_name == "💰 Fees":
            fees = await extract_fees(page)
            save_snapshot(chat_id, "fees", fees)
            session["cache"]["fees"] = fees

            screenshot = await browser_manager.save_screenshot(page, "fees")
            await loading.delete()

            await callback.message.answer_photo(
                FSInputFile(screenshot),
                caption="📸 Fee Details Page"
            )
            await callback.message.answer(
                format_fees_message(fees),
                parse_mode="Markdown",
                reply_markup=get_fees_menu()
            )

        # ── Exam ──────────────────────────────────────────────
        elif page_name == "📝 Exam":
            exam = await extract_exam(page)
            save_snapshot(chat_id, "exam", exam)
            session["cache"]["exam"] = exam

            screenshot = await browser_manager.save_screenshot(page, "exam")
            await loading.delete()

            await callback.message.answer_photo(
                FSInputFile(screenshot),
                caption="📸 Exam Results Page"
            )
            await callback.message.answer(
                format_exam_message(exam),
                parse_mode="Markdown",
                reply_markup=get_back_menu()
            )

        # ── All other pages → screenshot only ────────────────
        else:
            await page.goto(page_url, wait_until="networkidle")
            screenshot = await browser_manager.save_screenshot(page, "page")
            await loading.delete()
            await callback.message.answer_photo(
                FSInputFile(screenshot),
                caption=f"📸 {page_name}",
                reply_markup=get_back_menu()
            )

    elif data == "screenshot":
        await callback.answer("Taking screenshot...")
        screenshot = await browser_manager.save_screenshot(page, "manual")
        await callback.message.answer_photo(FSInputFile(screenshot), caption="📸 Current Page", reply_markup=get_back_menu())

    elif data == "smartdata":
        await callback.answer("Extracting data...")
        loading = await callback.message.answer(
            "⏳ Extracting ERP data…\n"
            "_(Attendance, Fees, Exam & Profile)_",
            parse_mode="Markdown"
        )
        att     = await extract_attendance(page)
        fees    = await extract_fees(page)
        exam    = await extract_exam(page)
        profile = await extract_profile(page)

        save_snapshot(chat_id, "attendance", att)
        save_snapshot(chat_id, "fees", fees)
        save_snapshot(chat_id, "exam", exam)
        save_snapshot(chat_id, "profile", profile)

        session.setdefault("cache", {}).update({
            "att": att, "fees": fees, "exam": exam, "profile": profile
        })

        await loading.delete()
        await callback.message.answer(
            format_profile_message(profile),
            parse_mode="Markdown",
            reply_markup=get_back_menu()
        )
        await callback.message.answer(
            format_attendance_message(att),
            parse_mode="Markdown",
            reply_markup=get_attendance_menu()
        )
        await callback.message.answer(
            format_fees_message(fees),
            parse_mode="Markdown",
            reply_markup=get_fees_menu()
        )
        await callback.message.answer(
            format_exam_message(exam),
            parse_mode="Markdown",
            reply_markup=get_back_menu()
        )

    elif data == "view_result":
        await callback.answer("Fetching results...")
        loading = await callback.message.answer("⏳ Loading your exam results...")
        result_data = await extract_result(page)
        save_snapshot(chat_id, "result", result_data)
        session.setdefault("cache", {})["result"] = result_data
        await loading.delete()
        text = format_result_message(result_data)
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await callback.message.answer(text[i:i+4000], parse_mode="Markdown")
        else:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=get_back_menu())

    elif data == "att_daily":
        await callback.answer("Loading daily log...")
        att = session.get("cache", {}).get("att")
        if not att:
            loading = await callback.message.answer("⏳ Fetching attendance...")
            att = await extract_attendance(page)
            session.setdefault("cache", {})["att"] = att
            await loading.delete()
        daily_text = format_attendance_daily(att)
        if len(daily_text) > 4000:
            for i in range(0, len(daily_text), 4000):
                await callback.message.answer(daily_text[i:i+4000], parse_mode="Markdown")
        else:
            await callback.message.answer(daily_text, parse_mode="Markdown", reply_markup=get_back_menu())

    elif data == "fees_detail":
        await callback.answer("Loading transactions...")
        fees = session.get("cache", {}).get("fees")
        if not fees:
            loading = await callback.message.answer("⏳ Fetching fees...")
            fees = await extract_fees(page)
            session.setdefault("cache", {})["fees"] = fees
            await loading.delete()
        await callback.message.answer(
            format_fees_detail_message(fees),
            parse_mode="Markdown",
            reply_markup=get_back_menu()
        )

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

    await bot.set_my_commands([
        BotCommand(command="start",      description="Start / Auto-login"),
        BotCommand(command="menu",       description="Show main menu"),
        BotCommand(command="profile",    description="View my profile"),
        BotCommand(command="result",     description="View exam results & SGPA"),
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

    logger.info("Clearing webhook and pending updates...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"delete_webhook failed (non-fatal): {e}")

    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Starting polling (attempt {attempt}/{max_retries})...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=True,
            )
            break
        except Exception as e:
            err = str(e)
            if "Conflict" in err or "terminated by other" in err:
                wait = 5 * attempt
                logger.warning(
                    f"Conflict: another instance running. "
                    f"Retrying in {wait}s ({attempt}/{max_retries})..."
                )
                await asyncio.sleep(wait)
            else:
                logger.error(f"Fatal polling error: {e}")
                raise
    else:
        logger.error("Could not start polling after max retries.")

if __name__ == "__main__":
    asyncio.run(main())
