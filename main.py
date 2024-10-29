import logging
import asyncio

import nest_asyncio
import openai
from telegram import Update, InputMediaPhoto, ChatMember, ChatPermissions
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler
import re
from telegram.error import RetryAfter
import sqlite3

# Apply nest_asyncio to allow nested async calls
nest_asyncio.apply()

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s', level=logging.INFO)

# Set your OpenAI API key

GROUP_CHAT_ID = -1002329036187
user_scores = {}
message_scores = []  # Store recent message scores
message_count = 0  # Count of processed messages


#EVERYTHING FOR DATABASE
def init_db():
    with sqlite3.connect('user_scores.db') as conn:
        conn.execute('''
       CREATE TABLE IF NOT EXISTS user_scores (
           user_id INTEGER PRIMARY KEY,
           score REAL NOT NULL
       )
       ''')
        conn.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    user_id INTEGER,
                    first_name TEXT,
                    username TEXT,
                    feedback_message TEXT
                )
                ''')
    conn.commit()
def update_user_score(user_id, score):
    with sqlite3.connect('user_scores.db') as conn:
        conn.execute('''
        INSERT INTO user_scores (user_id, score) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET score = score + excluded.score   
        ''', (user_id, score))
        conn.commit()
def get_user_score(user_id):
    with sqlite3.connect('user_scores.db') as conn:
        result = conn.execute('SELECT score FROM user_scores WHERE user_id = ?', (user_id,)).fetchone()
    return result[0] if result else 0

def get_leaderboard():
    with sqlite3.connect('user_scores.db') as conn:
        leaderboard = conn.execute('''
        SELECT user_id, score FROM user_scores
        ORDER BY score DESC
        LIMIT 10
        ''').fetchall()
    return leaderboard
def store_feedback(user_id, first_name, username, feedback_message):
    with sqlite3.connect('user_scores.db') as conn:
        conn.execute('''
        INSERT INTO feedback (user_id, first_name, username, feedback_message)
        VALUES (?, ?, ?, ?)
        ''', (user_id, first_name, username, feedback_message))
    conn.commit()


async def retry_after_handling(func, *args, **kwargs):
    while True:
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            delay = e.retry_after
            logging.warning(f"Flood control exceeded. Retrying in {delay} seconds.")
            await asyncio.sleep(delay)

# Function to handle /leaderboard command
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return

    leaderboard = get_leaderboard()
    if not leaderboard:
        await retry_after_handling(update.message.reply_text, "No scores available yet.")
        return

    # Format the leaderboard message
    leaderboard_text = "🏆 <b>**Leaderboard Positivity**</b>🏆\n\n"
    for idx, (user_id, score) in enumerate(leaderboard, start=1):
        # Fetch the user info to get the username
        user = await context.bot.get_chat(user_id)
        username = user.username if user.username else user.first_name  # Fallback to first name if no username

        user_mention = f'<a href="tg://user?id={user_id}">{username}</a>'
        leaderboard_text += f"{idx}. {user_mention}: {score:.2f}\n"

    await retry_after_handling(update.message.reply_text, leaderboard_text, parse_mode='HTML')


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for new_member in update.message.new_chat_members:
            user_name = new_member.first_name
            user_id = new_member.id
            new_member_mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'

            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system",
                     "content": f"You are a welcoming host. Generate a warm and friendly welcome message for a Telegram Tarsier memecoin group in max 30 words without hashtags. "}
                ],
                max_tokens=100  # Increased token limit for a longer message
            )
            welcome_text = response['choices'][0]['message']['content']
            welcome_mention = f"{new_member_mention}"

            # Custom introduction message
            introduction_message = (
                "I'm TarsierMood, and I analyze every message you send to gauge the mood of the group. "
                "Feel free to share your thoughts, and let's keep the positivity flowing!"
            )

            await update.message.reply_text(
                parse_mode='html',
                text=f"{welcome_mention}, {welcome_text}\n\n{introduction_message}"
            )

    except Exception as e:
        logging.error(f"Error fetching welcome message: {e}")
        await update.message.reply_text(
            "Welcome to the Tarsier Memecoin group! I'm here to analyze your messages and keep the vibe positive. We're glad to have you here."
        )

async def get_sentiment_and_score(user_message):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "Analyze the sentiment of the following message in the crypto world and provide a score from -1 (very negative) to 1 (very positive) and give a short explanation in []:"
                },
                {"role": "user", "content": user_message}
            ],
            max_tokens=50
        )
        sentiment_analysis = response['choices'][0]['message']['content']
        score=parse_score(sentiment_analysis)
        explanation = get_sentiment_explanation(sentiment_analysis)
        return score, explanation
    except Exception as e:
        logging.error(f"Error fetching sentiment score: {e}")
        return 0  # Neutral fallback if API fails

def parse_score(sentiment_analysis):
    score_match = re.search(r'(-?\d+(?:\.\d+)?)', sentiment_analysis)
    if score_match:
        return float(score_match.group(1))
    else:
        logging.warning("Failed to parse score; defaulting to neutral")
        return 0

def get_sentiment_explanation(sentiment_analysis):
    # Assuming the sentiment_analysis is in the format: "Score: X (Explanation)"
    try:
        # Split the string by '(' and ')' to extract the explanation
        explanation_start = sentiment_analysis.index('[') + 1
        explanation_end = sentiment_analysis.index(']')
        explanation = sentiment_analysis[explanation_start:explanation_end].strip()
        return explanation
    except ValueError:
        return "No explanation provided."  # Fallback if parsing fails

async def analyze_sentiment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global message_count
    global message_scores

    logging.info(f"Received message from {update.message.from_user.username}: {update.message.text}")

    if update.message.chat.id != GROUP_CHAT_ID:
        logging.info("Ignoring message from outside the specified group chat.")
        return

    user_id = update.message.from_user.id
    user_message = update.message.text

    score, explanation = await get_sentiment_and_score(user_message)

    logging.info(f"Sentiment score for {user_id}: {score}")

    # Update user score in the database
    update_user_score(user_id, score)

    # Retrieve the user's score from the database
    user_score = get_user_score(user_id)

    message_scores.append(score)
    message_count += 1

    if message_count >= 5:
        avg_score = sum(message_scores) / len(message_scores) if message_scores else 0
        average_text = f"Processed {message_count} messages. Overall average positivity: {avg_score:.2f}"

        # Send the average positivity message to the group
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=average_text)
        message_count = 0
        message_scores = []

    response = ""

    if score < -0.5:
        motivational_quote = await get_ai_quote()
        response = (f"Hey man, you are too negative.\n{explanation} Your score is now {user_score:.2f}.\n\n "
                    f"Here's a motivational quote for you:\n<b>{motivational_quote}</b>")

    if user_score < -4:
        try:
            await context.bot.restrict_chat_member(
                chat_id=update.message.chat.id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            response += "\n\n🚨<b>You've been muted for negativity. Try to be more positive!</b>🚨"
        except Exception as e:
            logging.error(f"Error muting user {user_id}: {e}")
            response += "\nFailed to mute the user."
    if response:  # Check if response is not empty before sending
        await retry_after_handling(update.message.reply_text, response, parse_mode='html')

async def get_ai_quote():
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a motivational coach. Generate a motivational quote for crypto called Tarsier, without hashtags!"}
            ],
            max_tokens=50
        )
        return response['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"Error fetching quote: {e}")
        return "Stay positive and keep pushing forward!"

async def generate_dalle_image(prompt):
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        return image_url
    except Exception as e:
        logging.error(f"Error generating image: {e}")
        return None

# /score
async def check_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return

    user_id = update.message.from_user.id
    score = get_user_score(user_id)  # Get score from database
    await retry_after_handling(update.message.reply_text, f"Your current positivity score is: {score:.2f}")

# /meme
async def meme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return  # Ignore messages from other chats

    # Send initial message and store it
    message = await retry_after_handling(update.message.reply_text, "Photo is being generated...")

    # Generate image
    image_url = await generate_dalle_image(
        "A cute tarsier with big eyes in a warm, magical forest. Generally realistic, with subtle, glowing plants adding a touch of mystical")

    if image_url:
        # Edit the message text to "Your photo 😀"
        await retry_after_handling(message.edit_text, "Your photo 😀")

        # Send the generated image as a reply to the edited message
        await retry_after_handling(context.bot.send_photo, chat_id=update.message.chat.id, photo=image_url,
                                   reply_to_message_id=message.message_id)
    else:
        # If image generation fails, edit the message to apologize
        await retry_after_handling(message.edit_text, "Sorry, I couldn't generate an image right now.")

# /ask
async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = ' '.join(context.args)
    if not user_message:
        await retry_after_handling(update.message.reply_text, "Please ask a question.")
        return

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": f"Tarsier, answer this question: {user_message}"}
            ],
            max_tokens=100
        )
        answer = response['choices'][0]['message']['content']
        await retry_after_handling(update.message.reply_text, answer)
    except Exception as e:
        logging.error(f"Error fetching answer for question '{user_message}': {e}")
        await retry_after_handling(update.message.reply_text, "Sorry, I couldn't get an answer for that.")
async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    first_name = update.message.from_user.first_name
    username = update.message.from_user.username
    feedback_message = ' '.join(context.args)

    if not feedback_message:
        await retry_after_handling(update.message.reply_text, "Please provide your feedback after the command.")
        return

    store_feedback(user_id, first_name, username, feedback_message)
    await retry_after_handling(update.message.reply_text, "Thank you for your feedback!")
# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await retry_after_handling(update.message.reply_text,
                                   "Hello! I'm TarsierMood. Share your thoughts, and I'll analyze your mood!")

async def main():
    init_db()
    app = ApplicationBuilder().token("8083231744:AAHqYR21xBYEQ838ke1dlRl4WOm3GhV-qkI").build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("meme", meme_command))
    app.add_handler(CommandHandler("score", check_score))
    app.add_handler(CommandHandler("ask", ask_command))
    new_member_handler = MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    app.add_handler(new_member_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_sentiment))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("feedback", feedback_command))

    logging.info("Bot is polling...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
