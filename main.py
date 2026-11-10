"""Telegram anti-abuse bot + Mini App + bagel balance.
requirements.txt:
aiogram>=3.20.0
fastapi>=0.115.0
uvicorn>=0.30.0

Render:
Build: pip install -r requirements.txt
Start: python main.py
Environment: BOT_TOKEN, APP_SECRET (32+ chars), ADMIN_IDS (comma-separated),
PUBLIC_URL or RENDER_EXTERNAL_URL. Use persistent storage for DB_PATH.
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
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, Update, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo)
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
TOKEN = os.environ['BOT_TOKEN'].strip()
SECRET = os.environ['APP_SECRET']
if len(SECRET) < 32:
    raise RuntimeError('APP_SECRET должен содержать не менее 32 символов')
URL = (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL', '')).rstrip('/')
if not URL:
    raise RuntimeError('Укажи PUBLIC_URL или RENDER_EXTERNAL_URL')
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
DB_PATH = os.getenv('DB_PATH', 'bot.sqlite3')
GEOIP_DB = os.getenv('GEOIP_DB', '')
GEO_ENABLED = os.getenv('GEO_ENABLED', '0') == '1'
HOOK_SECRET = hmac.new(SECRET.encode(), b'telegram-webhook', hashlib.sha256).hexdigest()
MAX_BALANCE = 9_000_000_000_000_000
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
        CREATE TABLE IF NOT EXISTS checks_v3(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,name TEXT,ip_hash TEXT,device TEXT,geo TEXT,browser TEXT,status TEXT,reason TEXT,created INTEGER,platform TEXT,webapp_version TEXT);
        CREATE INDEX IF NOT EXISTS checks_ip_v3 ON checks_v3(ip_hash);
        CREATE INDEX IF NOT EXISTS checks_uid_v3 ON checks_v3(uid);
        CREATE TABLE IF NOT EXISTS appeals_v3(uid INTEGER PRIMARY KEY,state TEXT,body TEXT,created INTEGER);
        CREATE TABLE IF NOT EXISTS exemptions_v3(uid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit_v3(id INTEGER PRIMARY KEY AUTOINCREMENT,admin INTEGER,uid INTEGER,action TEXT,created INTEGER);
        CREATE TABLE IF NOT EXISTS balances(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
        CREATE TABLE IF NOT EXISTS balance_operations(id INTEGER PRIMARY KEY AUTOINCREMENT,admin_id INTEGER NOT NULL,target_id INTEGER NOT NULL,delta INTEGER NOT NULL,balance_after INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,created INTEGER NOT NULL,UNIQUE(chat_id,message_id));
        ''')
        # Add new log columns if upgrading the earlier attached version.
        cols = {r['name'] for r in c.execute('PRAGMA table_info(checks_v3)')}
        for col in ('platform', 'webapp_version'):
            if col not in cols:
                c.execute(f'ALTER TABLE checks_v3 ADD COLUMN {col} TEXT')

def digest(value):
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest() if value else ''

def touch(uid, name):
    with db() as c:
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name[:256]))
        return c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def get_status(uid):
    with db() as c:
        row = c.execute('SELECT status FROM users WHERE id=?', (uid,)).fetchone()
        return row['status'] if row else 'pending'

def em(eid, text):
    return f'<tg-emoji emoji-id="{eid}">{text}</tg-emoji>'

def button(text, data=None, icon=None, url=None):
    kw = {}
    if data is not None: kw['callback_data'] = data
    if icon: kw['icon_custom_emoji_id'] = icon
    if url: kw['web_app'] = WebAppInfo(url=url)
    return InlineKeyboardButton(text=text, **kw)

def keyboard(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def menu():
    return keyboard([[button('Профиль','profile','5771887475421090729'),button('Топ','top','5913384919584741274')],
                     [button('Проверка',icon='5877260593903177342',url=URL)]])
def back(): return keyboard([[button('Назад','home','5875082500023258804')]])

WELCOME = '<b>'+em('5447195541345706844','👋')+' Добро пожаловать!\n\n'+em('5888584382553265934','🛡')+' Здесь вы пройдёте проверку на мульти-акк (твинк)</b>'
BLOCKED = em('5447145698250237717','⛔')+' Вы забанены: проверка выявила совпадение признаков.\nОспорить: /appeal ваше объяснение'
STATUS = {'pending':'Не пройдена','passed':'Проверка пройдена','failed':'Проверка не пройдена'}

async def display(message, text, markup=None, edit=False):
    method = message.edit_text if edit else message.answer
    try:
        await method(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower(): return
        plain = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)
        if plain == text: raise
        safe_markup = markup
        if markup:
            data = markup.model_dump(exclude_none=True)
            for row in data['inline_keyboard']:
                for item in row: item.pop('icon_custom_emoji_id', None)
            safe_markup = InlineKeyboardMarkup.model_validate(data)
        await method(plain, reply_markup=safe_markup)

async def notify_admins(text, markup=None):
    for uid in ADMINS:
        try: await bot.send_message(uid, text, reply_markup=markup)
        except Exception: log.exception('Не удалось уведомить администратора %s', uid)

async def admin_callback(call):
    if call.from_user.id not in ADMINS or not isinstance(call.message, Message) or call.message.chat.type != 'private':
        await call.answer('Нет доступа', show_alert=True)
        return False
    await call.answer()
    return True

@dp.message(Command('panel','панель'))
async def panel(message: Message):
    if message.chat.type != 'private' or message.from_user.id not in ADMINS: return
    with db() as c:
        passed = c.execute("SELECT count(*) FROM users WHERE status='passed'").fetchone()[0]
        failed = c.execute("SELECT count(*) FROM users WHERE status='failed'").fetchone()[0]
        appeals = c.execute("SELECT count(*) FROM appeals_v3 WHERE state='pending'").fetchone()[0]
    await message.answer(f'<b>Панель</b>\nПрошли: {passed}\nЗаблокированы: {failed}\nАпелляции: {appeals}', reply_markup=keyboard([
        [button('📋 Логи','logs:0')],[button('Апелляции','appeals:0')],
        [button('Разрешить повторную проверку','recheck_help')]]))

@dp.callback_query(F.data.startswith('logs:'))
async def logs(call: CallbackQuery):
    if not await admin_callback(call): return
    try: offset=max(0,int(call.data.split(':')[1]))
    except ValueError: return
    with db() as c: rows=c.execute('SELECT * FROM checks_v3 ORDER BY id DESC LIMIT 5 OFFSET ?',(offset,)).fetchall()
    lines=['<b>Журнал проверок</b>']; keys=[]
    for r in rows:
        lines.append(f'\n<code>{r["uid"]}</code> {html.escape((r["name"] or "")[:60])}\n{STATUS.get(r["status"],r["status"])}\nУстройство: {html.escape((r["device"] or "")[:100])}\nПлатформа Mini App: {html.escape((r["platform"] or "не указана")[:60])}; версия Web App: {html.escape((r["webapp_version"] or "не указана")[:40])}\nГород по IP: {html.escape((r["geo"] or "не определён")[:120])}\nIP-метка: <code>{(r["ip_hash"] or "")[:12] or "нет"}</code>\n{html.escape((r["reason"] or "")[:150])}\n{time.strftime("%d.%m %H:%M UTC",time.gmtime(r["created"] or 0))}')
        keys.append([button(f'Повторная проверка: {r["uid"]}',f'recheck_ask:{r["uid"]}')])
    nav=[]
    if offset: nav.append(button('←',f'logs:{max(0,offset-5)}'))
    if len(rows)==5: nav.append(button('→',f'logs:{offset+5}'))
    if nav: keys.append(nav)
    await display(call.message,'\n'.join(lines) if rows else 'Журнал пуст.',keyboard(keys) if keys else None,True)

@dp.callback_query(F.data=='recheck_help')
async def recheck_help(call: CallbackQuery):
    if not await admin_callback(call): return
    await call.message.answer('Выбери пользователя в журнале или отправь /recheck ID, например /recheck 123456789')

async def offer_reset(message, uid):
    with db() as c: row=c.execute('SELECT name,status FROM users WHERE id=?',(uid,)).fetchone()
    if not row: await message.answer('Пользователь не найден.'); return
    await message.answer(f'Разрешить повторную проверку <code>{uid}</code> ({html.escape(row["name"])}). История сохранится.',reply_markup=keyboard([[button('Подтвердить',f'recheck_yes:{uid}'),button('Отмена','recheck_cancel')]]))

@dp.message(Command('recheck'))
async def recheck(message: Message):
    if message.chat.type!='private' or message.from_user.id not in ADMINS: return
    p=(message.text or '').split()
    if len(p)!=2 or not p[1].isdigit(): await message.answer('Формат: /recheck 123456789'); return
    await offer_reset(message,int(p[1]))

@dp.callback_query(F.data.startswith('recheck_ask:'))
async def ask_reset(call: CallbackQuery):
    if not await admin_callback(call): return
    try: uid=int(call.data.split(':')[1])
    except ValueError: return
    await offer_reset(call.message,uid)

@dp.callback_query(F.data=='recheck_cancel')
async def cancel_reset(call: CallbackQuery):
    if await admin_callback(call): await call.message.edit_text('Отменено.')

@dp.callback_query(F.data.startswith('recheck_yes:'))
async def confirm_reset(call: CallbackQuery):
    if not await admin_callback(call): return
    try: uid=int(call.data.split(':')[1])
    except ValueError: return
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); found=c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone()
        if found:
            c.execute("UPDATE users SET status='pending',checked_at=NULL WHERE id=?",(uid,))
            c.execute('DELETE FROM exemptions_v3 WHERE uid=?',(uid,))
            c.execute("UPDATE appeals_v3 SET state='reset' WHERE uid=? AND state='pending'",(uid,))
            c.execute('INSERT INTO audit_v3(admin,uid,action,created) VALUES(?,?,?,?)',(call.from_user.id,uid,'allow_recheck',int(time.time())))
    await call.message.edit_text('Повторная проверка разрешена.' if found else 'Пользователь не найден.')
    if found:
        try: await bot.send_message(uid,'Администратор разрешил повторную проверку. Открой Mini App через /start')
        except Exception: pass

def appeal_keys(uid): return keyboard([[button('Снять бан',f'decide:yes:{uid}'),button('Отклонить',f'decide:no:{uid}')]])

@dp.message(Command('appeal','оспорить'))
async def appeal(message: Message):
    if message.chat.type!='private': return
    uid=message.from_user.id; text=(message.text or '').partition(' ')[2].strip()[:1500]
    if not text: await message.answer('Напиши: /appeal объяснение причины обжалования'); return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        user=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        old=c.execute('SELECT * FROM appeals_v3 WHERE uid=?',(uid,)).fetchone()
        if not user or user['status']!='failed': error='Активной блокировки нет.'
        elif old and (old['state']=='pending' or time.time()-old['created']<86400): error='Апелляция уже подана. Повторная — не чаще раза в сутки.'
        else: c.execute('INSERT OR REPLACE INTO appeals_v3 VALUES(?,?,?,?)',(uid,'pending',text,int(time.time()))); error=None
    if error: await message.answer(error); return
    await notify_admins(f'Апелляция <code>{uid}</code>\n{html.escape(text)}',appeal_keys(uid))
    await message.answer('Апелляция отправлена администрации.')

@dp.callback_query(F.data.startswith('appeals:'))
async def show_appeals(call: CallbackQuery):
    if not await admin_callback(call): return
    try: after=int(call.data.split(':')[1])
    except ValueError: return
    with db() as c: rows=c.execute("SELECT * FROM appeals_v3 WHERE state='pending' AND uid>? ORDER BY uid LIMIT 5",(after,)).fetchall()
    for r in rows: await call.message.answer(f'Апелляция <code>{r["uid"]}</code>\n{html.escape(r["body"])}',reply_markup=appeal_keys(r['uid']))
    if len(rows)==5: await call.message.answer('Другие заявки',reply_markup=keyboard([[button('Далее',f'appeals:{rows[-1]["uid"]}')]]))
    elif not rows: await call.message.answer('Новых апелляций нет.')

@dp.callback_query(F.data.startswith('decide:'))
async def decide(call: CallbackQuery):
    if not await admin_callback(call): return
    try:
        _,action,raw=call.data.split(':'); uid=int(raw)
        if action not in ('yes','no'): return
    except ValueError: return
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); row=c.execute('SELECT state FROM appeals_v3 WHERE uid=?',(uid,)).fetchone(); changed=bool(row and row['state']=='pending')
        if changed:
            c.execute('UPDATE appeals_v3 SET state=? WHERE uid=?',(action,uid))
            if action=='yes':
                c.execute("UPDATE users SET status='passed',checked_at=? WHERE id=?",(int(time.time()),uid))
                c.execute('INSERT OR IGNORE INTO exemptions_v3 VALUES(?)',(uid,))
            c.execute('INSERT INTO audit_v3(admin,uid,action,created) VALUES(?,?,?,?)',(call.from_user.id,uid,action,int(time.time())))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer('Решение сохранено.' if changed else 'Апелляция уже рассмотрена.')
    if changed:
        try: await bot.send_message(uid,'✅ Блокировка снята.' if action=='yes' else 'Апелляция отклонена.')
        except Exception: pass

# Balances: `б`, and admin-only /give and /take.
def fmt(n): return f'{n:,}'.replace(',',' ')
def user_link(user):
    # Telegram full_name (not username), clickable profile link.
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'

@dp.message(F.text.regexp(r'(?i)^\s*б\s*$'))
async def balance_command(message: Message):
    user=message.from_user
    if not user or message.sender_chat or user.is_bot: return
    touch(user.id,user.full_name)
    if get_status(user.id)=='failed':
        await message.reply(BLOCKED); return
    with db() as c:
        row=c.execute('SELECT amount FROM balances WHERE uid=?',(user.id,)).fetchone()
    amount=row['amount'] if row else 0
    text=f'<b>{user_link(user)}:\n{em("5388988070081082206","🥯")} Баланс: {fmt(amount)} бубликов</b>'
    try: await message.reply(text)
    except TelegramBadRequest:
        await message.reply(f'<b>{user_link(user)}:\n🥯 Баланс: {fmt(amount)} бубликов</b>')

@dp.message(Command('give','take'))
async def balance_admin_command(message: Message):
    admin=message.from_user
    if not admin or admin.id not in ADMINS or message.chat.type=='channel':
        await message.reply('Команда доступна только администраторам.'); return
    parts=(message.text or '').split()
    command=parts[0].split('@')[0].lower()
    if command not in ('/give','/take') or len(parts) not in (2,3):
        await message.reply('Использование: ответом на сообщение — /give 1000 или /take 500. По ID: /give 123456789 1000 или /take 123456789 500.'); return
    replied=message.reply_to_message
    if len(parts)==2:
        if not replied or not replied.from_user or replied.sender_chat or replied.from_user.is_bot:
            await message.reply('С двумя аргументами ответь командой на сообщение пользователя. Или укажи ID и сумму: /give ID 1000'); return
        target_id=replied.from_user.id; target_name=replied.from_user.full_name; amount_str=parts[1]
    else:
        target_id_str,amount_str=parts[1],parts[2]
        if not target_id_str.isdigit() or int(target_id_str)<=0:
            await message.reply('ID должен быть положительным числом.'); return
        target_id=int(target_id_str)
        if replied and replied.from_user and replied.from_user.id==target_id:
            target_name=replied.from_user.full_name
        else:
            with db() as c: found=c.execute('SELECT name FROM users WHERE id=?',(target_id,)).fetchone()
            target_name=found['name'] if found else f'Пользователь {target_id}'
    if not re.fullmatch(r'\d{1,16}',amount_str):
        await message.reply('Сумма должна быть положительным целым числом, без пробелов.'); return
    amount=int(amount_str)
    if not 1<=amount<=MAX_BALANCE:
        await message.reply(f'Сумма должна быть от 1 до {fmt(MAX_BALANCE)}.'); return
    touch(target_id,target_name)
    delta=amount if command=='/give' else -amount
    error=None; after=None
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        duplicate=c.execute('SELECT 1 FROM balance_operations WHERE chat_id=? AND message_id=?',(message.chat.id,message.message_id)).fetchone()
        if duplicate: error='Эта команда уже была обработана.'
        else:
            c.execute('INSERT OR IGNORE INTO balances(uid,amount) VALUES(?,0)',(target_id,))
            current=c.execute('SELECT amount FROM balances WHERE uid=?',(target_id,)).fetchone()['amount']
            after=current+delta
            if after<0: error=f'Недостаточно бубликов. Баланс: {fmt(current)}.'
            elif after>MAX_BALANCE: error='Превышен максимальный баланс.'
            else:
                c.execute('UPDATE balances SET amount=? WHERE uid=?',(after,target_id))
                c.execute('INSERT INTO balance_operations(admin_id,target_id,delta,balance_after,chat_id,message_id,created) VALUES(?,?,?,?,?,?,?)',(admin.id,target_id,delta,after,message.chat.id,message.message_id,int(time.time())))
    if error: await message.reply(error); return
    # If we know their username/full name from the replied message, use the real Telegram name.
    mention=f'<a href="tg://user?id={target_id}">{html.escape(target_name)}</a>'
    action='Выдано' if delta>0 else 'Забрано'
    await message.reply(f'<b>{action}: {fmt(amount)} бубликов\nПользователь: {mention}\nБаланс: {fmt(after)} бубликов</b>')
    await notify_admins(f'<b>Изменение баланса</b>\nАдминистратор: <code>{admin.id}</code>\nПолучатель: <code>{target_id}</code>\nИзменение: {delta:+,}\nБаланс: {fmt(after)}')

@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type!='private': await message.answer('Открой бота в личных сообщениях.'); return
    row=touch(message.from_user.id,message.from_user.full_name)
    await display(message,BLOCKED if row['status']=='failed' else WELCOME,None if row['status']=='failed' else menu())

@dp.callback_query(F.data.in_({'home','profile','top'}))
async def navigation(call: CallbackQuery):
    await call.answer()
    if not isinstance(call.message,Message): return
    row=touch(call.from_user.id,call.from_user.full_name)
    if row['status']=='failed': await display(call.message,BLOCKED,edit=True)
    elif call.data=='home': await display(call.message,WELCOME,menu(),True)
    elif call.data=='profile':
        text='• <b>Профиль</b>\n\n<blockquote>'+em('5771887475421090729','👤')+' Ник: '+html.escape(row['name'])+'\n'+em('5936017305585586269','🆔')+f' ID: {row["id"]}\n'+em('5884366771913233289','📋')+' Статус: '+STATUS.get(row['status'],'Не пройдена')+'</blockquote>'
        await display(call.message,text,back(),True)
    else:
        with db() as c: rows=c.execute("SELECT * FROM users WHERE status='passed' ORDER BY checked_at DESC LIMIT 10").fetchall()
        lines=[em('5879770735999717115','🏆')+f' <a href="tg://user?id={r["id"]}">{html.escape(r["name"][:80])}</a> — проверка пройдена' for r in rows]
        await display(call.message,'• <b>Топ</b>\n\n<blockquote>'+('\n'.join(lines) or 'Пока никого нет.')+'</blockquote>',back(),True)

@dp.message()
async def other(message: Message):
    if message.chat.type!='private' or not message.from_user: return
    row=touch(message.from_user.id,message.from_user.full_name)
    await display(message,BLOCKED if row['status']=='failed' else 'Меню: /start')

def validate_init(raw):
    try:
        if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True); data=dict(pairs)
        if len(data)!=len(pairs): raise ValueError()
        received=data.pop('hash'); check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        expected=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,received): raise ValueError()
        age=time.time()-int(data['auth_date'])
        if not -30<=age<=600: raise ValueError()
        user=json.loads(data['user'])
        if not isinstance(user,dict) or type(user['id']) is not int or user['id']<=0: raise ValueError()
        return user
    except (KeyError,ValueError,TypeError):
        raise HTTPException(401,'Сессия истекла или недействительна. Закрой и снова открой Mini App.')

def signals(address,ua):
    try:
        ip=ipaddress.ip_address(address); address=str(ip) if ip.is_global else ''
    except ValueError: address=''
    ua=ua[:1000]; model=''
    if 'Android' in ua:
        m=re.search(r'Android[^;)]*;\s*(?:[a-z]{2}[-_][A-Z]{2};\s*)?([^;)]+)',ua)
        model=m.group(1).split(' Build/')[0].strip()[:100] if m else ''
        if model in ('K','wv','Mobile',''): model=''
        device='Android'+(': '+model if model else ' (модель скрыта)')
    elif 'iPhone' in ua: device='iPhone (модель обычно не раскрывается)'
    elif 'iPad' in ua: device='iPad (модель обычно не раскрывается)'
    elif 'Windows' in ua: device='Windows'
    elif 'Macintosh' in ua: device='Mac'
    elif 'Linux' in ua: device='Linux'
    else: device='Не определено'
    geo=''
    if GEO_ENABLED and GEOIP_DB and address:
        try:
            import geoip2.database
            with geoip2.database.Reader(GEOIP_DB) as reader:
                city=reader.city(address)
                geo=', '.join(x for x in (city.country.iso_code,city.subdivisions.most_specific.name,city.city.name) if x)
        except Exception: log.warning('Не удалось определить гео по локальной GeoIP базе')
    return digest(address),device,geo,bool(model)

def check_account(uid,name,browser,ip_hash,device,geo,has_model,platform,webapp_version):
    now=int(time.time()); fingerprint=digest(browser)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        if old and old['status'] in ('passed','failed'): return old['status'],None
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
        exempt=c.execute('SELECT 1 FROM exemptions_v3 WHERE uid=?',(uid,)).fetchone()
        status='passed'; reason='Совпадений не найдено'
        if exempt: reason='Исключение после одобренной апелляции'
        else:
            owner=c.execute('SELECT user_id FROM browsers WHERE digest=? AND user_id<>?',(fingerprint,uid)).fetchone()
            match=c.execute('''SELECT uid,ip_hash FROM checks_v3 WHERE uid<>? AND status='passed' AND created>? AND
                ((ip_hash<>'' AND ip_hash=? AND device=? AND device<>'Не определено') OR
                 (?=1 AND geo<>'' AND geo=? AND device=?)) LIMIT 1''',(uid,now-30*86400,ip_hash,device,int(has_model),geo,device)).fetchone()
            if owner: status='failed'; reason='Совпал идентификатор браузера с другим аккаунтом'
            elif match: status='failed'; reason='Совпали IP и тип устройства' if ip_hash and match['ip_hash']==ip_hash else 'Совпали модель Android и город по IP'
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(status,now,uid))
        c.execute('INSERT INTO checks_v3(uid,name,ip_hash,device,geo,browser,status,reason,created,platform,webapp_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uid,name,ip_hash,device,geo,fingerprint,status,reason,now,platform,webapp_version))
        if status=='passed': c.execute('INSERT OR IGNORE INTO browsers VALUES(?,?,?)',(fingerprint,uid,now))
        c.execute('DELETE FROM checks_v3 WHERE created<?',(now-90*86400,))
    return status,reason

PAGE='''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверка</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>body{font:16px system-ui;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#fff);padding:24px;margin:0}main{max-width:480px;margin:40px auto}h1{font-size:25px}button{width:100%;padding:16px;margin-top:20px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:var(--tg-theme-button-text-color,white);font-size:16px}button:disabled{opacity:.5}label,details{display:block;font-size:13px;line-height:1.5;margin-top:20px}#result{white-space:pre-wrap;font-size:22px;font-weight:bold;margin-top:24px}[hidden]{display:none!important}</style></head><body><main><h1 id="title" hidden>🧹 Пройти проверку на твинк.</h1><section id="form" hidden><details><summary>Какие данные используются</summary><p>Сохраняются Telegram ID, имя, хеш IP, случайный идентификатор браузера, доступные сведения об ОС/устройстве, платформа Mini App и версия Telegram Web App. Это не уникальный аппаратный ID и не GPS. Точную модель iPhone, точную версию приложения Telegram и достоверный признак модифицированного клиента Mini App обычно не предоставляет.</p><p>IP может быть общим у разных людей; браузерные сведения могут быть скрыты или изменены. Совпадения могут ошибочно сработать. Заблокированный пользователь может подать /appeal с объяснением; администрация рассмотрит и одобрит или отклонит обращение.</p><p>Логи проверок доступны администрации. История проверок хранится 90 дней; профиль и статус хранятся до удаления владельцем бота. В топе публикуется имя Telegram со ссылкой на профиль.</p></details><label><input id="agree" type="checkbox"> Согласен на указанную обработку данных</label><button id="verify" disabled>Пройти проверку</button></section><div id="result" role="status">Загрузка…</div><p id="appeal" hidden>Оспорить бан: отправь боту /appeal и объяснение.</p><button id="close" hidden>Вернуться в бот</button><button id="retry" hidden>Обновить статус</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const form=document.getElementById('form'),title=document.getElementById('title'),agree=document.getElementById('agree'),btn=document.getElementById('verify'),result=document.getElementById('result'),close=document.getElementById('close'),retry=document.getElementById('retry'),appeal=document.getElementById('appeal');let busy=false;
close.onclick=()=>tg?.close();agree.onchange=()=>btn.disabled=busy||!agree.checked;
async function api(path,extra={}){if(!tg?.initData)throw Error('Открой приложение кнопкой «Проверка» в боте.');const r=await fetch(path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData,...extra})});const d=await r.json();if(!r.ok)throw Error(d.detail||'Ошибка сервера');return d;}
function render(status){const pending=status==='pending';form.hidden=!pending;title.hidden=!pending;close.hidden=pending;retry.hidden=true;appeal.hidden=status!=='failed';result.textContent=status==='failed'?'⛔️ ПРОВЕРКА НЕ ПРОЙДЕНА!':status==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':'';}
async function load(){form.hidden=true;title.hidden=true;result.textContent='Загрузка…';try{render((await api('/api/status')).status)}catch(e){result.textContent=e.message;retry.hidden=false}}
retry.onclick=load;btn.onclick=async()=>{if(busy||!agree.checked)return;busy=true;btn.disabled=true;agree.disabled=true;try{let browser=localStorage.getItem('check_browser_v1');if(!browser){browser=crypto.randomUUID();localStorage.setItem('check_browser_v1',browser)}const device={platform:String(navigator.platform||''),ua:String(navigator.userAgent||'').slice(0,500),language:String(navigator.language||''),screen:[screen.width,screen.height],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0};result.textContent='Проверяем…';render((await api('/api/verify',{browser,device,platform:String(tg?.platform||''),webapp_version:String(tg?.version||''),consent:true})).status)}catch(e){result.textContent=e.message}finally{busy=false;agree.disabled=false;btn.disabled=!agree.checked}};load();</script></main></body></html>'''

def validate_init(raw):
    try:
        if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True); data=dict(pairs)
        if len(data)!=len(pairs): raise ValueError()
        received=data.pop('hash'); check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key,check.encode(),hashlib.sha256).hexdigest(),received): raise ValueError()
        age=time.time()-int(data['auth_date'])
        if not -30<=age<=600: raise ValueError()
        user=json.loads(data['user'])
        if not isinstance(user,dict) or type(user.get('id')) is not int or user['id']<=0: raise ValueError()
        return user
    except (KeyError,ValueError,TypeError): raise HTTPException(401,'Сессия истекла. Закрой и снова открой Mini App.')

def signals(address,ua):
    try:
        ip=ipaddress.ip_address(address); address=str(ip) if ip.is_global else ''
    except ValueError: address=''
    ua=ua[:1000]; model=''
    if 'Android' in ua:
        m=re.search(r'Android[^;)]*;\s*(?:[a-z]{2}[-_][A-Z]{2};\s*)?([^;)]+)',ua)
        model=m.group(1).split(' Build/')[0].strip()[:100] if m else ''
        if model in ('K','wv','Mobile',''): model=''
        device='Android'+(': '+model if model else ' (модель скрыта)')
    elif 'iPhone' in ua: device='iPhone (модель обычно скрыта)'
    elif 'iPad' in ua: device='iPad (модель обычно скрыта)'
    elif 'Windows' in ua: device='Windows'
    elif 'Macintosh' in ua: device='Mac'
    elif 'Linux' in ua: device='Linux'
    else: device='Не определено'
    geo=''
    if GEO_ENABLED and GEOIP_DB and address:
        try:
            import geoip2.database
            with geoip2.database.Reader(GEOIP_DB) as reader:
                city=reader.city(address); geo=', '.join(x for x in (city.country.iso_code,city.subdivisions.most_specific.name,city.city.name) if x)
        except Exception: log.warning('GeoIP база недоступна')
    return digest(address),device,geo,bool(model)

def check_account(uid,name,browser,ip_hash,device,geo,has_model,platform,webapp_version):
    now=int(time.time()); fingerprint=digest(browser)
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        if old and old['status'] in ('passed','failed'): return old['status'],None
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
        exempt=c.execute('SELECT 1 FROM exemptions_v3 WHERE uid=?',(uid,)).fetchone(); status='passed'; reason='Совпадений не найдено'
        if exempt: reason='Исключение по решению администрации'
        else:
            owner=c.execute('SELECT user_id FROM browsers WHERE digest=? AND user_id<>?',(fingerprint,uid)).fetchone()
            match=c.execute('''SELECT uid,ip_hash FROM checks_v3 WHERE uid<>? AND status='passed' AND created>? AND
            ((ip_hash<>'' AND ip_hash=? AND device=? AND device<>'Не определено') OR (?=1 AND geo<>'' AND geo=? AND device=?)) LIMIT 1''',(uid,now-30*86400,ip_hash,device,int(has_model),geo,device)).fetchone()
            if owner: status='failed'; reason='Совпадение идентификатора браузера'
            elif match: status='failed'; reason='Совпали IP и тип устройства' if ip_hash and match['ip_hash']==ip_hash else 'Совпали модель Android и город по IP'
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(status,now,uid))
        c.execute('INSERT INTO checks_v3(uid,name,ip_hash,device,geo,browser,status,reason,created,platform,webapp_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uid,name,ip_hash,device,geo,fingerprint,status,reason,now,platform,webapp_version))
        if status=='passed': c.execute('INSERT OR IGNORE INTO browsers VALUES(?,?,?)',(fingerprint,uid,now))
        c.execute('DELETE FROM checks_v3 WHERE created<?',(now-90*86400,))
    return status,reason

@asynccontextmanager
async def lifespan(app):
    init_db()
    if not ADMINS: log.warning('ADMIN_IDS не задан')
    await bot.set_webhook(URL+'/telegram/webhook',secret_token=HOOK_SECRET,allowed_updates=dp.resolve_used_update_types())
    try: yield
    finally: await bot.session.close()

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.get('/',response_class=HTMLResponse)
async def index(): return HTMLResponse(PAGE,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
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

@app.post('/api/status')
async def api_status(request: Request):
    data=await small_json(request); user=validate_init(data.get('init_data'))
    return JSONResponse({'status':await asyncio.to_thread(get_status,user['id'])},headers={'Cache-Control':'no-store'})

@app.post('/api/verify')
async def verify(request: Request):
    data=await small_json(request); user=validate_init(data.get('init_data'))
    existing=await asyncio.to_thread(get_status,user['id'])
    if existing in ('passed','failed'): return JSONResponse({'status':existing},headers={'Cache-Control':'no-store'})
    if data.get('consent') is not True: raise HTTPException(400,'Необходимо согласие')
    browser=data.get('browser')
    if not isinstance(browser,str) or not 20<=len(browser)<=100: raise HTTPException(400,'Недоступно хранилище браузера')
    device_data=data.get('device') if isinstance(data.get('device'),dict) else {}
    ua=request.headers.get('user-agent','')[:700]
    ip_hash,device,geo,has_model=await asyncio.to_thread(signals,request.client.host if request.client else '',ua)
    platform=str(data.get('platform',''))[:60]; webapp_version=str(data.get('webapp_version',''))[:40]
    # Device fields supplied by the Web App are descriptive hints only, not reliable IDs.
    extra=' '.join(str(device_data.get(k,''))[:80] for k in ('platform','ua') if device_data.get(k))
    if extra: device=(device+' | '+extra)[:250]
    name=' '.join(str(user.get(k,'')) for k in ('first_name','last_name')).strip()[:256] or str(user['id'])
    status,reason=await asyncio.to_thread(check_account,user['id'],name,browser,ip_hash,device,geo,has_model,platform,webapp_version)
    if reason is not None:
        await notify_admins(f'<b>{STATUS[status]}</b>\n{html.escape(name)} · <code>{user["id"]}</code>\nУстройство: {html.escape(device)}\nПлатформа Mini App: {html.escape(platform or "не указана")}\nВерсия Telegram Web App: {html.escape(webapp_version or "не указана")}\nГород по IP: {html.escape(geo or "не определён")}\nIP-метка: <code>{ip_hash[:12] or "нет"}</code>\n{html.escape(reason)}')
    return JSONResponse({'status':status},headers={'Cache-Control':'no-store'})

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')))
