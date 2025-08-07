import os
import random
import logging
import datetime
import asyncio
from typing import Dict, Any, Optional, List
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Configuration
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
MONGO_URI = "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie"
DB_NAME = "telegram_bot_db"

# Webhook settings
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
HOST = '0.0.0.0'

# Global variables
db_client: Optional[AsyncIOMotorClient] = None
users_collection = None
videos_collection = None

async def init_database():
    """Initialize database connection."""
    global db_client, users_collection, videos_collection
    
    try:
        db_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        await db_client.admin.command('ping')
        
        db = db_client[DB_NAME]
        users_collection = db['users']
        videos_collection = db['videos']
        
        logger.info("✅ Connected to MongoDB successfully")
        return True
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return False

class TelegramBot:
    def __init__(self):
        self.app = None
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        user = update.effective_user
        user_id = user.id
        
        welcome_text = (
            f"🎬 *Welcome {user.first_name}\\!*\n\n"
            f"📋 Your ID: `{user_id}`\n\n"
            f"🎥 Get random videos from our collection\\!\n"
            f"📤 Upload your own videos\\!\n"
            f"🔥 Check trending videos\\!"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎲 Random Video", callback_data='get_video')],
            [InlineKeyboardButton("📤 Upload Video", callback_data='upload_video')],
            [InlineKeyboardButton("🔥 Trending", callback_data='trending_videos')]
        ]
        
        if ADMIN_ID and user_id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_text, 
            reply_markup=reply_markup, 
            parse_mode=ParseMode.MARKDOWN_V2
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries from inline keyboards."""
        query = update.callback_query
        await query.answer()
        
        try:
            if query.data == 'get_video':
                await self._handle_get_video(query, context)
            elif query.data == 'upload_video':
                await query.edit_message_text("📤 Send me a video to upload!")
            elif query.data == 'trending_videos':
                await self._handle_trending_videos(query, context)
            elif query.data == 'admin_panel':
                await self._handle_admin_panel(query, context)
            elif query.data == 'admin_stats':
                await self._handle_admin_stats(query, context)
            elif query.data.startswith('broadcast_'):
                await self._handle_broadcast_menu(query, context)
            elif query.data == 'back_to_main':
                await self._back_to_main(query, context)
                
        except Exception as e:
            logger.error(f"Error handling callback {query.data}: {e}")
            await query.edit_message_text("❌ An error occurred. Please try again.")

    async def _handle_get_video(self, query, context):
        """Handle get random video request."""
        user_id = query.from_user.id
        
        try:
            # Get or create user document
            user_doc = await users_collection.find_one({'user_id': user_id})
            
            if not user_doc:
                user_doc = {
                    'user_id': user_id,
                    'daily_count': 0,
                    'last_reset': datetime.date.today().isoformat(),
                    'uploaded_videos': 0
                }
                await users_collection.insert_one(user_doc)
            
            # Reset daily count if new day
            today = datetime.date.today().isoformat()
            if user_doc.get('last_reset') != today:
                await users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'daily_count': 0, 'last_reset': today}}
                )
                user_doc['daily_count'] = 0
            
            # Check daily limit
            if user_doc['daily_count'] >= DAILY_LIMIT:
                await query.edit_message_text(
                    f"⏰ Daily limit reached ({DAILY_LIMIT} videos)\\!\n"
                    f"Try again tomorrow\\.",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
            
            # Get random video
            videos_cursor = videos_collection.find({})
            videos_list = await videos_cursor.to_list(length=None)
            
            if not videos_list:
                await query.edit_message_text("📹 No videos available. Upload some first!")
                return
            
            random_video = random.choice(videos_list)
            
            # Send video
            await query.edit_message_text("🎬 Here's your random video:")
            
            video_message = await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=random_video['file_id'],
                protect_content=True
            )
            
            # Schedule auto-deletion
            context.job_queue.run_once(
                self._delete_message,
                300,  # 5 minutes
                data={
                    'chat_id': query.message.chat_id,
                    'message_id': video_message.message_id
                }
            )
            
            # Update user count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'daily_count': 1}}
            )
            
            remaining = DAILY_LIMIT - (user_doc['daily_count'] + 1)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ Video sent! {remaining} remaining today."
            )
            
        except Exception as e:
            logger.error(f"Error in get_video: {e}")
            await query.edit_message_text("❌ Error getting video. Try again later.")

    async def _handle_trending_videos(self, query, context):
        """Handle trending videos request."""
        try:
            trending_cursor = videos_collection.find({'is_trending': True})
            trending_list = await trending_cursor.to_list(length=3)
            
            if not trending_list:
                await query.edit_message_text("🔥 No trending videos available.")
                return
            
            await query.edit_message_text("🔥 Here are the trending videos:")
            
            for video in trending_list:
                try:
                    video_message = await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=video['file_id'],
                        protect_content=True
                    )
                    
                    # Auto-delete after 5 minutes
                    context.job_queue.run_once(
                        self._delete_message,
                        300,
                        data={
                            'chat_id': query.message.chat_id,
                            'message_id': video_message.message_id
                        }
                    )
                except Exception as e:
                    logger.error(f"Error sending trending video: {e}")
                    
        except Exception as e:
            logger.error(f"Error in trending_videos: {e}")
            await query.edit_message_text("❌ Error loading trending videos.")

    async def _handle_admin_panel(self, query, context):
        """Handle admin panel access."""
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text("❌ Admin access only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')],
            [InlineKeyboardButton("📡 Broadcast", callback_data='broadcast_menu')],
            [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⚙️ **Admin Panel**\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    async def _handle_admin_stats(self, query, context):
        """Handle admin statistics display."""
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text("❌ Admin access only.")
            return
        
        try:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})
            
            today = datetime.date.today().isoformat()
            active_today = await users_collection.count_documents({
                'last_reset': today,
                'daily_count': {'$gt': 0}
            })
            
            stats_text = (
                f"📊 **Bot Statistics**\n\n"
                f"👥 Total users: {total_users}\n"
                f"🔥 Active today: {active_today}\n"
                f"📹 Total videos: {total_videos}\n"
                f"⭐ Trending: {trending_count}\n"
                f"⚙️ Daily limit: {DAILY_LIMIT}"
            )
            
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                stats_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error in admin_stats: {e}")
            await query.edit_message_text("❌ Error loading statistics.")

    async def _handle_broadcast_menu(self, query, context):
        """Handle broadcast menu."""
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text("❌ Admin access only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("📝 Text Broadcast", callback_data='broadcast_text')],
            [InlineKeyboardButton("🎥 Video Broadcast", callback_data='broadcast_video')],
            [InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📡 **Broadcast Menu**\n\nChoose broadcast type:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    async def _back_to_main(self, query, context):
        """Return to main menu."""
        user = query.from_user
        
        welcome_text = (
            f"🎬 *Welcome back {user.first_name}\\!*\n\n"
            f"📋 Your ID: `{user.id}`\n\n"
            f"🎥 Get random videos from our collection\\!\n"
            f"📤 Upload your own videos\\!\n"
            f"🔥 Check trending videos\\!"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎲 Random Video", callback_data='get_video')],
            [InlineKeyboardButton("📤 Upload Video", callback_data='upload_video')],
            [InlineKeyboardButton("🔥 Trending", callback_data='trending_videos')]
        ]
        
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )

    async def handle_video_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle video uploads."""
        if not update.message or not update.message.video:
            return
        
        user_id = update.message.from_user.id
        video = update.message.video
        
        try:
            # Check if video already exists
            existing = await videos_collection.find_one({'file_id': video.file_id})
            if existing:
                await update.message.reply_text("📹 Video already in collection!")
                return
            
            # Add video to collection
            await videos_collection.insert_one({
                'file_id': video.file_id,
                'is_trending': False,
                'uploaded_by': user_id,
                'upload_date': datetime.datetime.now()
            })
            
            # Update user stats
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'uploaded_videos': 1}},
                upsert=True
            )
            
            # Get counts
            total_videos = await videos_collection.count_documents({})
            user_doc = await users_collection.find_one({'user_id': user_id})
            user_uploads = user_doc.get('uploaded_videos', 0) if user_doc else 0
            
            await update.message.reply_text(
                f"✅ Video uploaded successfully!\n"
                f"📊 Your uploads: {user_uploads}\n"
                f"📹 Total videos: {total_videos}"
            )
            
        except Exception as e:
            logger.error(f"Error uploading video: {e}")
            await update.message.reply_text("❌ Error uploading video.")

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages."""
        await update.message.reply_text(
            "💬 Use the menu buttons or send a video to upload!"
        )

    async def _delete_message(self, context: ContextTypes.DEFAULT_TYPE):
        """Delete a message after delay."""
        job_data = context.job.data
        try:
            await context.bot.delete_message(
                chat_id=job_data['chat_id'],
                message_id=job_data['message_id']
            )
        except TelegramError as e:
            logger.error(f"Error deleting message: {e}")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command."""
        user_id = update.message.from_user.id
        
        try:
            user_doc = await users_collection.find_one({'user_id': user_id})
            
            daily_count = user_doc.get('daily_count', 0) if user_doc else 0
            uploads = user_doc.get('uploaded_videos', 0) if user_doc else 0
            remaining = max(0, DAILY_LIMIT - daily_count)
            
            stats_text = (
                f"📊 **Your Statistics**\n\n"
                f"🆔 User ID: `{user_id}`\n"
                f"📹 Videos today: {daily_count}/{DAILY_LIMIT}\n"
                f"⏳ Remaining: {remaining}\n"
                f"📤 Total uploads: {uploads}"
            )
            
            await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
            
        except Exception as e:
            logger.error(f"Error in stats command: {e}")
            await update.message.reply_text("❌ Error loading statistics.")

def main():
    """Main function to run the bot."""
    if not API_TOKEN:
        logger.error("❌ TELEGRAM_API_TOKEN not found!")
        return
    
    if not WEBHOOK_URL:
        logger.error("❌ WEBHOOK_URL not found!")
        return
    
    # Create bot instance
    bot = TelegramBot()
    
    # Create application
    application = Application.builder().token(API_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("stats", bot.stats_command))
    application.add_handler(CallbackQueryHandler(bot.handle_callback))
    application.add_handler(MessageHandler(filters.VIDEO, bot.handle_video_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text_message))
    
    # Initialize database and start webhook
    async def startup():
        await init_database()
    
    # Add startup hook
    application.add_handler(CommandHandler("start", startup), group=-1)
    
    logger.info(f"🚀 Starting bot on {HOST}:{PORT}")
    logger.info(f"🔗 Webhook URL: {WEBHOOK_URL}")
    
    # Run webhook
    application.run_webhook(
        listen=HOST,
        port=PORT,
        url_path="",
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True
    )

if __name__ == '__main__':
    try:
        # Initialize database connection first
        import asyncio
        asyncio.run(init_database())
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped")
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}")
        raise
