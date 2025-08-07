import os
import uuid
import asyncio
import mimetypes
import json
import re
import logging
from datetime import datetime
from typing import Dict, List, Optional
import threading
import signal
import sys
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import TelegramError
from flask import Flask, Response, abort, jsonify, request, render_template_string
import requests
from pymongo import MongoClient
import pymongo.errors

# Configure logging for production
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Suppress noisy logs
logging.getLogger('pymongo').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

# Configuration with defaults
BOT_TOKEN = os.getenv('BOT_TOKEN')
STORAGE_CHANNEL_ID = os.getenv('STORAGE_CHANNEL_ID')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb+srv://food:food@food.1jskkt3.mongodb.net/?retryWrites=true&w=majority&appName=food')
DB_NAME = os.getenv('MONGO_DB_NAME', 'netflix_bot_db')
PORT = int(os.getenv('PORT', 8080))
MAX_FILE_SIZE = 4000 * 1024 * 1024  # 4GB

# Global state
app_state = {
    'mongo_client': None,
    'db': None,
    'files_collection': None,
    'content_collection': None,
    'bot_app': None,
    'webhook_set': False,
    'shutdown': False
}

# Supported formats
SUPPORTED_VIDEO_FORMATS = {
    'mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'webm', 'm4v',
    'mpg', 'mpeg', 'ogv', '3gp', 'rm', 'rmvb', 'asf', 'divx'
}

def get_koyeb_domain():
    """Get the Koyeb domain from environment"""
    domain = os.getenv('KOYEB_PUBLIC_DOMAIN')
    if not domain:
        # Try alternative environment variables
        domain = os.getenv('KOYEB_DOMAIN') or os.getenv('PUBLIC_DOMAIN')
    
    if not domain:
        logger.warning("No Koyeb domain found in environment variables")
        return None
    return domain

def is_video_file(filename):
    """Check if file is a supported video format"""
    if not filename or '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in SUPPORTED_VIDEO_FORMATS

def get_video_mime_type(filename):
    """Get MIME type for video file"""
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type and mime_type.startswith('video/'):
        return mime_type
    
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    mime_map = {
        'mp4': 'video/mp4', 'avi': 'video/x-msvideo', 'mkv': 'video/x-matroska',
        'mov': 'video/quicktime', 'wmv': 'video/x-ms-wmv', 'flv': 'video/x-flv',
        'webm': 'video/webm', 'm4v': 'video/mp4', 'mpg': 'video/mpeg',
        'mpeg': 'video/mpeg', 'ogv': 'video/ogg', '3gp': 'video/3gpp'
    }
    return mime_map.get(ext, 'video/mp4')

async def initialize_mongodb():
    """Initialize MongoDB connection with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"Connecting to MongoDB (attempt {attempt + 1}/{max_retries})")
            
            client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000,
                maxPoolSize=10,
                retryWrites=True
            )
            
            # Test connection
            client.admin.command('ping')
            
            db = client[DB_NAME]
            files_collection = db['files']
            content_collection = db['content']
            
            # Create indexes
            try:
                files_collection.create_index([('user_id', 1)], background=True)
                content_collection.create_index([('added_by', 1), ('type', 1)], background=True)
                content_collection.create_index([('type', 1)], background=True)
            except Exception as e:
                logger.warning(f"Index creation warning: {e}")
            
            app_state.update({
                'mongo_client': client,
                'db': db,
                'files_collection': files_collection,
                'content_collection': content_collection
            })
            
            logger.info("✅ MongoDB connected successfully!")
            return True
            
        except Exception as e:
            logger.error(f"MongoDB connection attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error("❌ All MongoDB connection attempts failed")
                return False
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    return False

async def initialize_telegram_bot():
    """Initialize Telegram bot application"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not provided")
        return False
    
    try:
        # Create bot application
        bot_app = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(CommandHandler("library", library_command))
        bot_app.add_handler(CommandHandler("frontend", frontend_command))
        bot_app.add_handler(CommandHandler("stats", stats_command))
        bot_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video_file))
        bot_app.add_handler(CallbackQueryHandler(handle_categorization))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_metadata_input))
        
        app_state['bot_app'] = bot_app
        logger.info("✅ Telegram bot initialized successfully!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Telegram bot initialization failed: {e}")
        return False

async def setup_webhook():
    """Set up Telegram webhook"""
    domain = get_koyeb_domain()
    if not domain or not BOT_TOKEN:
        logger.error("❌ Missing domain or bot token for webhook setup")
        return False
    
    webhook_url = f"https://{domain}/telegram-webhook"
    
    try:
        set_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        payload = {
            "url": webhook_url,
            "drop_pending_updates": True,
            "allowed_updates": ["message", "callback_query"]
        }
        
        response = requests.post(set_url, json=payload, timeout=15)
        result = response.json()
        
        if response.status_code == 200 and result.get('ok'):
            app_state['webhook_set'] = True
            logger.info(f"✅ Webhook set successfully: {webhook_url}")
            return True
        else:
            logger.error(f"❌ Webhook setup failed: {result}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error setting webhook: {e}")
        return False

# Flask application
# RENAMED from flask_app to app to satisfy Gunicorn's default behavior
app = Flask(__name__)
app.config.update({
    'JSON_SORT_KEYS': False,
    'JSONIFY_PRETTYPRINT_REGULAR': False
})

# Modern Netflix-style frontend
FRONTEND_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamFlix - Your Personal Netflix</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            background: linear-gradient(135deg, #0f0f0f 0%, #1a1a1a 100%);
            color: white; 
            min-height: 100vh;
        }
        .navbar { 
            background: rgba(0,0,0,0.9); 
            backdrop-filter: blur(10px);
            padding: 1rem 2rem; 
            position: fixed; 
            top: 0; 
            width: 100%; 
            z-index: 1000;
            border-bottom: 1px solid rgba(229, 9, 20, 0.3);
        }
        .navbar h1 { 
            color: #e50914; 
            font-size: 2rem; 
            font-weight: 700;
            text-shadow: 0 2px 10px rgba(229, 9, 20, 0.5);
        }
        .container { 
            max-width: 1400px; 
            margin: 0 auto; 
            padding: 100px 2rem 2rem; 
        }
        .stats-bar {
            display: flex;
            justify-content: center;
            gap: 3rem;
            margin: 2rem 0;
            padding: 1.5rem;
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            backdrop-filter: blur(10px);
        }
        .stat-item {
            text-align: center;
            padding: 0.5rem;
        }
        .stat-number {
            font-size: 2rem;
            font-weight: 700;
            color: #e50914;
            display: block;
        }
        .stat-label {
            color: #ccc;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .search-container {
            margin: 2rem 0;
            position: relative;
        }
        .search-input {
            width: 100%;
            padding: 1rem 1.5rem;
            font-size: 1.1rem;
            background: rgba(255,255,255,0.1);
            border: 2px solid transparent;
            border-radius: 50px;
            color: white;
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
        }
        .search-input:focus {
            outline: none;
            border-color: #e50914;
            background: rgba(255,255,255,0.15);
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(229, 9, 20, 0.3);
        }
        .search-input::placeholder {
            color: #999;
        }
        .content-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 1.5rem;
            margin-top: 2rem;
        }
        .content-card {
            background: linear-gradient(145deg, rgba(255,255,255,0.1) 0%, rgba(255,255,255,0.05) 100%);
            border-radius: 15px;
            padding: 1.5rem;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        .content-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: linear-gradient(90deg, #e50914, #ff6b6b);
        }
        .content-card:hover {
            transform: translateY(-5px) scale(1.02);
            box-shadow: 0 20px 40px rgba(0,0,0,0.3);
            border-color: rgba(229, 9, 20, 0.5);
        }
        .content-type {
            display: inline-block;
            padding: 0.3rem 0.8rem;
            background: #e50914;
            color: white;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 600;
            margin-bottom: 1rem;
            text-transform: uppercase;
        }
        .content-title {
            font-size: 1.3rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            color: white;
        }
        .content-meta {
            color: #ccc;
            margin-bottom: 0.8rem;
            font-size: 0.9rem;
        }
        .content-description {
            color: #aaa;
            line-height: 1.5;
            margin-bottom: 1.5rem;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .player-controls {
            display: flex;
            gap: 0.8rem;
            align-items: center;
        }
        .player-select {
            flex: 1;
            padding: 0.8rem;
            background: rgba(255,255,255,0.1);
            color: white;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            font-size: 0.9rem;
        }
        .stream-btn {
            padding: 0.8rem 1.5rem;
            background: linear-gradient(45deg, #e50914, #ff3030);
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: 600;
            transition: all 0.3s ease;
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .stream-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(229, 9, 20, 0.4);
            background: linear-gradient(45deg, #ff3030, #e50914);
        }
        .loading {
            text-align: center;
            padding: 4rem 2rem;
            color: #666;
            font-size: 1.2rem;
        }
        .loading::before {
            content: '';
            display: inline-block;
            width: 40px;
            height: 40px;
            border: 4px solid #333;
            border-top: 4px solid #e50914;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-bottom: 1rem;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: #666;
        }
        .empty-state h2 {
            color: #e50914;
            margin-bottom: 1rem;
            font-size: 2rem;
        }
        @media (max-width: 768px) {
            .navbar { padding: 1rem; }
            .navbar h1 { font-size: 1.5rem; }
            .container { padding: 80px 1rem 1rem; }
            .stats-bar { 
                flex-direction: column; 
                gap: 1rem; 
                text-align: center; 
            }
            .content-grid {
                grid-template-columns: 1fr;
                gap: 1rem;
            }
            .player-controls {
                flex-direction: column;
                align-items: stretch;
            }
        }
    </style>
</head>
<body>
    <nav class="navbar">
        <h1>🎬 StreamFlix</h1>
    </nav>
    
    <div class="container">
        <div class="stats-bar">
            <div class="stat-item">
                <span class="stat-number" id="movies-count">0</span>
                <span class="stat-label">Movies</span>
            </div>
            <div class="stat-item">
                <span class="stat-number" id="series-count">0</span>
                <span class="stat-label">Series</span>
            </div>
            <div class="stat-item">
                <span class="stat-number" id="total-count">0</span>
                <span class="stat-label">Total</span>
            </div>
        </div>

        <div class="search-container">
            <input type="text" class="search-input" id="searchInput" placeholder="🔍 Search your library...">
        </div>

        <div id="content-grid" class="content-grid">
            <div class="loading">Loading your content...</div>
        </div>
    </div>

    <script>
        let allContent = [];
        
        function updatePlayerLink(selectElement, encodedUrl) {
            const selectedPlayer = selectElement.value;
            const parentDiv = selectElement.closest('.player-controls');
            const streamButton = parentDiv.querySelector('.stream-btn');
            let url = decodeURIComponent(encodedUrl);

            if (selectedPlayer === 'mxplayer') {
                url = `intent:${url}#Intent;package=com.mxtech.videoplayer.ad;end;`;
            } else if (selectedPlayer === 'vlc') {
                url = `vlc://${url}`;
            }
            
            streamButton.href = url;
        }

        function renderContent(content) {
            const contentGrid = document.getElementById('content-grid');
            contentGrid.innerHTML = '';

            if (content.length === 0) {
                contentGrid.innerHTML = `
                    <div class="empty-state">
                        <h2>No Content Found</h2>
                        <p>Start building your library by uploading videos via the Telegram bot!</p>
                    </div>
                `;
                return;
            }

            content.forEach(item => {
                const card = document.createElement('div');
                card.className = 'content-card';

                const type = item.type === 'movie' ? 'Movie' : 'Series';
                const typeIcon = item.type === 'movie' ? '🎬' : '📺';
                const meta = item.type === 'movie' 
                    ? `${item.year || 'Unknown Year'}`
                    : `Season ${item.season || 'N/A'} • Episode ${item.episode || 'N/A'}`;
                const genres = Array.isArray(item.genre) ? item.genre.join(', ') : (item.genre || 'Unknown');
                const encodedUrl = encodeURIComponent(item.stream_url);

                card.innerHTML = `
                    <div class="content-type">${typeIcon} ${type}</div>
                    <h3 class="content-title">${item.title || 'Untitled'}</h3>
                    <p class="content-meta">${meta} • ${genres}</p>
                    <p class="content-description">${item.description || 'No description available.'}</p>
                    <div class="player-controls">
                        <select class="player-select" onchange="updatePlayerLink(this, '${encodedUrl}')">
                            <option value="default">Browser Player</option>
                            <option value="mxplayer">MX Player</option>
                            <option value="vlc">VLC Player</option>
                        </select>
                        <a href="${item.stream_url}" class="stream-btn" target="_blank">
                            ▶️ Play
                        </a>
                    </div>
                `;
                contentGrid.appendChild(card);
            });
        }

        function handleSearch() {
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const filtered = allContent.filter(item => {
                const title = (item.title || '').toLowerCase();
                const description = (item.description || '').toLowerCase();
                const genres = Array.isArray(item.genre) 
                    ? item.genre.join(' ').toLowerCase() 
                    : (item.genre || '').toLowerCase();
                
                return title.includes(searchTerm) || 
                       description.includes(searchTerm) || 
                       genres.includes(searchTerm);
            });
            renderContent(filtered);
        }

        async function loadContent() {
            try {
                const response = await fetch('/api/content', {
                    cache: 'no-cache',
                    headers: { 'Cache-Control': 'no-cache' }
                });
                
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                
                const data = await response.json();
                
                document.getElementById('movies-count').textContent = data.movies.length;
                document.getElementById('series-count').textContent = data.series.length;
                document.getElementById('total-count').textContent = data.total_content;
                
                allContent = [...data.movies, ...data.series];
                renderContent(allContent);
                
            } catch (error) {
                console.error('Failed to load content:', error);
                document.getElementById('content-grid').innerHTML = `
                    <div class="empty-state">
                        <h2>Connection Error</h2>
                        <p>Unable to load content. Please check your connection and try again.</p>
                    </div>
                `;
            }
        }

        // Event listeners
        document.getElementById('searchInput').addEventListener('input', handleSearch);
        
        // Initial load and periodic refresh
        loadContent();
        setInterval(loadContent, 30000);
    </script>
</body>
</html>
"""

# Flask Routes
@app.route('/')
def serve_frontend():
    """Serve the main frontend"""
    return render_template_string(FRONTEND_HTML)

@app.route('/health')
def health_check():
    """Comprehensive health check"""
    health_status = {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'services': {}
    }
    
    # Check MongoDB
    try:
        if app_state['mongo_client']:
            app_state['mongo_client'].admin.command('ping')
            health_status['services']['mongodb'] = 'ok'
        else:
            health_status['services']['mongodb'] = 'not_connected'
            health_status['status'] = 'degraded'
    except Exception as e:
        health_status['services']['mongodb'] = f'error: {str(e)[:50]}'
        health_status['status'] = 'degraded'
    
    # Check Bot
    health_status['services']['telegram_bot'] = 'ok' if app_state['bot_app'] else 'not_initialized'
    health_status['services']['webhook'] = 'set' if app_state['webhook_set'] else 'not_set'
    
    return jsonify(health_status), 200 if health_status['status'] == 'ok' else 503

@app.route('/api/content')
def get_content_library():
    """Get content library with error handling"""
    try:
        if not app_state['content_collection']:
            return jsonify({
                'movies': [],
                'series': [],
                'total_content': 0,
                'error': 'Database not available'
            }), 503
        
        projection = {
            '_id': 0, 'title': 1, 'type': 1, 'year': 1, 'season': 1, 
            'episode': 1, 'genre': 1, 'description': 1, 'stream_url': 1
        }
        
        movies = list(app_state['content_collection'].find(
            {'type': 'movie'}, projection
        ).sort('added_date', -1).limit(200))
        
        series = list(app_state['content_collection'].find(
            {'type': 'series'}, projection
        ).sort('added_date', -1).limit(200))
        
        return jsonify({
            'movies': movies,
            'series': series,
            'total_content': len(movies) + len(series),
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in get_content_library: {e}")
        return jsonify({
            'movies': [],
            'series': [],
            'total_content': 0,
            'error': 'Internal server error'
        }), 500

@app.route('/stream/<file_id>')
def stream_file(file_id):
    """Stream video files with range request support"""
    try:
        if not app_state['files_collection']:
            abort(503)
        
        file_info = app_state['files_collection'].find_one(
            {'_id': file_id}, 
            {'file_url': 1, 'file_size': 1, 'filename': 1}
        )
        
        if not file_info:
            abort(404)

        file_url = file_info['file_url']
        file_size = file_info['file_size']
        filename = file_info['filename']
        mime_type = get_video_mime_type(filename)

        range_header = request.environ.get('HTTP_RANGE', '').strip()
        
        if range_header:
            range_match = re.search(r'bytes=(\d+)-(\d*)', range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                
                start = max(0, min(start, file_size - 1))
                end = max(start, min(end, file_size - 1))

                def generate_range():
                    try:
                        headers = {'Range': f'bytes={start}-{end}'}
                        with requests.get(file_url, headers=headers, stream=True, timeout=30) as response:
                            response.raise_for_status()
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    yield chunk
                    except Exception as e:
                        logger.error(f"Range streaming error for {file_id}: {e}")

                return Response(
                    generate_range(),
                    206,
                    {
                        'Content-Type': mime_type,
                        'Accept-Ranges': 'bytes',
                        'Content-Range': f'bytes {start}-{end}/{file_size}',
                        'Content-Length': str(end - start + 1),
                        'Cache-Control': 'public, max-age=3600',
                        'Access-Control-Allow-Origin': '*'
                    }
                )

        def generate_full():
            try:
                with requests.get(file_url, stream=True, timeout=30) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
            except Exception as e:
                logger.error(f"Full streaming error for {file_id}: {e}")

        return Response(
            generate_full(),
            200,
            {
                'Content-Type': mime_type,
                'Accept-Ranges': 'bytes',
                'Content-Length': str(file_size),
                'Cache-Control': 'public, max-age=3600',
                'Access-Control-Allow-Origin': '*'
            }
        )

    except Exception as e:
        logger.error(f"Stream error for {file_id}: {e}")
        abort(500)

@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    """Handle Telegram webhook updates"""
    if not app_state['bot_app']:
        logger.error("Bot not ready for webhook")
        return "Bot not ready", 503

    try:
        update_json = request.get_json(force=True)
        if not update_json:
            return "Invalid JSON", 400

        update = Update.de_json(update_json, app_state['bot_app'].bot)
        
        # Process update in background to avoid blocking
        asyncio.create_task(app_state['bot_app'].process_update(update))
        
        return "OK", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

@app.route('/setup-webhook', methods=['POST', 'GET'])
async def manual_webhook_setup():
    """Manual webhook setup endpoint"""
    try:
        success = await setup_webhook()
        return jsonify({
            'success': success,
            'webhook_set': app_state['webhook_set'],
            'domain': get_koyeb_domain()
        })
    except Exception as e:
        logger.error(f"Manual webhook setup error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Telegram Bot Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    domain = get_koyeb_domain()
    frontend_url = f"https://{domain}" if domain else "https://your-app.koyeb.app"
    
    welcome_text = f"""
🎬 **StreamFlix - Your Personal Netflix** 🎬

Welcome to your own streaming platform! Transform any video into a Netflix-style streaming experience.

**✨ Features:**
• Netflix-style interface with modern design
• Mobile & Android TV optimized
• MX Player & VLC integration
• Movie & Series categorization
• Search functionality
• Permanent streaming URLs

**🎯 Commands:**
/start - Welcome message
/library - Browse your content
/frontend - Access web interface
/stats - View library statistics

**🚀 Get Started:**
1. Send me any video file
2. I'll categorize it (Movie/Series)
3. Access your library at: {frontend_url}

Ready to build your streaming empire! 🚀
"""
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Library command handler"""
    try:
        if not app_state['content_collection']:
            await update.message.reply_text("❌ Database unavailable")
            return
        
        movie_count = app_state['content_collection'].count_documents({'type': 'movie'})
        series_count = app_state['content_collection'].count_documents({'type': 'series'})
        total_count = movie_count + series_count
        
        domain = get_koyeb_domain()
        frontend_url = f"https://{domain}" if domain else "https://your-app.koyeb.app"
        
        library_text = f"""
📚 **Your Library Statistics**

🎬 Movies: {movie_count}
📺 Series: {series_count}
📊 Total Content: {total_count}

🌐 **Access Your Library:**
{frontend_url}

Upload more videos to expand your collection!
"""
        
        await update.message.reply_text(library_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Library command error: {e}")
        await update.message.reply_text("❌ Error retrieving library stats")

async def frontend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Frontend command handler"""
    domain = get_koyeb_domain()
    frontend_url = f"https://{domain}" if domain else "https://your-app.koyeb.app"
    
    frontend_text = f"""
🌐 **StreamFlix Web Interface**

Access your Netflix-style streaming platform:
{frontend_url}

**Features:**
• Modern Netflix-like design
• Search & filter content
• Mobile optimized
• External player support (MX, VLC)
• Permanent streaming URLs

Enjoy your personal streaming service! 🍿
"""
    
    await update.message.reply_text(frontend_text, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stats command handler"""
    try:
        if not app_state['content_collection']:
            await update.message.reply_text("❌ Database unavailable")
            return
        
        # Aggregate statistics
        pipeline = [
            {"$group": {
                "_id": "$type",
                "count": {"$sum": 1}
            }}
        ]
        
        stats = list(app_state['content_collection'].aggregate(pipeline))
        movie_count = next((s['count'] for s in stats if s['_id'] == 'movie'), 0)
        series_count = next((s['count'] for s in stats if s['_id'] == 'series'), 0)
        
        # Get recent uploads
        recent = list(app_state['content_collection'].find(
            {}, {'title': 1, 'type': 1, 'added_date': 1}
        ).sort('added_date', -1).limit(5))
        
        recent_text = "\n".join([
            f"• {item['title']} ({item['type']})" 
            for item in recent
        ]) if recent else "No recent uploads"
        
        stats_text = f"""
📊 **Detailed Statistics**

**Content Breakdown:**
🎬 Movies: {movie_count}
📺 Series: {series_count}
📈 Total: {movie_count + series_count}

**Storage Info:**
✅ MongoDB Connected
🔗 Streaming URLs Active
"""
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Stats command error: {e}")
        await update.message.reply_text("❌ Error retrieving statistics")

async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video file uploads"""
    try:
        user_id = update.effective_user.id
        
        # Get file info
        if update.message.video:
            file_obj = update.message.video
            file_size = file_obj.file_size
            filename = file_obj.file_name or f"video_{file_obj.file_unique_id}.mp4"
        elif update.message.document:
            file_obj = update.message.document
            file_size = file_obj.file_size
            filename = file_obj.file_name or f"document_{file_obj.file_unique_id}"
            
            if not is_video_file(filename):
                await update.message.reply_text(
                    "❌ Please send a video file. Supported formats: MP4, AVI, MKV, MOV, etc."
                )
                return
        else:
            await update.message.reply_text("❌ No valid file detected")
            return
        
        # Check file size
        if file_size and file_size > MAX_FILE_SIZE:
            await update.message.reply_text(
                f"❌ File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB"
            )
            return
        
        # Send processing message
        processing_msg = await update.message.reply_text("🎬 Processing your video...")
        
        # Get file from Telegram
        file = await context.bot.get_file(file_obj.file_id)
        file_url = file.file_path
        
        # Generate unique file ID
        file_id = str(uuid.uuid4())
        
        # Store file info in database
        file_doc = {
            '_id': file_id,
            'user_id': user_id,
            'filename': filename,
            'file_size': file_size,
            'file_url': file_url,
            'telegram_file_id': file_obj.file_id,
            'upload_date': datetime.now(),
            'mime_type': get_video_mime_type(filename)
        }
        
        app_state['files_collection'].insert_one(file_doc)
        
        # Generate streaming URL
        domain = get_koyeb_domain()
        stream_url = f"https://{domain}/stream/{file_id}" if domain else f"https://your-app.koyeb.app/stream/{file_id}"
        
        # Store context for categorization
        context.user_data['pending_file'] = {
            'file_id': file_id,
            'filename': filename,
            'stream_url': stream_url,
            'user_id': user_id
        }
        
        # Send categorization options
        keyboard = [
            [InlineKeyboardButton("🎬 Movie", callback_data="type_movie")],
            [InlineKeyboardButton("📺 Series", callback_data="type_series")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await processing_msg.edit_text(
            f"✅ **Video uploaded successfully!**\n\n"
            f"📁 File: {filename}\n"
            f"📏 Size: {file_size/(1024*1024):.1f}MB\n\n"
            f"Please categorize your content:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Video upload error: {e}")
        await update.message.reply_text("❌ Error processing video. Please try again.")

async def handle_categorization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle content categorization callbacks"""
    try:
        query = update.callback_query
        await query.answer()
        
        if not context.user_data.get('pending_file'):
            await query.edit_message_text("❌ No pending file found")
            return
        
        file_info = context.user_data['pending_file']
        
        if query.data == "type_movie":
            context.user_data['content_type'] = 'movie'
            await query.edit_message_text(
                "🎬 **Movie Selected**\n\n"
                "Please provide movie details in this format:\n\n"
                "**Title:** Movie Name\n"
                "**Year:** 2024\n"
                "**Genre:** Action, Drama\n"
                "**Description:** Brief description...\n\n"
                "Send the details as a single message:",
                parse_mode='Markdown'
            )
            
        elif query.data == "type_series":
            context.user_data['content_type'] = 'series'
            await query.edit_message_text(
                "📺 **Series Selected**\n\n"
                "Please provide series details in this format:\n\n"
                "**Title:** Series Name\n"
                "**Season:** 1\n"
                "**Episode:** 1\n"
                "**Genre:** Drama, Thriller\n"
                "**Description:** Brief description...\n\n"
                "Send the details as a single message:",
                parse_mode='Markdown'
            )
    
    except Exception as e:
        logger.error(f"Categorization error: {e}")
        await query.edit_message_text("❌ Error processing selection")

async def handle_metadata_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle metadata input from users"""
    try:
        if not context.user_data.get('pending_file') or not context.user_data.get('content_type'):
            return  # Not in metadata input mode
        
        file_info = context.user_data['pending_file']
        content_type = context.user_data['content_type']
        metadata_text = update.message.text
        
        # Parse metadata
        metadata = {}
        lines = metadata_text.strip().split('\n')
        
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip('* ').lower()
                value = value.strip()
                
                if key in ['title', 'year', 'season', 'episode', 'genre', 'description']:
                    metadata[key] = value
        
        # Validate required fields
        if not metadata.get('title'):
            await update.message.reply_text("❌ Title is required. Please try again.")
            return
        
        # Prepare content document
        content_doc = {
            '_id': str(uuid.uuid4()),
            'file_id': file_info['file_id'],
            'type': content_type,
            'title': metadata.get('title', 'Untitled'),
            'stream_url': file_info['stream_url'],
            'filename': file_info['filename'],
            'added_by': file_info['user_id'],
            'added_date': datetime.now(),
            'description': metadata.get('description', ''),
            'genre': metadata.get('genre', '').split(',') if metadata.get('genre') else []
        }
        
        # Add type-specific fields
        if content_type == 'movie':
            content_doc['year'] = metadata.get('year', '')
        elif content_type == 'series':
            content_doc['season'] = metadata.get('season', '')
            content_doc['episode'] = metadata.get('episode', '')
        
        # Save to database
        app_state['content_collection'].insert_one(content_doc)
        
        # Clear user data
        context.user_data.clear()
        
        # Get frontend URL
        domain = get_koyeb_domain()
        frontend_url = f"https://{domain}" if domain else "https://your-app.koyeb.app"
        
        # Send success message
        success_text = f"""
✅ **Content Added Successfully!**

🎬 **{content_doc['title']}** 📂 Type: {content_type.title()}
🔗 Stream URL: {file_info['stream_url']}

🌐 **Access your library:**
{frontend_url}

Ready for your next upload! 🚀
"""
        
        await update.message.reply_text(success_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Metadata input error: {e}")
        await update.message.reply_text("❌ Error saving content. Please try again.")

# Application Initialization
async def initialize_application():
    """Initialize the complete application"""
    logger.info("🚀 Starting StreamFlix application...")
    
    try:
        # Initialize MongoDB
        if not await initialize_mongodb():
            logger.error("❌ Failed to initialize MongoDB")
            return False
        
        # Initialize Telegram Bot
        if not await initialize_telegram_bot():
            logger.error("❌ Failed to initialize Telegram Bot")
            return False
        
        # Setup Webhook
        if not await setup_webhook():
            logger.warning("⚠️ Webhook setup failed, but continuing...")
        
        logger.info("✅ Application initialized successfully!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Application initialization failed: {e}")
        return False

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    app_state['shutdown'] = True
    
    # Close MongoDB connection
    if app_state['mongo_client']:
        app_state['mongo_client'].close()
        logger.info("MongoDB connection closed")
    
    sys.exit(0)

# Main execution
if __name__ == '__main__':
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Check required environment variables
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN environment variable is required")
        sys.exit(1)
    
    if not STORAGE_CHANNEL_ID:
        logger.warning("⚠️ STORAGE_CHANNEL_ID not set")
    
    # Run initialization in event loop
    async def startup():
        success = await initialize_application()
        if not success:
            logger.error("❌ Failed to initialize application")
            sys.exit(1)
    
    # Run startup
    asyncio.run(startup())
    
    # Start Flask application
    logger.info(f"🌐 Starting Flask server on port {PORT}")
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False
    )

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

