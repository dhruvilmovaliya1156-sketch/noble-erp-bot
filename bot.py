import asyncio
import os
from aiogram import Bot, Dispatcher, types
from flask import Flask
from threading import Thread

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

# Run flask server in background
def run_web():
    app.run(host="0.0.0.0", port=10000)

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer("🎓 Noble ERP Bot Working!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_web).start()
    asyncio.run(main())
