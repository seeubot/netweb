import os
import random
import logging
import datetime
import asyncio
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

# Store video file IDs and message IDs for deletion
video_storage = []
sent_messages = []  # Store message info for deletion

async def fetch_videos_from_channel(context: ContextTypes.DEFAULT_TYPE):
    """Fetch videos from the source channel"""
    if not SOURCE_CHANNEL:
        logger.warning("SOURCE_CHANNEL not configured")
        return
    
    try:
        # Get chat info
        chat = await context.bot.get_chat(SOURCE_CHANNEL)
        logger.info(f"Fetching videos from channel: {chat.title}")
        
        # In a real implementation, you would need to store video IDs when they're posted
        # For now, we'll use the stored videos or add some sample ones for testing
        if not video_storage:
            logger.info("No videos in storage. Add some videos to the source channel and upload them via the bot.")
            
    except TelegramError as e:
        logger.error(f"Error accessing source channel {SOURCE_CHANNEL}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    
    # Send user ID to user for reference
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot sends random videos from our collection\\.\n"
    welcome_message += "Use the buttons below to get videos or upload new ones\\."

    keyboard = [
        [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
        [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
        [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
    ]
    
    # Add admin panel for admins
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
    
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
                'last_reset': datetime.date.today()
            }
        
        user_data = context.bot_data['users'][user_id]
        
        # Reset daily count if it's a new day
        if user_data['last_reset'] != datetime.date.today():
            user_data['daily_count'] = 0
            user_data['last_reset'] = datetime.date.today()
        
        # Check daily limit
        if user_data['daily_count'] >= DAILY_LIMIT:
            await query.edit_message_text(
                text=f"⏰ You have reached your daily limit of {DAILY_LIMIT} videos.\n"
                     f"Please try again tomorrow!"
            )
            return

        # Get available videos
        if not video_storage:
            await query.edit_message_text(text="📹 No videos available at the moment.\nPlease upload some videos first!")
            return

        try:
            # Select random video
            random_video = random.choice(video_storage)
            await query.edit_message_text(text="📹 Here is your random video:")
            
            # Send video
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id, 
                video=random_video
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
                data={
                    'chat_id': query.message.chat_id, 
                    'message_id': sent_message.message_id
                }
            )

            # Increment daily count
            user_data['daily_count'] += 1
            
            # Inform user about remaining videos
            remaining = DAILY_LIMIT - user_data['daily_count']
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
            logger.error(f"Error sending video: {e}")
            await query.edit_message_text(text="❌ Sorry, there was an error sending the video.")

    elif query.data == 'upload_video':
        await query.edit_message_text(text="📤 Please send me the video you want to upload.")

    elif query.data == 'trending_videos':
        trending_videos = []
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_videos = [line.strip() for line in file.readlines() if line.strip()]

        if trending_videos:
            await query.edit_message_text(text="🔥 Here are the trending videos:")
            for video_id in trending_videos[:3]:  # Limit to 3 trending videos
                try:
                    sent_message = await context.bot.send_video(
                        chat_id=query.message.chat_id, 
                        video=video_id
                    )
                    
                    # Schedule deletion for trending videos too
                    context.job_queue.run_once(
                        delete_message, 
                        300,
                        data={
                            'chat_id': query.message.chat_id, 
                            'message_id': sent_message.message_id
                        }
                    )
                    
                except TelegramError as e:
                    logger.error(f"Error sending trending video {video_id}: {e}")
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
        total_users = len(context.bot_data.get('users', {}))
        total_videos = len(video_storage)
        
        trending_count = 0
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_count = len([line for line in file.readlines() if line.strip()])
        
        # Calculate active users (users who used bot today)
        today = datetime.date.today()
        active_today = 0
        for user_data in context.bot_data.get('users', {}).values():
            if user_data.get('last_reset') == today and user_data.get('daily_count', 0) > 0:
                active_today += 1
        
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
            
        trending_videos = []
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_videos = [line.strip() for line in file.readlines() if line.strip()]
        
        trending_keyboard = [
            [InlineKeyboardButton("➕ Add Trending", callback_data='add_trending')],
            [InlineKeyboardButton("🗑 Clear All", callback_data='clear_trending')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(trending_keyboard)
        
        await query.edit_message_text(
            text=f"🔥 **Trending Management**\n\n"
                 f"Current trending videos: {len(trending_videos)}\n\n"
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
            if os.path.exists('trending_videos.txt'):
                os.remove('trending_videos.txt')
            
            await query.edit_message_text(
                text="✅ All trending videos cleared successfully!"
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

async def handle_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast content from admin"""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        return
    
    broadcast_mode = context.user_data.get('broadcast_mode')
    trending_mode = context.user_data.get('trending_mode')
    
    if trending_mode:
        # Handle trending video addition
        video = update.message.video
        if video:
            try:
                with open('trending_videos.txt', 'a') as file:
                    file.write(f"{video.file_id}\n")
                
                await update.message.reply_text("✅ Video added to trending list successfully!")
                context.user_data.pop('trending_mode', None)
                
            except Exception as e:
                logger.error(f"Error adding trending video: {e}")
                await update.message.reply_text("❌ Error adding video to trending list.")
        else:
            await update.message.reply_text("❌ Please send a video file.")
        return
    
    if not broadcast_mode:
        return
    
    # Get all users
    all_users = list(context.bot_data.get('users', {}).keys())
    
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
                        parse_mode=ParseMode.MARKDOWN
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
                        parse_mode=ParseMode.MARKDOWN
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

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Check if update.message exists
    if not update.message:
        logger.error("No message in update")
        return
    
    user_id = update.message.from_user.id
    
    # Check if admin is in broadcast mode
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') or context.user_data.get('trending_mode'):
            await handle_broadcast_content(update, context)
            return
    
    if 'users' not in context.bot_data:
        context.bot_data['users'] = {}
    
    if user_id not in context.bot_data['users']:
        context.bot_data['users'][user_id] = {'uploaded_videos': 0}
    
    user_data = context.bot_data['users'][user_id]

    video = update.message.video
    if video:
        # Store video file ID (no forwarding to channels)
        video_storage.append(video.file_id)
        
        user_data['uploaded_videos'] = user_data.get('uploaded_videos', 0) + 1

        await update.message.reply_text(
            f"✅ Video uploaded successfully!\n"
            f"📊 Total videos uploaded: {user_data['uploaded_videos']}\n"
            f"📹 Total videos in collection: {len(video_storage)}"
        )
        
        logger.info(f"User {user_id} uploaded a video. Total videos: {len(video_storage)}")
    else:
        await update.message.reply_text("❌ Please send a valid video file.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages (for broadcast mode)"""
    if not update.message:
        return
    
    user_id = update.message.from_user.id
    
    # Check if admin is in broadcast mode
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') == 'text':
            await handle_broadcast_content(update, context)
            return
    
    # For regular users, you might want to add other text handling here
    # For now, we'll just ignore regular text messages

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages (for broadcast mode)"""
    if not update.message:
        return
    
    user_id = update.message.from_user.id
    
    # Check if admin is in broadcast mode
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') == 'image':
            await handle_broadcast_content(update, context)
            return
    
    # For regular users, you might want to handle photo uploads here
    await update.message.reply_text("📷 Photo received! Currently, only video uploads are supported for the collection.")

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a message after the specified time"""
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
    """Clean up old messages that weren't deleted properly"""
    current_time = datetime.datetime.now()
    messages_to_remove = []
    
    for msg_info in sent_messages:
        # If message is older than 10 minutes, try to delete it
        if (current_time - msg_info['timestamp']).total_seconds() > 600:
            try:
                await context.bot.delete_message(
                    chat_id=msg_info['chat_id'],
                    message_id=msg_info['message_id']
                )
                logger.info(f"Cleanup: deleted old message {msg_info['message_id']}")
            except TelegramError as e:
                logger.error(f"Cleanup: error deleting message {msg_info['message_id']}: {e}")
            
            messages_to_remove.append(msg_info)
    
    # Remove processed messages from the list
    for msg_info in messages_to_remove:
        sent_messages.remove(msg_info)

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any ongoing admin operation"""
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

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: broadcast message to all users (legacy command)"""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message:
        await update.message.reply_text("Please reply to a message to broadcast.")
        return

    # Get all users who have interacted with the bot
    all_users = list(context.bot_data.get('users', {}).keys())
    
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
    """Admin only: mark video as trending (legacy command)"""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return

    message = update.message.reply_to_message
    if not message or not message.video:
        await update.message.reply_text("Please reply to a video message to mark it as trending.")
        return

    try:
        with open('trending_videos.txt', 'a') as file:
            file.write(f"{message.video.file_id}\n")
        
        await update.message.reply_text("✅ Video marked as trending.")
    except Exception as e:
        logger.error(f"Error marking video as trending: {e}")
        await update.message.reply_text("❌ Error marking video as trending.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics"""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        # Show user stats
        user_id = update.message.from_user.id
        user_data = context.bot_data.get('users', {}).get(user_id, {})
        
        daily_count = user_data.get('daily_count', 0)
        uploaded_videos = user_data.get('uploaded_videos', 0)
        remaining = max(0, DAILY_LIMIT - daily_count)
        
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics"""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        # Show user stats
        user_id = update.message.from_user.id
        user_data = context.bot_data.get('users', {}).get(user_id, {})
        
        daily_count = user_data.get('daily_count', 0)
        uploaded_videos = user_data.get('uploaded_videos', 0)
        remaining = max(0, DAILY_LIMIT - daily_count)
        
        stats_text = f"📊 Your Stats:\n"
        stats_text += f"🆔 User ID: `{user_id}`\n"
        stats_text += f"📹 Videos watched today: {daily_count}/{DAILY_LIMIT}\n"
        stats_text += f"⏳ Remaining today: {remaining}\n"
        stats_text += f"📤 Videos uploaded: {uploaded_videos}"
        
        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
    else:
        # Show admin stats
        total_users = len(context.bot_data.get('users', {}))
        total_videos = len(video_storage)
        
        trending_count = 0
        if os.path.exists('trending_videos.txt'):
            with open('trending_videos.txt', 'r') as file:
                trending_count = len([line for line in file.readlines() if line.strip()])
        
        stats_text = f"📊 Bot Statistics:\n"
        stats_text += f"👥 Total users: {total_users}\n"
        stats_text += f"📹 Total videos: {total_videos}\n"
        stats_text += f"🔥 Trending videos: {trending_count}\n"
        stats_text += f"⚙️ Daily limit: {DAILY_LIMIT}"
        
        await update.message.reply_text(stats_text)

def main() -> None:
    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    
    application = Application.builder().token(API_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel_operation))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CommandHandler("broadcast", broadcast))  # Keep old broadcast command as backup
    application.add_handler(CommandHandler("trending", trending))

    # Schedule cleanup job to run every hour
    application.job_queue.run_repeating(cleanup_old_messages, interval=3600, first=3600)

    # Run the bot in polling mode
    logger.info("Starting bot in polling mode...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
