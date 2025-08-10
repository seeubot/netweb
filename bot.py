# bot.py
import os
import random
import logging
import datetime
import asyncio
import hashlib
import base64
import re
from typing import Dict, Any, Optional, List
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from dotenv import load_dotenv
from aiohttp import web # New import for the web server

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
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
BOT_USERNAME = os.getenv('BOT_USERNAME', 'your_bot_username')
MONGO_URI = "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie"
DB_NAME = "telegram_bot_db"

# Webhook configuration for Koyeb
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
LISTEN_ADDRESS = '0.0.0.0'

# Global database client and collections
db_client = None
db = None
users_collection = None
videos_collection = None
shared_videos_collection = None
ott_collection = None # New collection for OTT content

# State keys for admin content management
OTT_STATE = 'ott_state'
OTT_TYPE = 'ott_type'
OTT_DATA = 'ott_data'

async def connect_to_mongodb():
    """Connects to MongoDB and sets up global collections."""
    global db_client, db, users_collection, videos_collection, shared_videos_collection, ott_collection
    try:
        db_client = AsyncIOMotorClient(MONGO_URI)
        await db_client.admin.command('ping')
        db = db_client[DB_NAME]
        users_collection = db['users']
        videos_collection = db['videos']
        shared_videos_collection = db['shared_videos']
        ott_collection = db['ott_content'] # Initialize new collection
        logger.info("Successfully connected to MongoDB.")
        return True
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return False

def generate_share_token(video_id: str, user_id: int) -> str:
    """Generate a unique share token for a video."""
    timestamp = str(int(datetime.datetime.now().timestamp()))
    data = f"{video_id}_{user_id}_{timestamp}"
    token = hashlib.sha256(data.encode()).hexdigest()[:16]
    return token

async def create_share_url(video_doc: dict, user_id: int) -> str:
    """Create a shareable URL for a video."""
    try:
        share_token = generate_share_token(str(video_doc['_id']), user_id)
        
        share_data = {
            'token': share_token,
            'video_id': video_doc['_id'],
            'file_id': video_doc['file_id'],
            'shared_by': user_id,
            'created_at': datetime.datetime.now(),
            'access_count': 0,
            'expires_at': datetime.datetime.now() + datetime.timedelta(days=7)
        }
        
        await shared_videos_collection.insert_one(share_data)
        
        share_url = f"https://t.me/{BOT_USERNAME}?start=share_{share_token}"
        return share_url
        
    except Exception as e:
        logger.error(f"Error creating share URL: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and main menu keyboard to the user."""
    user = update.effective_user
    user_id = user.id
    
    if context.args and context.args[0].startswith('share_'):
        await handle_shared_video_access(update, context, context.args[0])
        return
    
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot shares random videos from our collection\\.\n"
    welcome_message += "Use the buttons below to get videos or upload new ones\\."

    keyboard = [
        [
            InlineKeyboardButton("🎥 Random Video", callback_data='get_video'),
            InlineKeyboardButton("🔎 Search Videos", callback_data='search_menu')
        ],
        [
            InlineKeyboardButton("📤 Upload Video", callback_data='upload_video'),
            InlineKeyboardButton("🔥 Trending Videos", callback_data='trending_videos')
        ],
        [InlineKeyboardButton("🔗 Get Share Link", callback_data='get_share_link')]
    ]
    
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def handle_shared_video_access(update: Update, context: ContextTypes.DEFAULT_TYPE, share_param: str) -> None:
    """Handle access to shared videos via URL."""
    try:
        share_token = share_param.replace('share_', '')
        
        share_doc = await shared_videos_collection.find_one({
            'token': share_token,
            'expires_at': {'$gt': datetime.datetime.now()}
        })
        
        if not share_doc:
            await update.message.reply_text(
                "❌ **Invalid or Expired Link**\n\n"
                "This video share link is either invalid or has expired.\n"
                "Share links expire after 7 days.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await shared_videos_collection.update_one(
            {'token': share_token},
            {'$inc': {'access_count': 1}}
        )
        
        await update.message.reply_text("🎥 **Shared Video:**")
        sent_message = await context.bot.send_video(
            chat_id=update.message.chat_id,
            video=share_doc['file_id'],
            caption=f"📤 Shared by user {share_doc['shared_by']}\n🔢 Access count: {share_doc['access_count'] + 1}",
            protect_content=True
        )
        
        context.job_queue.run_once(
            delete_message,
            300,
            data={'chat_id': update.message.chat_id, 'message_id': sent_message.message_id}
        )
        
        keyboard = [
            [
                InlineKeyboardButton("🎥 Random Video", callback_data='get_video'),
                InlineKeyboardButton("🔎 Search Videos", callback_data='search_menu')
            ],
            [
                InlineKeyboardButton("📤 Upload Video", callback_data='upload_video'),
                InlineKeyboardButton("🔥 Trending Videos", callback_data='trending_videos')
            ],
            [InlineKeyboardButton("🔗 Get Share Link", callback_data='get_share_link')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ Enjoy the video! Use the buttons below for more options:",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error handling shared video access: {e}")
        await update.message.reply_text("❌ Error accessing shared video.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    if query.data == 'get_video':
        await handle_get_video(query, context)
    
    elif query.data == 'get_share_link':
        await handle_get_share_link(query, context)

    elif query.data == 'upload_video':
        await query.edit_message_text(
            text="🎥 **Upload Video**\n\n"
                 "Please send me the video you want to upload.\n\n"
                 "💡 **Tip:** Add a caption and tags (e.g., `#funny #cat`) to make it searchable!"
        )
        context.user_data['upload_mode'] = True

    elif query.data == 'trending_videos':
        await handle_trending_videos(query, context)

    elif query.data == 'search_menu':
        await query.edit_message_text(
            text="🔎 **Video Search**\n\n"
                 "To search for a video, use the `/search <keyword>` command.\n\n"
                 "Example: `/search funny cats`"
        )
    
    # New Admin OTT panel entry point
    elif query.data == 'ott_menu':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied. Admin only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("🎬 Add Movie", callback_data='ott_add_movie')],
            [InlineKeyboardButton("📺 Add Series", callback_data='ott_add_series')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="➕ **Add New OTT Content**\n\nChoose content type:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    # New Admin OTT content type selection
    elif query.data in ['ott_add_movie', 'ott_add_series']:
        await handle_add_ott_content(query, context)
    
    # Existing admin panel logic
    elif query.data == 'admin_panel':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied. Admin only.")
            return
            
        admin_keyboard = [
            [
                InlineKeyboardButton("📡 Broadcast", callback_data='broadcast_menu'),
                InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')
            ],
            [
                InlineKeyboardButton("🔥 Manage Trending", callback_data='manage_trending'),
                InlineKeyboardButton("🔗 Share Statistics", callback_data='share_stats')
            ],
            [
                InlineKeyboardButton("🎬 OTT Content", callback_data='ott_menu'),
                InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(admin_keyboard)
        
        await query.edit_message_text(
            text="🛠 **Admin Panel**\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    # Rest of the existing callback handlers...
    elif query.data == 'manage_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        context.user_data['trending_mode'] = True
        await query.edit_message_text(
            text="🔥 **Add Trending Video**\n\n"
                 "Send me a video to add to trending list.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'share_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        try:
            total_shares = await shared_videos_collection.count_documents({})
            active_shares = await shared_videos_collection.count_documents({
                'expires_at': {'$gt': datetime.datetime.now()}
            })
            expired_shares = total_shares - active_shares
            
            pipeline = [
                {'$match': {'expires_at': {'$gt': datetime.datetime.now()}}},
                {'$sort': {'access_count': -1}},
                {'$limit': 5}
            ]
            
            top_shares = []
            async for doc in shared_videos_collection.aggregate(pipeline):
                top_shares.append(f"• Token: `{doc['token'][:8]}`... - {doc['access_count']} accesses")
            
            stats_text = f"🔗 **Share Statistics**\n\n"
            stats_text += f"📊 Total shares created: {total_shares}\n"
            stats_text += f"✅ Active shares: {active_shares}\n"
            stats_text += f"❌ Expired shares: {expired_shares}\n\n"
            
            if top_shares:
                stats_text += "🔥 **Top Accessed Shares:**\n"
                stats_text += "\n".join(top_shares[:3])
            
            back_keyboard = [[InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]]
            reply_markup = InlineKeyboardMarkup(back_keyboard)
            
            await query.edit_message_text(
                text=stats_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in share_stats: {e}")
            await query.edit_message_text(text="❌ Error loading share statistics.")

    elif query.data == 'broadcast_menu':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        broadcast_keyboard = [
            [InlineKeyboardButton("📝 Text Message", callback_data='broadcast_text')],
            [InlineKeyboardButton("🎥 Video Broadcast", callback_data='broadcast_video')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(broadcast_keyboard)
        
        await query.edit_message_text(
            text="📡 **Broadcast Menu**\n\n"
                 "Choose the type of content to broadcast:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data in ['broadcast_text', 'broadcast_video']:
        await handle_broadcast_setup(query, context)

    elif query.data == 'admin_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})
            total_shares = await shared_videos_collection.count_documents({})
            active_shares = await shared_videos_collection.count_documents({
                'expires_at': {'$gt': datetime.datetime.now()}
            })

            today_iso = datetime.date.today().isoformat()
            active_today = await users_collection.count_documents({
                'last_reset': today_iso,
                'daily_count': {'$gt': 0}
            })
            
            stats_text = f"📊 **Bot Statistics**\n\n"
            stats_text += f"👥 Total users: {total_users}\n"
            stats_text += f"🔥 Active today: {active_today}\n"
            stats_text += f"🎥 Total videos: {total_videos}\n"
            stats_text += f"⭐ Trending videos: {trending_count}\n"
            stats_text += f"🔗 Total shares created: {total_shares}\n"
            stats_text += f"✅ Active shares: {active_shares}\n"
            stats_text += f"⚙️ Daily limit: {DAILY_LIMIT}\n"
            stats_text += f"🤖 Auto-delete: 5 minutes"
            
            back_keyboard = [[InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]]
            reply_markup = InlineKeyboardMarkup(back_keyboard)
            
            await query.edit_message_text(
                text=stats_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in admin_stats: {e}")
            await query.edit_message_text(text="❌ Error loading statistics.")

    elif query.data == 'back_to_main':
        user = query.from_user
        welcome_message = f"Welcome back, {user.mention_markdown_v2()}\\!\n\n"
        welcome_message += f"Your User ID: `{user.id}`\n\n"
        welcome_message += "This bot shares random videos from our collection\\.\n"
        welcome_message += "Use the buttons below to get videos or upload new ones\\."

        keyboard = [
            [
                InlineKeyboardButton("🎥 Random Video", callback_data='get_video'),
                InlineKeyboardButton("🔎 Search Videos", callback_data='search_menu')
            ],
            [
                InlineKeyboardButton("📤 Upload Video", callback_data='upload_video'),
                InlineKeyboardButton("🔥 Trending Videos", callback_data='trending_videos')
            ],
            [InlineKeyboardButton("🔗 Get Share Link", callback_data='get_share_link')]
        ]
        
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def handle_add_ott_content(query, context):
    """Admin entry point to add a new movie or series."""
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.edit_message_text(text="❌ Access denied. Admin only.")
        return

    content_type = 'movie' if query.data == 'ott_add_movie' else 'series'
    context.user_data[OTT_STATE] = 'awaiting_name'
    context.user_data[OTT_TYPE] = content_type
    context.user_data[OTT_DATA] = {}

    await query.edit_message_text(
        f"📝 **Add New {content_type.capitalize()}**\n\n"
        f"Please send the **name** of the {content_type}."
    )

async def handle_get_video(query, context):
    # Existing handler for getting a random video
    # ... (no changes needed)
    pass

async def handle_get_share_link(query, context):
    # Existing handler for getting a share link
    # ... (no changes needed)
    pass

async def handle_trending_videos(query, context):
    # Existing handler for trending videos
    # ... (no changes needed)
    pass

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Existing handler for video uploads
    # ... (no changes needed)
    pass

async def handle_broadcast_setup(query, context):
    # Existing handler for broadcast setup
    # ... (no changes needed)
    pass

async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Existing handler for admin content
    # ... (no changes needed)
    pass

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels any ongoing admin operation (broadcast, trending add, OTT)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return
    
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('trending_mode', None)
    context.user_data.pop(OTT_STATE, None)
    context.user_data.pop(OTT_TYPE, None)
    context.user_data.pop(OTT_DATA, None)
    
    await update.message.reply_text(
        "✅ **Operation Cancelled**\n\n"
        "All ongoing operations have been cancelled.\n"
        "Use /start to return to the main menu."
    )

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a message after a specified delay using JobQueue."""
    job_data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=job_data['chat_id'], 
            message_id=job_data['message_id']
        )
        logger.info(f"Auto-deleted message {job_data['message_id']} from chat {job_data['chat_id']}")
    except TelegramError as e:
        logger.error(f"Error deleting message: {e}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Existing handler for stats
    # ... (no changes needed)
    pass

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, including for the new OTT content flow."""
    user_id = update.message.from_user.id
    
    # Handle OTT content creation flow for admin
    if ADMIN_ID and user_id == ADMIN_ID and context.user_data.get(OTT_STATE):
        await handle_ott_input(update, context)
        return

    # Handle admin broadcast
    if ADMIN_ID and user_id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'text':
        await handle_admin_content(update, context)
        return
    
    # Fallback message
    await update.message.reply_text("💬 I'm not configured to respond to general text messages yet. Please use the buttons or send videos!")

async def handle_ott_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the multi-step input for adding OTT content."""
    state = context.user_data.get(OTT_STATE)
    ott_type = context.user_data.get(OTT_TYPE)
    ott_data = context.user_data.get(OTT_DATA)

    if not update.message:
        return

    message = update.message
    
    if state == 'awaiting_name':
        ott_data['name'] = message.text
        context.user_data[OTT_STATE] = 'awaiting_thumbnail'
        await message.reply_text("🖼️ Please send the **thumbnail image** for the content.")
    
    elif state == 'awaiting_thumbnail':
        if not message.photo:
            await message.reply_text("❌ Please send a valid photo. Try again or use /cancel.")
            return
        
        ott_data['thumbnail'] = message.photo[-1].file_id
        
        if ott_type == 'movie':
            context.user_data[OTT_STATE] = 'awaiting_url'
            await message.reply_text("🔗 Please send the **streaming URL** for the movie.")
        
        elif ott_type == 'series':
            ott_data['seasons'] = []
            context.user_data[OTT_STATE] = 'awaiting_season_name'
            await message.reply_text("📺 Now, let's add the first season. Please send the **name of the season**.")

    elif state == 'awaiting_url':
        ott_data['streaming_url'] = message.text
        
        # Save movie to database
        ott_data['type'] = 'movie'
        await ott_collection.insert_one(ott_data)
        
        await message.reply_text(
            f"✅ **Movie '{ott_data['name']}' added successfully!**\n\n"
            f"Use /start to return to the main menu."
        )
        context.user_data.pop(OTT_STATE)
        context.user_data.pop(OTT_TYPE)
        context.user_data.pop(OTT_DATA)

    # Series-specific states
    elif state == 'awaiting_season_name':
        season = {'season_name': message.text, 'episodes': []}
        ott_data['seasons'].append(season)
        context.user_data[OTT_STATE] = 'awaiting_episode_name'
        await message.reply_text("🎬 Season added. Now send the **name of the first episode**.")

    elif state == 'awaiting_episode_name':
        current_season = ott_data['seasons'][-1]
        episode = {'episode_name': message.text}
        context.user_data['current_episode_data'] = episode
        context.user_data[OTT_STATE] = 'awaiting_episode_url'
        await message.reply_text("🔗 Please send the **streaming URL** for this episode.")

    elif state == 'awaiting_episode_url':
        current_season = ott_data['seasons'][-1]
        episode = context.user_data.get('current_episode_data')
        if not episode:
            await message.reply_text("❌ An error occurred. Please use /cancel and try again.")
            return

        episode['streaming_url'] = message.text
        current_season['episodes'].append(episode)
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Another Episode", callback_data='ott_add_episode')],
            [InlineKeyboardButton("➕ Add Another Season", callback_data='ott_add_season')],
            [InlineKeyboardButton("✅ Done", callback_data='ott_done')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        context.user_data[OTT_STATE] = 'awaiting_action'
        await message.reply_text(
            f"✅ Episode '{episode['episode_name']}' added.\n\n"
            f"What would you like to do next?",
            reply_markup=reply_markup
        )
    
    # Callback handlers for the series flow
    elif state == 'awaiting_action':
        query = update.callback_query
        if not query:
            return

        if query.data == 'ott_add_episode':
            context.user_data[OTT_STATE] = 'awaiting_episode_name'
            await query.edit_message_text("🎬 Please send the **name of the next episode**.")
        
        elif query.data == 'ott_add_season':
            context.user_data[OTT_STATE] = 'awaiting_season_name'
            await query.edit_message_text("📺 Please send the **name of the next season**.")
        
        elif query.data == 'ott_done':
            # Finalize and save the series to the database
            ott_data['type'] = 'series'
            await ott_collection.insert_one(ott_data)
            
            await query.edit_message_text(
                f"✅ **Series '{ott_data['name']}' added successfully!**\n\n"
                f"Use /start to return to the main menu."
            )
            context.user_data.pop(OTT_STATE)
            context.user_data.pop(OTT_TYPE)
            context.user_data.pop(OTT_DATA)

async def cleanup_expired_shares(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleanup expired share links."""
    try:
        result = await shared_videos_collection.delete_many({
            'expires_at': {'$lt': datetime.datetime.now()}
        })
        if result.deleted_count > 0:
            logger.info(f"Cleaned up {result.deleted_count} expired share links")
    except Exception as e:
        logger.error(f"Error cleaning up expired shares: {e}")
        
# --- NEW AIOHTTP ENDPOINT FOR OTT CONTENT ---
async def get_ott_content(request):
    """
    HTTP GET endpoint to retrieve all OTT content from the database.
    This will be called by the frontend web page.
    """
    if not ott_collection:
        return web.json_response({"error": "Database not initialized"}, status=503)

    try:
        # Fetch all documents from the collection
        cursor = ott_collection.find({})
        ott_content_list = []
        for doc in await cursor.to_list(length=None):
            # MongoDB's ObjectId is not JSON serializable, so convert it to a string
            doc['_id'] = str(doc['_id'])
            ott_content_list.append(doc)
        
        # Return the data as a JSON response
        return web.json_response(ott_content_list)
    
    except Exception as e:
        logger.error(f"Error fetching OTT content: {e}")
        return web.json_response({"error": "Failed to fetch content"}, status=500)

async def run_bot_and_server(application, app_runner):
    """Starts both the Telegram bot and the aiohttp web server."""
    # Start the aiohttp web server
    await app_runner.setup()
    site = web.TCPSite(app_runner, LISTEN_ADDRESS, PORT)
    await site.start()
    logger.info(f"Web server started on http://{LISTEN_ADDRESS}:{PORT}")
    
    # Start the Telegram bot
    await application.post_init()
    logger.info(f"Bot started in webhook mode...")
    
    # This keeps the application running indefinitely
    await asyncio.Future()

def main() -> None:
    """Starts the bot and sets up all handlers."""
    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found in environment variables. Webhook deployment requires this.")
        return
    if not BOT_USERNAME:
        logger.error("BOT_USERNAME not found in environment variables. Required for share URL generation.")
        return
    
    # Create the Telegram bot application
    tg_application = (
        Application.builder()
        .token(API_TOKEN)
        .build()
    )

    # Set up Telegram bot handlers
    tg_application.add_handler(CommandHandler("start", start))
    tg_application.add_handler(CommandHandler("stats", stats))
    tg_application.add_handler(CommandHandler("cancel", cancel_operation))
    tg_application.add_handler(CallbackQueryHandler(button))
    tg_application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    tg_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    if tg_application.job_queue:
        tg_application.job_queue.run_repeating(cleanup_expired_shares, interval=3600, first=3600)
        tg_application.job_queue.run_repeating(lambda context: logger.info("Bot is running..."), interval=3600, first=3600)
    
    # Set up the aiohttp web server and the bot webhook
    web_app = web.Application()
    web_app.router.add_get('/ott_content', get_ott_content)
    web_app.router.add_post("", tg_application.updater.webhook)
    
    runner = web.AppRunner(web_app)

    asyncio.get_event_loop().run_until_complete(connect_to_mongodb())
    asyncio.get_event_loop().run_until_complete(run_bot_and_server(tg_application, runner))
    
    
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


