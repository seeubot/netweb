# bot.py
import os
import random
import logging
import datetime
import asyncio
import string
from typing import Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')  # Channel to fetch videos from
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
MONGO_URI = "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie"
DB_NAME = "telegram_bot_db"
BASE_URL = os.getenv('BASE_URL', 'https://negative-sissy-seeutech-7924c707.koyeb.app')  # For share links

# Webhook configuration for Koyeb
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
LISTEN_ADDRESS = '0.0.0.0'

# Global database client and collections
db_client = None
db = None
users_collection = None
videos_collection = None
shares_collection = None

async def connect_to_mongodb():
    """Connects to MongoDB and sets up global collections."""
    global db_client, db, users_collection, videos_collection, shares_collection
    try:
        db_client = AsyncIOMotorClient(MONGO_URI)
        # Test the connection
        await db_client.admin.command('ping')
        db = db_client[DB_NAME]
        users_collection = db['users']
        videos_collection = db['videos']
        shares_collection = db['shares']
        logger.info("Successfully connected to MongoDB.")
        return True
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return False

async def generate_share_token(file_id: str) -> str:
    """Generates a unique share token for a video file."""
    # Generate random token
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    
    # Store in database with expiration (30 days)
    expires_at = datetime.datetime.now() + datetime.timedelta(days=30)
    await shares_collection.insert_one({
        'token': token,
        'file_id': file_id,
        'created_at': datetime.datetime.now(),
        'expires_at': expires_at
    })
    
    return token

async def generate_share_url(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Generates a share URL for a video file."""
    token = await generate_share_token(file_id)
    return f"{BASE_URL}/share/{token}"

async def handle_shared_video(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    """Handles a shared video link."""
    try:
        # Find the share record
        share_doc = await shares_collection.find_one({'token': token})
        
        if not share_doc:
            await update.message.reply_text("❌ This share link is invalid or has expired.")
            return
            
        if share_doc['expires_at'] < datetime.datetime.now():
            await update.message.reply_text("❌ This share link has expired.")
            return
            
        # Get the video
        video_doc = await videos_collection.find_one({'file_id': share_doc['file_id']})
        
        if not video_doc:
            await update.message.reply_text("❌ The video in this link is no longer available.")
            return
            
        # Send the video
        await update.message.reply_text("Here's the shared video:")
        await context.bot.send_video(
            chat_id=update.message.chat_id,
            video=video_doc['file_id'],
            protect_content=True
        )
        
    except Exception as e:
        logger.error(f"Error handling shared video: {e}")
        await update.message.reply_text("❌ An error occurred while processing the shared video.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and handles shared video links."""
    # Check if this is a shared video link
    if context.args and len(context.args) > 0:
        if context.args[0].startswith('video_'):
            file_id = context.args[0][6:]  # Extract the file_id
            await handle_shared_video(update, context, file_id)
            return
        elif context.args[0].startswith('share_'):
            token = context.args[0][6:]  # Extract the token
            await handle_shared_video(update, context, token)
            return
    
    # Original start function
    user = update.effective_user
    user_id = user.id
    
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot sends random videos from our collection\\.\n"
    welcome_message += "Use the buttons below to get videos or upload new ones\\."

    keyboard = [
        [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
        [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
        [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
    ]
    
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    if query.data == 'get_video':
        user_id = query.from_user.id
        
        try:
            user_doc = await users_collection.find_one({'user_id': user_id})
            
            if not user_doc:
                user_doc = {
                    'user_id': user_id,
                    'daily_count': 0,
                    'last_reset': datetime.date.today().isoformat(),
                    'uploaded_videos': 0
                }
                await users_collection.insert_one(user_doc)
            
            # Reset daily count if it's a new day
            if user_doc['last_reset'] != datetime.date.today().isoformat():
                await users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'daily_count': 0, 'last_reset': datetime.date.today().isoformat()}}
                )
                user_doc['daily_count'] = 0
                user_doc['last_reset'] = datetime.date.today().isoformat()
            
            # Check daily limit
            if user_doc['daily_count'] >= DAILY_LIMIT:
                await query.edit_message_text(
                    text=f"⏰ You have reached your daily limit of {DAILY_LIMIT} videos.\n"
                         f"Please try again tomorrow!"
                )
                return

            # Get all videos from collection
            all_videos = []
            async for doc in videos_collection.find({}):
                all_videos.append(doc['file_id'])
            
            if not all_videos:
                await query.edit_message_text(text="📹 No videos available at the moment.\nPlease upload some videos first!")
                return

            # Send random video
            random_video_id = random.choice(all_videos)
            await query.edit_message_text(text="📹 Here is your random video:")
            
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id, 
                video=random_video_id,
                protect_content=True
            )
            
            # Schedule message deletion after 5 minutes
            context.job_queue.run_once(
                delete_message,
                300,
                data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
            )

            # Generate share URL
            share_url = await generate_share_url(random_video_id, context)
            
            # Create share keyboard
            share_keyboard = [
                [InlineKeyboardButton("🔗 Share This Video", url=share_url)],
                [InlineKeyboardButton("📤 Get Another Video", callback_data='get_video')]
            ]
            reply_markup = InlineKeyboardMarkup(share_keyboard)
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🔗 Share this video with others:",
                reply_markup=reply_markup
            )

            # Update user's daily count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'daily_count': 1}}
            )
            
            remaining = DAILY_LIMIT - (user_doc['daily_count'] + 1)
            if remaining > 0:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"✅ Video sent! You have {remaining} videos left today."
                )
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="✅ Video sent! You've reached your daily limit. See you tomorrow!"
                )
                
        except Exception as e:
            logger.error(f"Error in get_video: {e}")
            await query.edit_message_text(text="❌ Sorry, there was an error processing your request.")

    elif query.data == 'trending_videos':
        try:
            trending_videos = []
            async for doc in videos_collection.find({'is_trending': True}):
                trending_videos.append(doc['file_id'])
            
            if trending_videos:
                await query.edit_message_text(text="🔥 Here are the trending videos:")
                for video_id in trending_videos[:3]:  # Limit to 3 trending videos
                    try:
                        sent_message = await context.bot.send_video(
                            chat_id=query.message.chat_id, 
                            video=video_id,
                            protect_content=True
                        )
                        
                        context.job_queue.run_once(
                            delete_message,
                            300,
                            data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
                        )
                        
                        # Generate share URL for trending video
                        share_url = await generate_share_url(video_id, context)
                        share_keyboard = [
                            [InlineKeyboardButton("🔗 Share This Video", url=share_url)]
                        ]
                        reply_markup = InlineKeyboardMarkup(share_keyboard)
                        
                        await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text="🔗 Share this trending video:",
                            reply_markup=reply_markup
                        )
                        
                    except TelegramError as e:
                        logger.error(f"Error sending trending video {video_id}: {e}")
            else:
                await query.edit_message_text(text="📹 No trending videos available at the moment.")
        except Exception as e:
            logger.error(f"Error in trending_videos: {e}")
            await query.edit_message_text(text="❌ Error loading trending videos.")

    # ... rest of the button function remains the same ...

# ... rest of the file remains the same ...

def main() -> None:
    """Starts the bot and sets up all handlers."""
    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found in environment variables. Webhook deployment requires this.")
        return
    
    # Create application with updated builder pattern
    application = (
        Application.builder()
        .token(API_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel_operation))
    
    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button))
    
    # Add message handlers
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Add a periodic cleanup job for expired shares
    if application.job_queue:
        application.job_queue.run_repeating(
            cleanup_expired_shares,
            interval=86400,  # Every 24 hours
            first=10
        )
    
    logger.info(f"Starting bot in webhook mode on {LISTEN_ADDRESS}:{PORT}...")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")

    try:
        # Start webhook with error handling
        application.run_webhook(
            listen=LISTEN_ADDRESS,
            port=PORT,
            url_path="",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Error starting webhook: {e}")
        raise

async def cleanup_expired_shares(context: ContextTypes.DEFAULT_TYPE):
    """Cleans up expired share links from the database."""
    try:
        result = await shares_collection.delete_many({
            'expires_at': {'$lt': datetime.datetime.now()}
        })
        logger.info(f"Cleaned up {result.deleted_count} expired share links")
    except Exception as e:
        logger.error(f"Error cleaning up expired shares: {e}")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
