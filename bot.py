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
from aiogram.types import Message, FSInputFile
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

# Simple health check server
async def health_check(request):
    return web.Response(text="OK")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server on port {PORT}")

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
        # Perform login and fetch attendance if successful
        result = await perform_login_and_fetch(username, password, message.chat.id)

        if result['success']:
            # Send the attendance message (already sent inside the function)
            # We just need to confirm completion
            await message.answer(
                f"✅ Login Successful!\n"
                f"Time: {result['timestamp']}\n"
                f"Title: {result.get('title', 'N/A')}"
            )
        else:
            error_msg = result.get('error', 'Unknown error')
            await message.answer(f"❌ Login Failed!\nReason: {error_msg}")

            # Send screenshot if available
            if result.get('screenshot'):
                try:
                    photo = FSInputFile(result['screenshot'])
                    await message.answer_photo(photo, caption="📸 Page after login attempt")
                except Exception as e:
                    logger.error(f"Failed to send screenshot: {e}")
                    try:
                        doc = FSInputFile(result['screenshot'])
                        await message.answer_document(doc, caption="📸 Screenshot (as file)")
                    except:
                        pass

    except Exception as e:
        logger.error(f"Login error: {e}")
        await message.answer("❌ Unexpected error. Try later.")
    finally:
        await processing.delete()

async def scrape_attendance(page: Page) -> str:
    """
    Scrape attendance details from the attendance page.
    Returns a formatted string to send to the user.
    """
    # Wait for student details to load
    await page.wait_for_selector("span[id$='lbl_name']", timeout=10000)
    
    # Extract student info
    student_info = await page.evaluate('''() => {
        const getText = (id) => document.querySelector(id)?.innerText.trim() || 'N/A';
        return {
            name: getText("span[id$='lbl_name']"),
            enroll: getText("span[id$='lbl_enroll']"),
            college: getText("span[id$='lbl_coll']"),
            dept: getText("span[id$='lbl_dept']"),
            course: getText("span[id$='lbl_course']"),
            sem: getText("span[id$='lbl_sm']"),
            div: getText("span[id$='lbl_div']"),
            batch: getText("span[id$='lbl_batch']"),
            term: getText("span[id$='lbl_term']")
        };
    }''')
    
    # Wait for the month-wise table to be populated (Angular data)
    # The table uses ng-repeat, we need to wait for at least one row to appear
    try:
        await page.wait_for_selector("tbody tr", timeout=15000)
    except PlaywrightTimeoutError:
        logger.warning("Month-wise attendance table did not load within timeout")
    
    # Extract month-wise attendance data
    attendance_rows = await page.evaluate('''() => {
        const rows = [];
        document.querySelectorAll('tbody tr').forEach(tr => {
            const cells = tr.querySelectorAll('td');
            if (cells.length >= 8) {
                rows.push({
                    sr: cells[0]?.innerText.trim() || '',
                    month: cells[1]?.innerText.trim() || '',
                    arranged: cells[2]?.innerText.trim() || '',
                    remaining: cells[3]?.innerText.trim() || '',
                    total: cells[4]?.innerText.trim() || '',
                    absent: cells[5]?.innerText.trim() || '',
                    present: cells[6]?.innerText.trim() || '',
                    percent: cells[7]?.innerText.trim() || ''
                });
            }
        });
        return rows;
    }''')
    
    # Format the message
    msg = f"📋 *Attendance Details*\n\n"
    msg += f"*Name*: {student_info['name']}\n"
    msg += f"*Enrollment*: {student_info['enroll']}\n"
    msg += f"*Course*: {student_info['course']} - Semester {student_info['sem']}\n"
    msg += f"*Division*: {student_info['div']}  *Batch*: {student_info['batch']}\n"
    msg += f"*Term*: {student_info['term']}\n\n"
    
    if attendance_rows:
        msg += "*Month-wise Attendance*\n"
        msg += "```\n"
        msg += f"{'Month':<12} {'Arr':>4} {'Rem':>4} {'Total':>5} {'Abs':>4} {'Pres':>4} {'%':>5}\n"
        msg += "-" * 45 + "\n"
        for row in attendance_rows:
            msg += f"{row['month']:<12} {row['arranged']:>4} {row['remaining']:>4} {row['total']:>5} {row['absent']:>4} {row['present']:>4} {row['percent']:>5}\n"
        msg += "```\n"
    else:
        msg += "No month-wise attendance data available.\n"
    
    return msg

async def perform_login_and_fetch(username: str, password: str, chat_id: int) -> Dict:
    """
    Perform login, close announcement popup, fetch attendance, and send result.
    Returns a dict indicating success/failure (same as before).
    """
    page = None
    try:
        page = await browser_manager.new_page()
        page.set_default_timeout(60000)

        # Navigate to login page
        logger.info("Navigating to login page")
        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        await page.wait_for_selector('input[name="txt_uname"]', state="visible", timeout=10000)
        logger.info("Login page loaded")

        # Fill credentials
        await page.fill('input[name="txt_uname"]', username)
        await page.fill('input[name="txt_password"]', password)
        logger.info("Credentials filled")

        # Click login
        await page.click('input[type="submit"]')
        logger.info("Login button clicked")

        # --- Check for error message (quick) ---
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
            pass  # No error message

        # --- Wait for navigation to dashboard URL ---
        try:
            await page.wait_for_url("**/Home_student.aspx**", timeout=30000)
            await page.wait_for_load_state("networkidle")
            logger.info("Navigation to dashboard detected")
        except PlaywrightTimeoutError:
            logger.warning("No navigation to dashboard within timeout")
            # Check for dashboard elements as fallback
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

        # --- Close announcement popup ---
        try:
            # Wait for popup close button (visible)
            close_button = await page.wait_for_selector(
                "span[onclick='hide_popup();']",
                state="visible",
                timeout=5000
            )
            await close_button.click()
            logger.info("Announcement popup closed")
            # Brief wait for popup to disappear
            await page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            logger.info("No announcement popup found or already closed")

        # --- Navigate to attendance page ---
        logger.info("Navigating to attendance page")
        await page.goto("https://noble.icrp.in/academic/Student-cp/Form_Students_Lecture_Wise_Attendance.aspx", wait_until="networkidle")
        
        # --- Scrape attendance ---
        attendance_msg = await scrape_attendance(page)
        
        # Send the attendance message to the user
        await bot.send_message(chat_id, attendance_msg, parse_mode="Markdown")
        
        # Return success
        return {
            'success': True,
            'title': await page.title(),
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        logger.error(f"Automation error: {e}")
        return {
            'success': False,
            'error': f"Automation error: {str(e)[:100]}",
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    finally:
        if page:
            await page.close()

async def on_startup():
    """Initialize browser and health server on startup"""
    asyncio.create_task(start_health_server())
    await browser_manager.start()

async def on_shutdown():
    """Cleanup browser on shutdown"""
    await browser_manager.stop()

async def main():
    """Main function"""
    try:
        # Register startup/shutdown handlers
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        
        # Start polling
        logger.info("Starting bot...")
        await dp.start_polling(bot)
    
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
    
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
