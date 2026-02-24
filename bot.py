import os
import logging
from aiogram import Bot, Dispatcher, executor, types

# Enable logging
logging.basicConfig(level=logging.INFO)

# Get token from Render Environment Variable
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("No BOT_TOKEN found in environment variables")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)


# /start command
@dp.message_handler(commands=["start"])
async def start_command(message: types.Message):
    await message.reply(
        "🎓 Welcome to Noble University ERP Bot\n\n"
        "Send your Username to login."
    )


# Simple echo (test)
@dp.message_handler()
async def echo(message: types.Message):
    await message.reply(f"You said: {message.text}")


if __name__ == "__main__":
    print("Bot is starting...")
    executor.start_polling(dp, skip_updates=True)
