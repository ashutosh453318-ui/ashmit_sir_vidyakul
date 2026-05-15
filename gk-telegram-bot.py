import logging
import re
import sqlite3
import asyncio
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, Poll, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    PollAnswerHandler,
)
from telegram.request import HTTPXRequest

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
TOKEN = "8928907436:AAGRk3kWTB_4AHHimsHWr_9dVPdzlD0k_Qs"
OWNER_ID = 6527942155
GYANENDRA_SIR_USERNAME = "ANISH2333" # Bina @ ke likhna hai

# Group 2 Invite Link aur Passing Marks
PASSING_MARKS = 70
# YAHAN APNE DOOSRE GROUP KA LINK DAALNA MAT BHULNA!


# Spam Words
BANNED_WORDS = ["scam", "fraud", "casino", "illegal", "bitcoin", "gali", "badword1", "badword2", "badword3", "join fast", "investment"]

# --- QUIZ FOLDERS & FILES HIERARCHY ---
QUIZ_STRUCTURE = {
    "Math": {
        "Test": "quiz.txt",
    }
}

ACTIVE_POLLS = {}
QUIZ_TASKS = {} 
COMPETITION_STATS = {} 

# --- DUMMY WEB SERVER FOR RENDER ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive and running on Render!")

    def log_message(self, format, *args):
        pass

def run_dummy_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server_address = ('0.0.0.0', port)
        httpd = HTTPServer(server_address, DummyHandler)
        logger.info(f"Starting dummy web server on port {port} to satisfy Render health checks...")
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Dummy Server Error: {e}")

# --- DATABASE SETUP ---
def get_db_connection():
    return sqlite3.connect("quiz_scores.db", timeout=10)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER,
            user_id INTEGER,
            full_name TEXT,
            points INTEGER DEFAULT 0,
            last_time REAL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    try: cursor.execute("ALTER TABLE scores ADD COLUMN last_time REAL DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE scores ADD COLUMN correct_answers INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE scores ADD COLUMN wrong_answers INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE scores ADD COLUMN total_duration REAL DEFAULT 0.0")
    except sqlite3.OperationalError: pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_state (
            chat_id INTEGER PRIMARY KEY,
            current_index INTEGER DEFAULT 0,
            subject TEXT DEFAULT 'quiz.txt'
        )
    """)
    conn.commit()
    conn.close()

def get_quiz_state(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT current_index, subject FROM quiz_state WHERE chat_id = ?", (chat_id,))
    result = cursor.fetchone()
    conn.close()
    if result: return result[0], result[1]
    return 0, 'quiz.txt'

def update_quiz_state(chat_id, new_index, subject=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if subject is None:
        _, subject = get_quiz_state(chat_id)
    cursor.execute("""
        INSERT INTO quiz_state (chat_id, current_index, subject) 
        VALUES (?, ?, ?) 
        ON CONFLICT(chat_id) DO UPDATE SET current_index = ?, subject = ?
    """, (chat_id, new_index, subject, new_index, subject))
    conn.commit()
    conn.close()

def reset_scores(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scores WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def record_answer(chat_id, user_id, full_name, is_correct, duration):
    conn = get_db_connection()
    cursor = conn.cursor()
    current_time = time.time()
    
    cursor.execute("SELECT points, correct_answers, wrong_answers, total_duration FROM scores WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    data = cursor.fetchone()
    
    # RULE: 2 marks for correct, 0 for wrong (no negative marking)
    points_to_add = 2 if is_correct else 0
    corr_add = 1 if is_correct else 0
    wrong_add = 0 if is_correct else 1
    
    if data:
        new_points = (data[0] or 0) + points_to_add
        new_corr = (data[1] or 0) + corr_add
        new_wrong = (data[2] or 0) + wrong_add
        new_duration = (data[3] or 0.0) + duration
        cursor.execute("""
            UPDATE scores 
            SET points = ?, correct_answers = ?, wrong_answers = ?, total_duration = ?, full_name = ?, last_time = ? 
            WHERE chat_id = ? AND user_id = ?
        """, (new_points, new_corr, new_wrong, new_duration, full_name, current_time, chat_id, user_id))
    else:
        cursor.execute("""
            INSERT INTO scores (chat_id, user_id, full_name, points, correct_answers, wrong_answers, total_duration, last_time) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, user_id, full_name, points_to_add, corr_add, wrong_add, duration, current_time))
    conn.commit()
    conn.close()

def get_top_scorers(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, points, correct_answers, wrong_answers, total_duration, user_id FROM scores WHERE chat_id = ? ORDER BY points DESC, total_duration ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- LONG MESSAGE HANDLER (TELEGRAM LIMIT FIX - 1020 CHARS) ---
async def send_long_message(chat_id, text, context: ContextTypes.DEFAULT_TYPE, parse_mode="HTML"):
    max_length = 1020 
    if len(text) <= max_length:
        await context.bot.send_message(chat_id, text, parse_mode=parse_mode)
        return

    lines = text.split('\n')
    current_msg = ""
    
    for line in lines:
        if len(current_msg) + len(line) + 1 > max_length:
            if current_msg.strip():
                await context.bot.send_message(chat_id, current_msg, parse_mode=parse_mode)
            current_msg = line + "\n"
            await asyncio.sleep(1) 
        else:
            current_msg += line + "\n"
            
    if current_msg.strip():
        await context.bot.send_message(chat_id, current_msg, parse_mode=parse_mode)

# --- LEADERBOARD FORMATTER (ALL STUDENTS - LIST 1) ---
def generate_leaderboard_msg(chat_id, file_name, reason="Completed"):
    top_users = get_top_scorers(chat_id)
    total_asked = COMPETITION_STATS.get(chat_id, {}).get('total_asked', 0)
    
    sub_title = file_name.replace(".txt", "").replace("_", " ").upper() if file_name else "QUIZ"
    
    msg = f"🏁 <b>The quiz '{sub_title}' has finished! ({reason})</b>\n\n"
    msg += f"<i>{total_asked} questions answered (Max Marks: {total_asked * 2})</i>\n\n"
    msg += "🏆 <b>ALL STUDENTS RESULT:</b>\n\n"
    
    if top_users:
        medals = ["🥇", "🥈", "🥉"]
        for idx, row in enumerate(top_users):
            name = row[0]
            points = row[1] or 0
            correct = row[2] or 0
            wrong = row[3] or 0
            duration = row[4] or 0.0
            
            skipped = total_asked - (correct + wrong)
            if skipped < 0: skipped = 0 
            
            rank_icon = medals[idx] if idx < 3 else f"<b>{idx+1}.</b>"
            
            msg += f"{rank_icon} {name} – <b>{points} Marks</b> ({round(duration, 1)} sec)\n"
            msg += f"   ✅ Sahi: {correct} | ❌ Galat: {wrong} | ⏭️ Skipped: {skipped}\n\n"
            
    else:
        msg += "Koi participate nahi kiya. 😔"
        
    return msg

# --- SCORE ENFORCEMENT (70+ AND 70- LISTS - LIST 2 & LIST 3) ---
async def enforce_score_rules(chat_id, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.type == 'private':
            return
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return

    users = get_top_scorers(chat_id)
    
    if not users:
        return 
        
    passed_users = []
    failed_users = []
    
    for row in users:
        name = row[0]
        points = row[1] or 0
        user_id = row[5]
            
        if points >= PASSING_MARKS:
            passed_users.append((user_id, name, points))
        else:
            failed_users.append((user_id, name, points))
            
    msg_safe = "✅ <b>PASSED (70+ Marks) - Safe List:</b>\n\n"
    if passed_users:
        for uid, name, pts in passed_users:
            msg_safe += f"🔹 {name} - {pts} Marks\n"
    else:
        msg_safe += "Koi bhi pass nahi hua 😔\n"
    
    try:
        await send_long_message(chat_id, msg_safe, context)
        await asyncio.sleep(1) 
    except Exception as e:
        logger.error(f"Failed to send safe list: {e}")

    msg_fail = "❌ <b>FAILED (< 70 Marks) - Punishment List:</b>\n\n"
    if failed_users:
        for uid, name, pts in failed_users:
            msg_fail += f"🔸 {name} - {pts} Marks\n"
            
        msg_fail += f"\n⚠️ <b>SECOND CHANCE WARNING:</b>\n"
        msg_fail += f"Jo students fail hue hain, unko punishment ke roop mein niche diye gaye Group ko join karna padega (Koi auto-kick nahi hoga):\n\n"
       
    else:
        msg_fail += "Koi fail nahi hua! Sab safe hain 🎉\n"
    
    try:
        await send_long_message(chat_id, msg_fail, context)
    except Exception as e:
        logger.error(f"Failed to send punishment msg: {e}")

# --- FILE SETUP & READING ---
def create_dummy_files_if_not_exist():
    for subject, chapters in QUIZ_STRUCTURE.items():
        for chap_name, file_name in chapters.items():
            if not os.path.exists(file_name):
                with open(file_name, "w", encoding="utf-8") as f:
                    f.write(f"Sample {subject.capitalize()} {chap_name} Question? | Option A | Option B | Option C | Option D | 1\n")
                logger.info(f"Created sample file: {file_name}")

def load_questions(file_name):
    questions = []
    if not file_name or not os.path.exists(file_name):
        return []
    
    try:
        with open(file_name, "r", encoding="utf-8-sig") as f:
            for line in f:
                if not line.strip() or line.startswith("#"): continue
                parts = line.strip().split("|")
                if len(parts) >= 6:
                    q_text = parts[0].strip()
                    options = [p.strip() for p in parts[1:5]]
                    try:
                        correct_idx = int(parts[5].strip()) - 1
                        if len(options) >= 2:
                            questions.append({
                                "q": q_text,
                                "options": options,
                                "correct": correct_idx
                            })
                    except Exception as inner_e:
                        logger.warning(f"Error parsing index in line: {line}. Error: {inner_e}")
    except Exception as e:
        logger.error(f"File Error [{file_name}]: {e}")
    return questions

# --- STRICT PERMISSION CHECK ---
async def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
        
    if user.id == OWNER_ID or (user.username and user.username.lower() == GYANENDRA_SIR_USERNAME.lower()):
        return True
        
    return False

# --- MODERATION LOGIC ---
async def moderate_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.lower()
    user = update.message.from_user
    chat = update.effective_chat
    if chat.type == 'private' or user.id == OWNER_ID: return

    if re.search(r"(https?://|t\.me/|www\.|bit\.ly|\.com|\.in|\.net)", text):
        try:
            await update.message.delete()
            warning = await context.bot.send_message(chat_id=chat.id, text=f"🚫 {user.first_name}, spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
            await asyncio.sleep(1)
            await warning.delete()
        except: pass
        return

    if any(word in text for word in BANNED_WORDS):
        try:
            await update.message.delete()
            warning = await context.bot.send_message(chat_id=chat.id, text=f"⚠️ {user.first_name}, spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
            await asyncio.sleep(5)
            await warning.delete()
        except: pass

# --- CUSTOM QUIZ RUNNER ---
async def send_sequential_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    current_idx, file_name = get_quiz_state(chat_id)
    questions = load_questions(file_name)
    
    if not questions:
        await context.bot.send_message(chat_id, f"⚠️ '{file_name}' file khali hai ya galat format mein hai!")
        return False

    if current_idx >= len(questions):
        return False
    
    question_data = questions[current_idx]
    
    try:
        total_q = len(questions)
        q_num = current_idx + 1
        sub_title = file_name.replace(".txt", "").replace("_", " ").title()
        
        correct_ans_text = question_data['options'][question_data['correct']]
        explanation_text = (
            f"✅ Sahi Jawab: {correct_ans_text}\n\n"
        )
        
        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"🎯 {sub_title} Quiz {q_num}/{total_q} 🎯\n\n{question_data['q']}",
            options=question_data['options'],
            type=Poll.QUIZ,
            correct_option_id=question_data['correct'],
            explanation=explanation_text,
            is_anonymous=False,
            open_period=60 
        )
        
        ACTIVE_POLLS[message.poll.id] = {
            'correct': question_data['correct'], 
            'chat_id': chat_id,
            'sent_time': time.time()
        }
        update_quiz_state(chat_id, current_idx + 1, file_name)
        
        if chat_id not in COMPETITION_STATS:
            COMPETITION_STATS[chat_id] = {'total_asked': 0}
        COMPETITION_STATS[chat_id]['total_asked'] += 1
        
        return True
        
    except Exception as e:
        logger.error(f"Quiz Error in Chat {chat_id}: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Question bhejne mein dikkat aayi (Error: {e}). \nPoll option 100 character se chota hona chahiye.")
        update_quiz_state(chat_id, current_idx + 1, file_name) 
        return True 

async def quiz_runner_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(2) 
    reason = "50 Questions Completed! Type /more ya /resume for next." 
    try:
        for _ in range(50):
            if chat_id not in QUIZ_TASKS:
                return 
                
            is_running = await send_sequential_quiz(context, chat_id)
            if not is_running:
                reason = "All Questions Completed!"
                break
                
            await asyncio.sleep(62) 
    except asyncio.CancelledError:
        return 
    
    if chat_id in QUIZ_TASKS:
        _, file_name = get_quiz_state(chat_id)
        
        all_msg = generate_leaderboard_msg(chat_id, file_name, reason)
        await send_long_message(chat_id, all_msg, context)
        
        await enforce_score_rules(chat_id, context)
        
        del QUIZ_TASKS[chat_id]

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id
    user_id = answer.user.id
    user_name = answer.user.full_name
    selected_option = answer.option_ids[0]

    if poll_id in ACTIVE_POLLS:
        poll_info = ACTIVE_POLLS[poll_id]
        correct_option = poll_info['correct']
        chat_id = poll_info['chat_id']
        sent_time = poll_info.get('sent_time', time.time())
        
        duration = time.time() - sent_time
        if duration < 0: duration = 0.1
        
        is_correct = (selected_option == correct_option)
        record_answer(chat_id, user_id, user_name, is_correct, duration)

# --- COMMANDS ---
async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return
        
    welcome_message = (
        "👋 Hello! Main <b>Ashmit Sir</b>, aapka Math teacher, aapko Ultimate Quiz Karaoonga.\n\n"
        "Main aapko <b>Maths</b> ke chapters sikhne me madad karunga.\n\n"
        "📜 <b>This Features:</b>\n"
        "🔹 /startcomp - Start a new quiz competition\n"
        "🔹 /stop - Stop an ongoing quiz (Result show hoga)\n"
        "🔹 /resume - Ruka hua quiz wahi se aage badhayein\n"
        "🔹 /more - Agle 50 questions mangwayein\n"
        "🔹 /resetq - Question sequence reset karein\n\n"
        "Niche command pe click karein ya menu se select karein! 🚀"
    )
    try:
        await update.message.reply_text(welcome_message, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Start CMD error: {e}")

async def show_quiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return

    chat_id = update.effective_chat.id
    if chat_id in QUIZ_TASKS:
        await update.message.reply_text("⚠️ Is chat mein competition pehle se chal raha hai! Pehle /stop karein.")
        return

    keyboard = [
        [InlineKeyboardButton("📐 Maths", callback_data="subj_Math")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.message.reply_text("📚 <b>Choose a Subject to Start Quiz:</b>", reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Menu error: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    
    if not await is_authorized(update, context):
        await query.answer("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge", show_alert=True)
        return

    data = query.data
    chat_id = query.message.chat_id
    
    if data == "back_to_main":
        keyboard = [
            [InlineKeyboardButton("📐 Maths", callback_data="subj_Math")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📚 <b>Choose a Subject to Start Quiz:</b>", reply_markup=reply_markup, parse_mode="HTML")
        return

    if data.startswith("subj_"):
        subject = data.split("_")[1] 
        keyboard = []
        
        if subject in QUIZ_STRUCTURE:
            for chap_name, file_name in QUIZ_STRUCTURE[subject].items():
                keyboard.append([InlineKeyboardButton(f"📂 {chap_name}", callback_data=f"play_{file_name}")])
                
        keyboard.append([InlineKeyboardButton("🔙 Back to Subjects", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"📘 <b>{subject.capitalize()}</b> ke chapters:\nSelect a chapter to start:", reply_markup=reply_markup, parse_mode="HTML")
        return

    if data.startswith("play_"):
        file_name = data.split("play_")[1] 
        display_sub = file_name.replace(".txt", "").replace("_", " ").title()
        
        reset_scores(chat_id)
        update_quiz_state(chat_id, 0, file_name)
        COMPETITION_STATS[chat_id] = {'total_asked': 0}
        
        await query.edit_message_text(f"🚀 {display_sub} COMPETITION START! 🚀\n⚡ 50 Questions ka round\n⚡ Har 60 Second me Naya Sawal\n⚠️ Jo < 70 marks layega unhe punishment ke liye group 2 bheja jayega!\n\nTaiyar ho jao! 🏁")
        
        if chat_id in QUIZ_TASKS:
            QUIZ_TASKS[chat_id].cancel()
            
        task = asyncio.create_task(quiz_runner_task(chat_id, context))
        QUIZ_TASKS[chat_id] = task

async def reset_question_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return
    chat_id = update.effective_chat.id
    update_quiz_state(chat_id, 0)
    await update.message.reply_text("✅ Sequence reset to Question 1.")

async def more_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return
    chat_id = update.effective_chat.id
    
    if chat_id in QUIZ_TASKS:
        await update.message.reply_text("⚠️ Quiz pehle se chal raha hai!")
        return
        
    current_idx, file_name = get_quiz_state(chat_id)
    if not file_name or file_name == 'gk':
        await update.message.reply_text("⚠️ Pehle /startcomp use karke koi subject aur chapter select karein!")
        return
        
    if chat_id not in COMPETITION_STATS:
        COMPETITION_STATS[chat_id] = {'total_asked': 0}
        
    display_sub = file_name.replace(".txt", "").replace("_", " ").title()
    await update.message.reply_text(f"▶️ Quiz aage badh raha hai! Topic: {display_sub}\n⚡ Agle 50 sawal aa rahe hain!")
    
    task = asyncio.create_task(quiz_runner_task(chat_id, context))
    QUIZ_TASKS[chat_id] = task

async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bich me ruka hua quiz wahi se aage badhane ke liye"""
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return
    chat_id = update.effective_chat.id
    
    if chat_id in QUIZ_TASKS:
        await update.message.reply_text("⚠️ Quiz pehle se chal raha hai! Ise resume karne ki zaroorat nahi hai.")
        return
        
    current_idx, file_name = get_quiz_state(chat_id)
    if not file_name:
        await update.message.reply_text("⚠️ Pehle /startcomp use karke koi subject select karein!")
        return
        
    if chat_id not in COMPETITION_STATS:
        COMPETITION_STATS[chat_id] = {'total_asked': 0}
        
    display_sub = file_name.replace(".txt", "").replace("_", " ").title()
    await update.message.reply_text(f"▶️ Quiz wahi se Resume ho raha hai! Topic: {display_sub}\n⚡ Taiyar ho jao!")
    
    task = asyncio.create_task(quiz_runner_task(chat_id, context))
    QUIZ_TASKS[chat_id] = task

async def stop_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")
        return
    chat_id = update.effective_chat.id
    
    if chat_id not in QUIZ_TASKS:
        await update.message.reply_text("⚠️ Abhi koi quiz nahi chal raha.")
        return
        
    QUIZ_TASKS[chat_id].cancel()
    del QUIZ_TASKS[chat_id]
    
    _, file_name = get_quiz_state(chat_id)
    
    all_msg = generate_leaderboard_msg(chat_id, file_name, "Manually Stopped")
    await send_long_message(chat_id, all_msg, context)
    
    await enforce_score_rules(chat_id, context)

# --- GENERIC UNKNOWN COMMAND HANDLER ---
async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Koi bhi random /command agar group me bheji gayi toh ye bot turant warning de dega.
    """
    if not await is_authorized(update, context):
        await update.message.reply_text("🚫 Warning: spam mat karo ye sab bilkul tum nahi kar sakte ho only hm hi karenge")

async def setup_commands(application: Application):
    try:
        commands = [
            BotCommand("start", "Welcome message dekhein"),
            BotCommand("startcomp", "Quiz competition start karein"),
            BotCommand("stop", "Current quiz ko stop karein"),
            BotCommand("resume", "Ruka hua quiz resume karein"),
            BotCommand("more", "Agle 50 questions mangwayein"),
            BotCommand("resetq", "Question sequence reset karein")
        ]
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")

# --- MAIN RUNNER ---
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    init_db()
    create_dummy_files_if_not_exist() 
    
    logger.info("Bot Live! With Strict User Authorization.")
    
    req = HTTPXRequest(connection_pool_size=20, connect_timeout=30, read_timeout=30)
    app = Application.builder().token(TOKEN).request(req).post_init(setup_commands).build()

    # Registered Commands
    app.add_handler(CommandHandler("start", start_bot))
    app.add_handler(CommandHandler("startcomp", show_quiz_menu)) 
    app.add_handler(CommandHandler("stop", stop_now))
    app.add_handler(CommandHandler("resume", resume_quiz))
    app.add_handler(CommandHandler("more", more_quiz))
    app.add_handler(CommandHandler("resetq", reset_question_number))
    
    # Catch any other random commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))

    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_messages))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    logger.info("✅ Bot is now polling messages...")
    
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
