import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, Update, WebAppInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ['BOT_TOKEN'].strip()
BASE_URL = (os.getenv('PUBLIC_URL') or os.environ['RENDER_EXTERNAL_URL']).rstrip('/')
APP_SECRET = os.environ['APP_SECRET']
if len(APP_SECRET) < 32:
    raise RuntimeError('APP_SECRET должен содержать минимум 32 символа')
DB_PATH = os.getenv('DB_PATH', '/tmp/bot.sqlite3')
HOOK_SECRET = hmac.new(APP_SECRET.encode(), b'telegram-webhook', hashlib.sha256).hexdigest()
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            checked_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS browsers (
            digest TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        ''')

def touch(uid, name):
    with db() as con:
        con.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name))
        return con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def emo(eid, fallback):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

STATUS = {'pending': 'Не пройдена', 'passed': 'Проверка пройдена', 'failed': 'Проверка не пройдена'}
BLOCKED = emo('5447145698250237717', '⛔') + ' Вы забанены. Найден твинк'
WELCOME = '<b>' + emo('5447195541345706844', '👋') + ' Добро пожаловать!\n\n' + emo('5888584382553265934', '🛡') + ' Здесь вы пройдете проверку на мульти-акк (твинк)</b>'

def button(text, eid, **kwargs):
    # Новые Bot API поддерживают иконку кнопки. Если она недоступна,
    # отправка ниже повторяется без custom emoji в кнопках.
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=eid, **kwargs)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button('Профиль', '5771887475421090729', callback_data='profile'),
         button('Топ', '5913384919584741274', callback_data='top')],
        [button('Проверка', '5877260593903177342', web_app=WebAppInfo(url=BASE_URL))]
    ])

def back():
    return InlineKeyboardMarkup(inline_keyboard=[[
        button('Назад', '5875082500023258804', callback_data='home')
    ]])

async def display(message, text, markup=None, edit=False):
    from aiogram.exceptions import TelegramBadRequest
    method = message.edit_text if edit else message.answer
    try:
        await method(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower():
            return
        # Fallback для ботов без права использовать custom emoji.
        import re
        plain = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)
        if markup:
            data = markup.model_dump(exclude_none=True)
            for row in data['inline_keyboard']:
                for item in row:
                    item.pop('icon_custom_emoji_id', None)
            markup = InlineKeyboardMarkup.model_validate(data)
        await method(plain, reply_markup=markup)

@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type != 'private':
        await message.answer('Открой бота в личных сообщениях.')
        return
    user = touch(message.from_user.id, message.from_user.full_name)
    await display(message, BLOCKED if user['status'] == 'failed' else WELCOME,
                  None if user['status'] == 'failed' else menu())

@dp.callback_query(F.data.in_({'home', 'profile', 'top'}))
async def navigation(call: CallbackQuery):
    await call.answer()
    user = touch(call.from_user.id, call.from_user.full_name)
    if not isinstance(call.message, Message):
        return
    if user['status'] == 'failed':
        await display(call.message, BLOCKED, edit=True)
    elif call.data == 'home':
        await display(call.message, WELCOME, menu(), edit=True)
    elif call.data == 'profile':
        text = ('• <b>Профиль</b>\n\n<blockquote>'
                + emo('5771887475421090729', '👤') + ' Ник: ' + html.escape(user['name']) + '\n'
                + emo('5936017305585586269', '🆔') + f" ID: {user['id']}\n"
                + emo('5884366771913233289', '📋') + ' Статус: ' + STATUS[user['status']] + '</blockquote>')
        await display(call.message, text, back(), edit=True)
    else:
        with db() as con:
            rows = con.execute("SELECT * FROM users WHERE status='passed' ORDER BY checked_at DESC LIMIT 10").fetchall()
        lines = [emo('5879770735999717115', '🏆') + f' <a href="tg://user?id={r["id"]}">{html.escape(r["name"])}</a> — проверка пройдена' for r in rows]
        await display(call.message, '• <b>Топ</b>\n\n<blockquote>' + ('\n'.join(lines) or 'Пока никого нет.') + '</blockquote>', back(), edit=True)

@dp.message()
async def other(message: Message):
    if message.chat.type != 'private' or not message.from_user:
        return
    user = touch(message.from_user.id, message.from_user.full_name)
    await display(message, BLOCKED if user['status'] == 'failed' else 'Открой меню: /start')

PAGE = '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка аккаунта</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font:16px system-ui;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#eef2ff);margin:0;padding:24px}main{max-width:480px;margin:30px auto}h1{font-size:26px}p{line-height:1.6;opacity:.85}button{width:100%;padding:16px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:var(--tg-theme-button-text-color,white);font-size:17px;margin-top:20px}button:disabled{opacity:.5}label{display:block;line-height:1.5}#result{white-space:pre-wrap;font-weight:700;margin-top:24px}.note{font-size:13px}</style></head>
<body><main><h1>🛡 Проверка аккаунта</h1>
<p>Проверяем, использовался ли этот браузер другим Telegram-аккаунтом. Доступа к серийному номеру устройства у Mini App нет.</p>
<p class="note">Сохраняются Telegram ID, имя, результат и хеш случайного идентификатора браузера. IP не используется для решения о блокировке и не записывается в нашу базу. Хостинг может вести технические журналы запросов. Данные хранятся до удаления владельцем бота.</p>
<p class="note">Общий браузер может дать ложное совпадение. Очистка хранилища или другой браузер обходят эту проверку. При ошибке свяжитесь с владельцем бота.</p>
<label><input type="checkbox" id="agree"> Согласен на обработку указанных данных и публикацию моего имени со ссылкой на Telegram в списке прошедших проверку.</label>
<button id="verify" disabled>Пройти проверку</button><div id="result" role="status"></div>
<button id="close" hidden>Вернуться в бот</button></main>
<script>
const tg=window.Telegram?.WebApp;
const btn=document.getElementById('verify'), result=document.getElementById('result');
tg?.ready();tg?.expand();
document.getElementById('agree').onchange=e=>btn.disabled=!e.target.checked;
document.getElementById('close').onclick=()=>tg?.close();
btn.onclick=async()=>{
 btn.disabled=true;
 try{
  if(!tg?.initData)throw new Error('Открой приложение кнопкой «Проверка» в боте.');
  let browser=localStorage.getItem('check_browser_v1');
  if(!browser){browser=crypto.randomUUID();localStorage.setItem('check_browser_v1',browser);}
  result.textContent='Проверяем…';
  const response=await fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData,browser,consent:true})});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Ошибка сервера');
  result.textContent=data.status==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':'‼ ПРОВЕРКА НЕ ПРОЙДЕНА!';
  document.getElementById('close').hidden=false;
 }catch(e){result.textContent=e.message;btn.disabled=false;}
};
</script></body></html>'''

def validate_init(raw):
    if not isinstance(raw, str) or len(raw) > 16384:
        raise HTTPException(401, 'Некорректные данные Telegram')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        received = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError()
        age = time.time() - int(data['auth_date'])
        if age < -30 or age > 600:
            raise ValueError()
        user = json.loads(data['user'])
        if type(user['id']) is not int or user['id'] <= 0:
            raise ValueError()
        return user
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, 'Сессия недействительна или истекла. Закрой и снова открой Mini App.')

def verify_in_db(uid, name, browser):
    digest = hmac.new(APP_SECRET.encode(), browser.encode(), hashlib.sha256).hexdigest()
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name))
        old = con.execute('SELECT status FROM users WHERE id=?', (uid,)).fetchone()['status']
        if old == 'failed':
            return old
        owner = con.execute('SELECT user_id FROM browsers WHERE digest=?', (digest,)).fetchone()
        status = 'failed' if owner and owner['user_id'] != uid else 'passed'
        now = int(time.time())
        if status == 'passed':
            con.execute('INSERT OR IGNORE INTO browsers VALUES(?,?,?)', (digest, uid, now))
        con.execute('UPDATE users SET status=?,checked_at=? WHERE id=?', (status, now, uid))
        return status

@asynccontextmanager
async def lifespan(app):
    init_db()
    await bot.set_webhook(BASE_URL + '/telegram/webhook', secret_token=HOOK_SECRET,
                          allowed_updates=dp.resolve_used_update_types())
    try:
        yield
    finally:
        await bot.session.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.get('/', response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

@app.get('/health')
async def health():
    return {'ok': True}

async def small_json(request, limit=32768):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, 'Слишком большой запрос')
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Некорректный JSON')

@app.post('/telegram/webhook')
async def telegram_webhook(request: Request):
    if not secrets.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), HOOK_SECRET):
        raise HTTPException(403, 'Forbidden')
    payload = await small_json(request, 1048576)
    update = Update.model_validate(payload, context={'bot': bot})
    await dp.feed_update(bot, update)
    return {'ok': True}

@app.post('/api/verify')
async def verify(request: Request):
    data = await small_json(request)
    user = validate_init(data.get('init_data'))
    browser = data.get('browser')
    if data.get('consent') is not True:
        raise HTTPException(400, 'Нужно согласие на обработку данных')
    if not isinstance(browser, str) or not 20 <= len(browser) <= 100:
        raise HTTPException(400, 'Недоступно хранилище браузера')
    name = ' '.join(str(user.get(k, '')) for k in ('first_name', 'last_name')).strip() or str(user['id'])
    status = await asyncio.to_thread(verify_in_db, user['id'], name[:256], browser)
    return {'status': status}
