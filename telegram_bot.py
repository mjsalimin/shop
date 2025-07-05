import asyncio
import json
import logging
import re
import urllib.parse
from typing import List, Optional, Dict, Any

import aiohttp
from bs4 import BeautifulSoup
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type, RetryError as TenacityRetryError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Embedded credentials
TELEGRAM_BOT_TOKEN = "1951771121:AAEn0VTc-8Ejx_RToZl_i69W0z6NCrau4I0"
METIS_API_KEY = "tpsg-6WW8eb5cZfq6fZru3B6tUbSaKB2EkVm"
METIS_MODEL = "gpt-4o"
API_URL_METIS = "https://api.metis.ai/v1/chat/completions"

# Retry configuration
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RetryableError(Exception):
    pass

class ContentScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }

    async def fetch_page(self, url: str, timeout: int = 15, params: dict = None) -> str:
        """Fetch a web page with proper headers and error handling"""
        try:
            kwargs = {
                'headers': self.headers,
                'timeout': timeout,
                'ssl': False  # Disable SSL verification
            }
            
            if params:
                kwargs['params'] = params
            
            async with self.session.get(url, **kwargs) as response:
                if response.status == 200:
                    content = await response.text()
                    logger.debug(f"Successfully fetched {len(content)} characters from {url}")
                    return content
                else:
                    logger.warning(f"HTTP {response.status} for {url}")
                    return ""
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return ""

    def clean_text(self, text: str) -> str:
        """Clean and normalize text content"""
        # Remove extra whitespace and newlines
        text = re.sub(r'\s+', ' ', text).strip()
        # Remove HTML entities
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        return text

    def extract_text_from_html(self, html: str, selectors: List[str]) -> List[str]:
        """Extract text content using multiple CSS selectors"""
        if not html:
            return []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            
            results = []
            for selector in selectors:
                try:
                    elements = soup.select(selector)
                    for element in elements:
                        text = self.clean_text(element.get_text())
                        if text and len(text) > 20:  # Filter out very short texts
                            results.append(text)
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            return results
        except Exception as e:
            logger.error(f"HTML parsing error: {e}")
            return []

    async def search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """Search using DuckDuckGo which is more reliable"""
        try:
            # DuckDuckGo instant answer API
            ddg_url = "https://api.duckduckgo.com/"
            params = {
                'q': query,
                'format': 'json',
                'no_html': '1',
                'skip_disambig': '1'
            }
            
            html = await self.fetch_page(ddg_url, params=params)
            if html:
                try:
                    data = json.loads(html)
                    abstract = data.get('AbstractText', '')
                    if abstract:
                        return [{'title': f'خلاصه {query}', 'snippet': abstract[:500], 'url': ''}]
                except:
                    pass
            
            # Search via HTML
            search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            html = await self.fetch_page(search_url)
            
            if not html:
                return []
            
            soup = BeautifulSoup(html, 'html.parser')
            results = []
            
            # Extract search results from DuckDuckGo
            search_results = soup.find_all('div', class_='result')
            
            for result in search_results[:8]:
                try:
                    link_elem = result.find('a', class_='result__a')
                    snippet_elem = result.find('a', class_='result__snippet')
                    
                    if link_elem:
                        url = link_elem.get('href', '')
                        title = self.clean_text(link_elem.get_text())
                        snippet = self.clean_text(snippet_elem.get_text()) if snippet_elem else ""
                        
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
        """Search using Bing as alternative"""
        try:
            search_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            html = await self.fetch_page(search_url)
            
            if not html:
                return []
            
            soup = BeautifulSoup(html, 'html.parser')
            results = []
            
            # Extract Bing search results
            search_results = soup.find_all('li', class_='b_algo')
            
            for result in search_results[:8]:
                try:
                    link_elem = result.find('a')
                    title_elem = result.find('h2')
                    snippet_elem = result.find('p')
                    
                    if link_elem and title_elem:
                        url = link_elem.get('href', '')
                        title = self.clean_text(title_elem.get_text())
                        snippet = self.clean_text(snippet_elem.get_text()) if snippet_elem else ""
                        
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

    async def search_linkedin(self, query: str) -> str:
        """Search LinkedIn for relevant content using multiple search engines"""
        try:
            # Try different search engines for LinkedIn content
            linkedin_query = f"site:linkedin.com {query}"
            
            # Try DuckDuckGo first
            ddg_results = await self.search_duckduckgo(linkedin_query)
            linkedin_content = []
            
            for result in ddg_results[:3]:
                if result['snippet']:
                    linkedin_content.append(result['snippet'])
            
            # If not enough content, try Bing
            if len(linkedin_content) < 2:
                bing_results = await self.search_bing(linkedin_query)
                for result in bing_results[:3]:
                    if result['snippet'] and result['snippet'] not in linkedin_content:
                        linkedin_content.append(result['snippet'])
            
            return "\n".join(linkedin_content[:5])
            
        except Exception as e:
            logger.error(f"LinkedIn search error: {e}")
            return ""

    async def scrape_url_content(self, url: str) -> str:
        """Scrape content from a specific URL"""
        try:
            html = await self.fetch_page(url)
            if not html:
                return ""
            
            # Common content selectors for different sites
            content_selectors = [
                'article', 'main', '.content', '.post-content', '.entry-content',
                '.article-body', '.story-body', 'p', '.text-content', '.description'
            ]
            
            texts = self.extract_text_from_html(html, content_selectors)
            
            # Combine and limit content
            content = "\n".join(texts[:10])  # Limit to first 10 text blocks
            return content[:2000]  # Limit total length
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return ""

    async def comprehensive_research(self, topic: str) -> (str, list):
        """Perform comprehensive research on a topic using multiple sources and collect URLs"""
        logger.info(f"Starting comprehensive research for: {topic}")
        research_parts = []
        sources = []
        try:
            # Try DuckDuckGo first
            logger.info("Searching with DuckDuckGo...")
            ddg_results = await self.search_duckduckgo(topic)
            if ddg_results:
                ddg_content = []
                for result in ddg_results[:5]:
                    if result['snippet']:
                        ddg_content.append(f"• {result['title']}: {result['snippet']}")
                        if result['url']:
                            sources.append({'title': result['title'], 'url': result['url']})
                if ddg_content:
                    research_parts.append("نتایج جستجو:\n" + "\n".join(ddg_content))
            # Try Bing as backup
            if len(research_parts) == 0:
                logger.info("Trying Bing search...")
                bing_results = await self.search_bing(topic)
                if bing_results:
                    bing_content = []
                    for result in bing_results[:5]:
                        if result['snippet']:
                            bing_content.append(f"• {result['title']}: {result['snippet']}")
                            if result['url']:
                                sources.append({'title': result['title'], 'url': result['url']})
                    if bing_content:
                        research_parts.append("نتایج جستجو:\n" + "\n".join(bing_content))
            # Search LinkedIn
            logger.info("Searching LinkedIn...")
            linkedin_content = await self.search_linkedin(topic)
            if linkedin_content:
                research_parts.append(f"محتوای لینکدین:\n{linkedin_content}")
            # Try to scrape some popular sites directly
            logger.info("Trying direct scraping...")
            popular_sites = [
                f"https://fa.wikipedia.org/wiki/{urllib.parse.quote(topic)}",
                f"https://en.wikipedia.org/wiki/{urllib.parse.quote(topic)}"
            ]
            for site_url in popular_sites:
                try:
                    content = await self.scrape_url_content(site_url)
                    if content and len(content) > 100:
                        research_parts.append(f"محتوای ویکی‌پدیا:\n{content[:800]}")
                        sources.append({'title': 'Wikipedia', 'url': site_url})
                        break
                except:
                    continue
            # If still no content, create basic research from topic
            if not research_parts:
                logger.warning("No external content found, creating basic research")
                basic_research = f"""موضوع تحقیق: {topic}\n\nاین موضوع در حوزه‌های مختلف کاربرد دارد و می‌تواند شامل جنبه‌های زیر باشد:\n• مفاهیم کلیدی و تعاریف اساسی\n• کاربردهای عملی در صنعت\n• روش‌ها و تکنیک‌های مرتبط\n• فواید و چالش‌های موجود\n• آینده و روندهای پیش‌رو\n\nبرای دریافت اطلاعات دقیق‌تر، توصیه می‌شود منابع معتبر مراجعه شود."""
                research_parts.append(basic_research)
        except Exception as e:
            logger.error(f"Error in comprehensive research: {e}")
            research_parts.append(f"موضوع: {topic}\nدر حال حاضر امکان دسترسی به منابع خارجی محدود است، اما بر اساس دانش عمومی، این موضوع شامل مباحث مهمی می‌باشد که نیاز به بررسی دقیق دارد.")
        combined_research = "\n\n".join(research_parts)
        logger.info(f"Research completed. Total content length: {len(combined_research)}")
        return combined_research, sources

class MetisAPI:
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.api_key = api_key
        self.model = model
        self.api_url = API_URL_METIS

    @retry(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        stop=stop_after_attempt(RETRY_ATTEMPTS)
    )
    async def generate_posts(self, session: aiohttp.ClientSession, topic: str, research_content: str) -> List[str]:
        """Generate educational posts using Metis API"""
        if len(research_content) > 3000:
            research_content = research_content[:3000] + "..."
        system_prompt = """تو یک تولیدکننده محتوای آموزشی هستی. وظیفه‌ات:\n1. دو پست جذاب و مفید بنویس\n2. لحن دوستانه و ساده باشه  \n3. از ایموجی استفاده کن\n4. هر پست حداکثر 250 کلمه\n5. اگر منابع یا لینک مفیدی داری، انتهای هر پست با عنوان 'منابع:' و به صورت لیست لینک بده\n6. مثال واقعی و کاربردی بیار\n7. خروجی رو طوری بنویس که برای کاربر تلگرام قابل فهم و جذاب باشه\n8. اگر لازم شد، خروجی رو به چند بخش تقسیم کن که هر بخش کمتر از ۴۰۰۰ کاراکتر باشه و شماره‌گذاری کن\n"""
        user_prompt = f"""موضوع: {topic}\n\nاطلاعات: {research_content}\n\nدو پست آموزشی بنویس:\nپست اول: معرفی کلی موضوع با مثال\nپست دوم: نکات عملی و کاربردی با مثال\nهر پست رو با [پست ۱] یا [پست ۲] شروع کن. اگر منابع داری، انتهای هر پست لیست کن."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 1000,
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Telegram-Bot/1.0"
        }
        try:
            logger.info(f"Sending request to Metis API...")
            logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)[:500]}...")
            async with session.post(
                self.api_url, 
                headers=headers, 
                json=payload, 
                timeout=45,
                ssl=False
            ) as response:
                logger.info(f"Metis API response status: {response.status}")
                response_text = await response.text()
                logger.debug(f"Full Metis API response: {response_text}")
                if response.status == 401:
                    logger.error("Authentication failed - check API key")
                    raise RetryableError("کلید API معتبر نیست. لطفا بررسی کنید.")
                elif response.status == 403:
                    logger.error("Access forbidden - check permissions")
                    raise RetryableError("دسترسی به سرویس متیس محدود شده است.")
                elif response.status >= 500:
                    logger.error(f"Server error: {response.status}")
                    raise RetryableError(f"خطای سرور متیس. لطفا بعدا تلاش کنید. ({response.status})")
                elif response.status != 200:
                    logger.error(f"Unexpected status: {response.status}, Response: {response_text}")
                    raise RetryableError(f"خطای غیرمنتظره از متیس: {response.status}")
                try:
                    data = json.loads(response_text)
                    logger.debug(f"Parsed JSON successfully: {data}")
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}, Response: {response_text}")
                    raise RetryableError("پاسخ دریافتی از متیس قابل خواندن نبود.")
                # Defensive: check for choices and content
                if isinstance(data, dict) and 'choices' in data and len(data['choices']) > 0:
                    content = data['choices'][0].get('message', {}).get('content', None)
                    if not content:
                        logger.error(f"No 'content' in Metis response: {data}")
                        raise RetryableError("پاسخ متیس فاقد متن است. لطفا بعدا تلاش کنید.")
                    logger.info(f"Generated content length: {len(content)}")
                    posts = []
                    if '[پست ۲]' in content:
                        parts = content.split('[پست ۲]')
                        if len(parts) == 2:
                            post1 = parts[0].replace('[پست ۱]', '').strip()
                            post2 = parts[1].strip()
                            posts = [post1, post2]
                    elif '--- پست ۲ ---' in content:
                        parts = content.split('--- پست ۲ ---')
                        if len(parts) == 2:
                            post1 = parts[0].replace('--- پست ۱ ---', '').strip()
                            post2 = parts[1].strip()
                            posts = [post1, post2]
                    if not posts:
                        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
                        if len(paragraphs) >= 4:
                            mid = len(paragraphs) // 2
                            post1 = '\n\n'.join(paragraphs[:mid])
                            post2 = '\n\n'.join(paragraphs[mid:])
                            posts = [post1, post2]
                        else:
                            words = content.split()
                            mid = len(words) // 2
                            post1 = ' '.join(words[:mid])
                            post2 = ' '.join(words[mid:])
                            posts = [post1, post2]
                    valid_posts = [post for post in posts if post.strip() and len(post.strip()) > 50]
                    if not valid_posts:
                        valid_posts = [
                            f"📚 {topic}\n\n{content}",
                            "💡 برای اطلاعات بیشتر، منابع معتبر را مطالعه کنید."
                        ]
                    logger.info(f"Successfully generated {len(valid_posts)} posts")
                    return valid_posts
                else:
                    logger.error(f"No choices or content in response: {data}")
                    raise RetryableError("پاسخ متیس معتبر نیست یا خروجی ندارد. لطفا بعدا تلاش کنید.")
        except aiohttp.ClientError as e:
            logger.error(f"Network error: {e}")
            raise RetryableError(f"خطای شبکه یا ارتباط با متیس: {e}")
        except RetryableError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise RetryableError(f"خطای غیرمنتظره: {e}")

class TelegramBot:
    def __init__(self):
        self.scraper = None
        self.metis_api = MetisAPI(METIS_API_KEY, METIS_MODEL)
        
        # Inline keyboard
        self.menu = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 موضوع جدید", callback_data='new')],
            [InlineKeyboardButton("❓ راهنما", callback_data='help')],
            [InlineKeyboardButton("🔍 جستجوی پیشرفته", callback_data='advanced')],
            [InlineKeyboardButton("❌ لغو", callback_data='cancel')]
        ])

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        logger.info(f"User {user_id} started the bot")
        
        welcome_message = """🤖 سلام! من ربات تولید محتوای آموزشی هستم

🔥 قابلیت‌های من:
• جستجو در تمام اینترنت
• تحقیق در لینکدین
• تولید محتوای آموزشی با هوش مصنوعی
• ایجاد پست‌های جذاب و کاربردی

✨ کافیه موضوع مورد نظرتون رو بفرستین!"""
        
        await update.message.reply_text(welcome_message, reply_markup=self.menu)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard buttons"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        action = query.data
        
        logger.info(f"User {user_id} pressed button: {action}")
        
        if action == 'new':
            message = "📝 لطفا موضوع مورد نظر خود را بنویسید:\n\n" \
                     "مثال: مدیریت فروش با هوش مصنوعی"
            await query.edit_message_text(message, reply_markup=self.menu)
            
        elif action == 'help':
            help_text = """📚 راهنمای استفاده:

1️⃣ موضوع خود را بنویسید
2️⃣ صبر کنید تا تحقیق انجام شود
3️⃣ دو پست آموزشی دریافت کنید

🔍 ربات از منابع زیر تحقیق می‌کند:
• گوگل (10 نتیجه اول)
• لینکدین
• سایت‌های معتبر

⏱ زمان تحقیق: 30-60 ثانیه"""
            await query.edit_message_text(help_text, reply_markup=self.menu)
            
        elif action == 'advanced':
            advanced_text = """🔬 جستجوی پیشرفته:

برای نتایج بهتر، موضوع خود را دقیق‌تر بنویسید:

✅ خوب: "استراتژی‌های بازاریابی دیجیتال برای کسب‌وکارهای کوچک"
❌ بد: "بازاریابی"

✅ خوب: "روش‌های افزایش فروش آنلاین"
❌ بد: "فروش"

💡 نکته: هرچه موضوع دقیق‌تر باشد، محتوای بهتری تولید می‌شود!"""
            await query.edit_message_text(advanced_text, reply_markup=self.menu)
            
        else:  # cancel
            await query.edit_message_text("❌ درخواست لغو شد.", reply_markup=self.menu)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user messages (topics)"""
        user_id = update.effective_user.id
        topic = update.message.text.strip()
        logger.info(f"User {user_id} requested topic: {topic}")
        if len(topic) < 3:
            await update.message.reply_text(
                "⚠️ لطفا موضوع دقیق‌تری وارد کنید (حداقل 3 کاراکتر)",
                reply_markup=self.menu
            )
            return
        status_message = await update.message.reply_text("🔍 در حال تحقیق... لطفا صبر کنید")
        try:
            await update.message.chat.send_action(ChatAction.TYPING)
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self.scraper = ContentScraper(session)
                await status_message.edit_text("🔍 در حال جستجو در اینترنت...")
                research_content, sources = await self.scraper.comprehensive_research(topic)
                if not research_content:
                    await status_message.edit_text(
                        "❌ متاسفانه نتوانستم اطلاعات کافی پیدا کنم. لطفا موضوع دیگری امتحان کنید.",
                        reply_markup=self.menu
                    )
                    return
                await status_message.edit_text("🤖 در حال تولید محتوا با هوش مصنوعی...")
                try:
                    posts = await self.metis_api.generate_posts(session, topic, research_content)
                    if not posts:
                        await status_message.edit_text(
                            "❌ خطا در تولید محتوا. لطفا مجددا تلاش کنید.",
                            reply_markup=self.menu
                        )
                        return
                    await status_message.delete()
                    # Split and send posts with respect to Telegram's character limit
                    all_posts = []
                    for i, post in enumerate(posts, 1):
                        split_posts = self.split_telegram_messages(post)
                        for idx, chunk in enumerate(split_posts):
                            all_posts.append((i, idx+1, chunk))
                    # If sources exist, format and append to last message
                    if sources:
                        sources_text = self.format_sources(sources)
                        if len(all_posts) > 0:
                            last = all_posts[-1]
                            if len(last[2]) + len(sources_text) < 4096:
                                all_posts[-1] = (last[0], last[1], last[2] + '\n\n' + sources_text)
                            else:
                                all_posts.append((last[0], last[1]+1, sources_text))
                    # Limit to 3 messages
                    all_posts = all_posts[:3]
                    for i, idx, chunk in all_posts:
                        await update.message.chat.send_action(ChatAction.TYPING)
                        await asyncio.sleep(1)
                        await update.message.reply_text(f"📝 پست {i} ({idx}):\n\n{chunk}")
                    await update.message.reply_text(
                        "✅ تولید محتوا با موفقیت انجام شد!\n\n💡 برای موضوع جدید، دکمه زیر را فشار دهید:",
                        reply_markup=self.menu
                    )
                except RetryableError as e:
                    logger.error(f"RetryableError in generate_posts: {e}")
                    await status_message.edit_text(
                        "❌ خطا در اتصال به سرویس هوش مصنوعی. لطفا مجددا تلاش کنید.",
                        reply_markup=self.menu
                    )
                except Exception as e:
                    logger.error(f"Exception in generate_posts: {e}")
                    try:
                        fallback_posts = self.create_fallback_posts(topic, research_content)
                        await status_message.delete()
                        for i, post in enumerate(fallback_posts, 1):
                            split_posts = self.split_telegram_messages(post)
                            for idx, chunk in enumerate(split_posts, 1):
                                await update.message.chat.send_action(ChatAction.TYPING)
                                await asyncio.sleep(1)
                                await update.message.reply_text(f"📝 پست {i} ({idx}):\n\n{chunk}")
                        if sources:
                            sources_text = self.format_sources(sources)
                            await update.message.reply_text(sources_text)
                        await update.message.reply_text(
                            "⚠️ محتوا بر اساس تحقیقات تولید شد (بدون هوش مصنوعی)\n\n💡 برای موضوع جدید، دکمه زیر را فشار دهید:",
                            reply_markup=self.menu
                        )
                    except Exception as fallback_error:
                        logger.error(f"Fallback also failed: {fallback_error}")
                        await status_message.edit_text(
                            "❌ خطای غیرمنتظره. لطفا مجددا تلاش کنید.",
                            reply_markup=self.menu
                        )
                except Exception as e:
                    logger.error(f"Exception in handle_message: {e}")
                    await status_message.edit_text(
                        "❌ خطای غیرمنتظره. لطفا مجددا تلاش کنید.",
                        reply_markup=self.menu
                    )
        except Exception as e:
            logger.error(f"Exception in handle_message: {e}")
            await status_message.edit_text(
                "❌ خطای غیرمنتظره. لطفا مجددا تلاش کنید.",
                reply_markup=self.menu
            )

    def create_fallback_posts(self, topic: str, research_content: str) -> List[str]:
        """Create posts without AI when API fails"""
        
        # Extract key points from research
        lines = research_content.split('\n')
        key_points = []
        
        for line in lines:
            line = line.strip()
            if line and len(line) > 30 and not line.startswith('http'):
                # Clean and format the line
                if line.startswith('•'):
                    key_points.append(line)
                elif ':' in line and len(line.split(':')[1].strip()) > 20:
                    key_points.append(f"• {line}")
                elif len(line) > 50:
                    key_points.append(f"• {line}")
        
        # Limit to most relevant points
        key_points = key_points[:8]
        
        # Create first post (introduction)
        post1 = f"""📚 {topic}

🔍 {topic} یکی از موضوعات مهم و کاربردی در دنیای امروز محسوب می‌شود.

💡 نکات کلیدی:
{chr(10).join(key_points[:4] if key_points else ['• موضوعی پرکاربرد و مفید', '• نیاز به مطالعه و تحقیق بیشتر', '• کاربرد در صنایع مختلف'])}

🎯 این موضوع می‌تواند در بهبود عملکرد و دستیابی به اهداف کمک کند."""

        # Create second post (practical tips)
        post2 = f"""⚡ نکات عملی {topic}

🚀 برای موفقیت در این حوزه:

{chr(10).join(key_points[4:] if len(key_points) > 4 else ['• مطالعه منابع معتبر', '• تمرین و تکرار مداوم', '• استفاده از ابزارهای مناسب', '• همکاری با متخصصان'])}

💪 تنها با عمل کردن می‌توان به نتایج مطلوب رسید!

#آموزش #{topic.replace(' ', '_')}"""

        return [post1, post2]

    def split_telegram_messages(self, text: str) -> list:
        """Split a long text into Telegram-sized messages (max 4096 chars, up to 3)"""
        max_len = 4096
        chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)]
        return chunks[:3]

    def format_sources(self, sources: list) -> str:
        """Format sources as a neat list of links"""
        if not sources:
            return ''
        lines = ["\n\n📚 منابع پیشنهادی:"]
        for s in sources:
            if s['url']:
                lines.append(f"🔗 {s['title']}: {s['url']}")
        return '\n'.join(lines)

def main():
    """Main function to run the bot"""
    logger.info("Starting Telegram bot...")
    
    # Create bot instance
    bot = TelegramBot()
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler('start', bot.start_command))
    application.add_handler(CallbackQueryHandler(bot.button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    # Run the bot
    logger.info("Bot is running...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
