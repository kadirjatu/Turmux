from dotenv import load_dotenv
import html
import os
import threading
import logging
import requests
import json
import time
import base64
import subprocess
import tempfile
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai
from google.genai import types as genai_types
import psychology_tiers

# Load .env file
load_dotenv()

# =========================
# ENV VARIABLES
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
PHP_API_URL = os.getenv("PHP_API_URL", "http://127.0.0.1:8000/movies.php")
# Hardcoded to match user's new link directly to avoid secret conflicts
GOOGLE_SHEETS_ID = "1BDGOwk2SRKsuVvHRT4eNkeTwAuOjhppQneD19wnfXf8"
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")  # Set when Real-ESRGAN API is ready

gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logging.info("Gemini API configured successfully.")
else:
    logging.warning("GEMINI_API_KEY not set. Subtitle correction will be skipped.")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN env variable not set")

# Configure Google Sheets
sheets_service = None
if GOOGLE_SHEETS_ID and GOOGLE_SERVICE_ACCOUNT_JSON_BASE64:
    try:
        # Add padding if necessary for base64 decoding
        b64_str = GOOGLE_SERVICE_ACCOUNT_JSON_BASE64.strip()
        padding = len(b64_str) % 4
        if padding:
            b64_str += "=" * (4 - padding)
            
        creds_json = base64.b64decode(b64_str).decode('utf-8')
        creds_info = json.loads(creds_json)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        sheets_service = build('sheets', 'v4', credentials=creds)
        logging.info("Google Sheets API configured successfully.")
    except Exception as e:
        logging.error(f"Failed to configure Google Sheets: {e}")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logging.getLogger("TeleBot").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

# Usage tracking (simple in-memory)
user_usage = {}
RATE_LIMIT_COOLDOWN = 60 # 1 minute

# Multi-version selection settings
ITEMS_PER_PAGE = 5
pending_selections = {}  # Store search results for pagination

# =========================
# CREDIT SYSTEM
# =========================
SIGNUP_CREDITS = 10
REFERRAL_CREDITS = 10
AD_CREDITS = 3
VIDEO_CREDIT_COST   = 2
ENHANCE_CREDIT_COST = 1   # 1 credit per image/video enhance
AD_COOLDOWN_SECONDS = 300  # 5 minutes
ADSTERRA_LINK = os.getenv("ADSTERRA_LINK", "https://www.effectivecpmnetwork.com/z0kuixztez?key=669f4fed4da4bb1e1233d5254a9b8887")

user_credits = {}       # {user_id: int}
ad_cooldowns = {}       # {user_id: float timestamp}
registered_users = set()  # set of user_ids already given signup credits
user_referrers = {}     # {new_user_id: referrer_user_id}

def get_credits(user_id):
    return user_credits.get(str(user_id), 0)

def add_credits(user_id, amount):
    uid = str(user_id)
    user_credits[uid] = user_credits.get(uid, 0) + amount

def deduct_credits(user_id, amount):
    uid = str(user_id)
    user_credits[uid] = max(0, user_credits.get(uid, 0) - amount)

def register_user_credits(user_id, referrer_id=None):
    uid = str(user_id)
    if uid not in registered_users:
        registered_users.add(uid)
        add_credits(uid, SIGNUP_CREDITS)
        if referrer_id and str(referrer_id) != uid:
            add_credits(str(referrer_id), REFERRAL_CREDITS)
            user_referrers[uid] = str(referrer_id)
        return True
    return False

def get_main_keyboard(user_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🎬 Create Caption Video", callback_data="menu_video"))
    markup.row(InlineKeyboardButton("🔮 AI Enhance (Image/Video)", callback_data="menu_enhance"))
    markup.row(InlineKeyboardButton("💰 Watch Ad & Earn (+3)", callback_data="menu_earn"))
    markup.row(
        InlineKeyboardButton("🎁 Invite Friends", callback_data="menu_invite"),
        InlineKeyboardButton("👑 Premium", callback_data="menu_premium")
    )
    return markup

# =========================
# HELPER FUNCTIONS
# =========================
def shorten_url(long_url):
    api_key = os.getenv("SHORTENER_API_KEY")
    if not api_key:
        logging.warning("SHORTENER_API_KEY not set. Returning original link.")
        return long_url
    
    try:
        # Shorte.st API implementation
        api_url = "https://api.shorte.st/v1/data/url"
        headers = {"public-api-token": api_key}
        # Shorte.st documentation suggests using PUT for shortening
        # Using json=payload for application/json content type
        payload = {"urlToShorten": long_url}
        
        response = requests.put(api_url, json=payload, headers=headers, timeout=10)
        
        logging.info(f"Shortener Request to {api_url} | Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "ok":
                short_url = result.get("shortenedUrl")
                if short_url:
                    logging.info(f"Successfully shortened link: {short_url}")
                    return short_url
            logging.error(f"Shortener API returned non-ok status: {result}")
        else:
            logging.error(f"Shortener API failed with status {response.status_code}: {response.text}")
    except Exception as e:
        logging.error(f"URL Shortening Error: {e}")
    
    return long_url

def log_to_sheets(movie_name):
    if not sheets_service or not GOOGLE_SHEETS_ID:
        logging.error(f"Google Sheets service or ID missing. Sheets Service: {bool(sheets_service)}, ID: {bool(GOOGLE_SHEETS_ID)}")
        return
    
    try:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        values = [[movie_name.lower(), timestamp, "Requested"]]
        body = {'values': values}
        
        # Log basic info for debugging
        logging.info(f"DEBUG: Sheets Service type: {type(sheets_service)}")
        logging.info(f"DEBUG: Attempting to append to Sheets ID: {GOOGLE_SHEETS_ID}")

        # Use metadata to find the first sheet name dynamically
        try:
            sheet_metadata = sheets_service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
            sheets = sheet_metadata.get('sheets', [])
            if sheets:
                title = sheets[0].get("properties", {}).get("title", "Sheet1")
                target_range = f"'{title}'!A:C"
                logging.info(f"DEBUG: Found sheet title: {title}")
            else:
                target_range = "Sheet1!A:C"
                logging.warning("DEBUG: No sheets found in metadata, using Sheet1 default")
        except Exception as e:
            logging.warning(f"Metadata fetch failed: {e}. Defaulting to 'Sheet1!A:C'")
            target_range = "Sheet1!A:C"

        logging.info(f"DEBUG: Final Target Range: {target_range}")
        result = sheets_service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=target_range,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        logging.info(f"Successfully logged to Sheets: {movie_name}. Result: {result}")
    except Exception as e:
        logging.error(f"Error logging to Google Sheets: {e}")

def is_rate_limited(user_id):
    now = time.time()
    if user_id in user_usage:
        last_time = user_usage[user_id]
        if now - last_time < RATE_LIMIT_COOLDOWN:
            return True
    user_usage[user_id] = now
    return False

# =========================
# TELEGRAM BOT LOGIC
# =========================
@bot.message_handler(commands=["start"])
def start(message):
    name = message.from_user.first_name or "User"
    user_id = message.from_user.id

    # Handle referral from deep link: /start ref_12345
    referrer_id = None
    parts = message.text.strip().split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            referrer_id = int(parts[1][4:])
        except ValueError:
            pass

    is_new = register_user_credits(user_id, referrer_id)
    credits = get_credits(user_id)
    new_user_note = "\n\n🎉 <b>Welcome bonus: +10 Credits!</b>" if is_new else ""

    # Movie of the Day logic
    motd_text = ""
    try:
        movies_path = os.path.join(os.path.dirname(__file__), "movies.json")
        if os.path.exists(movies_path):
            with open(movies_path, "r") as f:
                movies = json.load(f)
            if movies:
                import random
                # Use seed based on today's date so it changes once per day
                random.seed(time.strftime('%Y%m%d'))
                motd = random.choice(movies)
                motd_name = motd.get("name", "Unknown Movie")
                motd_text = f"\n\n🌟 <b>Movie of the Day:</b> {motd_name}\n"
    except Exception as e:
        logging.error(f"MOTD Error: {e}")

    bot.reply_to(
        message,
        f"Hello {name}! 🎬 Welcome to the <b>Instan movie Bot</b>.{new_user_note}{motd_text}\n\n💳 <b>Balance: {credits} Credits</b>\n\n{bot_guide_text}",
        reply_markup=get_main_keyboard(user_id)
    )

bot_guide_text = (
    f"I can give you legal movie info and recommendations.\n\n"
    f"<b>How to use:</b>\n"
    f"1. Just type the Movie or Anime name to get details.\n"
    f"2. Use /rate <b>Movie Name | 1-5</b> to rate a movie.\n"
    f"3. I only provide <b>LEGAL</b> information. No piracy links here!\n\n"
    f"<b>Rules:</b>\n"
    f"- Respect the community.\n"
    f"- No spamming (1 request per minute).\n"
    f"- Only admin-approved links are allowed."
)

@bot.message_handler(commands=["balance"])
def balance_cmd(message):
    register_user_credits(message.from_user.id)
    credits = get_credits(message.from_user.id)
    bot.reply_to(message, f"💳 <b>Aapka Balance: {credits} Credits</b>", reply_markup=get_main_keyboard(message.from_user.id))

# =========================
# CREDIT CALLBACKS
# =========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def handle_menu(call):
    user_id = call.from_user.id
    register_user_credits(user_id)
    action = call.data

    if action == "menu_earn":
        now = time.time()
        last = ad_cooldowns.get(str(user_id), 0)
        remaining = AD_COOLDOWN_SECONDS - (now - last)
        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            bot.answer_callback_query(call.id, f"⏳ {mins}m {secs}s baad try karo!", show_alert=True)
            return
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("📺 Ad Dekho", url=ADSTERRA_LINK))
        markup.row(InlineKeyboardButton("✅ Claim Credits (+3)", callback_data="claim_ad"))
        bot.send_message(
            call.message.chat.id,
            "📺 <b>Ad Dekho & Credits Kamao!</b>\n\n"
            "1. Neeche 'Ad Dekho' button dabao\n"
            "2. Ad page visit karo\n"
            "3. Wapas aa kar '✅ Claim Credits' dabao\n\n"
            f"💰 <b>+{AD_CREDITS} Credits milenge!</b>",
            reply_markup=markup,
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif action == "menu_invite":
        bot_username = "Tastingofthe_bot"
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        bot.send_message(
            call.message.chat.id,
            f"🎁 <b>Dosto ko Invite Karo!</b>\n\n"
            f"Har dost join karne par <b>+{REFERRAL_CREDITS} Credits</b> milenge!\n\n"
            f"🔗 Tera referral link:\n<code>{ref_link}</code>",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif action == "menu_premium":
        bot.send_message(
            call.message.chat.id,
            "👑 <b>Premium Plan</b>\n\n"
            "Coming soon! Premium users ko unlimited video processing milega.\n\n"
            "Admin se contact karo: @admin",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif action == "menu_video":
        credits = get_credits(user_id)
        bot.send_message(
            call.message.chat.id,
            f"🎬 <b>Caption Video Banao</b>\n\n"
            f"💳 Balance: <b>{credits} Credits</b>\n"
            f"💸 Cost: <b>{VIDEO_CREDIT_COST} Credits</b> per video\n\n"
            f"Video ya document send karo — main subtitles add kar dunga!",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "claim_ad")
def claim_ad(call):
    user_id = call.from_user.id
    now = time.time()
    last = ad_cooldowns.get(str(user_id), 0)
    remaining = AD_COOLDOWN_SECONDS - (now - last)

    if remaining > 0:
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        bot.answer_callback_query(call.id, f"⏳ {mins}m {secs}s baad claim karo!", show_alert=True)
        return

    ad_cooldowns[str(user_id)] = now
    add_credits(user_id, AD_CREDITS)
    credits = get_credits(user_id)
    bot.answer_callback_query(call.id, f"✅ +{AD_CREDITS} Credits mil gaye!", show_alert=True)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>+{AD_CREDITS} Credits claim ho gaye!</b>\n\n💳 <b>Naya Balance: {credits} Credits</b>\n\n⏳ 5 minute baad dobara earn kar sakte ho.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML"
    )

@bot.message_handler(commands=["psych"])
def psych_system(message):
    text = message.text[7:].strip() # Remove /psych
    if not text:
        msg = (
            "🧠 <b>Psychological Selection System</b>\n\n"
            "Choose your current mental state (1-5):\n"
            "1. ✨ Light / Happy\n"
            "2. 🌱 Emotional / Reflective\n"
            "3. 🎭 Tense / Curious\n"
            "4. 🌑 Disturbed / Intense\n"
            "5. 🌀 Deep / Existential\n\n"
            "Usage: /psych [1-5]"
        )
        bot.reply_to(message, msg, parse_mode="HTML")
        return

    try:
        tier_id = int(text)
        if not (1 <= tier_id <= 5):
            bot.reply_to(message, "Please choose a level between 1 and 5.")
            return
            
        movie, tier_name = psychology_tiers.get_recommendation_from_tier(tier_id)
        if movie:
            response = (
                f"🧠 <b>Tier {tier_id}: {tier_name}</b>\n\n"
                f"🎬 <b>Recommended:</b> {movie['name']}\n"
                f"🏷️ <b>Tags:</b> {', '.join(movie['tags'])}\n"
                f"🔗 <a href='{movie['link']}'>Watch Here</a>\n\n"
                f"<i>This selection is based on deterministic psychological rule-mapping.</i>"
            )
            bot.reply_to(message, response, parse_mode="HTML")
        else:
            bot.reply_to(message, "No movie found for this tier.")
    except ValueError:
        bot.reply_to(message, "Invalid input. Use /psych [1-5]")

@bot.message_handler(commands=["rate"])
def rate_movie(message):
    text = message.text[5:].strip() # Remove /rate
    if "|" not in text:
        bot.reply_to(message, "Usage: /rate Movie Name | 1-5 (e.g., /rate Dune | 5)")
        return

    try:
        movie_name, rating_str = [part.strip() for part in text.split("|", 1)]
        rating = float(rating_str)
        if not (1 <= rating <= 5):
            bot.reply_to(message, "Rating must be between 1 and 5.")
            return

        response = requests.post(PHP_API_URL, data={'action': 'rate', 'movie': movie_name, 'rating': rating}, timeout=15)
        if response.status_code == 200:
            data = response.json()
            bot.reply_to(message, f"✅ {data.get('message', 'Rating submitted!')}\nNew Rating: ⭐ {data.get('rating', '0')}/5 ({data.get('votes', '0')} votes)")
        else:
            bot.reply_to(message, f"Server Error: {response.status_code}")
    except ValueError:
        bot.reply_to(message, "Invalid rating format. Use numbers 1 to 5.")
    except Exception as e:
        logging.error(f"Error rating movie: {e}")
        bot.reply_to(message, "❌ Internal error.")

@bot.message_handler(commands=["add"])
def add_movie(message):
    user_id = str(message.from_user.id)
    
    if user_id != ADMIN_USER_ID:
        bot.reply_to(message, "❌ Better luck next time 😔")
        return

    text = message.text[5:].strip() # Remove /add
    if "|" not in text:
        bot.reply_to(message, "Usage: /add Movie Name | https://link")
        return

    name, link = [part.strip() for part in text.split("|", 1)]

    try:
        response = requests.post(PHP_API_URL, data={'action': 'add', 'movie': name, 'link': link}, timeout=15)
        if response.status_code == 200:
            data = response.json()
            bot.reply_to(message, f"✅ {data.get('message', 'Done')}")
        else:
            bot.reply_to(message, f"Server Error: {response.status_code}")
    except Exception as e:
        logging.error(f"Error adding movie: {e}")
        bot.reply_to(message, "❌ Internal error.")

def clean_query(text):
    # Remove common filler words
    fillers = ["information about", "movie", "film", "show", "anime", "please", "search", "find", "get"]
    clean_text = text.lower()
    for filler in fillers:
        clean_text = clean_text.replace(filler, "")
    
    # Advanced normalization for Dune 2/Part 2 style
    clean_text = clean_text.replace("part", "").replace("two", "2").replace("one", "1").replace("  ", " ")
    return clean_text.strip()

def normalize(text):
    return text.lower().replace(" ", "").replace("_", "").replace("-", "").strip()

def delete_message_after_delay(chat_id, message_id, delay):
    time.sleep(delay)
    try:
        bot.delete_message(chat_id, message_id)
        logging.info(f"Deleted message {message_id} in chat {chat_id} after {delay}s delay.")
    except Exception as e:
        logging.error(f"Failed to delete message: {e}")

def find_all_matching_movies(query, movies):
    q = normalize(query)
    matches = []
    for idx, m in enumerate(movies):
        stored_name = clean_query(m.get("name", ""))
        name_norm = normalize(stored_name)
        if q in name_norm or name_norm in q:
            matches.append((idx, m))
    return matches

def create_selection_keyboard(matches, page=0, query_id=""):
    markup = InlineKeyboardMarkup()
    total_pages = (len(matches) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start = page * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, len(matches))
    
    for idx, movie in matches[start:end]:
        name = movie.get('name', 'Unknown')
        display_name = name[:45] + "..." if len(name) > 45 else name
        callback_data = f"sel_{query_id}_{idx}"
        markup.add(InlineKeyboardButton(text=f"📁 {display_name}", callback_data=callback_data))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{query_id}_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{query_id}_{page+1}"))
    
    if nav_buttons:
        markup.row(*nav_buttons)
    
    return markup


@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_"))
def handle_selection(call):
    try:
        parts = call.data.split("_")
        query_id = parts[1]
        movie_idx = int(parts[-1])
        
        movies_path = os.path.join(os.path.dirname(__file__), "movies.json")
        if not os.path.exists(movies_path):
            bot.answer_callback_query(call.id, "Movie database not found.")
            return
            
        with open(movies_path, "r") as f:
            movies = json.load(f)
        
        if movie_idx >= len(movies):
            bot.answer_callback_query(call.id, "Invalid selection.")
            return
        
        movie = movies[movie_idx]
        movie_name = movie.get('name', 'Unknown')
        original_link = movie.get('link')
        movie_rating = movie.get('rating', 0)
        movie_votes = movie.get('votes', 0)
        
        short_link = shorten_url(original_link)
        warning = "\n\n⚠️ <b>IS File ko dusri jagah forward ⏩ kar lein, yeh file 2 minutes mein delete ho jayegi due to copyright © issue</b>"
        rating_text = f"\n⭐ Rating: {movie_rating}/5 ({movie_votes} votes)" if movie_votes > 0 else "\n⭐ No ratings yet. Use /rate to be the first!"
        
        final_message = f"🎬 <b>{movie_name}</b>\n\n{short_link}{rating_text}{warning}"
        
        # Send as a NEW message to avoid any conflicts with existing buttons
        sent_msg = bot.send_message(
            chat_id=call.message.chat.id,
            text=final_message,
            parse_mode="HTML"
        )
        
        threading.Thread(target=delete_message_after_delay, args=(call.message.chat.id, sent_msg.message_id, 120)).start()
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Selection callback error: {e}")
        bot.answer_callback_query(call.id, "Error processing selection.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("page_"))
def handle_pagination(call):
    try:
        parts = call.data.split("_")
        new_page = int(parts[-1])
        search_query = "_".join(parts[1:-1])
        
        movies_path = os.path.join(os.path.dirname(__file__), "movies.json")
        if not os.path.exists(movies_path):
            bot.answer_callback_query(call.id, "Movie database not found.")
            return
            
        with open(movies_path, "r") as f:
            movies = json.load(f)
        
        matches = find_all_matching_movies(search_query, movies)
        if not matches:
            bot.answer_callback_query(call.id, "No matches found.")
            return
        
        title = search_query.title()
        markup = create_selection_keyboard(matches, new_page, search_query)
        total = len(matches)
        
        header = f"🎬 <b>Title: {title}</b>\n📂 <i>Your Files are Ready Now</i>\n\n🎥 <b>{title}</b> 🎥\n\nFound <b>{total}</b> versions. Select one:"
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=header,
            reply_markup=markup,
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Pagination callback error: {e}")
        bot.answer_callback_query(call.id, "Error changing page.")

@bot.callback_query_handler(func=lambda call: call.data == "noop")
def handle_noop(call):
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    user_text = message.text.strip()

    # 🚫 Ignore commands
    if user_text.startswith("/"):
        return

    if not user_text: return
    
    query = clean_query(user_text)
    if not query: query = user_text # Fallback if cleaning removed everything
    
    # 1. Check local movies.json first (PRIORITIZED, NO COOLDOWN)
    try:
        movies_path = os.path.join(os.path.dirname(__file__), "movies.json")
        if os.path.exists(movies_path):
            with open(movies_path, "r") as f:
                movies = json.load(f)
            
            matches = find_all_matching_movies(query, movies)
            
            if len(matches) > 1:
                markup = create_selection_keyboard(matches, 0, query)
                total = len(matches)
                header = f"🎬 <b>Title: {user_text.title()}</b>\n📂 <i>Your Files are Ready Now</i>\n\n🎥 <b>{user_text.title()}</b> 🎥\n\nFound <b>{total}</b> versions. Select one:"
                
                sent_msg = bot.reply_to(message, header, reply_markup=markup)
                threading.Thread(target=delete_message_after_delay, args=(message.chat.id, sent_msg.message_id, 120)).start()
                logging.info(f"Path Used: LOCAL MULTI-SELECT for query '{query}' (Found: {total} matches)")
                return
            
            elif len(matches) == 1:
                movie = matches[0][1]
                movie_name = movie.get('name', query)
                original_link = movie.get('link')
                movie_rating = movie.get('rating', 0)
                movie_votes = movie.get('votes', 0)
                logging.info(f"Path Used: LOCAL for query '{query}' (Found: {movie_name})")
                
                short_link = shorten_url(original_link)
                
                warning = "\n\n⚠️ <b>IS File ko dusri jagah forward ⏩ kar lein, yeh file 2 minutes mein delete ho jayegi due to copyright © issue</b>"
                rating_text = f"\n⭐ Rating: {movie_rating}/5 ({movie_votes} votes)" if movie_votes > 0 else "\n⭐ No ratings yet. Use /rate to be the first!"
                
                final_message = f"{short_link}{rating_text}{warning}"
                sent_msg = bot.reply_to(message, final_message)
                
                threading.Thread(target=delete_message_after_delay, args=(message.chat.id, sent_msg.message_id, 120)).start()
                return
    except Exception as e:
        logging.error(f"Local search error: {e}")

    # 2. If no local match, APPLY RATE LIMIT
    user_id = message.from_user.id
    if is_rate_limited(user_id):
        sent_msg = bot.reply_to(message, "⏳ Please wait a minute before searching again.")
        threading.Thread(target=delete_message_after_delay, args=(message.chat.id, sent_msg.message_id, 30)).start()
        return

    # 3. Fallback to Psychological Conversion
    bot.send_chat_action(message.chat.id, 'typing')
    logging.info(f"Path Used: PSYCH_FALLBACK for query '{query}'")
    
    # Log to Google Sheets
    log_to_sheets(query)
    
    try:
        fallback_msg = psychology_tiers.get_fallback_message(query)
        sent_msg = bot.reply_to(message, fallback_msg)
        # Auto-delete after 120s
        threading.Thread(target=delete_message_after_delay, args=(message.chat.id, sent_msg.message_id, 120)).start()
    except Exception as e:
        logging.error(f"Psych Fallback Execution Error: {e}")
        sent_msg = bot.reply_to(message, "Sorry, I couldn't fetch the info right now.")
        threading.Thread(target=delete_message_after_delay, args=(message.chat.id, sent_msg.message_id, 30)).start()

# =========================
# REAL-ESRGAN ENHANCEMENT
# =========================
ESRGAN_MODEL = "nightmareai/real-esrgan:42fed1c4974146d4d2414e2be2c5277c7fcf05fcc3a73abf41610695738c1d7b"
pending_enhance = {}  # {chat_id: True} — tracks users waiting to send image

def replicate_enhance_image(image_url, scale=4, face_enhance=False):
    """Call Replicate Real-ESRGAN API. Returns enhanced image URL or raises."""
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN not set. API key add karein.")
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    # Create prediction
    payload = {
        "version": ESRGAN_MODEL.split(":")[1],
        "input": {"image": image_url, "scale": scale, "face_enhance": face_enhance}
    }
    r = requests.post("https://api.replicate.com/v1/predictions", json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    pred = r.json()
    pred_id = pred["id"]

    # Poll for result (max 3 minutes)
    for _ in range(36):
        time.sleep(5)
        r2 = requests.get(f"https://api.replicate.com/v1/predictions/{pred_id}", headers=headers, timeout=15)
        r2.raise_for_status()
        data = r2.json()
        status = data.get("status")
        if status == "succeeded":
            return data["output"]
        elif status in ("failed", "canceled"):
            raise RuntimeError(f"Enhancement failed: {data.get('error','unknown error')}")
    raise RuntimeError("Enhancement timeout — 3 min se zyada lag gaya.")

def process_image_enhance(bot, message, file_id, scale=4, face_enhance=False):
    """Download image from Telegram, upload to tmpfiles.org, call ESRGAN, send result."""
    chat_id = message.chat.id
    user_id = message.from_user.id
    status_msg = bot.send_message(chat_id, "⏳ Image enhance ho rahi hai... 0%\n🔮 Real-ESRGAN AI processing")

    try:
        # Download image from Telegram
        bot.edit_message_text("📥 Image download ho rahi hai... 20%", chat_id, status_msg.message_id)
        file_info = bot.get_file(file_id)
        file_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        resp = requests.get(file_url, timeout=60)
        img_bytes = resp.content

        # Upload to tmpfiles.org for public URL (Replicate needs a public URL)
        bot.edit_message_text("☁️ Image upload ho rahi hai... 40%", chat_id, status_msg.message_id)
        upload_resp = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": ("image.jpg", img_bytes, "image/jpeg")},
            timeout=30
        )
        upload_resp.raise_for_status()
        upload_data = upload_resp.json()
        # tmpfiles.org returns url like https://tmpfiles.org/XXXXX/image.jpg
        public_url = upload_data["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")

        # Call Real-ESRGAN
        bot.edit_message_text("🔮 Real-ESRGAN AI enhance kar raha hai... 60%\n⏳ 30-60 seconds lagenge", chat_id, status_msg.message_id)
        enhanced_url = replicate_enhance_image(public_url, scale=scale, face_enhance=face_enhance)

        # Download enhanced image
        bot.edit_message_text("📥 Enhanced image download ho rahi hai... 85%", chat_id, status_msg.message_id)
        enhanced_resp = requests.get(enhanced_url, timeout=60)

        bot.edit_message_text("📤 Bhej raha hoon... 100% ✅", chat_id, status_msg.message_id)
        bot.send_photo(
            chat_id,
            enhanced_resp.content,
            caption=(
                f"✅ <b>AI Enhancement Done!</b>\n\n"
                f"🔮 <b>Real-ESRGAN {scale}x</b> upscaling\n"
                f"😊 Face Enhance: <b>{'ON' if face_enhance else 'OFF'}</b>\n"
                f"💳 Cost: <b>-{ENHANCE_CREDIT_COST} Credit</b>"
            ),
            parse_mode="HTML"
        )
        bot.delete_message(chat_id, status_msg.message_id)

    except RuntimeError as e:
        bot.edit_message_text(f"❌ <b>Enhancement Error:</b>\n{e}", chat_id, status_msg.message_id, parse_mode="HTML")
    except Exception as e:
        logging.error(f"ESRGAN error: {e}")
        bot.edit_message_text(f"❌ Error: {e}", chat_id, status_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_enhance")
def handle_menu_enhance(call):
    user_id = call.from_user.id
    register_user_credits(user_id)
    credits = get_credits(user_id)
    bot.answer_callback_query(call.id)
    if credits < ENHANCE_CREDIT_COST:
        bot.send_message(
            call.message.chat.id,
            f"❌ <b>Credits kam hain!</b>\n\n"
            f"💳 Balance: <b>{credits} Credits</b>\n"
            f"💸 Required: <b>{ENHANCE_CREDIT_COST} Credit</b>\n\n"
            f"💰 Ad dekh kar credits kamao!",
            parse_mode="HTML", reply_markup=get_main_keyboard(user_id)
        )
        return
    esrgan_status = "✅ Ready" if REPLICATE_API_TOKEN else "⚠️ API key pending"
    bot.send_message(
        call.message.chat.id,
        f"🔮 <b>AI Image/Video Enhancer</b>\n\n"
        f"<b>Real-ESRGAN</b> se image 4x sharper aur HD ban jaati hai!\n\n"
        f"💳 Cost: <b>{ENHANCE_CREDIT_COST} Credit</b> per image\n"
        f"🔑 API Status: <b>{esrgan_status}</b>\n\n"
        f"📸 <b>Abhi image bhejo enhance karne ke liye!</b>\n\n"
        f"<i>Options:\n"
        f"• /enhance — Normal 4x enhance\n"
        f"• /enhance_face — Face detail boost ke saath\n"
        f"• /enhance2x — 2x (smaller file)</i>",
        parse_mode="HTML"
    )
    pending_enhance[str(call.message.chat.id)] = {"scale": 4, "face": False}

@bot.message_handler(commands=["enhance"])
def cmd_enhance(message):
    user_id = message.from_user.id
    register_user_credits(user_id)
    pending_enhance[str(message.chat.id)] = {"scale": 4, "face": False}
    bot.reply_to(message,
        "🔮 <b>AI Enhance Mode ON</b>\n\n"
        "📸 Abhi image bhejo — Real-ESRGAN 4x enhance karega!\n"
        f"💳 Cost: <b>{ENHANCE_CREDIT_COST} Credit</b>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["enhance_face"])
def cmd_enhance_face(message):
    user_id = message.from_user.id
    register_user_credits(user_id)
    pending_enhance[str(message.chat.id)] = {"scale": 4, "face": True}
    bot.reply_to(message,
        "🔮 <b>AI Face Enhance Mode ON</b>\n\n"
        "📸 Abhi image bhejo — Real-ESRGAN face details boost karega!\n"
        f"💳 Cost: <b>{ENHANCE_CREDIT_COST} Credit</b>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["enhance2x"])
def cmd_enhance2x(message):
    user_id = message.from_user.id
    register_user_credits(user_id)
    pending_enhance[str(message.chat.id)] = {"scale": 2, "face": False}
    bot.reply_to(message,
        "🔮 <b>AI Enhance 2x Mode ON</b>\n\n"
        "📸 Abhi image bhejo — Real-ESRGAN 2x enhance karega!\n"
        f"💳 Cost: <b>{ENHANCE_CREDIT_COST} Credit</b>",
        parse_mode="HTML"
    )

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    key = str(chat_id)

    if key not in pending_enhance:
        return  # Not in enhance mode, ignore

    enhance_opts = pending_enhance.pop(key)
    register_user_credits(user_id)

    if get_credits(user_id) < ENHANCE_CREDIT_COST:
        bot.reply_to(message,
            f"❌ <b>Credits kam hain!</b>\n💳 Balance: <b>{get_credits(user_id)}</b>\n"
            f"💸 Required: <b>{ENHANCE_CREDIT_COST} Credit</b>",
            parse_mode="HTML", reply_markup=get_main_keyboard(user_id)
        )
        return

    deduct_credits(user_id, ENHANCE_CREDIT_COST)
    # Use highest resolution photo
    file_id = message.photo[-1].file_id
    threading.Thread(
        target=process_image_enhance,
        args=(bot, message, file_id),
        kwargs={"scale": enhance_opts["scale"], "face_enhance": enhance_opts["face"]}
    ).start()

# =========================
# SPEECH TO TEXT + SUBTITLE
# =========================
MAX_VIDEO_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB
pending_videos = {}  # key -> {file_id, file_name, chat_id, message_id}

SUBTITLE_LANGUAGES = {
    "en":       ("🇬🇧 English", "en"),
    "hi":       ("🇮🇳 Hindi", "hi"),
    "hinglish": ("🔀 Hinglish", None),
}

pending_style_choices = {}  # vid_key -> {lang_label, whisper_lang, user_id}

STYLE_PRESETS = {
    "netflix": {
        "font": "Noto Sans Devanagari", "size": 20, "bold": 1,
        "color": "&H00FFFFFF", "outline_color": "&H00000000",
        "outline": 2, "shadow": 0, "alignment": 2,
        "marginv": 30, "fade": 200, "spacing": 0,
        "label": "🎬 Netflix"
    },
    "shorts": {
        "font": "Noto Sans Devanagari", "size": 26, "bold": 1,
        "color": "&H00FFFFFF", "outline_color": "&H00000000",
        "outline": 2, "shadow": 0, "alignment": 2,
        "marginv": 25, "fade": 100, "spacing": 0,
        "label": "📱 Shorts", "word_highlight": True, "max_words": 5
    },
    "reels": {
        "font": "Noto Sans Devanagari", "size": 24, "bold": 1,
        "color": "&H0000FFFF", "outline_color": "&H00000000",
        "outline": 2, "shadow": 0, "alignment": 2,
        "marginv": 25, "fade": 150, "spacing": 0,
        "label": "🎥 Reels", "max_words": 5
    },
    "gaming": {
        "font": "Noto Sans Devanagari", "size": 28, "bold": 1,
        "color": "&H0000FF00", "outline_color": "&H00FF0000",
        "outline": 3, "shadow": 1, "alignment": 2,
        "marginv": 20, "fade": 50, "spacing": 1,
        "label": "🎮 Gaming"
    },
    "cinematic": {
        "font": "Noto Sans Devanagari", "size": 18, "bold": 0,
        "color": "&H00FFFFFF", "outline_color": "&H00000000",
        "outline": 1, "shadow": 1, "alignment": 2,
        "marginv": 40, "fade": 400, "spacing": 2,
        "label": "✨ Cinematic"
    },
}

TRANSLATE_MODES = {
    "original": "📝 Original",
    "hindi":    "🇮🇳 Hindi",
    "english":  "🇬🇧 English",
    "bilingual": "🔀 Bilingual",
}

FONT_OPTIONS = {
    "poppins":     ("🅿️ Poppins",      "Poppins"),
    "montserrat":  ("🅼 Montserrat",    "Montserrat"),
    "bebas":       ("🆃 Bebas Neue",    "Bebas Neue"),
    "anton":       ("🆃 Anton",         "Anton"),
    "devanagari":  ("🔤 Noto Devanagari","Noto Sans Devanagari"),
}

COLOR_OPTIONS = {
    "white":   ("⚪ White",   "&H00FFFFFF"),
    "yellow":  ("🟡 Yellow",  "&H0000FFFF"),
    "red":     ("🔴 Red",     "&H000000FF"),
    "green":   ("🟢 Green",   "&H0000FF00"),
    "cyan":    ("🔵 Cyan",    "&H00FFFF00"),
}

def seconds_to_srt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def generate_srt(segments):
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = seconds_to_srt_time(seg['start'])
        end = seconds_to_srt_time(seg['end'])
        text = seg['text'].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)

def seconds_to_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"

def generate_ass(segments, font_name="Noto Sans Devanagari"):
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 384\n"
        "PlayResY: 288\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},20,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,2,10,10,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dialogues = []
    for seg in segments:
        start = seconds_to_ass_time(seg['start'])
        end   = seconds_to_ass_time(seg['end'])
        text  = seg['text'].strip().replace("\n", " ")
        # \fad(fadein_ms, fadeout_ms) — 200ms fade in + 200ms fade out
        dialogues.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\fad(200,200)}}{text}")
    return header + "\n".join(dialogues)

def auto_line_break(text, max_words=5):
    words = text.strip().split()
    if len(words) <= max_words:
        return text
    # Max 2 lines — split roughly in half
    mid = (len(words) + 1) // 2
    line1 = " ".join(words[:mid])
    line2 = " ".join(words[mid:])
    return line1 + "\\N" + line2

def _has_devanagari(segments):
    for seg in segments:
        for ch in seg.get('text', ''):
            if '\u0900' <= ch <= '\u097F':
                return True
    return False

def generate_ass_styled(segments, style_key="netflix", words_data=None, font_name=None, color=None):
    p = STYLE_PRESETS.get(style_key, STYLE_PRESETS["netflix"])
    # Auto-fallback: if text has Devanagari, always use Noto — other fonts show boxes
    if _has_devanagari(segments):
        eff_font = "Noto Sans Devanagari"
    else:
        eff_font = font_name if font_name else p['font']
    eff_color = color if color else p['color']
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{eff_font},{p['size']},{eff_color},&H000000FF,{p['outline_color']},&H64000000,{p['bold']},0,0,0,100,100,{p['spacing']},0,1,{p['outline']},{p['shadow']},{p['alignment']},10,10,{p['marginv']},1\n"
        "Style: Highlight,{font},{size},&H0000FFFF,&H000000FF,{oc},&H64000000,1,0,0,0,100,100,{sp},0,1,{ol},{sh},{al},10,10,{mv},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ).format(
        font=p['font'], size=p['size']+4, oc=p['outline_color'],
        sp=p['spacing'], ol=p['outline'], sh=p['shadow'],
        al=p['alignment'], mv=p['marginv']
    )
    fade = p.get('fade', 200)
    max_words = p.get('max_words', 0)
    use_word_highlight = p.get('word_highlight', False)
    dialogues = []

    for seg in segments:
        start = seconds_to_ass_time(seg['start'])
        end = seconds_to_ass_time(seg['end'])
        raw_text = seg['text'].strip().replace("\n", " ")
        text = auto_line_break(raw_text, max_words) if max_words > 0 else raw_text

        if use_word_highlight and words_data and seg.get('id') is not None:
            seg_words = [w for w in words_data if seg['start'] <= w['start'] < seg['end']]
            if seg_words:
                karaoke = ""
                for w in seg_words:
                    dur_cs = max(1, int((w['end'] - w['start']) * 100))
                    karaoke += f"{{\\kf{dur_cs}}}{w['word']} "
                text = karaoke.strip()

        dialogues.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\fad({fade},{fade})}}{text}")

    return header + "\n".join(dialogues)

def gemini_add_emojis(segments):
    if not gemini_client or not segments:
        return segments
    try:
        numbered = "\n".join(f"{i+1}. {s['text'].strip()}" for i, s in enumerate(segments))
        prompt = (
            "You are a subtitle emoji assistant. Add 1 relevant emoji at the END of lines that express strong emotion (happy, sad, angry, surprise, love, fear). "
            "Rules: Add emoji ONLY to max 30% of lines. Do NOT add emoji to neutral/informational lines. "
            f"Return exactly {len(segments)} lines as '1. text', '2. text'.\n\nLines:\n{numbered}"
        )
        resp = gemini_client.models.generate_content(model="gemini-2.0-flash-lite", contents=prompt)
        result_map = {}
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if ". " in line:
                idx_s, _, txt = line.partition(". ")
                if idx_s.isdigit():
                    result_map[int(idx_s)-1] = txt.strip()
        return [{**s, 'text': result_map.get(i, s['text'])} for i, s in enumerate(segments)]
    except Exception as e:
        logging.error(f"Gemini emoji error: {e}")
        return segments

def gemini_translate_segments(segments, target_lang, lang_label):
    if not gemini_client or not segments:
        return segments
    try:
        numbered = "\n".join(f"{i+1}. {s['text'].strip()}" for i, s in enumerate(segments))
        if target_lang == "bilingual":
            prompt = (
                f"Translate each subtitle line to English and return BOTH original and English translation on same line separated by \\N.\n"
                f"Return exactly {len(segments)} lines as '1. original\\Ntranslation'.\n\nLines:\n{numbered}"
            )
        else:
            lang_name = "Hindi" if target_lang == "hindi" else "English"
            prompt = (
                f"Translate these subtitle lines to {lang_name}. Keep it natural and short.\n"
                f"Return exactly {len(segments)} lines as '1. text', '2. text'.\n\nLines:\n{numbered}"
            )
        resp = gemini_client.models.generate_content(model="gemini-2.0-flash-lite", contents=prompt)
        result_map = {}
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if ". " in line:
                idx_s, _, txt = line.partition(". ")
                if idx_s.isdigit():
                    result_map[int(idx_s)-1] = txt.strip()
        return [{**s, 'text': result_map.get(i, s['text'])} for i, s in enumerate(segments)]
    except Exception as e:
        logging.error(f"Gemini translate error: {e}")
        return segments

def gemini_correct_segments(segments, lang_label):
    if not gemini_client:
        return segments
    try:
        # Build numbered text list to send to Gemini
        numbered_lines = "\n".join(
            f"{i+1}. {seg['text'].strip()}"
            for i, seg in enumerate(segments)
        )
        prompt = (
            f"You are a subtitle corrector. Language: {lang_label}.\n"
            f"Fix ONLY spelling, punctuation, grammar mistakes in these {len(segments)} subtitle lines.\n"
            f"STRICT RULES:\n"
            f"- Each corrected line must be SHORTER or EQUAL length to the original — NEVER repeat words\n"
            f"- Do NOT add new words, do NOT repeat any word more than it appears in the original\n"
            f"- Do NOT translate — keep the exact same language as input\n"
            f"- Return exactly {len(segments)} lines in format: '1. text', '2. text', etc.\n"
            f"- No extra explanation, no blank lines between entries\n\n"
            f"Lines:\n{numbered_lines}"
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt
        )
        corrected_raw = response.text.strip()

        # Parse corrected lines back into segments
        corrected_map = {}
        for line in corrected_raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if ". " in line:
                idx_str, _, text = line.partition(". ")
                if idx_str.isdigit():
                    corrected_map[int(idx_str) - 1] = text.strip()

        # Apply corrections — reject if corrected text is >1.5x the original length (hallucination guard)
        corrected_segments = []
        for i, seg in enumerate(segments):
            new_seg = dict(seg)
            original_text = seg['text'].strip()
            corrected_text = corrected_map.get(i, original_text)
            if len(corrected_text) > max(len(original_text) * 1.5, len(original_text) + 20):
                logging.warning(f"Gemini hallucination detected on segment {i}, using original.")
                new_seg['text'] = original_text
            else:
                new_seg['text'] = corrected_text
            corrected_segments.append(new_seg)

        logging.info(f"Gemini corrected {len(corrected_map)}/{len(segments)} segments.")
        return corrected_segments
    except Exception as e:
        logging.error(f"Gemini correction error: {e}")
        return segments  # Fall back to original if Gemini fails

def process_video_subtitles(bot, message, file_id, file_name, language=None, lang_label="Auto", style_key="netflix", translate_mode="original", font_name=None, color_hex=None):
    chat_id = message.chat.id
    style_label = STYLE_PRESETS.get(style_key, {}).get("label", "🎬 Netflix")
    status_msg = bot.send_message(chat_id, f"⏳ Downloading... 10%\nLanguage: <b>{lang_label}</b> | Style: <b>{style_label}</b>", parse_mode="HTML")

    tmp_dir = tempfile.mkdtemp()
    video_path = os.path.join(tmp_dir, "input_video.mp4")
    audio_path = os.path.join(tmp_dir, "audio.wav")
    ass_path = os.path.join(tmp_dir, "subtitles.ass")
    output_path = os.path.join(tmp_dir, "output_subtitled.mp4")

    try:
        # Download video
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        response = requests.get(file_url, stream=True, timeout=120)
        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        bot.edit_message_text("🎙️ Audio extract ho raha hai... 25%", chat_id, status_msg.message_id)

        # Extract audio using ffmpeg
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            audio_path
        ], check=True, capture_output=True)

        # Transcribe with whisper
        import whisper
        # Use better model for Hindi/Hinglish, tiny is enough for English
        if language == "hi" or language is None:
            model_name = "small"
        else:
            model_name = "tiny"
        bot.edit_message_text(f"🧠 Whisper ({model_name}) speech-to-text... 45%\n⏳ 1-4 min lag sakte hain", chat_id, status_msg.message_id)
        model = whisper.load_model(model_name)
        need_word_ts = style_key in ("shorts", "reels", "gaming")
        transcribe_kwargs = {
            "fp16": False,
            "beam_size": 5,
            "best_of": 5,
            "temperature": 0,
            "condition_on_previous_text": False,
            "task": "transcribe",
            "word_timestamps": need_word_ts,
        }
        if language:
            transcribe_kwargs["language"] = language
        result = model.transcribe(audio_path, **transcribe_kwargs)

        if not result.get("segments"):
            bot.edit_message_text("❌ Video mein koi speech nahi mili.", chat_id, status_msg.message_id)
            return

        # Gemini correction
        bot.edit_message_text("🤖 Gemini text correct kar raha hai... 60%", chat_id, status_msg.message_id)
        segments = result["segments"]
        for i, seg in enumerate(segments):
            seg['id'] = i
        corrected_segments = gemini_correct_segments(segments, lang_label)

        # Translation step
        if translate_mode != "original":
            mode_label = TRANSLATE_MODES.get(translate_mode, translate_mode)
            bot.edit_message_text(f"🌐 Gemini translate kar raha hai ({mode_label})... 70%", chat_id, status_msg.message_id)
            corrected_segments = gemini_translate_segments(corrected_segments, translate_mode, lang_label)

        # Emoji intelligence step (only for emotional content)
        bot.edit_message_text("😊 Gemini emotions detect kar raha hai... 80%", chat_id, status_msg.message_id)
        corrected_segments = gemini_add_emojis(corrected_segments)

        # Extract word-level timestamps if available (for Shorts/Reels/Gaming)
        words_data = None
        if need_word_ts:
            words_data = []
            for seg in result["segments"]:
                for w in seg.get("words", []):
                    words_data.append({"word": w.get("word","").strip(), "start": w.get("start",0), "end": w.get("end",0)})

        # Generate styled ASS
        ass_content = generate_ass_styled(corrected_segments, style_key=style_key, words_data=words_data, font_name=font_name, color=color_hex)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        bot.edit_message_text("🎨 Subtitles video mein burn ho rahi hain... 90%", chat_id, status_msg.message_id)

        # Burn subtitles into video — ASS format supports fade + Devanagari font
        fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        subtitle_filter = f"ass={ass_path}:fontsdir={fonts_dir}"
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", subtitle_filter,
            "-c:a", "copy",
            output_path
        ], check=True, capture_output=True)

        bot.edit_message_text("📤 Video bhej raha hoon... 100% ✅", chat_id, status_msg.message_id)

        # Send back the subtitled video
        tr_label = TRANSLATE_MODES.get(translate_mode, translate_mode)
        preview_text = ' '.join(s['text'] for s in corrected_segments)[:300].strip()
        dots = '...' if len(' '.join(s['text'] for s in corrected_segments)) > 300 else ''
        with open(output_path, 'rb') as vid:
            bot.send_video(
                chat_id,
                vid,
                caption=(
                    f"✅ <b>Done! Subtitles ready!</b>\n\n"
                    f"🎨 Style: <b>{style_label}</b>\n"
                    f"🌐 Mode: <b>{tr_label}</b>\n"
                    f"🗣️ Language: <b>{lang_label}</b>\n\n"
                    f"📝 <b>Preview:</b>\n<i>{preview_text}{dots}</i>"
                ),
                parse_mode="HTML",
                supports_streaming=True
            )

        bot.delete_message(chat_id, status_msg.message_id)

    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg error: {e.stderr.decode() if e.stderr else e}")
        bot.edit_message_text("❌ FFmpeg error: Video process nahi ho saki.", chat_id, status_msg.message_id)
    except Exception as e:
        logging.error(f"Subtitle process error: {e}")
        bot.edit_message_text(f"❌ Error: {e}", chat_id, status_msg.message_id)
    finally:
        # Cleanup temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

@bot.message_handler(content_types=['video', 'document'])
def handle_video_subtitle(message):
    if message.content_type == 'video':
        file_id = message.video.file_id
        file_size = message.video.file_size
        file_name = message.video.file_name or "video.mp4"
    elif message.content_type == 'document':
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            return
        file_id = message.document.file_id
        file_size = message.document.file_size
        file_name = message.document.file_name or "video.mp4"
    else:
        return

    if file_size and file_size > MAX_VIDEO_SIZE_BYTES:
        bot.reply_to(message, f"❌ Video bahut badi hai! Max size <b>200MB</b> hai.\nAapki video: {file_size // (1024*1024)}MB", parse_mode="HTML")
        return

    # Store video info and ask what user wants to do
    vid_key = f"{message.chat.id}_{message.message_id}"
    pending_videos[vid_key] = {
        "file_id": file_id,
        "file_name": file_name,
        "chat_id": message.chat.id,
        "message": message,
    }

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🎬 Subtitles Add Karo", callback_data=f"vidaction_{vid_key}_subtitle"),
    )
    markup.row(
        InlineKeyboardButton("🔮 AI Video Enhance Karo", callback_data=f"vidaction_{vid_key}_enhance"),
    )

    bot.reply_to(
        message,
        "🎬 <b>Video mili!</b>\n\nKya karna hai is video ke saath?",
        reply_markup=markup,
        parse_mode="HTML"
    )

VIDEO_QUALITY_OPTIONS = {
    "1080p30": {"label": "📺 1080p + 30fps",  "width": 1920, "height": 1080, "fps": 30, "crf": 18},
    "1080p60": {"label": "📺 1080p + 60fps",  "width": 1920, "height": 1080, "fps": 60, "crf": 18},
    "2k60":    {"label": "🖥️ 2K + 60fps",    "width": 2560, "height": 1440, "fps": 60, "crf": 16},
    "4k60":    {"label": "🎥 4K + 60fps",     "width": 3840, "height": 2160, "fps": 60, "crf": 14},
}

@bot.callback_query_handler(func=lambda call: call.data.startswith("vidaction_"))
def handle_video_action(call):
    try:
        parts   = call.data.split("_")
        action  = parts[-1]          # subtitle / enhance
        vid_key = "_".join(parts[1:-1])

        if vid_key not in pending_videos:
            bot.answer_callback_query(call.id, "⚠️ Session expire ho gaya. Video dobara bhejein.", show_alert=True)
            return

        if action == "subtitle":
            # Show language selection (same as before)
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("🇬🇧 English",        callback_data=f"sublang_{vid_key}_en"),
                InlineKeyboardButton("🇮🇳 Hindi",           callback_data=f"sublang_{vid_key}_hi"),
            )
            markup.row(
                InlineKeyboardButton("🔀 Hinglish (Auto)", callback_data=f"sublang_{vid_key}_hinglish"),
            )
            try:
                bot.edit_message_text(
                    "🎬 <b>Subtitles — Language choose karo:</b>",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=markup, parse_mode="HTML"
                )
            except Exception:
                bot.send_message(call.message.chat.id, "🌐 Language choose karo:", reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id, "🎬 Subtitle mode!")

        elif action == "enhance":
            user_id = call.from_user.id
            register_user_credits(user_id)
            if get_credits(user_id) < ENHANCE_CREDIT_COST:
                bot.answer_callback_query(call.id, f"❌ Credits kam hain! {ENHANCE_CREDIT_COST} credit chahiye.", show_alert=True)
                return
            # Show quality options
            qm = InlineKeyboardMarkup()
            qm.row(
                InlineKeyboardButton("📺 1080p + 30fps", callback_data=f"vidqual_{vid_key}_1080p30"),
                InlineKeyboardButton("📺 1080p + 60fps", callback_data=f"vidqual_{vid_key}_1080p60"),
            )
            qm.row(
                InlineKeyboardButton("🖥️ 2K + 60fps",  callback_data=f"vidqual_{vid_key}_2k60"),
                InlineKeyboardButton("🎥 4K + 60fps",   callback_data=f"vidqual_{vid_key}_4k60"),
            )
            try:
                bot.edit_message_text(
                    "🔮 <b>AI Video Enhance</b>\n\n"
                    "📺 <b>1080p 30fps</b> — Fast, standard HD\n"
                    "📺 <b>1080p 60fps</b> — Smooth HD\n"
                    "🖥️ <b>2K 60fps</b> — Ultra smooth QHD\n"
                    "🎥 <b>4K 60fps</b> — Cinema quality (slow)\n\n"
                    f"💳 Cost: <b>{ENHANCE_CREDIT_COST} Credit</b>",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=qm, parse_mode="HTML"
                )
            except Exception:
                bot.send_message(call.message.chat.id, "🎥 Quality choose karo:", reply_markup=qm, parse_mode="HTML")
            bot.answer_callback_query(call.id, "🔮 Enhancement mode!")
        else:
            bot.answer_callback_query(call.id, "❌ Unknown action.")

    except Exception as e:
        logging.error(f"Video action error: {e}")
        bot.answer_callback_query(call.id, "❌ Error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("vidqual_"))
def handle_video_quality(call):
    try:
        parts    = call.data.split("_")
        qual_key = parts[-1]
        vid_key  = "_".join(parts[1:-1])

        if vid_key not in pending_videos:
            bot.answer_callback_query(call.id, "⚠️ Session expire ho gaya.", show_alert=True)
            return

        if qual_key not in VIDEO_QUALITY_OPTIONS:
            bot.answer_callback_query(call.id, "❌ Invalid quality.")
            return

        user_id    = call.from_user.id
        video_data = pending_videos.pop(vid_key)
        deduct_credits(user_id, ENHANCE_CREDIT_COST)
        remaining  = get_credits(user_id)
        qual       = VIDEO_QUALITY_OPTIONS[qual_key]

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        bot.answer_callback_query(call.id, f"🎬 {qual['label']} — Processing! (-{ENHANCE_CREDIT_COST} Credit, Balance: {remaining})")
        threading.Thread(
            target=process_video_enhance,
            args=(bot, video_data["message"], video_data["file_id"]),
            kwargs={"qual_key": qual_key}
        ).start()

    except Exception as e:
        logging.error(f"Video quality error: {e}")
        bot.answer_callback_query(call.id, "❌ Error.")

def process_video_enhance(bot, message, file_id, qual_key="1080p30"):
    chat_id   = message.chat.id
    qual      = VIDEO_QUALITY_OPTIONS.get(qual_key, VIDEO_QUALITY_OPTIONS["1080p30"])
    w, h, fps = qual["width"], qual["height"], qual["fps"]
    crf       = qual["crf"]
    label     = qual["label"]
    status_msg = bot.send_message(
        chat_id,
        f"⏳ Enhance shuru... 0%\n🔮 Target: <b>{label}</b>",
        parse_mode="HTML"
    )
    tmp_dir     = tempfile.mkdtemp()
    input_path  = os.path.join(tmp_dir, "input.mp4")
    output_path = os.path.join(tmp_dir, "enhanced.mp4")

    try:
        # Download video
        bot.edit_message_text(f"📥 Video download ho rahi hai... 15%\n🔮 Target: <b>{label}</b>", chat_id, status_msg.message_id, parse_mode="HTML")
        file_info = bot.get_file(file_id)
        file_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        resp = requests.get(file_url, stream=True, timeout=120)
        with open(input_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        bot.edit_message_text(f"🔮 AI Enhancement processing... 40%\n📺 Upscaling to <b>{w}x{h} @ {fps}fps</b>", chat_id, status_msg.message_id, parse_mode="HTML")

        # Build FFmpeg enhancement filter chain:
        # 1. scale to target with high-quality Lanczos
        # 2. fps conversion with motion interpolation (minterpolate for 60fps)
        # 3. unsharp — sharpens details like ESRGAN
        # 4. hqdn3d — removes noise/grain cleanly
        # 5. eq — slight contrast + saturation boost
        sharpen    = "unsharp=lx=5:ly=5:la=0.8:cx=5:cy=5:ca=0.4"
        denoise    = "hqdn3d=4.0:3.0:6.0:4.5"
        color_fix  = "eq=contrast=1.05:saturation=1.1:brightness=0.02"
        scale_filt = f"scale={w}:{h}:flags=lanczos+accurate_rnd"

        if fps == 60:
            fps_filt = f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
        else:
            fps_filt = f"fps={fps}"

        vf = f"{scale_filt},{fps_filt},{sharpen},{denoise},{color_fix}"

        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path
        ], check=True, capture_output=True)

        bot.edit_message_text(f"📤 Enhanced video bhej raha hoon... 90%", chat_id, status_msg.message_id)

        output_size = os.path.getsize(output_path) // (1024 * 1024)
        with open(output_path, "rb") as vf_out:
            bot.send_video(
                chat_id, vf_out,
                caption=(
                    f"✅ <b>AI Enhancement Done!</b>\n\n"
                    f"🎯 Quality: <b>{label}</b>\n"
                    f"📐 Resolution: <b>{w}x{h}</b>\n"
                    f"🎞️ FPS: <b>{fps}fps</b>\n"
                    f"✨ Filters: Lanczos + Sharpen + Denoise + Color\n"
                    f"📦 Output size: <b>{output_size}MB</b>"
                ),
                parse_mode="HTML", supports_streaming=True
            )
        bot.delete_message(chat_id, status_msg.message_id)

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="ignore")[-300:] if e.stderr else str(e)
        logging.error(f"FFmpeg enhance error: {err}")
        bot.edit_message_text(f"❌ FFmpeg error:\n<code>{err}</code>", chat_id, status_msg.message_id, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Video enhance error: {e}")
        bot.edit_message_text(f"❌ Error: {e}", chat_id, status_msg.message_id)
    finally:
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("sublang_"))
def handle_lang_selection(call):
    try:
        # Format: sublang_{chat_id}_{msg_id}_{lang_key}
        parts = call.data.split("_")
        # lang_key is last part, vid_key is everything between "sublang_" and last "_"
        lang_key = parts[-1]
        vid_key = "_".join(parts[1:-1])

        if vid_key not in pending_videos:
            bot.answer_callback_query(call.id, "⚠️ Video session expire ho gaya. Dobara bhejein.", show_alert=True)
            return

        lang_info = SUBTITLE_LANGUAGES.get(lang_key)
        if not lang_info:
            bot.answer_callback_query(call.id, "❌ Invalid language selection.")
            return

        lang_label, whisper_lang = lang_info

        # Credit check (early, before style selection)
        user_id = call.from_user.id
        register_user_credits(user_id)
        if get_credits(user_id) < VIDEO_CREDIT_COST:
            bot.answer_callback_query(call.id, f"❌ Credits kam hain! {VIDEO_CREDIT_COST} Credits chahiye.", show_alert=True)
            bot.send_message(
                call.message.chat.id,
                f"❌ <b>Insufficient Credits!</b>\n\n"
                f"💳 Balance: <b>{get_credits(user_id)} Credits</b>\n"
                f"💸 Required: <b>{VIDEO_CREDIT_COST} Credits</b>\n\n"
                f"💰 Ad dekh kar credits kamao!",
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
            )
            return

        # Save language choice, then ask for style
        pending_style_choices[vid_key] = {
            "lang_label": lang_label,
            "whisper_lang": whisper_lang,
            "user_id": user_id,
        }

        style_markup = InlineKeyboardMarkup()
        style_markup.row(
            InlineKeyboardButton("🎬 Netflix", callback_data=f"style_{vid_key}_netflix"),
            InlineKeyboardButton("📱 Shorts",  callback_data=f"style_{vid_key}_shorts"),
        )
        style_markup.row(
            InlineKeyboardButton("🎥 Reels",   callback_data=f"style_{vid_key}_reels"),
            InlineKeyboardButton("🎮 Gaming",  callback_data=f"style_{vid_key}_gaming"),
        )
        style_markup.row(
            InlineKeyboardButton("✨ Cinematic", callback_data=f"style_{vid_key}_cinematic"),
        )
        try:
            bot.edit_message_text(
                "🎨 <b>Subtitle Style choose karo:</b>\n\n"
                "🎬 <b>Netflix</b> — White, black outline, bottom\n"
                "📱 <b>Shorts</b> — Word highlight, center, 3 words/line\n"
                "🎥 <b>Reels</b> — Yellow bold, center, 4 words/line\n"
                "🎮 <b>Gaming</b> — Neon green, bold, glow\n"
                "✨ <b>Cinematic</b> — Clean, fade, letter-spaced",
                call.message.chat.id, call.message.message_id,
                reply_markup=style_markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(
                call.message.chat.id, "🎨 <b>Style choose karo:</b>",
                reply_markup=style_markup, parse_mode="HTML"
            )
        bot.answer_callback_query(call.id, f"✅ {lang_label} select!")

    except Exception as e:
        logging.error(f"Lang selection callback error: {e}")
        bot.answer_callback_query(call.id, "❌ Error processing selection.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("style_"))
def handle_style_selection(call):
    try:
        parts = call.data.split("_")
        style_key = parts[-1]
        vid_key = "_".join(parts[1:-1])

        if vid_key not in pending_videos or vid_key not in pending_style_choices:
            bot.answer_callback_query(call.id, "⚠️ Session expire ho gaya. Video dobara bhejein.", show_alert=True)
            return

        if style_key not in STYLE_PRESETS:
            bot.answer_callback_query(call.id, "❌ Invalid style.")
            return

        lang_data = pending_style_choices.pop(vid_key)
        user_id = lang_data["user_id"]

        # Deduct credits now
        deduct_credits(user_id, VIDEO_CREDIT_COST)
        remaining = get_credits(user_id)

        # Ask translate mode
        tr_markup = InlineKeyboardMarkup()
        tr_markup.row(
            InlineKeyboardButton("📝 Original",  callback_data=f"tr_{vid_key}_{style_key}_original"),
            InlineKeyboardButton("🇮🇳 Hindi",    callback_data=f"tr_{vid_key}_{style_key}_hindi"),
        )
        tr_markup.row(
            InlineKeyboardButton("🇬🇧 English",  callback_data=f"tr_{vid_key}_{style_key}_english"),
            InlineKeyboardButton("🔀 Bilingual", callback_data=f"tr_{vid_key}_{style_key}_bilingual"),
        )

        # Store style choice back
        pending_style_choices[vid_key] = {**lang_data, "style_key": style_key}

        style_label = STYLE_PRESETS[style_key]["label"]
        try:
            bot.edit_message_text(
                f"✅ <b>{style_label}</b> select!\n\n🌐 <b>Subtitle language/mode choose karo:</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=tr_markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "🌐 <b>Mode choose karo:</b>",
                             reply_markup=tr_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, f"✅ {style_label}! (-{VIDEO_CREDIT_COST} Credits, Balance: {remaining})")

    except Exception as e:
        logging.error(f"Style selection error: {e}")
        bot.answer_callback_query(call.id, "❌ Error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("tr_"))
def handle_translate_selection(call):
    try:
        parts = call.data.split("_")
        translate_mode = parts[-1]
        style_key = parts[-2]
        vid_key = "_".join(parts[1:-2])

        if vid_key not in pending_videos or vid_key not in pending_style_choices:
            bot.answer_callback_query(call.id, "⚠️ Session expire ho gaya.", show_alert=True)
            return

        lang_data = pending_style_choices.pop(vid_key)
        pending_style_choices[vid_key] = {**lang_data, "style_key": style_key, "translate_mode": translate_mode}

        # Show Font + Color selection
        fm = InlineKeyboardMarkup()
        fm.row(
            InlineKeyboardButton("🅿️ Poppins",    callback_data=f"font_{vid_key}_poppins_white"),
            InlineKeyboardButton("🅼 Montserrat", callback_data=f"font_{vid_key}_montserrat_white"),
        )
        fm.row(
            InlineKeyboardButton("🆃 Bebas Neue", callback_data=f"font_{vid_key}_bebas_white"),
            InlineKeyboardButton("🆃 Anton",      callback_data=f"font_{vid_key}_anton_white"),
        )
        fm.row(
            InlineKeyboardButton("🔤 Noto (Hindi)", callback_data=f"font_{vid_key}_devanagari_white"),
        )
        fm.row(
            InlineKeyboardButton("⚪ White",  callback_data=f"font_{vid_key}_devanagari_white"),
            InlineKeyboardButton("🟡 Yellow", callback_data=f"font_{vid_key}_devanagari_yellow"),
            InlineKeyboardButton("🔴 Red",    callback_data=f"font_{vid_key}_devanagari_red"),
        )
        fm.row(
            InlineKeyboardButton("⏩ Default (Skip)", callback_data=f"font_{vid_key}_default_default"),
        )
        try:
            bot.edit_message_text(
                "🖋️ <b>Font & Color choose karo:</b>\n\n"
                "<i>Top row = Font, Bottom row = Color\n"
                "Ya 'Skip' karo default style use karne ke liye</i>",
                call.message.chat.id, call.message.message_id,
                reply_markup=fm, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "🖋️ <b>Font choose karo:</b>", reply_markup=fm, parse_mode="HTML")
        bot.answer_callback_query(call.id, "✅ Mode select!")

    except Exception as e:
        logging.error(f"Translate selection error: {e}")
        bot.answer_callback_query(call.id, "❌ Error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("font_"))
def handle_font_selection(call):
    try:
        parts = call.data.split("_")
        color_key = parts[-1]
        font_key  = parts[-2]
        vid_key   = "_".join(parts[1:-2])

        if vid_key not in pending_videos or vid_key not in pending_style_choices:
            bot.answer_callback_query(call.id, "⚠️ Session expire ho gaya.", show_alert=True)
            return

        lang_data  = pending_style_choices.pop(vid_key)
        video_data = pending_videos.pop(vid_key)

        # Resolve font name and color hex
        if font_key == "default":
            font_name_str = None
            color_hex     = None
            sel_label     = "Default"
        else:
            font_name_str = FONT_OPTIONS.get(font_key, (None, None))[1]
            color_hex     = COLOR_OPTIONS.get(color_key, ("⚪ White", "&H00FFFFFF"))[1]
            sel_label     = f"{FONT_OPTIONS.get(font_key,('',''))[0]} {COLOR_OPTIONS.get(color_key,('⚪',''))[0]}"

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        bot.answer_callback_query(call.id, f"🎬 Processing shuru! {sel_label}")
        threading.Thread(
            target=process_video_subtitles,
            args=(bot, video_data["message"], video_data["file_id"], video_data["file_name"]),
            kwargs={
                "language":       lang_data["whisper_lang"],
                "lang_label":     lang_data["lang_label"],
                "style_key":      lang_data.get("style_key", "netflix"),
                "translate_mode": lang_data.get("translate_mode", "original"),
                "font_name":      font_name_str,
                "color_hex":      color_hex,
            }
        ).start()

    except Exception as e:
        logging.error(f"Font selection error: {e}")
        bot.answer_callback_query(call.id, "❌ Error.")

# =========================
# MAIN RUNNER
# =========================
def force_take_polling_control():
    """Kick any other running bot instance by stealing the getUpdates lock."""
    try:
        bot.remove_webhook()
        # Direct API call to steal polling from any other running instance
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        for _ in range(3):
            requests.get(url, params={"offset": -1, "limit": 1, "timeout": 0}, timeout=10)
            time.sleep(1)
        logging.info("Force-takeover complete. Starting polling...")
    except Exception as e:
        logging.warning(f"Force-takeover warning (non-fatal): {e}")

if __name__ == "__main__":
    print("🤖 SMART Bot started (Psych System Integrated)")
    force_take_polling_control()
    while True:
        try:
            print("🤖 SMART Bot Polling Started...")
            bot.infinity_polling(skip_pending=True, timeout=90, long_polling_timeout=40)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            print("⏳ Retrying in 15 seconds...")
            force_take_polling_control()
            time.sleep(15)
