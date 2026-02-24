import os
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from aiohttp import web

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

PORT = int(os.getenv("PORT", 10000))

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Define states
class LoginStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()

class BrowserManager:
    """Manages Playwright browser instances"""
    
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context = None
        self.screenshot_counter = 0
    
    async def start(self):
        """Start browser instance"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        logger.info("Browser started")
    
    async def stop(self):
        """Stop browser instance"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped")
    
    async def new_page(self) -> Page:
        """Create a new page"""
        return await self.context.new_page()
    
    async def save_screenshot(self, page: Page, prefix: str = "debug") -> str:
        """Save a screenshot and return the file path"""
        self.screenshot_counter += 1
        filename = f"/tmp/{prefix}_{self.screenshot_counter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        await page.screenshot(path=filename)
        logger.info(f"Screenshot saved to {filename}")
        return filename

# Global browser manager
browser_manager = BrowserManager()

# Simple health check server (required for Render)
async def health_check(request):
    return web.Response(text="OK")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server running on port {PORT}")

# Map of page names to URLs
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

# Inline keyboard after login (two rows of pages, plus screenshot and logout)
def get_main_menu():
    buttons = []
    # First row: first 4 pages
    row1 = []
    for i, (name, url) in enumerate(list(PAGES.items())[:4]):
        row1.append(InlineKeyboardButton(text=name, callback_data=f"page_{i}"))
    buttons.append(row1)
    # Second row: next 4 pages
    row2 = []
    for i, (name, url) in enumerate(list(PAGES.items())[4:8]):
        row2.append(InlineKeyboardButton(text=name, callback_data=f"page_{i+4}"))
    buttons.append(row2)
    # Third row: screenshot and logout
    buttons.append([
        InlineKeyboardButton(text="📸 Screenshot", callback_data="screenshot"),
        InlineKeyboardButton(text="🚪 Logout", callback_data="logout")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Handle /start command"""
    await message.answer(
        "🔐 Welcome!\n\nEnter your username:"
    )
    await state.set_state(LoginStates.waiting_for_username)

@dp.message(LoginStates.waiting_for_username)
async def process_username(message: Message, state: FSMContext):
    """Process username input"""
    username = message.text.strip()
    if not username:
        await message.answer("Username cannot be empty. Try again:")
        return
    await state.update_data(username=username)
    await message.answer("Now enter your password:")
    await state.set_state(LoginStates.waiting_for_password)

@dp.message(LoginStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    """Process password input and perform login"""
    password = message.text.strip()
    if not password:
        await message.answer("Password cannot be empty. Try again:")
        return

    data = await state.get_data()
    username = data.get('username')
    await state.clear()

    processing = await message.answer("🔄 Logging in...")

    try:
        login_result = await perform_login(username, password, message.chat.id)

        if login_result['success']:
            await message.answer(
                f"✅ Login Successful!\nTime: {login_result['timestamp']}",
                reply_markup=get_main_menu()
            )
        else:
            error_msg = login_result.get('error', 'Unknown error')
            await message.answer(f"❌ Login Failed!\nReason: {error_msg}")

            if login_result.get('screenshot'):
                try:
                    photo = FSInputFile(login_result['screenshot'])
                    await message.answer_photo(photo, caption="📸 Page after login attempt")
                except Exception as e:
                    logger.error(f"Failed to send screenshot: {e}")
                    try:
                        doc = FSInputFile(login_result['screenshot'])
                        await message.answer_document(doc, caption="📸 Screenshot (as file)")
                    except:
                        pass

    except Exception as e:
        logger.error(f"Login error: {e}")
        await message.answer("❌ Unexpected error. Try later.")
    finally:
        await processing.delete()

async def perform_login(username: str, password: str, chat_id: int) -> Dict:
    """
    Perform login and return result.
    If successful, stores the page in a global dict for later use.
    """
    page = None
    try:
        page = await browser_manager.new_page()
        page.set_default_timeout(60000)

        # Navigate to login page
        logger.info("Navigating to login page")
        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        
        # Wait for username field
        logger.info("Waiting for username field")
        await page.wait_for_selector('input[name="txt_uname"]', state="visible", timeout=30000)
        logger.info("Login page loaded")

        # Fill credentials
        await page.fill('input[name="txt_uname"]', username)
        await page.fill('input[name="txt_password"]', password)
        logger.info("Credentials filled")

        # Click login
        await page.click('input[type="submit"]')
        logger.info("Login button clicked")

        # Check for error message
        try:
            error_element = await page.wait_for_selector(
                "span#lbl_msg:has-text('User Name or Password Incorrect')",
                state="visible",
                timeout=5000
            )
            error_text = await error_element.text_content()
            logger.info("Login failed - error message found")
            return {
                'success': False,
                'error': error_text or "User Name or Password Incorrect",
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        except PlaywrightTimeoutError:
            pass

        # Wait for navigation to dashboard URL
        try:
            await page.wait_for_url("**/Home_student.aspx**", timeout=30000)
            await page.wait_for_load_state("networkidle")
            logger.info("Navigation to dashboard detected")
        except PlaywrightTimeoutError:
            # Fallback: check for dashboard elements
            logger.warning("No navigation to dashboard within timeout, checking elements")
            dashboard_selectors = [
                "table[id$='grd_syllabus']",
                "table[id$='grd_notif']",
                "a:has-text('Logout')",
                "h3.content-header-title:has-text('Dashboard')",
                "span#ctl00_lbl_name",
                ".dashboard"
            ]
            found = await page.evaluate('''(selectors) => {
                for (const sel of selectors) {
                    if (document.querySelector(sel)) return true;
                }
                return false;
            }''', dashboard_selectors)
            if not found:
                logger.warning("No dashboard element or error found")
                screenshot = await browser_manager.save_screenshot(page, "timeout")
                return {
                    'success': False,
                    'error': f"Login timeout - no response. URL: {page.url}",
                    'screenshot': screenshot,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

        # Close announcement popup if present
        try:
            close_button = await page.wait_for_selector(
                "span[onclick='hide_popup();']",
                state="visible",
                timeout=5000
            )
            await close_button.click()
            logger.info("Announcement popup closed")
            await page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            logger.info("No announcement popup found")

        # Store the page for this user
        user_pages[chat_id] = page
        return {
            'success': True,
            'title': await page.title(),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    except PlaywrightTimeoutError as e:
        logger.error(f"Playwright timeout during login: {e}")
        screenshot = await browser_manager.save_screenshot(page, "timeout") if page else None
        return {
            'success': False,
            'error': f"Timeout during login: {str(e)}",
            'screenshot': screenshot,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"Automation error: {e}")
        return {
            'success': False,
            'error': f"Automation error: {str(e)[:100]}",
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

# Global dict to store pages per user (chat_id)
user_pages = {}

async def close_user_page(chat_id: int):
    """Close and remove the page for a user"""
    page = user_pages.pop(chat_id, None)
    if page:
        await page.close()
        logger.info(f"Closed page for user {chat_id}")

async def verify_logged_in(page: Page) -> bool:
    """Quick check if we are still on a logged-in page"""
    try:
        # Look for a logout link or user name
        logout_link = await page.query_selector("a:has-text('Logout')")
        user_name = await page.query_selector("span#ctl00_lbl_name")
        return bool(logout_link or user_name)
    except:
        return False

@dp.callback_query()
async def handle_menu_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    page = user_pages.get(chat_id)
    if not page:
        await callback.message.answer("❌ You are not logged in. Please use /start to login.")
        await callback.answer()
        return

    if data.startswith("page_"):
        # Extract page index
        idx = int(data.split("_")[1])
        page_name = list(PAGES.keys())[idx]
        page_url = list(PAGES.values())[idx]
        await callback.answer(f"Loading {page_name}...")
        try:
            # Navigate quickly – use domcontentloaded for speed
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            # Take screenshot
            screenshot_path = await browser_manager.save_screenshot(page, page_name.replace(" ", "_"))
            photo = FSInputFile(screenshot_path)
            await callback.message.answer_photo(photo, caption=f"📸 {page_name}")
        except Exception as e:
            logger.error(f"Navigation error: {e}")
            await callback.message.answer(f"❌ Failed to load {page_name}.")
        # Keep the menu
        await callback.message.edit_reply_markup(reply_markup=get_main_menu())

    elif data == "screenshot":
        await callback.answer("Taking screenshot...")
        try:
            screenshot_path = await browser_manager.save_screenshot(page, "manual")
            photo = FSInputFile(screenshot_path)
            await callback.message.answer_photo(photo, caption="📸 Current page")
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            await callback.message.answer("❌ Failed to take screenshot.")
        await callback.message.edit_reply_markup(reply_markup=get_main_menu())

    elif data == "logout":
        await callback.answer("Logging out...")
        await close_user_page(chat_id)
        await callback.message.answer("🔓 You have been logged out. Use /start to login again.")
        await callback.message.edit_reply_markup(reply_markup=None)

async def on_startup():
    """Initialize browser and health server on startup"""
    asyncio.create_task(start_health_server())
    await browser_manager.start()

async def on_shutdown():
    """Cleanup browser on shutdown"""
    for chat_id in list(user_pages.keys()):
        await close_user_page(chat_id)
    await browser_manager.stop()

async def main():
    """Main function"""
    try:
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        logger.info("Starting bot...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
