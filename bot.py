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
from playwright.async_api import async_playwright, Browser

# ================= CONFIG =================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SESSION_TIMEOUT = 15  # minutes
user_sessions: Dict[int, Dict] = {}

# ================= STATES =================

class LoginStates(StatesGroup):
    username = State()
    password = State()

# ================= BROWSER =================

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )
        logger.info("Browser started")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_context(self):
        return await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

browser_manager = BrowserManager()

# ================= ERP PAGES =================

PAGES = {
    "🏠 Dashboard": "https://noble.icrp.in/academic/Student-cp/Home_student.aspx",
    "📋 Attendance": "https://noble.icrp.in/academic/Student-cp/Form_Students_Lecture_Wise_Attendance.aspx",
    "👤 Profile": "https://noble.icrp.in/academic/Student-cp/Students_profile.aspx",
    "📚 Academics": "https://noble.icrp.in/academic/Student-cp/Form_Display_Division_TimeTableS.aspx",
    "💰 Fees": "https://noble.icrp.in/academic/Student-cp/Form_students_pay_fees.aspx",
    "📝 Exam": "https://noble.icrp.in/academic/Student-cp/Form_Students_Exam_Result_Login.aspx",
}

def menu():
    rows = []
    items = list(PAGES.items())
    for i in range(0, len(items), 3):
        row = []
        for j in range(3):
            if i+j < len(items):
                row.append(
                    InlineKeyboardButton(
                        text=items[i+j][0],
                        callback_data=f"page_{i+j}"
                    )
                )
        rows.append(row)

    rows.append([
        InlineKeyboardButton(text="📊 Analyze Attendance", callback_data="analyze"),
        InlineKeyboardButton(text="🚪 Logout", callback_data="logout")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================= SESSION =================

def expired(session):
    return datetime.now() > session["expires"]

async def close_session(chat_id):
    session = user_sessions.pop(chat_id, None)
    if session:
        await session["page"].close()
        await session["context"].close()

# ================= LOGIN =================

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await message.answer("🔐 Enter Username:")
    await state.set_state(LoginStates.username)

@dp.message(LoginStates.username)
async def get_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await message.answer("Enter Password:")
    await state.set_state(LoginStates.password)

@dp.message(LoginStates.password)
async def get_password(message: Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text.strip()
    await state.clear()

    msg = await message.answer("🔄 Logging in...")

    context = await browser_manager.new_context()
    page = await context.new_page()

    await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")

    await page.type('input[name="txt_uname"]', username, delay=50)
    await page.type('input[name="txt_password"]', password, delay=50)

    await page.click('input[type="submit"]')
    await page.wait_for_load_state("networkidle")

    if "Home_student" not in page.url:
        await message.answer("❌ Login Failed")
        await context.close()
        await msg.delete()
        return

    user_sessions[message.chat.id] = {
        "context": context,
        "page": page,
        "expires": datetime.now() + timedelta(minutes=SESSION_TIMEOUT)
    }

    await message.answer("✅ Login Successful", reply_markup=menu())
    await msg.delete()

# ================= CALLBACK =================

@dp.callback_query()
async def handle(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = user_sessions.get(chat_id)

    if not session or expired(session):
        await close_session(chat_id)
        await callback.message.answer("⏳ Session expired. Login again.")
        return

    page = session["page"]
    data = callback.data

    # Navigate pages
    if data.startswith("page_"):
        idx = int(data.split("_")[1])
        page_name = list(PAGES.keys())[idx]
        page_url = list(PAGES.values())[idx]

        await page.goto(page_url, wait_until="networkidle")
        file = f"/tmp/page_{chat_id}.png"
        await page.screenshot(path=file, full_page=True)
        await callback.message.answer_photo(FSInputFile(file), caption=page_name)

    # ================= ATTENDANCE ANALYZER =================
    elif data == "analyze":
        await page.goto(PAGES["📋 Attendance"], wait_until="networkidle")

        # Try extracting attendance numbers
        try:
            attendance_text = await page.inner_text("body")
            numbers = [int(s) for s in attendance_text.split() if s.isdigit()]

            if len(numbers) >= 2:
                present = numbers[0]
                total = numbers[1]
                percent = round((present/total)*100, 2)

                needed = 0
                if percent < 75:
                    while (present+needed)/(total+needed)*100 < 75:
                        needed += 1

                risk = "LOW"
                if percent < 75:
                    risk = "HIGH"
                elif percent < 80:
                    risk = "MEDIUM"

                result = (
                    f"📊 Attendance Analysis\n\n"
                    f"Present: {present}\n"
                    f"Total: {total}\n"
                    f"Percentage: {percent}%\n"
                    f"Risk Level: {risk}\n"
                )

                if needed > 0:
                    result += f"\nYou must attend next {needed} lectures to reach 75%."

                await callback.message.answer(result)
            else:
                await callback.message.answer("Could not extract attendance properly.")

        except:
            await callback.message.answer("Error analyzing attendance.")

    elif data == "logout":
        await close_session(chat_id)
        await callback.message.answer("🔓 Logged out.")
        await callback.message.edit_reply_markup(reply_markup=None)

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

# ================= START =================

async def main():
    await browser_manager.start()
    asyncio.create_task(start_health())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
