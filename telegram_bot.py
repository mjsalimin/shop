import asyncio
import json
import logging
import re
import urllib.parse
import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

import aiohttp
from bs4 import BeautifulSoup
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type, RetryError as TenacityRetryError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# کلیدهای API
TELEGRAM_BOT_TOKEN = "1951771121:AAHxdMix9xAR6a592sTZKC6aBArdfIaLwco"
METIS_API_KEY = "tpsg-6WW8eb5cZfq6fZru3B6tUbSaKB2EkVm"
METIS_BOT_ID = "30f054f0-2363-4128-b6c6-308efc31c5d9"
METIS_MODEL = "gpt-4o"
METIS_BASE_URL = "https://api.metisai.ir"

# تنظیمات
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2
MAX_DAILY_REQUESTS = 20  # افزایش محدودیت روزانه
MAX_CONTENT_LENGTH = 4000
SUPPORTED_LANGUAGES = ['fa', 'en', 'ar']
DEFAULT_LANGUAGE = 'fa'

# تنظیمات logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RetryableError(Exception):
    pass

class ContentTemplate:
    """کلاس قالب‌های محتوا"""
    
    @staticmethod
    def get_template(category: str, language: str = 'fa') -> dict:
        """دریافت قالب محتوا بر اساس دسته‌بندی"""
        templates = {
            'ai': {
                'fa': {
                    'intro': "🤖 هوش مصنوعی در {topic}",
                    'structure': ["🔬 تعریف و مفاهیم", "⚙️ کاربردهای عملی", "🛠️ ابزارها و تکنولوژی‌ها", "📊 مزایا و چالش‌ها"],
                    'hashtags': "#هوش_مصنوعی #AI #تکنولوژی #آینده #نوآوری"
                },
                'en': {
                    'intro': "🤖 Artificial Intelligence in {topic}",
                    'structure': ["🔬 Definition and Concepts", "⚙️ Practical Applications", "🛠️ Tools and Technologies", "📊 Benefits and Challenges"],
                    'hashtags': "#AI #ArtificialIntelligence #Technology #Innovation #Future"
                }
            },
            'marketing': {
                'fa': {
                    'intro': "📈 استراتژی‌های بازاریابی در {topic}",
                    'structure': ["🎯 استراتژی و برنامه‌ریزی", "📊 تحلیل بازار", "🚀 اجرا و پیاده‌سازی", "📈 نتایج و بهینه‌سازی"],
                    'hashtags': "#بازاریابی #مارکتینگ #استراتژی #فروش #کسب_وکار"
                },
                'en': {
                    'intro': "📈 Marketing Strategies in {topic}",
                    'structure': ["🎯 Strategy and Planning", "📊 Market Analysis", "🚀 Implementation", "📈 Results and Optimization"],
                    'hashtags': "#Marketing #Strategy #Sales #Business #Growth"
                }
            },
            'management': {
                'fa': {
                    'intro': "👥 مدیریت و رهبری در {topic}",
                    'structure': ["📋 برنامه‌ریزی استراتژیک", "👥 مدیریت تیم", "📊 نظارت و کنترل", "🚀 بهبود مستمر"],
                    'hashtags': "#مدیریت #رهبری #سازمان #توسعه #موفقیت"
                },
                'en': {
                    'intro': "👥 Management and Leadership in {topic}",
                    'structure': ["📋 Strategic Planning", "👥 Team Management", "📊 Monitoring and Control", "🚀 Continuous Improvement"],
                    'hashtags': "#Management #Leadership #Organization #Development #Success"
                }
            }
        }
        return templates.get(category, templates.get('ai')).get(language, templates.get('ai')['fa'])

class AnalyticsManager:
    """کلاس مدیریت آمار و تحلیل"""
    
    def __init__(self, db_manager):
        self.db = db_manager
    
    def get_user_analytics(self, user_id: int) -> dict:
        """دریافت آمار کاربر"""
        try:
            conn = sqlite3.connect(self.db.db_path)
            cursor = conn.cursor()
            
            # آمار کلی کاربر
            cursor.execute('''
                SELECT COUNT(*) as total_requests,
                       COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful_requests,
                       COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_requests
                FROM requests WHERE user_id = ?
            ''', (user_id,))
            stats = cursor.fetchone()
            
            # دسته‌بندی‌های محبوب
            cursor.execute('''
                SELECT category, COUNT(*) as count
                FROM requests 
                WHERE user_id = ? 
                GROUP BY category 
                ORDER BY count DESC 
                LIMIT 5
            ''', (user_id,))
            categories = cursor.fetchall()
            
            # آمار روزانه
            cursor.execute('''
                SELECT DATE(created_at) as date, COUNT(*) as count
                FROM requests 
                WHERE user_id = ? 
                AND created_at >= date('now', '-7 days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            ''', (user_id,))
            daily_stats = cursor.fetchall()
            
            conn.close()
            
            return {
                'total_requests': stats[0] if stats else 0,
                'successful_requests': stats[1] if stats else 0,
                'failed_requests': stats[2] if stats else 0,
                'popular_categories': categories,
                'daily_stats': daily_stats
            }
        except Exception as e:
            logger.error(f"Error getting user analytics: {e}")
            return {}
    
    def get_global_analytics(self) -> dict:
        """دریافت آمار کلی سیستم"""
        try:
            conn = sqlite3.connect(self.db.db_path)
            cursor = conn.cursor()
            
            # آمار کلی
            cursor.execute('''
                SELECT COUNT(*) as total_users,
                       COUNT(CASE WHEN join_date >= date('now', '-7 days') THEN 1 END) as new_users_week,
                       COUNT(CASE WHEN join_date >= date('now', '-30 days') THEN 1 END) as new_users_month
                FROM users
            ''')
            user_stats = cursor.fetchone()
            
            # آمار درخواست‌ها
            cursor.execute('''
                SELECT COUNT(*) as total_requests,
                       COUNT(CASE WHEN created_at >= date('now', '-24 hours') THEN 1 END) as requests_today,
                       COUNT(CASE WHEN created_at >= date('now', '-7 days') THEN 1 END) as requests_week
                FROM requests
            ''')
            request_stats = cursor.fetchone()
            
            conn.close()
            
            return {
                'total_users': user_stats[0] if user_stats else 0,
                'new_users_week': user_stats[1] if user_stats else 0,
                'new_users_month': user_stats[2] if user_stats else 0,
                'total_requests': request_stats[0] if request_stats else 0,
                'requests_today': request_stats[1] if request_stats else 0,
                'requests_week': request_stats[2] if request_stats else 0
            }
        except Exception as e:
            logger.error(f"Error getting global analytics: {e}")
            return {}

class NotificationManager:
    """کلاس مدیریت اعلان‌ها"""
    
    def __init__(self, application):
        self.app = application
        self.scheduled_tasks = {}
    
    async def send_daily_reminder(self, user_id: int, username: str = None):
        """ارسال یادآوری روزانه"""
        try:
            message = f"""🌅 سلام {username or 'کاربر'}!

💡 یادآوری روزانه:
• امروز {MAX_DAILY_REQUESTS} درخواست رایگان دارید
• موضوعات جدید را امتحان کنید
• از قابلیت‌های پیشرفته استفاده کنید

🚀 برای شروع: /start"""
            
            await self.app.bot.send_message(chat_id=user_id, text=message)
            logger.info(f"Daily reminder sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending daily reminder to {user_id}: {e}")
    
    async def send_weekly_report(self, user_id: int, analytics: dict):
        """ارسال گزارش هفتگی"""
        try:
            message = f"""📊 گزارش هفتگی شما

📈 آمار کلی:
• کل درخواست‌ها: {analytics.get('total_requests', 0)}
• درخواست‌های موفق: {analytics.get('successful_requests', 0)}
• درخواست‌های ناموفق: {analytics.get('failed_requests', 0)}

🏆 دسته‌بندی‌های محبوب شما:
"""
            
            for category, count in analytics.get('popular_categories', [])[:3]:
                message += f"• {category}: {count} درخواست\n"
            
            message += "\n💡 برای مشاهده آمار کامل: /analytics"
            
            await self.app.bot.send_message(chat_id=user_id, text=message)
            logger.info(f"Weekly report sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending weekly report to {user_id}: {e}")

class ContentScheduler:
    """کلاس زمان‌بندی محتوا"""
    
    def __init__(self):
        self.scheduled_content = {}
    
    def schedule_content(self, user_id: int, topic: str, category: str, delay_hours: int = 24):
        """زمان‌بندی ارسال محتوا"""
        scheduled_time = datetime.now() + timedelta(hours=delay_hours)
        self.scheduled_content[user_id] = {
            'topic': topic,
            'category': category,
            'scheduled_time': scheduled_time,
            'sent': False
        }
        logger.info(f"Content scheduled for user {user_id} at {scheduled_time}")
    
    def get_pending_content(self) -> List[tuple]:
        """دریافت محتوای در انتظار"""
        now = datetime.now()
        pending = []
        
        for user_id, content in self.scheduled_content.items():
            if not content['sent'] and content['scheduled_time'] <= now:
                pending.append((user_id, content))
        
        return pending
    
    def mark_as_sent(self, user_id: int):
        """علامت‌گذاری محتوا به عنوان ارسال شده"""
        if user_id in self.scheduled_content:
            self.scheduled_content[user_id]['sent'] = True

class DatabaseManager:
    def __init__(self, db_path="bot_database.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """ایجاد جداول دیتابیس"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # جدول کاربران
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    join_date TEXT DEFAULT (datetime('now')),
                    daily_requests INTEGER DEFAULT 0,
                    last_request_date TEXT,
                    preferred_category TEXT DEFAULT 'general',
                    language TEXT DEFAULT 'fa'
                )
            ''')
            
            # جدول درخواست‌ها
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    category TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'completed',
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول آمار
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    total_requests INTEGER DEFAULT 0,
                    successful_requests INTEGER DEFAULT 0,
                    failed_requests INTEGER DEFAULT 0
                )
            ''')
            
            # جدول محتوای ذخیره شده
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS saved_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    category TEXT,
                    content TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    is_favorite BOOLEAN DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول تنظیمات کاربر
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT DEFAULT 'fa',
                    content_length TEXT DEFAULT 'medium',
                    notification_enabled BOOLEAN DEFAULT 1,
                    auto_save BOOLEAN DEFAULT 1,
                    preferred_categories TEXT DEFAULT 'general',
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول یادآوری‌ها
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    scheduled_time TEXT,
                    is_sent BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول بازخورد
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    rating INTEGER,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """دریافت اطلاعات کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            user = cursor.fetchone()
            conn.close()
            
            if user:
                return {
                    'user_id': user[0],
                    'username': user[1],
                    'first_name': user[2],
                    'last_name': user[3],
                    'join_date': user[4],
                    'daily_requests': user[5],
                    'last_request_date': user[6],
                    'preferred_category': user[7],
                    'language': user[8]
                }
            return None
        except Exception as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None
    
    def create_user(self, user_id: int, username: str, first_name: str, last_name: str):
        """ایجاد کاربر جدید"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name))
            conn.commit()
            conn.close()
            logger.info(f"User {user_id} created/updated successfully")
        except Exception as e:
            logger.error(f"Error creating user {user_id}: {e}")
            raise
    
    def update_daily_requests(self, user_id: int) -> int:
        """به‌روزرسانی تعداد درخواست‌های روزانه"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # بررسی تاریخ آخرین درخواست
            cursor.execute('SELECT last_request_date, daily_requests FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            
            if result and result[0]:
                try:
                    last_date = datetime.strptime(result[0], '%Y-%m-%d').date()
                    today_date = datetime.now().date()
                    if last_date == today_date:
                        # همان روز - افزایش شمارنده
                        new_count = result[1] + 1
                    else:
                        # روز جدید - ریست شمارنده
                        new_count = 1
                except ValueError:
                    # اگر فرمت تاریخ اشتباه باشد
                    new_count = 1
            else:
                # اولین درخواست
                new_count = 1
            
            cursor.execute('''
                UPDATE users 
                SET daily_requests = ?, last_request_date = ?
                WHERE user_id = ?
            ''', (new_count, today, user_id))
            
            conn.commit()
            conn.close()
            logger.debug(f"Updated daily requests for user {user_id}: {new_count}")
            return new_count
        except Exception as e:
            logger.error(f"Error updating daily requests for user {user_id}: {e}")
            return 0
    
    def can_make_request(self, user_id: int) -> bool:
        """بررسی امکان درخواست"""
        try:
            count = self.update_daily_requests(user_id)
            return count <= MAX_DAILY_REQUESTS
        except Exception as e:
            logger.error(f"Error checking request limit for user {user_id}: {e}")
            return True  # در صورت خطا، اجازه درخواست بده
    
    def log_request(self, user_id: int, topic: str, category: str):
        """ثبت درخواست"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO requests (user_id, topic, category)
                VALUES (?, ?, ?)
            ''', (user_id, topic, category))
            conn.commit()
            conn.close()
            logger.info(f"Request logged for user {user_id}: {topic} ({category})")
        except Exception as e:
            logger.error(f"Error logging request for user {user_id}: {e}")
            # در صورت خطا، ادامه کار بدون ثبت
    
    def save_content(self, user_id: int, topic: str, category: str, content: str):
        """ذخیره محتوا"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO saved_content (user_id, topic, category, content)
                VALUES (?, ?, ?, ?)
            ''', (user_id, topic, category, content))
            conn.commit()
            conn.close()
            logger.info(f"Content saved for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving content for user {user_id}: {e}")
    
    def get_saved_content(self, user_id: int, limit: int = 10) -> List[Dict]:
        """دریافت محتوای ذخیره شده"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, topic, category, content, created_at, is_favorite
                FROM saved_content 
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (user_id, limit))
            results = cursor.fetchall()
            conn.close()
            
            return [
                {
                    'id': row[0],
                    'topic': row[1],
                    'category': row[2],
                    'content': row[3],
                    'created_at': row[4],
                    'is_favorite': bool(row[5])
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error getting saved content for user {user_id}: {e}")
            return []
    
    def toggle_favorite(self, content_id: int, user_id: int) -> bool:
        """تغییر وضعیت مورد علاقه"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE saved_content 
                SET is_favorite = CASE WHEN is_favorite = 1 THEN 0 ELSE 1 END
                WHERE id = ? AND user_id = ?
            ''', (content_id, user_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error toggling favorite for content {content_id}: {e}")
            return False
    
    def save_feedback(self, user_id: int, topic: str, rating: int, comment: str = ""):
        """ذخیره بازخورد"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO feedback (user_id, topic, rating, comment)
                VALUES (?, ?, ?, ?)
            ''', (user_id, topic, rating, comment))
            conn.commit()
            conn.close()
            logger.info(f"Feedback saved for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving feedback for user {user_id}: {e}")
    
    def get_user_settings(self, user_id: int) -> Dict:
        """دریافت تنظیمات کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'user_id': result[0],
                    'language': result[1],
                    'content_length': result[2],
                    'notification_enabled': bool(result[3]),
                    'auto_save': bool(result[4]),
                    'preferred_categories': result[5]
                }
            else:
                # ایجاد تنظیمات پیش‌فرض
                self.create_user_settings(user_id)
                return self.get_user_settings(user_id)
        except Exception as e:
            logger.error(f"Error getting user settings for {user_id}: {e}")
            return {}
    
    def create_user_settings(self, user_id: int):
        """ایجاد تنظیمات پیش‌فرض برای کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO user_settings (user_id)
                VALUES (?)
            ''', (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error creating user settings for {user_id}: {e}")
    
    def update_user_settings(self, user_id: int, settings: Dict):
        """به‌روزرسانی تنظیمات کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            updates = []
            values = []
            
            if 'language' in settings:
                updates.append('language = ?')
                values.append(settings['language'])
            if 'content_length' in settings:
                updates.append('content_length = ?')
                values.append(settings['content_length'])
            if 'notification_enabled' in settings:
                updates.append('notification_enabled = ?')
                values.append(settings['notification_enabled'])
            if 'auto_save' in settings:
                updates.append('auto_save = ?')
                values.append(settings['auto_save'])
            if 'preferred_categories' in settings:
                updates.append('preferred_categories = ?')
                values.append(settings['preferred_categories'])
            
            if updates:
                values.append(user_id)
                query = f"UPDATE user_settings SET {', '.join(updates)} WHERE user_id = ?"
                cursor.execute(query, values)
                conn.commit()
            
            conn.close()
            logger.info(f"User settings updated for {user_id}")
        except Exception as e:
            logger.error(f"Error updating user settings for {user_id}: {e}")

class ContentScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'no-cache',
        }

    async def fetch_page(self, url: str, timeout: int = 15, params: dict = None) -> str:
        """واکشی صفحه وب با headers مناسب و مدیریت خطا"""
        try:
            kwargs = {
                'headers': self.headers,
                'timeout': aiohttp.ClientTimeout(total=timeout),
                'ssl': False,
                'allow_redirects': True,
                'max_redirects': 5
            }
            
            if params:
                kwargs['params'] = params
            
            async with self.session.get(url, **kwargs) as response:
                if response.status == 200:
                    content = await response.text()
                    logger.debug(f"Successfully fetched {len(content)} characters from {url}")
                    return content
                elif response.status == 403:
                    logger.warning(f"Access forbidden (403) for {url}")
                    return ""
                elif response.status == 404:
                    logger.warning(f"Page not found (404) for {url}")
                    return ""
                elif response.status >= 500:
                    logger.warning(f"Server error ({response.status}) for {url}")
                    return ""
                else:
                    logger.warning(f"HTTP {response.status} for {url}")
                    return ""
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching {url}")
            return ""
        except aiohttp.ClientError as e:
            logger.error(f"Client error fetching {url}: {e}")
            return ""
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return ""

    def clean_text(self, text: str) -> str:
        """تمیز کردن و نرمال‌سازی متن"""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip()
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&quot;', '"').replace('&#39;', "'")
        return text

    async def search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """جستجو با DuckDuckGo"""
        try:
            search_url = f"https://duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            html = await self.fetch_page(search_url)
            
            if not html:
                return []
            
            soup = BeautifulSoup(html, 'html.parser')
            results = []
            
            search_results = soup.find_all('div', class_='result__body')
            
            for result in search_results[:5]:
                try:
                    title_elem = result.find('a', class_='result__a')
                    snippet_elem = result.find('a', class_='result__snippet')
                    
                    if title_elem:
                        url = title_elem.get('href', '')
                        title = self.clean_text(title_elem.get_text())
                        snippet = self.clean_text(snippet_elem.get_text()) if snippet_elem else ""
                        
                        if url.startswith('//'):
                            url = 'https:' + url
                        elif url.startswith('/'):
                            url = 'https://duckduckgo.com' + url
                        
                        if title and len(title) > 10:
                            results.append({
                                'url': url,
                                'title': title,
                                'snippet': snippet
                            })
                except Exception as e:
                    logger.debug(f"Error parsing DDG result: {e}")
                    continue
            
            return results
            
        except Exception as e:
            logger.error(f"DuckDuckGo search error: {e}")
            return []

    async def search_bing(self, query: str) -> List[Dict[str, str]]:
        """جستجو با Bing"""
        try:
            search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            html = await self.fetch_page(search_url)
            
            if not html:
                return []
            
            soup = BeautifulSoup(html, 'html.parser')
            results = []
            
            search_results = soup.find_all('li', class_='b_algo')
            
            for result in search_results[:5]:
                try:
                    title_elem = result.find('h2')
                    link_elem = title_elem.find('a') if title_elem else None
                    snippet_elem = result.find('p') or result.find('div', class_='b_caption')
                    
                    if link_elem and title_elem:
                        url = link_elem.get('href', '')
                        title = self.clean_text(title_elem.get_text())
                        snippet = self.clean_text(snippet_elem.get_text()) if snippet_elem else ""
                        
                        if title and len(title) > 10:
                            results.append({
                                'url': url,
                                'title': title,
                                'snippet': snippet
                            })
                except Exception as e:
                    logger.debug(f"Error parsing Bing result: {e}")
                    continue
            
            return results
            
        except Exception as e:
            logger.error(f"Bing search error: {e}")
            return []

    async def comprehensive_research(self, topic: str) -> (str, list):
        """تحقیق جامع در مورد موضوع"""
        logger.info(f"Starting comprehensive research for: {topic}")
        research_parts = []
        sources = []
        
        try:
            # جستجو با DuckDuckGo
            logger.info("Searching with DuckDuckGo...")
            ddg_results = await self.search_duckduckgo(topic)
            if ddg_results:
                ddg_content = []
                for result in ddg_results:
                    if result['snippet']:
                        ddg_content.append(f"• {result['title']}: {result['snippet']}")
                        if result['url']:
                            sources.append({'title': result['title'], 'url': result['url']})
                
                if ddg_content:
                    research_parts.append("🔍 نتایج جستجو:\n" + "\n".join(ddg_content))
            
            # اگر نتیجه کافی نداریم، Bing را امتحان کنیم
            if len(research_parts) == 0:
                logger.info("Trying Bing search...")
                bing_results = await self.search_bing(topic)
                if bing_results:
                    bing_content = []
                    for result in bing_results:
                        if result['snippet']:
                            bing_content.append(f"• {result['title']}: {result['snippet']}")
                            if result['url']:
                                sources.append({'title': result['title'], 'url': result['url']})
                    
                    if bing_content:
                        research_parts.append("🔍 نتایج جستجو:\n" + "\n".join(bing_content))
            
            # اگر هیچ محتوای خارجی پیدا نکردیم، محتوای پایه بسازیم
            if not research_parts:
                logger.warning("No external content found, creating basic research")
                basic_research = f"""📚 موضوع: {topic}

این موضوع در حوزه‌های مختلف کاربرد دارد و شامل موارد زیر می‌باشد:

🔹 مفاهیم کلیدی و تعاریف اساسی
🔹 کاربردهای عملی در صنعت
🔹 روش‌ها و تکنیک‌های مرتبط
🔹 فواید و چالش‌های موجود
🔹 روندهای آینده

💡 برای اطلاعات دقیق‌تر، مراجعه به منابع معتبر توصیه می‌شود."""
                research_parts.append(basic_research)
                
        except Exception as e:
            logger.error(f"Error in comprehensive research: {e}")
            research_parts.append(f"📚 موضوع: {topic}\n\nدر حال حاضر امکان دسترسی به منابع خارجی محدود است، اما این موضوع شامل مباحث مهمی می‌باشد که نیاز به بررسی دقیق دارد.")
        
        combined_research = "\n\n".join(research_parts)
        logger.info(f"Research completed. Total content length: {len(combined_research)}")
        return combined_research, sources

class MetisAPI:
    def __init__(self, api_key: str, bot_id: str, model: str = "gpt-4o"):
        self.api_key = api_key
        self.bot_id = bot_id
        self.model = model
        self.base_url = METIS_BASE_URL
        self.conversation_id = None

    async def create_conversation(self, session: aiohttp.ClientSession) -> str:
        """ایجاد گفتگوی جدید با ربات"""
        try:
            url = f"{self.base_url}/api/conversations"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "botId": self.bot_id,
                "title": f"Educational Content Generation - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            }
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 201:
                    data = await response.json()
                    self.conversation_id = data.get('id')
                    logger.info(f"Conversation created: {self.conversation_id}")
                    return self.conversation_id
                else:
                    logger.error(f"Failed to create conversation: {response.status}")
                    raise RetryableError("خطا در ایجاد گفتگو")
                    
        except Exception as e:
            logger.error(f"Error creating conversation: {e}")
            raise RetryableError(f"خطا در ایجاد گفتگو: {str(e)}")

    @retry(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        stop=stop_after_attempt(RETRY_ATTEMPTS)
    )
    async def send_message(self, session: aiohttp.ClientSession, message: str) -> str:
        """ارسال پیام به ربات و دریافت پاسخ"""
        try:
            if not self.conversation_id:
                await self.create_conversation(session)
            
            url = f"{self.base_url}/api/conversations/{self.conversation_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "content": message,
                "role": "user"
            }
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 201:
                    data = await response.json()
                    # دریافت پاسخ ربات
                    return await self.get_bot_response(session, data.get('id'))
                else:
                    logger.error(f"Failed to send message: {response.status}")
                    raise RetryableError("خطا در ارسال پیام")
                    
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            raise RetryableError(f"خطا در ارسال پیام: {str(e)}")

    async def get_bot_response(self, session: aiohttp.ClientSession, message_id: str) -> str:
        """دریافت پاسخ ربات"""
        try:
            url = f"{self.base_url}/api/conversations/{self.conversation_id}/messages/{message_id}/response"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('content', '')
                else:
                    logger.error(f"Failed to get bot response: {response.status}")
                    raise RetryableError("خطا در دریافت پاسخ ربات")
                    
        except Exception as e:
            logger.error(f"Error getting bot response: {e}")
            raise RetryableError(f"خطا در دریافت پاسخ ربات: {str(e)}")

    @retry(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        stop=stop_after_attempt(RETRY_ATTEMPTS)
    )
    async def generate_educational_content(self, session: aiohttp.ClientSession, topic: str, research_content: str) -> str:
        """تولید محتوای آموزشی با Metis API"""
        try:
            # محدود کردن طول محتوا
            if len(research_content) > 2000:
                research_content = research_content[:2000] + "..."
            
            # ایجاد پیام آموزشی
            educational_prompt = f"""موضوع: {topic}

اطلاعات تحقیق: {research_content}

لطفاً محتوای آموزشی علمی و کاربردی بنویس که شامل موارد زیر باشد:

1. معرفی و تعریف موضوع
2. کاربردهای عملی و واقعی
3. نکات کلیدی و مهم
4. مثال‌های کاربردی
5. مزایا و چالش‌ها

محتوا را به دو بخش تقسیم کن:
[بخش اول] - معرفی و مفاهیم
[بخش دوم] - کاربردهای عملی و نکات کلیدی

از ایموجی مناسب استفاده کن و لحن آموزشی و دوستانه داشته باش."""
            
            logger.info(f"Generating educational content for topic: {topic}")
            
            # ارسال پیام به ربات متیس
            response = await self.send_message(session, educational_prompt)
            
            if not response:
                raise RetryableError("پاسخ خالی از ربات")
            
            logger.info(f"Generated content length: {len(response)}")
            return response
                
        except RetryableError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise RetryableError(f"خطای غیرمنتظره: {str(e)}")

class ContentGenerator:
    """کلاس تولید محتوا با قابلیت‌های پیشرفته"""
    
    @staticmethod
    def detect_category(topic: str) -> str:
        """تشخیص دسته‌بندی موضوع"""
        topic_lower = topic.lower()
        
        # کلمات کلیدی برای هر دسته
        ai_keywords = ['هوش مصنوعی', 'ai', 'machine learning', 'deep learning', 'chatbot', 'چت‌بات', 'الگوریتم']
        marketing_keywords = ['بازاریابی', 'marketing', 'فروش', 'sales', 'تبلیغات', 'مشتری', 'کمپین']
        management_keywords = ['مدیریت', 'management', 'رهبری', 'leadership', 'تیم', 'پروژه', 'سازمان']
        programming_keywords = ['برنامه‌نویسی', 'programming', 'کد', 'code', 'توسعه', 'نرم‌افزار', 'اپلیکیشن']
        business_keywords = ['کسب‌وکار', 'business', 'استارتاپ', 'startup', 'کارآفرینی', 'سرمایه‌گذاری']
        
        # شمارش کلمات کلیدی
        ai_count = sum(1 for word in ai_keywords if word in topic_lower)
        marketing_count = sum(1 for word in marketing_keywords if word in topic_lower)
        management_count = sum(1 for word in management_keywords if word in topic_lower)
        programming_count = sum(1 for word in programming_keywords if word in topic_lower)
        business_count = sum(1 for word in business_keywords if word in topic_lower)
        
        # انتخاب دسته با بیشترین تطابق
        counts = {
            'ai': ai_count,
            'marketing': marketing_count,
            'management': management_count,
            'programming': programming_count,
            'business': business_count
        }
        
        max_category = max(counts, key=counts.get)
        
        # اگر هیچ تطابقی نباشد، بر اساس کلمات اصلی تصمیم بگیر
        if counts[max_category] == 0:
            if 'مدیریت' in topic_lower:
                return 'management'
            elif 'بازاریابی' in topic_lower or 'فروش' in topic_lower:
                return 'marketing'
            elif 'هوش مصنوعی' in topic_lower:
                return 'ai'
            else:
                return 'general'
        
        return max_category
    
    @staticmethod
    def create_advanced_posts(topic: str, research_content: str, category: str = 'general') -> List[str]:
        """ایجاد پست‌های علمی و کاربردی"""
        try:
            # استخراج اطلاعات مفید از محتوای تحقیق
            useful_info = ContentGenerator._extract_useful_info(research_content)
            
            # ایجاد پست اول - معرفی علمی
            post1 = ContentGenerator._create_scientific_post1(topic, useful_info, category)
            
            # ایجاد پست دوم - کاربردهای عملی
            post2 = ContentGenerator._create_practical_post2(topic, useful_info, category)
            
            return [post1, post2]
            
        except Exception as e:
            logger.error(f"Error creating advanced posts: {e}")
            return [
                f"📚 {topic}\n\nاین موضوع شامل مباحث مهمی در حوزه مربوطه است که نیاز به بررسی دقیق دارد.",
                f"💡 کاربردهای عملی {topic}:\n\n• اهمیت در صنعت\n• روش‌های پیاده‌سازی\n• مزایای استفاده\n\nبرای اطلاعات بیشتر، منابع معتبر را بررسی کنید."
            ]
    
    @staticmethod
    async def create_metis_posts(metis_api: MetisAPI, session: aiohttp.ClientSession, topic: str, research_content: str) -> List[str]:
        """ایجاد پست‌ها با استفاده از Metis API"""
        try:
            # تولید محتوا با Metis
            content = await metis_api.generate_educational_content(session, topic, research_content)
            
            # تقسیم محتوا به دو بخش
            posts = []
            if '[بخش دوم]' in content:
                parts = content.split('[بخش دوم]')
                if len(parts) >= 2:
                    post1 = parts[0].replace('[بخش اول]', '').strip()
                    post2 = parts[1].strip()
                    posts = [post1, post2]
            
            if not posts:
                # اگر تقسیم نشد، محتوا را به دو قسمت تقسیم کن
                paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
                if len(paragraphs) >= 2:
                    mid = len(paragraphs) // 2
                    post1 = '\n\n'.join(paragraphs[:mid])
                    post2 = '\n\n'.join(paragraphs[mid:])
                    posts = [post1, post2]
                else:
                    posts = [content]
            
            # فیلتر کردن پست‌های معتبر
            valid_posts = [post for post in posts if post.strip() and len(post.strip()) > 100]
            
            if not valid_posts:
                raise Exception("پست‌های تولید شده معتبر نیستند")
            
            logger.info(f"Successfully generated {len(valid_posts)} posts with Metis API")
            return valid_posts
            
        except Exception as e:
            logger.error(f"Error creating Metis posts: {e}")
            raise
    

    
    @staticmethod
    def _extract_useful_info(research_content: str) -> dict:
        """استخراج اطلاعات مفید از محتوای تحقیق"""
        info = {
            'key_points': [],
            'tools': [],
            'benefits': [],
            'methods': [],
            'examples': []
        }
        
        try:
            # استخراج نکات کلیدی
            if "نتایج جستجو:" in research_content:
                search_part = research_content.split("نتایج جستجو:")[1]
                if "•" in search_part:
                    items = search_part.split("•")[1:6]  # 5 مورد اول
                    for item in items:
                        if ":" in item:
                            title = item.split(":")[0].strip()
                            if len(title) > 10:
                                info['key_points'].append(title)
            
            # استخراج ابزارها و روش‌ها
            content_lower = research_content.lower()
            if 'ابزار' in content_lower or 'tool' in content_lower:
                # جستجوی ابزارها
                pass
            
            # استخراج مزایا
            if 'مزایا' in content_lower or 'benefit' in content_lower:
                # جستجوی مزایا
                pass
                
        except Exception as e:
            logger.error(f"Error extracting useful info: {e}")
        
        return info
    
    @staticmethod
    def _create_scientific_post1(topic: str, useful_info: dict, category: str) -> str:
        """ایجاد پست اول - معرفی علمی"""
        post = f"🔬 {topic}\n\n"
        
        if useful_info['key_points']:
            post += "📋 نکات کلیدی:\n"
            for i, point in enumerate(useful_info['key_points'][:3], 1):
                post += f"{i}. {point}\n"
            post += "\n"
        
        # اضافه کردن اطلاعات علمی بر اساس دسته‌بندی
        if category == 'ai':
            post += "🤖 هوش مصنوعی در این حوزه:\n"
            post += "• استفاده از الگوریتم‌های پیشرفته\n"
            post += "• یادگیری ماشین و تحلیل داده\n"
            post += "• اتوماسیون فرآیندها\n\n"
        elif category == 'marketing':
            post += "📈 جنبه‌های بازاریابی:\n"
            post += "• استراتژی‌های دیجیتال\n"
            post += "• تحلیل رفتار مشتری\n"
            post += "• بهینه‌سازی تبدیل\n\n"
        elif category == 'management':
            post += "👥 جنبه‌های مدیریتی:\n"
            post += "• برنامه‌ریزی استراتژیک\n"
            post += "• مدیریت منابع\n"
            post += "• رهبری تیم\n\n"
        
        post += "💡 این موضوع در حال حاضر یکی از مهم‌ترین مباحث در حوزه مربوطه است."
        
        return post
    
    @staticmethod
    def _create_practical_post2(topic: str, useful_info: dict, category: str) -> str:
        """ایجاد پست دوم - کاربردهای عملی"""
        post = f"⚙️ کاربردهای عملی {topic}\n\n"
        
        # کاربردهای عملی بر اساس دسته‌بندی
        if category == 'ai':
            post += "🔧 ابزارهای کاربردی:\n"
            post += "• پلتفرم‌های هوش مصنوعی\n"
            post += "• کتابخانه‌های برنامه‌نویسی\n"
            post += "• API های آماده\n\n"
            post += "📊 مزایای پیاده‌سازی:\n"
            post += "• افزایش دقت تا 90%\n"
            post += "• کاهش زمان پردازش\n"
            post += "• صرفه‌جویی در هزینه\n\n"
        elif category == 'marketing':
            post += "🎯 استراتژی‌های عملی:\n"
            post += "• بازاریابی محتوا\n"
            post += "• تبلیغات هدفمند\n"
            post += "• تحلیل رقبا\n\n"
            post += "📈 نتایج مورد انتظار:\n"
            post += "• افزایش فروش 30-50%\n"
            post += "• بهبود نرخ تبدیل\n"
            post += "• افزایش آگاهی از برند\n\n"
        elif category == 'management':
            post += "📋 روش‌های اجرایی:\n"
            post += "• مدیریت پروژه چابک\n"
            post += "• تصمیم‌گیری داده‌محور\n"
            post += "• بهبود فرآیندها\n\n"
            post += "🎯 نتایج پیاده‌سازی:\n"
            post += "• افزایش بهره‌وری 25-40%\n"
            post += "• کاهش هزینه‌ها\n"
            post += "• بهبود رضایت کارکنان\n\n"
        else:
            post += "🔧 روش‌های پیاده‌سازی:\n"
            post += "• برنامه‌ریزی مرحله‌ای\n"
            post += "• تست و ارزیابی\n"
            post += "• بهبود مستمر\n\n"
            post += "📊 مزایای استفاده:\n"
            post += "• بهبود عملکرد\n"
            post += "• صرفه‌جویی در زمان\n"
            post += "• افزایش کیفیت\n\n"
        
        post += "🚀 برای شروع، ابتدا نیازهای خود را شناسایی کرده و سپس گام به گام پیش بروید."
        
        return post
    
    @staticmethod
    def _get_hashtags(category: str) -> str:
        """دریافت هشتگ‌های مناسب برای دسته‌بندی"""
        hashtags = {
            'ai': '#هوش_مصنوعی #AI #تکنولوژی #آینده',
            'marketing': '#بازاریابی #مارکتینگ #فروش #استراتژی',
            'management': '#مدیریت #رهبری #سازمان #توسعه',
            'programming': '#برنامه_نویسی #کد #تکنولوژی #نرم_افزار',
            'business': '#کسب_وکار #استارتاپ #کارآفرینی #موفقیت',
            'general': '#آموزش #توسعه_فردی #موفقیت #یادگیری'
        }
        return hashtags.get(category, '#آموزش #توسعه_فردی #موفقیت')

class AdvancedTelegramBot:
    def __init__(self):
        self.scraper = None
        self.db = DatabaseManager()
        self.user_states = {}
        self.content_generator = ContentGenerator()
        self.metis_api = MetisAPI(METIS_API_KEY, METIS_BOT_ID, METIS_MODEL)
        self.analytics_manager = AnalyticsManager(self.db)
        self.notification_manager = None
        self.content_scheduler = ContentScheduler()
        self.content_template = ContentTemplate()
        
    def get_main_menu(self):
        """دریافت منوی اصلی پیشرفته"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 موضوع جدید", callback_data='new_topic')],
            [InlineKeyboardButton("💾 محتوای ذخیره شده", callback_data='saved_content')],
            [InlineKeyboardButton("📊 آمار و گزارش", callback_data='analytics')],
            [InlineKeyboardButton("⚙️ تنظیمات", callback_data='settings')],
            [InlineKeyboardButton("❓ راهنما", callback_data='help'), 
             InlineKeyboardButton("🔍 جستجوی پیشرفته", callback_data='advanced_search')],
            [InlineKeyboardButton("⭐ بازخورد", callback_data='feedback')],
            [InlineKeyboardButton("📅 یادآوری‌ها", callback_data='reminders')],
            [InlineKeyboardButton("📊 درباره ربات", callback_data='about')]
        ])

    def get_back_menu(self):
        """دریافت منوی بازگشت"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 برگشت به منوی اصلی", callback_data='main_menu')]
        ])
    
    def get_category_menu(self):
        """منوی انتخاب دسته‌بندی"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 هوش مصنوعی", callback_data='category_ai')],
            [InlineKeyboardButton("📈 بازاریابی", callback_data='category_marketing')],
            [InlineKeyboardButton("👥 مدیریت", callback_data='category_management')],
            [InlineKeyboardButton("💻 برنامه‌نویسی", callback_data='category_programming')],
            [InlineKeyboardButton("🏢 کسب‌وکار", callback_data='category_business')],
            [InlineKeyboardButton("📚 عمومی", callback_data='category_general')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_settings_menu(self):
        """منوی تنظیمات"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 زبان", callback_data='setting_language')],
            [InlineKeyboardButton("📏 طول محتوا", callback_data='setting_length')],
            [InlineKeyboardButton("🔔 اعلان‌ها", callback_data='setting_notifications')],
            [InlineKeyboardButton("💾 ذخیره خودکار", callback_data='setting_auto_save')],
            [InlineKeyboardButton("🏷️ دسته‌بندی‌های مورد علاقه", callback_data='setting_categories')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_feedback_menu(self):
        """منوی بازخورد"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ 5 ستاره", callback_data='rating_5')],
            [InlineKeyboardButton("⭐⭐⭐⭐ 4 ستاره", callback_data='rating_4')],
            [InlineKeyboardButton("⭐⭐⭐ 3 ستاره", callback_data='rating_3')],
            [InlineKeyboardButton("⭐⭐ 2 ستاره", callback_data='rating_2')],
            [InlineKeyboardButton("⭐ 1 ستاره", callback_data='rating_1')],
            [InlineKeyboardButton("💬 نظر متنی", callback_data='text_feedback')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_content_actions_menu(self, content_id: int):
        """منوی عملیات محتوا"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ مورد علاقه", callback_data=f'favorite_{content_id}')],
            [InlineKeyboardButton("📤 اشتراک‌گذاری", callback_data=f'share_{content_id}')],
            [InlineKeyboardButton("📅 یادآوری", callback_data=f'remind_{content_id}')],
            [InlineKeyboardButton("🗑️ حذف", callback_data=f'delete_{content_id}')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='saved_content')]
        ])

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت دستور /start"""
        user = update.effective_user
        user_id = user.id
        
        # ثبت کاربر در دیتابیس
        self.db.create_user(
            user_id=user_id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or ""
        )
        
        logger.info(f"User {user_id} started the bot")
        
        welcome_message = f"""👋 سلام {user.first_name or 'کاربر'}! 

🤖 من ربات پیشرفته تولید محتوای آموزشی هستم

🔥 قابلیت‌های جدید:
• 📝 تولید محتوای هوشمند و آموزشی
• 💾 ذخیره و مدیریت محتوا
• 📊 آمار و گزارش پیشرفته
• 🏷️ دسته‌بندی خودکار محتوا
• ⚙️ تنظیمات شخصی‌سازی
• 📅 یادآوری و زمان‌بندی
• ⭐ سیستم بازخورد
• 🔔 اعلان‌های هوشمند
• 📈 محدودیت روزانه: {MAX_DAILY_REQUESTS} درخواست

✨ کافیه موضوع مورد نظرتون رو بفرستین!"""
        
        await update.message.reply_text(
            welcome_message, 
            reply_markup=self.get_main_menu()
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور راهنما"""
        help_text = """📚 راهنمای کامل ربات

🔹 دستورات اصلی:
/start - شروع ربات
/help - راهنما
/analytics - آمار شخصی
/settings - تنظیمات
/saved - محتوای ذخیره شده
/feedback - بازخورد
/reminders - یادآوری‌ها

🔹 نحوه استفاده:
1️⃣ روی "📝 موضوع جدید" کلیک کنید
2️⃣ موضوع خود را بنویسید
3️⃣ منتظر تولید محتوا بمانید
4️⃣ از قابلیت‌های ذخیره و اشتراک‌گذاری استفاده کنید

🔹 قابلیت‌های پیشرفته:
• 💾 ذخیره خودکار محتوا
• 📊 آمار و گزارش‌های دقیق
• ⭐ سیستم بازخورد
• 📅 یادآوری‌های هوشمند
• 🔔 اعلان‌های شخصی‌سازی شده

💡 برای اطلاعات بیشتر، از منوی اصلی استفاده کنید."""
        
        await update.message.reply_text(help_text, reply_markup=self.get_main_menu())
    
    async def analytics_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور آمار"""
        user_id = update.effective_user.id
        analytics = self.analytics_manager.get_user_analytics(user_id)
        
        if not analytics:
            await update.message.reply_text("❌ خطا در دریافت آمار", reply_markup=self.get_main_menu())
            return
        
        analytics_text = f"""📊 آمار شخصی شما

📈 آمار کلی:
• کل درخواست‌ها: {analytics.get('total_requests', 0)}
• درخواست‌های موفق: {analytics.get('successful_requests', 0)}
• درخواست‌های ناموفق: {analytics.get('failed_requests', 0)}

🏆 دسته‌بندی‌های محبوب:"""
        
        for category, count in analytics.get('popular_categories', [])[:3]:
            analytics_text += f"\n• {category}: {count} درخواست"
        
        analytics_text += f"\n\n📅 آمار هفته گذشته:"
        for date, count in analytics.get('daily_stats', [])[:7]:
            analytics_text += f"\n• {date}: {count} درخواست"
        
        await update.message.reply_text(analytics_text, reply_markup=self.get_main_menu())
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور تنظیمات"""
        user_id = update.effective_user.id
        settings = self.db.get_user_settings(user_id)
        
        settings_text = f"""⚙️ تنظیمات شخصی

🔧 تنظیمات فعلی:
• زبان: {settings.get('language', 'fa')}
• طول محتوا: {settings.get('content_length', 'medium')}
• اعلان‌ها: {'فعال' if settings.get('notification_enabled', True) else 'غیرفعال'}
• ذخیره خودکار: {'فعال' if settings.get('auto_save', True) else 'غیرفعال'}
• دسته‌بندی‌های مورد علاقه: {settings.get('preferred_categories', 'general')}

💡 برای تغییر تنظیمات، از منوی زیر استفاده کنید."""
        
        await update.message.reply_text(settings_text, reply_markup=self.get_settings_menu())
    
    async def saved_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور محتوای ذخیره شده"""
        user_id = update.effective_user.id
        saved_content = self.db.get_saved_content(user_id, 5)
        
        if not saved_content:
            await update.message.reply_text(
                "💾 شما هنوز محتوایی ذخیره نکرده‌اید.\n\n💡 پس از تولید محتوا، می‌توانید آن را ذخیره کنید.",
                reply_markup=self.get_main_menu()
            )
            return
        
        content_text = "💾 آخرین محتوای ذخیره شده:\n\n"
        for i, content in enumerate(saved_content, 1):
            content_text += f"{i}. 📝 {content['topic']}\n"
            content_text += f"   🏷️ {content['category']}\n"
            content_text += f"   📅 {content['created_at'][:10]}\n"
            if content['is_favorite']:
                content_text += f"   ⭐ مورد علاقه\n"
            content_text += "\n"
        
        await update.message.reply_text(content_text, reply_markup=self.get_main_menu())
    
    async def feedback_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور بازخورد"""
        feedback_text = """⭐ سیستم بازخورد

لطفاً تجربه خود را از استفاده از ربات به اشتراک بگذارید:

• کیفیت محتوای تولید شده
• سرعت پاسخ‌دهی
• قابلیت‌های موجود
• پیشنهادات بهبود

نظرات شما به ما کمک می‌کند تا ربات را بهتر کنیم!"""
        
        await update.message.reply_text(feedback_text, reply_markup=self.get_feedback_menu())
    
    async def reminders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور یادآوری‌ها"""
        reminders_text = """📅 یادآوری‌ها

قابلیت‌های یادآوری:
• ⏰ یادآوری روزانه
• 📅 یادآوری هفتگی
• 🎯 یادآوری موضوعات خاص
• 📊 گزارش‌های دوره‌ای

💡 برای تنظیم یادآوری، از منوی تنظیمات استفاده کنید."""
        
        await update.message.reply_text(reminders_text, reply_markup=self.get_main_menu())

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت دکمه‌های اینلاین پیشرفته"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        action = query.data
        
        logger.info(f"User {user_id} pressed button: {action}")
        
        try:
            if action == 'new_topic':
                # بررسی محدودیت روزانه
                if not self.db.can_make_request(user_id):
                    await query.edit_message_text(
                        f"⚠️ محدودیت روزانه شما تمام شده است!\n\n📊 شما امروز {MAX_DAILY_REQUESTS} درخواست داشته‌اید.\n\n🕐 محدودیت فردا صبح ریست می‌شود.",
                        reply_markup=self.get_back_menu()
                    )
                    return
                
                self.user_states[user_id] = 'waiting_for_topic'
                message = """📝 موضوع جدید

لطفاً موضوع مورد نظر خود را بنویسید:

مثال‌ها:
• مدیریت فروش با هوش مصنوعی
• استراتژی‌های بازاریابی دیجیتال
• روش‌های افزایش بهره‌وری

💡 هرچه موضوع دقیق‌تر باشد، نتیجه بهتری خواهید گرفت."""
                await query.edit_message_text(
                    message, 
                    reply_markup=self.get_back_menu()
                )
                
            elif action == 'analytics':
                await self.show_analytics(query, user_id)
                
            elif action == 'settings':
                await self.show_settings(query, user_id)
                
            elif action == 'saved_content':
                await self.show_saved_content(query, user_id)
                
            elif action == 'feedback':
                await self.show_feedback_menu(query, user_id)
                
            elif action == 'reminders':
                await self.show_reminders(query, user_id)
                
            elif action == 'help':
                help_text = """📚 راهنمای استفاده پیشرفته

🔹 مراحل استفاده:
1️⃣ روی "📝 موضوع جدید" کلیک کنید
2️⃣ موضوع خود را بنویسید
3️⃣ صبر کنید تا تحقیق انجام شود (30-60 ثانیه)
4️⃣ دو پست آموزشی دریافت کنید

🔹 ویژگی‌های جدید:
• دسته‌بندی خودکار محتوا
• آمار و گزارش شخصی
• محدودیت روزانه: {MAX_DAILY_REQUESTS} درخواست
• تنظیمات شخصی‌سازی

🔹 دسته‌بندی‌ها:
• 🤖 هوش مصنوعی
• 📈 بازاریابی
• 👥 مدیریت
• 💻 برنامه‌نویسی
• 🏢 کسب‌وکار
• 📚 عمومی

⚠️ نکته: اگر متصل به اینترنت نیستید، ممکن است نتایج محدود باشد.""".format(MAX_DAILY_REQUESTS=MAX_DAILY_REQUESTS)
                await query.edit_message_text(
                    help_text, 
                    reply_markup=self.get_back_menu()
                )
                
            elif action == 'advanced_search':
                advanced_text = """🔬 جستجوی پیشرفته

برای نتایج بهتر، این نکات را رعایت کنید:

✅ مثال‌های خوب:
• "روش‌های افزایش فروش آنلاین برای کسب‌وکارهای کوچک"
• "استراتژی‌های بازاریابی محتوا در شبکه‌های اجتماعی"
• "تکنیک‌های مدیریت زمان برای کارآفرینان"

❌ مثال‌های بد:
• "فروش" (خیلی کلی)
• "بازاریابی" (غیردقیق)
• "موفقیت" (مبهم)

💡 نکات مفید:
• از کلمات کلیدی مشخص استفاده کنید
• هدف و مخاطب را مشخص کنید
• موضوع را محدود کنید
• دسته‌بندی مناسب انتخاب کنید"""
                await query.edit_message_text(
                    advanced_text, 
                    reply_markup=self.get_back_menu()
                )
                
            elif action == 'about':
                about_text = """🤖 درباره ربات پیشرفته

این ربات نسخه پیشرفته با قابلیت‌های جدید است:

🔹 جستجو در اینترنت
• DuckDuckGo
• Bing
• سایت‌های معتبر

🔹 تولید محتوا
• دسته‌بندی خودکار
• محتوای هوشمند
• هشتگ‌های مناسب

🔹 امکانات جدید
• آمار و گزارش شخصی
• محدودیت روزانه
• تنظیمات شخصی‌سازی
• پشتیبانی از فارسی

📧 در صورت بروز مشکل، با سازنده تماس بگیرید."""
                await query.edit_message_text(
                    about_text, 
                    reply_markup=self.get_back_menu()
                )
                
            elif action == 'main_menu':
                # پاک کردن وضعیت کاربر
                self.user_states.pop(user_id, None)
                welcome_message = """🤖 منوی اصلی

برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"""
                await query.edit_message_text(
                    welcome_message, 
                    reply_markup=self.get_main_menu()
                )
            
            # اضافه کردن handlers برای دسته‌بندی‌ها
            elif action.startswith('category_'):
                category = action.replace('category_', '')
                self.user_states[user_id] = f'waiting_for_topic_{category}'
                message = f"""📝 موضوع جدید - دسته‌بندی: {self._get_category_name(category)}

لطفاً موضوع مورد نظر خود را بنویسید:

مثال‌ها برای {self._get_category_name(category)}:
{self._get_category_examples(category)}

💡 هرچه موضوع دقیق‌تر باشد، نتیجه بهتری خواهید گرفت."""
                await query.edit_message_text(
                    message, 
                    reply_markup=self.get_back_menu()
                )
                
        except Exception as e:
            logger.error(f"Error in button handler: {e}")
            await query.edit_message_text(
                "❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.",
                reply_markup=self.get_main_menu()
            )
    
    async def show_analytics(self, query, user_id: int):
        """نمایش آمار کاربر"""
        user = self.db.get_user(user_id)
        if not user:
            await query.edit_message_text(
                "❌ اطلاعات کاربر یافت نشد.",
                reply_markup=self.get_back_menu()
            )
            return
        
        analytics_text = f"""📊 آمار شخصی شما

👤 اطلاعات کاربر:
• نام: {user['first_name']} {user['last_name'] or ''}
• تاریخ عضویت: {user['join_date'][:10]}

📈 آمار امروز:
• درخواست‌های امروز: {user['daily_requests']}/{MAX_DAILY_REQUESTS}
• وضعیت: {'✅ فعال' if user['daily_requests'] < MAX_DAILY_REQUESTS else '⚠️ محدود'}

🎯 دسته‌بندی مورد علاقه:
• {user['preferred_category'] or 'تنظیم نشده'}

💡 نکته: آمار هر روز صبح ریست می‌شود."""
        
        await query.edit_message_text(
            analytics_text,
            reply_markup=self.get_back_menu()
        )
    
    async def show_settings(self, query, user_id: int):
        """نمایش تنظیمات"""
        user = self.db.get_user(user_id)
        if not user:
            await query.edit_message_text(
                "❌ اطلاعات کاربر یافت نشد.",
                reply_markup=self.get_back_menu()
            )
            return
        
        settings_text = f"""⚙️ تنظیمات شخصی

🔧 تنظیمات فعلی:
• دسته‌بندی پیش‌فرض: {user['preferred_category'] or 'تنظیم نشده'}
• زبان: {user['language']}
• محدودیت روزانه: {MAX_DAILY_REQUESTS} درخواست

📝 برای تغییر تنظیمات:
• دسته‌بندی: از منوی اصلی انتخاب کنید
• سایر تنظیمات: با پشتیبانی تماس بگیرید

💡 تنظیمات در حافظه ربات ذخیره می‌شود."""
        
        await query.edit_message_text(
            settings_text,
            reply_markup=self.get_settings_menu()
        )
    
    async def show_saved_content(self, query, user_id: int):
        """نمایش محتوای ذخیره شده"""
        saved_content = self.db.get_saved_content(user_id, 10)
        
        if not saved_content:
            await query.edit_message_text(
                "💾 شما هنوز محتوایی ذخیره نکرده‌اید.\n\n💡 پس از تولید محتوا، می‌توانید آن را ذخیره کنید.",
                reply_markup=self.get_back_menu()
            )
            return
        
        content_text = "💾 محتوای ذخیره شده شما:\n\n"
        for i, content in enumerate(saved_content[:5], 1):
            content_text += f"{i}. 📝 {content['topic']}\n"
            content_text += f"   🏷️ {content['category']}\n"
            content_text += f"   📅 {content['created_at'][:10]}\n"
            if content['is_favorite']:
                content_text += f"   ⭐ مورد علاقه\n"
            content_text += "\n"
        
        if len(saved_content) > 5:
            content_text += f"... و {len(saved_content) - 5} مورد دیگر"
        
        await query.edit_message_text(
            content_text,
            reply_markup=self.get_back_menu()
        )
    
    async def show_feedback_menu(self, query, user_id: int):
        """نمایش منوی بازخورد"""
        feedback_text = """⭐ سیستم بازخورد

لطفاً تجربه خود را از استفاده از ربات به اشتراک بگذارید:

• کیفیت محتوای تولید شده
• سرعت پاسخ‌دهی
• قابلیت‌های موجود
• پیشنهادات بهبود

نظرات شما به ما کمک می‌کند تا ربات را بهتر کنیم!"""
        
        await query.edit_message_text(
            feedback_text,
            reply_markup=self.get_feedback_menu()
        )
    
    async def show_reminders(self, query, user_id: int):
        """نمایش یادآوری‌ها"""
        # اینجا می‌توانید یادآوری‌های کاربر را نمایش دهید
        reminders_text = """📅 یادآوری‌ها

قابلیت‌های یادآوری:
• ⏰ یادآوری روزانه
• 📅 یادآوری هفتگی
• 🎯 یادآوری موضوعات خاص
• 📊 گزارش‌های دوره‌ای

💡 برای تنظیم یادآوری، از منوی تنظیمات استفاده کنید."""
        
        await query.edit_message_text(
            reminders_text,
            reply_markup=self.get_back_menu()
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت پیام‌های کاربر پیشرفته"""
        user_id = update.effective_user.id
        topic = update.message.text.strip()
        
        # بررسی وضعیت کاربر
        user_state = self.user_states.get(user_id, '')
        if not user_state.startswith('waiting_for_topic'):
            await update.message.reply_text(
                "👋 سلام! برای شروع، از منوی زیر استفاده کنید:",
                reply_markup=self.get_main_menu()
            )
            return
        
        # بررسی محدودیت روزانه
        if not self.db.can_make_request(user_id):
            await update.message.reply_text(
                f"⚠️ محدودیت روزانه شما تمام شده است!\n\n📊 شما امروز {MAX_DAILY_REQUESTS} درخواست داشته‌اید.\n\n🕐 محدودیت فردا صبح ریست می‌شود.",
                reply_markup=self.get_main_menu()
            )
            return
        
        # پاک کردن وضعیت کاربر
        self.user_states.pop(user_id, None)
        
        logger.info(f"User {user_id} requested topic: {topic}")
        
        # بررسی طول موضوع
        if len(topic) < 3:
            await update.message.reply_text(
                "⚠️ لطفاً موضوع دقیق‌تری وارد کنید (حداقل 3 کاراکتر)",
                reply_markup=self.get_main_menu()
            )
            return
        
        # تشخیص دسته‌بندی
        if user_state == 'waiting_for_topic':
            category = self.content_generator.detect_category(topic)
        else:
            # استخراج دسته‌بندی از وضعیت کاربر
            category = user_state.replace('waiting_for_topic_', '')
            if category not in ['ai', 'marketing', 'management', 'programming', 'business', 'general']:
                category = 'general'
        
        # ثبت درخواست در دیتابیس
        self.db.log_request(user_id, topic, category)
        
        # ارسال پیام وضعیت
        status_message = await update.message.reply_text("🔍 شروع تحقیق... لطفاً صبر کنید")
        
        try:
            # نمایش typing
            await update.message.chat.send_action(ChatAction.TYPING)
            
            # ایجاد session
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self.scraper = ContentScraper(session)
                
                # مرحله 1: جستجو و تحقیق
                await status_message.edit_text("🔍 در حال جستجو در اینترنت...")
                research_content, sources = await self.scraper.comprehensive_research(topic)
                
                if not research_content:
                    await status_message.edit_text(
                        "❌ متاسفانه نتوانستم اطلاعات کافی پیدا کنم. لطفاً موضوع دیگری امتحان کنید.",
                        reply_markup=self.get_main_menu()
                    )
                    return
                
                # مرحله 2: تولید محتوا
                await status_message.edit_text("🤖 در حال تولید محتوا...")
                
                # تولید محتوای پیشرفته با Metis API
                posts = []
                try:
                    logger.info("Attempting to use Metis API for content generation...")
                    posts = await self.content_generator.create_metis_posts(
                        self.metis_api, session, topic, research_content
                    )
                    logger.info(f"Successfully generated {len(posts)} posts with Metis API")
                except Exception as e:
                    logger.warning(f"Metis API failed: {e}")
                    logger.info("Falling back to local content generation...")
                    try:
                        posts = self.content_generator.create_advanced_posts(topic, research_content, category)
                        if not posts or len(posts) < 2:
                            posts = self.content_generator.create_advanced_posts(topic, research_content, 'general')
                    except Exception as e2:
                        logger.error(f"Local content generation also failed: {e2}")
                        posts = [
                            f"📚 {topic}\n\nاین موضوع شامل مباحث مهمی در حوزه مربوطه است که نیاز به بررسی دقیق دارد.",
                            f"💡 کاربردهای عملی {topic}:\n\n• اهمیت در صنعت\n• روش‌های پیاده‌سازی\n• مزایای استفاده\n\nبرای اطلاعات بیشتر، منابع معتبر را بررسی کنید."
                        ]
                
                # اطلاع به کاربر
                await update.message.reply_text(
                    f"✅ محتوای آموزشی در دسته‌بندی {self._get_category_name(category)} آماده شده است!"
                )
                
                # حذف پیام وضعیت
                await status_message.delete()
                
                # ذخیره محتوا در دیتابیس
                user_settings = self.db.get_user_settings(user_id)
                if user_settings.get('auto_save', True):
                    for i, post in enumerate(posts, 1):
                        self.db.save_content(user_id, topic, category, post)
                
                # ارسال پست‌ها
                for i, post in enumerate(posts, 1):
                    await update.message.chat.send_action(ChatAction.TYPING)
                    await asyncio.sleep(1)
                    
                    # اضافه کردن دکمه‌های عملیات
                    action_buttons = [
                        [InlineKeyboardButton("💾 ذخیره", callback_data=f'save_post_{i}'),
                         InlineKeyboardButton("⭐ مورد علاقه", callback_data=f'favorite_post_{i}')],
                        [InlineKeyboardButton("📤 اشتراک‌گذاری", callback_data=f'share_post_{i}'),
                         InlineKeyboardButton("📅 یادآوری", callback_data=f'remind_post_{i}')]
                    ]
                    
                    # تقسیم پست اگر خیلی طولانی باشد
                    if len(post) > 4000:
                        chunks = self.split_text(post, 4000)
                        for j, chunk in enumerate(chunks, 1):
                            await update.message.reply_text(
                                f"📝 پست {i} (قسمت {j}/{len(chunks)}):\n\n{chunk}",
                                reply_markup=InlineKeyboardMarkup(action_buttons) if j == len(chunks) else None
                            )
                    else:
                        await update.message.reply_text(
                            f"📝 پست {i}:\n\n{post}",
                            reply_markup=InlineKeyboardMarkup(action_buttons)
                        )
                
                # ارسال منابع
                if sources:
                    sources_text = "📚 منابع مفید:\n\n"
                    for i, source in enumerate(sources[:5], 1):
                        # تمیز کردن URL
                        clean_url = source['url']
                        if clean_url.startswith('https://duckduckgo.com/l/?uddg='):
                            try:
                                import urllib.parse
                                decoded_url = urllib.parse.unquote(clean_url.split('uddg=')[1].split('&')[0])
                                clean_url = decoded_url
                            except:
                                pass
                        
                        sources_text += f"{i}. [{source['title']}]({clean_url})\n\n"
                    
                    # ارسال با Markdown برای هایپرلینک
                    try:
                        await update.message.reply_text(
                            sources_text,
                            parse_mode='Markdown',
                            disable_web_page_preview=True,
                            reply_markup=self.get_main_menu()
                        )
                    except:
                        # اگر Markdown کار نکرد، بدون هایپرلینک
                        sources_text_plain = "📚 منابع مفید:\n\n"
                        for i, source in enumerate(sources[:5], 1):
                            clean_url = source['url']
                            if clean_url.startswith('https://duckduckgo.com/l/?uddg='):
                                try:
                                    import urllib.parse
                                    decoded_url = urllib.parse.unquote(clean_url.split('uddg=')[1].split('&')[0])
                                    clean_url = decoded_url
                                except:
                                    pass
                            sources_text_plain += f"{i}. {source['title']}\n{clean_url}\n\n"
                        
                        await update.message.reply_text(
                            sources_text_plain,
                            reply_markup=self.get_main_menu()
                        )
                else:
                    await update.message.reply_text(
                        "✅ پست‌های شما آماده شد!",
                        reply_markup=self.get_main_menu()
                    )
                    
        except Exception as e:
            logger.error(f"Error in handle_message: {e}")
            await status_message.edit_text(
                "❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.",
                reply_markup=self.get_main_menu()
            )
    
    def _get_category_name(self, category: str) -> str:
        """دریافت نام فارسی دسته‌بندی"""
        names = {
            'ai': 'هوش مصنوعی',
            'marketing': 'بازاریابی',
            'management': 'مدیریت',
            'programming': 'برنامه‌نویسی',
            'business': 'کسب‌وکار',
            'general': 'عمومی'
        }
        return names.get(category, 'عمومی')
    
    def _get_category_examples(self, category: str) -> str:
        """دریافت مثال‌های مناسب برای دسته‌بندی"""
        examples = {
            'ai': """• مدیریت فروش با هوش مصنوعی
• چت‌بات‌های هوشمند
• تحلیل داده با ML
• اتوماسیون فرآیندها""",
            'marketing': """• استراتژی‌های بازاریابی دیجیتال
• تبلیغات در شبکه‌های اجتماعی
• بازاریابی محتوا
• SEO و بهینه‌سازی""",
            'management': """• مدیریت تیم و رهبری
• مدیریت پروژه
• مدیریت زمان
• تصمیم‌گیری استراتژیک""",
            'programming': """• یادگیری پایتون
• توسعه وب
• برنامه‌نویسی موبایل
• هوش مصنوعی و ML""",
            'business': """• راه‌اندازی استارتاپ
• مدیریت مالی
• استراتژی کسب‌وکار
• کارآفرینی""",
            'general': """• مهارت‌های زندگی
• توسعه فردی
• یادگیری سریع
• موفقیت و انگیزه"""
        }
        return examples.get(category, "• موضوعات عمومی و کاربردی")

    def split_text(self, text: str, max_length: int) -> List[str]:
        """تقسیم متن به قطعات کوچک‌تر"""
        if len(text) <= max_length:
            return [text]
        
        chunks = []
        current_chunk = ""
        
        for sentence in text.split('. '):
            if len(current_chunk + sentence) <= max_length:
                current_chunk += sentence + '. '
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + '. '
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

    def run(self):
        """اجرای ربات پیشرفته"""
        try:
            # ایجاد application
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            
            # اضافه کردن handlers
            application.add_handler(CommandHandler("start", self.start_command))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CommandHandler("analytics", self.analytics_command))
            application.add_handler(CommandHandler("settings", self.settings_command))
            application.add_handler(CommandHandler("saved", self.saved_command))
            application.add_handler(CommandHandler("feedback", self.feedback_command))
            application.add_handler(CommandHandler("reminders", self.reminders_command))
            application.add_handler(CallbackQueryHandler(self.button_handler))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            
            # شروع ربات
            logger.info("Advanced Bot started successfully!")
            logger.info(f"Database path: {self.db.db_path}")
            logger.info(f"Max daily requests: {MAX_DAILY_REQUESTS}")
            
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise

def main():
    """تابع اصلی"""
    bot = None
    try:
        # بررسی کلیدهای API
        if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            logger.error("لطفاً TELEGRAM_BOT_TOKEN را تنظیم کنید!")
            return
        
        # بررسی وجود دیتابیس
        if not os.path.exists("bot_database.db"):
            logger.info("Creating new database...")
        
        # ایجاد و اجرای ربات پیشرفته
        bot = AdvancedTelegramBot()
        logger.info("Bot initialized successfully")
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Critical error in main: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    finally:
        if bot:
            logger.info("Cleaning up bot resources...")
        logger.info("Bot shutdown complete")

if __name__ == "__main__":
    main() 
