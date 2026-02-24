import os
import requests
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

TOKEN = os.environ.get("BOT_TOKEN")

user_data = {}

LOGIN_URL = "https://noble.icrp.in/academic/"

# Start
def start(update: Update, context: CallbackContext):
    update.message.reply_text("🎓 Noble University ERP\n\nEnter Username:")
    user_data[update.message.chat_id] = {"step": "username"}

def handle_message(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    text = update.message.text

    if chat_id not in user_data:
        update.message.reply_text("Type /start first.")
        return

    step = user_data[chat_id]["step"]

    if step == "username":
        user_data[chat_id]["username"] = text
        user_data[chat_id]["step"] = "password"
        update.message.reply_text("Enter Password:")

    elif step == "password":
        username = user_data[chat_id]["username"]
        password = text

        update.message.reply_text("🔍 Checking login...")

        try:
            session = requests.Session()

            # Step 1: Get login page (to get cookies)
            session.get(LOGIN_URL)

            # Step 2: Send login data
            payload = {
                "txt_uname": username,
                "txt_password": password
            }

            response = session.post(LOGIN_URL, data=payload)

            # Step 3: Check result
            if "Dashboard" in response.text or "Logout" in response.text:
                update.message.reply_text("✅ Login Successful 🎉")
            else:
                update.message.reply_text("❌ Login Failed")

        except Exception as e:
            update.message.reply_text("⚠️ Error connecting to website.")

        user_data.pop(chat_id)

# Flask server (Required for Render)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Running"

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    Thread(target=run).start()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()
