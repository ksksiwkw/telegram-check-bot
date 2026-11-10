"""Complete bot + Mini App, balances, Hi-Lo, moderation.
Requirements: aiogram>=3.20,<4 fastapi uvicorn geoip2
Start: python main.py
Use one process and a persistent SQLite disk. Backup existing DB before upgrade.
Required env: BOT_TOKEN, APP_SECRET (32+ chars), ADMIN_IDS, PUBLIC_URL or RENDER_EXTERNAL_URL.
Optional: DB_PATH, TRUST_PROXY=0/1, GEOIP_DB (local GeoLite2-City.mmdb).
TRUST_PROXY=1 assumes a single trusted edge APPENDS the client IP to X-Forwarded-For.
Confirm that assumption for your hosting configuration. Never blindly trust the first address.
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
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
TOKEN = os.environ['BOT_TOKEN'].strip()
SECRET = os.environ['APP_SECRET']
if len(SECRET) < 32:
    raise RuntimeError('APP_SECRET: минимум 32 случайных символа')
URL = (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL', '')).rstrip('/')
if not URL.startswith('https://'):
    raise RuntimeError('PUBLIC_URL / RENDER_EXTERNAL_URL должен начинаться с https://')
ADMINS = {int(x.strip()) for x in os.environ['ADMIN_IDS'].split(',') if x.strip()}
if not ADMINS:
    raise RuntimeError('Укажи числовые Telegram ID администраторов в ADMIN_IDS')
DB_PATH = os.getenv('DB_PATH', 'bot.sqlite3')
TRUST_PROXY = os.getenv('TRUST_PROXY', '0') == '1'
GEOIP_DB = os.getenv('GEOIP_DB', '')
MAX_MONEY = 9_000_000_000_000_000
MAX_BET = 1_000_000_000
MAX_PAYOUT = 1_000_000_000_000
HOOK = hmac.new(SECRET.encode(), b'telegram-webhook', hashlib.sha256).hexdigest()
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()
admin_waiting = {}

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',checked_at INTEGER);
        CREATE TABLE IF NOT EXISTS balances(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
        CREATE TABLE IF NOT EXISTS app_checks_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,ip TEXT NOT NULL,fp TEXT NOT NULL,device TEXT NOT NULL,geo TEXT NOT NULL,platform TEXT NOT NULL,api_version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS app_checks_v6_ip ON app_checks_v6(ip);
        CREATE INDEX IF NOT EXISTS app_checks_v6_fp ON app_checks_v6(fp);
        CREATE INDEX IF NOT EXISTS app_checks_v6_user ON app_checks_v6(uid,ts);
        CREATE TABLE IF NOT EXISTS app_exempt_v6(uid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS app_appeals_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS app_audit_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,actor INTEGER,uid INTEGER,ts INTEGER,action TEXT);
        CREATE TABLE IF NOT EXISTS app_ops_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,chat INTEGER,msg INTEGER,actor INTEGER,uid INTEGER,delta INTEGER,amount INTEGER,ts INTEGER,UNIQUE(chat,msg));
        CREATE TABLE IF NOT EXISTS app_hilo_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,card INTEGER NOT NULL,mult TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,wins INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,UNIQUE(chat,msg));
        CREATE UNIQUE INDEX IF NOT EXISTS app_hilo_one_active_v6 ON app_hilo_v6(uid) WHERE state='active';
        ''')
        c.execute('DELETE FROM app_checks_v6 WHERE ts<?', (int(time.time()) - 90*86400,))

def esc(x): return html.escape(str(x))
def fmt(n): return f'{n:,}'.replace(',', ' ')
def emoji(eid, fallback): return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
def mention(uid, name): return f'<a href="tg://user?id={uid}">{esc(name)}</a>'
def hashed(value): return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def button(text, data=None, icon=None, style=None, url=None):
    args = {'text': text}
    if data is not None: args['callback_data'] = data
    if icon: args['icon_custom_emoji_id'] = icon
    if style: args['style'] = style
    if url: args['web_app'] = WebAppInfo(url=url)
    return InlineKeyboardButton(**args)

def touch(uid, name):
    with db() as c:
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid,name[:256]))
        return dict(c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())

def user_status(uid):
    with db() as c:
        r=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
    return r['status'] if r else 'pending'

async def output(m, text, markup=None, edit=False, reply=False):
    method = m.edit_text if edit else (m.reply if reply else m.answer)
    try:
        await method(text,reply_markup=markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower(): return
        plain = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>',r'\1',text)
        if markup:
            data=markup.model_dump(exclude_none=True)
            for row in data['inline_keyboard']:
                for b in row:
                    b.pop('icon_custom_emoji_id',None)
                    b.pop('style',None)
            markup=InlineKeyboardMarkup.model_validate(data)
        await method(plain,reply_markup=markup)

async def notify(text, markup=None):
    for uid in ADMINS:
        try: await bot.send_message(uid,text,reply_markup=markup)
        except Exception: log.warning('Не удалось уведомить администратора %s',uid)

WELCOME='<b>'+emoji('5447195541345706844','👋')+' Добро пожаловать!\n\n'+emoji('5888584382553265934','🛡')+' Здесь вы пройдёте проверку на мульти-акк (твинк)</b>'
BLOCKED=emoji('5447145698250237717','⛔')+' <b>Вы забанены.</b> Совпали признаки аккаунта либо принято решение администратора.\nБан бессрочный. Оспорить: /appeal объяснение'
STATUS={'pending':'Не проверен','passed':'Проверка пройдена','failed':'Заблокирован'}
def menu(): return kb([[button('Профиль','profile','5771887475421090729'),button('Топ','top','5913384919584741274')],[button('Проверка',icon='5877260593903177342',url=URL)]])
def back(): return kb([[button('Назад','home','5875082500023258804')]])

@dp.message(CommandStart())
async def start(m: Message):
    if m.chat.type!='private':
        await m.answer('Открой бота в личных сообщениях.'); return
    u=touch(m.from_user.id,m.from_user.full_name)
    await output(m,BLOCKED if u['status']=='failed' else WELCOME,None if u['status']=='failed' else menu())

@dp.callback_query(F.data.in_({'home','profile','top'}))
async def navigation(q: CallbackQuery):
    await q.answer()
    if not isinstance(q.message,Message): return
    u=touch(q.from_user.id,q.from_user.full_name)
    if u['status']=='failed': await output(q.message,BLOCKED,edit=True)
    elif q.data=='home': await output(q.message,WELCOME,menu(),edit=True)
    elif q.data=='profile':
        text='• <b>Профиль</b>\n\n<blockquote>'+emoji('5771887475421090729','👤')+' Ник: '+esc(u['name'])+'\n'+emoji('5936017305585586269','🆔')+f' ID: <code>{u["id"]}</code>\n'+emoji('5884366771913233289','📋')+' Статус: '+STATUS.get(u['status'],'Не проверен')+'</blockquote>'
        await output(q.message,text,back(),edit=True)
    else:
        with db() as c: rows=c.execute("SELECT * FROM users WHERE status='passed' ORDER BY checked_at DESC LIMIT 10").fetchall()
        lines=[emoji('5879770735999717115','🏆')+' '+mention(r['id'],r['name'][:80])+' — проверка пройдена' for r in rows]
        await output(q.message,'• <b>Топ</b>\n\n<blockquote>'+('\n'.join(lines) or 'Пока никого нет.')+'</blockquote>',back(),edit=True)

# Administrator moderation. Private chats only. All state changes are audited.
def is_admin_message(m): return bool(m.from_user and not m.sender_chat and m.chat.type=='private' and m.from_user.id in ADMINS)
async def admin_callback(q):
    ok=q.from_user.id in ADMINS and isinstance(q.message,Message) and q.message.chat.type=='private'
    await q.answer(None if ok else 'Нет доступа',show_alert=not ok)
    return ok

def admin_menu():
    return kb([[button('📋 Логи проверок','admin:logs:0')],[button('✉️ Апелляции','admin:appeals:0')],[button('🚫 Забанить','admin:input:ban'),button('✅ Разбанить','admin:input:unban')],[button('🔄 Повторная проверка','admin:input:recheck')]])

@dp.message(Command('panel','панель'))
async def panel(m: Message):
    if not is_admin_message(m): return
    admin_waiting.pop(m.from_user.id,None)
    await m.answer('<b>Панель администратора</b>\n/ban ID причина\n/unban ID\n/recheck ID\nОтмена ввода: /cancel',reply_markup=admin_menu())

@dp.message(Command('cancel'))
async def cancel(m: Message):
    if is_admin_message(m):
        admin_waiting.pop(m.from_user.id,None)
        await m.answer('Ввод отменён.')

def moderate(actor, uid, action, reason=''):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT OR IGNORE INTO users(id,name) VALUES(?,?)',(uid,f'ID {uid}'))
        new={'ban':'failed','unban':'passed','recheck':'pending'}[action]
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(new,int(time.time()),uid))
        if action=='unban': c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(uid,))
        else: c.execute('DELETE FROM app_exempt_v6 WHERE uid=?',(uid,))
        c.execute("UPDATE app_appeals_v6 SET state='closed' WHERE uid=? AND state='open'",(uid,))
        c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(actor,uid,int(time.time()),action+': '+reason[:1000]))
    return {'ban':'Пользователь заблокирован.','unban':'Бан снят; добавлено исключение из автопроверки.','recheck':'Статус сброшен. Открой Mini App заново. История сохранена.'}[action]

async def process_moderation(m, action, text):
    p=text.split(maxsplit=1)
    if not p or not re.fullmatch(r'[0-9]{1,15}',p[0]) or int(p[0])<=0:
        await m.answer('Нужен числовой ID, например: 123456789 причина'); return False
    uid=int(p[0]); reason=p[1] if len(p)>1 else ''
    answer=moderate(m.from_user.id,uid,action,reason)
    await m.answer(answer+f' ID: <code>{uid}</code>')
    await notify(f'Админ {m.from_user.id}: {esc(action)}, ID {uid}. {esc(reason[:600])}')
    try: await bot.send_message(uid,BLOCKED if action=='ban' else answer+' /start')
    except Exception: pass
    return True

@dp.message(Command('ban','unban','recheck'))
async def moderation_command(m: Message):
    if not is_admin_message(m): return
    p=(m.text or '').split(maxsplit=1)
    action=p[0].split('@')[0][1:]
    await process_moderation(m,action,p[1] if len(p)>1 else '')

@dp.message(lambda m: bool(m.from_user and m.from_user.id in admin_waiting and is_admin_message(m)))
async def moderation_input(m: Message):
    action,deadline=admin_waiting[m.from_user.id]
    if time.time()>deadline:
        admin_waiting.pop(m.from_user.id,None)
        await m.answer('Время ввода истекло. /panel'); return
    if await process_moderation(m,action,m.text or ''): admin_waiting.pop(m.from_user.id,None)

def log_text(r):
    return (f'<b>Проверка #{r["id"]}</b> · {time.strftime("%d.%m %H:%M UTC",time.gmtime(r["ts"]))}\n'
            f'ID: <code>{r["uid"]}</code>\n{esc(STATUS.get(r["status"],r["status"]))}\n'
            f'IP: <code>{esc(r["ip"])}</code>\nУстройство/ОС (данные браузера): {esc(r["device"])}\n'
            f'Гео IP: {esc(r["geo"] or "не определено")}\nПлатформа Mini App: {esc(r["platform"])}\n'
            f'Версия Web App API, НЕ приложения: {esc(r["api_version"])}\n{esc(r["reason"])}')

def appeal_buttons(aid): return kb([[button('✅ Одобрить',f'admin:approve:{aid}'),button('❌ Отклонить',f'admin:reject:{aid}')]])

@dp.callback_query(F.data.startswith('admin:'))
async def admin_actions(q: CallbackQuery):
    if not await admin_callback(q): return
    p=q.data.split(':'); action=p[1]
    if action=='input':
        if p[2] not in ('ban','unban','recheck'): return
        admin_waiting[q.from_user.id]=(p[2],time.time()+300)
        await q.message.answer('Пришли ID пользователя (можно добавить причину). Отмена: /cancel'); return
    if action=='logs':
        try: offset=max(0,min(int(p[2]),1_000_000))
        except ValueError: return
        with db() as c: rows=c.execute('SELECT * FROM app_checks_v6 ORDER BY id DESC LIMIT 1 OFFSET ?',(offset,)).fetchall()
        if not rows: await q.message.answer('Записей больше нет.'); return
        r=rows[0]
        keys=kb([[button('←',f'admin:logs:{max(0,offset-1)}'),button('→',f'admin:logs:{offset+1}')],[button('Управление пользователем',f'admin:user:{r["uid"]}')]])
        await q.message.answer(log_text(r),reply_markup=keys); return
    if action=='user':
        uid=int(p[2])
        await q.message.answer(f'ID <code>{uid}</code>',reply_markup=kb([[button('🚫 Бан',f'admin:confirmban:{uid}'),button('✅ Разбан',f'admin:confirmunban:{uid}')]])); return
    if action in ('confirmban','confirmunban'):
        uid=int(p[2]); act='ban' if action=='confirmban' else 'unban'
        await q.message.answer(f'Подтвердить {act} для ID {uid}?',reply_markup=kb([[button('Подтвердить',f'admin:do{act}:{uid}')]])); return
    if action in ('doban','dounban'):
        uid=int(p[2]); act='ban' if action=='doban' else 'unban'
        answer=moderate(q.from_user.id,uid,act,'Панель администратора')
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.answer(answer)
        try: await bot.send_message(uid,BLOCKED if act=='ban' else answer)
        except Exception: pass
        return
    if action=='appeals':
        after=int(p[2])
        with db() as c: rows=c.execute("SELECT * FROM app_appeals_v6 WHERE state='open' AND id>? ORDER BY id LIMIT 5",(after,)).fetchall()
        for r in rows: await q.message.answer(f'Апелляция #{r["id"]}\nID: <code>{r["uid"]}</code>\n{esc(r["body"])}',reply_markup=appeal_buttons(r['id']))
        if len(rows)==5: await q.message.answer('Другие заявки',reply_markup=kb([[button('Далее',f'admin:appeals:{rows[-1]["id"]}')]]))
        elif not rows: await q.message.answer('Нет открытых апелляций.')
        return
    if action in ('approve','reject'):
        aid=int(p[2])
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute("SELECT * FROM app_appeals_v6 WHERE id=? AND state='open'",(aid,)).fetchone()
            if r:
                c.execute('UPDATE app_appeals_v6 SET state=? WHERE id=?',(action,aid))
                if action=='approve':
                    c.execute("UPDATE users SET status='passed',checked_at=? WHERE id=?",(int(time.time()),r['uid']))
                    c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(r['uid'],))
                c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(q.from_user.id,r['uid'],int(time.time()),action+f' appeal #{aid}'))
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.answer('Сохранено.' if r else 'Уже рассмотрено.')
        if r:
            try: await bot.send_message(r['uid'],'✅ Апелляция одобрена, бан снят. /start' if action=='approve' else 'Апелляция отклонена.')
            except Exception: pass

@dp.message(Command('appeal','оспорить'))
async def appeal(m: Message):
    if m.chat.type!='private': return
    text=(m.text or '').partition(' ')[2].strip()
    if not 5<=len(text)<=1500:
        await m.answer('Напиши /appeal объяснение длиной 5–1500 символов.'); return
    uid=m.from_user.id; now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        old=c.execute('SELECT * FROM app_appeals_v6 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if not u or u['status']!='failed': err='Активного бана нет.'
        elif old and (old['state']=='open' or now-old['ts']<86400): err='Апелляция уже открыта либо с предыдущей не прошли сутки.'
        else:
            err=None
            aid=c.execute('INSERT INTO app_appeals_v6(uid,ts,body) VALUES(?,?,?)',(uid,now,text)).lastrowid
    if err: await m.answer(err); return
    await m.answer('Апелляция сохранена.')
    await notify(f'Апелляция #{aid} · ID <code>{uid}</code>\n{esc(text)}',appeal_buttons(aid))

# Balance operations: transactional, nonnegative, idempotent on webhook retries.
@dp.message(F.text.regexp(r'(?i)^\s*б\s*$'))
async def balance(m: Message):
    u=m.from_user
    if not u or u.is_bot or m.sender_chat: return
    touch(u.id,u.full_name)
    if user_status(u.id)=='failed': await output(m,BLOCKED,reply=True); return
    with db() as c: r=c.execute('SELECT amount FROM balances WHERE uid=?',(u.id,)).fetchone()
    amount=r['amount'] if r else 0
    await output(m,f'<b>{mention(u.id,u.full_name)}:\n{emoji("5388988070081082206","🥯")} Баланс: {fmt(amount)} бубликов</b>',reply=True)

@dp.message(Command('give','take'))
async def change_balance(m: Message):
    if not m.from_user or m.sender_chat or m.from_user.id not in ADMINS:
        await m.reply('Только для администратора.'); return
    p=(m.text or '').split(); target=None
    if len(p)==2 and m.reply_to_message:
        r=m.reply_to_message
        if r.sender_chat or not r.from_user or r.from_user.is_bot:
            await m.reply('Нужен ответ на сообщение обычного пользователя.'); return
        target=r.from_user; uid=target.id; amount_raw=p[1]; name=target.full_name
    elif len(p)==3 and re.fullmatch(r'[0-9]{1,15}',p[1]) and int(p[1])>0:
        uid=int(p[1]); amount_raw=p[2]
        r=m.reply_to_message
        if r and not r.sender_chat and r.from_user and r.from_user.id==uid: target=r.from_user
        with db() as c: known=c.execute('SELECT name FROM users WHERE id=?',(uid,)).fetchone()
        name=target.full_name if target else (known['name'] if known else f'ID {uid}')
    else:
        await m.reply('Ответом: /give 1000 или /take 500\nПо ID: /give 123456789 1000 или /take 123456789 500'); return
    if not re.fullmatch(r'[0-9]{1,16}',amount_raw) or not 1<=int(amount_raw)<=MAX_MONEY:
        await m.reply('Нужна положительная целая сумма без пробелов.'); return
    if target: touch(uid,target.full_name)
    amount=int(amount_raw); delta=amount if p[0].split('@')[0].lower()=='/give' else -amount
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM app_ops_v6 WHERE chat=? AND msg=?',(m.chat.id,m.message_id)).fetchone()
        error=None
        if old: error='Эта команда уже обработана.'
        else:
            c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)',(uid,))
            current=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()['amount']
            after=current+delta
            if after<0: error=f'Недостаточно бубликов. Баланс: {fmt(current)}.'
            elif after>MAX_MONEY: error='Превышен лимит баланса.'
            else:
                c.execute('UPDATE balances SET amount=? WHERE uid=?',(after,uid))
                c.execute('INSERT INTO app_ops_v6(chat,msg,actor,uid,delta,amount,ts) VALUES(?,?,?,?,?,?,?)',(m.chat.id,m.message_id,m.from_user.id,uid,delta,after,int(time.time())))
    if error: await m.reply(error); return
    await m.reply(f'<b>{mention(uid,name)}\n{"Выдано" if delta>0 else "Списано"}: {fmt(amount)} бубликов\nБаланс: {fmt(after)} бубликов</b>')
    await notify(f'Баланс: админ {m.from_user.id}, ID {uid}, изменение {delta:+}, баланс {fmt(after)}.')

# Hi-Lo: replacement draw from 1..13; equality loses. Odds depend on current card.
# 96.75% gross expected return per guess before downward rounding.
# Initial stake x current accumulated multiplier = total cashout (not net profit).
def odds(card, direction):
    count=card-1 if direction=='low' else 13-card
    if count<=0: return None
    return (Decimal('0.9675')*13/Decimal(count)).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
def money_out(g): return min(MAX_PAYOUT,int(Decimal(g['bet'])*Decimal(g['mult'])))
def card_name(n): return {1:'Туз (1)',11:'Валет (11)',12:'Дама (12)',13:'Король (13)'}.get(n,str(n))
def game_text(g):
    return ('<b>'+emoji('5447327564345416222','🎴')+' Хило (карточная игра)</b>\n\n<blockquote>'
            '<b>'+emoji('5389099885259666198','🃏')+' Карта:</b> <code>'+card_name(g['card'])+'</code>\n'
            '<b>'+emoji('5447160283959173987','✖️')+' Множитель:</b> <code>x'+g['mult']+'</code></blockquote>')
def game_keys(g):
    row=[]
    for direction,label,eid,color in [('low','Ниже','5447490850412074781','danger'),('high','Выше','5449683594425410231','success')]:
        k=odds(g['card'],direction)
        if k:
            row.append(button(f'{label} x{k}',f'hilo:{g["id"]}:{g["rev"]}:{direction}',eid,color))
    rows=[row]
    if g['wins']:
        rows.append([button(f'Забрать выигрыш x{g["mult"]}',f'hilo:{g["id"]}:{g["rev"]}:cash','5447184078077992135','success')])
    return kb(rows)

def create_game(uid,bet,chat,msg):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        same=c.execute('SELECT * FROM app_hilo_v6 WHERE chat=? AND msg=?',(chat,msg)).fetchone()
        if same:
            if same['state']=='active': return dict(same),None
            return None,'Эта команда уже была выполнена.'
        active=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
        if active: return dict(active),None
        r=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()
        if not r or r['amount']<bet: return None,'Недостаточно бубликов.'
        c.execute('UPDATE balances SET amount=amount-? WHERE uid=?',(bet,uid))
        gid=c.execute("INSERT INTO app_hilo_v6(uid,bet,card,mult,chat,msg) VALUES(?,?,?,'1.0',?,?)",(uid,bet,secrets.randbelow(13)+1,chat,msg)).lastrowid
        return dict(c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()),None

@dp.message(F.text.regexp(r'(?i)^\s*(хл|хило)(?:\s+.*)?$'))
async def hilo(m: Message):
    if not m.from_user or m.from_user.is_bot or m.sender_chat: return
    touch(m.from_user.id,m.from_user.full_name)
    if user_status(m.from_user.id)=='failed': await output(m,BLOCKED,reply=True); return
    p=(m.text or '').split()
    if len(p)==1:
        with db() as c: active=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(m.from_user.id,)).fetchone()
        if active:
            g=dict(active); await output(m,game_text(g),game_keys(g),reply=True); return
    if len(p)!=2 or not re.fullmatch(r'[0-9]{1,10}',p[1]):
        await output(m,'<b>'+emoji('5886436057091673541','🎴')+' Хило [N]\n'+emoji('5897846616966041652','🔮')+' Угадать последовательность: Выше | Ниже от Туза (1) до Короля (13)</b>\n\nРавная карта — проигрыш. Коэффициенты зависят от карты и накапливаются. Ставка списывается при старте.\nМинимум 10; максимум '+fmt(MAX_BET)+'. Лимит выплаты '+fmt(MAX_PAYOUT)+'.\n«хило» без суммы восстанавливает активную игру.',reply=True); return
    bet=int(p[1])
    if not 10<=bet<=MAX_BET:
        await output(m,'<b>'+emoji('5363938656874673963','💰')+' Ставки от 10 бубликов</b>\nМаксимум: '+fmt(MAX_BET),reply=True); return
    g,error=await asyncio.to_thread(create_game,m.from_user.id,bet,m.chat.id,m.message_id)
    if error: await m.reply(error); return
    await output(m,game_text(g),game_keys(g),reply=True)

def play_game(uid,gid,rev,action):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
        if not r or r['uid']!=uid: return None,'Это не твоя игра.'
        if r['state']!='active': return dict(r),'Игра уже завершена.'
        u=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        if u and u['status']=='failed': return dict(r),'Аккаунт заблокирован; обратись к администратору.'
        g=dict(r)
        if rev!=g['rev']: return g,'Это устаревшая кнопка. Показан текущий ход.'
        if action=='cash':
            if not g['wins']: return g,'Сначала угадай хотя бы одну карту.'
            value=money_out(g)
            bal=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()['amount']
            if bal+value>MAX_MONEY: return g,'Лимит баланса. Обратись к администратору.'
            c.execute('UPDATE balances SET amount=amount+? WHERE uid=?',(value,uid))
            c.execute("UPDATE app_hilo_v6 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(value,gid))
        elif action in ('low','high'):
            rate=odds(g['card'],action)
            if rate is None: return g,'Этот ход невозможен.'
            new=secrets.randbelow(13)+1
            win=new<g['card'] if action=='low' else new>g['card']
            if win:
                mult=(Decimal(g['mult'])*rate).quantize(Decimal('0.000001'),rounding=ROUND_DOWN)
                # Bound the multiplier; payoff is separately capped at MAX_PAYOUT.
                mult=min(mult,Decimal(MAX_PAYOUT)/Decimal(g['bet']))
                c.execute('UPDATE app_hilo_v6 SET card=?,mult=?,wins=wins+1,rev=rev+1 WHERE id=?',(new,str(mult),gid))
            else:
                c.execute("UPDATE app_hilo_v6 SET state='lost',card=?,rev=rev+1 WHERE id=?",(new,gid))
        else: return g,'Неизвестное действие.'
        return dict(c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()),None

def game_result(g):
    if g['state']=='active': return game_text(g),game_keys(g)
    if g['state']=='paid': return '<b>'+emoji('5206607081334906820','✅')+' Успешно</b>\n\n<blockquote><b>'+emoji('5447281848713518836','🥯')+' '+fmt(g['payout'])+' бубликов</b></blockquote>',None
    return '<b>'+emoji('5283283384418707920','❌')+' Неправильно! Выпало: '+card_name(g['card'])+'</b>',None

@dp.callback_query(F.data.startswith('hilo:'))
async def hilo_action(q: CallbackQuery):
    if not isinstance(q.message,Message): await q.answer(); return
    try:
        _,raw,revision,action=q.data.split(':'); gid=int(raw); rev=int(revision)
    except ValueError: await q.answer('Ошибка кнопки.'); return
    g,error=await asyncio.to_thread(play_game,q.from_user.id,gid,rev,action)
    await q.answer(error,show_alert=bool(error))
    if g:
        text,markup=game_result(g)
        await output(q.message,text,markup,edit=True)

@dp.message()
async def other(m: Message):
    if m.chat.type!='private' or not m.from_user: return
    u=touch(m.from_user.id,m.from_user.full_name)
    await output(m,BLOCKED if u['status']=='failed' else 'Меню: /start\nБаланс: б\nИгра: хило 10')

# Web verification. No random browser ID is used for the decision.
def validate_init(raw):
    try:
        if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True); d=dict(pairs)
        if len(d)!=len(pairs): raise ValueError()
        sig=d.pop('hash')
        check='\n'.join(f'{k}={v}' for k,v in sorted(d.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        expected=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sig): raise ValueError()
        if not -30<=time.time()-int(d['auth_date'])<=600: raise ValueError()
        u=json.loads(d['user'])
        if not isinstance(u,dict) or type(u.get('id')) is not int or u['id']<=0: raise ValueError()
        return u
    except (ValueError,TypeError,KeyError): raise HTTPException(401,'Сессия истекла. Закрой и снова открой Mini App.')

def get_ip(req):
    raw=req.client.host if req.client else ''
    if TRUST_PROXY:
        # Exactly one trusted proxy must append the real client IP.
        # Left-most entries can be supplied by the user; never trust them here.
        forwarded=req.headers.get('x-forwarded-for','')
        if forwarded: raw=forwarded.split(',')[-1].strip()
    try:
        addr=ipaddress.ip_address(raw)
        if getattr(addr,'ipv4_mapped',None): addr=addr.ipv4_mapped
        return str(addr) if addr.is_global else ''
    except ValueError: return ''

def geographic(ip):
    if not GEOIP_DB: return ''
    try:
        import geoip2.database
        with geoip2.database.Reader(GEOIP_DB) as r:
            x=r.city(ip)
            return ', '.join(v for v in (x.country.iso_code,x.subdivisions.most_specific.name,x.city.name) if v)[:180]
    except Exception: return ''

def clean_text(value,length=100):
    if not isinstance(value,str): return ''
    return ''.join(x for x in value if x.isprintable())[:length]

def device_signals(ua,data):
    ua=clean_text(ua,700)
    if not isinstance(data,dict): raise HTTPException(400,'Некорректные данные устройства')
    os_label='Не определена'; model='Не раскрыта'; version=''
    if 'Android' in ua:
        os_label='Android'
        ver=re.search(r'Android\s+([\d.]+)',ua)
        version=ver.group(1) if ver else ''
        found=re.search(r'Android[^;)]*;\s*([^;)]+)',ua)
        if found:
            candidate=found.group(1).split(' Build/')[0].strip()
            if candidate not in ('K','wv','Mobile',''): model=candidate[:80]
    elif 'iPhone' in ua or 'iPad' in ua or 'iPod' in ua:
        os_label='iOS/iPadOS'
        ver=re.search(r'OS\s+([0-9_]+)',ua)
        version=ver.group(1).replace('_','.') if ver else ''
        model='iPhone (точная модель скрыта)' if 'iPhone' in ua else 'iPad/iPod (точная модель скрыта)'
    elif 'Windows' in ua:
        os_label='Windows (версия ОС по UA ненадёжна)'
    elif 'Macintosh' in ua: os_label='macOS / возможно iPadOS'
    elif 'Linux' in ua: os_label='Linux'
    hint_model=clean_text(data.get('model'),80)
    if hint_model: model=hint_model+' (заявлено браузером)'
    screen=data.get('screen')
    if not isinstance(screen,list) or len(screen)!=2 or any(type(v) is not int or not 1<=v<=20000 for v in screen):
        raise HTTPException(400,'Недоступны параметры экрана')
    cores=data.get('cores',0); touchpoints=data.get('touch',0)
    if type(cores) is not int or not 0<=cores<=1024 or type(touchpoints) is not int or not 0<=touchpoints<=100:
        raise HTTPException(400,'Некорректные параметры устройства')
    ratio=data.get('ratio',1)
    if type(ratio) not in (int,float) or not 0<ratio<=20: raise HTTPException(400,'Некорректный масштаб экрана')
    # Deliberately excludes username, browser storage ID and Telegram account.
    # This is an approximate fingerprint and may collide or be spoofed.
    signals={'os':os_label,'version':version,'model':model,'screen':sorted(screen),'cores':cores,'touch':touchpoints,'ratio':round(ratio,2)}
    fingerprint=hashed(json.dumps(signals,sort_keys=True,ensure_ascii=True))
    label=f'{os_label} {version}; {model}; экран {sorted(screen)}; ядра {cores}; touch {touchpoints}; DPR {ratio}'
    return fingerprint,label[:400]

def record_check(uid,name,ip,fp,device,geo,platform,api_version):
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
        old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()['status']
        if old=='failed': return {'status':'failed'},None
        latest=c.execute('SELECT ts FROM app_checks_v6 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if latest and now-latest['ts']<15: raise HTTPException(429,'Подожди 15 секунд.')
        exempt=c.execute('SELECT 1 FROM app_exempt_v6 WHERE uid=?',(uid,)).fetchone()
        # Requested strict policy: same public IP OR same approximate device fingerprint.
        # This can ban different people on the same Wi-Fi/VPN or identical phones.
        match=c.execute("SELECT uid,ip,fp FROM app_checks_v6 WHERE uid<>? AND status='passed' AND ts>? AND (ip=? OR fp=?) ORDER BY id DESC LIMIT 1",(uid,now-90*86400,ip,fp)).fetchone()
        if exempt:
            result='passed'; reason='Исключение администратора'
        elif match:
            result='failed'
            reason=('Совпал IP' if match['ip']==ip else 'Совпали приблизительные характеристики устройства')+f'; другой ID {match["uid"]}'
        else:
            result='passed'; reason='Совпадений не найдено'
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(result,now,uid))
        rid=c.execute('INSERT INTO app_checks_v6(uid,ts,ip,fp,device,geo,platform,api_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',(uid,now,ip,fp,device,geo,platform,api_version,result,reason)).lastrowid
        c.execute('DELETE FROM app_checks_v6 WHERE ts<?',(now-90*86400,))
        row=dict(c.execute('SELECT * FROM app_checks_v6 WHERE id=?',(rid,)).fetchone())
    return {'status':result},row

PAGE=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверка</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>body{font:16px system-ui;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#fff);padding:24px;margin:0}main{max-width:480px;margin:40px auto}button{width:100%;padding:16px;margin-top:20px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:var(--tg-theme-button-text-color,white);font-size:16px}button:disabled{opacity:.5}details,label{display:block;font-size:13px;line-height:1.5;margin-top:20px}#result{white-space:pre-wrap;font-size:20px;font-weight:bold;margin-top:24px}[hidden]{display:none!important}</style></head><body><main><h2>🧹 Пройти проверку на твинк.</h2><section id="form" hidden><details><summary>Правила и обработка данных</summary><p>Сохраняются Telegram ID, имя, IP-адрес, характеристики ОС/устройства и экрана, приблизительный отпечаток устройства, платформа и версия Web App API. Если настроена локальная GeoIP-база, определяется приблизительный город по IP — не GPS. Данные проверки хранятся до 90 дней и отправляются администраторам в Telegram. Копии сообщений у администраторов автоматически не удаляются. Профиль, баланс, статус и финансовая история остаются до удаления владельцем. Имя прошедшего проверку со ссылкой публикуется в топе.</p><p>Автобан при совпадении IP ИЛИ набора характеристик устройства с другим прошедшим аккаунтом. Общий Wi-Fi/VPN и одинаковые телефоны могут дать ложный бан. Сведения браузера можно подделать. Это не доказательство твинка и не уникальный ID телефона. Точный мод/официальный клиент и версия приложения Telegram не определяются. Оспаривание: /appeal причина. Запрос удаления данных можно передать владельцу бота; заблокированные пользователи могут включить его в апелляцию.</p></details><label><input id="agree" type="checkbox"> Согласен на указанную обработку данных и публикацию в топе</label><button id="verify" disabled>Пройти проверку</button></section><div id="result">Загрузка…</div><button id="retry" hidden>Повторить загрузку</button><button id="close" hidden>Вернуться в бот</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const form=document.getElementById('form'),agree=document.getElementById('agree'),btn=document.getElementById('verify'),result=document.getElementById('result'),retry=document.getElementById('retry'),closeBtn=document.getElementById('close');let busy=false;
closeBtn.onclick=()=>tg?.close();agree.onchange=()=>btn.disabled=busy||!agree.checked;
async function api(path,extra={}){if(!tg?.initData)throw Error('Открой Mini App через кнопку в боте.');const r=await fetch(path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData,...extra})});const d=await r.json();if(!r.ok)throw Error(d.detail||'Ошибка сервера');return d;}
function render(s){form.hidden=s!=='pending';closeBtn.hidden=s==='pending';retry.hidden=true;result.textContent=s==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':s==='failed'?'‼ ПРОВЕРКА НЕ ПРОЙДЕНА!\nОспорить: /appeal причина':'';}
async function load(){retry.hidden=true;try{render((await api('/api/status')).status)}catch(e){result.textContent=e.message;retry.hidden=false;}}
retry.onclick=load;
btn.onclick=async()=>{if(busy||!agree.checked)return;busy=true;btn.disabled=true;agree.disabled=true;try{let hints={};try{hints=await navigator.userAgentData?.getHighEntropyValues(['model'])||{}}catch(e){}const device={model:String(hints.model||''),screen:[Math.min(screen.width,screen.height),Math.max(screen.width,screen.height)],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0,ratio:devicePixelRatio};result.textContent='Проверяем…';render((await api('/api/verify',{consent:true,device,platform:String(tg.platform||''),api_version:String(tg.version||'')})).status)}catch(e){result.textContent=e.message}finally{busy=false;agree.disabled=false;btn.disabled=!agree.checked;}};load();
</script></body></html>'''

@asynccontextmanager
async def lifespan(app):
    init_db()
    await bot.set_webhook(URL+'/telegram/webhook',secret_token=HOOK,allowed_updates=dp.resolve_used_update_types())
    try: yield
    finally: await bot.session.close()

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/',response_class=HTMLResponse)
async def index(): return HTMLResponse(PAGE,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'})
@app.get('/health')
async def health(): return {'ok':True}

async def read_json(req,limit=32768):
    body=bytearray()
    async for chunk in req.stream():
        body.extend(chunk)
        if len(body)>limit: raise HTTPException(413,'Слишком большой запрос')
    try:
        d=json.loads(body)
        if not isinstance(d,dict): raise ValueError()
        return d
    except (ValueError,UnicodeDecodeError): raise HTTPException(400,'Некорректный JSON')

@app.post('/telegram/webhook')
async def webhook(req: Request):
    if not secrets.compare_digest(req.headers.get('X-Telegram-Bot-Api-Secret-Token',''),HOOK): raise HTTPException(403,'Forbidden')
    d=await read_json(req,1048576)
    await dp.feed_update(bot,Update.model_validate(d,context={'bot':bot}))
    return {'ok':True}

@app.post('/api/status')
async def api_status(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data'))
    s=await asyncio.to_thread(user_status,u['id'])
    # Old passed profiles without a NEW-format check must be checked once more.
    if s=='passed':
        with db() as c:
            evidence=c.execute('SELECT 1 FROM app_checks_v6 WHERE uid=? LIMIT 1',(u['id'],)).fetchone()
            exempt=c.execute('SELECT 1 FROM app_exempt_v6 WHERE uid=?',(u['id'],)).fetchone()
        if not evidence and not exempt: s='pending'
    return JSONResponse({'status':s},headers={'Cache-Control':'no-store'})

@app.post('/api/verify')
async def verify(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data'))
    if await asyncio.to_thread(user_status,u['id'])=='failed': return {'status':'failed'}
    if d.get('consent') is not True: raise HTTPException(400,'Нужно согласие')
    ip=get_ip(req)
    if not ip:
        raise HTTPException(503,'Сервер не определил публичный IP. Администратору нужно проверить TRUST_PROXY и настройки прокси.')
    fp,device=device_signals(req.headers.get('user-agent',''),d.get('device'))
    platform=clean_text(d.get('platform'),50); api_version=clean_text(d.get('api_version'),30)
    geo=await asyncio.to_thread(geographic,ip)
    name=' '.join(str(u.get(k,'')) for k in ('first_name','last_name')).strip()[:256] or str(u['id'])
    result,row=await asyncio.to_thread(record_check,u['id'],name,ip,fp,device,geo,platform,api_version)
    if row: await notify(log_text(row))
    return JSONResponse(result,headers={'Cache-Control':'no-store'})

if __name__=='__main__':
    import uvicorn
    # Parse trusted forwarding explicitly in get_ip, not twice.
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),proxy_headers=False)
