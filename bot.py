import os
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
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
    
    async def start(self):
        """Start browser instance"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']  # Required for Render
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        logger.info("Browser started successfully")
    
    async def stop(self):
        """Stop browser instance"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped successfully")
    
    async def new_page(self) -> Page:
        """Create a new page"""
        return await self.context.new_page()

# Global browser manager
browser_manager = BrowserManager()

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Handle /start command"""
    await message.answer(
        "🔐 Welcome to Login Bot!\n\n"
        "This bot will help you test login to Noble ICRP Academic portal.\n\n"
        "Please enter your username:"
    )
    await state.set_state(LoginStates.waiting_for_username)

@dp.message(LoginStates.waiting_for_username)
async def process_username(message: Message, state: FSMContext):
    """Process username input"""
    username = message.text.strip()
    
    if not username:
        await message.answer("❌ Username cannot be empty. Please enter your username:")
        return
    
    await state.update_data(username=username)
    await message.answer("Please enter your password:")
    await state.set_state(LoginStates.waiting_for_password)

@dp.message(LoginStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    """Process password input and perform login"""
    password = message.text.strip()
    
    if not password:
        await message.answer("❌ Password cannot be empty. Please enter your password:")
        return
    
    # Get stored username
    data = await state.get_data()
    username = data.get('username')
    
    # Clear state immediately for security
    await state.clear()
    
    # Send processing message
    processing_msg = await message.answer("🔄 Processing login... Please wait.")
    
    try:
        # Perform login
        result = await perform_login(username, password)
        
        # Send result
        if result['success']:
            await message.answer(
                f"✅ Login Successful!\n\n"
                f"Time: {result['timestamp']}\n"
                f"Page Title: {result.get('title', 'N/A')}"
            )
        else:
            error_msg = result.get('error', 'Unknown error')
            await message.answer(f"❌ Login Failed!\n\nReason: {error_msg}")
    
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        await message.answer(f"❌ An error occurred during login. Please try again later.")
    
    finally:
        # Delete processing message
        await processing_msg.delete()

async def perform_login(username: str, password: str) -> Dict:
    """
    Perform login using Playwright with improved element-based waiting.
    """
    page = None
    try:
        page = await browser_manager.new_page()
        page.set_default_timeout(60000)  # 60 seconds

        # Navigate to login page
        logger.info("Navigating to login page")
        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        
        # Wait for username field
        await page.wait_for_selector('input[name="txt_uname"]', state="visible", timeout=10000)
        logger.info("Login page loaded")

        # Fill credentials
        await page.fill('input[name="txt_uname"]', username)
        await page.fill('input[name="txt_password"]', password)
        logger.info("Credentials filled")

        # Click login – do NOT wait for navigation yet
        await page.click('input[type="submit"]')
        logger.info("Login button clicked")

        # Wait for either success indicators OR error indicators (up to 30 seconds)
        # Based on your after-login image, look for dashboard-specific elements
        success_selectors = [
            "text=Faculty of Computer Application",  # from your image
            "text=Dashboard",                         # heading in image
            ".dashboard",                              # common class
            "text=Attendance Details",                 # from image
            "text=Syllabus Detail",                     # from image
            "text=Recent Announcement",                 # from image
            'a[href="logout.php"]'                      # typical logout link
        ]
        
        error_selectors = [
            '.error',
            '.alert',
            'text=Invalid',
            'text=Wrong',
            'text=Failed',
            'text=Incorrect'
        ]

        # Combine selectors for waiting (wait for any of these to appear)
        all_selectors = success_selectors + error_selectors
        try:
            # Wait for any of the selectors to appear (max 30 seconds)
            element = await page.wait_for_selector(
                ', '.join(all_selectors),
                state="visible",
                timeout=30000
            )
            
            # Determine if it's a success or error element
            element_text = await element.text_content() or ""
            element_tag = await page.evaluate('(el) => el.tagName', element)
            
            # Check if the found element matches any success selector
            is_success = False
            for sel in success_selectors:
                if sel.startswith("text="):
                    expected_text = sel[5:].strip().lower()
                    if expected_text in element_text.lower():
                        is_success = True
                        break
                else:
                    # Check if element matches the CSS selector
                    try:
                        if await element.matches(sel):
                            is_success = True
                            break
                    except:
                        pass
            
            if is_success:
                logger.info("Login successful - found dashboard element")
                return {
                    'success': True,
                    'url': page.url,
                    'title': await page.title(),
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            else:
                # Error element found
                error_text = element_text or "Login failed (unknown error)"
                logger.info(f"Login failed - error: {error_text}")
                return {
                    'success': False,
                    'error': error_text,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
        except PlaywrightTimeoutError:
            # No success or error indicator appeared within timeout
            logger.warning("Timeout waiting for login result indicators")
            # Take a screenshot for debugging (optional, could be saved)
            # await page.screenshot(path="timeout_screenshot.png")
            return {
                'success': False,
                'error': "Login timeout - no response from server",
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

    except PlaywrightTimeoutError as e:
        logger.error(f"Timeout error during login: {str(e)}")
        return {
            'success': False,
            'error': "Page load timeout",
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    except Exception as e:
        logger.error(f"Browser automation error: {str(e)}")
        return {
            'success': False,
            'error': f"Automation error: {str(e)[:100]}",
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    finally:
        if page:
            await page.close()

async def on_startup():
    """Initialize browser on startup"""
    logger.info("Starting browser...")
    await browser_manager.start()

async def on_shutdown():
    """Cleanup browser on shutdown"""
    logger.info("Stopping browser...")
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
