import os
import random
import logging
import datetime
import asyncio
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
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
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Premium plan details
PREMIUM_PLANS = {
    '1_week': 20,
    '1_month': 50,
    'lifetime': 149
}

# Store video file IDs (in production, use a database)
video_storage = []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += "This is a Telegram bot that can send random videos from a group or channel\\.\n"
    welcome_message += "Use the buttons below to get a random video, access the premium plan, or upload your own videos\\."

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
        
        # Initialize user data if not exists
        if 'users' not in context.bot_data:
            context.bot_data['users'] = {}
        
        if user_id not in context.bot_data['users']:
            context.bot_data['users'][user_id] = {
                'daily_count': 0,
                'last_reset': datetime.date.today(),
                'premium': False,
                'expiry': None
            }
        
        user_data = context.bot_data['users'][user_id]
        
        # Reset daily count if it's a new day
        if user_data['last_reset'] != datetime.date.today():
            user_data['daily_count'] = 0
            user_data['last_reset'] = datetime.date.today()
        
        # Check if user has premium or is within daily limit
        is_premium = user_data.get('premium', False)
        if user_data.get('expiry') and datetime.datetime.now() > user_data['expiry']:
            is_premium = False
            user_data['premium'] = False
        
        if not is_premium and user_data['daily_count'] >= DAILY_LIMIT:
            await query.edit_message_text(text="You have reached your daily limit. Please upgrade to a premium plan to continue.")
            return

        # Get videos from storage or transfer channel
        available_videos = video_storage.copy()
        
        if not available_videos:
            await query.edit_message_text(text="No videos available at the moment.")
            return

        try:
            random_video = random.choice(available_videos)
            await query.edit_message_text(text="Here is your random video:")
            message = await context.bot.send_video(chat_id=query.message.chat_id, video=random_video)
            
            # Schedule message deletion after 5 minutes
            context.job_queue.run_once(
                delete_message, 
                300, 
                data={'chat_id': query.message.chat_id, 'message_id': message.message_id}
            )

            if not is_premium:
                user_data['daily_count'] += 1
                
        except Exception as e:
            logger.error(f"Error sending video: {e}")
            await query.edit_message_text(text="Sorry, there was an error sending the video.")

    elif query.data == 'premium_plan':
        premium_message = "Choose your premium plan:\n"
        premium_message += "• 1 Week - ₹20\n"
        premium_message += "• 1 Month - ₹50\n"
        premium_message += "• Lifetime - ₹149\n\n"
        premium_message += "Premium benefits:\n"
        premium_message += "- Unlimited video downloads\n"
        premium_message += "- Access to exclusive content\n"
        premium_message += "- No ads"

        keyboard = [
            [InlineKeyboardButton("1 Week - ₹20", callback_data='plan_1_week')],
            [InlineKeyboardButton("1 Month - ₹50", callback_data='plan_1_month')],
            [InlineKeyboardButton("Lifetime - ₹149", callback_data='plan_lifetime')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=premium_message, reply_markup=reply_markup)

    elif query.data.startswith('plan_'):
        plan_type = query.data.replace('plan_', '')
        price = PREMIUM_PLANS[plan_type]
        user_id = query.from_user.id
        
        if 'users' not in context.bot_data:
            context.bot_data['users'] = {}
        
        if user_id not in context.bot_data['users']:
            context.bot_data['users'][user_id] = {}

        # In a real implementation, integrate with a payment gateway here
        # For demo purposes, we'll simulate successful payment
        
        if plan_type == 'lifetime':
            context.bot_data['users'][user_id]['premium'] = True
            context.bot_data['users'][user_id]['expiry'] = None
        else:
            days = 7 if plan_type == '1_week' else 30
            context.bot_data['users'][user_id]['premium'] = True
            context.bot_data['users'][user_id]['expiry'] = datetime.datetime.now() + datetime.timedelta(days=days)

        # In production, you would add user to premium channel here
        # await context.bot.add_chat_member(chat_id=PREMIUM_CHANNEL, user_id=user_id)
        
        await query.edit_message_text(
            text=f"✅ Payment simulation successful!\n\nYou have subscribed to the {plan_type.replace('_', ' ').title()} plan for ₹{price}.\n\n"
                 f"Note: In production, this would integrate with a real payment gateway."
        )

    elif query.data == 'upload_video':
        await query.edit_message_text(text="Please send me the video you want to upload.")

    elif query.data == 'trending_videos':
        trending_videos = []
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_videos = [line.strip() for line in file.readlines() if line.strip()]

        if trending_videos:
            await query.edit_message_text(text="Here are the trending videos:")
            for video_id in trending_videos[:5]:  # Limit to 5 trending videos
                try:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=video_id)
                except TelegramError as e:
                    logger.error(f"Error sending trending video {video_id}: {e}")
        else:
            await query.edit_message_text(text="No trending videos available at the moment.")

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    
    if 'users' not in context.bot_data:
        context.bot_data['users'] = {}
    
    if user_id not in context.bot_data['users']:
        context.bot_data['users'][user_id] = {'uploaded_videos': 0}
    
    user_data = context.bot_data['users'][user_id]

    video = update.message.video
    if video:
        # Store video file ID
        video_storage.append(video.file_id)
        
        # Forward to transfer channel if configured
        if TRANSFER_CHANNEL:
            try:
                await context.bot.send_video(chat_id=TRANSFER_CHANNEL, video=video.file_id)
            except TelegramError as e:
                logger.error(f"Error forwarding video to transfer channel: {e}")
        
        user_data['uploaded_videos'] = user_data.get('uploaded_videos', 0) + 1

        # Reward user with premium for uploading 10 videos
        if user_data['uploaded_videos'] >= 10 and not user_data.get('premium'):
            user_data['premium'] = True
            user_data['expiry'] = datetime.datetime.now() + datetime.timedelta(days=7)
            
            # In production, add to premium channel
            # await context.bot.add_chat_member(chat_id=PREMIUM_CHANNEL, user_id=user_id)
            
            await update.message.reply_text(
                "🎉 Congratulations! You have uploaded 10 videos and activated a 1-week premium plan!"
            )

        await update.message.reply_text(
            f"✅ Video uploaded successfully!\nTotal videos uploaded: {user_data['uploaded_videos']}"
        )
    else:
        await update.message.reply_text("Please send a valid video file.")

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    try:
        await context.bot.delete_message(chat_id=job_data['chat_id'], message_id=job_data['message_id'])
    except TelegramError as e:
        logger.error(f"Error deleting message: {e}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message:
        await update.message.reply_text("Please reply to a message to broadcast.")
        return

    # In production, get all premium users from database
    premium_users = [
        user_id for user_id, data in context.bot_data.get('users', {}).items() 
        if data.get('premium', False)
    ]
    
    if not premium_users:
        await update.message.reply_text("No premium users found.")
        return

    success_count = 0
    failed_count = 0

    for user_id in premium_users:
        try:
            if message.photo:
                await context.bot.send_photo(
                    chat_id=user_id, 
                    photo=message.photo[-1].file_id, 
                    caption=message.caption
                )
            elif message.video:
                await context.bot.send_video(
                    chat_id=user_id, 
                    video=message.video.file_id, 
                    caption=message.caption
                )
            else:
                await context.bot.send_message(chat_id=user_id, text=message.text)
            
            success_count += 1
        except TelegramError as e:
            logger.error(f"Error broadcasting to user {user_id}: {e}")
            failed_count += 1
        
        # Small delay to avoid rate limiting
        await asyncio.sleep(0.1)

    await update.message.reply_text(
        f"📡 Broadcast completed!\n✅ Successful: {success_count}\n❌ Failed: {failed_count}"
    )

async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message or not message.video:
        await update.message.reply_text("Please reply to a video message to mark it as trending.")
        return

    # Add to trending videos file
    try:
        with open('trending_videos.txt', 'a') as file:
            file.write(f"{message.video.file_id}\n")
        
        await update.message.reply_text("✅ Video marked as trending.")
    except Exception as e:
        logger.error(f"Error marking video as trending: {e}")
        await update.message.reply_text("❌ Error marking video as trending.")

def main() -> None:
    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    
    application = Application.builder().token(API_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))  # Fixed: filters.VIDEO instead of filters.Video
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("trending", trending))

    # Run the bot in polling mode (works without webhook dependencies)
    logger.info("Starting bot in polling mode...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
