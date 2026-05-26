import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>VideoSave</title>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial; text-align: center; padding: 50px; background: #0f0c29; color: white; }
            h1 { color: #a855f7; }
        </style>
    </head>
    <body>
        <h1>🎬 VideoSave</h1>
        <p>Сайт восстанавливается. Полная версия скоро вернется.</p>
        <p>Код ошибки найден, исправляем...</p>
    </body>
    </html>
    """

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))