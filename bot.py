# bot.py
import os
import random
import logging
import datetime
import asyncio
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
FILE_DAILY_LIMIT = int(os.getenv('FILE_DAILY_LIMIT', 3))  # Separate limit for files
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 50)) * 1024 * 1024  # 50MB default
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
files_collection = None

async def connect_to_mongodb():
    """Connects to MongoDB and sets up global collections."""
    global db_client, db, users_collection, videos_collection, files_collection
    try:
        db_client = AsyncIOMotorClient(MONGO_URI)
        # Test the connection
        await db_client.admin.command('ping')
        db = db_client[DB_NAME]
        users_collection = db['users']
        videos_collection = db['videos']
        files_collection = db['files']  # New collection for files
        logger.info("Successfully connected to MongoDB.")
        return True
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return False

async def fetch_videos_from_channel(context: ContextTypes.DEFAULT_TYPE):
    """
    Placeholder function to fetch videos from the source channel.
    The actual video file IDs are now stored in the database.
    """
    if not SOURCE_CHANNEL:
        logger.warning("SOURCE_CHANNEL not configured")
        return
    
    try:
        chat = await context.bot.get_chat(SOURCE_CHANNEL)
        logger.info(f"Fetching videos from channel: {chat.title}")
    except TelegramError as e:
        logger.error(f"Error accessing source channel {SOURCE_CHANNEL}: {e}")

def format_file_size(size_bytes):
    """Convert bytes to human readable format."""
    if size_bytes == 0:
        return "0B"
    size_names = ["B", "KB", "MB", "GB"]
    import math
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and main menu keyboard to the user."""
    user = update.effective_user
    user_id = user.id
    
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot shares random videos and files from our collection\\.\n"
    welcome_message += "Use the buttons below to get content or upload new ones\\."

    keyboard = [
        [
            InlineKeyboardButton("🎥 Random Video", callback_data='get_video'),
            InlineKeyboardButton("📁 Random File", callback_data='get_file')
        ],
        [
            InlineKeyboardButton("📤 Upload Video", callback_data='upload_video'),
            InlineKeyboardButton("📎 Upload File", callback_data='upload_file')
        ],
        [
            InlineKeyboardButton("🔥 Trending Videos", callback_data='trending_videos'),
            InlineKeyboardButton("📊 Popular Files", callback_data='popular_files')
        ],
        [InlineKeyboardButton("📋 Browse Categories", callback_data='browse_categories')]
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
        await handle_get_content(query, context, 'video')
    
    elif query.data == 'get_file':
        await handle_get_content(query, context, 'file')

    elif query.data == 'upload_video':
        await query.edit_message_text(text="🎥 Please send me the video you want to upload.")

    elif query.data == 'upload_file':
        await query.edit_message_text(
            text=f"📎 **File Upload**\n\n"
                 f"Please send me the file you want to share.\n\n"
                 f"📏 **Limits:**\n"
                 f"• Maximum file size: {format_file_size(MAX_FILE_SIZE)}\n"
                 f"• Daily file downloads: {FILE_DAILY_LIMIT}\n\n"
                 f"✅ **Supported types:** Documents, Images, Audio, Archives, etc."
        )

    elif query.data == 'trending_videos':
        await handle_trending_content(query, context, 'video')
    
    elif query.data == 'popular_files':
        await handle_trending_content(query, context, 'file')

    elif query.data == 'browse_categories':
        await handle_browse_categories(query, context)

    elif query.data.startswith('category_'):
        category = query.data.replace('category_', '')
        await handle_category_files(query, context, category)

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
                InlineKeyboardButton("📁 Manage Files", callback_data='manage_files')
            ],
            [InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(admin_keyboard)
        
        await query.edit_message_text(
            text="🛠 **Admin Panel**\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'manage_files':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        try:
            total_files = await files_collection.count_documents({})
            popular_files = await files_collection.count_documents({'is_popular': True})
            
            file_keyboard = [
                [InlineKeyboardButton("⭐ Add Popular", callback_data='add_popular_file')],
                [InlineKeyboardButton("🗑 Clear Popular", callback_data='clear_popular_files')],
                [InlineKeyboardButton("📋 File Stats", callback_data='file_stats')],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
            ]
            reply_markup = InlineKeyboardMarkup(file_keyboard)
            
            await query.edit_message_text(
                text=f"📁 **File Management**\n\n"
                     f"📊 Total files: {total_files}\n"
                     f"⭐ Popular files: {popular_files}\n\n"
                     f"Choose an action:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in manage_files: {e}")
            await query.edit_message_text(text="❌ Error loading file management.")

    elif query.data == 'add_popular_file':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        context.user_data['popular_file_mode'] = True
        await query.edit_message_text(
            text="⭐ **Add Popular File**\n\n"
                 "Send me a file to add to popular list.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'clear_popular_files':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        try:
            result = await files_collection.update_many(
                {'is_popular': True},
                {'$set': {'is_popular': False}}
            )
            await query.edit_message_text(
                text=f"✅ Cleared {result.modified_count} popular files successfully!"
            )
        except Exception as e:
            logger.error(f"Error clearing popular files: {e}")
            await query.edit_message_text(text="❌ Error clearing popular files.")

    elif query.data == 'file_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        try:
            # Get file statistics by category
            pipeline = [
                {'$group': {'_id': '$category', 'count': {'$sum': 1}}},
                {'$sort': {'count': -1}}
            ]
            
            categories = {}
            async for doc in files_collection.aggregate(pipeline):
                categories[doc['_id'] or 'Other'] = doc['count']
            
            total_files = await files_collection.count_documents({})
            popular_files = await files_collection.count_documents({'is_popular': True})
            
            stats_text = f"📁 **File Statistics**\n\n"
            stats_text += f"📊 Total files: {total_files}\n"
            stats_text += f"⭐ Popular files: {popular_files}\n\n"
            stats_text += "📋 **By Category:**\n"
            
            for category, count in categories.items():
                stats_text += f"• {category}: {count}\n"
            
            back_keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='manage_files')]]
            reply_markup = InlineKeyboardMarkup(back_keyboard)
            
            await query.edit_message_text(
                text=stats_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in file_stats: {e}")
            await query.edit_message_text(text="❌ Error loading file statistics.")

    # Handle other existing callback queries...
    elif query.data == 'broadcast_menu':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        broadcast_keyboard = [
            [InlineKeyboardButton("📝 Text Message", callback_data='broadcast_text')],
            [InlineKeyboardButton("🖼 Image Broadcast", callback_data='broadcast_image')],
            [InlineKeyboardButton("🎥 Video Broadcast", callback_data='broadcast_video')],
            [InlineKeyboardButton("📎 File Broadcast", callback_data='broadcast_file')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(broadcast_keyboard)
        
        await query.edit_message_text(
            text="📡 **Broadcast Menu**\n\n"
                 "Choose the type of content to broadcast:\n\n"
                 "• **Text**: Send a text message to all users\n"
                 "• **Image**: Send an image to all users\n"
                 "• **Video**: Send a video to all users\n"
                 "• **File**: Send a file to all users",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'broadcast_file':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
        
        context.user_data['broadcast_mode'] = 'file'
        await query.edit_message_text(
            text="📎 **File Broadcast Mode**\n\n"
                 "Send me the file you want to broadcast to all users.\n"
                 "You can include a caption with the file.\n\n"
                 "Use /cancel to cancel this operation."
        )

    # Add other existing handlers (broadcast_text, broadcast_image, etc.)
    elif query.data in ['broadcast_text', 'broadcast_image', 'broadcast_video']:
        await handle_broadcast_setup(query, context)

    elif query.data == 'admin_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            total_files = await files_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})
            popular_count = await files_collection.count_documents({'is_popular': True})

            today_iso = datetime.date.today().isoformat()
            active_today = await users_collection.count_documents({
                'last_reset': today_iso,
                '$or': [
                    {'daily_count': {'$gt': 0}},
                    {'file_daily_count': {'$gt': 0}}
                ]
            })
            
            stats_text = f"📊 **Bot Statistics**\n\n"
            stats_text += f"👥 Total users: {total_users}\n"
            stats_text += f"🔥 Active today: {active_today}\n"
            stats_text += f"🎥 Total videos: {total_videos}\n"
            stats_text += f"📁 Total files: {total_files}\n"
            stats_text += f"⭐ Trending videos: {trending_count}\n"
            stats_text += f"⭐ Popular files: {popular_count}\n"
            stats_text += f"⚙️ Video daily limit: {DAILY_LIMIT}\n"
            stats_text += f"⚙️ File daily limit: {FILE_DAILY_LIMIT}\n"
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
        welcome_message += "This bot shares random videos and files from our collection\\.\n"
        welcome_message += "Use the buttons below to get content or upload new ones\\."

        keyboard = [
            [
                InlineKeyboardButton("🎥 Random Video", callback_data='get_video'),
                InlineKeyboardButton("📁 Random File", callback_data='get_file')
            ],
            [
                InlineKeyboardButton("📤 Upload Video", callback_data='upload_video'),
                InlineKeyboardButton("📎 Upload File", callback_data='upload_file')
            ],
            [
                InlineKeyboardButton("🔥 Trending Videos", callback_data='trending_videos'),
                InlineKeyboardButton("📊 Popular Files", callback_data='popular_files')
            ],
            [InlineKeyboardButton("📋 Browse Categories", callback_data='browse_categories')]
        ]
        
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def handle_get_content(query, context, content_type):
    """Handle getting random video or file."""
    user_id = query.from_user.id
    
    try:
        user_doc = await users_collection.find_one({'user_id': user_id})
        
        if not user_doc:
            user_doc = {
                'user_id': user_id,
                'daily_count': 0,
                'file_daily_count': 0,
                'last_reset': datetime.date.today().isoformat(),
                'uploaded_videos': 0,
                'uploaded_files': 0
            }
            await users_collection.insert_one(user_doc)
        
        # Reset daily count if it's a new day
        if user_doc['last_reset'] != datetime.date.today().isoformat():
            await users_collection.update_one(
                {'user_id': user_id},
                {'$set': {
                    'daily_count': 0, 
                    'file_daily_count': 0,
                    'last_reset': datetime.date.today().isoformat()
                }}
            )
            user_doc['daily_count'] = 0
            user_doc['file_daily_count'] = 0
            user_doc['last_reset'] = datetime.date.today().isoformat()
        
        # Check daily limit based on content type
        if content_type == 'video':
            limit = DAILY_LIMIT
            count_field = 'daily_count'
            current_count = user_doc.get('daily_count', 0)
            collection = videos_collection
            emoji = "🎥"
        else:  # file
            limit = FILE_DAILY_LIMIT
            count_field = 'file_daily_count'
            current_count = user_doc.get('file_daily_count', 0)
            collection = files_collection
            emoji = "📁"
        
        if current_count >= limit:
            await query.edit_message_text(
                text=f"⏰ You have reached your daily limit of {limit} {content_type}s.\n"
                     f"Please try again tomorrow!"
            )
            return

        # Get all content from collection
        all_content = []
        async for doc in collection.find({}):
            all_content.append(doc)
        
        if not all_content:
            await query.edit_message_text(
                text=f"{emoji} No {content_type}s available at the moment.\n"
                     f"Please upload some {content_type}s first!"
            )
            return

        # Send random content
        random_content = random.choice(all_content)
        await query.edit_message_text(text=f"{emoji} Here is your random {content_type}:")
        
        if content_type == 'video':
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id, 
                video=random_content['file_id'],
                protect_content=True
            )
        else:  # file
            caption = f"📁 **{random_content.get('file_name', 'File')}**\n"
            if random_content.get('file_size'):
                caption += f"📏 Size: {format_file_size(random_content['file_size'])}\n"
            if random_content.get('category'):
                caption += f"📂 Category: {random_content['category']}"
            
            sent_message = await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=random_content['file_id'],
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                protect_content=True
            )
        
        # Schedule message deletion after 5 minutes
        context.job_queue.run_once(
            delete_message,
            300,
            data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
        )

        # Update user's daily count
        await users_collection.update_one(
            {'user_id': user_id},
            {'$inc': {count_field: 1}}
        )
        
        remaining = limit - (current_count + 1)
        if remaining > 0:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ {content_type.title()} sent! You have {remaining} {content_type}s left today."
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ {content_type.title()} sent! You've reached your daily limit. See you tomorrow!"
            )
            
    except Exception as e:
        logger.error(f"Error in get_{content_type}: {e}")
        await query.edit_message_text(text="❌ Sorry, there was an error processing your request.")

async def handle_trending_content(query, context, content_type):
    """Handle trending videos or popular files."""
    try:
        if content_type == 'video':
            collection = videos_collection
            field = 'is_trending'
            emoji = "🔥"
            title = "trending videos"
        else:
            collection = files_collection
            field = 'is_popular'
            emoji = "⭐"
            title = "popular files"
        
        trending_content = []
        async for doc in collection.find({field: True}):
            trending_content.append(doc)
        
        if trending_content:
            await query.edit_message_text(text=f"{emoji} Here are the {title}:")
            for content in trending_content[:3]:  # Limit to 3 items
                try:
                    if content_type == 'video':
                        sent_message = await context.bot.send_video(
                            chat_id=query.message.chat_id, 
                            video=content['file_id'],
                            protect_content=True
                        )
                    else:
                        caption = f"📁 **{content.get('file_name', 'File')}**\n"
                        if content.get('file_size'):
                            caption += f"📏 Size: {format_file_size(content['file_size'])}\n"
                        if content.get('category'):
                            caption += f"📂 Category: {content['category']}"
                        
                        sent_message = await context.bot.send_document(
                            chat_id=query.message.chat_id,
                            document=content['file_id'],
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            protect_content=True
                        )
                    
                    context.job_queue.run_once(
                        delete_message,
                        300,
                        data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
                    )
                except TelegramError as e:
                    logger.error(f"Error sending {content_type} {content['file_id']}: {e}")
        else:
            await query.edit_message_text(text=f"{emoji} No {title} available at the moment.")
    except Exception as e:
        logger.error(f"Error in {title}: {e}")
        await query.edit_message_text(text=f"❌ Error loading {title}.")

async def handle_browse_categories(query, context):
    """Handle browsing file categories."""
    try:
        # Get unique categories
        pipeline = [
            {'$group': {'_id': '$category', 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}}
        ]
        
        categories = []
        async for doc in files_collection.aggregate(pipeline):
            if doc['_id']:  # Skip null categories
                categories.append((doc['_id'], doc['count']))
        
        if categories:
            keyboard = []
            for i in range(0, len(categories), 2):
                row = []
                cat_name, count = categories[i]
                row.append(InlineKeyboardButton(f"📂 {cat_name} ({count})", callback_data=f'category_{cat_name}'))
                
                if i + 1 < len(categories):
                    cat_name2, count2 = categories[i + 1]
                    row.append(InlineKeyboardButton(f"📂 {cat_name2} ({count2})", callback_data=f'category_{cat_name2}'))
                
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text="📋 **File Categories**\n\nChoose a category to browse:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(text="📂 No file categories available yet.")
    except Exception as e:
        logger.error(f"Error in browse_categories: {e}")
        await query.edit_message_text(text="❌ Error loading categories.")

async def handle_category_files(query, context, category):
    """Handle showing files from a specific category."""
    try:
        files = []
        async for doc in files_collection.find({'category': category}).limit(5):
            files.append(doc)
        
        if files:
            await query.edit_message_text(text=f"📂 **{category}** files:")
            for file_doc in files:
                caption = f"📁 **{file_doc.get('file_name', 'File')}**\n"
                if file_doc.get('file_size'):
                    caption += f"📏 Size: {format_file_size(file_doc['file_size'])}\n"
                caption += f"📂 Category: {category}"
                
                sent_message = await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=file_doc['file_id'],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    protect_content=True
                )
                
                context.job_queue.run_once(
                    delete_message,
                    300,
                    data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
                )
        else:
            await query.edit_message_text(text=f"📂 No files found in **{category}** category.")
    except Exception as e:
        logger.error(f"Error in category_files for {category}: {e}")
        await query.edit_message_text(text="❌ Error loading category files.")

def get_file_category(file_name):
    """Determine file category based on extension."""
    if not file_name:
        return "Other"
    
    extension = file_name.lower().split('.')[-1] if '.' in file_name else ""
    
    categories = {
        'Images': ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg'],
        'Documents': ['pdf', 'doc', 'docx', 'txt', 'rtf', 'odt'],
        'Audio': ['mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a'],
        'Video': ['mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'webm'],
        'Archives': ['zip', 'rar', '7z', 'tar', 'gz', 'bz2'],
        'Spreadsheets': ['xls', 'xlsx', 'csv', 'ods'],
        'Presentations': ['ppt', 'pptx', 'odp'],
        'Code': ['py', 'js', 'html', 'css', 'java', 'cpp', 'c', 'php', 'rb'],
        'Ebooks': ['epub', 'mobi', 'azw', 'azw3', 'fb2']
    }
    
    for category, extensions in categories.items():
        if extension in extensions:
            return category
    
    return "Other"

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles video uploads from users or admin for broadcast/trending."""
    if not update.message:
        logger.error("No message in update")
        return
    
    user_id = update.message.from_user.id
    
    # Handle admin operations first
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') or context.user_data.get('trending_mode'):
            await handle_admin_content(update, context)
            return
    
    video = update.message.video
    if video:
        try:
            # Check if video already exists
            existing_video = await videos_collection.find_one({'file_id': video.file_id})
            if existing_video:
                await update.message.reply_text("This video has already been uploaded.")
                return

            # Add video to collection
            await videos_collection.insert_one({
                'file_id': video.file_id,
                'is_trending': False,
                'upload_timestamp': datetime.datetime.now(),
                'uploaded_by': user_id
            })
            
            # Update user's upload count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'uploaded_videos': 1}},
                upsert=True
            )
            
            # Get counts for response
            total_videos = await videos_collection.count_documents({})
            user_doc = await users_collection.find_one({'user_id': user_id})
            uploaded_videos = user_doc['uploaded_videos'] if user_doc else 0

            await update.message.reply_text(
                f"✅ Video uploaded successfully!\n"
                f"📊 Total videos uploaded by you: {uploaded_videos}\n"
                f"🎥 Total videos in collection: {total_videos}"
            )
            
            logger.info(f"User {user_id} uploaded a video. Total videos: {total_videos}")
            
        except Exception as e:
            logger.error(f"Error uploading video: {e}")
            await update.message.reply_text("❌ Sorry, there was an error uploading your video.")
    else:
        await update.message.reply_text("❌ Please send a valid video file.")

async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles file uploads from users or admin for broadcast/popular."""
    if not update.message:
        logger.error("No message in update")
        return
    
    user_id = update.message.from_user.id
    
    # Handle admin operations first
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') == 'file' or context.user_data.get('popular_file_mode'):
            await handle_admin_content(update, context)
            return
    
    document = update.message.document
    if document:
        try:
            # Check file size limit
            if document.file_size > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"❌ File too large! Maximum size allowed is {format_file_size(MAX_FILE_SIZE)}.\n"
                    f"Your file size: {format_file_size(document.file_size)}"
                )
                return

            # Check if file already exists
            existing_file = await files_collection.find_one({'file_id': document.file_id})
            if existing_file:
                await update.message.reply_text("This file has already been uploaded.")
                return

            # Determine file category
            category = get_file_category(document.file_name)

            # Add file to collection
            await files_collection.insert_one({
                'file_id': document.file_id,
                'file_name': document.file_name,
                'file_size': document.file_size,
                'mime_type': document.mime_type,
                'category': category,
                'is_popular': False,
                'upload_timestamp': datetime.datetime.now(),
                'uploaded_by': user_id
            })
            
            # Update user's upload count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'uploaded_files': 1}},
                upsert=True
            )
            
            # Get counts for response
            total_files = await files_collection.count_documents({})
            user_doc = await users_collection.find_one({'user_id': user_id})
            uploaded_files = user_doc.get('uploaded_files', 0) if user_doc else 0

            await update.message.reply_text(
                f"✅ File uploaded successfully!\n"
                f"📁 File: {document.file_name}\n"
                f"📏 Size: {format_file_size(document.file_size)}\n"
                f"📂 Category: {category}\n"
                f"📊 Total files uploaded by you: {uploaded_files}\n"
                f"📁 Total files in collection: {total_files}"
            )
            
            logger.info(f"User {user_id} uploaded file {document.file_name}. Total files: {total_files}")
            
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            await update.message.reply_text("❌ Sorry, there was an error uploading your file.")
    else:
        await update.message.reply_text("❌ Please send a valid file.")

async def handle_broadcast_setup(query, context):
    """Handle broadcast setup for text, image, video."""
    broadcast_type = query.data.replace('broadcast_', '')
    
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.edit_message_text(text="❌ Access denied.")
        return
    
    context.user_data['broadcast_mode'] = broadcast_type
    
    messages = {
        'text': "📝 **Text Broadcast Mode**\n\nSend me the text message you want to broadcast to all users.",
        'image': "🖼 **Image Broadcast Mode**\n\nSend me the image you want to broadcast to all users.\nYou can include a caption with the image.",
        'video': "🎥 **Video Broadcast Mode**\n\nSend me the video you want to broadcast to all users.\nYou can include a caption with the video."
    }
    
    await query.edit_message_text(
        text=f"{messages[broadcast_type]}\n\nUse /cancel to cancel this operation.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles content (video, photo, text, file) sent by admin for broadcast, trending, or popular."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        return
    
    broadcast_mode = context.user_data.get('broadcast_mode')
    trending_mode = context.user_data.get('trending_mode')
    popular_file_mode = context.user_data.get('popular_file_mode')
    
    # Handle popular file mode
    if popular_file_mode:
        document = update.message.document
        if document:
            try:
                category = get_file_category(document.file_name)
                
                # Add or update file as popular
                result = await files_collection.update_one(
                    {'file_id': document.file_id},
                    {
                        '$set': {
                            'file_name': document.file_name,
                            'file_size': document.file_size,
                            'mime_type': document.mime_type,
                            'category': category,
                            'is_popular': True,
                            'upload_timestamp': datetime.datetime.now(),
                            'uploaded_by': update.message.from_user.id
                        }
                    },
                    upsert=True
                )
                
                await update.message.reply_text("✅ File added to popular list successfully!")
                context.user_data.pop('popular_file_mode', None)
                
            except Exception as e:
                logger.error(f"Error adding popular file: {e}")
                await update.message.reply_text("❌ Error adding file to popular list.")
        else:
            await update.message.reply_text("❌ Please send a file.")
        return
    
    # Handle trending mode
    if trending_mode:
        video = update.message.video
        if video:
            try:
                # Add or update video as trending
                result = await videos_collection.update_one(
                    {'file_id': video.file_id},
                    {
                        '$set': {
                            'is_trending': True,
                            'upload_timestamp': datetime.datetime.now(),
                            'uploaded_by': update.message.from_user.id
                        }
                    },
                    upsert=True
                )
                
                await update.message.reply_text("✅ Video added to trending list successfully!")
                context.user_data.pop('trending_mode', None)
                
            except Exception as e:
                logger.error(f"Error adding trending video: {e}")
                await update.message.reply_text("❌ Error adding video to trending list.")
        else:
            await update.message.reply_text("❌ Please send a video file.")
        return
    
    # Handle broadcast mode
    if not broadcast_mode:
        return
    
    try:
        # Get all users for broadcasting
        all_users = []
        async for doc in users_collection.find({}, {'user_id': 1}):
            all_users.append(doc['user_id'])
        
        if not all_users:
            await update.message.reply_text("❌ No users found to broadcast to.")
            return
        
        success_count = 0
        failed_count = 0
        
        progress_msg = await update.message.reply_text(
            f"📡 Starting broadcast to {len(all_users)} users...\n⏳ Please wait..."
        )
        
        # Handle different broadcast types
        if broadcast_mode == 'text':
            text_to_send = update.message.text
            
            for user_id in all_users:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📢 **Admin Announcement**\n\n{text_to_send}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting text to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)  # Rate limiting
        
        elif broadcast_mode == 'image' and update.message.photo:
            photo = update.message.photo[-1]
            caption = update.message.caption or ""
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting image to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        elif broadcast_mode == 'video' and update.message.video:
            video = update.message.video
            caption = update.message.caption or ""
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=video.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting video to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        elif broadcast_mode == 'file' and update.message.document:
            document = update.message.document
            caption = update.message.caption or ""
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=document.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting file to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        else:
            await update.message.reply_text(
                f"❌ Invalid content type for {broadcast_mode} broadcast.\n"
                f"Please send the correct type of content."
            )
            return
        
        # Update progress message with results
        await progress_msg.edit_text(
            f"📡 **Broadcast Completed!**\n\n"
            f"✅ Successfully sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📊 Total users: {len(all_users)}\n\n"
            f"Broadcast mode: {broadcast_mode.capitalize()}"
        )
        
        context.user_data.pop('broadcast_mode', None)
        
    except Exception as e:
        logger.error(f"Error during broadcast: {e}")
        await update.message.reply_text(
            f"❌ **Broadcast Error**\n\n"
            f"An error occurred during broadcast: {str(e)}"
        )

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels any ongoing admin operation (broadcast, trending add, popular file add)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return
    
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('trending_mode', None)
    context.user_data.pop('popular_file_mode', None)
    
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
    """Shows bot statistics for users or admin."""
    user_id = update.message.from_user.id
    
    try:
        user_doc = await users_collection.find_one({'user_id': user_id})
        
        daily_count = user_doc.get('daily_count', 0) if user_doc else 0
        file_daily_count = user_doc.get('file_daily_count', 0) if user_doc else 0
        uploaded_videos = user_doc.get('uploaded_videos', 0) if user_doc else 0
        uploaded_files = user_doc.get('uploaded_files', 0) if user_doc else 0
        
        video_remaining = max(0, DAILY_LIMIT - daily_count)
        file_remaining = max(0, FILE_DAILY_LIMIT - file_daily_count)
        
        stats_text = f"📊 **Your Stats:**\n"
        stats_text += f"🆔 User ID: `{user_id}`\n\n"
        stats_text += f"🎥 Videos watched today: {daily_count}/{DAILY_LIMIT}\n"
        stats_text += f"⏳ Videos remaining today: {video_remaining}\n"
        stats_text += f"📁 Files downloaded today: {file_daily_count}/{FILE_DAILY_LIMIT}\n"
        stats_text += f"⏳ Files remaining today: {file_remaining}\n\n"
        stats_text += f"📤 Videos uploaded: {uploaded_videos}\n"
        stats_text += f"📎 Files uploaded: {uploaded_files}"

        if ADMIN_ID and user_id == ADMIN_ID:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            total_files = await files_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})
            popular_count = await files_collection.count_documents({'is_popular': True})
            
            stats_text += f"\n\n📊 **Bot Admin Statistics:**\n"
            stats_text += f"👥 Total users: {total_users}\n"
            stats_text += f"🎥 Total videos in collection: {total_videos}\n"
            stats_text += f"📁 Total files in collection: {total_files}\n"
            stats_text += f"🔥 Trending videos: {trending_count}\n"
            stats_text += f"⭐ Popular files: {popular_count}\n"
            stats_text += f"⚙️ Video Daily Limit: {DAILY_LIMIT}\n"
            stats_text += f"⚙️ File Daily Limit: {FILE_DAILY_LIMIT}\n"
            stats_text += f"📏 Max File Size: {format_file_size(MAX_FILE_SIZE)}"

        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ Error loading statistics.")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming photo messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'image':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("📸 Thanks for the photo! Currently, I only support video and file uploads or admin broadcasts.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'text':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("💬 I'm not configured to respond to general text messages yet. Please use the buttons or send videos/files!")

async def post_init(application: Application) -> None:
    """Post-initialization hook to connect to MongoDB."""
    connection_success = await connect_to_mongodb()
    if not connection_success:
        logger.error("Failed to connect to MongoDB. Bot may not function properly.")
        # You could decide to exit here if MongoDB is critical
        # import sys
        # sys.exit(1)

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
    application.add_handler(MessageHandler(filters.Document.ALL, upload_file))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Add a periodic cleanup job (optional)
    if application.job_queue:
        application.job_queue.run_repeating(
            lambda context: logger.info("Bot is running..."),
            interval=3600,  # Every hour
            first=3600,
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

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
