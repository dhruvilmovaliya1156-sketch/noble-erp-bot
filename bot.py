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
    Perform login using Playwright
    
    Args:
        username: Username to login with
        password: Password to login with
    
    Returns:
        Dict with login result information
    """
    page = None
    
    try:
        # Create new page
        page = await browser_manager.new_page()
        
        # Set timeout
        page.set_default_timeout(30000)  # 30 seconds
        
        # Navigate to login page
        logger.info("Navigating to login page")
        await page.goto("https://noble.icrp.in/academic/", wait_until="networkidle")
        
        # Wait for form to load
        await page.wait_for_selector('input[name="txt_uname"]', state="visible", timeout=10000)
        
        # Get page info
        page_title = await page.title()
        logger.info(f"Page loaded: {page_title}")
        
        # Fill username
        await page.fill('input[name="txt_uname"]', username)
        logger.info("Username filled")
        
        # Fill password
        await page.fill('input[name="txt_password"]', password)
        logger.info("Password filled")
        
        # Submit form
        await page.click('input[type="submit"]')
        logger.info("Login button clicked")
        
        # Wait for navigation/response
        try:
            # Wait for either dashboard elements or error message
            await page.wait_for_url("**/academic/**", timeout=10000)
            await page.wait_for_load_state("networkidle")
            
            # Check if login was successful by looking for dashboard indicators
            success_indicators = [
                'a[href="logout.php"]',
                '.dashboard',
                'text/Logout',
                'text/Welcome',
                'text/Dashboard'
            ]
            
            login_successful = False
            for selector in success_indicators:
                try:
                    element = await page.wait_for_selector(selector, timeout=5000)
                    if element:
                        login_successful = True
                        break
                except:
                    continue
            
            if login_successful:
                final_url = page.url
                final_title = await page.title()
                
                return {
                    'success': True,
                    'url': final_url,
                    'title': final_title,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            else:
                # Check for error message
                error_elements = [
                    '.error',
                    '.alert',
                    'text/Invalid',
                    'text/Wrong',
                    'text/Failed'
                ]
                
                error_message = "Invalid credentials or login failed"
                for selector in error_elements:
                    try:
                        element = await page.wait_for_selector(selector, timeout=3000)
                        if element:
                            error_message = await element.text_content() or error_message
                            break
                    except:
                        continue
                
                return {
                    'success': False,
                    'error': error_message,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
        except PlaywrightTimeoutError:
            # Check current URL to determine if we're on dashboard
            current_url = page.url
            if "dashboard" in current_url.lower() or "welcome" in current_url.lower():
                return {
                    'success': True,
                    'url': current_url,
                    'title': await page.title(),
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            else:
                return {
                    'success': False,
                    'error': "Login timeout - server not responding",
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
    
    except PlaywrightTimeoutError as e:
        logger.error(f"Timeout error: {str(e)}")
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
