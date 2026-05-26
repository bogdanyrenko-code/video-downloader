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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ssl._create_default_https_context = ssl._create_unverified_context

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-key-2024-change-me')

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
    else:
        PREMIUM_USERS = {}

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
    match = re.search(r'rutube\.ru/embed/([a-f0-9]+)', url)
    if match:
        return match.group(1)
    return None

def get_rutube_video_info(url):
    video_id = extract_rutube_id(url)
    if not video_id:
        return None, "Не удалось определить ID видео"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Accept': 'application/json'}
    try:
        api_url = f"https://rutube.ru/api/video/{video_id}/"
        resp = requests.get(api_url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            formats = [{'format_id': 'rutube_best', 'resolution': 'Лучшее качество', 'ext': 'mp4', 'filesize_mb': '?'}]
            return {'title': data.get('title', 'RuTube видео'), 'thumbnail': data.get('thumbnail_url', ''), 'duration': data.get('duration', 0), 'formats': formats}, None
        return None, "Не удалось получить информацию о видео"
    except Exception as e:
        logger.error(f"Ошибка получения RuTube видео: {e}")
        return None, str(e)

def download_rutube_video(url, format_id='best'):
    try:
        ydl_opts = {'format': 'best', 'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(id)s.%(ext)s'), 'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, None
    except Exception as e:
        logger.error(f"Ошибка скачивания RuTube: {e}")
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
        logger.error(f"Ошибка получения информации о видео: {e}")
        return None, str(e)

def download_video(url, format_id='best'):
    if 'rutube.ru' in url:
        return download_rutube_video(url, format_id)
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
        logger.error(f"Ошибка скачивания видео: {e}")
        return None, str(e)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VideoSave — Скачивай видео легко</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        .header { text-align: center; color: white; margin-bottom: 40px; }
        .header h1 { font-size: 3em; margin-bottom: 10px; text-shadow: 2px 2px 4px rgba(0,0,0,0.2); }
        .header p { font-size: 1.2em; opacity: 0.9; }
        .card { background: white; border-radius: 20px; padding: 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); margin-bottom: 20px; }
        .premium-badge { display: inline-block; background: linear-gradient(135deg, #ffd700, #ffed4e); color: #333; padding: 5px 15px; border-radius: 20px; font-weight: bold; font-size: 0.9em; margin-left: 10px; }
        .user-info { background: #f8f9fa; padding: 15px; border-radius: 10px; margin-bottom: 20px; }
        .input-group { margin-bottom: 20px; }
        .input-group label { display: block; margin-bottom: 8px; font-weight: 600; color: #333; }
        .input-group input, .input-group select { width: 100%; padding: 15px; border: 2px solid #e0e0e0; border-radius: 10px; font-size: 16px; transition: all 0.3s; }
        .input-group input:focus, .input-group select:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }
        .btn { width: 100%; padding: 15px; border: none; border-radius: 10px; font-size: 18px; font-weight: 600; cursor: pointer; transition: all 0.3s; text-transform: uppercase; letter-spacing: 1px; }
        .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3); }
        .btn-success { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white; margin-top: 10px; }
        .btn-success:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(56, 239, 125, 0.3); }
        .video-info { display: none; margin-top: 20px; padding: 20px; background: #f8f9fa; border-radius: 10px; }
        .video-info img { width: 100%; border-radius: 10px; margin-bottom: 15px; }
        .formats { display: grid; gap: 10px; margin-top: 15px; }
        .format-option { padding: 12px; background: white; border: 2px solid #e0e0e0; border-radius: 8px; cursor: pointer; transition: all 0.3s; }
        .format-option:hover { border-color: #667eea; background: #f0f4ff; }
        .format-option.selected { border-color: #667eea; background: #e8eeff; }
        .alert { padding: 15px; border-radius: 10px; margin-bottom: 20px; }
        .alert-error { background: #fee; color: #c33; border: 1px solid #fcc; }
        .alert-success { background: #efe; color: #3c3; border: 1px solid #cfc; }
        .loader { display: none; text-align: center; padding: 20px; }
        .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #667eea; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 30px; }
        .feature { text-align: center; color: white; }
        .feature-icon { font-size: 3em; margin-bottom: 10px; }
        .supported-platforms { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; justify-content: center; }
        .platform-tag { background: rgba(255, 255, 255, 0.2); color: white; padding: 8px 15px; border-radius: 20px; font-size: 0.9em; }
        @media (max-width: 600px) { .header h1 { font-size: 2em; } .card { padding: 20px; } }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🎥 VideoSave</h1>
        <p>Скачивайте видео с YouTube, RuTube и других платформ</p>
        <div class="supported-platforms">
            <span class="platform-tag">YouTube</span>
            <span class="platform-tag">RuTube</span>
            <span class="platform-tag">VK</span>
            <span class="platform-tag">Twitch</span>
            <span class="platform-tag">TikTok</span>
            <span class="platform-tag">Instagram</span>
        </div>
    </div>
    <div class="card">
        <div class="user-info">
            <strong>Статус:</strong>
            {% if is_premium %}
                <span class="premium-badge">⭐ PREMIUM</span>
                <div style="margin-top: 10px; font-size: 0.9em;">Действует до: {{ premium_expire }}</div>
            {% else %}
                Бесплатный аккаунт
                <div style="margin-top: 10px; font-size: 0.9em;">Скачиваний сегодня: {{ downloads_today }}/{{ max_downloads }}</div>
            {% endif %}
        </div>
        <div id="alertContainer"></div>
        <div class="input-group">
            <label for="videoUrl">🔗 Вставьте ссылку на видео</label>
            <input type="text" id="videoUrl" placeholder="https://youtube.com/watch?v=..." autocomplete="off">
        </div>
        <button class="btn btn-primary" onclick="getVideoInfo()">Получить информацию</button>
        <div class="loader" id="loader"><div class="spinner"></div><p style="margin-top: 10px;">Загрузка...</p></div>
        <div class="video-info" id="videoInfo">
            <img id="videoThumbnail" src="" alt="Превью">
            <h3 id="videoTitle"></h3>
            <div id="videoDuration"></div>
            <div class="input-group">
                <label>📊 Выберите качество</label>
                <div class="formats" id="formatsList"></div>
            </div>
            <button class="btn btn-success" onclick="downloadVideo()">⬇️ Скачать видео</button>
        </div>
    </div>
    {% if not is_premium %}
    <div class="card" style="background: linear-gradient(135deg, #667eea, #764ba2); color: white;">
        <h2 style="margin-bottom: 20px; text-align: center;">✨ Премиум возможности</h2>
        <div class="features">
            <div class="feature"><div class="feature-icon">🚀</div><h3>Без ограничений</h3><p>Безлимитные скачивания</p></div>
            <div class="feature"><div class="feature-icon">📁</div><h3>Большие файлы</h3><p>До 500 МБ</p></div>
            <div class="feature"><div class="feature-icon">⚡</div><h3>Приоритет</h3><p>Быстрая скорость</p></div>
            <div class="feature"><div class="feature-icon">🎯</div><h3>HD качество</h3><p>До 4K разрешения</p></div>
        </div>
        <a href="/create_payment" class="btn" style="background: #ffd700; color: #333; display: inline-block; width: auto; margin: 0 auto; text-decoration: none;">🌟 Получить Premium за 50₽/месяц</a>
    </div>
    {% endif %}
    <div class="card" style="text-align: center; font-size: 0.9em; color: #666;">
        <p>Сделано с ❤️ для удобного скачивания видео</p>
        <p style="margin-top: 10px;">© 2026 VideoSave. Все права защищены.</p>
    </div>
</div>
<script>
    let selectedFormat = null;
    let currentVideoUrl = null;

    function showAlert(message, type) {
        const alertContainer = document.getElementById('alertContainer');
        alertContainer.innerHTML = `<div class="alert alert-${type}">${message}</div>`;
        setTimeout(() => { alertContainer.innerHTML = ''; }, 5000);
    }

    function showLoader(show) {
        document.getElementById('loader').style.display = show ? 'block' : 'none';
    }

    async function getVideoInfo() {
        const url = document.getElementById('videoUrl').value.trim();
        if (!url) { showAlert('Введите ссылку на видео', 'error'); return; }
        currentVideoUrl = url;
        showLoader(true);
        document.getElementById('videoInfo').style.display = 'none';
        try {
            const response = await fetch('/api/video-info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: url }) });
            const data = await response.json();
            if (data.error) { showAlert(data.error, 'error'); return; }
            document.getElementById('videoThumbnail').src = data.thumbnail || '';
            document.getElementById('videoTitle').textContent = data.title;
            if (data.duration) {
                const minutes = Math.floor(data.duration / 60);
                const seconds = data.duration % 60;
                document.getElementById('videoDuration').textContent = `⏱️ Длительность: ${minutes}:${seconds.toString().padStart(2, '0')}`;
            }
            const formatsList = document.getElementById('formatsList');
            formatsList.innerHTML = '';
            data.formats.forEach(format => {
                const formatDiv = document.createElement('div');
                formatDiv.className = 'format-option';
                formatDiv.innerHTML = `<strong>${format.resolution}</strong> <span style="float: right;">${format.ext.toUpperCase()} · ${format.filesize_mb} МБ</span>`;
                formatDiv.onclick = () => selectFormat(format.format_id, formatDiv);
                formatsList.appendChild(formatDiv);
            });
            if (data.formats.length > 0) { selectFormat(data.formats[0].format_id, formatsList.firstChild); }
            document.getElementById('videoInfo').style.display = 'block';
            showAlert('Информация о видео загружена!', 'success');
        } catch (error) { showAlert('Ошибка: ' + error.message, 'error'); } finally { showLoader(false); }
    }

    function selectFormat(formatId, element) {
        selectedFormat = formatId;
        document.querySelectorAll('.format-option').forEach(el => el.classList.remove('selected'));
        element.classList.add('selected');
    }

    async function downloadVideo() {
        if (!selectedFormat || !currentVideoUrl) { showAlert('Выберите качество видео', 'error'); return; }
        showLoader(true);
        try {
            const response = await fetch('/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: currentVideoUrl, format_id: selectedFormat }) });
            if (!response.ok) { const data = await response.json(); throw new Error(data.error || 'Ошибка скачивания'); }
            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            let filename = 'video.mp4';
            const contentDisposition = response.headers.get('Content-Disposition');
            if (contentDisposition) { const match = contentDisposition.match(/filename="?(.+)"?/); if (match) filename = match[1]; }
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(downloadUrl);
            showAlert('Видео успешно скачано! 🎉', 'success');
        } catch (error) { showAlert('Ошибка: ' + error.message, 'error'); } finally { showLoader(false); }
    }

    document.getElementById('videoUrl').addEventListener('keypress', function(e) { if (e.key === 'Enter') getVideoInfo(); });
</script>
</body>
</html>"""

# ---------- Flask Routes ----------
@app.route('/')
def index():
    user_id = get_user_id()
    today = datetime.now().strftime('%Y-%m-%d')
    downloads_today = DOWNLOAD_STATS.get(user_id, {}).get(today, 0)
    premium_expire = PREMIUM_USERS[user_id]['expire'] if is_premium(user_id) else None
    return render_template_string(HTML_TEMPLATE, is_premium=is_premium(user_id), premium_expire=premium_expire, downloads_today=downloads_today, max_downloads=MAX_FREE_DOWNLOADS_PER_DAY)

@app.route('/api/video-info', methods=['POST'])
@rate_limit(max_requests=20, window=60)
def api_video_info():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL не указан'}), 400
        user_id = get_user_id()
        can_download, error = check_download_limit(user_id)
        if not can_download:
            return jsonify({'error': error}), 403
        info, error = get_video_info(url)
        if error:
            return jsonify({'error': error}), 400
        return jsonify(info)
    except Exception as e:
        logger.error(f"Ошибка в api_video_info: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
@rate_limit(max_requests=10, window=60)
def api_download():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        format_id = data.get('format_id', 'best')
        if not url:
            return jsonify({'error': 'URL не указан'}), 400
        user_id = get_user_id()
        can_download, error = check_download_limit(user_id)
        if not can_download:
            return jsonify({'error': error}), 403
        filepath, error = download_video(url, format_id)
        if error:
            return jsonify({'error': error}), 400
        if not filepath or not os.path.exists(filepath):
            return jsonify({'error': 'Не удалось скачать видео'}), 500
        increment_download_count(user_id)

        @after_this_request
        def remove_file(response):
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"Файл удален: {filepath}")
            except Exception as e:
                logger.error(f"Ошибка удаления файла: {e}")
            return response

        filename = os.path.basename(filepath)
        return send_file(filepath, as_attachment=True, download_name=filename, mimetype='video/mp4')
    except Exception as e:
        logger.error(f"Ошибка в api_download: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/premium', methods=['POST'])
def premium():
    user_id = get_user_id()
    days = int(request.form.get('days', 30))
    add_premium(user_id, days)
    return redirect(url_for('index'))

@app.route('/api/stats')
def api_stats():
    total_users = len(set(list(PREMIUM_USERS.keys()) + list(DOWNLOAD_STATS.keys())))
    premium_users = len(PREMIUM_USERS)
    total_downloads = sum(sum(daily.values()) for daily in DOWNLOAD_STATS.values())
    return jsonify({'total_users': total_users, 'premium_users': premium_users, 'total_downloads': total_downloads, 'active_sessions': len(USER_SESSIONS)})

@app.route('/requisites')
def requisites():
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Реквизиты</title><style>body { font-family: Arial; padding: 40px; background: #0f0c29; color: white; }.card { background: rgba(255,255,255,0.1); padding: 30px; border-radius: 20px; max-width: 600px; margin: auto; }h1 { color: #a855f7; }</style></head><body><div class="card"><h1>Реквизиты самозанятого</h1><p><strong>ИНН:</strong> 231408820790</p><p><strong>ФИО:</strong> Богдан</p><p><strong>Статус:</strong> Самозанятый</p><p><strong>Налог:</strong> 4% от доходов</p></div></body></html>'''

@app.route('/create_payment')
def create_payment():
    user_id = get_user_id()
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Оплата подписки</title><style>body {{ font-family: Arial; padding: 40px; background: #0f0c29; color: white; text-align: center; }}.container {{ max-width: 500px; margin: auto; background: rgba(255,255,255,0.1); padding: 30px; border-radius: 20px; }}form {{ display: flex; flex-direction: column; gap: 15px; }}input, button {{ padding: 12px; border-radius: 8px; border: none; font-size: 16px; }}button {{ background: #f59e0b; color: #333; font-weight: bold; cursor: pointer; }}</style></head><body><div class="container"><h1>💎 Оформление Premium</h1><p>Стоимость подписки: <strong>50 ₽ / месяц</strong></p><form action="https://merchant.intellectmoney.ru/ru/payment/" method="POST"><input type="hidden" name="eshopId" value="472541"><input type="hidden"name="paymentAmount" value="50"><input type="hidden" name="paymentCurrency" value="RUB"><input type="hidden" name="paymentDesc" value="Premium подписка на 30 дней"><input type="hidden" name="successUrl" value="https://video-downloader-r3y6.onrender.com/payment_success"><input type="hidden" name="failUrl" value="https://video-downloader-r3y6.onrender.com/create_payment"><input type="hidden" name="user_id" value="{user_id}"><button type="submit">Перейти к оплате 50 ₽</button></form><p class="info" style="margin-top: 20px; font-size: 14px;">Оплата защищена. Данные карты не хранятся на сайте.</p></div></body></html>'''

@app.route('/payment_success', methods=['GET', 'POST'])
def payment_success():
    if request.method == 'POST':
        data = request.form
        logger.info(f"Получено уведомление: {data}")
        if data.get('paymentStatus') == '5':
            user_id = data.get('user_id')
            if user_id:
                add_premium(user_id, 30)
                logger.info(f"✅ Премиум активирован для {user_id} через уведомление")
        return 'OK', 200
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Страница оплаты</title><meta http-equiv="refresh" content="2;url=/"><style>body { font-family: Arial; text-align: center; padding: 50px; background: #0f0c29; color: white; }.loader { margin: 20px auto; width: 40px; height: 40px; border: 4px solid #f3f3f3; border-top: 4px solid #22c55e; border-radius: 50%; animation: spin 1s linear infinite; }@keyframes spin { 100% { transform: rotate(360deg); } }</style></head><body><div class="loader"></div><h1>🔄 Перенаправление...</h1><p>Через секунду вы вернётесь на главную страницу.</p><a href="/" style="color: #a855f7;">Вернуться сейчас</a></body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))