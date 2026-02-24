import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from aiohttp import web

from aiogram import Bot, Dispatcher
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
)

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError

# ================= CONFIG =================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SESSION_TIMEOUT_MINUTES = 15
user_sessions: Dict[int, Dict] = {}

# ================= STATES =================

class LoginStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()

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
            args=["--no-sandbox", "--disable-setuid-sandbox"],
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        )

    async def save_screenshot(self, page, prefix="shot"):
        self.screenshot_counter += 1
        path = f"/tmp/{prefix}_{self.screenshot_counter}.png"
        await page.screenshot(path=path, full_page=True)
        return path


browser_manager = BrowserManager()

# ================= ERP PAGES =================

PAGES = {
    "🏠 Dashboard": "https://noble.icrp.in/academic/Student-cp/Home_student.aspx",
    "📋 Attendance": "https://noble.icrp.in/academic/Student-cp/Form_Students_Lecture_Wise_Attendance.aspx",
    "👤 Profile": "https://noble.icrp.in/academic/Student-cp/Students_profile.aspx",
    "📚 Academics": "https://noble.icrp.in/academic/Student-cp/Form_Display_Division_TimeTableS.aspx",
    "💰 Fees": "https://noble.icrp.in/academic/Student-cp/Form_students_pay_fees.aspx",
    "📝 Exam": "https://noble.icrp.in/academic/Student-cp/Form_Students_Exam_Result_Login.aspx",
    "📅 Holidays": "https://noble.icrp.in/academic/Student-cp/List_Students_College_Wise_Holidays.aspx",
    "🎓 Convocation": "https://noble.icrp.in/academic/Student-cp/Form_student_Convocation_Registration.aspx",
}

def get_menu():
    rows = []
    items = list(PAGES.items())
    for i in range(0, len(items), 4):
        row = []
        for j in range(4):
            if i + j < len(items):
                row.append(
                    InlineKeyboardButton(
                        text=items[i + j][0],
                        callback_data=f"page_{i+j}",
                    )
                )
        rows.append(row)

    rows.append([
        InlineKeyboardButton(text="📸 Screenshot", callback_data="screenshot"),
        InlineKeyboardButton(text="🚪 Logout", callback_data="logout"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================= SESSION HELPERS =================

def is_expired(session):
    return datetime.now() > session["expires"]

async def close_session(chat_id):
    session = user_sessions.pop(chat_id, None)
    if session:
        try:
            await session["page"].close()
            await session["context"].close()
        except:
            pass
        logger.info(f"Session closed {chat_id}")

async def verify_logged_in(page):
    try:
        logout = await page.query_selector("a:has-text('Logout')")
        return logout is not None
    except:
        return False

# ================= LOGIN FLOW =================

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await message.answer("🔐 Enter Username:")
    await state.set_state(LoginStates.waiting_for_username)

@dp.message(LoginStates.waiting_for_username)
async def get_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await message.answer("Enter Password:")
    await state.set_state(LoginStates.waiting_for_password)

@dp.message(LoginStates.waiting_for_password)
async def get_password(message: Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text.strip()
    await state.clear()

    msg = await message.answer("🔄 Logging in...")

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
            await message.answer_photo(FSInputFile(screenshot), caption="❌ Login Failed")
            await context.close()
            await msg.delete()
            return

        # Close popup if exists
        try:
            await page.click("span[onclick='hide_popup();']", timeout=5000)
        except:
            pass

        user_sessions[message.chat.id] = {
            "context": context,
            "page": page,
            "expires": datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES),
        }

        await message.answer("✅ Login Successful!", reply_markup=get_menu())

    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")

    await msg.delete()

# ================= MENU HANDLER =================

@dp.callback_query()
async def menu_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = user_sessions.get(chat_id)

    if not session:
        await callback.message.answer("❌ Not logged in.")
        await callback.answer()
        return

    if is_expired(session):
        await close_session(chat_id)
        await callback.message.answer("⏳ Session expired. Login again.")
        await callback.answer()
        return

    page = session["page"]

    if not await verify_logged_in(page):
        await close_session(chat_id)
        await callback.message.answer("❌ Session lost. Login again.")
        await callback.answer()
        return

    data = callback.data

    if data.startswith("page_"):
        idx = int(data.split("_")[1])
        page_name = list(PAGES.keys())[idx]
        page_url = list(PAGES.values())[idx]

        await callback.answer(f"Loading {page_name}")

        await page.goto(page_url, wait_until="networkidle")
        screenshot = await browser_manager.save_screenshot(page, "page")
        await callback.message.answer_photo(FSInputFile(screenshot), caption=f"📸 {page_name}")

    elif data == "screenshot":
        screenshot = await browser_manager.save_screenshot(page, "manual")
        await callback.message.answer_photo(FSInputFile(screenshot), caption="📸 Current Page")

    elif data == "logout":
        await close_session(chat_id)
        await callback.message.answer("🔓 Logged out.")
        await callback.message.edit_reply_markup(reply_markup=None)

    await callback.answer()

# ================= HEALTH SERVER =================

async def health(request):
    return web.Response(text="OK")

async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ================= STARTUP =================

async def on_startup():
    await browser_manager.start()
    asyncio.create_task(start_health())

async def on_shutdown():
    for chat_id in list(user_sessions.keys()):
        await close_session(chat_id)
    await browser_manager.stop()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
