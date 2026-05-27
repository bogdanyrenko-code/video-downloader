import os
import uuid
import time
import ssl
import re
import json
import logging
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

YOOKASSA_SHOP_ID = "1369767"
YOOKASSA_SECRET_KEY = "test_92d73ZaVYlLk9i1BvEwS6p5tflhwj7PSqiutGHHtosY"

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

PREMIUM_FILE = "premium_users.json"
DOWNLOAD_STATS = {}
USER_SESSIONS = {}

MAX_FREE_DOWNLOADS_PER_WEEK = 3
MAX_VIDEO_SIZE_FREE_MB = 200
MAX_VIDEO_SIZE_PREMIUM_MB = 500
CLEANUP_INTERVAL = 3600
FILE_RETENTION_TIME = 1800

SECRET_REQUISITES_KEY = "Bogdan2025Secure"

def load_premium_users():
    """Загружает премиум-пользователей из файла"""
    if os.path.exists(PREMIUM_FILE):
        try:
            with open(PREMIUM_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                now = datetime.now()
                result = {}
                for user_id, data in loaded.items():
                    expire_date = datetime.strptime(data['expire'], '%Y-%m-%d')
                    if expire_date >= now:
                        result[user_id] = data
                logger.info(f"Загружено {len(result)} премиум-пользователей")
                return result
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")
    return {}

def save_premium_users(premium_users):
    """Сохраняет премиум-пользователей в файл"""
    try:
        with open(PREMIUM_FILE, 'w', encoding='utf-8') as f:
            json.dump(premium_users, f, ensure_ascii=False, indent=2)
        logger.info(f"Сохранено {len(premium_users)} премиум-пользователей")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def is_premium(user_id):
    """Проверяет премиум статус (всегда читает из файла)"""
    premium_users = load_premium_users()
    if user_id not in premium_users:
        return False
    expire_date = datetime.strptime(premium_users[user_id]['expire'], '%Y-%m-%d')
    return datetime.now() < expire_date

def add_premium(user_id, days=30):
    """Добавляет премиум подписку (с сохранением в файл)"""
    premium_users = load_premium_users()
    expire_date = datetime.now() + timedelta(days=days)
    premium_users[user_id] = {
        'expire': expire_date.strftime('%Y-%m-%d'),
        'activated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_premium_users(premium_users)
    logger.info(f"Премиум активирован для {user_id} до {expire_date}")

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
        return False, f"Достигнут лимит скачиваний ({MAX_FREE_DOWNLOADS_PER_WEEK} видео в неделю). Купите Premium для безлимита!"
    
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
                            formats.append({
                                'format_id': f['format_id'],
                                'resolution': res_str,
                                'ext': f.get('ext', 'mp4'),
                                'filesize_mb': filesize_mb
                            })
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

# HTML_TEMPLATE (здесь тот же самый, не меняется)
# ... (оставь свой HTML_TEMPLATE без изменений)

HTML_TEMPLATE = """... (твой существующий HTML) ..."""

@app.route('/')
def index():
    user_id = get_user_id()
    resp = make_response(render_template_string(HTML_TEMPLATE))
    set_user_id_cookie(resp, user_id)
    return resp

@app.route('/api/premium-status')
def api_premium_status():
    user_id = request.cookies.get('videoSaveUserId')
    if not user_id:
        return jsonify({'is_premium': False, 'expire_date': None, 'downloads_left': MAX_FREE_DOWNLOADS_PER_WEEK})
    
    week_key = get_week_key()
    downloads_week = DOWNLOAD_STATS.get(user_id, {}).get(week_key, 0)
    downloads_left = max(0, MAX_FREE_DOWNLOADS_PER_WEEK - downloads_week)
    
    if is_premium(user_id):
        premium_users = load_premium_users()
        expire_date = premium_users[user_id]['expire'] if user_id in premium_users else None
        return jsonify({'is_premium': True, 'expire_date': expire_date, 'downloads_left': downloads_left})
    else:
        return jsonify({'is_premium': False, 'expire_date': None, 'downloads_left': downloads_left})

@app.route('/api/video-info', methods=['POST'])
@rate_limit(20, 60)
def api_video_info():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL не указан'}), 400
        
        info, err = get_video_info(url)
        if err:
            return jsonify({'error': err}), 400
        
        user_id = request.cookies.get('videoSaveUserId')
        if user_id:
            info['premium'] = is_premium(user_id)
        else:
            info['premium'] = False
        
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
        
        user_id = request.cookies.get('videoSaveUserId')
        if not user_id:
            user_id = str(uuid.uuid4())
        
        ok, err = check_download_limit(user_id)
        if not ok:
            return jsonify({'error': err}), 403
        
        path, err = download_video(url, fid)
        if err:
            return jsonify({'error': err}), 400
        if not path or not os.path.exists(path):
            return jsonify({'error': 'Не удалось скачать'}), 500
        
        increment_download_count(user_id)
        
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
    user_id = request.cookies.get('videoSaveUserId')
    if not user_id:
        user_id = str(uuid.uuid4())
    
    try:
        return_url = f"https://video-downloader-r3y6.onrender.com/payment_success_yookassa?user_id={user_id}"
        
        payment = Payment.create({
            "amount": {"value": "50.00", "currency": "RUB"},
            "confirmation": {
                "type": "redirect", 
                "return_url": return_url
            },
            "capture": True,
            "description": "Premium подписка на 30 дней",
            "metadata": {"user_id": user_id}
        })
        return redirect(payment.confirmation.confirmation_url)
    except Exception as e:
        logger.error(f"Ошибка при создании платежа: {e}")
        return f"Ошибка: {e}"

@app.route('/payment_success_yookassa')
def payment_success_yookassa():
    user_id = request.args.get('user_id')
    
    if not user_id:
        user_id = request.cookies.get('videoSaveUserId')
    
    if user_id:
        add_premium(user_id, 30)
        logger.info(f"✅ Премиум активирован для {user_id}")
    else:
        logger.error("❌ Не удалось получить user_id")
    
    return '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Оплата прошла успешно</title>
    <meta http-equiv="refresh" content="3;url=/">
    <style>
        body { font-family: Arial; text-align: center; padding: 50px; background: #0f0c29; color: white; }
        h1 { color: #22c55e; }
        .loader {
            margin: 20px auto;
            width: 40px;
            height: 40px;
            border: 4px solid #f3f3f3;
            border-top: 4px solid #22c55e;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="loader"></div>
    <h1>✅ Оплата прошла успешно!</h1>
    <p>Ваша премиум-подписка активирована.</p>
    <p>Перенаправление...</p>
</body>
</html>
    '''

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

@app.route('/force-premium')
def force_premium():
    user_id = request.cookies.get('videoSaveUserId')
    if not user_id:
        user_id = str(uuid.uuid4())
    
    add_premium(user_id, 30)
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Премиум активирован</title>
        <meta http-equiv="refresh" content="2;url=/">
    </head>
    <body>
        <h1>✅ Премиум активирован для {user_id}</h1>
        <p>Перенаправление на главную...</p>
    </body>
    </html>
    '''

@app.route('/requisites')
def requisites_redirect():
    return redirect(url_for('index'))

@app.route('/requisites/secret', methods=['GET', 'POST'])
def requisites_secret():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == SECRET_REQUISITES_KEY:
            session['requisites_auth'] = True
            return redirect(url_for('requisites_secret'))
        else:
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>Доступ запрещён</title>
            <style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;text-align:center;padding:50px}
            .card{background:rgba(20,20,40,0.6);padding:30px;border-radius:24px;max-width:400px;margin:auto}
            input,button{padding:10px;margin:10px;border-radius:8px;border:none}
            button{background:#a855f7;color:white;cursor:pointer}</style>
            </head>
            <body><div class="card"><h1>🔒 Неверный пароль</h1><a href="/requisites/secret">Попробовать снова</a></div></body>
            </html>
            '''
    
    if session.get('requisites_auth'):
        return '''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Реквизиты</title>
<style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;padding:40px}.card{background:rgba(20,20,40,0.6);backdrop-filter:blur(12px);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid rgba(168,85,247,0.3)}h1{color:#a855f7}h2{color:#f59e0b}</style>
</head>
<body>
<div class="card">
    <h1>🔐 Реквизиты самозанятого</h1>
    <p><strong>ФИО:</strong> Юренко Богдан Петрович</p>
    <p><strong>ИНН:</strong> 231408820790</p>
    <p><strong>Статус:</strong> Самозанятый</p>
    <hr>
    <p><strong>Email:</strong> bogdanyrenko@gmail.com</p>
    <p><strong>Сайт:</strong> https://video-downloader-r3y6.onrender.com</p>
    <h2>📋 Условия оплаты</h2>
    <ul><li>Оплата через ЮKassa (банковская карта)</li><li>Стоимость: 50₽/месяц</li></ul>
    <h2>↩️ Условия возврата</h2>
    <ul><li>Возврат в течение 14 дней</li><li>Связь: bogdanyrenko@gmail.com</li></ul>
    <p><a href="/logout-requisites">Выйти</a> | <a href="/">На главную</a></p>
</div>
</body>
</html>'''
    
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>Введите пароль</title>
    <style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;text-align:center;padding:50px}
    .card{background:rgba(20,20,40,0.6);padding:30px;border-radius:24px;max-width:400px;margin:auto}
    input,button{padding:10px;margin:10px;border-radius:8px;border:none}
    button{background:#a855f7;color:white;cursor:pointer}</style>
    </head>
    <body>
    <div class="card">
        <h1>🔒 Доступ к реквизитам</h1>
        <p>Введите пароль для продолжения</p>
        <form method="POST">
            <input type="password" name="password" placeholder="Пароль" autofocus>
            <br>
            <button type="submit">Войти</button>
        </form>
    </div>
    </body>
    </html>
    '''

@app.route('/logout-requisites')
def logout_requisites():
    session.pop('requisites_auth', None)
    return redirect(url_for('index'))

@app.route('/return-policy')
def return_policy():
    return '''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Условия возврата</title><style>body{background:#0f0f1a;color:#e0e0e0;font-family:Arial;padding:40px}.card{background:rgba(20,20,40,0.6);backdrop-filter:blur(12px);padding:30px;border-radius:24px;max-width:700px;margin:auto;border:1px solid rgba(168,85,247,0.3)}h1{color:#a855f7}</style></head>
<body><div class="card"><h1>📋 Политика возврата</h1><h2>Условия оплаты</h2><ul><li>Оплата через ЮKassa</li><li>Стоимость: 50₽/месяц</li></ul><h2>Условия возврата</h2><ul><li>Возврат в течение 14 дней</li><li>Для возврата: bogdanyrenko@gmail.com</li></ul><a href="/">← На главную</a></div></body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))