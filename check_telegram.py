"""Read-only diagnostics. Never prints or saves the Telegram bot token."""
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent


def report(label, message):
    print(label + ': ' + message)


def telegram(token, method):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=b'{}', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        if not result.get('ok'):
            report('ОШИБКА Telegram', 'Запрос отклонён. Проверьте действующий токен.')
            return None
        return result['result']
    except urllib.error.HTTPError as error:
        if error.code in (401, 404):
            report('ОШИБКА токена', 'Telegram не принимает токен. Получите действующий токен в BotFather и сохраните его в .env.')
        elif error.code == 429:
            report('ОШИБКА Telegram', 'Лимит запросов. Подождите несколько минут.')
        else:
            report('ОШИБКА Telegram', 'HTTP ' + str(error.code))
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            report('ОШИБКА SSL', 'Python не доверяет сертификатам Telegram.')
            if sys.platform == 'darwin':
                print('На Mac с Python от python.org: откройте Finder → Программы → Python 3.x → Install Certificates.command. После выполнения перезапустите сервер.')
            else:
                print('Проверьте сертификаты вашей установки Python. Проверку SSL отключать не нужно.')
        else:
            report('ОШИБКА сети', 'Python не смог подключиться к api.telegram.org. Проверьте интернет, VPN/прокси и повторите проверку.')
    except (ssl.SSLError, TimeoutError, OSError):
        report('ОШИБКА соединения', 'Telegram недоступен или проблема TLS. Проверьте интернет и сертификаты Python.')
    except Exception:
        report('ОШИБКА ответа', 'Не удалось прочитать ответ Telegram.')
    return None


def main():
    print('ПРОВЕРКА FAMILY BUDGET / TELEGRAM')
    print('Токен не выводится. Проверка ничего не меняет.')
    if not (ROOT / 'app.py').is_file():
        report('ОШИБКА папки', 'Переместите check_telegram.py в ту же папку, где лежит app.py, и повторите запуск.')
        return
    report('Папка проекта', 'OK')
    config = {}
    env_path = ROOT / '.env'
    if not env_path.is_file():
        report('ОШИБКА .env', 'Рядом с app.py нет файла .env.')
        if (ROOT / '.env.example').is_file():
            print('Переименуйте .env.example в .env. Сохраните токен и перезапустите сервер.')
        if (ROOT / '.env.txt').is_file():
            print('Найден .env.txt: уберите расширение .txt.')
        return
    try:
        raw = env_path.read_text(encoding='utf-8')
        for line in raw.lstrip('\ufeff').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        report('ОШИБКА .env', 'Не удалось прочитать файл как UTF-8. Сохраните его как обычный текст UTF-8.')
        return
    report('Файл .env', 'OK')
    if raw.startswith('\ufeff'):
        report('ПРЕДУПРЕЖДЕНИЕ', 'Файл начинается с BOM. Сохраните .env в UTF-8 без BOM и перезапустите сервер.')
    token = config.get('TELEGRAM_BOT_TOKEN', '')
    override = os.environ.get('TELEGRAM_BOT_TOKEN')
    if override is not None and override != token:
        report('ПРЕДУПРЕЖДЕНИЕ', 'В терминале задан другой TELEGRAM_BOT_TOKEN. Сервер использует его вместо .env. Откройте новый терминал без этой переменной.')
        token = override
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        report('ОШИБКА токена', 'Нужна одна строка из BotFather вида числа:буквы. Название Family Budget не является токеном. Копируйте только токен без текста сообщения.')
        return
    report('Формат токена', 'OK; действительность проверяется далее')
    me = telegram(token, 'getMe')
    if me is None:
        return
    username = me.get('username', '')
    if not re.fullmatch(r'[A-Za-z0-9_]+', username):
        report('ОШИБКА Telegram', 'Ответ не содержит корректного имени бота.')
        return
    report('Подключение Telegram', 'OK, бот @' + username)
    expected = config.get('TELEGRAM_BOT_USERNAME', '').lstrip('@')
    if expected and expected != username:
        report('ПРЕДУПРЕЖДЕНИЕ', 'Имя в .env отличается от имени бота для этого токена. Проверьте, что открываете именно @' + username)
    webhook = telegram(token, 'getWebhookInfo')
    if webhook is not None:
        if webhook.get('url'):
            report('ОШИБКА webhook', 'У бота включён webhook, мешающий getUpdates. Для сайта нужен бот без активного webhook. Сообщите об этой строке — токен не присылайте.')
        else:
            report('Webhook', 'OK, не включён')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4) as response:
            text = response.read(131072)
            if response.status == 200 and b'Family Budget' in text:
                report('Сайт на порту 8000', 'OK')
            else:
                report('ПРЕДУПРЕЖДЕНИЕ сайта', 'Порт 8000 отвечает, но приложение не распознано.')
    except Exception:
        report('ОШИБКА запуска', 'Сайт на порту 8000 недоступен. В отдельном терминале из папки проекта запустите python3 app.py. Для другого порта эта проверка неприменима.')
    path = ROOT / 'data' / 'users.sqlite3'
    if not path.is_file():
        report('ОШИБКА базы', 'Нет стандартной data/users.sqlite3. Зарегистрируйтесь на сайте. Если запускали с --db, используется другая папка данных.')
        return
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'telegram_accounts' not in tables:
                report('ОШИБКА версии', 'В базе нет привязок Telegram. Убедитесь, что запускаете последнюю версию app.py из этой папки.')
            else:
                count = db.execute('SELECT COUNT(*) FROM telegram_accounts').fetchone()[0]
                if not count:
                    report('ОШИБКА привязки', 'Ни один аккаунт не привязан к Telegram. Войдите на сайт, введите текущий пароль, нажмите «Привязать Telegram», откройте ссылку с сайта и нажмите Start.')
                else:
                    report('Привязки Telegram', 'Найдены. Это не подтверждает привязку именно текущего пользователя. Проверьте надпись «Telegram привязан» в аккаунте.')
    except Exception:
        report('ОШИБКА базы', 'Не удалось прочитать привязки. Проверьте папку запуска и права доступа.')
    finally:
        try:
            db.close()
        except Exception:
            pass
    print('Если всё OK, оставьте работающим только один экземпляр python3 app.py и повторите привязку по новой ссылке с сайта.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Проверка остановлена.')
    except Exception:
        print('Проверка остановилась из-за локальной ошибки. Токен не выводился.')

