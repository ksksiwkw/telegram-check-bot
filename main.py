"""Telegram bot + Mini App, SQLite, automatic matching, appeals.
requirements.txt:
aiogram>=3.20,<4
fastapi>=0.115
uvicorn>=0.30
geoip2>=4.8

Start: uvicorn main:app --host 0.0.0.0 --port $PORT
Environment: BOT_TOKEN, PUBLIC_URL (or RENDER_EXTERNAL_URL), APP_SECRET
(minimum 32 chars), ADMIN_IDS (comma separated Telegram IDs).
DB_PATH defaults to bot.sqlite3: use persistent storage in production.
Optional GEOIP_DB: path to local GeoLite2-City.mmdb.
IP_HEADER optionally names a header OVERWRITTEN by your trusted ingress.
Leave empty unless direct access is blocked and ingress strips spoofed headers.
No header is trusted by default. Configure uvicorn trusted proxies separately.
Device model and IP geolocation are approximate, not hardware identity.
"""
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
TOKEN = os.environ['BOT_TOKEN'].strip()
BASE_URL = (os.getenv('PUBLIC_URL') or os.environ['RENDER_EXTERNAL_URL']).rstrip('/')
SECRET = os.environ['APP_SECRET']
if len(SECRET) < 32:
    raise RuntimeError('APP_SECRET: минимум 32 символа')
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
DB_PATH = os.getenv('DB_PATH', 'bot.sqlite3')
GEOIP_DB = os.getenv('GEOIP_DB', '')
IP_HEADER = os.getenv('IP_HEADER', '').strip()
HOOK_SECRET = hmac.new(SECRET.encode(), b'telegram-webhook', hashlib.sha256).hexdigest()
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
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
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',checked_at INTEGER);
        CREATE TABLE IF NOT EXISTS browsers(digest TEXT PRIMARY KEY,user_id INTEGER NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS checks_v3(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,name TEXT,ip_hash TEXT,device TEXT,geo TEXT,browser TEXT,status TEXT,reason TEXT,created INTEGER);
        CREATE INDEX IF NOT EXISTS checks_ip_v3 ON checks_v3(ip_hash);
        CREATE TABLE IF NOT EXISTS appeals_v3(uid INTEGER PRIMARY KEY,state TEXT,body TEXT,created INTEGER);
        CREATE TABLE IF NOT EXISTS exemptions_v3(uid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit_v3(id INTEGER PRIMARY KEY,admin INTEGER,uid INTEGER,action TEXT,created INTEGER);
        ''')

def digest(value):
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest() if value else ''

def touch(uid, name):
    with db() as c:
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name[:256]))
        return c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()

def emoji(eid, fallback):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

def btn(text, eid=None, **kwargs):
    return InlineKeyboardButton(text=text, **({'icon_custom_emoji_id':eid} if eid else {}), **kwargs)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('Профиль','5771887475421090729',callback_data='profile'),btn('Топ','5913384919584741274',callback_data='top')],
        [btn('Проверка','5877260593903177342',web_app=WebAppInfo(url=BASE_URL))]])

def back():
    return InlineKeyboardMarkup(inline_keyboard=[[btn('Назад','5875082500023258804',callback_data='home')]])

WELCOME = '<b>'+emoji('5447195541345706844','👋')+' Добро пожаловать!\n\n'+emoji('5888584382553265934','🛡')+' Здесь вы пройдете проверку на мульти-акк (твинк)</b>'
BLOCKED = emoji('5447145698250237717','⛔')+' Вы забанены: обнаружено совпадение признаков другого аккаунта.\nОспорить: /appeal ваше объяснение'
STATUS = {'pending':'Не пройдена','passed':'Проверка пройдена','failed':'Проверка не пройдена'}

async def display(message,text,markup=None,edit=False):
    from aiogram.exceptions import TelegramBadRequest
    method = message.edit_text if edit else message.answer
    try:
        await method(text,reply_markup=markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower(): return
        plain = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>',r'\1',text)
        if markup:
            data=markup.model_dump(exclude_none=True)
            for row in data['inline_keyboard']:
                for item in row: item.pop('icon_custom_emoji_id',None)
            markup=InlineKeyboardMarkup.model_validate(data)
        await method(plain,reply_markup=markup)

async def admin_send(text,markup=None):
    for uid in ADMINS:
        try: await bot.send_message(uid,text,reply_markup=markup)
        except Exception: log.warning('Не удалось отправить сообщение администратору')

@dp.message(Command('panel','панель'))
async def panel(message: Message):
    if message.chat.type!='private' or message.from_user.id not in ADMINS: return
    with db() as c:
        passed=c.execute("SELECT count(*) FROM users WHERE status='passed'").fetchone()[0]
        failed=c.execute("SELECT count(*) FROM users WHERE status='failed'").fetchone()[0]
        pending=c.execute("SELECT count(*) FROM appeals_v3 WHERE state='pending'").fetchone()[0]
    keyboard=InlineKeyboardMarkup(inline_keyboard=[
        [btn('📋 Логи',callback_data='logs:0')],
        [btn('Апелляции',callback_data='appeals:0')]])
    await message.answer(f'<b>Панель</b>\nПрошли: {passed}\nЗаблокированы: {failed}\nАпелляции: {pending}',reply_markup=keyboard)

@dp.callback_query(F.data.startswith('logs:'))
async def logs(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        await call.answer('Нет доступа'); return
    await call.answer()
    try: offset=max(0,int(call.data.split(':')[1]))
    except ValueError: return
    with db() as c:
        rows=c.execute('SELECT * FROM checks_v3 ORDER BY id DESC LIMIT 5 OFFSET ?',(offset,)).fetchall()
    lines=['<b>Журнал проверок</b>']
    for r in rows:
        lines.append(f"\n<code>{r['uid']}</code> {html.escape(r['name'][:80])}\n{STATUS.get(r['status'],r['status'])}\nУстройство: {html.escape(r['device'])}\nРегион по IP: {html.escape(r['geo'] or 'не определён')}\nIP-метка: <code>{r['ip_hash'][:12] or 'нет'}</code>\n{html.escape(r['reason'])}\n{time.strftime('%d.%m %H:%M UTC',time.gmtime(r['created']))}")
    keys=[]
    if offset: keys.append(btn('←',callback_data=f'logs:{max(0,offset-5)}'))
    if len(rows)==5: keys.append(btn('→',callback_data=f'logs:{offset+5}'))
    await display(call.message,'\n'.join(lines) if rows else 'Журнал пуст.',InlineKeyboardMarkup(inline_keyboard=[keys]) if keys else None,True)

@dp.message(Command('appeal','оспорить'))
async def appeal(message: Message):
    if message.chat.type!='private': return
    uid=message.from_user.id
    if not ADMINS:
        await message.answer('Приём апелляций пока не настроен владельцем.'); return
    body=(message.text or '').partition(' ')[2].strip()[:1500]
    if not body:
        await message.answer('Отправь объяснение: /appeal Почему считаешь блокировку ошибочной'); return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        user=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        prev=c.execute('SELECT * FROM appeals_v3 WHERE uid=?',(uid,)).fetchone()
        if not user or user['status']!='failed': result='Активной блокировки нет.'
        elif prev and (prev['state']=='pending' or time.time()-prev['created']<86400): result='Апелляция уже подана. Повторная заявка — не чаще раза в сутки.'
        else:
            c.execute('INSERT OR REPLACE INTO appeals_v3 VALUES(?,?,?,?)',(uid,'pending',body,int(time.time())))
            result=None
    if result:
        await message.answer(result); return
    await admin_send(f'Апелляция <code>{uid}</code>\n{html.escape(body)}',appeal_keys(uid))
    await message.answer('Апелляция сохранена и отправлена администрации.')

def appeal_keys(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[btn('Снять бан',callback_data=f'decide:yes:{uid}'),btn('Отклонить',callback_data=f'decide:no:{uid}')]])

@dp.callback_query(F.data.startswith('appeals:'))
async def appeals(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        await call.answer('Нет доступа'); return
    await call.answer()
    try: after=int(call.data.split(':')[1])
    except ValueError: return
    with db() as c:
        rows=c.execute("SELECT * FROM appeals_v3 WHERE state='pending' AND uid>? ORDER BY uid LIMIT 5",(after,)).fetchall()
    for r in rows:
        await call.message.answer(f"Апелляция <code>{r['uid']}</code>\n{html.escape(r['body'])}",reply_markup=appeal_keys(r['uid']))
    if len(rows)==5:
        await call.message.answer('Следующая страница',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn('Далее',callback_data=f"appeals:{rows[-1]['uid']}")]]))
    elif not rows: await call.message.answer('Новых апелляций нет.')

@dp.callback_query(F.data.startswith('decide:'))
async def decide(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        await call.answer('Нет доступа'); return
    try:
        _,action,raw=call.data.split(':'); uid=int(raw)
        if action not in ('yes','no'): return
    except ValueError: return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT state FROM appeals_v3 WHERE uid=?',(uid,)).fetchone()
        changed=bool(row and row['state']=='pending')
        if changed:
            c.execute('UPDATE appeals_v3 SET state=? WHERE uid=?',(action,uid))
            if action=='yes':
                c.execute("UPDATE users SET status='passed',checked_at=? WHERE id=?",(int(time.time()),uid))
                c.execute('INSERT OR IGNORE INTO exemptions_v3 VALUES(?)',(uid,))
            c.execute('INSERT INTO audit_v3(admin,uid,action,created) VALUES(?,?,?,?)',(call.from_user.id,uid,action,int(time.time())))
    await call.answer('Решение сохранено' if changed else 'Уже рассмотрено')
    if changed:
        await call.message.edit_reply_markup(reply_markup=None)
        try: await bot.send_message(uid,'✅ Блокировка снята. /start' if action=='yes' else 'Апелляция отклонена.')
        except Exception: pass

@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type!='private':
        await message.answer('Открой бота в личных сообщениях.'); return
    user=touch(message.from_user.id,message.from_user.full_name)
    await display(message,BLOCKED if user['status']=='failed' else WELCOME,None if user['status']=='failed' else menu())

@dp.callback_query(F.data.in_({'home','profile','top'}))
async def navigation(call: CallbackQuery):
    await call.answer()
    if not isinstance(call.message,Message): return
    user=touch(call.from_user.id,call.from_user.full_name)
    if user['status']=='failed': await display(call.message,BLOCKED,edit=True)
    elif call.data=='home': await display(call.message,WELCOME,menu(),True)
    elif call.data=='profile':
        text=('• <b>Профиль</b>\n\n<blockquote>'+emoji('5771887475421090729','👤')+' Ник: '+html.escape(user['name'])+'\n'+emoji('5936017305585586269','🆔')+f" ID: {user['id']}\n"+emoji('5884366771913233289','📋')+' Статус: '+STATUS.get(user['status'],'Не пройдена')+'</blockquote>')
        await display(call.message,text,back(),True)
    else:
        with db() as c: rows=c.execute("SELECT * FROM users WHERE status='passed' ORDER BY checked_at DESC LIMIT 10").fetchall()
        lines=[emoji('5879770735999717115','🏆')+f' <a href="tg://user?id={r["id"]}">{html.escape(r["name"][:80])}</a> — проверка пройдена' for r in rows]
        await display(call.message,'• <b>Топ</b>\n\n<blockquote>'+('\n'.join(lines) or 'Пока никого нет.')+'</blockquote>',back(),True)

@dp.message()
async def other(message: Message):
    if message.chat.type!='private' or not message.from_user: return
    user=touch(message.from_user.id,message.from_user.full_name)
    await display(message,BLOCKED if user['status']=='failed' else 'Меню: /start')

def validate_init(raw):
    try:
        if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True)
        data=dict(pairs)
        if len(data)!=len(pairs): raise ValueError()
        received=data.pop('hash')
        check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        expected=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,received): raise ValueError()
        age=time.time()-int(data['auth_date'])
        if not -30<=age<=600: raise ValueError()
        user=json.loads(data['user'])
        if not isinstance(user,dict) or type(user['id']) is not int or user['id']<=0: raise ValueError()
        return user
    except (KeyError,ValueError,TypeError):
        raise HTTPException(401,'Сессия истекла или недействительна. Снова открой Mini App.')

def get_signals(address,ua):
    try:
        ip=ipaddress.ip_address(address)
        address=str(ip) if ip.is_global else ''
    except ValueError: address=''
    ua=ua[:1000]
    model=''
    if 'Android' in ua:
        match=re.search(r'Android[^;)]*;\s*(?:[a-z]{2}[-_][A-Z]{2};\s*)?([^;)]+)',ua)
        model=match.group(1).split(' Build/')[0].strip()[:100] if match else ''
        if model in ('K','wv','Mobile',''): model=''
        device='Android'+(': '+model if model else ' (модель скрыта)')
    elif 'iPhone' in ua: device='iPhone (модель скрыта)'
    elif 'iPad' in ua: device='iPad (модель скрыта)'
    elif 'Windows' in ua: device='Windows'
    elif 'Macintosh' in ua: device='Mac'
    elif 'Linux' in ua: device='Linux'
    else: device='Не определено'
    geo=''
    if GEOIP_DB and address:
        try:
            import geoip2.database
            with geoip2.database.Reader(GEOIP_DB) as reader:
                city=reader.city(address)
                if city.city.name:
                    geo=', '.join(x for x in [city.country.iso_code,city.subdivisions.most_specific.name,city.city.name] if x)
        except Exception: pass
    return digest(address),device,geo,bool(model)

def check_account(uid,name,browser,ip_hash,device,geo,has_model):
    now=int(time.time()); fingerprint=digest(browser)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        latest=c.execute('SELECT created FROM checks_v3 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if latest and now-latest['created']<10: raise HTTPException(429,'Повтори через 10 секунд.')
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
        old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()['status']
        exempt=c.execute('SELECT 1 FROM exemptions_v3 WHERE uid=?',(uid,)).fetchone()
        reason='Совпадений нет'; status='passed'
        if old=='failed': status='failed'; reason='Блокировка сохранена'
        elif exempt: reason='Апелляция одобрена: исключение из автобана'
        else:
            owner=c.execute('SELECT user_id FROM browsers WHERE digest=? AND user_id<>?',(fingerprint,uid)).fetchone()
            match=c.execute("""SELECT uid,ip_hash,device FROM checks_v3 WHERE uid<>? AND status='passed' AND created>? AND
                ((ip_hash<>'' AND ip_hash=? AND device=? AND device<>'Не определено') OR
                 (?=1 AND geo<>'' AND geo=? AND device=?)) LIMIT 1""",(uid,now-30*86400,ip_hash,device,int(has_model),geo,device)).fetchone()
            if owner:
                status='failed'; reason='ID браузера совпал с другим аккаунтом'
            elif match:
                status='failed'; reason='Совпали IP и устройство' if ip_hash and match['ip_hash']==ip_hash else 'Совпали модель устройства и город по IP'
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(status,now,uid))
        c.execute('INSERT INTO checks_v3(uid,name,ip_hash,device,geo,browser,status,reason,created) VALUES(?,?,?,?,?,?,?,?,?)',(uid,name,ip_hash,device,geo,fingerprint,status,reason,now))
        if status=='passed': c.execute('INSERT OR IGNORE INTO browsers VALUES(?,?,?)',(fingerprint,uid,now))
        c.execute('DELETE FROM checks_v3 WHERE created<?',(now-90*86400,))
    return status,reason

PAGE='''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверка</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>body{font:16px system-ui;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#eef2ff);padding:24px;margin:0}main{max-width:480px;margin:40px auto}h1{font-size:25px}button{width:100%;padding:16px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:var(--tg-theme-button-text-color,white);font-size:17px;margin-top:20px}button:disabled{opacity:.5}label,details{font-size:13px;line-height:1.5;margin-top:20px;display:block}#result{white-space:pre-wrap;margin-top:24px;font-weight:bold}</style></head><body><main><h1>🧹 Пройти проверку на твинк.</h1><details><summary>Обработка данных и правила проверки</summary><p>Сохраняем Telegram ID, имя, хеш IP и идентификатора браузера, доступный тип/модель устройства, приблизительный город по IP, результат проверки. Модель может быть скрыта, геолокация неточна. Это не аппаратный ID и не GPS.</p><p>Автобан: совпал браузер либо IP вместе с типом устройства, либо доступная модель Android вместе с городом. Общая сеть и одинаковые телефоны могут дать ошибочное совпадение. Обжалование в боте: /appeal объяснение.</p><p>Логи доступны администраторам и отправляются им в Telegram. Журнал хранится до 90 дней; аккаунт, бан, привязка браузера и апелляции — до удаления владельцем. Имя и ссылка прошедшего проверку попадают в топ.</p></details><label><input type="checkbox" id="agree"> Согласен на указанную обработку данных и публикацию в топе.</label><button id="verify" disabled>Пройти проверку</button><div id="result" role="status"></div><button id="close" hidden>Вернуться в бот</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const b=document.getElementById('verify'),r=document.getElementById('result'),a=document.getElementById('agree');
a.onchange=()=>b.disabled=!a.checked;
document.getElementById('close').onclick=()=>tg?.close();
b.onclick=async()=>{b.disabled=true;a.disabled=true;try{
if(!tg?.initData)throw Error('Открой приложение через кнопку в боте.');
let browser=localStorage.getItem('check_browser_v1');
if(!browser){browser=crypto.randomUUID();localStorage.setItem('check_browser_v1',browser);}
r.textContent='Проверяем…';
const response=await fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData,browser,consent:true})});
const data=await response.json();if(!response.ok)throw Error(data.detail||'Ошибка сервера');
r.textContent=data.status==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':'‼ ПРОВЕРКА НЕ ПРОЙДЕНА!\\nОспорить: отправь боту /appeal и объяснение.';
document.getElementById('close').hidden=false;
}catch(e){r.textContent=e.message;b.disabled=false;a.disabled=false;}};
</script></body></html>'''

@asynccontextmanager
async def lifespan(app):
    init_db()
    if not ADMINS: log.warning('ADMIN_IDS не настроен: панель и апелляции недоступны')
    if not GEOIP_DB: log.warning('GEOIP_DB не задан: геолокация отключена')
    await bot.set_webhook(BASE_URL+'/telegram/webhook',secret_token=HOOK_SECRET,allowed_updates=dp.resolve_used_update_types())
    try: yield
    finally: await bot.session.close()

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/',response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

@app.get('/health')
async def health(): return {'ok':True}

async def small_json(request,limit=32768):
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>limit: raise HTTPException(413,'Слишком большой запрос')
    try:
        value=json.loads(body)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (ValueError,UnicodeDecodeError): raise HTTPException(400,'Некорректный JSON')

@app.post('/telegram/webhook')
async def webhook(request: Request):
    if not secrets.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),HOOK_SECRET): raise HTTPException(403,'Forbidden')
    payload=await small_json(request,1048576)
    await dp.feed_update(bot,Update.model_validate(payload,context={'bot':bot}))
    return {'ok':True}

@app.post('/api/verify')
async def verify(request: Request):
    data=await small_json(request)
    user=validate_init(data.get('init_data'))
    if data.get('consent') is not True: raise HTTPException(400,'Необходимо согласие')
    browser=data.get('browser')
    if not isinstance(browser,str) or not 20<=len(browser)<=100: raise HTTPException(400,'Недоступно хранилище браузера')
    address=request.client.host if request.client else ''
    if IP_HEADER:
        # Configure ONLY for a header overwritten by a trusted ingress.
        address=request.headers.get(IP_HEADER,'').strip()
    ip_hash,device,geo,has_model=await asyncio.to_thread(get_signals,address,request.headers.get('user-agent',''))
    name=(' '.join(str(user.get(k,'')) for k in ('first_name','last_name')).strip() or str(user['id']))[:256]
    status,reason=await asyncio.to_thread(check_account,user['id'],name,browser,ip_hash,device,geo,has_model)
    text=(f'<b>{STATUS[status]}</b>\n{html.escape(name)} · <code>{user["id"]}</code>\nУстройство: {html.escape(device)}\nРегион по IP: {html.escape(geo or "не определён")}\nIP-метка: <code>{ip_hash[:12] or "нет"}</code>\n{html.escape(reason)}')
    await admin_send(text)
    return {'status':status}

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')))
