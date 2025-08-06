import os
import random
import logging
import datetime
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, JobQueue
from telegram.error import TelegramError
from dotenv import load_dotenv
import pymongo # Import pymongo

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
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')  # Channel to fetch videos from and broadcast to
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))

# Webhook configuration for Koyeb
WEBHOOK_URL = os.getenv('WEBHOOK_URL') # Koyeb will provide this as an environment variable
PORT = int(os.getenv('PORT', 8000)) # Default Koyeb port is often 8000
LISTEN_ADDRESS = '0.0.0.0' # Listen on all available interfaces

# MongoDB Connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI not found in environment variables. Please set it.")
    exit(1) # Exit if no MongoDB URI is provided

try:
    client = pymongo.MongoClient(MONGODB_URI)
    db = client.get_database("telegram_bot_db") # Name your database
    users_collection = db.users
    videos_collection = db.videos
    trending_videos_collection = db.trending_videos
    logger.info("Successfully connected to MongoDB.")
except pymongo.errors.ConnectionFailure as e:
    logger.error(f"Could not connect to MongoDB: {e}")
    exit(1)

# Store message info for deletion (this is for Telegram messages, not database entries)
sent_messages = [] 

async def fetch_videos_from_channel(context: ContextTypes.DEFAULT_TYPE):
    """Fetch videos from the source channel (placeholder for future implementation)"""
    if not SOURCE_CHANNEL:
        logger.warning("SOURCE_CHANNEL not configured")
        return
    
    try:
        chat = await context.bot.get_chat(SOURCE_CHANNEL)
        logger.info(f"Fetching videos from channel: {chat.title}")
        
        # In a real implementation, you would need to store video IDs when they're posted
        # For now, we'll rely on user uploads
        if await videos_collection.count_documents({}) == 0:
            logger.info("No videos in database. Users need to upload some videos.")
            
    except TelegramError as e:
        logger.error(f"Error accessing source channel {SOURCE_CHANNEL}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and main menu keyboard to the user."""
    user = update.effective_user
    user_id = user.id
    
    # Ensure user data exists in DB, initialize if not
    user_doc = users_collection.find_one({'user_id': user_id})
    if not user_doc:
        users_collection.insert_one({
            'user_id': user_id,
            'daily_count': 0,
            'last_reset': datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
            'uploaded_videos': 0
        })
        logger.info(f"New user {user_id} registered in MongoDB.")

    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot sends random videos from our collection\\.\n"
    welcome_message += "Use the buttons below to get videos or upload new ones\\."

    keyboard = [
        [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
        [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
        [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
    ]
    
    # Add admin panel for admins (only for the specified ADMIN_ID)
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
        
        user_doc = users_collection.find_one({'user_id': user_id})
        if not user_doc:
            # This should ideally not happen if start command is used, but as a fallback
            users_collection.insert_one({
                'user_id': user_id,
                'daily_count': 0,
                'last_reset': datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
                'uploaded_videos': 0
            })
            user_doc = users_collection.find_one({'user_id': user_id}) # Re-fetch after insert

        # Convert last_reset from datetime to date for comparison
        last_reset_date = user_doc['last_reset'].date() if isinstance(user_doc['last_reset'], datetime.datetime) else user_doc['last_reset']
        
        # Reset daily count if it's a new day
        if last_reset_date != datetime.date.today():
            user_doc['daily_count'] = 0
            user_doc['last_reset'] = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            users_collection.update_one({'user_id': user_id}, {'$set': {'daily_count': 0, 'last_reset': user_doc['last_reset']}})

        # Check daily limit ONLY for non-admin users
        if user_id != ADMIN_ID and user_doc['daily_count'] >= DAILY_LIMIT:
            await query.edit_message_text(
                text=f"⏰ You have reached your daily limit of {DAILY_LIMIT} videos.\n"
                     f"Please try again tomorrow!"
            )
            return

        # Get available videos from MongoDB
        video_docs = list(videos_collection.find({}))
        if not video_docs:
            await query.edit_message_text(text="📹 No videos available at the moment.\nPlease upload some videos first!")
            return

        try:
            # Select random video
            random_video_doc = random.choice(video_docs)
            random_video_file_id = random_video_doc['file_id']
            await query.edit_message_text(text="📹 Here is your random video:")
            
            # Send video with content protection
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id, 
                video=random_video_file_id,
                protect_content=True # Disable forwarding and saving
            )
            
            # Store message info for auto-deletion
            sent_messages.append({
                'chat_id': query.message.chat_id,
                'message_id': sent_message.message_id,
                'timestamp': datetime.datetime.now()
            })
            
            # Schedule message deletion after 5 minutes
            context.job_queue.run_once(
                delete_message,
                300,  # 5 minutes
                data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
            )

            # Increment daily count ONLY for non-admin users
            if user_id != ADMIN_ID:
                users_collection.update_one({'user_id': user_id}, {'$inc': {'daily_count': 1}})
                user_doc['daily_count'] += 1 # Update local copy for immediate feedback
            
            # Inform user about remaining videos (only for non-admin)
            if user_id != ADMIN_ID:
                remaining = DAILY_LIMIT - user_doc['daily_count']
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
            else: # Admin confirmation
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="✅ Video sent! (Admin: No daily limit applied to you)."
                )
                
        except Exception as e:
            logger.error(f"Error sending video: {e}")
            await query.edit_message_text(text="❌ Sorry, there was an error sending the video.")

    elif query.data == 'upload_video':
        await query.edit_message_text(text="📤 Please send me the video you want to upload.")

    elif query.data == 'trending_videos':
        trending_video_docs = list(trending_videos_collection.find({}))

        if trending_video_docs:
            await query.edit_message_text(text="🔥 Here are the trending videos:")
            for video_doc in trending_video_docs[:3]:  # Limit to 3 trending videos
                try:
                    sent_message = await context.bot.send_video(
                        chat_id=query.message.chat_id, 
                        video=video_doc['file_id'],
                        protect_content=True # Disable forwarding and saving
                    )
                    
                    # Schedule deletion for trending videos too
                    context.job_queue.run_once(
                        delete_message,
                        300,  # 5 minutes
                        data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
                    )
                    
                except TelegramError as e:
                    logger.error(f"Error sending trending video {video_doc['file_id']}: {e}")
        else:
            await query.edit_message_text(text="📹 No trending videos available at the moment.")

    elif query.data == 'admin_panel':
        # Check if user is admin
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied. Admin only.")
            return
            
        admin_keyboard = [
            [InlineKeyboardButton("📡 Broadcast Message", callback_data='broadcast_menu')],
            [InlineKeyboardButton("📊 Bot Statistics", callback_data='admin_stats')],
            [InlineKeyboardButton("🔥 Manage Trending", callback_data='manage_trending')],
            [InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(admin_keyboard)
        
        await query.edit_message_text(
            text="🛠 **Admin Panel**\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'broadcast_menu':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        broadcast_keyboard = [
            [InlineKeyboardButton("📝 Text Message", callback_data='broadcast_text')],
            [InlineKeyboardButton("🖼 Image Broadcast", callback_data='broadcast_image')],
            [InlineKeyboardButton("🎥 Video Broadcast", callback_data='broadcast_video')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(broadcast_keyboard)
        
        await query.edit_message_text(
            text="📡 **Broadcast Menu**\n\n"
                 "Choose the type of content to broadcast:\n\n"
                 "• **Text**: Send a text message to all users\n"
                 "• **Image**: Send an image to all users\n"
                 "• **Video**: Send a video to all users",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'broadcast_text':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'text'
        await query.edit_message_text(
            text="📝 **Text Broadcast Mode**\n\n"
                 "Send me the text message you want to broadcast to all users.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'broadcast_image':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'image'
        await query.edit_message_text(
            text="🖼 **Image Broadcast Mode**\n\n"
                 "Send me the image you want to broadcast to all users.\n"
                 "You can include a caption with the image.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'broadcast_video':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'video'
        await query.edit_message_text(
            text="🎥 **Video Broadcast Mode**\n\n"
                 "Send me the video you want to broadcast to all users.\n"
                 "You can include a caption with the video.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'admin_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        # Show admin statistics
        total_users = users_collection.count_documents({})
        total_videos = videos_collection.count_documents({})
        trending_count = trending_videos_collection.count_documents({})
        
        # Calculate active users (users who used bot today)
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        active_today = users_collection.count_documents({'last_reset': today, 'daily_count': {'$gt': 0}})
        
        stats_text = f"📊 **Bot Statistics**\n\n"
        stats_text += f"👥 Total users: {total_users}\n"
        stats_text += f"🔥 Active today: {active_today}\n"
        stats_text += f"📹 Total videos: {total_videos}\n"
        stats_text += f"⭐ Trending videos: {trending_count}\n"
        stats_text += f"⚙️ Daily limit: {DAILY_LIMIT}\n"
        stats_text += f"🤖 Auto-delete: 5 minutes"
        
        back_keyboard = [[InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]]
        reply_markup = InlineKeyboardMarkup(back_keyboard)
        
        await query.edit_message_text(
            text=stats_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'manage_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        trending_count = trending_videos_collection.count_documents({})
        
        trending_keyboard = [
            [InlineKeyboardButton("➕ Add Trending", callback_data='add_trending')],
            [InlineKeyboardButton("🗑 Clear All", callback_data='clear_trending')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(trending_keyboard)
        
        await query.edit_message_text(
            text=f"🔥 **Trending Management**\n\n"
                 f"Current trending videos: {trending_count}\n\n"
                 f"• Add new trending videos\n"
                 f"• Clear all trending videos",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'add_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['trending_mode'] = True
        await query.edit_message_text(
            text="🔥 **Add Trending Video**\n\n"
                 "Send me a video to add to trending list.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'clear_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            result = trending_videos_collection.delete_many({})
            await query.edit_message_text(
                text=f"✅ Cleared {result.deleted_count} trending videos successfully!"
            )
        except Exception as e:
            logger.error(f"Error clearing trending videos: {e}")
            await query.edit_message_text(
                text="❌ Error clearing trending videos."
            )

    elif query.data == 'back_to_main':
        # Return to main menu
        user = query.from_user
        welcome_message = f"Welcome back, {user.mention_markdown_v2()}\\!\n\n"
        welcome_message += f"Your User ID: `{user.id}`\n\n"
        welcome_message += "This bot sends random videos from our collection\\.\n"
        welcome_message += "Use the buttons below to get videos or upload new ones\\."

        keyboard = [
            [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
            [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
            [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
        ]
        
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles video uploads from users or admin for broadcast/trending."""
    # Check if update.message exists
    if not update.message:
        logger.error("No message in update")
        return
    
    user_id = update.message.from_user.id
    
    # Check if admin is in broadcast or trending mode
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') or context.user_data.get('trending_mode'):
            await handle_admin_content(update, context) 
            return
    
    video = update.message.video
    if video:
        try:
            # Store video file ID in MongoDB
            videos_collection.insert_one({
                'file_id': video.file_id,
                'uploaded_by': user_id,
                'uploaded_at': datetime.datetime.now()
            })
            
            # Update user's uploaded videos count in MongoDB
            users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'uploaded_videos': 1}},
                upsert=True # Create user if not exists (should already exist from /start)
            )
            
            total_videos_in_collection = videos_collection.count_documents({})

            await update.message.reply_text(
                f"✅ Video uploaded successfully!\n"
                f"📊 Total videos you uploaded: {users_collection.find_one({'user_id': user_id}).get('uploaded_videos', 0)}\n"
                f"📹 Total videos in collection: {total_videos_in_collection}"
            )
            
            logger.info(f"User {user_id} uploaded a video. Total videos in DB: {total_videos_in_collection}")

            # --- New: Broadcast to SOURCE_CHANNEL ---
            if SOURCE_CHANNEL:
                try:
                    await context.bot.send_video(
                        chat_id=SOURCE_CHANNEL,
                        video=video.file_id,
                        caption=f"New video uploaded by a user! 📹\n\n"
                                f"Total videos: {total_videos_in_collection}",
                        protect_content=True # Disable forwarding and saving
                    )
                    logger.info(f"Video {video.file_id} broadcasted to channel {SOURCE_CHANNEL}")
                except TelegramError as e:
                    logger.error(f"Error broadcasting video to SOURCE_CHANNEL {SOURCE_CHANNEL}: {e}")
                    await update.message.reply_text("⚠️ Warning: Could not broadcast video to the channel.")
            else:
                logger.warning("SOURCE_CHANNEL is not set, skipping channel broadcast.")
            # --- End New Broadcast ---

        except Exception as e:
            logger.error(f"Error uploading video to MongoDB: {e}")
            await update.message.reply_text("❌ Sorry, there was an error uploading the video.")
    else:
        await update.message.reply_text("❌ Please send a valid video file.")

async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles content (video, photo, text) sent by admin for broadcast or trending."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        return
    
    broadcast_mode = context.user_data.get('broadcast_mode')
    trending_mode = context.user_data.get('trending_mode')
    
    if trending_mode:
        # Handle trending video addition
        video = update.message.video
        if video:
            try:
                # Store trending video file ID in MongoDB
                trending_videos_collection.insert_one({
                    'file_id': video.file_id,
                    'added_by': update.message.from_user.id,
                    'added_at': datetime.datetime.now()
                })
                
                await update.message.reply_text("✅ Video added to trending list successfully!")
                context.user_data.pop('trending_mode', None)
                
            except Exception as e:
                logger.error(f"Error adding trending video to MongoDB: {e}")
                await update.message.reply_text("❌ Error adding video to trending list.")
        else:
            await update.message.reply_text("❌ Please send a video file.")
        return
    
    if not broadcast_mode:
        return
    
    # Get all users from MongoDB
    all_users = [doc['user_id'] for doc in users_collection.find({}, {'user_id': 1})]
    
    if not all_users:
        await update.message.reply_text("❌ No users found to broadcast to.")
        return
    
    success_count = 0
    failed_count = 0
    
    # Show progress message
    progress_msg = await update.message.reply_text(
        f"📡 Starting broadcast to {len(all_users)} users...\n⏳ Please wait..."
    )
    
    try:
        if broadcast_mode == 'text':
            # Broadcast text message
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
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.05)
        
        elif broadcast_mode == 'image' and update.message.photo:
            # Broadcast image
            photo = update.message.photo[-1]  # Get highest resolution
            caption = update.message.caption or ""
            
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True # Disable forwarding and saving
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting image to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        elif broadcast_mode == 'video' and update.message.video:
            # Broadcast video
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
                        protect_content=True # Disable forwarding and saving
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting video to user {user_id}: {e}")
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
        
        # Clear broadcast mode
        context.user_data.pop('broadcast_mode', None)
        
    except Exception as e:
        logger.error(f"Error during broadcast: {e}")
        await progress_msg.edit_text(
            f"❌ **Broadcast Error**\n\n"
            f"An error occurred during broadcast: {str(e)}"
        )

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels any ongoing admin operation (broadcast, trending add)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return
    
    # Clear any ongoing operations
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('trending_mode', None)
    
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

async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodically cleans up old message references from `sent_messages` list."""
    current_time = datetime.datetime.now()
    messages_to_remove = []
    
    for msg_info in sent_messages:
        # If message is older than 10 minutes, mark for removal
        if (current_time - msg_info['timestamp']).total_seconds() > 600: # 10 minutes * 60 seconds
            messages_to_remove.append(msg_info)
    
    # Remove processed messages from the list
    for msg_info in messages_to_remove:
        if msg_info in sent_messages:
            sent_messages.remove(msg_info)
    
    logger.info(f"Cleanup: removed {len(messages_to_remove)} old message references")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: broadcast message to all users (legacy command, now handled by button menu)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message:
        await update.message.reply_text("Please reply to a message to broadcast.")
        return

    # Get all users who have interacted with the bot from MongoDB
    all_users = [doc['user_id'] for doc in users_collection.find({}, {'user_id': 1})]
    
    if not all_users:
        await update.message.reply_text("No users found in database.")
        return

    success_count = 0
    failed_count = 0

    for user_id in all_users:
        try:
            if message.photo:
                await context.bot.send_photo(
                    chat_id=user_id, 
                    photo=message.photo[-1].file_id, 
                    caption=message.caption,
                    protect_content=True # Disable forwarding and saving
                )
            elif message.video:
                await context.bot.send_video(
                    chat_id=user_id, 
                    video=message.video.file_id, 
                    caption=message.caption,
                    protect_content=True # Disable forwarding and saving
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

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: mark video as trending (legacy command, now handled by button menu)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message or not message.video:
        await update.message.reply_text("Please reply to a video message to mark it as trending.")
        return

    try:
        # Store trending video file ID in MongoDB
        trending_videos_collection.insert_one({
            'file_id': message.video.file_id,
            'added_by': update.message.from_user.id,
            'added_at': datetime.datetime.now()
        })
        
        await update.message.reply_text("✅ Video marked as trending.")
    except Exception as e:
        logger.error(f"Error marking video as trending: {e}")
        await update.message.reply_text("❌ Error marking video as trending.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows bot statistics for users or admin."""
    user_id = update.message.from_user.id
    user_doc = users_collection.find_one({'user_id': user_id})
    
    daily_count = user_doc.get('daily_count', 0) if user_doc else 0
    uploaded_videos = user_doc.get('uploaded_videos', 0) if user_doc else 0
    
    stats_text = f"📊 Your Stats:\n"
    stats_text += f"🆔 User ID: `{user_id}`\n"
    stats_text += f"📤 Videos uploaded: {uploaded_videos}"

    if ADMIN_ID and user_id == ADMIN_ID:
        # Admin-specific stats
        total_users = users_collection.count_documents({})
        total_videos = videos_collection.count_documents({})
        trending_count = trending_videos_collection.count_documents({})
        
        stats_text += f"\n\n📊 **Bot Admin Statistics:**\n"
        stats_text += f"👥 Total users: {total_users}\n"
        stats_text += f"📹 Total videos in collection: {total_videos}\n"
        stats_text += f"🔥 Trending videos: {trending_count}\n"
        stats_text += f"⚙️ Global Daily Limit for users: {DAILY_LIMIT}\n"
        stats_text += f"ℹ️ **Admin: You have no daily video limit.**"
    else:
        remaining = max(0, DAILY_LIMIT - daily_count)
        stats_text += f"\n📹 Videos watched today: {daily_count}/{DAILY_LIMIT}\n"
        stats_text += f"⏳ Remaining today: {remaining}"


    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming photo messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'image':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("📸 Thanks for the photo! Currently, I only support video uploads or admin broadcasts.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'text':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("💬 I'm not configured to respond to general text messages yet. Please use the buttons or send a video!")


# Global application instance for Gunicorn
application = None

def main() -> None:
    """Starts the bot and sets up all handlers."""
    global application # Declare global to assign to it

    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    
    # Webhook configuration for Koyeb
    # These variables are expected to be set in Koyeb environment
    WEBHOOK_URL = os.getenv('WEBHOOK_URL') 
    PORT = int(os.getenv('PORT', 8000)) 
    LISTEN_ADDRESS = '0.0.0.0' 

    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found in environment variables. Webhook deployment requires this.")
        return
    
    # Build the application with JobQueue enabled
    application = Application.builder().token(API_TOKEN).job_queue(JobQueue()).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel_operation))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CommandHandler("broadcast", broadcast_command))  # Legacy broadcast command
    application.add_handler(CommandHandler("trending", trending_command)) # Legacy trending command

    # Schedule cleanup job to run every hour
    application.job_queue.run_repeating(cleanup_old_messages, interval=3600, first=3600)

    # Start the bot in webhook mode
    logger.info(f"Starting bot in webhook mode on {LISTEN_ADDRESS}:{PORT}...")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")
    logger.info("Auto-delete feature: Enabled (5 minutes for sent videos)")
    logger.info("Background cleanup: Enabled (every hour for message references)")

    # Set the webhook
    application.run_webhook(
        listen=LISTEN_ADDRESS,
        port=PORT,
        url_path="", # Empty url_path means the root path
        webhook_url=WEBHOOK_URL
    )

if __name__ == '__main__':
    main()

