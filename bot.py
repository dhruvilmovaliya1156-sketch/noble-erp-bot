import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from flask import Flask
from threading import Thread

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Flask app for Render
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

@dp.message_handler(commands=["start"])
async def start(message: Message):
    await message.answer("🎓 Noble ERP Bot is Live!")

async def run_bot():
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(run_bot())
