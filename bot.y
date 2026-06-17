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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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
logging.basicConfig(level=logging.INFO)

# Usage tracking (simple in-memory)
user_usage = {}
RATE_LIMIT_COOLDOWN = 60 # 1 minute

# Multi-version selection settings
ITEMS_PER_PAGE = 5
pending_selections = {}  # Store search results for pagination
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "@Instantmoviebackup") # Change this to your channel username

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

    bot.reply_to(message, f"Hello {name}! 🎬 Welcome to the <b>Instan movie Bot</b>.{motd_text}\n\n{bot_guide_text}")

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

def check_membership(user_id):
    try:
        member = bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
    except Exception as e:
        logging.error(f"Error checking membership: {e}")
    return False

def get_force_join_keyboard(original_query):
    markup = InlineKeyboardMarkup()
    # Replace with your actual channel link
    channel_url = f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}"
    markup.add(InlineKeyboardButton("📢 Join Channel", url=channel_url))
    markup.add(InlineKeyboardButton("🔄 Refresh / Verify", callback_data=f"verify_{original_query}"))
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def handle_verify(call):
    try:
        data_parts = call.data.split("_")
        original_query = data_parts[1]
        
        # Check if this was a specific movie selection or a general search
        movie_idx = None
        if len(data_parts) > 2 and data_parts[2].isdigit():
            movie_idx = int(data_parts[2])
            
        user_id = call.from_user.id
        
        if check_membership(user_id):
            # Try to delete the "Join" message
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception as e:
                logging.warning(f"Could not delete join message: {e}")
                
            if movie_idx is not None:
                # Automate the movie selection delivery by simulating handle_selection logic
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
                
                sent_msg = bot.send_message(call.message.chat.id, final_message, parse_mode="HTML")
                threading.Thread(target=delete_message_after_delay, args=(call.message.chat.id, sent_msg.message_id, 120)).start()
                bot.answer_callback_query(call.id, "✅ Membership verified! Movie bhej di gayi hai.")
            else:
                # Re-trigger the movie search
                class FakeMessage:
                    def __init__(self, chat_id, from_user, text):
                        self.chat = type('Chat', (), {'id': chat_id})
                        self.from_user = from_user
                        self.text = text
                        self.message_id = call.message.message_id
                
                fake_msg = FakeMessage(call.message.chat.id, call.from_user, original_query)
                handle_message(fake_msg)
                bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "❌ Aapne abhi tak channel join nahi kiya hai!", show_alert=True)
    except Exception as e:
        logging.error(f"Verification callback error: {e}")
        bot.answer_callback_query(call.id, "Error processing verification.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_"))
def handle_selection(call):
    try:
        parts = call.data.split("_")
        query_id = parts[1]
        movie_idx = int(parts[-1])
        
        # Force Join Check BEFORE showing the final link
        if not check_membership(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Aapko channel join karna hoga link dekhne ke liye!", show_alert=True)
            
            header = "❌ <b>Access Denied!</b>\n\nAapko hamara channel join karna hoga movie links dekhne ke liye."
            # Store the specific movie index in callback_data so refresh can deliver it directly
            markup = InlineKeyboardMarkup()
            channel_url = f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}"
            markup.add(InlineKeyboardButton("📢 Join Channel", url=channel_url))
            markup.add(InlineKeyboardButton("🔄 Refresh / Verify", callback_data=f"verify_{query_id}_{movie_idx}"))
            
            # Send NEW message instead of editing to avoid "message to edit not found" errors
            bot.send_message(call.message.chat.id, header, reply_markup=markup, parse_mode="HTML")
            return

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
# SPEECH TO TEXT + SUBTITLE
# =========================
MAX_VIDEO_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB
pending_videos = {}  # key -> {file_id, file_name, chat_id, message_id}

SUBTITLE_LANGUAGES = {
    "en":       ("🇬🇧 English", "en"),
    "hi":       ("🇮🇳 Hindi", "hi"),
    "hinglish": ("🔀 Hinglish", None),  # None = whisper auto-detect
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

def process_video_subtitles(bot, message, file_id, file_name, language=None, lang_label="Auto"):
    chat_id = message.chat.id
    status_msg = bot.send_message(chat_id, f"⏳ Processing shuru... Language: <b>{lang_label}</b>\n(Thoda waqt lagega)", parse_mode="HTML")

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

        bot.edit_message_text("🎙️ Audio extract ho raha hai...", chat_id, status_msg.message_id)

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
        bot.edit_message_text(f"🧠 Whisper ({model_name} model) se speech-to-text ho raha hai...\n⏳ 1-4 min lag sakte hain", chat_id, status_msg.message_id)
        model = whisper.load_model(model_name)
        transcribe_kwargs = {
            "fp16": False,
            "beam_size": 5,
            "best_of": 5,
            "temperature": 0,
            "condition_on_previous_text": False,
            "task": "transcribe",
        }
        if language:
            transcribe_kwargs["language"] = language
        result = model.transcribe(audio_path, **transcribe_kwargs)

        if not result.get("segments"):
            bot.edit_message_text("❌ Video mein koi speech nahi mili.", chat_id, status_msg.message_id)
            return

        # Gemini correction step (Hindi/Hinglish ke liye especially useful)
        bot.edit_message_text("✨ Gemini se text correct ho raha hai...", chat_id, status_msg.message_id)
        corrected_segments = gemini_correct_segments(result["segments"], lang_label)

        # Generate ASS (with fade in/out animation)
        ass_content = generate_ass(corrected_segments)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        bot.edit_message_text("🎬 Subtitles video mein add ho rahi hain...", chat_id, status_msg.message_id)

        # Burn subtitles into video — ASS format supports fade + Devanagari font
        fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        subtitle_filter = f"ass={ass_path}:fontsdir={fonts_dir}"
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", subtitle_filter,
            "-c:a", "copy",
            output_path
        ], check=True, capture_output=True)

        bot.edit_message_text("📤 Video bhej raha hoon...", chat_id, status_msg.message_id)

        # Send back the subtitled video
        with open(output_path, 'rb') as vid:
            bot.send_video(
                chat_id,
                vid,
                caption=f"✅ Subtitles add ho gayi! ✨ Gemini corrected\n\n📝 <b>Transcript preview:</b>\n<i>{' '.join(s['text'] for s in corrected_segments)[:300].strip()}{'...' if len(' '.join(s['text'] for s in corrected_segments)) > 300 else ''}</i>",
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

    # Store video info and ask language
    vid_key = f"{message.chat.id}_{message.message_id}"
    pending_videos[vid_key] = {
        "file_id": file_id,
        "file_name": file_name,
        "chat_id": message.chat.id,
        "message": message,
    }

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🇬🇧 English", callback_data=f"sublang_{vid_key}_en"),
        InlineKeyboardButton("🇮🇳 Hindi", callback_data=f"sublang_{vid_key}_hi"),
    )
    markup.row(
        InlineKeyboardButton("🔀 Hinglish (Auto)", callback_data=f"sublang_{vid_key}_hinglish"),
    )

    bot.reply_to(
        message,
        "🎬 <b>Video mili!</b>\n\nSubtitles kis language mein chahiye?",
        reply_markup=markup,
        parse_mode="HTML"
    )

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
        video_data = pending_videos.pop(vid_key)

        bot.answer_callback_query(call.id, f"✅ {lang_label} select ki! Processing shuru...")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        threading.Thread(
            target=process_video_subtitles,
            args=(bot, video_data["message"], video_data["file_id"], video_data["file_name"]),
            kwargs={"language": whisper_lang, "lang_label": lang_label}
        ).start()

    except Exception as e:
        logging.error(f"Lang selection callback error: {e}")
        bot.answer_callback_query(call.id, "❌ Error processing selection.")

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
