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
        CREATE TABLE IF NOT EXISTS roulette_rounds_v1(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            last_bet_at INTEGER NOT NULL,
            result_number INTEGER,
            result_color TEXT
        );
        CREATE INDEX IF NOT EXISTS roulette_rounds_chat_state_v1 ON roulette_rounds_v1(chat,state,id);
        CREATE TABLE IF NOT EXISTS roulette_bets_v1(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            name TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            selection TEXT NOT NULL,
            label TEXT NOT NULL,
            amount INTEGER NOT NULL,
            multiplier TEXT NOT NULL,
            payout INTEGER NOT NULL DEFAULT 0,
            won INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL,
            source_chat INTEGER NOT NULL,
            source_msg INTEGER NOT NULL,
            source_index INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_chat,source_msg,source_index,uid)
        );
        CREATE INDEX IF NOT EXISTS roulette_bets_round_v1 ON roulette_bets_v1(round_id,state,id);
        CREATE INDEX IF NOT EXISTS roulette_bets_user_v1 ON roulette_bets_v1(round_id,uid,state);
        CREATE TABLE IF NOT EXISTS roulette_replays_v1(
            round_id INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            mode TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(round_id,uid)
        );
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
def card_name(n): return {1:'A',11:'J',12:'Q',13:'K'}.get(n,str(n))
def fmt_mult(value):
    return format(Decimal(str(value)).normalize(), 'f')
def game_text(g):
    return ('<b>'+emoji('5447327564345416222','🎴')+' Хило (карточная игра)</b>\n\n<blockquote>'
            '<b>'+emoji('5389099885259666198','🃏')+' Карта:</b> <code>'+card_name(g['card'])+'</code>\n'
            '<b>'+emoji('5447160283959173987','✖️')+' Множитель:</b> <code>x'+fmt_mult(g['mult'])+'</code></blockquote>')
def game_keys(g):
    row=[]
    for direction,label,eid,color in [('low','Ниже','5447490850412074781','danger'),('high','Выше','5449683594425410231','success')]:
        k=odds(g['card'],direction)
        if k:
            row.append(button(f'{label} x{k}',f'hilo:{g["id"]}:{g["rev"]}:{direction}',eid,color))
    rows=[row]
    if g['wins']:
        rows.append([button(f'Забрать выигрыш x{fmt_mult(g["mult"])}',f'hilo:{g["id"]}:{g["rev"]}:cash','5447184078077992135','primary')])
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
        await output(m,'<b>'+emoji('5886436057091673541','🎴')+' Хило [ N ]\n'+emoji('5897846616966041652','🔮')+' Угадать последовательность: Выше | Ниже от Туза (1) До короля (13)</b>',reply=True); return
    bet=int(p[1])
    if not 10<=bet<=MAX_BET:
        await output(m,'<b>'+emoji('5886436057091673541','🎴')+' Хило [ N ]\n'+emoji('5897846616966041652','🔮')+' Угадать последовательность: Выше | Ниже от Туза (1) До короля (13)</b>',reply=True); return
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

# Roulette uses the same virtual bублики balance as Hi-Lo. A round is per chat.
ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_COOLDOWN = 13
ROULETTE_EMOJI = {
    'red': ('5388647401865097828', '🔴'),
    'green': ('5388907487904674520', '🟢'),
    'fire': ('6008104758236156926', '🔥'),
    'smile': ('5994473545650934240', '😀'),
    'crown': ('5807868868886009920', '👑'),
    'clock': ('5258258882022612173', '⏲'),
    'repeat': ('5877410604225924969', '🔄'),
    'double': ('6026261955818294779', '⏩'),
}

def roulette_color(number):
    if number == 0: return 'green'
    return 'red' if number in ROULETTE_RED else 'black'

def roulette_color_emoji(color):
    if color == 'red': return emoji(*ROULETTE_EMOJI['red'])
    if color == 'green': return emoji(*ROULETTE_EMOJI['green'])
    return '⚫️'

def roulette_parse_type(raw):
    token = raw.strip().lower().replace('ё','е')
    if token in ('к','красный','красная','красное','red'):
        return ('color','red','RED',Decimal('1.98'))
    if token in ('ч','черный','черная','черное','black'):
        return ('color','black','BLACK',Decimal('1.98'))
    if token in ('з','зеленый','зеленая','зеленое','green'):
        return ('color','green','GREEN',Decimal('35.98'))
    if token in ('odd','нечет','нечетный'):
        return ('parity','odd','ODD',Decimal('1.98'))
    if token in ('even','чет','четный'):
        return ('parity','even','EVEN',Decimal('1.98'))
    if token.isdigit():
        n=int(token)
        if not 0 <= n <= 36: raise ValueError('Число должно быть от 0 до 36.')
        return ('number',str(n),str(n),Decimal('35.98'))
    m=re.fullmatch(r'(\d{1,2})-(\d{1,2})',token)
    if m:
        lo,hi=map(int,m.groups())
        if not (0 <= lo <= hi <= 36): raise ValueError('Диапазон должен быть внутри 0–36.')
        if lo==hi: return ('number',str(lo),str(lo),Decimal('35.98'))
        if (lo,hi) in ((1,18),(19,36)):
            label='1–18' if lo==1 else '19–36'
            return ('range',f'{lo}-{hi}',label,Decimal('1.98'))
        count=hi-lo+1
        rate=(Decimal('35.98')/Decimal(count)).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
        return ('range',f'{lo}-{hi}',f'{lo}–{hi}',rate)
    raise ValueError('Тип ставки: к, ч, з, odd, even, число 0–36 или диапазон, например 2-24.')

def roulette_bet_wins(bet, number, color):
    kind=bet['bet_type']; sel=bet['selection']
    if kind=='color': return color==sel
    if kind=='parity': return number!=0 and ('odd' if number%2 else 'even')==sel
    if kind=='number': return number==int(sel)
    if kind=='range':
        lo,hi=map(int,sel.split('-'))
        return lo<=number<=hi
    return False

def roulette_round_bets(round_id, uid=None):
    with db() as c:
        if uid is None:
            rows=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND state='active' ORDER BY id",(round_id,)).fetchall()
        else:
            rows=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND uid=? AND state='active' ORDER BY id",(round_id,uid)).fetchall()
    return [dict(r) for r in rows]

def roulette_add_bet(chat, uid, name, amount, raw_type, source_chat, source_msg):
    kind,selection,label,mult=roulette_parse_type(raw_type)
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        duplicate=c.execute('SELECT id FROM roulette_bets_v1 WHERE source_chat=? AND source_msg=? AND source_index=0 AND uid=?',(source_chat,source_msg,uid)).fetchone()
        if duplicate: return None,'Эта ставка уже принята.'
        bal=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()
        current=bal['amount'] if bal else 0
        if current<amount: return None,f'Недостаточно бубликов. Баланс: {fmt(current)}.'
        r=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='open' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if not r:
            rid=c.execute("INSERT INTO roulette_rounds_v1(chat,state,created_at,last_bet_at) VALUES(?,'open',?,?)",(chat,now,now)).lastrowid
        else: rid=r['id']
        c.execute('UPDATE balances SET amount=amount-? WHERE uid=?',(amount,uid))
        c.execute("""INSERT INTO roulette_bets_v1(round_id,uid,name,bet_type,selection,label,amount,multiplier,created_at,source_chat,source_msg)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(rid,uid,name[:256],kind,selection,label,amount,str(mult),now,source_chat,source_msg))
        c.execute('UPDATE roulette_rounds_v1 SET last_bet_at=? WHERE id=?',(now,rid))
        row=dict(c.execute('SELECT * FROM roulette_rounds_v1 WHERE id=?',(rid,)).fetchone())
    return row,None

def roulette_cancel_bets(chat,uid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='open' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if not r: return None,'Нет открытого раунда.'
        rows=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND uid=? AND state='active'",(r['id'],uid)).fetchall()
        if not rows: return None,'У тебя нет ставок в текущем раунде.'
        total=sum(x['amount'] for x in rows)
        c.execute('UPDATE balances SET amount=amount+? WHERE uid=?',(total,uid))
        c.execute("UPDATE roulette_bets_v1 SET state='cancelled' WHERE round_id=? AND uid=? AND state='active'",(r['id'],uid))
        left=c.execute("SELECT COUNT(*) n FROM roulette_bets_v1 WHERE round_id=? AND state='active'",(r['id'],)).fetchone()['n']
        if not left: c.execute("UPDATE roulette_rounds_v1 SET state='cancelled' WHERE id=?",(r['id'],))
        return {'round_id':r['id'],'amount':total},None

def roulette_start_round(chat):
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='open' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if not r: return None,'Нет открытого раунда.',0
        bets=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND state='active' ORDER BY id",(r['id'],)).fetchall()
        if not bets:
            c.execute("UPDATE roulette_rounds_v1 SET state='cancelled' WHERE id=?",(r['id'],))
            return None,'В раунде нет ставок.',0
        remaining=ROULETTE_COOLDOWN-(now-r['last_bet_at'])
        if remaining>0: return None,'wait',remaining
        number=secrets.randbelow(37); color=roulette_color(number)
        c.execute("UPDATE roulette_rounds_v1 SET state='finished',result_number=?,result_color=? WHERE id=?",(number,color,r['id']))
        results=[]
        for b in bets:
            won=roulette_bet_wins(b,number,color)
            payout=0
            if won:
                payout=int((Decimal(b['amount'])*Decimal(b['multiplier'])).to_integral_value(rounding=ROUND_DOWN))
                bal=c.execute('SELECT amount FROM balances WHERE uid=?',(b['uid'],)).fetchone()
                current=bal['amount'] if bal else 0
                payout=max(0,min(payout,MAX_PAYOUT,MAX_MONEY-current))
                if payout: c.execute('UPDATE balances SET amount=amount+? WHERE uid=?',(payout,b['uid']))
            c.execute("UPDATE roulette_bets_v1 SET state='settled',won=?,payout=? WHERE id=?",(1 if won else 0,payout,b['id']))
            results.append({**dict(b),'won':1 if won else 0,'payout':payout})
        return {'id':r['id'],'number':number,'color':color,'bets':results},None,0

def roulette_replay(old_round_id,uid,mode,chat,source_chat,source_msg):
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute("SELECT * FROM roulette_rounds_v1 WHERE id=? AND state='finished'",(old_round_id,)).fetchone()
        if not old or old['chat']!=chat: return None,'Раунд уже недоступен.'
        used=c.execute('SELECT 1 FROM roulette_replays_v1 WHERE round_id=? AND uid=?',(old_round_id,uid)).fetchone()
        if used: return None,'Ты уже повторил ставки этого раунда.'
        old_bets=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND uid=? AND state='settled' ORDER BY id",(old_round_id,uid)).fetchall()
        if not old_bets: return None,'В прошлом раунде у тебя не было ставок.'
        factor=2 if mode=='double' else 1
        total=sum(b['amount']*factor for b in old_bets)
        if any(b['amount']*factor>MAX_BET for b in old_bets): return None,'Удвоенная ставка превышает лимит.'
        bal=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()
        current=bal['amount'] if bal else 0
        if current<total: return None,f'Недостаточно бубликов. Нужно: {fmt(total)}, баланс: {fmt(current)}.'
        r=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='open' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if not r:
            rid=c.execute("INSERT INTO roulette_rounds_v1(chat,state,created_at,last_bet_at) VALUES(?,'open',?,?)",(chat,now,now)).lastrowid
        else: rid=r['id']
        c.execute('UPDATE balances SET amount=amount-? WHERE uid=?',(total,uid))
        c.execute('INSERT INTO roulette_replays_v1(round_id,uid,mode,created_at) VALUES(?,?,?,?)',(old_round_id,uid,mode,now))
        for i,b in enumerate(old_bets):
            amount=b['amount']*factor
            c.execute("""INSERT INTO roulette_bets_v1(round_id,uid,name,bet_type,selection,label,amount,multiplier,created_at,source_chat,source_msg,source_index)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(rid,uid,b['name'],b['bet_type'],b['selection'],b['label'],amount,b['multiplier'],now,source_chat,source_msg,i+1))
        c.execute('UPDATE roulette_rounds_v1 SET last_bet_at=? WHERE id=?',(now,rid))
    return {'round_id':rid,'amount':total,'count':len(old_bets)},None

def roulette_result_bets(round_id):
    with db() as c:
        rows=c.execute("SELECT * FROM roulette_bets_v1 WHERE round_id=? AND state='settled' ORDER BY id",(round_id,)).fetchall()
    return [dict(r) for r in rows]

def roulette_bets_message(round_id, uid=None):
    rows=roulette_round_bets(round_id,uid)
    if not rows: return None
    lines=[]
    for b in rows[:35]:
        lines.append('<b>'+emoji(*ROULETTE_EMOJI['smile'])+fmt(b['amount'])+' на '+roulette_label_markup(b['label'])+'</b>')
    if len(rows)>35: lines.append('<b>…и ещё '+str(len(rows)-35)+' ставок</b>')
    return '<b>'+emoji(*ROULETTE_EMOJI['fire'])+' Ставки приняты:</b>\n\n<blockquote>'+'\n'.join(lines)+'</blockquote>'

def roulette_label_markup(label):
    if label=='RED': return roulette_color_emoji('red')+' <b>RED</b>'
    if label=='BLACK': return '⚫️ <b>BLACK</b>'
    if label=='GREEN': return roulette_color_emoji('green')+' <b>GREEN</b>'
    return '<b>'+esc(label)+'</b>'

def roulette_result_message(result):
    color=result['color']; number=result['number']
    text='<b>'+emoji(*ROULETTE_EMOJI['crown'])+' Выпало: '+roulette_color_emoji(color)+' '+str(number)+'</b>'
    lines=[]
    for b in result['bets'][:35]:
        status=('выиграл '+fmt(b['payout'])+' бубликов' if b['won'] else 'проиграл '+fmt(b['amount'])+' бубликов')
        lines.append('<b>'+mention(b['uid'],b['name'])+' ставил '+fmt(b['amount'])+' на '+roulette_label_markup(b['label'])+' — '+status+'</b>')
    if len(result['bets'])>35: lines.append('<b>…и ещё '+str(len(result['bets'])-35)+' ставок</b>')
    return text+'\n\n<blockquote>'+'\n'.join(lines)+'</blockquote>',kb([[button('Повторить ставку',f'roulette:repeat:{result["id"]}',ROULETTE_EMOJI['repeat'][0]),button('Удвоить ставку',f'roulette:double:{result["id"]}',ROULETTE_EMOJI['double'][0])]])

@dp.message(F.text.regexp(r'(?i)^\s*(?:/(?:лог|log|го|go|отмена|ставки)(?:@\w+)?|(?:лог|log|го|go|отмена|ставки))\s*$'))
async def roulette_commands(m: Message):
    if not m.from_user or m.from_user.is_bot or m.sender_chat: return
    if m.chat.type not in ('group','supergroup'):
        await m.reply('Рулетка доступна в групповых чатах.'); return
    text=(m.text or '').strip().lower().split('@',1)[0]
    cmd=text[1:] if text.startswith('/') else text
    uid=m.from_user.id; chat=m.chat.id
    touch(uid,m.from_user.full_name)
    if user_status(uid)=='failed': await output(m,BLOCKED,reply=True); return
    if cmd in ('го','go'):
        result,error,remaining=await asyncio.to_thread(roulette_start_round,chat)
        if error=='wait':
            await output(m,'<b>'+emoji(*ROULETTE_EMOJI['clock'])+' Подождите '+str(remaining)+' секунд…</b>',reply=True); return
        if error: await m.reply(error); return
        msg,markup=roulette_result_message(result)
        await output(m,msg,markup); return
    with db() as c:
        current=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='open' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
    if cmd=='отмена':
        result,error=await asyncio.to_thread(roulette_cancel_bets,chat,uid)
        if error: await m.reply(error)
        else: await output(m,'<b>Ставки отменены. Возвращено: '+fmt(result['amount'])+' бубликов.</b>',reply=True)
        return
    if cmd=='ставки':
        if not current: await m.reply('В текущем раунде у тебя нет ставок.'); return
        rows=roulette_round_bets(current['id'],uid)
        if not rows: await m.reply('В текущем раунде у тебя нет ставок.'); return
        lines=['<b>'+fmt(b['amount'])+' на '+roulette_label_markup(b['label'])+'</b>' for b in rows]
        total=sum(b['amount'] for b in rows)
        await output(m,'<b>Твои ставки в раунде #'+str(current['id'])+':</b>\n\n<blockquote>'+'\n'.join(lines)+'\n\n<b>Всего: '+fmt(total)+' бубликов</b></blockquote>',reply=True); return
    if cmd in ('лог','log'):
        if current:
            body=roulette_bets_message(current['id'])
            await output(m,'<b>Лог рулетки · раунд #'+str(current['id'])+'</b>\n\n'+(body or 'Ставок пока нет.'),reply=True)
        else:
            with db() as c: last=c.execute("SELECT * FROM roulette_rounds_v1 WHERE chat=? AND state='finished' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
            if not last: await m.reply('История рулетки пока пуста.'); return
            lines,markup=roulette_result_message({'id':last['id'],'number':last['result_number'],'color':last['result_color'],'bets':roulette_result_bets(last['id'])})
            await output(m,lines,markup,reply=True)
        return

@dp.message(F.text.regexp(r'(?i)^\s*[0-9]{1,12}\s+(?:к|красн\w*|red|ч|ч[её]рн\w*|black|з|зелен\w*|green|odd|even|ч[её]т\w*|неч[её]т\w*|[0-9]{1,2}(?:-[0-9]{1,2})?)\s*$'))
async def roulette_bet(m: Message):
    if not m.from_user or m.from_user.is_bot or m.sender_chat: return
    if m.chat.type not in ('group','supergroup'): return
    uid=m.from_user.id; touch(uid,m.from_user.full_name)
    if user_status(uid)=='failed': await output(m,BLOCKED,reply=True); return
    p=(m.text or '').split()
    amount=int(p[0])
    if not 1<=amount<=MAX_BET:
        await m.reply('Ставка должна быть от 1 до '+fmt(MAX_BET)+' бубликов.'); return
    try:
        roulette_parse_type(p[1])
    except ValueError as exc:
        await m.reply(str(exc)); return
    try:
        r,error=await asyncio.to_thread(roulette_add_bet,m.chat.id,uid,m.from_user.full_name,amount,p[1],m.chat.id,m.message_id)
    except ValueError as exc:
        await m.reply(str(exc)); return
    if error: await m.reply(error); return
    body=roulette_bets_message(r['id'])
    await output(m,(body or '<b>Ставка принята.</b>')+'\n\n<b>'+emoji(*ROULETTE_EMOJI['clock'])+' Подождите 13 секунд…</b>',reply=True)

@dp.callback_query(F.data.startswith('roulette:'))
async def roulette_replay_action(q: CallbackQuery):
    if not isinstance(q.message,Message): await q.answer(); return
    try:
        _,mode,raw=q.data.split(':'); old_id=int(raw)
        if mode not in ('repeat','double'): raise ValueError()
    except ValueError:
        await q.answer('Некорректная кнопка.',show_alert=True); return
    if q.message.chat.type not in ('group','supergroup'):
        await q.answer('Рулетка доступна в группах.',show_alert=True); return
    touch(q.from_user.id,q.from_user.full_name)
    if user_status(q.from_user.id)=='failed':
        await q.answer('Аккаунт заблокирован.',show_alert=True); return
    result,error=await asyncio.to_thread(roulette_replay,old_id,q.from_user.id,mode,q.message.chat.id,q.message.chat.id,q.message.message_id)
    if error:
        await q.answer(error,show_alert=True); return
    await q.answer('Ставки повторены.' if mode=='repeat' else 'Ставки удвоены.')
    with db() as c: r=c.execute('SELECT id FROM roulette_rounds_v1 WHERE id=?',(result['round_id'],)).fetchone()
    if r:
        body=roulette_bets_message(result['round_id'])
        try: await q.message.reply((body or '<b>Ставки приняты.</b>')+'\n\n<b>'+emoji(*ROULETTE_EMOJI['clock'])+' Подождите 13 секунд…</b>')
        except Exception: pass

@dp.message()
async def other(m: Message):
    # Ignore unrelated messages; only registered commands and game actions get a reply.
    return

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

def start_web_game(uid, bet):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        active=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
        if active: return dict(active), None
        bal=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()
        if not bal or bal['amount'] < bet: return None, 'Недостаточно бубликов.'
        c.execute('UPDATE balances SET amount=amount-? WHERE uid=?',(bet,uid))
        # Negative message IDs keep Mini App games distinct from Telegram message games.
        msg=-secrets.randbelow(2_000_000_000)-1
        gid=c.execute("INSERT INTO app_hilo_v6(uid,bet,card,mult,chat,msg) VALUES(?,?,?,'1',?,?)",(uid,bet,secrets.randbelow(13)+1,uid,msg)).lastrowid
        return dict(c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()), None

def web_game_payload(uid, g=None):
    with db() as c:
        bal=c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()
    amount=bal['amount'] if bal else 0
    if not g:
        with db() as c: row=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
        g=dict(row) if row else None
    if not g: return {'game':None,'balance':amount}
    return {'game':{'id':g['id'],'rev':g['rev'],'state':g['state'],'bet':g['bet'],'card':g['card'],'card_label':card_name(g['card']),'mult':fmt_mult(g['mult']),'wins':g['wins'],'payout':g['payout'],'cashout':money_out(g) if g['state']=='active' else g['payout'],'can_cash':bool(g['wins'])},'balance':amount}

PAGE=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#101323"><title>Игры</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d32;--panel2:#1b2741;--muted:#9aa8c3;--text:#f4f7ff;--line:#293653;--accent:#8b7cff;--green:#28c58a;--red:#ff647c}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 20% -10%,#34306b 0,transparent 38%),radial-gradient(ellipse at 100% 30%,#153b54 0,transparent 32%),var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh;padding:18px 15px 32px}
main{max-width:520px;margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;margin:10px 2px 22px}.brand{font-size:21px;font-weight:900;letter-spacing:.2px}.brand span{color:#a99eff}.balance{background:#1c2943;border:1px solid #354466;padding:9px 13px;border-radius:14px;font-weight:800}.balance small{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px}.card{background:linear-gradient(145deg,rgba(28,39,65,.97),rgba(17,26,46,.97));border:1px solid var(--line);border-radius:23px;padding:20px;box-shadow:0 18px 55px #0005;margin-bottom:15px}.eyebrow{color:#b5aaff;font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:1.7px}.title{font-size:25px;font-weight:950;margin:4px 0 5px}.muted{color:var(--muted)}.tabs{display:flex;gap:8px;margin:0 0 15px}.tab{flex:1;border:1px solid var(--line);background:#151d32;color:var(--muted);border-radius:13px;padding:11px;font-weight:800}.tab.active{background:#5144a8;border-color:#8174e8;color:white}.game-tile{display:flex;align-items:center;gap:14px;width:100%;text-align:left;background:#1b2741;border:1px solid #354466;border-radius:18px;padding:15px;color:var(--text);margin-top:12px}.tile-icon{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;font-size:24px;background:linear-gradient(135deg,#7161dd,#a15dd0)}.game-tile strong{display:block;font-size:16px}.game-tile small{color:var(--muted)}button{font:inherit;cursor:pointer}.primary{width:100%;border:0;border-radius:14px;padding:14px 16px;background:linear-gradient(135deg,#7968f2,#5b68e8);color:white;font-weight:900;box-shadow:0 8px 24px #615be54a}.secondary{border:1px solid #405072;background:#202d49;color:white;border-radius:13px;padding:12px;font-weight:800}.danger{background:#54273b;border:1px solid #8a4058;color:#ff9aac}.input{width:100%;background:#0f172a;border:1px solid #3a4968;border-radius:13px;padding:14px;color:white;font:inherit;margin:9px 0 13px;outline:none}.game-head{display:flex;justify-content:space-between;align-items:center}.playing-card{margin:18px auto;width:132px;height:176px;border-radius:19px;background:linear-gradient(145deg,#fff,#dce5ff);color:#171d35;display:flex;flex-direction:column;justify-content:space-between;padding:14px;font-size:37px;font-weight:950;box-shadow:0 12px 30px #0006}.playing-card .suit{font-size:29px;align-self:flex-end}.stats{display:grid;grid-template-columns:1fr 1fr;gap:9px}.stat{background:#10192c;border:1px solid #2b3a59;border-radius:14px;padding:11px}.stat small{display:block;color:var(--muted);font-size:11px}.stat b{font-size:19px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:14px}.action{border:0;border-radius:13px;padding:14px 8px;font-weight:950;color:white}.up{background:linear-gradient(135deg,#149b77,#27c58a)}.down{background:linear-gradient(135deg,#bd4668,#ed637b)}.cash{margin-top:10px}.notice{font-size:12px;color:var(--muted);margin-top:14px}.hidden{display:none!important}.result{font-weight:900;margin:12px 0;color:#c4baff}.verify-button{margin-top:18px}.foot{color:#72809e;text-align:center;font-size:11px;margin:22px 0}
</style></head><body><main>
<header class="top"><div class="brand">✦ <span>GAME</span> HUB</div><div class="balance"><small>Баланс</small><span id="balance">—</span> 🥯</div></header>
<section id="verifyView" class="card hidden"><div class="eyebrow">Доступ к приложению</div><div class="title">Проверка аккаунта</div><p class="muted">Пройди проверку на повторные аккаунты, чтобы открыть игры. Для проверки используются Telegram ID, IP и технические характеристики устройства.</p><button id="verifyBtn" class="primary verify-button">Пройти проверку</button><div id="verifyResult" class="result"></div></section>
<section id="blockedView" class="card hidden"><div class="title">Доступ ограничен</div><p class="muted">Проверка не пройдена. Если это ошибка, обратись к администратору через бота.</p></section>
<section id="appView" class="hidden"><nav class="tabs"><button id="gamesTab" class="tab active">🎮 Игры</button><button id="profileTab" class="tab">👤 Профиль</button></nav>
<div id="gamesView" class="card"><div class="eyebrow">Выбирай и играй</div><div class="title">Игры</div><button id="openHilo" class="game-tile"><span class="tile-icon">🂡</span><span><strong>Хило</strong><small>Угадай, следующая карта выше или ниже</small></span><span style="margin-left:auto">›</span></button></div>
<div id="profileView" class="card hidden"><div class="eyebrow">Аккаунт</div><div class="title">Профиль</div><div class="stats"><div class="stat"><small>Telegram ID</small><b id="uid">—</b></div><div class="stat"><small>Статус</small><b>Пройден</b></div></div></div>
<div id="hiloView" class="card hidden"><div class="game-head"><div><div class="eyebrow">Карточная игра</div><div class="title">Хило</div></div><button id="backGames" class="secondary">← Игры</button></div>
<div id="hiloStart"><p class="muted">Ставка списывается при старте. Угадай, будет следующая карта выше или ниже текущей. Равная карта — проигрыш.</p><label class="muted" for="bet">Ставка в бубликах</label><input id="bet" class="input" type="number" min="10" step="1" value="10" inputmode="numeric"><button id="startHilo" class="primary">Начать игру</button></div>
<div id="hiloGame" class="hidden"><div class="playing-card"><span id="cardLabel">A</span><span class="suit">♠</span></div><div class="stats"><div class="stat"><small>Множитель</small><b id="mult">x1</b></div><div class="stat"><small>Выигрыш сейчас</small><b id="payout">0</b></div></div><div class="actions"><button id="lower" class="action down">↓ Ниже</button><button id="higher" class="action up">↑ Выше</button></div><button id="cash" class="primary cash hidden">Забрать выигрыш</button><div id="gameMessage" class="result"></div></div>
<div id="hiloDone" class="hidden"><div class="result" id="doneText"></div><button id="again" class="primary">Новая игра</button></div><div class="notice">Туз — A · Валет — J · Дама — Q · Король — K</div></div>
</section><div id="error" class="result"></div><div class="foot">GAME HUB · Mini App</div></main>
<script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const $=id=>document.getElementById(id);let currentGame=null,uid=null,busy=false;
async function api(path,extra={}){if(!tg?.initData)throw Error('Открой Mini App через кнопку в боте.');const r=await fetch(path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData,...extra})});const d=await r.json();if(!r.ok)throw Error(d.detail||'Ошибка сервера');return d;}
function showError(e){$('error').textContent=e?.message||String(e);}
function setBalance(v){$('balance').textContent=Number(v||0).toLocaleString('ru-RU');}
function setTab(which){$('gamesView').classList.toggle('hidden',which!=='games');$('profileView').classList.toggle('hidden',which!=='profile');$('gamesTab').classList.toggle('active',which==='games');$('profileTab').classList.toggle('active',which==='profile');$('hiloView').classList.add('hidden');}
function showHilo(){ $('gamesView').classList.add('hidden');$('profileView').classList.add('hidden');$('hiloView').classList.remove('hidden');$('gamesTab').classList.remove('active');$('profileTab').classList.remove('active'); }
function renderGame(d){setBalance(d.balance);currentGame=d.game;if(!currentGame){$('hiloStart').classList.remove('hidden');$('hiloGame').classList.add('hidden');$('hiloDone').classList.add('hidden');return;}if(currentGame.state==='active'){$('hiloStart').classList.add('hidden');$('hiloGame').classList.remove('hidden');$('hiloDone').classList.add('hidden');$('cardLabel').textContent=currentGame.card_label;$('mult').textContent='x'+currentGame.mult;$('payout').textContent=Number(currentGame.cashout).toLocaleString('ru-RU');$('cash').classList.toggle('hidden',!currentGame.can_cash);$('cash').disabled=false;$('lower').disabled=currentGame.card<=1;$('higher').disabled=currentGame.card>=13;$('gameMessage').textContent='Угадай следующую карту';}else{$('hiloStart').classList.add('hidden');$('hiloGame').classList.add('hidden');$('hiloDone').classList.remove('hidden');$('doneText').textContent=currentGame.state==='paid'?'Выигрыш зачислен: '+Number(currentGame.payout).toLocaleString('ru-RU')+' 🥯':'Не угадал. Игра окончена.';}}
async function loadGame(){const d=await api('/api/hilo/state');renderGame(d);}
async function actionGame(action){if(busy||!currentGame)return;busy=true;['lower','higher','cash'].forEach(x=>$(x).disabled=true);try{const d=await api('/api/hilo/action',{game_id:currentGame.id,rev:currentGame.rev,action});renderGame(d);if(d.game?.state==='active'&&d.game.rev!==currentGame.rev)$('gameMessage').textContent='Карта открыта!';}catch(e){showError(e)}finally{busy=false;if(currentGame?.state==='active'){$('lower').disabled=currentGame.card<=1;$('higher').disabled=currentGame.card>=13;$('cash').disabled=false;}}}
$('gamesTab').onclick=()=>setTab('games');$('profileTab').onclick=()=>setTab('profile');$('openHilo').onclick=async()=>{showHilo();try{await loadGame()}catch(e){showError(e)}};$('backGames').onclick=()=>setTab('games');$('again').onclick=()=>{currentGame=null;$('hiloDone').classList.add('hidden');$('hiloStart').classList.remove('hidden');};$('lower').onclick=()=>actionGame('low');$('higher').onclick=()=>actionGame('high');$('cash').onclick=()=>actionGame('cash');
$('startHilo').onclick=async()=>{const bet=Number($('bet').value);if(!Number.isSafeInteger(bet)||bet<10){showError(new Error('Минимальная ставка — 10 бубликов.'));return;}try{renderGame(await api('/api/hilo/start',{bet}));}catch(e){showError(e)}};
$('verifyBtn').onclick=async()=>{if(busy)return;busy=true;$('verifyBtn').disabled=true;$('verifyResult').textContent='Проверяем…';try{const hints=await navigator.userAgentData?.getHighEntropyValues(['model']).catch(()=>({}))||{};const device={model:String(hints.model||''),screen:[Math.min(screen.width,screen.height),Math.max(screen.width,screen.height)],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0,ratio:devicePixelRatio};const d=await api('/api/verify',{device,platform:String(tg.platform||''),api_version:String(tg.version||'')});if(d.status==='passed'){ $('verifyView').classList.add('hidden');$('appView').classList.remove('hidden');$('uid').textContent=uid||'—';await loadGame();}else{$('verifyResult').textContent='Проверка не пройдена.';$('blockedView').classList.remove('hidden');$('verifyView').classList.add('hidden');}}catch(e){$('verifyResult').textContent=e.message;}finally{busy=false;$('verifyBtn').disabled=false;}};
async function boot(){try{const d=await api('/api/status');uid=d.id||null;if(d.status==='passed'){$('appView').classList.remove('hidden');$('uid').textContent=uid||'—';setBalance(d.balance);await loadGame();}else if(d.status==='failed')$('blockedView').classList.remove('hidden');else $('verifyView').classList.remove('hidden');}catch(e){$('verifyView').classList.remove('hidden');$('verifyResult').textContent=e.message;}}
boot();
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
    with db() as c: bal=c.execute('SELECT amount FROM balances WHERE uid=?',(u['id'],)).fetchone()
    return JSONResponse({'status':s,'id':u['id'],'balance':bal['amount'] if bal else 0},headers={'Cache-Control':'no-store'})

@app.post('/api/verify')
async def verify(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data'))
    if await asyncio.to_thread(user_status,u['id'])=='failed': return {'status':'failed'}
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

def require_passed(u):
    if user_status(u['id'])!='passed': raise HTTPException(403,'Сначала пройди проверку аккаунта.')

@app.post('/api/hilo/state')
async def hilo_state(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data')); require_passed(u)
    return JSONResponse(await asyncio.to_thread(web_game_payload,u['id']),headers={'Cache-Control':'no-store'})

@app.post('/api/hilo/start')
async def hilo_start(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data')); require_passed(u)
    bet=d.get('bet')
    if type(bet) is not int or not 10<=bet<=MAX_BET: raise HTTPException(400,'Ставка должна быть от 10 до '+fmt(MAX_BET)+'.')
    g,error=await asyncio.to_thread(start_web_game,u['id'],bet)
    if error: raise HTTPException(400,error)
    return JSONResponse(await asyncio.to_thread(web_game_payload,u['id'],g),headers={'Cache-Control':'no-store'})

@app.post('/api/hilo/action')
async def hilo_web_action(req: Request):
    d=await read_json(req); u=validate_init(d.get('init_data')); require_passed(u)
    gid,rev,action=d.get('game_id'),d.get('rev'),d.get('action')
    if type(gid) is not int or type(rev) is not int or action not in ('low','high','cash'): raise HTTPException(400,'Некорректный ход.')
    g,error=await asyncio.to_thread(play_game,u['id'],gid,rev,action)
    if not g: raise HTTPException(400,error or 'Игра не найдена.')
    return JSONResponse(await asyncio.to_thread(web_game_payload,u['id'],g),headers={'Cache-Control':'no-store'})

if __name__=='__main__':
    import uvicorn
    # Parse trusted forwarding explicitly in get_ip, not twice.
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),proxy_headers=False)
