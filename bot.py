import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_data = {}

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user_data[message.from_user.id] = {"step": "id"}
    await message.answer("🎓 Noble ERP Bot\n\nEnter your ERP ID:")

@dp.message_handler()
async def handle(message: types.Message):
    uid = message.from_user.id

    if uid not in user_data:
        return

    if user_data[uid]["step"] == "id":
        user_data[uid]["erp_id"] = message.text
        user_data[uid]["step"] = "password"
        await message.answer("Enter your Password:")

    elif user_data[uid]["step"] == "password":
        erp_id = user_data[uid]["erp_id"]
        password = message.text

        await message.answer("⏳ Logging into ERP...")

        attendance = await login_and_fetch(erp_id, password)

        if attendance:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Attendance", callback_data="attendance")]
            ])

            user_data[uid]["attendance"] = attendance
            await message.answer("✅ Login Successful!", reply_markup=keyboard)
        else:
            await message.answer("❌ Login Failed. Check ID/Password.")

        user_data.pop(uid)

@dp.callback_query_handler()
async def callback_handler(callback: types.CallbackQuery):
    if callback.data == "attendance":
        await callback.message.answer("📊 Your Attendance:\n(Attendance extraction needs selector update)")
    await callback.answer()

async def login_and_fetch(erp_id, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto("https://noble.icrp.in/academic/student-cp/")
            await page.fill("#txtUserName", erp_id)
            await page.fill("#txtPassword", password)
            await page.click("#btnLogin")
            await page.wait_for_load_state("networkidle")

            content = await page.content()

            if "Dashboard" not in content:
                await browser.close()
                return None

            await browser.close()
            return "Login Success"

        except:
            await browser.close()
            return None

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
