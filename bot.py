# bot.py
import os
import random
import logging
import datetime
import asyncio
import hashlib
import base64
import re
import uvicorn
from typing import Dict, Any, Optional, List
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter
from telegram.warnings import PTBUserWarning
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
import warnings

# Suppress PTBUserWarning for webhook updates
warnings.filterwarnings("ignore", category=PTBUserWarning)

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
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")

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
ott_collection = None

# State keys for admin content management
OTT_STATE = 'ott_state'
OTT_TYPE = 'ott_type'
OTT_DATA = 'ott_data'

# Global application instance
application = None

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
        ott_collection = db['ott_content']
        logger.info("Successfully connected to MongoDB.")
        return True
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return False

async def set_webhook_with_retry(bot, webhook_url, max_retries=3):
    """Set webhook with retry logic for rate limiting."""
    for attempt in range(max_retries):
        try:
            # First, delete any existing webhook
            await bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(1)
            
            # Set the new webhook
            await bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook set successfully to {webhook_url} on attempt {attempt + 1}")
            
            # Verify webhook was set
            webhook_info = await bot.get_webhook_info()
            logger.info(f"Webhook info: {webhook_info}")
            return True
        except RetryAfter as e:
            if attempt < max_retries - 1:
                wait_time = e.retry_after + 1
                logger.warning(f"Rate limited. Waiting {wait_time} seconds before retry {attempt + 2}")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Failed to set webhook after {max_retries} attempts due to rate limiting")
                raise
        except Exception as e:
            logger.error(f"Error setting webhook on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
            else:
                raise
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
    
    logger.info(f"Start command received from user {user_id}")
    
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
    
    logger.info(f"Button pressed: {query.data} by user {query.from_user.id}")

    if query.data == 'get_video':
        await handle_get_video(query, context)
    
    elif query.data == 'get_share_link':
        await handle_get_share_link(query, context)

    elif query.data == 'upload_video':
        await query.edit_message_text(
            text="🎥 **Upload Video**\n\n"
                 "Please send me the video you want to upload.\n\n"
                 "💡 **Tip:** Add a caption and tags (e.g., `#funny #cat`) to make it searchable!",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['upload_mode'] = True

    elif query.data == 'trending_videos':
        await handle_trending_videos(query, context)

    elif query.data == 'search_menu':
        await query.edit_message_text(
            text="🔎 **Video Search**\n\n"
                 "To search for a video, use the `/search <keyword>` command.\n\n"
                 "Example: `/search funny cats`",
            parse_mode=ParseMode.MARKDOWN
        )
    
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

    elif query.data in ['ott_add_movie', 'ott_add_series']:
        await handle_add_ott_content(query, context)
    
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
    """Handles getting a random video from the database."""
    user = query.from_user
    user_id = user.id
    try:
        user_doc = await users_collection.find_one({'user_id': user_id})
        
        today_iso = datetime.date.today().isoformat()
        
        if not user_doc or user_doc.get('last_reset') != today_iso:
            await users_collection.update_one(
                {'user_id': user_id},
                {'$set': {'daily_count': 0, 'last_reset': today_iso, 'user_id': user_id}},
                upsert=True
            )
            daily_count = 0
        else:
            daily_count = user_doc['daily_count']

        if daily_count >= DAILY_LIMIT:
            await query.edit_message_text(
                f"🚫 **Daily Limit Reached**\n\n"
                f"You have reached your daily limit of {DAILY_LIMIT} videos.\n"
                f"Your limit will reset tomorrow\\. Enjoy your day\\! ✨",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        videos = await videos_collection.find().to_list(length=None)
        if not videos:
            await query.edit_message_text("😔 **No videos found!**\n\nCome back later.")
            return

        video_doc = random.choice(videos)
        
        await users_collection.update_one(
            {'user_id': user_id},
            {'$inc': {'daily_count': 1}}
        )
        
        caption_text = f"🎥 **Video**\n\n"
        if 'caption' in video_doc:
            caption_text += f"{video_doc['caption']}\n\n"
        caption_text += f"**Views:** {video_doc.get('views', 0) + 1}\n"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Get another video", callback_data='get_video')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        sent_message = await context.bot.send_video(
            chat_id=query.message.chat_id,
            video=video_doc['file_id'],
            caption=caption_text,
            protect_content=True,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

        await videos_collection.update_one(
            {'_id': video_doc['_id']},
            {'$inc': {'views': 1}}
        )

        context.job_queue.run_once(
            delete_message,
            300,
            data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
        )
        await query.message.delete()
        
    except Exception as e:
        logger.error(f"Error handling get_video: {e}")
        await query.edit_message_text("❌ An error occurred while fetching the video.")

async def handle_get_share_link(query, context):
    """Handles creating a share link for a random video."""
    try:
        videos = await videos_collection.find().to_list(length=None)
        if not videos:
            await query.edit_message_text("😔 No videos found to share.")
            return
            
        video_doc = random.choice(videos)
        
        share_url = await create_share_url(video_doc, query.from_user.id)
        if share_url:
            await query.edit_message_text(
                f"🔗 **Share Link Created!**\n\n"
                f"Share this link with your friends to give them access to this video:\n\n"
                f"`{share_url}`\n\n"
                f"This link is valid for **7 days**.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text("❌ An error occurred while creating the share link.")
    except Exception as e:
        logger.error(f"Error handling share link request: {e}")
        await query.edit_message_text("❌ An error occurred while creating the share link.")

async def handle_trending_videos(query, context):
    """Handles getting trending videos from the database."""
    try:
        trending_videos = await videos_collection.find(
            {'is_trending': True}
        ).to_list(length=None)

        if not trending_videos:
            await query.edit_message_text("🔥 **No trending videos found!**\n\nCheck back later.")
            return

        for video_doc in trending_videos:
            caption_text = f"🔥 **Trending Video**\n\n"
            if 'caption' in video_doc:
                caption_text += f"{video_doc['caption']}\n\n"
            caption_text += f"**Views:** {video_doc.get('views', 0)}\n"
            
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=video_doc['file_id'],
                caption=caption_text,
                protect_content=True,
                parse_mode=ParseMode.MARKDOWN
            )

            context.job_queue.run_once(
                delete_message,
                300,
                data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
            )

        await query.message.reply_text("✅ Enjoy the trending videos!")
        await query.message.delete()

    except Exception as e:
        logger.error(f"Error handling trending videos: {e}")
        await query.message.reply_text("❌ An error occurred while fetching trending videos.")

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming video uploads."""
    user = update.effective_user
    logger.info(f"Video upload from user {user.id}")
    
    if user.id != ADMIN_ID and not context.user_data.get('upload_mode'):
        await update.message.reply_text(
            "❌ This bot only accepts videos from the admin or in upload mode. "
            "Use the '📤 Upload Video' button to start uploading."
        )
        return
        
    try:
        video_file_id = update.message.video.file_id
        caption = update.message.caption if update.message.caption else ""
        
        tags = re.findall(r'#(\w+)', caption)
        
        video_doc = {
            'file_id': video_file_id,
            'caption': caption,
            'uploader_id': user.id,
            'upload_date': datetime.datetime.now(),
            'tags': tags,
            'views': 0,
            'is_trending': False
        }

        await videos_collection.insert_one(video_doc)
        
        await update.message.reply_text("✅ Video uploaded successfully!")
        
        if context.user_data.get('upload_mode'):
            del context.user_data['upload_mode']
    except Exception as e:
        logger.error(f"Error saving video to DB: {e}")
        await update.message.reply_text("❌ An error occurred while uploading the video.")

async def handle_broadcast_setup(query, context):
    """Admin entry point for setting up a broadcast."""
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.edit_message_text(text="❌ Access denied.")
        return

    broadcast_type = query.data.split('_')[1]
    context.user_data['broadcast_mode'] = broadcast_type
    
    if broadcast_type == 'text':
        await query.edit_message_text(
            text="📝 **Broadcast Text Message**\n\n"
                 "Please send the text message you want to broadcast to all users."
        )
    elif broadcast_type == 'video':
        await query.edit_message_text(
            text="🎥 **Broadcast Video**\n\n"
                 "Please send the video you want to broadcast to all users."
        )

# Continuing from the cut-off point in handle_admin_content function

async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles admin-specific content, like broadcast messages and trending videos."""
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        return

    # Handle broadcast text messages
    if context.user_data.get('broadcast_mode') == 'text' and update.message.text:
        message_text = update.message.text
        await broadcast_message(context, message_text)
        del context.user_data['broadcast_mode']
        await update.message.reply_text("✅ Text broadcast completed!")
    
    # Handle broadcast video messages
    elif context.user_data.get('broadcast_mode') == 'video' and update.message.video:
        video_file_id = update.message.video.file_id
        caption = update.message.caption if update.message.caption else ""
        await broadcast_video(context, video_file_id, caption)
        del context.user_data['broadcast_mode']
        await update.message.reply_text("✅ Video broadcast completed!")
    
    # Handle trending video additions
    elif context.user_data.get('trending_mode') and update.message.video:
        try:
            video_file_id = update.message.video.file_id
            caption = update.message.caption if update.message.caption else ""
            tags = re.findall(r'#(\w+)', caption)
            
            video_doc = {
                'file_id': video_file_id,
                'caption': caption,
                'uploader_id': user_id,
                'upload_date': datetime.datetime.now(),
                'tags': tags,
                'views': 0,
                'is_trending': True
            }
            
            await videos_collection.insert_one(video_doc)
            del context.user_data['trending_mode']
            await update.message.reply_text("✅ Video added to trending list!")
        except Exception as e:
            logger.error(f"Error adding trending video: {e}")
            await update.message.reply_text("❌ Error adding video to trending list.")
    
    # Handle OTT content addition workflow
    elif context.user_data.get(OTT_STATE):
        await handle_ott_workflow(update, context)

async def handle_ott_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the OTT content addition workflow."""
    state = context.user_data.get(OTT_STATE)
    content_type = context.user_data.get(OTT_TYPE)
    ott_data = context.user_data.get(OTT_DATA, {})
    
    try:
        if state == 'awaiting_name':
            ott_data['name'] = update.message.text
            context.user_data[OTT_DATA] = ott_data
            context.user_data[OTT_STATE] = 'awaiting_description'
            
            await update.message.reply_text(
                f"📝 **Add Description**\n\n"
                f"Now send the description for **{ott_data['name']}**"
            )
        
        elif state == 'awaiting_description':
            ott_data['description'] = update.message.text
            context.user_data[OTT_DATA] = ott_data
            context.user_data[OTT_STATE] = 'awaiting_genre'
            
            await update.message.reply_text(
                f"🎭 **Add Genre**\n\n"
                f"Send the genre(s) for **{ott_data['name']}**\n"
                f"(e.g., Action, Comedy, Drama)"
            )
        
        elif state == 'awaiting_genre':
            ott_data['genre'] = update.message.text
            context.user_data[OTT_DATA] = ott_data
            context.user_data[OTT_STATE] = 'awaiting_year'
            
            await update.message.reply_text(
                f"📅 **Add Release Year**\n\n"
                f"Send the release year for **{ott_data['name']}**"
            )
        
        elif state == 'awaiting_year':
            try:
                year = int(update.message.text)
                ott_data['year'] = year
                context.user_data[OTT_DATA] = ott_data
                context.user_data[OTT_STATE] = 'awaiting_link'
                
                await update.message.reply_text(
                    f"🔗 **Add Download Link**\n\n"
                    f"Send the download link for **{ott_data['name']}**"
                )
            except ValueError:
                await update.message.reply_text("❌ Please send a valid year (e.g., 2023)")
        
        elif state == 'awaiting_link':
            ott_data['download_link'] = update.message.text
            ott_data['content_type'] = content_type
            ott_data['added_by'] = update.message.from_user.id
            ott_data['added_date'] = datetime.datetime.now()
            ott_data['views'] = 0
            
            # Save to database
            await ott_collection.insert_one(ott_data)
            
            # Clear user data
            del context.user_data[OTT_STATE]
            del context.user_data[OTT_TYPE]
            del context.user_data[OTT_DATA]
            
            # Send confirmation
            confirmation_text = (
                f"✅ **{content_type.capitalize()} Added Successfully!**\n\n"
                f"**Name:** {ott_data['name']}\n"
                f"**Genre:** {ott_data['genre']}\n"
                f"**Year:** {ott_data['year']}\n"
                f"**Description:** {ott_data['description'][:100]}..."
            )
            
            await update.message.reply_text(confirmation_text, parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"Error in OTT workflow: {e}")
        await update.message.reply_text("❌ Error processing OTT content.")

async def broadcast_message(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    """Broadcasts a text message to all users."""
    try:
        users = await users_collection.find().to_list(length=None)
        success_count = 0
        error_count = 0
        
        for user_doc in users:
            try:
                await context.bot.send_message(
                    chat_id=user_doc['user_id'],
                    text=f"📢 **Broadcast Message**\n\n{message}",
                    parse_mode=ParseMode.MARKDOWN
                )
                success_count += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to send broadcast to user {user_doc['user_id']}: {e}")
                error_count += 1
        
        logger.info(f"Broadcast completed: {success_count} sent, {error_count} failed")
    except Exception as e:
        logger.error(f"Error in broadcast_message: {e}")

async def broadcast_video(context: ContextTypes.DEFAULT_TYPE, video_file_id: str, caption: str = "") -> None:
    """Broadcasts a video to all users."""
    try:
        users = await users_collection.find().to_list(length=None)
        success_count = 0
        error_count = 0
        
        broadcast_caption = f"📢 **Broadcast Video**\n\n{caption}" if caption else "📢 **Broadcast Video**"
        
        for user_doc in users:
            try:
                await context.bot.send_video(
                    chat_id=user_doc['user_id'],
                    video=video_file_id,
                    caption=broadcast_caption,
                    parse_mode=ParseMode.MARKDOWN,
                    protect_content=True
                )
                success_count += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to send broadcast video to user {user_doc['user_id']}: {e}")
                error_count += 1
        
        logger.info(f"Video broadcast completed: {success_count} sent, {error_count} failed")
    except Exception as e:
        logger.error(f"Error in broadcast_video: {e}")

async def search_videos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles video search command."""
    if not context.args:
        await update.message.reply_text(
            "🔎 **Video Search**\n\n"
            "Usage: `/search <keyword>`\n"
            "Example: `/search funny cats`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    search_query = ' '.join(context.args).lower()
    
    try:
        # Search in captions and tags
        search_filter = {
            '$or': [
                {'caption': {'$regex': search_query, '$options': 'i'}},
                {'tags': {'$in': [search_query]}}
            ]
        }
        
        videos = await videos_collection.find(search_filter).limit(5).to_list(length=None)
        
        if not videos:
            await update.message.reply_text(
                f"😔 **No videos found**\n\n"
                f"No videos found for: *{search_query}*",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await update.message.reply_text(
            f"🔎 **Search Results** ({len(videos)} found)\n\n"
            f"Query: *{search_query}*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        for video_doc in videos:
            caption_text = f"🎥 **Search Result**\n\n"
            if video_doc.get('caption'):
                caption_text += f"{video_doc['caption']}\n\n"
            caption_text += f"**Views:** {video_doc.get('views', 0)}"
            
            sent_message = await context.bot.send_video(
                chat_id=update.message.chat_id,
                video=video_doc['file_id'],
                caption=caption_text,
                protect_content=True,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Auto-delete after 5 minutes
            context.job_queue.run_once(
                delete_message,
                300,
                data={'chat_id': update.message.chat_id, 'message_id': sent_message.message_id}
            )
            
    except Exception as e:
        logger.error(f"Error in search_videos: {e}")
        await update.message.reply_text("❌ An error occurred while searching for videos.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels current operation."""
    user_data_keys = ['upload_mode', 'broadcast_mode', 'trending_mode', OTT_STATE, OTT_TYPE, OTT_DATA]
    
    cancelled_operations = []
    for key in user_data_keys:
        if key in context.user_data:
            del context.user_data[key]
            cancelled_operations.append(key)
    
    if cancelled_operations:
        await update.message.reply_text("✅ Current operation cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active operation to cancel.")

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a message after a delay."""
    try:
        job_data = context.job.data
        await context.bot.delete_message(
            chat_id=job_data['chat_id'],
            message_id=job_data['message_id']
        )
    except Exception as e:
        logger.error(f"Error deleting message: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates."""
    logger.error(f'Update {update} caused error {context.error}')

# FastAPI application setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the application lifecycle."""
    logger.info("Starting application...")
    
    # Connect to MongoDB
    if not await connect_to_mongodb():
        logger.error("Failed to connect to MongoDB. Exiting.")
        return
    
    # Initialize Telegram bot
    global application
    application = Application.builder().token(API_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('search', search_videos))
    application.add_handler(CommandHandler('cancel', cancel_command))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_content))
    application.add_error_handler(error_handler)
    
    # Initialize the application
    await application.initialize()
    await application.start()
    
    # Set webhook if URL is provided
    if WEBHOOK_URL:
        try:
            await set_webhook_with_retry(application.bot, WEBHOOK_URL)
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
            await application.stop()
            return
    else:
        logger.warning("No WEBHOOK_URL provided. Bot will not receive updates.")
    
    logger.info("Application started successfully!")
    
    yield
    
    # Cleanup
    logger.info("Shutting down application...")
    if application:
        await application.stop()
        await application.shutdown()
    if db_client:
        db_client.close()
    logger.info("Application shut down complete!")

# Create FastAPI app
app = FastAPI(lifespan=lifespan, title="Telegram Video Bot", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Root endpoint with basic info."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Video Bot</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background-color: #f5f5f5; }
            .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h1 { color: #2c3e50; text-align: center; }
            .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
            .online { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .feature { padding: 8px; margin: 5px 0; background-color: #f8f9fa; border-left: 4px solid #007bff; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Telegram Video Bot</h1>
            <div class="status online">✅ Bot is running and operational</div>
            <div class="info">📡 Webhook configured and ready to receive updates</div>
            
            <h3>🌟 Features:</h3>
            <div class="feature">🎥 Random video sharing with daily limits</div>
            <div class="feature">🔎 Video search functionality</div>
            <div class="feature">📤 Video upload for users</div>
            <div class="feature">🔥 Trending videos section</div>
            <div class="feature">🔗 Shareable video links</div>
            <div class="feature">📡 Admin broadcast system</div>
            <div class="feature">🎬 OTT content management</div>
            <div class="feature">📊 Usage statistics and analytics</div>
            <div class="feature">🛡️ Content protection and auto-deletion</div>
            
            <div class="info">
                <strong>Bot Username:</strong> @{bot_username}<br>
                <strong>Daily Limit:</strong> {daily_limit} videos per user<br>
                <strong>Auto-delete:</strong> 5 minutes
            </div>
        </div>
    </body>
    </html>
    """.format(bot_username=BOT_USERNAME, daily_limit=DAILY_LIMIT)

@app.post(f"/webhook")
async def webhook_handler(request: Request):
    """Handle incoming webhook updates from Telegram."""
    try:
        # Get the raw body
        body = await request.body()
        
        # Parse the update
        update = Update.de_json(body.decode('utf-8'), application.bot)
        
        if update:
            # Process the update
            await application.process_update(update)
            return JSONResponse({"status": "ok"})
        else:
            logger.warning("Received invalid update")
            return JSONResponse({"status": "error", "message": "Invalid update"}, status_code=400)
            
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    try:
        # Check MongoDB connection
        await db_client.admin.command('ping')
        
        # Check bot connection
        bot_info = await application.bot.get_me()
        
        return JSONResponse({
            "status": "healthy",
            "bot_username": bot_info.username,
            "database": "connected",
            "timestamp": datetime.datetime.now().isoformat()
        })
    except Exception as e:
        return JSONResponse({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.datetime.now().isoformat()
        }, status_code=503)

@app.get("/stats")
async def get_stats():
    """Get bot statistics."""
    try:
        stats = {
            "total_users": await users_collection.count_documents({}),
            "total_videos": await videos_collection.count_documents({}),
            "trending_videos": await videos_collection.count_documents({"is_trending": True}),
            "total_shares": await shared_videos_collection.count_documents({}),
            "active_shares": await shared_videos_collection.count_documents({
                "expires_at": {"$gt": datetime.datetime.now()}
            }),
            "ott_content": await ott_collection.count_documents({}),
            "daily_limit": DAILY_LIMIT,
            "timestamp": datetime.datetime.now().isoformat()
        }
        
        return JSONResponse(stats)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

if __name__ == '__main__':
    logger.info(f"Starting server on {LISTEN_ADDRESS}:{PORT}")
    uvicorn.run(
        "bot:app",
        host=LISTEN_ADDRESS,
        port=PORT,
        reload=False,
        log_level="info"
    )
