import os
import random
import logging
import datetime
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
PREMIUM_CHANNEL = os.getenv('PREMIUM_CHANNEL')
TRANSFER_CHANNEL = os.getenv('TRANSFER_CHANNEL')
ADMIN_ID = os.getenv('ADMIN_ID')
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Premium plan details
PREMIUM_PLANS = {
    '1_week': 20,
    '1_month': 50,
    'lifetime': 149
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome_message = f"Welcome, {user.mention_markdown_v2()}!\n\n"
    welcome_message += "This is a Telegram bot that can send random videos from a group or channel.\n"
    welcome_message += "Use the buttons below to get a random video, access the premium plan, or upload your own videos."

    keyboard = [
        [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
        [InlineKeyboardButton("Premium Plan", callback_data='premium_plan')],
        [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
        [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == 'get_video':
        user_id = query.from_user.id
        user_data = context.user_data
        if user_id not in user_data:
            user_data[user_id] = {'daily_count': 0}

        if user_data[user_id]['daily_count'] >= DAILY_LIMIT:
            await query.edit_message_text(text="You have reached your daily limit. Please upgrade to a premium plan to continue.")
            return

        # Fetch videos from the transfer channel
        videos = context.bot.get_chat(TRANSFER_CHANNEL).get_messages()
        if not videos:
            await query.edit_message_text(text="No videos available at the moment.")
            return

        random_video = random.choice(videos)
        message = await query.edit_message_text(text="Here is your random video:", video=random_video.video.file_id)
        context.job_queue.run_once(delete_message, 300, context=message.message_id)  # Delete after 5 minutes

        user_data[user_id]['daily_count'] += 1

    elif query.data == 'premium_plan':
        premium_message = "Choose your premium plan:\n"
        premium_message += "1. 1 Week - ₹20\n"
        premium_message += "2. 1 Month - ₹50\n"
        premium_message += "3. Lifetime - ₹149\n"
        premium_message += "Reply with the number corresponding to your choice."

        keyboard = [
            [InlineKeyboardButton("1 Week", callback_data='plan_1_week')],
            [InlineKeyboardButton("1 Month", callback_data='plan_1_month')],
            [InlineKeyboardButton("Lifetime", callback_data='plan_lifetime')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=premium_message, reply_markup=reply_markup)

    elif query.data.startswith('plan_'):
        plan_type = query.data.split('_')[1]
        price = PREMIUM_PLANS[plan_type]
        user_id = query.from_user.id
        user_data = context.user_data

        # Here you would typically integrate with a payment gateway to process the payment
        # For simplicity, we'll assume the payment is successful and add the user to the premium channel

        if plan_type == 'lifetime':
            user_data[user_id] = {'premium': True, 'expiry': None}
        else:
            user_data[user_id] = {'premium': True, 'expiry': datetime.datetime.now() + datetime.timedelta(days=7 if plan_type == '1_week' else 30)}

        await context.bot.add_chat_members(chat_id=PREMIUM_CHANNEL, user_id=user_id)
        await query.edit_message_text(text=f"You have successfully subscribed to the {plan_type.replace('_', ' ').capitalize()} plan for ₹{price}.")

    elif query.data == 'upload_video':
        await query.edit_message_text(text="Please send me the video you want to upload.")

    elif query.data == 'trending_videos':
        trending_videos = []
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_videos = file.readlines()

        if trending_videos:
            for video_id in trending_videos:
                video_id = video_id.strip()
                await query.edit_message_text(text="Here are the trending videos:", video=video_id)
        else:
            await query.edit_message_text(text="No trending videos available at the moment.")

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    user_data = context.user_data
    if user_id not in user_data:
        user_data[user_id] = {'uploaded_videos': 0}

    video = update.message.video
    if video:
        message = await context.bot.send_video(chat_id=TRANSFER_CHANNEL, video=video.file_id)
        user_data[user_id]['uploaded_videos'] += 1

        if user_data[user_id]['uploaded_videos'] >= 10:
            user_data[user_id] = {'premium': True, 'expiry': datetime.datetime.now() + datetime.timedelta(days=7)}
            await context.bot.add_chat_members(chat_id=PREMIUM_CHANNEL, user_id=user_id)
            await update.message.reply_text("Congratulations! You have uploaded 10 videos and activated a 1-week premium plan.")

        await update.message.reply_text("Video uploaded successfully.")
    else:
        await update.message.reply_text("Please send a valid video file.")

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    message_id = context.job.context
    await context.bot.delete_message(chat_id=context.job.chat_id, message_id=message_id)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message:
        await update.message.reply_text("Reply to a message to broadcast.")
        return

    if message.photo:
        for user in context.bot.get_chat_members(PREMIUM_CHANNEL):
            await context.bot.send_photo(chat_id=user.user.id, photo=message.photo[-1].file_id, caption=message.caption)
    elif message.video:
        for user in context.bot.get_chat_members(PREMIUM_CHANNEL):
            await context.bot.send_video(chat_id=user.user.id, video=message.video.file_id, caption=message.caption)
    else:
        await update.message.reply_text("Only images and videos can be broadcasted.")

async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message:
        await update.message.reply_text("Reply to a message to mark it as trending.")
        return

    # Mark the message as trending (you can store this in a database or a file)
    with open('trending_videos.txt', 'a') as file:
        file.write(f"{message.video.file_id}\n")

    await update.message.reply_text("Video marked as trending.")

def main() -> None:
    application = Application.builder().token(API_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.Video, upload_video))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("trending", trending))

    # Set up webhook
    application.run_webhook(listen='0.0.0.0', port=8443, url_path=API_TOKEN, webhook_url=WEBHOOK_URL, cert='cert.pem', key='key.pem')

if __name__ == '__main__':
    main()
