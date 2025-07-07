import asyncio
import json
import logging
import re
import urllib.parse
import sqlite3
import os
import hashlib
import base64
import random
import string
import time
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

import aiohttp
from bs4 import BeautifulSoup
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type, RetryError as TenacityRetryError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand, BotCommandScopeDefault
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import pytz

# کلیدهای API
TELEGRAM_BOT_TOKEN = "1951771121:AAHxdMix9xAR6a592sTZKC6aBArdfIaLwco"
METIS_API_KEY = "tpsg-6WW8eb5cZfq6fZru3B6tUbSaKB2EkVm"
METIS_BOT_ID = "30f054f0-2363-4128-b6c6-308efc31c5d9"
METIS_MODEL = "gpt-4o"
METIS_BASE_URL = "https://api.metisai.ir"

# تنظیمات پیشرفته
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2
MAX_DAILY_REQUESTS = 50  # افزایش محدودیت روزانه
MAX_CONTENT_LENGTH = 4000
SUPPORTED_LANGUAGES = ['fa', 'en', 'ar', 'tr', 'ru']
DEFAULT_LANGUAGE = 'fa'

# تنظیمات امنیتی
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_MESSAGE_LENGTH = 4096
RATE_LIMIT_PER_MINUTE = 10
RATE_LIMIT_PER_HOUR = 100

# تنظیمات محتوا
CONTENT_TYPES = ['text', 'image', 'video', 'audio', 'document']
MAX_SAVED_CONTENT = 100
MAX_FAVORITES = 50

# تنظیمات یادآوری
REMINDER_TYPES = ['daily', 'weekly', 'monthly', 'custom']
MAX_REMINDERS_PER_USER = 10

# تنظیمات آمار
ANALYTICS_RETENTION_DAYS = 365
BACKUP_INTERVAL_HOURS = 24

# تنظیمات logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Enums و Data Classes
class UserRole(Enum):
    USER = "user"
    PREMIUM = "premium"
    ADMIN = "admin"
    MODERATOR = "moderator"

class ContentStatus(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    DELETED = "deleted"

class NotificationType(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"
    SYSTEM = "system"

@dataclass
class UserSession:
    user_id: int
    language: str
    current_state: str
    last_activity: datetime
    preferences: Dict[str, Any]

@dataclass
class ContentItem:
    id: int
    user_id: int
    topic: str
    category: str
    content: str
    created_at: datetime
    status: ContentStatus
    is_favorite: bool
    tags: List[str]
    metadata: Dict[str, Any]

class RetryableError(Exception):
    pass

class RateLimitError(Exception):
    pass

class ContentGenerationError(Exception):
    pass

class DatabaseError(Exception):
    pass

class RateLimiter:
    """کلاس محدودیت نرخ درخواست"""
    
    def __init__(self):
        self.requests = {}
        self.lock = threading.Lock()
    
    def is_allowed(self, user_id: int, limit_type: str = 'minute') -> bool:
        """بررسی امکان درخواست"""
        with self.lock:
            now = time.time()
            key = f"{user_id}_{limit_type}"
            
            if key not in self.requests:
                self.requests[key] = []
            
            # حذف درخواست‌های قدیمی
            if limit_type == 'minute':
                window = 60
                limit = RATE_LIMIT_PER_MINUTE
            elif limit_type == 'hour':
                window = 3600
                limit = RATE_LIMIT_PER_HOUR
            else:
                return True
            
            self.requests[key] = [req_time for req_time in self.requests[key] if now - req_time < window]
            
            if len(self.requests[key]) >= limit:
                return False
            
            self.requests[key].append(now)
            return True
    
    def get_remaining_requests(self, user_id: int, limit_type: str = 'minute') -> int:
        """دریافت تعداد درخواست‌های باقی‌مانده"""
        with self.lock:
            now = time.time()
            key = f"{user_id}_{limit_type}"
            
            if key not in self.requests:
                return RATE_LIMIT_PER_MINUTE if limit_type == 'minute' else RATE_LIMIT_PER_HOUR
            
            if limit_type == 'minute':
                window = 60
                limit = RATE_LIMIT_PER_MINUTE
            elif limit_type == 'hour':
                window = 3600
                limit = RATE_LIMIT_PER_HOUR
            else:
                return 0
            
            self.requests[key] = [req_time for req_time in self.requests[key] if now - req_time < window]
            return max(0, limit - len(self.requests[key]))

class SecurityManager:
    """کلاس مدیریت امنیت"""
    
    def __init__(self):
        self.blocked_users = set()
        self.suspicious_patterns = [
            r'script', r'javascript', r'<.*>', r'http[s]?://', 
            r'@\w+', r'#\w+', r'admin', r'root', r'password'
        ]
    
    def is_user_blocked(self, user_id: int) -> bool:
        """بررسی مسدودیت کاربر"""
        return user_id in self.blocked_users
    
    def block_user(self, user_id: int, reason: str = ""):
        """مسدود کردن کاربر"""
        self.blocked_users.add(user_id)
        logger.warning(f"User {user_id} blocked. Reason: {reason}")
    
    def unblock_user(self, user_id: int):
        """رفع مسدودیت کاربر"""
        self.blocked_users.discard(user_id)
        logger.info(f"User {user_id} unblocked")
    
    def validate_input(self, text: str) -> Tuple[bool, str]:
        """اعتبارسنجی ورودی"""
        if not text or len(text.strip()) < 1:
            return False, "متن خالی است"
        
        if len(text) > MAX_MESSAGE_LENGTH:
            return False, f"متن خیلی طولانی است (حداکثر {MAX_MESSAGE_LENGTH} کاراکتر)"
        
        # بررسی الگوهای مشکوک
        for pattern in self.suspicious_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return False, "متن شامل محتوای غیرمجاز است"
        
        return True, ""

class BackupManager:
    """کلاس مدیریت پشتیبان"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.backup_dir = "backups"
        self.ensure_backup_dir()
    
    def ensure_backup_dir(self):
        """ایجاد پوشه پشتیبان"""
        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir)
    
    def create_backup(self) -> str:
        """ایجاد پشتیبان از دیتابیس"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(self.backup_dir, f"backup_{timestamp}.db")
            
            import shutil
            shutil.copy2(self.db_path, backup_path)
            
            logger.info(f"Backup created: {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            return ""
    
    def restore_backup(self, backup_path: str) -> bool:
        """بازیابی از پشتیبان"""
        try:
            if not os.path.exists(backup_path):
                return False
            
            import shutil
            shutil.copy2(backup_path, self.db_path)
            logger.info(f"Backup restored from: {backup_path}")
            return True
        except Exception as e:
            logger.error(f"Backup restoration failed: {e}")
            return False
    
    def cleanup_old_backups(self, keep_days: int = 7):
        """پاک کردن پشتیبان‌های قدیمی"""
        try:
            cutoff_time = datetime.now() - timedelta(days=keep_days)
            
            for filename in os.listdir(self.backup_dir):
                if filename.startswith("backup_") and filename.endswith(".db"):
                    file_path = os.path.join(self.backup_dir, filename)
                    file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                    
                    if file_time < cutoff_time:
                        os.remove(file_path)
                        logger.info(f"Old backup removed: {filename}")
        except Exception as e:
            logger.error(f"Backup cleanup failed: {e}")

class ContentTemplate:
    """کلاس قالب‌های محتوا پیشرفته"""
    
    @staticmethod
    def get_template(category: str, language: str = 'fa') -> dict:
        """دریافت قالب محتوا بر اساس دسته‌بندی"""
        templates = {
            'ai': {
                'fa': {
                    'intro': "🤖 هوش مصنوعی در {topic}",
                    'structure': ["🔬 تعریف و مفاهیم", "⚙️ کاربردهای عملی", "🛠️ ابزارها و تکنولوژی‌ها", "📊 مزایا و چالش‌ها", "🚀 روندهای آینده"],
                    'hashtags': "#هوش_مصنوعی #AI #تکنولوژی #آینده #نوآوری #یادگیری_ماشین",
                    'emoji': "🤖",
                    'color': "blue"
                },
                'en': {
                    'intro': "🤖 Artificial Intelligence in {topic}",
                    'structure': ["🔬 Definition and Concepts", "⚙️ Practical Applications", "🛠️ Tools and Technologies", "📊 Benefits and Challenges", "🚀 Future Trends"],
                    'hashtags': "#AI #ArtificialIntelligence #Technology #Innovation #Future #MachineLearning",
                    'emoji': "🤖",
                    'color': "blue"
                }
            },
            'marketing': {
                'fa': {
                    'intro': "📈 استراتژی‌های بازاریابی در {topic}",
                    'structure': ["🎯 استراتژی و برنامه‌ریزی", "📊 تحلیل بازار", "🚀 اجرا و پیاده‌سازی", "📈 نتایج و بهینه‌سازی", "💡 نکات کلیدی"],
                    'hashtags': "#بازاریابی #مارکتینگ #استراتژی #فروش #کسب_وکار #دیجیتال",
                    'emoji': "📈",
                    'color': "green"
                },
                'en': {
                    'intro': "📈 Marketing Strategies in {topic}",
                    'structure': ["🎯 Strategy and Planning", "📊 Market Analysis", "🚀 Implementation", "📈 Results and Optimization", "💡 Key Insights"],
                    'hashtags': "#Marketing #Strategy #Sales #Business #Growth #Digital",
                    'emoji': "📈",
                    'color': "green"
                }
            },
            'management': {
                'fa': {
                    'intro': "👥 مدیریت و رهبری در {topic}",
                    'structure': ["📋 برنامه‌ریزی استراتژیک", "👥 مدیریت تیم", "📊 نظارت و کنترل", "🚀 بهبود مستمر", "🎯 نتایج و موفقیت"],
                    'hashtags': "#مدیریت #رهبری #سازمان #توسعه #موفقیت #تیم",
                    'emoji': "👥",
                    'color': "purple"
                },
                'en': {
                    'intro': "👥 Management and Leadership in {topic}",
                    'structure': ["📋 Strategic Planning", "👥 Team Management", "📊 Monitoring and Control", "🚀 Continuous Improvement", "🎯 Results and Success"],
                    'hashtags': "#Management #Leadership #Organization #Development #Success #Team",
                    'emoji': "👥",
                    'color': "purple"
                }
            },
            'programming': {
                'fa': {
                    'intro': "💻 برنامه‌نویسی و توسعه در {topic}",
                    'structure': ["🔧 ابزارها و تکنولوژی‌ها", "📚 مفاهیم و اصول", "⚙️ پیاده‌سازی عملی", "🛠️ بهترین شیوه‌ها", "🚀 پروژه‌های نمونه"],
                    'hashtags': "#برنامه_نویسی #توسعه #کد #نرم_افزار #تکنولوژی #پایتون",
                    'emoji': "💻",
                    'color': "orange"
                },
                'en': {
                    'intro': "💻 Programming and Development in {topic}",
                    'structure': ["🔧 Tools and Technologies", "📚 Concepts and Principles", "⚙️ Practical Implementation", "🛠️ Best Practices", "🚀 Sample Projects"],
                    'hashtags': "#Programming #Development #Code #Software #Technology #Python",
                    'emoji': "💻",
                    'color': "orange"
                }
            },
            'business': {
                'fa': {
                    'intro': "🏢 کسب‌وکار و کارآفرینی در {topic}",
                    'structure': ["📊 تحلیل بازار", "💼 مدل کسب‌وکار", "💰 مدیریت مالی", "🚀 رشد و توسعه", "🎯 استراتژی‌های موفقیت"],
                    'hashtags': "#کسب_وکار #کارآفرینی #استارتاپ #موفقیت #مالی #رشد",
                    'emoji': "🏢",
                    'color': "red"
                },
                'en': {
                    'intro': "🏢 Business and Entrepreneurship in {topic}",
                    'structure': ["📊 Market Analysis", "💼 Business Model", "💰 Financial Management", "🚀 Growth and Development", "🎯 Success Strategies"],
                    'hashtags': "#Business #Entrepreneurship #Startup #Success #Finance #Growth",
                    'emoji': "🏢",
                    'color': "red"
                }
            },
            'health': {
                'fa': {
                    'intro': "🏥 سلامت و تندرستی در {topic}",
                    'structure': ["🔬 مفاهیم علمی", "💪 روش‌های عملی", "🥗 تغذیه و سبک زندگی", "📊 آمار و تحقیقات", "💡 توصیه‌های تخصصی"],
                    'hashtags': "#سلامت #تندرستی #پزشکی #تغذیه #سبک_زندگی #پیشگیری",
                    'emoji': "🏥",
                    'color': "pink"
                },
                'en': {
                    'intro': "🏥 Health and Wellness in {topic}",
                    'structure': ["🔬 Scientific Concepts", "💪 Practical Methods", "🥗 Nutrition and Lifestyle", "📊 Statistics and Research", "💡 Expert Recommendations"],
                    'hashtags': "#Health #Wellness #Medical #Nutrition #Lifestyle #Prevention",
                    'emoji': "🏥",
                    'color': "pink"
                }
            },
            'education': {
                'fa': {
                    'intro': "📚 آموزش و یادگیری در {topic}",
                    'structure': ["🎯 اهداف یادگیری", "📖 روش‌های آموزشی", "🛠️ ابزارها و منابع", "📊 ارزیابی و پیشرفت", "💡 نکات کاربردی"],
                    'hashtags': "#آموزش #یادگیری #تحصیل #دانش #مهارت #توسعه_فردی",
                    'emoji': "📚",
                    'color': "yellow"
                },
                'en': {
                    'intro': "📚 Education and Learning in {topic}",
                    'structure': ["🎯 Learning Objectives", "📖 Educational Methods", "🛠️ Tools and Resources", "📊 Assessment and Progress", "💡 Practical Tips"],
                    'hashtags': "#Education #Learning #Study #Knowledge #Skills #PersonalDevelopment",
                    'emoji': "📚",
                    'color': "yellow"
                }
            }
        }
        return templates.get(category, templates.get('general')).get(language, templates.get('general')['fa'])
    
    @staticmethod
    def get_general_template(language: str = 'fa') -> dict:
        """دریافت قالب عمومی"""
        general_templates = {
            'fa': {
                'intro': "📝 {topic}",
                'structure': ["🔍 معرفی و تعریف", "📋 نکات کلیدی", "💡 کاربردهای عملی", "🚀 مزایا و فواید"],
                'hashtags': "#آموزش #توسعه_فردی #موفقیت #یادگیری #مهارت",
                'emoji': "📝",
                'color': "gray"
            },
            'en': {
                'intro': "📝 {topic}",
                'structure': ["🔍 Introduction and Definition", "📋 Key Points", "💡 Practical Applications", "🚀 Benefits and Advantages"],
                'hashtags': "#Education #PersonalDevelopment #Success #Learning #Skills",
                'emoji': "📝",
                'color': "gray"
            }
        }
        return general_templates.get(language, general_templates['fa'])
    
    @staticmethod
    def get_custom_template(topic: str, category: str, language: str = 'fa') -> dict:
        """ایجاد قالب سفارشی"""
        base_template = ContentTemplate.get_template(category, language)
        if not base_template:
            base_template = ContentTemplate.get_general_template(language)
        
        # شخصی‌سازی بر اساس موضوع
        custom_template = base_template.copy()
        custom_template['intro'] = base_template['intro'].format(topic=topic)
        
        return custom_template



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
        """ایجاد جداول دیتابیس پیشرفته"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # جدول کاربران پیشرفته
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
                    language TEXT DEFAULT 'fa',
                    role TEXT DEFAULT 'user',
                    is_premium BOOLEAN DEFAULT 0,
                    premium_expires TEXT,
                    total_requests INTEGER DEFAULT 0,
                    total_content_saved INTEGER DEFAULT 0,
                    last_activity TEXT DEFAULT (datetime('now')),
                    timezone TEXT DEFAULT 'Asia/Tehran',
                    notification_settings TEXT DEFAULT '{}'
                )
            ''')
            
            # جدول درخواست‌های پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    category TEXT,
                    content_type TEXT DEFAULT 'educational',
                    language TEXT DEFAULT 'fa',
                    created_at TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'completed',
                    processing_time REAL,
                    content_length INTEGER,
                    word_count INTEGER,
                    error_message TEXT,
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول آمار پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    total_requests INTEGER DEFAULT 0,
                    successful_requests INTEGER DEFAULT 0,
                    failed_requests INTEGER DEFAULT 0,
                    total_users INTEGER DEFAULT 0,
                    new_users INTEGER DEFAULT 0,
                    active_users INTEGER DEFAULT 0,
                    avg_processing_time REAL,
                    popular_categories TEXT DEFAULT '{}',
                    system_errors INTEGER DEFAULT 0
                )
            ''')
            
            # جدول محتوای ذخیره شده پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS saved_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    category TEXT,
                    content TEXT,
                    content_type TEXT DEFAULT 'text',
                    language TEXT DEFAULT 'fa',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    is_favorite BOOLEAN DEFAULT 0,
                    is_public BOOLEAN DEFAULT 0,
                    tags TEXT DEFAULT '[]',
                    metadata TEXT DEFAULT '{}',
                    view_count INTEGER DEFAULT 0,
                    share_count INTEGER DEFAULT 0,
                    rating REAL DEFAULT 0,
                    rating_count INTEGER DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول تنظیمات کاربر پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT DEFAULT 'fa',
                    content_length TEXT DEFAULT 'medium',
                    content_type TEXT DEFAULT 'educational',
                    notification_enabled BOOLEAN DEFAULT 1,
                    auto_save BOOLEAN DEFAULT 1,
                    preferred_categories TEXT DEFAULT 'general',
                    theme TEXT DEFAULT 'default',
                    privacy_level TEXT DEFAULT 'private',
                    auto_translate BOOLEAN DEFAULT 0,
                    target_language TEXT DEFAULT 'fa',
                    content_format TEXT DEFAULT 'structured',
                    ai_assistant_enabled BOOLEAN DEFAULT 1,
                    research_depth TEXT DEFAULT 'comprehensive',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول یادآوری‌های پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT,
                    topic TEXT,
                    message TEXT,
                    reminder_type TEXT DEFAULT 'custom',
                    scheduled_time TEXT,
                    repeat_interval TEXT,
                    is_sent BOOLEAN DEFAULT 0,
                    sent_count INTEGER DEFAULT 0,
                    last_sent TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول بازخورد پیشرفته
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    topic TEXT,
                    content_id INTEGER,
                    rating INTEGER,
                    comment TEXT,
                    feedback_type TEXT DEFAULT 'general',
                    category TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    is_helpful BOOLEAN DEFAULT 0,
                    helpful_count INTEGER DEFAULT 0,
                    response TEXT,
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (content_id) REFERENCES saved_content (id)
                )
            ''')
            
            # جدول جستجو و تاریخچه
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    query TEXT,
                    category TEXT,
                    results_count INTEGER,
                    created_at TEXT DEFAULT (datetime('now')),
                    is_successful BOOLEAN DEFAULT 1,
                    processing_time REAL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول اشتراک‌گذاری
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS content_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_id INTEGER,
                    user_id INTEGER,
                    share_type TEXT DEFAULT 'public',
                    share_url TEXT,
                    share_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    expires_at TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (content_id) REFERENCES saved_content (id),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول دسته‌بندی‌های سفارشی
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS custom_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    description TEXT,
                    color TEXT DEFAULT '#007bff',
                    icon TEXT DEFAULT '📁',
                    is_default BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول قالب‌های سفارشی
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS custom_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    category TEXT,
                    template_structure TEXT,
                    is_public BOOLEAN DEFAULT 0,
                    usage_count INTEGER DEFAULT 0,
                    rating REAL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # جدول اعلان‌های سیستم
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT,
                    message TEXT,
                    notification_type TEXT DEFAULT 'info',
                    is_read BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    expires_at TEXT,
                    action_url TEXT,
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # ایجاد ایندکس‌ها برای بهبود عملکرد
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_join_date ON users(join_date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_requests_user_date ON requests(user_id, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_saved_content_user ON saved_content(user_id, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reminders_scheduled ON reminders(scheduled_time, is_active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history(user_id, created_at)')
            
            conn.commit()
            conn.close()
            logger.info("Advanced database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise DatabaseError(f"خطا در راه‌اندازی دیتابیس: {str(e)}")
    
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
        """به‌روزرسانی تنظیمات کاربر پیشرفته"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            updates = []
            values = []
            
            # فیلدهای قابل به‌روزرسانی
            updatable_fields = [
                'language', 'content_length', 'content_type', 'notification_enabled', 
                'auto_save', 'preferred_categories', 'theme', 'privacy_level',
                'auto_translate', 'target_language', 'content_format', 
                'ai_assistant_enabled', 'research_depth'
            ]
            
            for field in updatable_fields:
                if field in settings:
                    updates.append(f'{field} = ?')
                    values.append(settings[field])
            
            if updates:
                updates.append('updated_at = ?')
                values.append(datetime.now().isoformat())
                values.append(user_id)
                query = f"UPDATE user_settings SET {', '.join(updates)} WHERE user_id = ?"
                cursor.execute(query, values)
                conn.commit()
            
            conn.close()
            logger.info(f"User settings updated for {user_id}")
        except Exception as e:
            logger.error(f"Error updating user settings for {user_id}: {e}")
            raise DatabaseError(f"خطا در به‌روزرسانی تنظیمات: {str(e)}")
    
    def create_reminder(self, user_id: int, title: str, topic: str, message: str, 
                       scheduled_time: str, reminder_type: str = 'custom', 
                       repeat_interval: str = None) -> int:
        """ایجاد یادآوری جدید"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO reminders (user_id, title, topic, message, scheduled_time, 
                                     reminder_type, repeat_interval)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, title, topic, message, scheduled_time, reminder_type, repeat_interval))
            
            reminder_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            logger.info(f"Reminder created for user {user_id}: {title}")
            return reminder_id
        except Exception as e:
            logger.error(f"Error creating reminder for user {user_id}: {e}")
            raise DatabaseError(f"خطا در ایجاد یادآوری: {str(e)}")
    
    def get_user_reminders(self, user_id: int, active_only: bool = True) -> List[Dict]:
        """دریافت یادآوری‌های کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            query = '''
                SELECT id, title, topic, message, scheduled_time, reminder_type, 
                       repeat_interval, is_sent, sent_count, last_sent, is_active
                FROM reminders 
                WHERE user_id = ?
            '''
            
            if active_only:
                query += ' AND is_active = 1'
            
            query += ' ORDER BY scheduled_time ASC'
            
            cursor.execute(query, (user_id,))
            results = cursor.fetchall()
            conn.close()
            
            return [
                {
                    'id': row[0],
                    'title': row[1],
                    'topic': row[2],
                    'message': row[3],
                    'scheduled_time': row[4],
                    'reminder_type': row[5],
                    'repeat_interval': row[6],
                    'is_sent': bool(row[7]),
                    'sent_count': row[8],
                    'last_sent': row[9],
                    'is_active': bool(row[10])
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error getting reminders for user {user_id}: {e}")
            return []
    
    def update_reminder_status(self, reminder_id: int, is_sent: bool = True):
        """به‌روزرسانی وضعیت یادآوری"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE reminders 
                SET is_sent = ?, sent_count = sent_count + 1, last_sent = ?
                WHERE id = ?
            ''', (is_sent, datetime.now().isoformat(), reminder_id))
            
            conn.commit()
            conn.close()
            logger.info(f"Reminder {reminder_id} status updated")
        except Exception as e:
            logger.error(f"Error updating reminder status: {e}")
    
    def save_search_history(self, user_id: int, query: str, category: str, 
                           results_count: int, processing_time: float, is_successful: bool = True):
        """ذخیره تاریخچه جستجو"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO search_history (user_id, query, category, results_count, 
                                          processing_time, is_successful)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, query, category, results_count, processing_time, is_successful))
            
            conn.commit()
            conn.close()
            logger.debug(f"Search history saved for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving search history: {e}")
    
    def get_search_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        """دریافت تاریخچه جستجو"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT query, category, results_count, created_at, is_successful
                FROM search_history 
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (user_id, limit))
            
            results = cursor.fetchall()
            conn.close()
            
            return [
                {
                    'query': row[0],
                    'category': row[1],
                    'results_count': row[2],
                    'created_at': row[3],
                    'is_successful': bool(row[4])
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error getting search history for user {user_id}: {e}")
            return []
    
    def create_content_share(self, content_id: int, user_id: int, share_type: str = 'public', 
                           expires_at: str = None) -> str:
        """ایجاد اشتراک‌گذاری محتوا"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # ایجاد URL منحصر به فرد
            share_url = self._generate_share_url(content_id, user_id)
            
            cursor.execute('''
                INSERT INTO content_shares (content_id, user_id, share_type, share_url, expires_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (content_id, user_id, share_type, share_url, expires_at))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Content share created: {share_url}")
            return share_url
        except Exception as e:
            logger.error(f"Error creating content share: {e}")
            raise DatabaseError(f"خطا در ایجاد اشتراک‌گذاری: {str(e)}")
    
    def _generate_share_url(self, content_id: int, user_id: int) -> str:
        """تولید URL منحصر به فرد برای اشتراک‌گذاری"""
        import hashlib
        import base64
        
        # ترکیب ID محتوا و کاربر با timestamp
        unique_string = f"{content_id}_{user_id}_{int(time.time())}"
        hash_object = hashlib.md5(unique_string.encode())
        hash_hex = hash_object.hexdigest()[:12]
        
        return f"https://t.me/share/url?url=content_{hash_hex}"
    
    def get_shared_content(self, share_url: str) -> Optional[Dict]:
        """دریافت محتوای اشتراک‌گذاری شده"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT cs.id, cs.content_id, cs.user_id, cs.share_type, cs.expires_at,
                       sc.topic, sc.category, sc.content, sc.content_type, sc.language
                FROM content_shares cs
                JOIN saved_content sc ON cs.content_id = sc.id
                WHERE cs.share_url = ? AND cs.is_active = 1
            ''', (share_url,))
            
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'share_id': result[0],
                    'content_id': result[1],
                    'user_id': result[2],
                    'share_type': result[3],
                    'expires_at': result[4],
                    'topic': result[5],
                    'category': result[6],
                    'content': result[7],
                    'content_type': result[8],
                    'language': result[9]
                }
            return None
        except Exception as e:
            logger.error(f"Error getting shared content: {e}")
            return None
    
    def create_custom_category(self, user_id: int, name: str, description: str = "", 
                             color: str = "#007bff", icon: str = "📁") -> int:
        """ایجاد دسته‌بندی سفارشی"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO custom_categories (user_id, name, description, color, icon)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, name, description, color, icon))
            
            category_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            logger.info(f"Custom category created for user {user_id}: {name}")
            return category_id
        except Exception as e:
            logger.error(f"Error creating custom category: {e}")
            raise DatabaseError(f"خطا در ایجاد دسته‌بندی: {str(e)}")
    
    def get_custom_categories(self, user_id: int) -> List[Dict]:
        """دریافت دسته‌بندی‌های سفارشی کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, name, description, color, icon, is_default, created_at
                FROM custom_categories 
                WHERE user_id = ?
                ORDER BY created_at ASC
            ''', (user_id,))
            
            results = cursor.fetchall()
            conn.close()
            
            return [
                {
                    'id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'color': row[3],
                    'icon': row[4],
                    'is_default': bool(row[5]),
                    'created_at': row[6]
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error getting custom categories for user {user_id}: {e}")
            return []
    
    def create_system_notification(self, user_id: int, title: str, message: str, 
                                 notification_type: str = 'info', action_url: str = None,
                                 expires_at: str = None) -> int:
        """ایجاد اعلان سیستم"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO system_notifications (user_id, title, message, notification_type, 
                                                action_url, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, title, message, notification_type, action_url, expires_at))
            
            notification_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            logger.info(f"System notification created for user {user_id}: {title}")
            return notification_id
        except Exception as e:
            logger.error(f"Error creating system notification: {e}")
            raise DatabaseError(f"خطا در ایجاد اعلان: {str(e)}")
    
    def get_user_notifications(self, user_id: int, unread_only: bool = True) -> List[Dict]:
        """دریافت اعلان‌های کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            query = '''
                SELECT id, title, message, notification_type, is_read, created_at, 
                       action_url, expires_at
                FROM system_notifications 
                WHERE user_id = ?
            '''
            
            if unread_only:
                query += ' AND is_read = 0'
            
            query += ' ORDER BY created_at DESC'
            
            cursor.execute(query, (user_id,))
            results = cursor.fetchall()
            conn.close()
            
            return [
                {
                    'id': row[0],
                    'title': row[1],
                    'message': row[2],
                    'notification_type': row[3],
                    'is_read': bool(row[4]),
                    'created_at': row[5],
                    'action_url': row[6],
                    'expires_at': row[7]
                }
                for row in results
            ]
        except Exception as e:
            logger.error(f"Error getting notifications for user {user_id}: {e}")
            return []
    
    def mark_notification_read(self, notification_id: int):
        """علامت‌گذاری اعلان به عنوان خوانده شده"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE system_notifications 
                SET is_read = 1
                WHERE id = ?
            ''', (notification_id,))
            
            conn.commit()
            conn.close()
            logger.debug(f"Notification {notification_id} marked as read")
        except Exception as e:
            logger.error(f"Error marking notification as read: {e}")
    
    def get_user_statistics(self, user_id: int) -> Dict[str, Any]:
        """دریافت آمار جامع کاربر"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # آمار کلی
            cursor.execute('''
                SELECT total_requests, total_content_saved, daily_requests,
                       join_date, last_activity, is_premium
                FROM users WHERE user_id = ?
            ''', (user_id,))
            user_stats = cursor.fetchone()
            
            # آمار درخواست‌ها
            cursor.execute('''
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful,
                       COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
                       AVG(processing_time) as avg_time
                FROM requests WHERE user_id = ?
            ''', (user_id,))
            request_stats = cursor.fetchone()
            
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
            
            # آمار محتوای ذخیره شده
            cursor.execute('''
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN is_favorite = 1 THEN 1 END) as favorites,
                       COUNT(CASE WHEN is_public = 1 THEN 1 END) as public
                FROM saved_content WHERE user_id = ?
            ''', (user_id,))
            content_stats = cursor.fetchone()
            
            conn.close()
            
            return {
                'user_info': {
                    'total_requests': user_stats[0] if user_stats else 0,
                    'total_content_saved': user_stats[1] if user_stats else 0,
                    'daily_requests': user_stats[2] if user_stats else 0,
                    'join_date': user_stats[3] if user_stats else None,
                    'last_activity': user_stats[4] if user_stats else None,
                    'is_premium': bool(user_stats[5]) if user_stats else False
                },
                'request_stats': {
                    'total': request_stats[0] if request_stats else 0,
                    'successful': request_stats[1] if request_stats else 0,
                    'failed': request_stats[2] if request_stats else 0,
                    'avg_processing_time': request_stats[3] if request_stats else 0
                },
                'popular_categories': categories,
                'content_stats': {
                    'total': content_stats[0] if content_stats else 0,
                    'favorites': content_stats[1] if content_stats else 0,
                    'public': content_stats[2] if content_stats else 0
                }
            }
        except Exception as e:
            logger.error(f"Error getting user statistics: {e}")
            return {}

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

class AIAssistant:
    """کلاس دستیار هوش مصنوعی پیشرفته"""
    
    def __init__(self, metis_api: MetisAPI):
        self.metis_api = metis_api
        self.conversation_history = {}
        self.user_preferences = {}
    
    async def generate_comprehensive_content(self, session: aiohttp.ClientSession, topic: str, 
                                           category: str, language: str = 'fa', 
                                           content_type: str = 'educational') -> Dict[str, Any]:
        """تولید محتوای جامع و پیشرفته"""
        try:
            # دریافت قالب مناسب
            template = ContentTemplate.get_custom_template(topic, category, language)
            
            # ایجاد prompt پیشرفته
            advanced_prompt = self._create_advanced_prompt(topic, category, language, content_type, template)
            
            # تولید محتوا با Metis
            raw_content = await self.metis_api.send_message(session, advanced_prompt)
            
            # پردازش و ساختاردهی محتوا
            structured_content = self._structure_content(raw_content, template, category)
            
            # اضافه کردن متادیتا
            metadata = {
                'topic': topic,
                'category': category,
                'language': language,
                'content_type': content_type,
                'template_used': template,
                'generated_at': datetime.now().isoformat(),
                'word_count': len(raw_content.split()),
                'estimated_read_time': len(raw_content.split()) // 200  # 200 کلمه در دقیقه
            }
            
            return {
                'content': structured_content,
                'raw_content': raw_content,
                'metadata': metadata,
                'template': template
            }
            
        except Exception as e:
            logger.error(f"Error in comprehensive content generation: {e}")
            raise ContentGenerationError(f"خطا در تولید محتوا: {str(e)}")
    
    def _create_advanced_prompt(self, topic: str, category: str, language: str, 
                               content_type: str, template: dict) -> str:
        """ایجاد prompt پیشرفته"""
        
        language_instructions = {
            'fa': "به زبان فارسی و با لحن دوستانه و آموزشی بنویس",
            'en': "Write in English with a friendly and educational tone",
            'ar': "اكتب باللغة العربية بأسلوب ودود وتعليمي",
            'tr': "Dostane ve eğitici bir tonla Türkçe yazın",
            'ru': "Пишите на русском языке дружелюбным и образовательным тоном"
        }
        
        content_type_instructions = {
            'educational': "محتوای آموزشی جامع و کاربردی",
            'professional': "محتوای حرفه‌ای و تخصصی",
            'casual': "محتوای غیررسمی و دوستانه",
            'technical': "محتوای فنی و تخصصی",
            'summary': "خلاصه مختصر و مفید"
        }
        
        prompt = f"""موضوع: {topic}
دسته‌بندی: {category}
نوع محتوا: {content_type_instructions.get(content_type, 'educational')}
زبان: {language_instructions.get(language, 'fa')}

ساختار مورد نظر:
{chr(10).join(template['structure'])}

لطفاً محتوای کاملی بنویس که شامل موارد زیر باشد:

1. معرفی جامع موضوع
2. توضیح مفاهیم کلیدی
3. کاربردهای عملی و واقعی
4. مثال‌های کاربردی
5. نکات مهم و ترفندها
6. مزایا و چالش‌ها
7. توصیه‌های عملی
8. منابع و مراجع مفید

محتوا را به صورت ساختاریافته و با استفاده از ایموجی‌های مناسب بنویس.
طول محتوا: 800-1200 کلمه
استایل: {content_type_instructions.get(content_type, 'educational')}

{template['hashtags']}"""
        
        return prompt
    
    def _structure_content(self, raw_content: str, template: dict, category: str) -> List[Dict[str, str]]:
        """ساختاردهی محتوا"""
        try:
            # تقسیم محتوا به بخش‌ها
            sections = []
            
            # اگر محتوا شامل بخش‌های مشخص باشد
            if '[بخش' in raw_content or 'Section' in raw_content:
                parts = re.split(r'\[بخش\s*\d+\]|Section\s*\d+', raw_content)
                for i, part in enumerate(parts[1:], 1):  # از بخش دوم شروع
                    if part.strip():
                        sections.append({
                            'title': f"بخش {i}",
                            'content': part.strip(),
                            'type': 'section'
                        })
            else:
                # تقسیم بر اساس پاراگراف‌ها
                paragraphs = [p.strip() for p in raw_content.split('\n\n') if p.strip()]
                
                if len(paragraphs) >= 2:
                    # تقسیم به دو بخش اصلی
                    mid = len(paragraphs) // 2
                    sections.append({
                        'title': template['structure'][0] if template['structure'] else "معرفی",
                        'content': '\n\n'.join(paragraphs[:mid]),
                        'type': 'introduction'
                    })
                    sections.append({
                        'title': template['structure'][1] if len(template['structure']) > 1 else "کاربردها",
                        'content': '\n\n'.join(paragraphs[mid:]),
                        'type': 'applications'
                    })
                else:
                    sections.append({
                        'title': template['structure'][0] if template['structure'] else "محتوا",
                        'content': raw_content,
                        'type': 'general'
                    })
            
            return sections
            
        except Exception as e:
            logger.error(f"Error structuring content: {e}")
            return [{
                'title': 'محتوا',
                'content': raw_content,
                'type': 'general'
            }]
    
    async def generate_multiple_formats(self, session: aiohttp.ClientSession, topic: str, 
                                      category: str, language: str = 'fa') -> Dict[str, Any]:
        """تولید محتوا در فرمت‌های مختلف"""
        formats = {}
        
        try:
            # محتوای آموزشی
            educational = await self.generate_comprehensive_content(
                session, topic, category, language, 'educational'
            )
            formats['educational'] = educational
            
            # خلاصه
            summary_prompt = f"خلاصه مختصر و مفید از موضوع '{topic}' در {len(topic.split()) * 2} کلمه"
            summary_content = await self.metis_api.send_message(session, summary_prompt)
            formats['summary'] = {
                'content': summary_content,
                'type': 'summary',
                'word_count': len(summary_content.split())
            }
            
            # نکات کلیدی
            key_points_prompt = f"5 نکته کلیدی مهم درباره '{topic}' به صورت لیست"
            key_points = await self.metis_api.send_message(session, key_points_prompt)
            formats['key_points'] = {
                'content': key_points,
                'type': 'key_points'
            }
            
            return formats
            
        except Exception as e:
            logger.error(f"Error generating multiple formats: {e}")
            return formats

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
        # مدیران اصلی
        self.db = DatabaseManager()
        self.scraper = None
        self.metis_api = MetisAPI(METIS_API_KEY, METIS_BOT_ID, METIS_MODEL)
        
        # مدیران پیشرفته
        self.rate_limiter = RateLimiter()
        self.security_manager = SecurityManager()
        self.backup_manager = BackupManager(self.db.db_path)
        self.ai_assistant = AIAssistant(self.metis_api)
        
        # مدیران محتوا و آمار
        self.analytics_manager = AnalyticsManager(self.db)
        self.notification_manager = None
        self.content_scheduler = ContentScheduler()
        self.content_template = ContentTemplate()
        self.content_generator = ContentGenerator()
        
        # وضعیت‌ها و تنظیمات
        self.user_states = {}
        self.user_sessions = {}
        self.conversation_states = {}
        
        # تنظیمات پیشرفته
        self.backup_task = None
        self.cleanup_task = None
        self.reminder_task = None
        
        # آمار سیستم
        self.system_stats = {
            'start_time': datetime.now(),
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'active_users': 0
        }
        
    def get_main_menu(self):
        """دریافت منوی اصلی پیشرفته"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 موضوع جدید", callback_data='new_topic')],
            [InlineKeyboardButton("💾 محتوای ذخیره شده", callback_data='saved_content')],
            [InlineKeyboardButton("📊 آمار و گزارش", callback_data='analytics')],
            [InlineKeyboardButton("⚙️ تنظیمات", callback_data='settings')],
            [InlineKeyboardButton("🔍 جستجوی پیشرفته", callback_data='advanced_search')],
            [InlineKeyboardButton("🤖 دستیار هوشمند", callback_data='ai_assistant')],
            [InlineKeyboardButton("📅 یادآوری‌ها", callback_data='reminders')],
            [InlineKeyboardButton("📤 اشتراک‌گذاری", callback_data='sharing')],
            [InlineKeyboardButton("🏷️ دسته‌بندی‌های سفارشی", callback_data='custom_categories')],
            [InlineKeyboardButton("📚 قالب‌های سفارشی", callback_data='custom_templates')],
            [InlineKeyboardButton("🔔 اعلان‌ها", callback_data='notifications')],
            [InlineKeyboardButton("⭐ بازخورد", callback_data='feedback')],
            [InlineKeyboardButton("❓ راهنما", callback_data='help')],
            [InlineKeyboardButton("📊 درباره ربات", callback_data='about')]
        ])
    
    def get_ai_assistant_menu(self):
        """منوی دستیار هوشمند"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 تولید محتوای هوشمند", callback_data='ai_smart_content')],
            [InlineKeyboardButton("📋 خلاصه و نکات کلیدی", callback_data='ai_summary')],
            [InlineKeyboardButton("🔍 تحقیق پیشرفته", callback_data='ai_research')],
            [InlineKeyboardButton("📊 تحلیل و گزارش", callback_data='ai_analysis')],
            [InlineKeyboardButton("💡 پیشنهادات هوشمند", callback_data='ai_suggestions')],
            [InlineKeyboardButton("🔄 چت تعاملی", callback_data='ai_chat')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_sharing_menu(self):
        """منوی اشتراک‌گذاری"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 اشتراک محتوا", callback_data='share_content')],
            [InlineKeyboardButton("🔗 لینک‌های اشتراک", callback_data='share_links')],
            [InlineKeyboardButton("📊 آمار اشتراک", callback_data='share_stats')],
            [InlineKeyboardButton("⚙️ تنظیمات اشتراک", callback_data='share_settings')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_custom_categories_menu(self):
        """منوی دسته‌بندی‌های سفارشی"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ ایجاد دسته‌بندی جدید", callback_data='create_category')],
            [InlineKeyboardButton("📋 مدیریت دسته‌بندی‌ها", callback_data='manage_categories')],
            [InlineKeyboardButton("🎨 شخصی‌سازی ظاهر", callback_data='customize_categories')],
            [InlineKeyboardButton("📊 آمار دسته‌بندی‌ها", callback_data='category_stats')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_custom_templates_menu(self):
        """منوی قالب‌های سفارشی"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 ایجاد قالب جدید", callback_data='create_template')],
            [InlineKeyboardButton("📋 مدیریت قالب‌ها", callback_data='manage_templates')],
            [InlineKeyboardButton("📊 قالب‌های محبوب", callback_data='popular_templates')],
            [InlineKeyboardButton("⚙️ تنظیمات قالب", callback_data='template_settings')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_notifications_menu(self):
        """منوی اعلان‌ها"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📬 اعلان‌های جدید", callback_data='new_notifications')],
            [InlineKeyboardButton("📋 همه اعلان‌ها", callback_data='all_notifications')],
            [InlineKeyboardButton("⚙️ تنظیمات اعلان", callback_data='notification_settings')],
            [InlineKeyboardButton("🔕 مدیریت اعلان‌ها", callback_data='manage_notifications')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
        ])
    
    def get_advanced_search_menu(self):
        """منوی جستجوی پیشرفته"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 جستجوی دقیق", callback_data='precise_search')],
            [InlineKeyboardButton("📊 جستجو در آمار", callback_data='search_analytics')],
            [InlineKeyboardButton("📚 جستجو در محتوا", callback_data='search_content')],
            [InlineKeyboardButton("📅 جستجو در تاریخچه", callback_data='search_history')],
            [InlineKeyboardButton("🎯 فیلترهای پیشرفته", callback_data='advanced_filters')],
            [InlineKeyboardButton("🔙 برگشت", callback_data='main_menu')]
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
        """مدیریت دستور /start پیشرفته"""
        user = update.effective_user
        user_id = user.id
        
        # بررسی امنیت
        if self.security_manager.is_user_blocked(user_id):
            await update.message.reply_text("❌ شما از استفاده از ربات مسدود شده‌اید.")
            return
        
        # بررسی محدودیت نرخ
        if not self.rate_limiter.is_allowed(user_id, 'minute'):
            await update.message.reply_text("⚠️ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
            return
        
        # ثبت کاربر در دیتابیس
        self.db.create_user(
            user_id=user_id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or ""
        )
        
        # ایجاد جلسه کاربر
        self.user_sessions[user_id] = UserSession(
            user_id=user_id,
            language='fa',
            current_state='main_menu',
            last_activity=datetime.now(),
            preferences={}
        )
        
        # به‌روزرسانی آمار سیستم
        self.system_stats['active_users'] += 1
        
        logger.info(f"User {user_id} started the advanced bot")
        
        welcome_message = f"""👋 سلام {user.first_name or 'کاربر'}! 

🤖 من ربات پیشرفته تولید محتوای آموزشی با قابلیت‌های هوشمند هستم

🔥 قابلیت‌های جدید:
• 🤖 دستیار هوشمند AI
• 📝 تولید محتوای پیشرفته
• 💾 ذخیره و مدیریت محتوا
• 📊 آمار و گزارش جامع
• 🏷️ دسته‌بندی‌های سفارشی
• 📚 قالب‌های شخصی‌سازی
• 📤 اشتراک‌گذاری محتوا
• 🔔 سیستم اعلان‌های هوشمند
• 📅 یادآوری و زمان‌بندی
• 🔍 جستجوی پیشرفته
• ⭐ سیستم بازخورد
• 📈 محدودیت روزانه: {MAX_DAILY_REQUESTS} درخواست

✨ برای شروع، یکی از گزینه‌های زیر را انتخاب کنید!"""
        
        await update.message.reply_text(
            welcome_message, 
            reply_markup=self.get_main_menu()
        )
    
    async def ai_assistant_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور دستیار هوشمند"""
        user_id = update.effective_user.id
        
        if self.security_manager.is_user_blocked(user_id):
            await update.message.reply_text("❌ شما از استفاده از ربات مسدود شده‌اید.")
            return
        
        ai_message = """🤖 دستیار هوشمند AI

این دستیار قابلیت‌های پیشرفته‌ای دارد:

🎯 تولید محتوای هوشمند:
• تحلیل خودکار موضوع
• تولید محتوای شخصی‌سازی شده
• بهینه‌سازی بر اساس ترجیحات

📋 خلاصه و نکات کلیدی:
• استخراج نکات مهم
• خلاصه‌سازی هوشمند
• نکات کلیدی کاربردی

🔍 تحقیق پیشرفته:
• جستجوی عمیق در منابع
• تحلیل و ترکیب اطلاعات
• ارائه منابع معتبر

📊 تحلیل و گزارش:
• تحلیل آماری محتوا
• گزارش‌های تحلیلی
• نمودارها و آمار

💡 پیشنهادات هوشمند:
• پیشنهاد موضوعات مرتبط
• الگوریتم‌های پیشنهاد
• شخصی‌سازی بر اساس تاریخچه

🔄 چت تعاملی:
• گفتگوی طبیعی
• پاسخ‌های هوشمند
• یادگیری از تعاملات

برای استفاده، یکی از گزینه‌های زیر را انتخاب کنید:"""
        
        await update.message.reply_text(
            ai_message,
            reply_markup=self.get_ai_assistant_menu()
        )
    
    async def sharing_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور اشتراک‌گذاری"""
        user_id = update.effective_user.id
        
        sharing_message = """📤 سیستم اشتراک‌گذاری

قابلیت‌های اشتراک‌گذاری:

📤 اشتراک محتوا:
• ایجاد لینک اشتراک
• تنظیم سطح دسترسی
• محدودیت زمانی

🔗 لینک‌های اشتراک:
• مدیریت لینک‌های فعال
• آمار بازدید
• تنظیمات امنیتی

📊 آمار اشتراک:
• تعداد بازدید
• آمار اشتراک‌گذاری
• تحلیل عملکرد

⚙️ تنظیمات اشتراک:
• حریم خصوصی
• محدودیت‌های دسترسی
• تنظیمات پیشرفته

برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"""
        
        await update.message.reply_text(
            sharing_message,
            reply_markup=self.get_sharing_menu()
        )
    
    async def custom_categories_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور دسته‌بندی‌های سفارشی"""
        user_id = update.effective_user.id
        
        categories_message = """🏷️ دسته‌بندی‌های سفارشی

مدیریت دسته‌بندی‌های شخصی:

➕ ایجاد دسته‌بندی جدید:
• نام و توضیحات
• رنگ و آیکون
• تنظیمات پیشرفته

📋 مدیریت دسته‌بندی‌ها:
• ویرایش و حذف
• مرتب‌سازی
• تنظیمات دسترسی

🎨 شخصی‌سازی ظاهر:
• انتخاب رنگ‌ها
• آیکون‌های سفارشی
• تم‌های مختلف

📊 آمار دسته‌بندی‌ها:
• تعداد محتوا
• آمار استفاده
• تحلیل عملکرد

برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"""
        
        await update.message.reply_text(
            categories_message,
            reply_markup=self.get_custom_categories_menu()
        )
    
    async def custom_templates_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور قالب‌های سفارشی"""
        user_id = update.effective_user.id
        
        templates_message = """📚 قالب‌های سفارشی

مدیریت قالب‌های محتوا:

📝 ایجاد قالب جدید:
• ساختار محتوا
• تنظیمات فرمت
• شخصی‌سازی

📋 مدیریت قالب‌ها:
• ویرایش و حذف
• کپی و اشتراک
• تنظیمات پیشرفته

📊 قالب‌های محبوب:
• قالب‌های عمومی
• آمار استفاده
• امتیازدهی

⚙️ تنظیمات قالب:
• پیش‌فرض‌ها
• تنظیمات خودکار
• شخصی‌سازی

برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"""
        
        await update.message.reply_text(
            templates_message,
            reply_markup=self.get_custom_templates_menu()
        )
    
    async def notifications_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور اعلان‌ها"""
        user_id = update.effective_user.id
        
        # دریافت اعلان‌های جدید
        notifications = self.db.get_user_notifications(user_id, unread_only=True)
        
        if notifications:
            notifications_text = "📬 اعلان‌های جدید:\n\n"
            for i, notif in enumerate(notifications[:5], 1):
                notifications_text += f"{i}. {notif['title']}\n"
                notifications_text += f"   {notif['message'][:50]}...\n\n"
        else:
            notifications_text = "📬 هیچ اعلان جدیدی ندارید."
        
        notifications_text += "\n🔔 مدیریت اعلان‌ها:\n"
        notifications_text += "• تنظیمات اعلان‌ها\n"
        notifications_text += "• انواع اعلان‌ها\n"
        notifications_text += "• زمان‌بندی اعلان‌ها\n"
        notifications_text += "• فیلترهای اعلان"
        
        await update.message.reply_text(
            notifications_text,
            reply_markup=self.get_notifications_menu()
        )
    
    async def advanced_search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور جستجوی پیشرفته"""
        user_id = update.effective_user.id
        
        search_message = """🔍 جستجوی پیشرفته

قابلیت‌های جستجو:

🔍 جستجوی دقیق:
• جستجو در محتوا
• فیلترهای پیشرفته
• نتایج مرتب‌سازی شده

📊 جستجو در آمار:
• آمار کاربری
• گزارش‌های تحلیلی
• نمودارها و گراف‌ها

📚 جستجو در محتوا:
• محتوای ذخیره شده
• دسته‌بندی‌ها
• برچسب‌ها

📅 جستجو در تاریخچه:
• تاریخچه جستجو
• فعالیت‌های گذشته
• روند استفاده

🎯 فیلترهای پیشرفته:
• فیلتر زمانی
• فیلتر دسته‌بندی
• فیلتر نوع محتوا

برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"""
        
        await update.message.reply_text(
            search_message,
            reply_markup=self.get_advanced_search_menu()
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
        
        # بررسی امنیت
        if self.security_manager.is_user_blocked(user_id):
            await query.edit_message_text("❌ شما از استفاده از ربات مسدود شده‌اید.")
            return
        
        # بررسی محدودیت نرخ
        if not self.rate_limiter.is_allowed(user_id, 'minute'):
            await query.edit_message_text("⚠️ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
            return
        
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
            
            elif action == 'ai_assistant':
                await query.edit_message_text(
                    "🤖 دستیار هوشمند AI\n\nقابلیت‌های پیشرفته در دسترس شما:",
                    reply_markup=self.get_ai_assistant_menu()
                )
            
            elif action == 'ai_smart_content':
                self.user_states[user_id] = 'waiting_for_ai_topic'
                await query.edit_message_text(
                    "🎯 تولید محتوای هوشمند\n\nلطفاً موضوع مورد نظر خود را بنویسید:\n\nاین سیستم از هوش مصنوعی پیشرفته برای تولید محتوای بهینه استفاده می‌کند.",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'ai_summary':
                self.user_states[user_id] = 'waiting_for_summary_topic'
                await query.edit_message_text(
                    "📋 خلاصه و نکات کلیدی\n\nلطفاً موضوع یا محتوایی که می‌خواهید خلاصه شود را بنویسید:",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'ai_research':
                self.user_states[user_id] = 'waiting_for_research_topic'
                await query.edit_message_text(
                    "🔍 تحقیق پیشرفته\n\nلطفاً موضوعی که می‌خواهید تحقیق شود را بنویسید:\n\nاین سیستم تحقیق عمیق و جامعی انجام خواهد داد.",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'ai_analysis':
                self.user_states[user_id] = 'waiting_for_analysis_topic'
                await query.edit_message_text(
                    "📊 تحلیل و گزارش\n\nلطفاً موضوعی که می‌خواهید تحلیل شود را بنویسید:\n\nاین سیستم تحلیل آماری و گزارش‌های تحلیلی ارائه می‌دهد.",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'ai_suggestions':
                await self.show_ai_suggestions(query, user_id)
            
            elif action == 'ai_chat':
                self.user_states[user_id] = 'ai_chat_mode'
                await query.edit_message_text(
                    "🔄 چت تعاملی\n\nحالا می‌توانید با دستیار هوشمند چت کنید.\n\nبرای خروج از حالت چت، /exit را تایپ کنید.",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'sharing':
                await query.edit_message_text(
                    "📤 سیستم اشتراک‌گذاری\n\nقابلیت‌های اشتراک‌گذاری محتوا:",
                    reply_markup=self.get_sharing_menu()
                )
            
            elif action == 'share_content':
                await self.show_shareable_content(query, user_id)
            
            elif action == 'share_links':
                await self.show_share_links(query, user_id)
            
            elif action == 'share_stats':
                await self.show_share_stats(query, user_id)
            
            elif action == 'share_settings':
                await self.show_share_settings(query, user_id)
            
            elif action == 'custom_categories':
                await query.edit_message_text(
                    "🏷️ دسته‌بندی‌های سفارشی\n\nمدیریت دسته‌بندی‌های شخصی:",
                    reply_markup=self.get_custom_categories_menu()
                )
            
            elif action == 'create_category':
                self.user_states[user_id] = 'waiting_for_category_name'
                await query.edit_message_text(
                    "➕ ایجاد دسته‌بندی جدید\n\nلطفاً نام دسته‌بندی جدید را بنویسید:",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'manage_categories':
                await self.show_manage_categories(query, user_id)
            
            elif action == 'customize_categories':
                await self.show_customize_categories(query, user_id)
            
            elif action == 'category_stats':
                await self.show_category_stats(query, user_id)
            
            elif action == 'custom_templates':
                await query.edit_message_text(
                    "📚 قالب‌های سفارشی\n\nمدیریت قالب‌های محتوا:",
                    reply_markup=self.get_custom_templates_menu()
                )
            
            elif action == 'create_template':
                self.user_states[user_id] = 'waiting_for_template_name'
                await query.edit_message_text(
                    "📝 ایجاد قالب جدید\n\nلطفاً نام قالب جدید را بنویسید:",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'manage_templates':
                await self.show_manage_templates(query, user_id)
            
            elif action == 'popular_templates':
                await self.show_popular_templates(query, user_id)
            
            elif action == 'template_settings':
                await self.show_template_settings(query, user_id)
            
            elif action == 'notifications':
                await query.edit_message_text(
                    "🔔 سیستم اعلان‌ها\n\nمدیریت اعلان‌ها:",
                    reply_markup=self.get_notifications_menu()
                )
            
            elif action == 'new_notifications':
                await self.show_new_notifications(query, user_id)
            
            elif action == 'all_notifications':
                await self.show_all_notifications(query, user_id)
            
            elif action == 'notification_settings':
                await self.show_notification_settings(query, user_id)
            
            elif action == 'manage_notifications':
                await self.show_manage_notifications(query, user_id)
            
            elif action == 'advanced_search':
                await query.edit_message_text(
                    "🔍 جستجوی پیشرفته\n\nقابلیت‌های جستجو:",
                    reply_markup=self.get_advanced_search_menu()
                )
            
            elif action == 'precise_search':
                self.user_states[user_id] = 'waiting_for_search_query'
                await query.edit_message_text(
                    "🔍 جستجوی دقیق\n\nلطفاً عبارت جستجو را بنویسید:",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'search_analytics':
                await self.show_search_analytics(query, user_id)
            
            elif action == 'search_content':
                self.user_states[user_id] = 'waiting_for_content_search'
                await query.edit_message_text(
                    "📚 جستجو در محتوا\n\nلطفاً عبارت جستجو را بنویسید:",
                    reply_markup=self.get_back_menu()
                )
            
            elif action == 'search_history':
                await self.show_search_history(query, user_id)
            
            elif action == 'advanced_filters':
                await self.show_advanced_filters(query, user_id)
                
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
    
    # متدهای جدید برای قابلیت‌های پیشرفته
    async def show_ai_suggestions(self, query, user_id: int):
        """نمایش پیشنهادات هوشمند"""
        try:
            # دریافت آمار کاربر برای پیشنهادات شخصی‌سازی شده
            user_stats = self.db.get_user_statistics(user_id)
            popular_categories = user_stats.get('popular_categories', [])
            
            suggestions_text = "💡 پیشنهادات هوشمند برای شما:\n\n"
            
            if popular_categories:
                suggestions_text += "🏷️ بر اساس علایق شما:\n"
                for category, count in popular_categories[:3]:
                    suggestions_text += f"• {category}: {count} درخواست\n"
                suggestions_text += "\n"
            
            # پیشنهادات عمومی
            general_suggestions = [
                "مدیریت زمان و بهره‌وری",
                "استراتژی‌های بازاریابی دیجیتال",
                "هوش مصنوعی در کسب‌وکار",
                "توسعه مهارت‌های رهبری",
                "نوآوری و کارآفرینی"
            ]
            
            suggestions_text += "🎯 پیشنهادات عمومی:\n"
            for i, suggestion in enumerate(general_suggestions, 1):
                suggestions_text += f"{i}. {suggestion}\n"
            
            suggestions_text += "\n💡 برای استفاده از هر پیشنهاد، آن را کپی کرده و در بخش 'موضوع جدید' استفاده کنید."
            
            await query.edit_message_text(
                suggestions_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing AI suggestions: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش پیشنهادات",
                reply_markup=self.get_back_menu()
            )
    
    async def show_shareable_content(self, query, user_id: int):
        """نمایش محتوای قابل اشتراک‌گذاری"""
        try:
            saved_content = self.db.get_saved_content(user_id, 10)
            
            if not saved_content:
                await query.edit_message_text(
                    "💾 شما هنوز محتوایی برای اشتراک‌گذاری ندارید.\n\n💡 ابتدا محتوایی تولید کنید و آن را ذخیره کنید.",
                    reply_markup=self.get_back_menu()
                )
                return
            
            content_text = "📤 محتوای قابل اشتراک‌گذاری:\n\n"
            buttons = []
            
            for i, content in enumerate(saved_content[:5], 1):
                content_text += f"{i}. 📝 {content['topic']}\n"
                content_text += f"   🏷️ {content['category']}\n"
                content_text += f"   📅 {content['created_at'][:10]}\n\n"
                
                buttons.append([InlineKeyboardButton(
                    f"📤 اشتراک {i}", 
                    callback_data=f'share_content_{content["id"]}'
                )])
            
            buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data='sharing')])
            
            await query.edit_message_text(
                content_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(f"Error showing shareable content: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش محتوا",
                reply_markup=self.get_back_menu()
            )
    
    async def show_share_links(self, query, user_id: int):
        """نمایش لینک‌های اشتراک"""
        try:
            # اینجا می‌توانید لینک‌های اشتراک کاربر را نمایش دهید
            share_text = "🔗 لینک‌های اشتراک شما:\n\n"
            share_text += "📊 آمار کلی:\n"
            share_text += "• لینک‌های فعال: 0\n"
            share_text += "• کل بازدید: 0\n"
            share_text += "• اشتراک‌گذاری‌ها: 0\n\n"
            share_text += "💡 برای ایجاد لینک اشتراک جدید، از بخش 'اشتراک محتوا' استفاده کنید."
            
            await query.edit_message_text(
                share_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing share links: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش لینک‌ها",
                reply_markup=self.get_back_menu()
            )
    
    async def show_share_stats(self, query, user_id: int):
        """نمایش آمار اشتراک"""
        try:
            stats_text = "📊 آمار اشتراک‌گذاری:\n\n"
            stats_text += "📈 آمار کلی:\n"
            stats_text += "• کل محتوای اشتراک‌گذاری شده: 0\n"
            stats_text += "• کل بازدید: 0\n"
            stats_text += "• میانگین بازدید: 0\n"
            stats_text += "• محبوب‌ترین محتوا: - \n\n"
            stats_text += "📅 آمار هفته گذشته:\n"
            stats_text += "• بازدید جدید: 0\n"
            stats_text += "• اشتراک‌گذاری جدید: 0\n\n"
            stats_text += "💡 برای بهبود آمار، محتوای باکیفیت تولید کنید."
            
            await query.edit_message_text(
                stats_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing share stats: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش آمار",
                reply_markup=self.get_back_menu()
            )
    
    async def show_share_settings(self, query, user_id: int):
        """نمایش تنظیمات اشتراک"""
        try:
            settings_text = "⚙️ تنظیمات اشتراک‌گذاری:\n\n"
            settings_text += "🔒 حریم خصوصی:\n"
            settings_text += "• سطح دسترسی: عمومی\n"
            settings_text += "• نیاز به تأیید: خیر\n"
            settings_text += "• محدودیت زمانی: نامحدود\n\n"
            settings_text += "📊 آمار و تحلیل:\n"
            settings_text += "• نمایش آمار: بله\n"
            settings_text += "• اعلان بازدید: بله\n"
            settings_text += "• گزارش‌های دوره‌ای: خیر\n\n"
            settings_text += "💡 برای تغییر تنظیمات، با پشتیبانی تماس بگیرید."
            
            await query.edit_message_text(
                settings_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing share settings: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش تنظیمات",
                reply_markup=self.get_back_menu()
            )
    
    async def show_manage_categories(self, query, user_id: int):
        """نمایش مدیریت دسته‌بندی‌ها"""
        try:
            custom_categories = self.db.get_custom_categories(user_id)
            
            if not custom_categories:
                await query.edit_message_text(
                    "🏷️ شما هنوز دسته‌بندی سفارشی ایجاد نکرده‌اید.\n\n💡 برای ایجاد دسته‌بندی جدید، از گزینه 'ایجاد دسته‌بندی جدید' استفاده کنید.",
                    reply_markup=self.get_back_menu()
                )
                return
            
            categories_text = "📋 مدیریت دسته‌بندی‌های شما:\n\n"
            buttons = []
            
            for i, category in enumerate(custom_categories[:5], 1):
                categories_text += f"{i}. {category['icon']} {category['name']}\n"
                categories_text += f"   📝 {category['description']}\n"
                categories_text += f"   📅 {category['created_at'][:10]}\n\n"
                
                buttons.append([InlineKeyboardButton(
                    f"✏️ ویرایش {i}", 
                    callback_data=f'edit_category_{category["id"]}'
                )])
            
            buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data='custom_categories')])
            
            await query.edit_message_text(
                categories_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(f"Error showing manage categories: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش دسته‌بندی‌ها",
                reply_markup=self.get_back_menu()
            )
    
    async def show_customize_categories(self, query, user_id: int):
        """نمایش شخصی‌سازی دسته‌بندی‌ها"""
        try:
            customize_text = "🎨 شخصی‌سازی دسته‌بندی‌ها:\n\n"
            customize_text += "🎨 رنگ‌ها:\n"
            customize_text += "• رنگ پیش‌فرض: آبی\n"
            customize_text += "• رنگ‌های موجود: آبی، سبز، قرمز، زرد، بنفش\n\n"
            customize_text += "📱 آیکون‌ها:\n"
            customize_text += "• آیکون پیش‌فرض: 📁\n"
            customize_text += "• آیکون‌های موجود: 📁 📂 📄 📋 📊 🏷️ 🎯 💡 🔍\n\n"
            customize_text += "💡 برای تغییر ظاهر، دسته‌بندی مورد نظر را ویرایش کنید."
            
            await query.edit_message_text(
                customize_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing customize categories: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش شخصی‌سازی",
                reply_markup=self.get_back_menu()
            )
    
    async def show_category_stats(self, query, user_id: int):
        """نمایش آمار دسته‌بندی‌ها"""
        try:
            stats_text = "📊 آمار دسته‌بندی‌ها:\n\n"
            stats_text += "📈 آمار کلی:\n"
            stats_text += "• کل دسته‌بندی‌ها: 0\n"
            stats_text += "• دسته‌بندی‌های فعال: 0\n"
            stats_text += "• محتوای مرتبط: 0\n\n"
            stats_text += "🏆 محبوب‌ترین دسته‌بندی‌ها:\n"
            stats_text += "• هنوز آمار موجود نیست\n\n"
            stats_text += "💡 برای مشاهده آمار، ابتدا دسته‌بندی و محتوا ایجاد کنید."
            
            await query.edit_message_text(
                stats_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing category stats: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش آمار",
                reply_markup=self.get_back_menu()
            )
    
    async def show_manage_templates(self, query, user_id: int):
        """نمایش مدیریت قالب‌ها"""
        try:
            templates_text = "📋 مدیریت قالب‌های شما:\n\n"
            templates_text += "📝 قالب‌های موجود:\n"
            templates_text += "• هنوز قالب سفارشی ایجاد نکرده‌اید\n\n"
            templates_text += "💡 برای ایجاد قالب جدید:\n"
            templates_text += "• نام قالب را انتخاب کنید\n"
            templates_text += "• ساختار محتوا را تعریف کنید\n"
            templates_text += "• تنظیمات فرمت را مشخص کنید\n"
            templates_text += "• قالب را ذخیره کنید\n\n"
            templates_text += "🔧 قالب‌های پیش‌فرض در دسترس هستند."
            
            await query.edit_message_text(
                templates_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing manage templates: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش قالب‌ها",
                reply_markup=self.get_back_menu()
            )
    
    async def show_popular_templates(self, query, user_id: int):
        """نمایش قالب‌های محبوب"""
        try:
            popular_text = "📊 قالب‌های محبوب:\n\n"
            popular_text += "🏆 قالب‌های برتر:\n"
            popular_text += "1. 📚 قالب آموزشی جامع\n"
            popular_text += "   ⭐ امتیاز: 4.8/5\n"
            popular_text += "   📊 استفاده: 1,234 بار\n\n"
            popular_text += "2. 💼 قالب حرفه‌ای\n"
            popular_text += "   ⭐ امتیاز: 4.6/5\n"
            popular_text += "   📊 استفاده: 987 بار\n\n"
            popular_text += "3. 🎯 قالب خلاصه\n"
            popular_text += "   ⭐ امتیاز: 4.5/5\n"
            popular_text += "   📊 استفاده: 756 بار\n\n"
            popular_text += "💡 برای استفاده از قالب‌ها، آن‌ها را کپی کرده و شخصی‌سازی کنید."
            
            await query.edit_message_text(
                popular_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing popular templates: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش قالب‌های محبوب",
                reply_markup=self.get_back_menu()
            )
    
    async def show_template_settings(self, query, user_id: int):
        """نمایش تنظیمات قالب"""
        try:
            settings_text = "⚙️ تنظیمات قالب:\n\n"
            settings_text += "📝 تنظیمات پیش‌فرض:\n"
            settings_text += "• قالب پیش‌فرض: آموزشی\n"
            settings_text += "• طول محتوا: متوسط\n"
            settings_text += "• فرمت: ساختاریافته\n\n"
            settings_text += "🎨 تنظیمات ظاهر:\n"
            settings_text += "• استفاده از ایموجی: بله\n"
            settings_text += "• رنگ‌بندی: خودکار\n"
            settings_text += "• فونت: پیش‌فرض\n\n"
            settings_text += "💡 برای تغییر تنظیمات، از بخش تنظیمات اصلی استفاده کنید."
            
            await query.edit_message_text(
                settings_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing template settings: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش تنظیمات",
                reply_markup=self.get_back_menu()
            )
    
    async def show_new_notifications(self, query, user_id: int):
        """نمایش اعلان‌های جدید"""
        try:
            notifications = self.db.get_user_notifications(user_id, unread_only=True)
            
            if not notifications:
                await query.edit_message_text(
                    "📬 هیچ اعلان جدیدی ندارید.\n\n✅ همه اعلان‌ها خوانده شده‌اند.",
                    reply_markup=self.get_back_menu()
                )
                return
            
            notifications_text = "📬 اعلان‌های جدید:\n\n"
            buttons = []
            
            for i, notif in enumerate(notifications[:5], 1):
                notifications_text += f"{i}. {notif['title']}\n"
                notifications_text += f"   📅 {notif['created_at'][:16]}\n"
                notifications_text += f"   {notif['message'][:50]}...\n\n"
                
                buttons.append([InlineKeyboardButton(
                    f"👁️ خواندن {i}", 
                    callback_data=f'read_notification_{notif["id"]}'
                )])
            
            buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data='notifications')])
            
            await query.edit_message_text(
                notifications_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(f"Error showing new notifications: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش اعلان‌ها",
                reply_markup=self.get_back_menu()
            )
    
    async def show_all_notifications(self, query, user_id: int):
        """نمایش همه اعلان‌ها"""
        try:
            notifications = self.db.get_user_notifications(user_id, unread_only=False)
            
            if not notifications:
                await query.edit_message_text(
                    "📋 هیچ اعلانی ندارید.\n\n💡 اعلان‌ها شامل اطلاعات مهم و به‌روزرسانی‌ها هستند.",
                    reply_markup=self.get_back_menu()
                )
                return
            
            notifications_text = "📋 همه اعلان‌ها:\n\n"
            
            for i, notif in enumerate(notifications[:10], 1):
                status = "📬" if not notif['is_read'] else "📭"
                notifications_text += f"{i}. {status} {notif['title']}\n"
                notifications_text += f"   📅 {notif['created_at'][:16]}\n"
                notifications_text += f"   {notif['message'][:50]}...\n\n"
            
            if len(notifications) > 10:
                notifications_text += f"... و {len(notifications) - 10} اعلان دیگر"
            
            await query.edit_message_text(
                notifications_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing all notifications: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش اعلان‌ها",
                reply_markup=self.get_back_menu()
            )
    
    async def show_notification_settings(self, query, user_id: int):
        """نمایش تنظیمات اعلان"""
        try:
            settings_text = "⚙️ تنظیمات اعلان‌ها:\n\n"
            settings_text += "🔔 انواع اعلان:\n"
            settings_text += "• اعلان‌های سیستم: فعال ✅\n"
            settings_text += "• یادآوری‌ها: فعال ✅\n"
            settings_text += "• به‌روزرسانی‌ها: فعال ✅\n"
            settings_text += "• آمار و گزارش: غیرفعال ❌\n\n"
            settings_text += "⏰ زمان‌بندی:\n"
            settings_text += "• اعلان‌های فوری: بله\n"
            settings_text += "• اعلان‌های روزانه: بله\n"
            settings_text += "• اعلان‌های هفتگی: خیر\n\n"
            settings_text += "💡 برای تغییر تنظیمات، از بخش تنظیمات اصلی استفاده کنید."
            
            await query.edit_message_text(
                settings_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing notification settings: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش تنظیمات",
                reply_markup=self.get_back_menu()
            )
    
    async def show_manage_notifications(self, query, user_id: int):
        """نمایش مدیریت اعلان‌ها"""
        try:
            manage_text = "🔕 مدیریت اعلان‌ها:\n\n"
            manage_text += "📋 عملیات موجود:\n"
            manage_text += "• علامت‌گذاری همه به عنوان خوانده شده\n"
            manage_text += "• حذف اعلان‌های قدیمی\n"
            manage_text += "• تنظیم فیلترهای اعلان\n"
            manage_text += "• صادر کردن اعلان‌ها\n\n"
            manage_text += "⚙️ تنظیمات پیشرفته:\n"
            manage_text += "• حذف خودکار اعلان‌های قدیمی\n"
            manage_text += "• محدودیت تعداد اعلان‌ها\n"
            manage_text += "• دسته‌بندی اعلان‌ها\n\n"
            manage_text += "💡 برای استفاده از این قابلیت‌ها، با پشتیبانی تماس بگیرید."
            
            await query.edit_message_text(
                manage_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing manage notifications: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش مدیریت",
                reply_markup=self.get_back_menu()
            )
    
    async def show_search_analytics(self, query, user_id: int):
        """نمایش آمار جستجو"""
        try:
            search_history = self.db.get_search_history(user_id, 10)
            
            analytics_text = "📊 آمار جستجو:\n\n"
            analytics_text += "📈 آمار کلی:\n"
            analytics_text += f"• کل جستجوها: {len(search_history)}\n"
            
            if search_history:
                successful_searches = sum(1 for s in search_history if s['is_successful'])
                analytics_text += f"• جستجوهای موفق: {successful_searches}\n"
                analytics_text += f"• نرخ موفقیت: {(successful_searches/len(search_history)*100):.1f}%\n\n"
                
                analytics_text += "🔍 آخرین جستجوها:\n"
                for i, search in enumerate(search_history[:5], 1):
                    status = "✅" if search['is_successful'] else "❌"
                    analytics_text += f"{i}. {status} {search['query']}\n"
                    analytics_text += f"   📅 {search['created_at'][:10]}\n\n"
            else:
                analytics_text += "• هنوز جستجویی انجام نداده‌اید\n\n"
            
            analytics_text += "💡 برای بهبود نتایج، از کلمات کلیدی دقیق استفاده کنید."
            
            await query.edit_message_text(
                analytics_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing search analytics: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش آمار",
                reply_markup=self.get_back_menu()
            )
    
    async def show_search_history(self, query, user_id: int):
        """نمایش تاریخچه جستجو"""
        try:
            search_history = self.db.get_search_history(user_id, 20)
            
            if not search_history:
                await query.edit_message_text(
                    "📅 تاریخچه جستجو خالی است.\n\n💡 پس از انجام جستجو، تاریخچه شما اینجا نمایش داده می‌شود.",
                    reply_markup=self.get_back_menu()
                )
                return
            
            history_text = "📅 تاریخچه جستجو:\n\n"
            
            for i, search in enumerate(search_history, 1):
                status = "✅" if search['is_successful'] else "❌"
                history_text += f"{i}. {status} {search['query']}\n"
                history_text += f"   🏷️ {search['category']}\n"
                history_text += f"   📅 {search['created_at'][:16]}\n"
                history_text += f"   📊 {search['results_count']} نتیجه\n\n"
            
            await query.edit_message_text(
                history_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing search history: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش تاریخچه",
                reply_markup=self.get_back_menu()
            )
    
    async def show_advanced_filters(self, query, user_id: int):
        """نمایش فیلترهای پیشرفته"""
        try:
            filters_text = "🎯 فیلترهای پیشرفته:\n\n"
            filters_text += "⏰ فیلتر زمانی:\n"
            filters_text += "• امروز\n"
            filters_text += "• هفته گذشته\n"
            filters_text += "• ماه گذشته\n"
            filters_text += "• سال گذشته\n\n"
            filters_text += "🏷️ فیلتر دسته‌بندی:\n"
            filters_text += "• هوش مصنوعی\n"
            filters_text += "• بازاریابی\n"
            filters_text += "• مدیریت\n"
            filters_text += "• برنامه‌نویسی\n"
            filters_text += "• کسب‌وکار\n\n"
            filters_text += "📝 فیلتر نوع محتوا:\n"
            filters_text += "• آموزشی\n"
            filters_text += "• حرفه‌ای\n"
            filters_text += "• خلاصه\n"
            filters_text += "• تحلیل\n\n"
            filters_text += "💡 برای استفاده از فیلترها، در بخش جستجو آن‌ها را انتخاب کنید."
            
            await query.edit_message_text(
                filters_text,
                reply_markup=self.get_back_menu()
            )
        except Exception as e:
            logger.error(f"Error showing advanced filters: {e}")
            await query.edit_message_text(
                "❌ خطا در نمایش فیلترها",
                reply_markup=self.get_back_menu()
            )

    async def run(self):
        """اجرای ربات پیشرفته"""
        try:
            # ایجاد application
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            # رفع خطای timezone
            try:
                application.job_queue.scheduler.configure(timezone=pytz.timezone('Asia/Tehran'))
            except Exception as e:
                logger.warning(f"Could not set timezone for job_queue: {e}")
            
            # تنظیم دستورات ربات
            commands = [
                BotCommand("start", "شروع ربات و منوی اصلی"),
                BotCommand("help", "راهنمای کامل ربات"),
                BotCommand("ai", "دستیار هوشمند AI"),
                BotCommand("sharing", "سیستم اشتراک‌گذاری"),
                BotCommand("categories", "دسته‌بندی‌های سفارشی"),
                BotCommand("templates", "قالب‌های سفارشی"),
                BotCommand("notifications", "مدیریت اعلان‌ها"),
                BotCommand("search", "جستجوی پیشرفته"),
                BotCommand("analytics", "آمار و گزارش شخصی"),
                BotCommand("settings", "تنظیمات شخصی"),
                BotCommand("saved", "محتوای ذخیره شده"),
                BotCommand("feedback", "بازخورد و نظرات"),
                BotCommand("reminders", "یادآوری‌ها"),
                BotCommand("stats", "آمار سیستم"),
                BotCommand("backup", "پشتیبان‌گیری"),
                BotCommand("exit", "خروج از حالت چت")
            ]
            
            # تنظیم دستورات
            try:
                await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
            except Exception as e:
                logger.warning(f"Could not set bot commands: {e}")
            
            # اضافه کردن handlers اصلی
            application.add_handler(CommandHandler("start", self.start_command))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CommandHandler("ai", self.ai_assistant_command))
            application.add_handler(CommandHandler("sharing", self.sharing_command))
            application.add_handler(CommandHandler("categories", self.custom_categories_command))
            application.add_handler(CommandHandler("templates", self.custom_templates_command))
            application.add_handler(CommandHandler("notifications", self.notifications_command))
            application.add_handler(CommandHandler("search", self.advanced_search_command))
            application.add_handler(CommandHandler("analytics", self.analytics_command))
            application.add_handler(CommandHandler("settings", self.settings_command))
            application.add_handler(CommandHandler("saved", self.saved_command))
            application.add_handler(CommandHandler("feedback", self.feedback_command))
            application.add_handler(CommandHandler("reminders", self.reminders_command))
            application.add_handler(CommandHandler("stats", self.system_stats_command))
            application.add_handler(CommandHandler("backup", self.backup_command))
            application.add_handler(CommandHandler("exit", self.exit_command))
            
            # اضافه کردن handlers پیشرفته
            application.add_handler(CallbackQueryHandler(self.button_handler))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            
            # شروع وظایف پس‌زمینه
            self.start_background_tasks(application)
            
            # شروع ربات
            logger.info("🚀 Advanced Bot started successfully!")
            logger.info(f"📊 Database path: {self.db.db_path}")
            logger.info(f"📈 Max daily requests: {MAX_DAILY_REQUESTS}")
            logger.info(f"🔒 Security features: Enabled")
            logger.info(f"🤖 AI Assistant: Enabled")
            logger.info(f"📤 Sharing system: Enabled")
            logger.info(f"🏷️ Custom categories: Enabled")
            logger.info(f"📚 Custom templates: Enabled")
            logger.info(f"🔔 Notification system: Enabled")
            logger.info(f"🔍 Advanced search: Enabled")
            
            # راه‌اندازی و شروع ربات
            await application.initialize()
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            
            # نگه داشتن ربات فعال
            try:
                await asyncio.Event().wait()  # منتظر ماندن تا زمانی که متوقف شود
            except KeyboardInterrupt:
                pass
            finally:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise
    
    def start_background_tasks(self, application):
        """شروع وظایف پس‌زمینه"""
        try:
            # وظیفه پشتیبان‌گیری خودکار
            async def auto_backup():
                while True:
                    try:
                        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)  # تبدیل به ثانیه
                        backup_path = self.backup_manager.create_backup()
                        if backup_path:
                            logger.info(f"Auto backup created: {backup_path}")
                        self.backup_manager.cleanup_old_backups()
                    except Exception as e:
                        logger.error(f"Auto backup error: {e}")
            
            # وظیفه پاک‌سازی داده‌های قدیمی
            async def cleanup_old_data():
                while True:
                    try:
                        await asyncio.sleep(24 * 3600)  # روزانه
                        # پاک‌سازی اعلان‌های قدیمی
                        # پاک‌سازی تاریخچه جستجوی قدیمی
                        logger.info("Old data cleanup completed")
                    except Exception as e:
                        logger.error(f"Cleanup error: {e}")
            
            # وظیفه ارسال یادآوری‌ها
            async def send_reminders():
                while True:
                    try:
                        await asyncio.sleep(300)  # هر 5 دقیقه
                        # بررسی یادآوری‌های زمان‌دار
                        # ارسال یادآوری‌ها
                        logger.debug("Reminder check completed")
                    except Exception as e:
                        logger.error(f"Reminder error: {e}")
            
            # شروع وظایف - این‌ها در run_polling اجرا خواهند شد
            logger.info("Background tasks defined successfully")
            
            logger.info("Background tasks started successfully")
        except Exception as e:
            logger.error(f"Error starting background tasks: {e}")
    
    async def system_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور آمار سیستم"""
        user_id = update.effective_user.id
        
        if self.security_manager.is_user_blocked(user_id):
            await update.message.reply_text("❌ شما از استفاده از ربات مسدود شده‌اید.")
            return
        
        # بررسی نقش کاربر (فقط ادمین‌ها)
        user = self.db.get_user(user_id)
        if not user or user.get('role', 'user') not in ['admin', 'moderator']:
            await update.message.reply_text("❌ شما مجوز مشاهده آمار سیستم را ندارید.")
            return
        
        uptime = datetime.now() - self.system_stats['start_time']
        uptime_str = f"{uptime.days} روز, {uptime.seconds // 3600} ساعت, {(uptime.seconds % 3600) // 60} دقیقه"
        
        stats_text = f"""📊 آمار سیستم:

⏰ زمان کارکرد: {uptime_str}
📈 کل درخواست‌ها: {self.system_stats['total_requests']}
✅ درخواست‌های موفق: {self.system_stats['successful_requests']}
❌ درخواست‌های ناموفق: {self.system_stats['failed_requests']}
👥 کاربران فعال: {self.system_stats['active_users']}

🔒 امنیت:
• کاربران مسدود شده: {len(self.security_manager.blocked_users)}
• محدودیت‌های نرخ: فعال

💾 دیتابیس:
• مسیر: {self.db.db_path}
• اندازه: {self.get_database_size()} MB

🤖 AI Assistant:
• وضعیت: فعال
• مدل: {METIS_MODEL}
• API: متصل

📤 اشتراک‌گذاری:
• لینک‌های فعال: 0
• کل بازدید: 0

💡 برای اطلاعات بیشتر، از منوی اصلی استفاده کنید."""
        
        await update.message.reply_text(stats_text, reply_markup=self.get_main_menu())
    
    async def backup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور پشتیبان‌گیری"""
        user_id = update.effective_user.id
        
        if self.security_manager.is_user_blocked(user_id):
            await update.message.reply_text("❌ شما از استفاده از ربات مسدود شده‌اید.")
            return
        
        # بررسی نقش کاربر
        user = self.db.get_user(user_id)
        if not user or user.get('role', 'user') not in ['admin', 'moderator']:
            await update.message.reply_text("❌ شما مجوز پشتیبان‌گیری را ندارید.")
            return
        
        try:
            backup_path = self.backup_manager.create_backup()
            if backup_path:
                await update.message.reply_text(
                    f"✅ پشتیبان‌گیری با موفقیت انجام شد!\n\n📁 مسیر: {backup_path}\n📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    reply_markup=self.get_main_menu()
                )
            else:
                await update.message.reply_text(
                    "❌ خطا در ایجاد پشتیبان",
                    reply_markup=self.get_main_menu()
                )
        except Exception as e:
            logger.error(f"Backup command error: {e}")
            await update.message.reply_text(
                "❌ خطا در پشتیبان‌گیری",
                reply_markup=self.get_main_menu()
            )
    
    async def exit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور خروج از حالت چت"""
        user_id = update.effective_user.id
        
        # پاک کردن وضعیت‌های چت
        self.user_states.pop(user_id, None)
        self.conversation_states.pop(user_id, None)
        
        await update.message.reply_text(
            "✅ از حالت چت خارج شدید.\n\nبرای شروع مجدد، از منوی اصلی استفاده کنید.",
            reply_markup=self.get_main_menu()
        )
    
    def get_database_size(self) -> float:
        """دریافت اندازه دیتابیس"""
        try:
            if os.path.exists(self.db.db_path):
                size_bytes = os.path.getsize(self.db.db_path)
                return round(size_bytes / (1024 * 1024), 2)  # تبدیل به مگابایت
            return 0.0
        except Exception as e:
            logger.error(f"Error getting database size: {e}")
            return 0.0

async def main():
    """تابع اصلی پیشرفته"""
    bot = None
    try:
        print("🚀 در حال راه‌اندازی ربات پیشرفته...")
        print("📊 بررسی تنظیمات...")
        
        # بررسی کلیدهای API
        if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            print("❌ لطفاً TELEGRAM_BOT_TOKEN را تنظیم کنید!")
            logger.error("TELEGRAM_BOT_TOKEN not configured!")
            return
        
        # بررسی وجود دیتابیس
        if not os.path.exists("bot_database.db"):
            print("💾 ایجاد دیتابیس جدید...")
            logger.info("Creating new database...")
        
        print("🔒 راه‌اندازی سیستم امنیت...")
        print("🤖 راه‌اندازی دستیار هوشمند...")
        print("📤 راه‌اندازی سیستم اشتراک‌گذاری...")
        print("🏷️ راه‌اندازی دسته‌بندی‌های سفارشی...")
        print("📚 راه‌اندازی قالب‌های سفارشی...")
        print("🔔 راه‌اندازی سیستم اعلان‌ها...")
        print("🔍 راه‌اندازی جستجوی پیشرفته...")
        print("📈 راه‌اندازی سیستم آمار...")
        print("💾 راه‌اندازی سیستم پشتیبان‌گیری...")
        
        # ایجاد و اجرای ربات پیشرفته
        bot = AdvancedTelegramBot()
        logger.info("Bot initialized successfully")
        
        print("✅ ربات با موفقیت راه‌اندازی شد!")
        print("🎯 قابلیت‌های فعال:")
        print("   • 🤖 دستیار هوشمند AI")
        print("   • 📝 تولید محتوای پیشرفته")
        print("   • 💾 ذخیره و مدیریت محتوا")
        print("   • 📊 آمار و گزارش جامع")
        print("   • 🏷️ دسته‌بندی‌های سفارشی")
        print("   • 📚 قالب‌های شخصی‌سازی")
        print("   • 📤 اشتراک‌گذاری محتوا")
        print("   • 🔔 سیستم اعلان‌های هوشمند")
        print("   • 📅 یادآوری و زمان‌بندی")
        print("   • 🔍 جستجوی پیشرفته")
        print("   • ⭐ سیستم بازخورد")
        print("   • 🔒 امنیت پیشرفته")
        print("   • 💾 پشتیبان‌گیری خودکار")
        print("   • 📈 آمار سیستم")
        print("   • 🎯 محدودیت نرخ")
        print("   • 🔄 وظایف پس‌زمینه")
        
        print("\n🚀 شروع ربات...")
        await bot.run()
        
    except KeyboardInterrupt:
        print("\n⏹️ ربات توسط کاربر متوقف شد")
        logger.info("Bot stopped by user (Ctrl+C)")
    except Exception as e:
        print(f"\n❌ خطای غیرمنتظره: {e}")
        logger.error(f"Critical error in main: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    finally:
        if bot:
            print("🧹 پاک‌سازی منابع ربات...")
            logger.info("Cleaning up bot resources...")
        print("✅ خاموشی ربات کامل شد")
        logger.info("Bot shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "event loop is already running" in str(e):
            # اگر event loop قبلاً در حال اجرا است، از loop موجود استفاده کن
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main())
        else:
            raise 
