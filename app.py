import os
import uuid
import time
import ssl
import re
import json
import logging
import subprocess
import zipfile
import threading
from datetime import datetime, timedelta
from flask import Flask, request, send_file, render_template_string, session, redirect, url_for, jsonify, after_this_request, make_response
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
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

YOOKASSA_SHOP_ID = "1369767"
YOOKASSA_SECRET_KEY = "test_92d73ZaVYlLk9i1BvEwS6p5tflhwj7PSqiutGHHtosY"

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

PREMIUM_FILE = "premium_users.json"
DOWNLOAD_STATS = {}
USER_SESSIONS = {}
ONLINE_USERS = {}
ONLINE_TIMEOUT = 300

def cleanup_online_users():
    now = time.time()
    expired = [uid for uid, last_seen in ONLINE_USERS.items() if now - last_seen > ONLINE_TIMEOUT]
    for uid in expired:
        del ONLINE_USERS[uid]
    threading.Timer(60, cleanup_online_users).start()

cleanup_online_users()

def update_online_status(user_id):
    if user_id:
        ONLINE_USERS[user_id] = time.time()

MAX_FREE_DOWNLOADS_PER_WEEK = 3
MAX_FREE_QUALITY = 720
MAX_VIDEO_SIZE_FREE_MB = 200
MAX_VIDEO_SIZE_PREMIUM_MB = 500
CLEANUP_INTERVAL = 3600
FILE_RETENTION_TIME = 600

SECRET_REQUISITES_KEY = "Bogdan2025Secure"

PRICES = {
    'month': 50,
    'year': 650,
    'forever': 800
}

def load_premium_users():
    if os.path.exists(PREMIUM_FILE):
        try:
            with open(PREMIUM_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                now = datetime.now()
                result = {}
                for user_id, data in loaded.items():
                    expire_date = datetime.strptime(data.get('expire', '2000-01-01'), '%Y-%m-%d')
                    if data.get('ads_disabled_forever', False) or expire_date >= now:
                        result[user_id] = data
                return result
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")
    return {}

def save_premium_users(premium_users):
    try:
        with open(PREMIUM_FILE, 'w', encoding='utf-8') as f:
            json.dump(premium_users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def is_premium(user_id):
    premium_users = load_premium_users()
    if user_id not in premium_users:
        return False
    if premium_users[user_id].get('ads_disabled_forever', False):
        return True
    expire_date = datetime.strptime(premium_users[user_id].get('expire', '2000-01-01'), '%Y-%m-%d')
    return datetime.now() < expire_date

def should_show_ad(user_id):
    premium_users = load_premium_users()
    if user_id not in premium_users:
        return True
    user_data = premium_users[user_id]
    if user_data.get('ads_disabled_forever', False):
        return False
    expire_date = datetime.strptime(user_data.get('expire', '2000-01-01'), '%Y-%m-%d')
    return datetime.now() >= expire_date

def add_premium(user_id, days=30):
    premium_users = load_premium_users()
    expire_date = datetime.now() + timedelta(days=days)
    premium_users[user_id] = {
        'expire': expire_date.strftime('%Y-%m-%d'),
        'activated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ads_disabled_forever': premium_users.get(user_id, {}).get('ads_disabled_forever', False)
    }
    save_premium_users(premium_users)

def add_forever(user_id):
    premium_users = load_premium_users()
    premium_users[user_id] = {
        'expire': '2099-12-31',
        'activated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ads_disabled_forever': True
    }
    save_premium_users(premium_users)

def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for filename in os.listdir(DOWNLOAD_FOLDER):
                filepath = os.path.join(DOWNLOAD_FOLDER, filename)
                if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > FILE_RETENTION_TIME:
                    os.remove(filepath)
        except:
            pass
        time.sleep(CLEANUP_INTERVAL)

cleanup_thread = Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_user_id():
    user_id = request.cookies.get('videoSaveUserId')
    if user_id:
        return user_id
    if 'user_id' in session:
        return session['user_id']
    user_id = str(uuid.uuid4())
    session['user_id'] = user_id
    return user_id

def set_user_id_cookie(response, user_id):
    response.set_cookie('videoSaveUserId', user_id, max_age=365*24*60*60, httponly=False)
    return response

def get_week_key():
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    return week_start.strftime('%Y-%W')

def check_download_limit(user_id):
    if is_premium(user_id):
        return True, None
    week_key = get_week_key()
    if user_id not in DOWNLOAD_STATS:
        DOWNLOAD_STATS[user_id] = {}
    if week_key not in DOWNLOAD_STATS[user_id]:
        DOWNLOAD_STATS[user_id][week_key] = 0
    if DOWNLOAD_STATS[user_id][week_key] >= MAX_FREE_DOWNLOADS_PER_WEEK:
        return False, f"Лимит {MAX_FREE_DOWNLOADS_PER_WEEK} видео в неделю исчерпан. Premium снимает все ограничения!"
    return True, None

def increment_download_count(user_id):
    week_key = get_week_key()
    if user_id not in DOWNLOAD_STATS:
        DOWNLOAD_STATS[user_id] = {}
    if week_key not in DOWNLOAD_STATS[user_id]:
        DOWNLOAD_STATS[user_id][week_key] = 0
    DOWNLOAD_STATS[user_id][week_key] += 1

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
                return jsonify({'error': 'Слишком много запросов. Подождите.'}), 429
            USER_SESSIONS[user_id].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def extract_rutube_id(url):
    if '?' in url:
        url = url.split('?')[0]
    match = re.search(r'rutube\.ru/video/([a-f0-9]+)', url)
    return match.group(1) if match else None

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

def get_playlist_info(url):
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'ignoreerrors': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                videos = []
                for entry in info['entries']:
                    if entry:
                        videos.append({
                            'id': entry.get('id'),
                            'title': entry.get('title', 'Без названия'),
                            'duration': entry.get('duration', 0),
                            'url': f"https://youtube.com/watch?v={entry.get('id')}"
                        })
                return {'title': info.get('title', 'Плейлист'), 'count': len(videos), 'videos': videos}, None
            return None, "Не удалось распознать плейлист"
    except Exception as e:
        return None, str(e)

def download_playlist(url, selected_videos, output_dir):
    downloaded_files = []
    ydl_opts = {
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'quiet': True, 'no_warnings': True, 'ignoreerrors': True,
        'format': 'best[height<=720]',
    }
    for video_url in selected_videos:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                filename = ydl.prepare_filename(info)
                if os.path.exists(filename):
                    downloaded_files.append(filename)
                else:
                    for ext in ['.mp4', '.webm', '.mkv']:
                        test_path = filename.replace('.mp4', ext) if '.mp4' in filename else filename + ext
                        if os.path.exists(test_path):
                            downloaded_files.append(test_path)
                            break
        except Exception as e:
            logger.error(f"Ошибка скачивания {video_url}: {e}")
    if not downloaded_files:
        return None, "Не удалось скачать ни одного видео"
    zip_path = os.path.join(output_dir, f"playlist_{uuid.uuid4()}.zip")
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file in downloaded_files:
            zipf.write(file, os.path.basename(file))
            os.remove(file)
    return zip_path, None

def convert_to_mp3(input_path, output_path):
    try:
        title = os.path.splitext(os.path.basename(input_path))[0]
        cmd = [
            'ffmpeg', '-i', input_path,
            '-vn', '-acodec', 'libmp3lame', '-ab', '192k',
            '-metadata', f'title={title}',
            '-metadata', 'artist=VideoSave',
            '-id3v2_version', '3',
            output_path
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        return True
    except Exception as e:
        logger.error(f"Ошибка конвертации в MP3: {e}")
        return False

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
                            formats.append({
                                'format_id': f['format_id'],
                                'resolution': res_str,
                                'ext': f.get('ext', 'mp4'),
                                'filesize_mb': filesize_mb
                            })
                            seen_resolutions.add(res_str)
            return {'title': info.get('title', 'Видео'), 'thumbnail': info.get('thumbnail', ''), 'duration': info.get('duration', 0), 'formats': sorted(formats, key=lambda x: int(x['resolution'].replace('p', '')), reverse=True), 'is_playlist': False}, None
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
                if not is_premium(user_id):
                    resolution_match = re.search(r'(\d+)p', format_id)
                    if resolution_match:
                        resolution = int(resolution_match.group(1))
                        if resolution > MAX_FREE_QUALITY:
                            os.remove(filename)
                            return None, f"Качество {resolution}p доступно только в Premium"
                return filename, None
            return None, "Не удалось скачать видео"
    except Exception as e:
        return None, str(e)

# ========== НОВЫЙ ФУТУРИСТИЧНЫЙ ДИЗАЙН ==========
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VideoSave — Скачивай видео будущего</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg: #0a0a1a;
            --neon-purple: #b44dff;
            --neon-blue: #4d7cff;
            --neon-cyan: #00e5ff;
            --neon-pink: #ff4d8c;
            --neon-yellow: #ffd700;
            --card-bg: rgba(10, 10, 30, 0.7);
            --card-border: rgba(180, 77, 255, 0.3);
            --text: #e0e0f0;
            --text-secondary: #a0a0c0;
            --input-bg: rgba(0, 0, 0, 0.5);
            --glow: 0 0 15px rgba(180, 77, 255, 0.4);
        }

        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            cursor: default;
            transition: background 0.3s, color 0.3s;
        }

        /* Анимированная сетка на фоне */
        .bg-grid {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-image: 
                linear-gradient(rgba(180, 77, 255, 0.05) 1px, transparent 1px),
                linear-gradient(90deg, rgba(180, 77, 255, 0.05) 1px, transparent 1px);
            background-size: 60px 60px;
            z-index: 0;
            animation: gridMove 20s linear infinite;
            pointer-events: none;
        }
        @keyframes gridMove {
            0% { transform: translate(0,0); }
            100% { transform: translate(60px, 60px); }
        }
        /* Светящиеся частицы */
        .particles {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: 1;
            pointer-events: none;
        }
        .particle {
            position: absolute;
            width: 4px;
            height: 4px;
            background: var(--neon-purple);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--neon-purple);
            animation: floatParticle linear infinite;
        }
        @keyframes floatParticle {
            0% { transform: translateY(0) rotate(0deg); opacity: 0; }
            10% { opacity: 1; }
            90% { opacity: 1; }
            100% { transform: translateY(-100vh) rotate(360deg); opacity: 0; }
        }

        .container {
            max-width: 1000px;
            margin: 0 auto;
            padding: 30px 20px;
            position: relative;
            z-index: 10;
        }

        /* Кибер-карточка */
        .cyber-card {
            background: var(--card-bg);
            backdrop-filter: blur(20px);
            border: 1px solid var(--card-border);
            border-radius: 40px;
            padding: 45px 40px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.6), inset 0 0 30px rgba(180,77,255,0.1);
            position: relative;
            overflow: hidden;
            animation: cardAppear 0.8s cubic-bezier(0.23, 1, 0.32, 1);
        }
        @keyframes cardAppear {
            from { opacity: 0; transform: translateY(50px) scale(0.95); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        .cyber-card::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: conic-gradient(from 0deg, transparent, var(--neon-purple), transparent, var(--neon-cyan), transparent);
            animation: rotateGlow 8s linear infinite;
            opacity: 0.1;
        }
        @keyframes rotateGlow {
            100% { transform: rotate(360deg); }
        }

        /* Логотип */
        .logo-icon {
            font-size: 5rem;
            text-align: center;
            filter: drop-shadow(0 0 15px var(--neon-purple));
            animation: logoFloat 3s ease-in-out infinite;
        }
        @keyframes logoFloat {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-12px); }
        }
        h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 3.8rem;
            text-align: center;
            background: linear-gradient(90deg, var(--neon-purple), var(--neon-blue), var(--neon-cyan));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 10px 0;
            animation: textShine 3s ease infinite;
        }
        @keyframes textShine {
            0%, 100% { filter: hue-rotate(0deg); }
            50% { filter: hue-rotate(15deg); }
        }
        .subtitle {
            text-align: center;
            color: var(--text-secondary);
            margin-bottom: 30px;
            font-size: 1.1rem;
            letter-spacing: 1px;
        }

        /* Платформы */
        .platforms {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 10px;
            margin-bottom: 30px;
        }
        .platform-badge {
            background: rgba(180, 77, 255, 0.1);
            border: 1px solid var(--card-border);
            padding: 8px 20px;
            border-radius: 30px;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--neon-purple);
            backdrop-filter: blur(5px);
            transition: all 0.3s;
            cursor: default;
        }
        .platform-badge:hover {
            background: rgba(180, 77, 255, 0.25);
            box-shadow: 0 0 15px rgba(180, 77, 255, 0.4);
            transform: translateY(-2px);
        }

        /* Статусная панель */
        .status-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
            background: rgba(0, 0, 0, 0.4);
            border-radius: 30px;
            padding: 14px 28px;
            margin-bottom: 30px;
            border: 1px solid var(--card-border);
        }
        .premium-badge {
            background: linear-gradient(135deg, #ffd700, #ffaa00);
            padding: 6px 24px;
            border-radius: 30px;
            font-weight: bold;
            color: #000;
            box-shadow: 0 0 20px rgba(255, 215, 0, 0.5);
            animation: glowPulse 2s infinite;
        }
        @keyframes glowPulse {
            0%, 100% { box-shadow: 0 0 15px rgba(255,215,0,0.5); }
            50% { box-shadow: 0 0 25px rgba(255,215,0,0.8); }
        }
        .free-badge {
            background: rgba(255,255,255,0.1);
            padding: 6px 24px;
            border-radius: 30px;
            color: var(--text-secondary);
            border: 1px solid rgba(255,255,255,0.2);
        }
        .online-counter {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 229, 255, 0.1);
            padding: 6px 20px;
            border-radius: 30px;
            border: 1px solid rgba(0, 229, 255, 0.3);
            color: var(--neon-cyan);
            font-weight: 600;
        }
        .online-dot {
            width: 10px;
            height: 10px;
            background: var(--neon-cyan);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--neon-cyan);
            animation: pulseDot 1.5s infinite;
        }
        @keyframes pulseDot {
            0%, 100% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.6); opacity: 0.6; }
        }

        /* Поле ввода */
        .url-input {
            width: 100%;
            padding: 18px 28px;
            background: var(--input-bg);
            border: 2px solid var(--card-border);
            border-radius: 60px;
            font-size: 1rem;
            color: white;
            margin-bottom: 20px;
            backdrop-filter: blur(10px);
            transition: 0.3s;
        }
        .url-input:focus {
            outline: none;
            border-color: var(--neon-purple);
            box-shadow: 0 0 25px rgba(180, 77, 255, 0.4);
        }

        /* Кнопки */
        .btn {
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 60px;
            font-weight: 700;
            font-size: 1rem;
            cursor: pointer;
            background: linear-gradient(135deg, var(--neon-purple), var(--neon-blue));
            color: white;
            position: relative;
            overflow: hidden;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 0 30px rgba(180, 77, 255, 0.6);
        }
        .btn:active { transform: scale(0.98); }
        .btn-premium {
            display: inline-block;
            background: linear-gradient(135deg, #ffd700, #ffaa00);
            padding: 12px 28px;
            border-radius: 50px;
            color: #000;
            font-weight: bold;
            text-decoration: none;
            transition: 0.3s;
        }
        .btn-premium:hover {
            transform: translateY(-2px);
            box-shadow: 0 0 25px rgba(255,215,0,0.5);
        }
        .btn-mp3 {
            background: linear-gradient(135deg, #00e676, #00c853);
            border-radius: 50px;
            padding: 10px 20px;
            color: #000;
            font-weight: bold;
            text-decoration: none;
            transition: 0.3s;
        }
        .btn-mp3:hover {
            transform: translateY(-2px);
            box-shadow: 0 0 20px rgba(0, 230, 118, 0.5);
        }

        /* Загрузчик */
        .loader-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 30px;
        }
        .neon-spinner {
            width: 50px;
            height: 50px;
            border: 4px solid rgba(180, 77, 255, 0.2);
            border-top-color: var(--neon-purple);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            box-shadow: 0 0 15px var(--neon-purple);
        }
        @keyframes spin { 100% { transform: rotate(360deg); } }

        /* Форматы */
        .formats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 14px;
            margin: 20px 0;
        }
        .format-card {
            background: rgba(20, 20, 40, 0.6);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 16px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
            backdrop-filter: blur(5px);
        }
        .format-card:hover {
            border-color: var(--neon-purple);
            box-shadow: 0 0 20px rgba(180, 77, 255, 0.3);
            transform: translateY(-4px);
        }
        .format-card.selected {
            background: rgba(180, 77, 255, 0.2);
            border-color: var(--neon-purple);
            box-shadow: 0 0 25px var(--neon-purple);
            transform: scale(1.03);
        }
        .format-card.premium-locked {
            opacity: 0.5;
            cursor: not-allowed;
            position: relative;
        }
        .format-card.premium-locked::after {
            content: '🔒';
            position: absolute;
            top: 5px;
            right: 10px;
            font-size: 14px;
        }

        /* Плейлист */
        .playlist-panel {
            display: none;
            margin-top: 20px;
            padding: 20px;
            background: rgba(10,10,30,0.7);
            border-radius: 24px;
            border: 1px solid var(--card-border);
            backdrop-filter: blur(15px);
        }

        /* Уведомления */
        .alert {
            padding: 14px 20px;
            border-radius: 16px;
            margin-bottom: 20px;
            animation: slideIn 0.4s ease;
            font-weight: 500;
        }
        .alert-error {
            background: rgba(255, 77, 140, 0.2);
            border: 1px solid var(--neon-pink);
            color: #ffb3cc;
        }
        .alert-success {
            background: rgba(0, 230, 118, 0.2);
            border: 1px solid #00e676;
            color: #b3ffcc;
        }
        @keyframes slideIn {
            from { transform: translateX(20px); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }

        /* Реклама */
        .ad-container {
            background: rgba(255, 215, 0, 0.1);
            border: 1px solid rgba(255, 215, 0, 0.3);
            border-radius: 24px;
            padding: 12px;
            margin: 20px 0;
            text-align: center;
        }

        /* Счётчик лопнутых сфер */
        .score-board {
            position: fixed;
            top: 20px;
            right: 80px;
            background: rgba(10,10,30,0.8);
            border: 1px solid var(--neon-purple);
            border-radius: 30px;
            padding: 8px 22px;
            font-weight: bold;
            z-index: 100;
            display: flex;
            align-items: center;
            gap: 8px;
            backdrop-filter: blur(10px);
        }

        /* Антистресс-сферы (неоновые) */
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
            box-shadow: 0 0 20px currentColor;
            animation: floatSphere 8s ease-in-out infinite;
        }
        @keyframes floatSphere {
            0%, 100% { transform: translateY(0) translateX(0); }
            25% { transform: translateY(-25px) translateX(15px); }
            50% { transform: translateY(10px) translateX(-15px); }
            75% { transform: translateY(-10px) translateX(20px); }
        }
        @keyframes popExplosion {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(2); opacity: 0.8; filter: blur(1px); }
            100% { transform: scale(0); opacity: 0; }
        }
        .pop-animation { animation: popExplosion 0.3s ease-out forwards; }

        /* Достижение */
        .achievement {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0);
            background: linear-gradient(135deg, #ffd700, #ffaa00);
            color: #000;
            font-size: 4rem;
            font-weight: bold;
            padding: 20px 40px;
            border-radius: 60px;
            z-index: 200;
            white-space: nowrap;
            box-shadow: 0 0 50px gold;
            animation: achievementPop 0.5s ease-out forwards;
            pointer-events: none;
        }
        @keyframes achievementPop {
            0% { transform: translate(-50%, -50%) scale(0); }
            50% { transform: translate(-50%, -50%) scale(1.3); }
            100% { transform: translate(-50%, -50%) scale(1); }
        }
        .achievement-fade { animation: achievementFade 2s ease-in forwards; }
        @keyframes achievementFade {
            0% { opacity: 1; transform: translate(-50%, -50%) scale(1); }
            80% { opacity: 1; }
            100% { opacity: 0; transform: translate(-50%, -50%) scale(1.5); }
        }

        @media (max-width: 600px) {
            .cyber-card { padding: 25px; }
            h1 { font-size: 2.2rem; }
        }
    </style>
</head>
<body>
    <div class="bg-grid"></div>
    <div class="particles" id="particles"></div>
    <div id="spheresContainer"></div>
    <div class="score-board"><span>💥</span><span id="scoreValue">0</span></div>
    <div class="container">
        <div class="cyber-card">
            <div class="logo-icon">🎬</div>
            <h1>VIDEOSAVE</h1>
            <p class="subtitle">НЕЙРО-ЗАГРУЗЧИК МЕДИА</p>
            <div class="platforms">
                <span class="platform-badge">YouTube</span>
                <span class="platform-badge">RuTube</span>
                <span class="platform-badge">VK</span>
                <span class="platform-badge">Twitch</span>
                <span class="platform-badge">TikTok</span>
            </div>
            <div class="status-bar">
                <strong>📊 СТАТУС</strong>
                <span id="premiumStatus">🔍 Загрузка...</span>
                <span class="online-counter" id="onlineCounter">
                    <span class="online-dot"></span>
                    <span id="onlineCount">0</span> онлайн
                </span>
            </div>
            <div id="alertContainer"></div>
            <div id="adBlock" class="ad-container" style="display: none;">
                <div style="color: var(--neon-yellow);">РЕКЛАМА</div>
                <p style="margin: 10px 0;">Отключи рекламу навсегда за 800₽ или оформи Premium</p>
                <a href="#premium-section" class="btn-premium" style="font-size: 0.8rem; padding: 6px 16px;">🔓 Отключить</a>
            </div>
            <input type="text" id="videoUrl" class="url-input" placeholder="ВСТАВЬТЕ ССЫЛКУ НА ВИДЕО ИЛИ ПЛЕЙЛИСТ...">
            <button class="btn" onclick="getVideoInfo()">⚡ ПОЛУЧИТЬ ИНФОРМАЦИЮ</button>
            <div class="loader-container" id="loader" style="display:none;">
                <div class="neon-spinner"></div>
                <p style="margin-top:15px; color: var(--neon-purple);">ОБРАБОТКА...</p>
            </div>
            <div id="videoInfo" style="display:none; margin-top:30px;">
                <img id="videoThumbnail" style="width:100%; border-radius: 24px; border: 2px solid var(--card-border);">
                <h3 id="videoTitle" style="margin: 20px 0 10px;"></h3>
                <div id="videoDuration" style="color:var(--text-secondary); margin:10px 0;"></div>
                <div class="formats-grid" id="formatsList"></div>
                <div style="margin-top: 20px; display: flex; gap: 15px; flex-wrap: wrap;">
                    <button class="btn" id="downloadBtn" onclick="downloadVideo()" style="flex:1;">⬇️ СКАЧАТЬ ВИДЕО</button>
                    <button class="btn-mp3" id="downloadMp3Btn" onclick="downloadMp3()" style="display: none; flex:1;">🎵 СКАЧАТЬ MP3</button>
                </div>
            </div>
            <div class="playlist-panel" id="playlistPanel">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h3>📋 ПЛЕЙЛИСТ</h3>
                    <button class="btn-premium" id="downloadPlaylistBtn">⬇️ СКАЧАТЬ ВЫБРАННОЕ</button>
                </div>
                <div class="playlist-videos" id="playlistVideos"></div>
            </div>
            <div style="margin-top:30px; text-align:center; display:none;" id="premiumCard">
                <div style="font-size:2.5rem;">✨</div>
                <h3>ОТКЛЮЧИ РЕКЛАМУ</h3>
                <p style="margin-bottom:20px;">Безлимит, 4K, плейлисты, MP3</p>
                <div id="premium-section" style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap;">
                    <a href="#" class="btn-premium" id="payMonthBtn">🔓 Месяц — 50₽</a>
                    <a href="#" class="btn-premium" id="payForeverBtn" style="background: linear-gradient(135deg, #00e676, #00c853);">⭐ НАВСЕГДА — 800₽</a>
                    <a href="#" class="btn-premium" id="payYearBtn" style="background: linear-gradient(135deg, #4d7cff, #1d4ed8);">💎 Год — 650₽</a>
                </div>
            </div>
            <div style="text-align:center; margin-top:30px; font-size:0.8rem; color: var(--text-secondary);">
                <p>© 2026 VideoSave — нейро-загрузчик | <a href="/return-policy">Возврат</a> | <a href="/requisites/secret">Реквизиты</a></p>
            </div>
        </div>
    </div>

    <script>
        // Частицы
        const particlesContainer = document.getElementById('particles');
        for (let i = 0; i < 40; i++) {
            const particle = document.createElement('div');
            particle.className = 'particle';
            particle.style.left = Math.random() * 100 + '%';
            particle.style.bottom = '-10px';
            particle.style.animationDuration = (Math.random() * 10 + 8) + 's';
            particle.style.animationDelay = Math.random() * 5 + 's';
            particle.style.background = ['#b44dff','#4d7cff','#00e5ff','#ff4d8c'][Math.floor(Math.random()*4)];
            particlesContainer.appendChild(particle);
        }

        let userId = localStorage.getItem('videoSaveUserId');
        if (!userId) {
            userId = crypto.randomUUID ? crypto.randomUUID() : 'user_' + Date.now() + '_' + Math.random().toString(36);
            localStorage.setItem('videoSaveUserId', userId);
        }
        let currentVideoUrl = null;
        let currentVideoInfo = null;
        let currentPlaylist = null;
        let selectedFormat = null;
        let isPremiumUser = false;

        function getHeaders() {
            return { 'Content-Type': 'application/json', 'X-User-Id': userId };
        }

        async function checkPremiumStatus() {
            try {
                const response = await fetch('/api/premium-status', { headers: getHeaders() });
                const data = await response.json();
                isPremiumUser = data.is_premium;
                document.getElementById('premiumStatus').innerHTML = data.is_premium ? 
                    '<span class="premium-badge">⭐ PREMIUM до ' + data.expire_date + '</span>' :
                    '<span class="free-badge">🔓 Бесплатно (' + data.downloads_left + ' из 3 скачиваний)</span>';
                document.getElementById('premiumCard').style.display = data.is_premium ? 'none' : 'block';
                document.getElementById('adBlock').style.display = (data.show_ad && !data.is_premium) ? 'block' : 'none';
                document.getElementById('payMonthBtn').href = '/create_yookassa_payment?plan=month';
                document.getElementById('payYearBtn').href = '/create_yookassa_payment?plan=year';
                document.getElementById('payForeverBtn').href = '/create_yookassa_payment?plan=forever';
                document.getElementById('downloadMp3Btn').style.display = data.is_premium ? 'inline-block' : 'none';
            } catch(e) { console.error(e); }
        }

        let lastOnline = 0;
        async function updateOnlineCounter() {
            try {
                const resp = await fetch('/api/online');
                const data = await resp.json();
                const newCount = data.online || 0;
                animateNumber(document.getElementById('onlineCount'), lastOnline, newCount);
                lastOnline = newCount;
            } catch(e) {}
        }
        function animateNumber(el, start, end) {
            const dur = 400;
            const step = end > start ? 1 : -1;
            const interval = Math.abs(Math.floor(dur / (end - start))) || 20;
            let cur = start;
            const timer = setInterval(() => {
                cur += step;
                el.textContent = cur;
                if (cur === end) clearInterval(timer);
            }, interval);
        }
        setInterval(updateOnlineCounter, 30000);
        updateOnlineCounter();

        // Антистресс сферы (неоновые)
        let score = 0, spheres = [], achievementShown = false;
        const spheresContainer = document.getElementById('spheresContainer');
        const scoreElement = document.getElementById('scoreValue');
        const colors = ['#b44dff','#4d7cff','#00e5ff','#ff4d8c','#ffd700'];
        function createSphere() {
            const sphere = document.createElement('div');
            sphere.className = 'pop-sphere';
            const size = Math.random() * 35 + 25;
            sphere.style.width = size + 'px';
            sphere.style.height = size + 'px';
            sphere.style.left = Math.random() * (window.innerWidth - 80) + 'px';
            sphere.style.top = Math.random() * (window.innerHeight - 80) + 'px';
            const color = colors[Math.floor(Math.random() * colors.length)];
            sphere.style.background = `radial-gradient(circle at 30% 30%, ${color}, #000)`;
            sphere.style.color = color;
            sphere.addEventListener('click', (e) => { e.stopPropagation(); popSphere(sphere); });
            spheresContainer.appendChild(sphere);
            spheres.push(sphere);
            setTimeout(() => { if(sphere.parentNode) { sphere.remove(); spheres = spheres.filter(s => s !== sphere); } }, 14000);
        }
        function popSphere(sphere) {
            sphere.classList.add('pop-animation');
            score++;
            scoreElement.textContent = score;
            if (score === 100 && !achievementShown) {
                achievementShown = true;
                const ach = document.createElement('div');
                ach.className = 'achievement';
                ach.textContent = 'ТЫКУН!';
                document.body.appendChild(ach);
                for (let i=0;i<80;i++) {
                    const conf = document.createElement('div');
                    conf.className = 'particle';
                    conf.style.position = 'fixed';
                    conf.style.left = Math.random() * window.innerWidth + 'px';
                    conf.style.top = Math.random() * window.innerHeight + 'px';
                    conf.style.background = colors[Math.floor(Math.random()*colors.length)];
                    conf.style.width = '8px'; conf.style.height = '8px';
                    conf.style.zIndex = '150';
                    conf.style.animation = 'confettiFall 3s ease-out forwards';
                    document.body.appendChild(conf);
                    setTimeout(() => conf.remove(), 3000);
                }
                setTimeout(() => ach.classList.add('achievement-fade'), 1500);
                setTimeout(() => ach.remove(), 3500);
            }
            setTimeout(() => { if(sphere.parentNode) sphere.remove(); spheres = spheres.filter(s => s !== sphere); }, 300);
        }
        setInterval(() => { if(spheres.length < 25) createSphere(); }, 1800);
        for(let i=0;i<12;i++) setTimeout(createSphere, i*250);

        function showAlert(msg, type) {
            const cont = document.getElementById('alertContainer');
            cont.innerHTML = `<div class="alert alert-${type}">${type==='error'?'⚠️':'✅'} ${msg}</div>`;
            setTimeout(() => cont.innerHTML = '', 4000);
        }

        async function getVideoInfo() {
            const url = document.getElementById('videoUrl').value.trim();
            if(!url) { showAlert('Введите ссылку', 'error'); return; }
            currentVideoUrl = url;
            document.getElementById('loader').style.display = 'flex';
            document.getElementById('videoInfo').style.display = 'none';
            document.getElementById('playlistPanel').style.display = 'none';
            try {
                const resp = await fetch('/api/video-info', { method: 'POST', headers: getHeaders(), body: JSON.stringify({ url }) });
                const data = await resp.json();
                document.getElementById('loader').style.display = 'none';
                if(data.error) { showAlert(data.error, 'error'); return; }
                if(data.is_playlist) {
                    currentPlaylist = data;
                    document.getElementById('playlistPanel').style.display = 'block';
                    const videosDiv = document.getElementById('playlistVideos');
                    videosDiv.innerHTML = '';
                    data.videos.forEach(v => {
                        const div = document.createElement('div');
                        div.className = 'playlist-video-item';
                        div.innerHTML = `<input type="checkbox" value="${v.url}"> <label>${v.title}</label> <span>${Math.floor(v.duration/60)}:${(v.duration%60).toString().padStart(2,'0')}</span>`;
                        videosDiv.appendChild(div);
                    });
                } else {
                    currentVideoInfo = data;
                    document.getElementById('videoThumbnail').src = data.thumbnail || '';
                    document.getElementById('videoTitle').innerText = data.title;
                    document.getElementById('videoDuration').innerHTML = `⏱️ ${Math.floor(data.duration/60)}:${(data.duration%60).toString().padStart(2,'0')}`;
                    const list = document.getElementById('formatsList');
                    list.innerHTML = '';
                    selectedFormat = null;
                    data.formats.forEach(f => {
                        const div = document.createElement('div');
                        div.className = 'format-card';
                        const isLocked = !isPremiumUser && f.resolution !== '480p' && f.resolution !== '360p';
                        if(isLocked) div.classList.add('premium-locked');
                        div.innerHTML = `<strong>${f.resolution}</strong><br><small>${f.ext} · ${f.filesize_mb} MB</small>`;
                        if(!isLocked) div.onclick = () => {
                            selectedFormat = f.format_id;
                            document.querySelectorAll('.format-card').forEach(c => c.classList.remove('selected'));
                            div.classList.add('selected');
                        };
                        list.appendChild(div);
                    });
                    const first = document.querySelector('.format-card:not(.premium-locked)');
                    if(first) first.click();
                    document.getElementById('videoInfo').style.display = 'block';
                }
            } catch(e) { 
                document.getElementById('loader').style.display = 'none'; 
                showAlert('Ошибка сервера', 'error'); 
            }
        }

        async function downloadVideo() {
            if(!selectedFormat || !currentVideoUrl) { showAlert('Выберите качество', 'error'); return; }
            try {
                const resp = await fetch('/api/download', { method: 'POST', headers: getHeaders(), body: JSON.stringify({ url: currentVideoUrl, format_id: selectedFormat }) });
                if(!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
                const blob = await resp.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'video.mp4';
                a.click();
                showAlert('Скачивание началось!', 'success');
                checkPremiumStatus();
            } catch(e) { showAlert(e.message, 'error'); }
        }

        async function downloadMp3() {
            if(!currentVideoUrl) return showAlert('Сначала получите информацию', 'error');
            if(!isPremiumUser) return showAlert('MP3 только для Premium', 'error');
            try {
                const resp = await fetch('/api/download-mp3', { method: 'POST', headers: getHeaders(), body: JSON.stringify({ url: currentVideoUrl }) });
                if(!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
                const blob = await resp.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'audio.mp3';
                a.click();
                showAlert('MP3 готов!', 'success');
            } catch(e) { showAlert(e.message, 'error'); }
        }

        document.getElementById('downloadPlaylistBtn').addEventListener('click', async () => {
            if(!currentPlaylist) return;
            if(!isPremiumUser) return showAlert('Плейлисты только для Premium', 'error');
            const selected = [...document.querySelectorAll('.playlist-video-item input:checked')].map(cb => cb.value);
            if(!selected.length) return showAlert('Выберите видео', 'error');
            try {
                const resp = await fetch('/api/download-playlist', { method: 'POST', headers: getHeaders(), body: JSON.stringify({ playlist_url: currentVideoUrl, selected_videos: selected }) });
                if(!resp.ok) { const e = await resp.json(); throw new Error(e.error); }
                const blob = await resp.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'playlist.zip';
                a.click();
                showAlert('Плейлист скачан!', 'success');
            } catch(e) { showAlert(e.message, 'error'); }
        });

        document.getElementById('videoUrl').addEventListener('keypress', e => { if(e.key === 'Enter') getVideoInfo(); });
        checkPremiumStatus();
    </script>
</body>
</html>
"""

# ---------- МАРШРУТЫ ----------
@app.route('/')
def index():
    user_id = get_user_id()
    update_online_status(user_id)
    resp = make_response(render_template_string(HTML_TEMPLATE))
    set_user_id_cookie(resp, user_id)
    return resp

@app.route('/api/premium-status')
def api_premium_status():
    user_id = request.cookies.get('videoSaveUserId')
    if not user_id:
        return jsonify({'is_premium': False, 'expire_date': None, 'downloads_left': MAX_FREE_DOWNLOADS_PER_WEEK, 'show_ad': True})
    week_key = get_week_key()
    downloads_week = DOWNLOAD_STATS.get(user_id, {}).get(week_key, 0)
    downloads_left = max(0, MAX_FREE_DOWNLOADS_PER_WEEK - downloads_week)
    show_ad = should_show_ad(user_id)
    if is_premium(user_id):
        premium_users = load_premium_users()
        expire_date = premium_users[user_id].get('expire', '2099-12-31') if user_id in premium_users else None
        return jsonify({'is_premium': True, 'expire_date': expire_date, 'downloads_left': downloads_left, 'show_ad': False})
    else:
        return jsonify({'is_premium': False, 'expire_date': None, 'downloads_left': downloads_left, 'show_ad': show_ad})

@app.route('/api/online')
def api_online():
    return jsonify({'online': len(ONLINE_USERS)})

@app.route('/api/video-info', methods=['POST'])
@rate_limit(20, 60)
def api_video_info():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url: return jsonify({'error': 'URL не указан'}), 400
    if 'playlist' in url or 'list=' in url:
        info, err = get_playlist_info(url)
        if err: return jsonify({'error': err}), 400
        if info:
            info['is_playlist'] = True
            return jsonify(info)
    info, err = get_video_info(url)
    if err: return jsonify({'error': err}), 400
    info['is_playlist'] = False
    return jsonify(info)

@app.route('/api/download', methods=['POST'])
@rate_limit(10, 60)
def api_download():
    data = request.get_json()
    url = data.get('url', '').strip()
    fid = data.get('format_id', 'best')
    if not url: return jsonify({'error': 'URL не указан'}), 400
    user_id = request.cookies.get('videoSaveUserId') or str(uuid.uuid4())
    ok, err = check_download_limit(user_id)
    if not ok: return jsonify({'error': err}), 403
    path, err = download_video(url, fid)
    if err: return jsonify({'error': err}), 400
    if not path or not os.path.exists(path): return jsonify({'error': 'Не удалось скачать'}), 500
    increment_download_count(user_id)
    @after_this_request
    def remove(resp):
        try:
            if os.path.exists(path): os.remove(path)
        except: pass
        return resp
    return send_file(path, as_attachment=True, download_name='video.mp4')

@app.route('/api/download-mp3', methods=['POST'])
@rate_limit(10, 60)
def api_download_mp3():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url: return jsonify({'error': 'URL не указан'}), 400
    user_id = request.cookies.get('videoSaveUserId') or str(uuid.uuid4())
    if not is_premium(user_id): return jsonify({'error': 'Конвертация в MP3 доступна только в Premium'}), 403
    video_path, err = download_video(url, 'bestaudio')
    if err: return jsonify({'error': err}), 400
    mp3_path = video_path.replace('.mp4', '.mp3').replace('.webm', '.mp3')
    if not convert_to_mp3(video_path, mp3_path):
        return jsonify({'error': 'Ошибка конвертации в MP3'}), 500
    try: os.remove(video_path)
    except: pass
    @after_this_request
    def remove_mp3(resp):
        try:
            if os.path.exists(mp3_path): os.remove(mp3_path)
        except: pass
        return resp
    return send_file(mp3_path, as_attachment=True, download_name='audio.mp3')

@app.route('/api/download-playlist', methods=['POST'])
@rate_limit(5, 120)
def api_download_playlist():
    data = request.get_json()
    playlist_url = data.get('playlist_url', '').strip()
    selected_videos = data.get('selected_videos', [])
    if not playlist_url or not selected_videos: return jsonify({'error': 'Не указаны параметры'}), 400
    user_id = request.cookies.get('videoSaveUserId') or str(uuid.uuid4())
    if not is_premium(user_id): return jsonify({'error': 'Скачивание плейлистов доступно только в Premium'}), 403
    temp_dir = os.path.join(DOWNLOAD_FOLDER, f'playlist_{uuid.uuid4()}')
    os.makedirs(temp_dir, exist_ok=True)
    zip_path, err = download_playlist(playlist_url, selected_videos, temp_dir)
    try: import shutil; shutil.rmtree(temp_dir)
    except: pass
    if err: return jsonify({'error': err}), 500
    @after_this_request
    def remove_zip(resp):
        try:
            if os.path.exists(zip_path): os.remove(zip_path)
        except: pass
        return resp
    return send_file(zip_path, as_attachment=True, download_name='playlist.zip')

@app.route('/create_yookassa_payment')
def create_yookassa_payment():
    user_id = request.cookies.get('videoSaveUserId') or str(uuid.uuid4())
    plan = request.args.get('plan', 'month')
    amount = PRICES.get(plan, 50)
    days = {'month': 30, 'year': 365, 'forever': 36500}.get(plan, 30)
    try:
        return_url = f"https://video-downloader-r3y6.onrender.com/payment_success_yookassa?user_id={user_id}&plan={plan}"
        payment = Payment.create({
            "amount": {"value": str(amount), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": f"Отключение рекламы - {plan}",
            "metadata": {"user_id": user_id, "plan": plan, "days": days}
        })
        return redirect(payment.confirmation.confirmation_url)
    except Exception as e:
        return f"Ошибка: {e}"

@app.route('/payment_success_yookassa')
def payment_success_yookassa():
    user_id = request.args.get('user_id') or request.cookies.get('videoSaveUserId')
    plan = request.args.get('plan', 'month')
    if user_id:
        if plan == 'forever': add_forever(user_id)
        else: add_premium(user_id, 365 if plan == 'year' else 30)
    return '''
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>Успех</title>
    <meta http-equiv="refresh" content="3;url=/">
    <style>body{background:#0a0a1a;color:white;text-align:center;padding:80px;font-family:Inter;}</style>
    </head><body><h1 style="color:#00e676;">✅ Оплата прошла успешно!</h1><p>Перенаправление...</p></body></html>'''

@app.route('/yookassa-webhook', methods=['POST'])
def yookassa_webhook():
    data = request.json
    if data.get('event') == 'payment.succeeded':
        payment = data.get('object', {})
        metadata = payment.get('metadata', {})
        user_id = metadata.get('user_id')
        plan = metadata.get('plan', 'month')
        if user_id:
            if plan == 'forever': add_forever(user_id)
            else: add_premium(user_id, int(metadata.get('days', 30)))
    return jsonify({'status': 'ok'}), 200

@app.route('/force-premium')
def force_premium():
    user_id = request.cookies.get('videoSaveUserId') or str(uuid.uuid4())
    add_premium(user_id, 30)
    return f'<h1>✅ Премиум активирован для {user_id}</h1><meta http-equiv="refresh" content="2;url=/">'

@app.route('/requisites')
def requisites_redirect():
    return redirect(url_for('index'))

@app.route('/requisites/secret', methods=['GET', 'POST'])
def requisites_secret():
    if request.method == 'POST':
        if request.form.get('password') == SECRET_REQUISITES_KEY:
            session['requisites_auth'] = True
            return redirect(url_for('requisites_secret'))
        else:
            return '<h1>🔒 Неверный пароль</h1><a href="/requisites/secret">Попробовать снова</a>'
    if session.get('requisites_auth'):
        return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Реквизиты</title>
        <style>body{background:#0a0a1a;color:#e0e0f0;font-family:Inter;padding:40px;} 
        .card{background:rgba(20,20,40,0.6);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid #b44dff;}
        h1{color:#b44dff}h2{color:#ffd700}</style></head><body><div class="card">
        <h1>🔐 Реквизиты</h1><p><strong>ФИО:</strong> Юренко Богдан Петрович</p>
        <p><strong>ИНН:</strong> 231408820790</p><p><strong>Статус:</strong> Самозанятый</p>
        <hr><p><strong>Email:</strong> bogdanyrenko@gmail.com</p>
        <h2>📋 Условия</h2><ul><li>Оплата через ЮKassa</li><li>Месяц — 50₽</li><li>Навсегда — 800₽</li><li>Год — 650₽</li></ul>
        <h2>↩️ Возврат</h2><ul><li>14 дней, связь: bogdanyrenko@gmail.com</li></ul>
        <p><a href="/logout-requisites">Выйти</a> | <a href="/">На главную</a></p>
        </div></body></html>'''
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Пароль</title>
    <style>body{background:#0a0a1a;color:white;text-align:center;padding:50px;font-family:Inter;}
    .card{background:rgba(20,20,40,0.6);padding:30px;border-radius:24px;max-width:400px;margin:auto;border:1px solid #b44dff;}
    input,button{padding:10px;margin:10px;border-radius:8px;border:none}button{background:#b44dff;color:white;}</style>
    </head><body><div class="card"><h1>🔒 Доступ</h1><form method="POST">
    <input type="password" name="password" placeholder="Пароль"><br><button type="submit">Войти</button></form></div></body></html>'''

@app.route('/logout-requisites')
def logout_requisites():
    session.pop('requisites_auth', None)
    return redirect(url_for('index'))

@app.route('/return-policy')
def return_policy():
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Возврат</title>
    <style>body{background:#0a0a1a;color:#e0e0f0;font-family:Inter;padding:40px;}
    .card{background:rgba(20,20,40,0.6);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid #b44dff;}
    h1{color:#b44dff}</style></head><body><div class="card"><h1>📋 Политика возврата</h1>
    <h2>Условия оплаты</h2><ul><li>ЮKassa</li><li>Месяц — 50₽</li><li>Навсегда — 800₽</li><li>Год — 650₽</li></ul>
    <h2>Условия возврата</h2><ul><li>14 дней</li><li>Email: bogdanyrenko@gmail.com</li></ul>
    <a href="/">← На главную</a></div></body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7860)
  