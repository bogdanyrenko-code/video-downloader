import os
import uuid
import time
import ssl
import re
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, send_file, render_template_string, session, redirect, url_for, jsonify, after_this_request
import yt_dlp
import requests
from threading import Thread
from functools import wraps
from yookassa import Configuration, Payment

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ssl._create_default_https_context = ssl._create_unverified_context

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-key-2024-change-me')

# =========== НАСТРОЙКИ ЮKASSA (ТЕСТОВЫЙ РЕЖИМ) ===========
YOOKASSA_SHOP_ID = "1369767"
YOOKASSA_SECRET_KEY = "test_bnUzopYIE4j-h9PiqeM2I0D16sHjo9C2CBBwVCJyJf4"

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# =========== ОСТАЛЬНЫЕ НАСТРОЙКИ ===========
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

PREMIUM_FILE = "premium_users.json"
PREMIUM_USERS = {}
DOWNLOAD_STATS = {}
USER_SESSIONS = {}

MAX_FREE_DOWNLOADS_PER_DAY = 5
MAX_VIDEO_SIZE_FREE_MB = 100
MAX_VIDEO_SIZE_PREMIUM_MB = 500
CLEANUP_INTERVAL = 3600
FILE_RETENTION_TIME = 1800

SECRET_REQUISITES_KEY = "Bogdan2025Secure"

def load_premium_users():
    global PREMIUM_USERS
    if os.path.exists(PREMIUM_FILE):
        try:
            with open(PREMIUM_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                now = datetime.now()
                for user_id, data in loaded.items():
                    expire_date = datetime.strptime(data['expire'], '%Y-%m-%d')
                    if expire_date >= now:
                        PREMIUM_USERS[user_id] = data
                logger.info(f"Загружено {len(PREMIUM_USERS)} премиум-пользователей")
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")

def save_premium_users():
    try:
        with open(PREMIUM_FILE, 'w', encoding='utf-8') as f:
            json.dump(PREMIUM_USERS, f, ensure_ascii=False, indent=2)
        logger.info(f"Сохранено {len(PREMIUM_USERS)} премиум-пользователей")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

load_premium_users()

def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for filename in os.listdir(DOWNLOAD_FOLDER):
                filepath = os.path.join(DOWNLOAD_FOLDER, filename)
                if os.path.isfile(filepath):
                    if now - os.path.getmtime(filepath) > FILE_RETENTION_TIME:
                        os.remove(filepath)
                        logger.info(f"Удален старый файл: {filename}")
        except Exception as e:
            logger.error(f"Ошибка при очистке файлов: {e}")
        time.sleep(CLEANUP_INTERVAL)

cleanup_thread = Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_user_id():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
    return session['user_id']

def is_premium(user_id):
    if user_id not in PREMIUM_USERS:
        return False
    expire_date = datetime.strptime(PREMIUM_USERS[user_id]['expire'], '%Y-%m-%d')
    return datetime.now() < expire_date

def add_premium(user_id, days=30):
    expire_date = datetime.now() + timedelta(days=days)
    PREMIUM_USERS[user_id] = {
        'expire': expire_date.strftime('%Y-%m-%d'),
        'activated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_premium_users()
    logger.info(f"Премиум активирован для {user_id} до {expire_date}")

def check_download_limit(user_id):
    if is_premium(user_id):
        return True, None
    today = datetime.now().strftime('%Y-%m-%d')
    if user_id not in DOWNLOAD_STATS:
        DOWNLOAD_STATS[user_id] = {}
    if today not in DOWNLOAD_STATS[user_id]:
        DOWNLOAD_STATS[user_id][today] = 0
    if DOWNLOAD_STATS[user_id][today] >= MAX_FREE_DOWNLOADS_PER_DAY:
        return False, f"Достигнут лимит скачиваний ({MAX_FREE_DOWNLOADS_PER_DAY}/день). Купите премиум!"
    return True, None

def increment_download_count(user_id):
    today = datetime.now().strftime('%Y-%m-%d')
    if user_id not in DOWNLOAD_STATS:
        DOWNLOAD_STATS[user_id] = {}
    if today not in DOWNLOAD_STATS[user_id]:
        DOWNLOAD_STATS[user_id][today] = 0
    DOWNLOAD_STATS[user_id][today] += 1

def rate_limit(max_requests=10, window=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user_id = get_user_id()
            now = time.time()
            if user_id not in USER_SESSIONS:
                USER_SESSIONS[user_id] = []
            USER_SESSIONS[user_id] = [req_time for req_time in USER_SESSIONS[user_id] if now - req_time < window]
            if len(USER_SESSIONS[user_id]) >= max_requests and not is_premium(user_id):
                return jsonify({'error': 'Слишком много запросов. Подождите немного.'}), 429
            USER_SESSIONS[user_id].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def extract_rutube_id(url):
    if '?' in url:
        url = url.split('?')[0]
    match = re.search(r'rutube\.ru/video/([a-f0-9]+)', url)
    if match:
        return match.group(1)
    return None

def get_rutube_video_info(url):
    video_id = extract_rutube_id(url)
    if not video_id:
        return None, "Не удалось определить ID видео"
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    try:
        api_url = f"https://rutube.ru/api/video/{video_id}/"
        resp = requests.get(api_url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            formats = [{'format_id': 'rutube_best', 'resolution': 'Лучшее качество', 'ext': 'mp4', 'filesize_mb': '?'}]
            return {'title': data.get('title', 'RuTube видео'), 'thumbnail': data.get('thumbnail_url', ''), 'duration': data.get('duration', 0), 'formats': formats}, None
        return None, "Не удалось получить информацию о видео"
    except Exception as e:
        return None, str(e)

def get_video_info(url):
    if 'rutube.ru' in url:
        return get_rutube_video_info(url)
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False, 'ignoreerrors': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None, "Не удалось получить информацию о видео"
            formats = []
            seen_resolutions = set()
            for f in info.get('formats', []):
                if f.get('vcodec') != 'none':
                    resolution = f.get('height')
                    if resolution:
                        res_str = f"{resolution}p"
                        if res_str not in seen_resolutions:
                            filesize_mb = '?'
                            if f.get('filesize'):
                                filesize_mb = f"{f['filesize'] / 1024 / 1024:.1f}"
                            formats.append({'format_id': f['format_id'], 'resolution': res_str, 'ext': f.get('ext', 'mp4'), 'filesize_mb': filesize_mb})
                            seen_resolutions.add(res_str)
            return {'title': info.get('title', 'Видео'), 'thumbnail': info.get('thumbnail', ''), 'duration': info.get('duration', 0), 'formats': sorted(formats, key=lambda x: int(x['resolution'].replace('p', '')), reverse=True)}, None
    except Exception as e:
        return None, str(e)

def download_video(url, format_id='best'):
    try:
        output_template = os.path.join(DOWNLOAD_FOLDER, f'{uuid.uuid4()}.%(ext)s')
        ydl_opts = {'format': format_id if format_id != 'best' else 'best', 'outtmpl': output_template, 'quiet': True, 'no_warnings': True, 'ignoreerrors': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / 1024 / 1024
                user_id = get_user_id()
                max_size = MAX_VIDEO_SIZE_PREMIUM_MB if is_premium(user_id) else MAX_VIDEO_SIZE_FREE_MB
                if size_mb > max_size:
                    os.remove(filename)
                    return None, f"Файл слишком большой ({size_mb:.1f} МБ). Максимум: {max_size} МБ"
                return filename, None
            return None, "Не удалось скачать видео"
    except Exception as e:
        return None, str(e)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VideoSave — Галактический загрузчик</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg-gradient: radial-gradient(ellipse at 20% 30%, #1a1a2e, #0f0f1a);
            --text-primary: #e0e0e0;
            --text-secondary: #a0a0c0;
            --card-bg: rgba(20, 20, 40, 0.55);
            --card-border: rgba(168, 85, 247, 0.25);
            --card-border-hover: rgba(168, 85, 247, 0.5);
            --input-bg: rgba(0, 0, 0, 0.4);
            --status-bg: rgba(0, 0, 0, 0.3);
            --premium-card-bg: rgba(30, 30, 50, 0.5);
            --premium-card-border: rgba(245, 158, 11, 0.3);
            --footer-border: rgba(255, 255, 255, 0.1);
            --alert-error-bg: rgba(239, 68, 68, 0.15);
            --alert-error-border: rgba(239, 68, 68, 0.4);
            --alert-success-bg: rgba(34, 197, 94, 0.15);
            --alert-success-border: rgba(34, 197, 94, 0.4);
            --format-card-bg: rgba(30, 30, 50, 0.6);
        }

        body.light {
            --bg-gradient: radial-gradient(ellipse at 20% 30%, #e0e0e0, #c0c0d0);
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --card-bg: rgba(255, 255, 255, 0.7);
            --card-border: rgba(168, 85, 247, 0.3);
            --card-border-hover: rgba(168, 85, 247, 0.6);
            --input-bg: rgba(255, 255, 255, 0.8);
            --status-bg: rgba(255, 255, 255, 0.4);
            --premium-card-bg: rgba(255, 255, 255, 0.5);
            --premium-card-border: rgba(245, 158, 11, 0.4);
            --footer-border: rgba(0, 0, 0, 0.1);
            --alert-error-bg: rgba(239, 68, 68, 0.1);
            --alert-error-border: rgba(239, 68, 68, 0.3);
            --alert-success-bg: rgba(34, 197, 94, 0.1);
            --alert-success-border: rgba(34, 197, 94, 0.3);
            --format-card-bg: rgba(255, 255, 255, 0.7);
        }

        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg-gradient);
            min-height: 100vh;
            color: var(--text-primary);
            overflow-x: hidden;
            transition: background 0.4s ease, color 0.3s ease;
            cursor: default;
        }

        /* Летающие сферы (фон) */
        #spheresContainer {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: auto;
            z-index: 5;
            overflow: hidden;
        }

        .pop-sphere {
            position: absolute;
            border-radius: 50%;
            cursor: pointer;
            transition: transform 0.05s linear;
            animation: floatSphere 8s ease-in-out infinite;
            box-shadow: 0 0 15px rgba(168, 85, 247, 0.5);
            z-index: 5;
            pointer-events: auto;
        }

        @keyframes floatSphere {
            0%, 100% { transform: translateY(0) translateX(0); }
            25% { transform: translateY(-20px) translateX(15px); }
            50% { transform: translateY(10px) translateX(-10px); }
            75% { transform: translateY(-10px) translateX(20px); }
        }

        @keyframes popExplosion {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.8); opacity: 0.8; background: radial-gradient(circle, #ffaa00, #ff6600); }
            100% { transform: scale(0); opacity: 0; }
        }

        .pop-animation {
            animation: popExplosion 0.3s ease-out forwards;
        }

        .score-board {
            position: fixed;
            top: 20px;
            right: 80px;
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 50px;
            padding: 8px 18px;
            font-size: 1.2rem;
            font-weight: bold;
            z-index: 100;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s;
        }

        .achievement {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0);
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: white;
            font-size: 4rem;
            font-weight: bold;
            padding: 20px 40px;
            border-radius: 80px;
            z-index: 200;
            white-space: nowrap;
            box-shadow: 0 0 50px rgba(245, 158, 11, 0.8);
            text-shadow: 0 0 10px rgba(0,0,0,0.3);
            animation: achievementPop 0.5s ease-out forwards;
            pointer-events: none;
        }

        @keyframes achievementPop {
            0% { transform: translate(-50%, -50%) scale(0); opacity: 0; }
            50% { transform: translate(-50%, -50%) scale(1.2); opacity: 1; }
            100% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
        }

        .achievement-fade {
            animation: achievementFade 2s ease-in forwards;
        }

        @keyframes achievementFade {
            0% { opacity: 1; transform: translate(-50%, -50%) scale(1); }
            80% { opacity: 1; transform: translate(-50%, -50%) scale(1.1); }
            100% { opacity: 0; transform: translate(-50%, -50%) scale(1.3); display: none; }
        }

        .confetti {
            position: fixed;
            width: 10px;
            height: 10px;
            background: #f59e0b;
            position: absolute;
            z-index: 150;
            animation: confettiFall 3s ease-out forwards;
        }

        @keyframes confettiFall {
            0% { transform: translateY(-100vh) rotate(0deg); opacity: 1; }
            100% { transform: translateY(100vh) rotate(360deg); opacity: 0; }
        }

        .container {
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            position: relative;
            z-index: 20;
        }

        .theme-toggle {
            position: fixed;
            top: 20px;
            right: 20px;
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 50px;
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 1.6rem;
            z-index: 100;
            transition: all 0.3s ease;
        }

        .glass-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border-radius: 48px;
            padding: 40px;
            border: 1px solid var(--card-border);
            box-shadow: 0 25px 45px -12px rgba(0, 0, 0, 0.4), 0 0 20px rgba(168, 85, 247, 0.05);
            transition: all 0.4s cubic-bezier(0.2, 0.9, 0.4, 1.1);
            z-index: 20;
            position: relative;
        }

        .glass-card:hover {
            border-color: var(--card-border-hover);
            transform: translateY(-4px);
        }

        .logo {
            text-align: center;
            font-size: 4.5rem;
            margin-bottom: 10px;
            animation: floatLogo 3s ease-in-out infinite;
        }

        @keyframes floatLogo {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-12px); }
        }

        h1 {
            font-size: 3rem;
            text-align: center;
            background: linear-gradient(135deg, var(--text-primary), #a855f7, #7c3aed);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            margin-bottom: 10px;
        }

        .subtitle {
            text-align: center;
            color: var(--text-secondary);
            margin-bottom: 30px;
        }

        .platforms {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 12px;
            margin-bottom: 30px;
        }

        .platform-badge {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(4px);
            padding: 6px 18px;
            border-radius: 40px;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-secondary);
            border: 1px solid var(--card-border);
            transition: all 0.3s;
            cursor: default;
        }

        .status-card {
            background: var(--status-bg);
            border-radius: 24px;
            padding: 16px 24px;
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
            border: 1px solid var(--card-border);
        }

        .premium-badge {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            padding: 8px 22px;
            border-radius: 40px;
            font-weight: bold;
            color: white;
            animation: glow 2s infinite;
        }

        .free-badge {
            background: rgba(255, 255, 255, 0.1);
            padding: 8px 22px;
            border-radius: 40px;
            color: var(--text-secondary);
        }

        .url-input {
            width: 100%;
            padding: 16px 24px;
            background: var(--input-bg);
            border: 1px solid var(--card-border);
            border-radius: 60px;
            font-size: 1rem;
            color: var(--text-primary);
            transition: all 0.3s;
            margin-bottom: 20px;
        }

        .btn {
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 60px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            background: linear-gradient(135deg, #a855f7, #7c3aed);
            color: white;
            position: relative;
            overflow: hidden;
        }

        .btn::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
            transition: left 0.5s;
        }

        .btn:hover::before {
            left: 100%;
        }

        .btn-premium {
            display: inline-block;
            background: linear-gradient(135deg, #f59e0b, #d97706);
            border-radius: 60px;
            padding: 12px 32px;
            color: white;
            text-decoration: none;
            font-weight: bold;
            transition: all 0.3s;
        }

        .btn-premium:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px -5px rgba(245, 158, 11, 0.4);
        }

        .formats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 12px;
            margin: 20px 0;
        }

        .format-card {
            background: var(--format-card-bg);
            backdrop-filter: blur(4px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 14px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.2, 0.9, 0.4, 1.1);
        }

        .format-card.selected {
            background: rgba(168, 85, 247, 0.2);
            border-color: #a855f7;
            box-shadow: 0 0 15px rgba(168, 85, 247, 0.2);
        }

        .footer {
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid var(--footer-border);
            font-size: 0.8rem;
            color: var(--text-secondary);
        }

        @media (max-width: 600px) {
            .glass-card { padding: 24px; }
            h1 { font-size: 2rem; }
            .score-board { top: 10px; right: 60px; font-size: 1rem; }
            .achievement { font-size: 2rem; padding: 15px 30px; }
        }
    </style>
</head>
<body>
    <div id="spheresContainer"></div>
    <div class="score-board"><span>💥</span><span id="scoreValue">0</span></div>
    <div class="theme-toggle" id="themeToggle">🌙</div>
    <div class="container">
        <div class="glass-card animate">
            <div class="logo">🎬</div>
            <h1>VideoSave</h1>
            <p class="subtitle">Галактический загрузчик видео</p>
            <div class="platforms">
                <span class="platform-badge">YouTube</span>
                <span class="platform-badge">RuTube</span>
                <span class="platform-badge">VK</span>
                <span class="platform-badge">Twitch</span>
                <span class="platform-badge">TikTok</span>
            </div>
            <div class="status-card">
                <strong>📊 Статус:</strong>
                {% if is_premium %}
                    <span class="premium-badge">⭐ PREMIUM до {{ premium_expire }}</span>
                {% else %}
                    <span class="free-badge">🔓 Бесплатный ({{ downloads_today }}/{{ max_downloads }} сегодня)</span>
                {% endif %}
            </div>
            <div id="alertContainer"></div>
            <input type="text" id="videoUrl" class="url-input" placeholder="Вставьте ссылку на видео..." autocomplete="off">
            <button class="btn" onclick="getVideoInfo()">🎯 Получить информацию</button>
            <div class="loader" id="loader" style="display:none; text-align:center; padding:20px;"><div class="spinner" style="width:40px;height:40px;border:3px solid rgba(168,85,247,0.2);border-top-color:#a855f7;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto;"></div><p>Обработка...</p></div>
            <div class="video-info" id="videoInfo" style="display:none; margin-top:30px;">
                <img id="videoThumbnail" style="width:100%; border-radius:24px;">
                <h3 id="videoTitle"></h3>
                <div id="videoDuration" style="color:var(--text-secondary); margin:10px 0;"></div>
                <div class="formats-grid" id="formatsList"></div>
                <button class="btn" id="downloadBtn" onclick="downloadVideo()">⬇️ Скачать видео</button>
            </div>
            {% if not is_premium %}
            <div class="premium-card" style="margin-top:30px; text-align:center;">
                <div style="font-size:2rem;">✨</div>
                <h3>Премиум возможности</h3>
                <div style="display:flex; justify-content:center; gap:30px; margin:20px 0; flex-wrap:wrap;">
                    <div><div style="font-size:2rem;">🚀</div><div>Безлимит</div></div>
                    <div><div style="font-size:2rem;">🎯</div><div>4K качество</div></div>
                    <div><div style="font-size:2rem;">⚡</div><div>Мгновенно</div></div>
                </div>
                <a href="/create_yookassa_payment" class="btn-premium">💳 Оплатить Premium 50₽ через ЮKassa</a>
            </div>
            {% endif %}
            <div class="footer">
                <p>🎥 VideoSave — космическая скорость скачивания</p>
                <p><a href="/return-policy">Политика возврата</a> | <a href="/requisites/secret?key=Bogdan2025Secure">Реквизиты</a></p>
            </div>
        </div>
    </div>
    <style>
        .loader .spinner { animation: spin 1s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
    <script>
        let score = 0, spheres = [], achievementShown = false;
        const spheresContainer = document.getElementById('spheresContainer');
        const scoreElement = document.getElementById('scoreValue');

        function createSphere() {
            const sphere = document.createElement('div');
            sphere.classList.add('pop-sphere');
            const size = Math.random() * 40 + 30;
            sphere.style.width = size + 'px';
            sphere.style.height = size + 'px';
            sphere.style.left = Math.random() * (window.innerWidth - 100) + 'px';
            sphere.style.top = Math.random() * (window.innerHeight - 100) + 'px';
            sphere.style.background = `radial-gradient(circle at 30% 30%, rgba(168,85,247,0.85), rgba(124,58,237,0.65))`;
            sphere.style.animationDuration = (Math.random() * 5 + 5) + 's';
            sphere.addEventListener('click', (e) => { e.stopPropagation(); popSphere(sphere); });
            spheresContainer.appendChild(sphere);
            spheres.push(sphere);
            setTimeout(() => { if(sphere.parentNode) { sphere.remove(); spheres = spheres.filter(s => s !== sphere); } }, 15000);
        }

        function popSphere(sphere) {
            sphere.classList.add('pop-animation');
            score++;
            scoreElement.textContent = score;
            if(score === 100 && !achievementShown) {
                achievementShown = true;
                const achievementDiv = document.createElement('div');
                achievementDiv.className = 'achievement';
                achievementDiv.textContent = 'ТЫКУН!';
                document.body.appendChild(achievementDiv);
                for(let i=0;i<100;i++) {
                    const conf = document.createElement('div');
                    conf.className = 'confetti';
                    conf.style.left = Math.random() * window.innerWidth + 'px';
                    conf.style.backgroundColor = `hsl(${Math.random() * 360}, 100%, 50%)`;
                    conf.style.width = Math.random() * 8 + 4 + 'px';
                    conf.style.animationDuration = Math.random() * 2 + 2 + 's';
                    document.body.appendChild(conf);
                    setTimeout(() => conf.remove(), 3000);
                }
                setTimeout(() => achievementDiv.classList.add('achievement-fade'), 1500);
                setTimeout(() => achievementDiv.remove(), 3500);
            }
            setTimeout(() => { if(sphere.parentNode) sphere.remove(); spheres = spheres.filter(s => s !== sphere); }, 300);
        }

        setInterval(() => { if(spheres.length < 30) createSphere(); }, 2000);
        for(let i=0;i<15;i++) setTimeout(() => createSphere(), i*300);

        const themeToggle = document.getElementById('themeToggle');
        const body = document.body;
        function setTheme(theme) {
            if(theme === 'light') { body.classList.add('light'); themeToggle.innerHTML = '🌙'; localStorage.setItem('theme', 'light'); }
            else { body.classList.remove('light'); themeToggle.innerHTML = '☀️'; localStorage.setItem('theme', 'dark'); }
        }
        (localStorage.getItem('theme') === 'light') ? setTheme('light') : setTheme('dark');
        themeToggle.addEventListener('click', () => body.classList.contains('light') ? setTheme('dark') : setTheme('light'));

        let selectedFormat = null, currentVideoUrl = null;
        function showAlert(msg, type) {
            const container = document.getElementById('alertContainer');
            container.innerHTML = `<div class="alert alert-${type}" style="padding:12px; border-radius:20px; margin-bottom:20px; background:${type==='error'?'rgba(239,68,68,0.15)':'rgba(34,197,94,0.15)'}">${msg}</div>`;
            setTimeout(() => container.innerHTML = '', 5000);
        }
        async function getVideoInfo() {
            const url = document.getElementById('videoUrl').value.trim();
            if(!url) { showAlert('Введите ссылку', 'error'); return; }
            currentVideoUrl = url;
            document.getElementById('loader').style.display = 'block';
            document.getElementById('videoInfo').style.display = 'none';
            try {
                const response = await fetch('/api/video-info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
                const data = await response.json();
                document.getElementById('loader').style.display = 'none';
                if(data.error) { showAlert(data.error, 'error'); return; }
                document.getElementById('videoThumbnail').src = data.thumbnail || '';
                document.getElementById('videoTitle').innerText = data.title;
                if(data.duration) document.getElementById('videoDuration').innerHTML = `⏱️ Длительность: ${Math.floor(data.duration/60)}:${(data.duration%60).toString().padStart(2,'0')}`;
                const list = document.getElementById('formatsList');
                list.innerHTML = '';
                data.formats.forEach(f => {
                    const div = document.createElement('div');
                    div.className = 'format-card';
                    if(!data.premium && f.resolution !== '480p') div.style.opacity = '0.5';
                    div.innerHTML = `<strong>${f.resolution}</strong><br><small>${f.ext.toUpperCase()} · ${f.filesize_mb} МБ</small>`;
                    if(!(!data.premium && f.resolution !== '480p')) div.onclick = () => { selectedFormat = f.format_id; document.querySelectorAll('.format-card').forEach(c => c.classList.remove('selected')); div.classList.add('selected'); };
                    list.appendChild(div);
                });
                if(data.formats.length && (data.premium || data.formats[0].resolution === '480p')) selectedFormat = data.formats[0].format_id;
                document.getElementById('videoInfo').style.display = 'block';
            } catch(e) { document.getElementById('loader').style.display = 'none'; showAlert('Ошибка сервера', 'error'); }
        }
        async function downloadVideo() {
            if(!selectedFormat || !currentVideoUrl) { showAlert('Выберите качество', 'error'); return; }
            try {
                const response = await fetch('/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: currentVideoUrl, format_id: selectedFormat }) });
                if(!response.ok) { const data = await response.json(); throw new Error(data.error || 'Ошибка'); }
                const blob = await response.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'video.mp4';
                a.click();
                URL.revokeObjectURL(a.href);
                showAlert('✅ Скачивание началось!', 'success');
            } catch(e) { showAlert('Ошибка: '+e.message, 'error'); }
        }
        document.getElementById('videoUrl').addEventListener('keypress', e => { if(e.key === 'Enter') getVideoInfo(); });
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    uid = get_user_id()
    today = datetime.now().strftime('%Y-%m-%d')
    downloads = DOWNLOAD_STATS.get(uid, {}).get(today, 0)
    expire = PREMIUM_USERS[uid]['expire'] if is_premium(uid) else None
    return render_template_string(HTML_TEMPLATE, is_premium=is_premium(uid), premium_expire=expire, downloads_today=downloads, max_downloads=MAX_FREE_DOWNLOADS_PER_DAY)

@app.route('/api/video-info', methods=['POST'])
@rate_limit(20, 60)
def api_video_info():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL не указан'}), 400
        uid = get_user_id()
        ok, err = check_download_limit(uid)
        if not ok:
            return jsonify({'error': err}), 403
        info, err = get_video_info(url)
        if err:
            return jsonify({'error': err}), 400
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
@rate_limit(10, 60)
def api_download():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        fid = data.get('format_id', 'best')
        if not url:
            return jsonify({'error': 'URL не указан'}), 400
        uid = get_user_id()
        ok, err = check_download_limit(uid)
        if not ok:
            return jsonify({'error': err}), 403
        path, err = download_video(url, fid)
        if err:
            return jsonify({'error': err}), 400
        if not path or not os.path.exists(path):
            return jsonify({'error': 'Не удалось скачать'}), 500
        increment_download_count(uid)
        @after_this_request
        def remove(resp):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except:
                pass
            return resp
        return send_file(path, as_attachment=True, download_name='video.mp4')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/create_yookassa_payment')
def create_yookassa_payment():
    user_id = get_user_id()
    try:
        payment = Payment.create({
            "amount": {"value": "50.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://video-downloader-r3y6.onrender.com/payment_success_yookassa"},
            "capture": True,
            "description": f"Premium подписка на 30 дней (user: {user_id})",
            "metadata": {"user_id": user_id}
        })
        return redirect(payment.confirmation.confirmation_url)
    except Exception as e:
        logger.error(f"Ошибка при создании платежа: {e}")
        return f"Ошибка при создании платежа: {e}"

@app.route('/payment_success_yookassa')
def payment_success_yookassa():
    user_id = get_user_id()
    add_premium(user_id, 30)
    logger.info(f"Премиум активирован для {user_id} через ЮKassa")
    return '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Оплата прошла</title><meta http-equiv="refresh" content="3;url=/"><style>body{background:#0f0c29;color:#fff;text-align:center;padding:50px;font-family:Arial}h1{color:#22c55e}</style></head>
<body><h1>✅ Оплата прошла успешно!</h1><p>Ваша премиум-подписка активирована.</p><p>Через 3 секунды вы вернётесь на главную.</p><a href="/" style="color:#a855f7;">Вернуться сейчас</a></body>
</html>'''

@app.route('/yookassa-webhook', methods=['POST'])
def yookassa_webhook():
    try:
        data = request.json
        logger.info(f"Webhook от ЮKassa: {data}")
        if data.get('event') == 'payment.succeeded':
            payment = data.get('object', {})
            metadata = payment.get('metadata', {})
            user_id = metadata.get('user_id')
            if user_id:
                add_premium(user_id, 30)
                logger.info(f"Премиум активирован для {user_id} через webhook")
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/requisites')
def requisites_redirect():
    return redirect(url_for('index'))

@app.route('/requisites/secret')
def requisites_secret():
    key = request.args.get('key')
    if key != SECRET_REQUISITES_KEY:
        return "Доступ запрещён", 403
    return '''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Реквизиты</title><style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;padding:40px}.card{background:rgba(20,20,40,0.6);backdrop-filter:blur(12px);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid rgba(168,85,247,0.3)}h1{color:#a855f7}h2{color:#f59e0b}</style></head>
<body><div class="card"><h1>🔐 Реквизиты самозанятого</h1><p><strong>ФИО:</strong> Юренко Богдан Петрович</p><p><strong>ИНН:</strong> 231408820790</p><p><strong>Статус:</strong> Самозанятый</p><hr><p><strong>Email:</strong> bogdanyrenko@gmail.com</p><p><strong>Сайт:</strong> https://video-downloader-r3y6.onrender.com</p><h2>📋 Условия оплаты</h2><ul><li>Оплата через ЮKassa (банковская карта)</li><li>Стоимость: 50₽/месяц</li></ul><h2>↩️ Условия возврата</h2><ul><li>Возврат в течение 14 дней</li><li>Связь: bogdanyrenko@gmail.com</li></ul></div></body></html>'''

@app.route('/return-policy')
def return_policy():
    return '''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Условия возврата</title><style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;padding:40px}.card{background:rgba(20,20,40,0.6);backdrop-filter:blur(12px);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid rgba(168,85,247,0.3)}h1{color:#a855f7}</style></head>
<body><div class="card"><h1>📋 Политика возврата</h1><h2>Условия оплаты</h2><ul><li>Оплата через ЮKassa</li><li>Стоимость: 50₽/месяц</li></ul><h2>Условия возврата</h2><ul><li>Возврат в течение 14 дней</li><li>Для возврата: bogdanyrenko@gmail.com</li></ul><a href="/">← На главную</a></div></body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))