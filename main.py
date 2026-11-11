"""Telegram games, virtual currency only. Python 3.11+.
Install: pip install 'aiogram>=3.20,<4' fastapi uvicorn geoip2
Environment: BOT_TOKEN, APP_SECRET (32+ chars), ADMIN_IDS (comma-separated),
PUBLIC_URL or RENDER_EXTERNAL_URL (HTTPS). Optional DB_PATH, PORT, GEOIP_DB,
TRUST_PROXY=1 only behind one trusted ingress; deny direct access in that case.
Back up the database before upgrading. Existing users, balances, bonuses,
roulette rounds, verification records and active legacy HiLo/mines are retained.
No purchases, deposits or cash withdrawal. Not executed/tested by the author here.
Tower uses per-floor factors 1.21/1.62/2.43/4.85, compounded without intermediate
rounding. Display rounds DOWN to 2 places; payout rounds DOWN to integer.
"""
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager, asynccontextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ['BOT_TOKEN'].strip()
SECRET = os.environ['APP_SECRET']
if len(SECRET) < 32:
    raise RuntimeError('APP_SECRET должен содержать минимум 32 символа')
URL = (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL', '')).rstrip('/')
if not URL.startswith('https://'):
    raise RuntimeError('Нужен HTTPS PUBLIC_URL')
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
DB = os.getenv('DB_PATH', 'bot.sqlite3')
TRUST = os.getenv('TRUST_PROXY', '0') == '1'
HOOK = hmac.new(SECRET.encode(), b'telegram-webhook', hashlib.sha256).hexdigest()
MAX_BET, MAX_BAL, MAX_PAY = 10**9, 9*10**15, 10**12
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()
BOT_USERNAME = ''
WAITING = {}
E = {'gold':'5388787654022148728', 'tower':'5388571110361020010', 'question':'5447590953214846419', 'cash':'5262495450648300372', 'mine':'5375445874988036618', 'diamond':'5265083645185512728', 'explode':'5276032951342088188', 'key':'5330115548900501467', 'cancel':'5449395513789031846', 'crown':'5210952531676516344', 'paper':'5193157563046189671', 'trap':'5190404278556055959', 'ok':'5206607081334906820'}

def esc(x): return html.escape(str(x))
def fmt(x): return f'{int(x):,}'.replace(',', ' ')
def dec(x): return Decimal(str(x))
def two(x): return dec(x).quantize(Decimal('.01'), rounding=ROUND_DOWN)
def number(x): return format(two(x), 'f').rstrip('0').rstrip('.')
def em(key, fallback): return f'<tg-emoji emoji-id="{E[key]}">{fallback}</tg-emoji>'
def mention(uid, name): return f'<a href="tg://user?id={uid}">{esc(name)}</a>'
def btn(text, data=None, style=None, url=None):
    kw = {'text': text}
    if data: kw['callback_data'] = data
    if style: kw['style'] = style
    if url: kw['url'] = url
    return InlineKeyboardButton(**kw)
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

def fallback(keys):
    if not keys: return None
    d = keys.model_dump(exclude_none=True)
    for row in d['inline_keyboard']:
        for item in row:
            item.pop('style', None)
            item.pop('icon_custom_emoji_id', None)
    return InlineKeyboardMarkup.model_validate(d)

async def out(m, text, keys=None, edit=False):
    method = m.edit_text if edit else m.answer
    for attempt in range(4):
        try:
            return await method(text, reply_markup=keys)
        except TelegramRetryAfter as e:
            if attempt == 3: raise
            await asyncio.sleep(e.retry_after + .2)
        except TelegramBadRequest as e:
            if 'message is not modified' in str(e).lower(): return
            plain = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)
            try:
                return await method(plain, reply_markup=fallback(keys))
            except TelegramBadRequest as retry:
                if 'message is not modified' in str(retry).lower(): return
                if edit:
                    return await m.answer(plain, reply_markup=fallback(keys))
                raise

async def notify(text, keys=None):
    for uid in ADMINS:
        try: await bot.send_message(uid, text, reply_markup=keys)
        except Exception: logging.exception('Admin notification failed')

@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally: c.close()

def table(c, name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

def init():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',checked_at INTEGER);
        CREATE TABLE IF NOT EXISTS balances(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
        CREATE TABLE IF NOT EXISTS daily_bonus_v1(uid INTEGER PRIMARY KEY,last_claim INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS app_checks_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,ip TEXT NOT NULL,fp TEXT NOT NULL,device TEXT NOT NULL,geo TEXT NOT NULL,platform TEXT NOT NULL,api_version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_exempt_v6(uid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS app_appeals_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS app_audit_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,actor INTEGER,uid INTEGER,ts INTEGER,action TEXT);
        CREATE TABLE IF NOT EXISTS command9(chat INTEGER,msg INTEGER,PRIMARY KEY(chat,msg));
        CREATE TABLE IF NOT EXISTS r_round9(id INTEGER PRIMARY KEY,chat INTEGER,thread INTEGER DEFAULT 0,last REAL,state TEXT DEFAULT 'open',number INTEGER,ts INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS r_open9 ON r_round9(chat,thread) WHERE state='open';
        CREATE TABLE IF NOT EXISTS r_bet9(id INTEGER PRIMARY KEY,rid INTEGER,uid INTEGER,name TEXT,amount INTEGER,kind TEXT,rate TEXT,state TEXT DEFAULT 'active',payout INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS r_repeat9(source INTEGER,target INTEGER,uid INTEGER,PRIMARY KEY(source,target,uid));
        CREATE TABLE IF NOT EXISTS game_v12(id TEXT PRIMARY KEY,uid INTEGER NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS active_game_v12 ON game_v12(uid,state);
        ''')
        # Import active legacy games without changing the balance or hidden field.
        # If a user had two active legacy games, both are retained. No new game
        # can be created until both have been settled.
        c.execute('BEGIN IMMEDIATE')
        for oldtable, kind, prefix in [('app_hilo_v6','hilo','lh'),('mines_v1','mines','lm')]:
            if not table(c, oldtable): continue
            for row in c.execute(f"SELECT * FROM {oldtable} WHERE state='active'").fetchall():
                r = dict(row)
                gid = prefix + str(r['id'])
                if load(c, gid): continue
                u = c.execute('SELECT name FROM users WHERE id=?', (r['uid'],)).fetchone()
                g = {'id':gid,'uid':r['uid'],'name':u['name'] if u else str(r['uid']), 'kind':kind,'state':'active','bet':r['bet'],'rev':r['rev'], 'step':0,'payout':0,'legacy':True,'created':int(time.time())}
                if kind == 'hilo':
                    g.update(card=r['card'], mult=r['mult'], step=r['wins'], history=[], total=0)
                else:
                    mines = [i for i in range(25) if r['mine_mask'] & (1 << i)]
                    opened = [i for i in range(25) if r['opened_mask'] & (1 << i)]
                    g.update(mines=r['mine_count'],field=[mines],opened=opened,step=len(opened),total=25-r['mine_count'],hit=None,cents=r['cents'])
                save(c, g)
                c.execute(f"UPDATE {oldtable} SET state='migrated_v12' WHERE id=?", (r['id'],))

def touch(uid, name):
    with db() as c:
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid,name[:256]))
        c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)', (uid,))
def status(uid):
    with db() as c:
        r = c.execute('SELECT status FROM users WHERE id=?', (uid,)).fetchone()
        return r['status'] if r else 'pending'
def allowed(c, uid):
    r = c.execute('SELECT status FROM users WHERE id=?', (uid,)).fetchone()
    if not r or r['status'] != 'passed': raise ValueError('Сначала пройдите проверку. При бане: /appeal объяснение')
def balance(c, uid):
    c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)', (uid,))
    return c.execute('SELECT amount FROM balances WHERE uid=?', (uid,)).fetchone()[0]
def change(c, uid, delta):
    value = balance(c, uid) + int(delta)
    if value < 0: raise ValueError('Недостаточно бубликов.')
    if value > MAX_BAL: raise ValueError('Лимит баланса. Обратитесь к администратору.')
    c.execute('UPDATE balances SET amount=? WHERE uid=?', (value,uid))
    return value
def once(c, m):
    if c.execute('SELECT 1 FROM command9 WHERE chat=? AND msg=?',(m.chat.id,m.message_id)).fetchone(): raise ValueError('Команда уже обработана. Напишите «продолжить».')
    c.execute('INSERT INTO command9 VALUES(?,?)',(m.chat.id,m.message_id))
def audit(c, actor, uid, action):
    c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(actor,uid,int(time.time()),action))
def save(c, g):
    c.execute('INSERT OR REPLACE INTO game_v12 VALUES(?,?,?,?,?,?)',(g['id'],g['uid'],g['kind'],g['state'],json.dumps(g,ensure_ascii=False),g['created']))
def load(c, gid):
    r = c.execute('SELECT data FROM game_v12 WHERE id=?',(gid,)).fetchone()
    return json.loads(r[0]) if r else None
def active(c, uid):
    return [json.loads(r[0]) for r in c.execute("SELECT data FROM game_v12 WHERE uid=? AND state='active' ORDER BY created,id",(uid,))]

def verify_keys(private):
    if private:
        return kb([[InlineKeyboardButton(text='Пройти проверку',web_app=WebAppInfo(url=URL))]])
    return kb([[btn('Пройти проверку',url=f'https://t.me/{BOT_USERNAME}?start=verify')]])

class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if not u or u.is_bot or isinstance(event,Message) and event.sender_chat: return
        touch(u.id,u.full_name)
        s = status(u.id)
        m = event if isinstance(event,Message) else event.message
        if not isinstance(m,Message): return
        cmd = (event.text or '').split(maxsplit=1)[0].split('@')[0].lower() if isinstance(event,Message) and event.text else ''
        private = m.chat.type == 'private'
        exception = private and (cmd in ('/appeal','/оспорить') or u.id in ADMINS and (cmd in ('/panel','/панель','/ban','/unban','/recheck','/cancel') or u.id in WAITING or isinstance(event,CallbackQuery) and (event.data or '').startswith('adm:')))
        if s == 'passed' or exception: return await handler(event,data)
        if isinstance(event,CallbackQuery): await event.answer('Пройдите проверку' if s=='pending' else 'Вы забанены',show_alert=True)
        await out(m,'<b>🛡 Сначала пройдите проверку!</b>' if s=='pending' else '<b>⛔ Вы забанены. Обжалование в личке: /appeal объяснение</b>',verify_keys(private) if s=='pending' else None)
dp.message.middleware(Gate())
dp.callback_query.middleware(Gate())

NAMES = {'gold':'🔱 Золото','tower':'🏯 Башня','hilo':'🎴 Хило','mines':'💣 Мины'}
RULES = {
'gold':'<b>🔱 Золото | Help game</b>\nКоманда: <code>золото 40</code>\n12 шагов снизу вверх. В каждом ряду две клетки, одна мина. Каждый успех удваивает выплату: ставка 40 → 80 → 160. После успеха можно забрать деньги; мина обнуляет выплату. После 12 успехов выплата автоматическая.',
'tower':'<b>🏯 Башня | Help game</b>\nКоманда: <code>башня 100 2</code>\n8 этажей, 5 клеток на каждом. Мин от 1 до 4. Выбирайте безопасные клетки и забирайте выплату после успешного хода. Множители за этаж: 1 мина х1.21; 2 х1.62; 3 х2.43; 4 х4.85. Общий коэффициент равен множителю в степени числа этажей. На 8-м этаже автоматическая выплата.',
'hilo':'<b>🎴 Хило | Help game</b>\nКоманда: <code>хило 100</code>\nУгадайте выше или ниже, от A=1 до K=13. Текущая карта исключается из следующего выпадения. Вероятность успеха: число подходящих карт / 12. Коэффициент хода = 11.88 / число подходящих карт, с округлением вниз до сотых. После успеха можно забрать выплату.',
'mines':'<b>💣 Мины | Help game</b>\nКоманда: <code>мины 100 3</code>\n25 клеток, от 1 до 24 мин. Открывайте безопасные клетки и забирайте выплату. Коэффициент зависит от числа мин и открытых клеток. При открытии всех безопасных клеток выплата автоматическая.',
'roulette':'<b>🎡 Рулетка | Help game</b>\n<code>100 к</code>, <code>100 ч</code>, <code>100 7</code>, <code>100 одд</code>, <code>100 евен</code>, <code>100 1-18</code>. Можно указать несколько типов после суммы.\n<code>го</code> через 13 секунд после последней ставки. <code>ставки</code>, <code>отмена</code>, <code>лог</code>. Ноль не выигрывает цвет, чётность или нечётность. Цвет/чётность х1.98, число х35.98.'}
HELP = '<b>📎 Помощь</b>\n<blockquote><code>б</code> — баланс\n<code>бонус</code> — 5 000 раз в 24 часа\n<code>топ б</code> — топ балансов\n<code>игры</code> — правила игр\n<code>продолжить</code> — текущая игра\n<code>лог игр</code> — ваша история\n<code>/appeal объяснение</code> — апелляция</blockquote>\nВсе бублики виртуальные. Покупки и вывода нет. Выплата включает ставку. Минимум ставки 10, максимум 1 000 000 000. Лимит выплаты 1 000 000 000 000. Одновременно одна новая игра из хило, мин, золота и башни. Рулетка отдельная. До первого хода доступна отмена.'

def menu(): return kb([[btn('Профиль','nav:profile'),btn('Топ','nav:top')],[btn('Игры','nav:games'),btn('Помощь','nav:help')]])
def game_menu(): return kb([[btn(NAMES[k],f'rules:{k}')] for k in NAMES]+[[btn('🎡 Рулетка','rules:roulette')],[btn('Назад','nav:home')]])

def mult(g, step=None):
    n = g['step'] if step is None else step
    if g['kind']=='gold': return dec(2)**n
    if g['kind']=='tower': return {1:dec('1.21'),2:dec('1.62'),3:dec('2.43'),4:dec('4.85')}[g['mines']]**n
    if g['kind']=='hilo': return dec(g['mult'])
    return dec(g.get('cents',100))/100

def payout(g, step=None): return min(MAX_PAY,int(dec(g['bet'])*mult(g,step)))
def card(n): return {1:'A',11:'J',12:'Q',13:'K'}.get(n,str(n))
def hilo_rate(n, direction):
    count = n-1 if direction=='low' else 13-n
    return two(dec('11.88')/count) if count else None

def payload(g): return json.dumps({'field':g['field'],'salt':g['salt']},sort_keys=True,separators=(',',':'))
def gamebtn(g, text, action, style=None): return btn(text,f'g:{g["id"]}:{g["rev"]}:{action}',style)

def render(g, fair=False):
    title = NAMES[g['kind']]
    who = mention(g['uid'],g['name'])
    if fair:
        text = who+'\n<b>🔑 '+title+' · Честность</b>\n'
        if not g.get('hash'):
            text += 'В старой игре поле заранее не фиксировалось хэшем.'
        else:
            text += '<b>SHA-256:</b> <code>'+g['hash']+'</code>\n'
            if g['state']=='active': text += '<b>Результат:</b> игра не завершилась.'
            else:
                text += '<b>Данные для проверки:</b>\n<code>'+esc(payload(g))+'</code>\nSHA-256 от UTF-8 строки выше должен совпасть с исходным хэшем. field: индексы мин с нуля; этажи снизу вверх. В минах клетки слева направо по строкам. Хэш подтверждает неизменность поля, но не доказывает сам по себе случайность генерации.'
        return text,kb([[gamebtn(g,'Назад','back')]])
    state = g['state']
    label = {'active':'Game On','paid':'👑 Победа! ✔️','lost':'💥 Проигрыш','cancelled':'🚫 Отменено'}[state]
    text = who+f'\n<b>{title} | {label}</b>\n<blockquote><b>🧻 Ставка:</b> <code>{fmt(g["bet"])}</code> <b>бубликов</b>\n'
    if g['kind']=='hilo': text += '<b>Карта:</b> <code>'+card(g['card'])+'</code>\n'
    else: text += f'<b>Шагов:</b> <code>{g["step"]}/{g["total"]}</code>\n'
    if 'mines' in g: text += f'<b>💣 Мин:</b> <code>{g["mines"]}</code>\n'
    text += f'<b>Коэффициент:</b> <code>х{number(mult(g))}</code>\n'
    amount = payout(g) if state=='active' else g['payout']
    text += f'<b>{"Можно забрать" if state=="active" else "Выплата"}:</b> <code>{fmt(amount)}</code> <b>бубликов</b></blockquote>'
    rows = []
    if g['kind'] in ('gold','tower'):
        fieldrows = []
        for i in reversed(range(g['total'])):
            chosen = g['choices'][i] if i<len(g['choices']) else None
            revealed = state!='active' or i<g['step']
            cells = []
            for j in range(g['width']):
                cells.append('❓' if not revealed else ('💥' if j==chosen else '💣') if j in g['field'][i] else '💰' if j==chosen else '💎')
            line = '|'.join(cells)
            if g['kind']=='gold': line += f' {fmt(payout(g,i+1))} бубликов ({2**(i+1)}x)'
            fieldrows.append(line)
        text += '\n\n'+'\n'.join(fieldrows)
        if state=='active': rows.append([gamebtn(g,'❓',str(i)) for i in range(g['width'])])
    elif g['kind']=='mines':
        for y in range(5):
            row=[]
            for x in range(5):
                cell=y*5+x
                face = '💥' if state!='active' and g.get('hit')==cell else '💣' if state!='active' and cell in g['field'][0] else '💎' if cell in g['opened'] or state!='active' else '❓'
                row.append(gamebtn(g,face,str(cell) if state=='active' and cell not in g['opened'] else 'noop'))
            rows.append(row)
    elif state=='active':
        row=[]
        for action,label,style in [('low','Ниже','danger'),('high','Выше','success')]:
            k=hilo_rate(g['card'],action)
            if k: row.append(gamebtn(g,label+' х'+number(k),action,style))
        rows.append(row)
    if g['kind']=='hilo' and g.get('history'):
        text += '\n\n<b>Последние ходы:</b>\n'+'\n'.join(esc(x) for x in g['history'][-12:])
    if state=='active':
        if g['step']: rows.append([gamebtn(g,'👑 Забрать выигрыш','cash','primary')])
        else: rows.append([gamebtn(g,'🚫 Отменить','cancel')])
    if g.get('hash'):
        text += '\n\n<b>Исходный SHA-256:</b> <code>'+g['hash']+'</code>'
        rows.append([gamebtn(g,'🔑 Честность','fair')])
    return text,kb(rows)

def create(c, uid, name, kind, bet, mines):
    g = dict(id=secrets.token_hex(7),uid=uid,name=name[:100],kind=kind,state='active',bet=bet,step=0,rev=0,payout=0,created=int(time.time()))
    if kind=='hilo': g.update(card=secrets.randbelow(13)+1,mult='1',history=[],total=0)
    else:
        g.update(mines=mines,salt=secrets.token_hex(32))
        if kind=='mines':
            g.update(total=25-mines,field=[sorted(secrets.SystemRandom().sample(range(25),mines))],opened=[],hit=None,cents=100)
        else:
            total,width=(12,2) if kind=='gold' else (8,5)
            g.update(total=total,width=width,choices=[],field=[sorted(secrets.SystemRandom().sample(range(width),mines)) for _ in range(total)])
        g['hash']=hashlib.sha256(payload(g).encode()).hexdigest()
    change(c,uid,-bet)
    save(c,g)
    return g

def finish(c,g,state,amount):
    if amount: change(c,g['uid'],amount)
    g.update(state=state,payout=amount)
    audit(c,g['uid'],g['uid'],f'{g["kind"]} {g["id"]} {state}: bet={g["bet"]}, payout={amount}')

def move(c,g,action):
    if action=='cash':
        if not g['step']: raise ValueError('Сначала сделайте успешный ход.')
        finish(c,g,'paid',payout(g))
    elif action=='cancel':
        if g['step']: raise ValueError('После хода можно только забрать выигрыш.')
        finish(c,g,'cancelled',g['bet'])
    elif g['kind']=='hilo':
        if action not in ('low','high'): raise ValueError('Неизвестное действие.')
        k=hilo_rate(g['card'],action)
        if k is None: raise ValueError('Невозможный ход.')
        old=g['card']
        n=secrets.randbelow(12)+1
        if n>=old: n+=1
        win=n<old if action=='low' else n>old
        g['card']=n
        g['history']=(g.get('history',[])+[f'{card(old)} → {card(n)} · {"ниже" if action=="low" else "выше"} · {"успех" if win else "проигрыш"}'])[-30:]
        if win:
            g['step']+=1
            g['mult']=str(two(dec(g['mult'])*k))
            if dec(g['bet'])*mult(g)>=MAX_PAY: finish(c,g,'paid',MAX_PAY)
        else: finish(c,g,'lost',0)
    elif g['kind']=='mines':
        if not action.isdigit() or not 0<=int(action)<25: raise ValueError('Неверная клетка.')
        cell=int(action)
        if cell in g['opened']: return
        if cell in g['field'][0]:
            g['hit']=cell
            finish(c,g,'lost',0)
        else:
            g['opened'].append(cell)
            g['step']+=1
            numerator=95*math.comb(25,g['step'])
            denominator=math.comb(25-g['mines'],g['step'])
            g['cents']=(2*numerator+denominator)//(2*denominator)
            if g['step']==g['total'] or payout(g)>=MAX_PAY: finish(c,g,'paid',payout(g))
    else:
        if not action.isdigit() or not 0<=int(action)<g['width']: raise ValueError('Неверная клетка.')
        cell=int(action)
        g['choices'].append(cell)
        if cell in g['field'][g['step']]: finish(c,g,'lost',0)
        else:
            g['step']+=1
            if g['step']==g['total'] or payout(g)>=MAX_PAY: finish(c,g,'paid',payout(g))
    g['rev']+=1
    save(c,g)

@dp.callback_query(F.data.startswith('g:'))
async def game_callback(q:CallbackQuery):
    if not isinstance(q.message,Message): await q.answer(); return
    try:
        _,gid,rev,action=q.data.split(':')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            allowed(c,q.from_user.id)
            g=load(c,gid)
            if not g or g['uid']!=q.from_user.id: raise ValueError('Это не ваша игра.')
            stale=int(rev)!=g['rev']
            if action not in ('fair','back','noop') and g['state']=='active' and not stale: move(c,g,action)
        await q.answer('Ход уже обработан. Показано актуальное состояние.' if stale and action not in ('fair','back','noop') else None)
        text,keys=render(g,action=='fair')
        await out(q.message,text,keys,True)
    except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Old buttons cannot mutate old tables after migration.
@dp.callback_query(F.data.startswith('hl:') | F.data.startswith('mn:'))
async def old_button(q:CallbackQuery):
    await q.answer('Игра сохранена. Напишите «продолжить» для новых кнопок.',show_alert=True)

async def start_game(m,kind,parts):
    uid=m.from_user.id
    with db() as c: games=active(c,uid)
    if games:
        await out(m,'У вас уже есть текущая игра. Продолжите её или заберите выплату после успешного хода.')
        for g in games:
            text,keys=render(g); await out(m,text,keys)
        return
    try:
        required=3 if kind in ('tower','mines') else 2
        if len(parts)!=required: await out(m,RULES[kind]); return
        if not re.fullmatch(r'\d{1,10}',parts[1]): raise ValueError('Нужна целая положительная ставка.')
        bet=int(parts[1])
        if not 10<=bet<=MAX_BET: raise ValueError('Ставки от 10 до 1 000 000 000 бубликов.')
        mines=int(parts[2]) if required==3 else 1
        if kind=='tower' and not 1<=mines<=4 or kind=='mines' and not 1<=mines<=24: raise ValueError('Неверное количество мин.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
            if active(c,uid): raise ValueError('Уже есть текущая игра. Напишите «продолжить».')
            g=create(c,uid,m.from_user.full_name,kind,bet,mines)
        text,keys=render(g); await out(m,text,keys)
    except ValueError as e: await out(m,esc(e))

RED={1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
def color(n): return '🟢' if n==0 else '🔴' if n in RED else '⚫️'
def betlabel(k): return {'red':'🔴 RED','black':'⚫️ BLACK','odd':'ODD','even':'EVEN'}.get(k,k)
def parsebet(s):
    aliases={'к':'red','красный':'red','ч':'black','черный':'black','чёрный':'black','одд':'odd','нечет':'odd','нечёт':'odd','евен':'even','чет':'even','чёт':'even','з':'0','зеленый':'0','зелёный':'0'}
    s=aliases.get(s,s)
    if s in ('red','black','odd','even'): return s,dec('1.98')
    if re.fullmatch(r'\d{1,2}',s) and 0<=int(s)<=36: return str(int(s)),dec('35.98')
    r=re.fullmatch(r'(\d{1,2})-(\d{1,2})',s)
    if r:
        a,z=map(int,r.groups())
        if 0<=a<z<=36: return f'{a}-{z}',dec('1.98') if (a,z) in ((1,18),(19,36)) else two(dec('35.98')/(z-a+1))
    raise ValueError('Неверный тип ставки.')
def wins(k,n):
    if k=='red': return n in RED
    if k=='black': return n!=0 and n not in RED
    if k=='odd': return n%2==1
    if k=='even': return n!=0 and n%2==0
    if '-' in k:
        a,z=map(int,k.split('-')); return a<=n<=z
    return int(k)==n

def round_open(c,chat,thread):
    r=c.execute("SELECT * FROM r_round9 WHERE chat=? AND thread=? AND state='open'",(chat,thread)).fetchone()
    if not r:
        rid=c.execute('INSERT INTO r_round9(chat,thread,last,ts) VALUES(?,?,?,?)',(chat,thread,0,int(time.time()))).lastrowid
        r=c.execute('SELECT * FROM r_round9 WHERE id=?',(rid,)).fetchone()
    return r

def place(c,r,uid,name,bets):
    allowed(c,uid)
    report=[]; accepted=0
    for amount,kind,rate in bets:
        if not 10<=amount<=MAX_BET: report.append(f'❗ {fmt(amount)} на {betlabel(kind)}: лимит ставки'); continue
        if balance(c,uid)<amount: report.append(f'❗ {fmt(amount)} на {betlabel(kind)}: недостаточно средств'); continue
        change(c,uid,-amount)
        c.execute('INSERT INTO r_bet9(rid,uid,name,amount,kind,rate) VALUES(?,?,?,?,?,?)',(r['id'],uid,name[:100],amount,kind,str(rate)))
        report.append(f'✅ {fmt(amount)} на {betlabel(kind)}'); accepted+=1
    if accepted: c.execute('UPDATE r_round9 SET last=? WHERE id=?',(time.time(),r['id']))
    return report,accepted

async def lines_out(m,title,lines):
    chunk=title
    for line in lines:
        line='\n'+line
        if len(html.unescape(re.sub('<[^>]*>','',chunk+line)).encode('utf-16-le'))//2>3800:
            await out(m,chunk); chunk=title
        chunk+=line
    await out(m,chunk)

def results(c,r,page=0):
    count=c.execute("SELECT count(*) FROM r_bet9 WHERE rid=? AND state='settled' AND payout>0",(r['id'],)).fetchone()[0]
    page=max(0,min(page,max(0,(count-1)//5)))
    rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND state='settled' AND payout>0 ORDER BY id LIMIT 5 OFFSET ?",(r['id'],page*5)).fetchall()
    text=f'<b>👑 Выпало: {color(r["number"])} {r["number"]}</b>\n\n'
    text+='\n'.join(mention(x['uid'],x['name'])+f' ставил {fmt(x["amount"])} на {esc(betlabel(x["kind"]))}, выплата {fmt(x["payout"])}' for x in rows) or 'Победителей нет.'
    if not rows: text+='Победителей нет.'
    nav=[]
    if page: nav.append(btn('←',f'rp:{r["id"]}:{page-1}'))
    if (page+1)*5<count: nav.append(btn('→',f'rp:{r["id"]}:{page+1}'))
    buttons=[nav] if nav else []
    buttons.append([btn('Повторить',f'rr:{r["id"]}:1'),btn('Удвоить',f'rr:{r["id"]}:2')])
    return text,kb(buttons)

async def roulette(m,parts):
    cmd=parts[0]; uid=m.from_user.id; chat=m.chat.id; thread=m.message_thread_id or 0
    try:
        if cmd=='лог':
            with db() as c: rows=c.execute("SELECT number FROM r_round9 WHERE chat=? AND thread=? AND state='done' ORDER BY id DESC LIMIT 10",(chat,thread)).fetchall()
            await out(m,'<b>Лог рулетки · новые сверху</b>\n'+('\n'.join(f'{color(r[0])} {r[0]}' for r in rows) or 'Пока пусто.')); return
        if cmd=='ставки':
            with db() as c: rows=c.execute("SELECT b.* FROM r_bet9 b JOIN r_round9 r ON r.id=b.rid WHERE r.chat=? AND r.thread=? AND r.state='open' AND b.uid=? AND b.state='active' ORDER BY b.id",(chat,thread,uid)).fetchall()
            await lines_out(m,'<b>Ваши ставки</b>',[f'{fmt(x["amount"])} на {esc(betlabel(x["kind"]))}' for x in rows] or ['Ставок нет.']); return
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
            r=round_open(c,chat,thread)
            if cmd=='отмена':
                total=c.execute("SELECT coalesce(sum(amount),0) FROM r_bet9 WHERE rid=? AND uid=? AND state='active'",(r['id'],uid)).fetchone()[0]
                change(c,uid,total)
                c.execute("UPDATE r_bet9 SET state='cancelled' WHERE rid=? AND uid=? AND state='active'",(r['id'],uid))
                text=f'Возвращено {fmt(total)} бубликов.'; keys=None
            elif cmd=='го':
                rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND state='active'",(r['id'],)).fetchall()
                if not rows: raise ValueError('Ставок нет.')
                wait=math.ceil(13-(time.time()-r['last']))
                if wait>0: raise ValueError(f'Подождите {wait} секунд.')
                n=secrets.randbelow(37)
                for x in rows:
                    pay=int(dec(x['amount'])*dec(x['rate'])) if wins(x['kind'],n) else 0
                    if pay: change(c,x['uid'],pay)
                    c.execute("UPDATE r_bet9 SET state='settled',payout=? WHERE id=?",(pay,x['id']))
                c.execute("UPDATE r_round9 SET state='done',number=?,ts=? WHERE id=?",(n,int(time.time()),r['id']))
                r=c.execute('SELECT * FROM r_round9 WHERE id=?',(r['id'],)).fetchone()
                text,keys=results(c,r)
            else:
                if len(parts)<2 or len(parts)>151: raise ValueError('Укажите сумму и от 1 до 150 типов ставок.')
                amount=int(parts[0])
                report,_=place(c,r,uid,m.from_user.full_name,[(amount,*parsebet(s)) for s in parts[1:]])
                text=None
        if text is None: await lines_out(m,'<b>Ставки обработаны</b>',[esc(x) for x in report])
        else: await out(m,text,keys)
    except ValueError as e: await out(m,esc(e))

@dp.callback_query(F.data.startswith('rr:') | F.data.startswith('rp:') | F.data.startswith('rpage:'))
async def roulette_button(q:CallbackQuery):
    if not isinstance(q.message,Message): await q.answer(); return
    try:
        action,rid,arg=q.data.split(':'); rid=int(rid); arg=int(arg)
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
            r=c.execute("SELECT * FROM r_round9 WHERE id=? AND state='done'",(rid,)).fetchone()
            if not r or r['chat']!=q.message.chat.id or r['thread']!=(q.message.message_thread_id or 0): raise ValueError('Раунд недоступен.')
            if action in ('rp','rpage'): text,keys=results(c,r,arg)
            else:
                if arg not in (1,2): raise ValueError('Неверная кнопка.')
                last=c.execute("SELECT max(id) FROM r_round9 WHERE chat=? AND thread=? AND state='done'",(r['chat'],r['thread'])).fetchone()[0]
                if last!=rid: raise ValueError('Используйте последний раунд.')
                target=round_open(c,r['chat'],r['thread'])
                if c.execute('SELECT 1 FROM r_repeat9 WHERE source=? AND target=? AND uid=?',(rid,target['id'],q.from_user.id)).fetchone(): raise ValueError('Уже повторено.')
                bets=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND uid=? AND state='settled' ORDER BY id",(rid,q.from_user.id)).fetchall()
                if not bets: raise ValueError('У вас не было ставок в этом раунде.')
                report,accepted=place(c,target,q.from_user.id,q.from_user.full_name,[(x['amount']*arg,x['kind'],dec(x['rate'])) for x in bets])
                if accepted: c.execute('INSERT INTO r_repeat9 VALUES(?,?,?)',(rid,target['id'],q.from_user.id))
        await q.answer()
        if action=='rr': await lines_out(q.message,'<b>Ставки обработаны</b>',[esc(x) for x in report])
        else: await out(q.message,text,keys,True)
    except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

async def top(m,edit=False):
    with db() as c:
        rows=c.execute("SELECT u.id,u.name,b.amount FROM users u JOIN balances b ON b.uid=u.id WHERE u.status='passed' ORDER BY b.amount DESC,u.id LIMIT 10").fetchall()
    text='<b>🏆 Топ по балансу</b>\n'+'\n'.join(f'{i}. {mention(r["id"],r["name"][:55])}: {fmt(r["amount"])}' for i,r in enumerate(rows,1))
    await out(m,text or 'Пока пусто.',menu(),edit)

@dp.callback_query(F.data.startswith('nav:') | F.data.startswith('rules:'))
async def navigation(q:CallbackQuery):
    await q.answer()
    if not isinstance(q.message,Message): return
    prefix,key=q.data.split(':')
    if prefix=='rules':
        if key in RULES: await out(q.message,RULES[key],kb([[btn('Назад','nav:games')]]),True)
    elif key=='top': await top(q.message,True)
    elif key=='games': await out(q.message,'<b>Игры</b>',game_menu(),True)
    elif key=='help': await out(q.message,HELP,menu(),True)
    elif key=='profile':
        with db() as c: amount=balance(c,q.from_user.id)
        await out(q.message,f'<b>Профиль</b>\n{mention(q.from_user.id,q.from_user.full_name)}\nID: <code>{q.from_user.id}</code>\nБаланс: {fmt(amount)}\nПроверка пройдена.',menu(),True)
    else: await out(q.message,'<b>Добро пожаловать!</b>\nИгры на виртуальные бублики.',menu(),True)

async def bonus(m):
    uid=m.from_user.id; now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
        r=c.execute('SELECT last_claim FROM daily_bonus_v1 WHERE uid=?',(uid,)).fetchone()
        left=max(0,r[0]+86400-now) if r else 0
        if not left:
            change(c,uid,5000)
            c.execute('INSERT INTO daily_bonus_v1 VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_claim=excluded.last_claim',(uid,now))
            audit(c,uid,uid,'daily_bonus +5000')
    if left:
        hours,left=divmod(left,3600); minutes,seconds=divmod(left,60)
        await out(m,f'⏲ Подождите {hours}ч {minutes:02d}м {seconds:02d}с.')
    else: await out(m,'<b>🎁 Ежедневный бонус</b>\n<blockquote><code>+5 000</code> бубликов</blockquote>')

def mod(c,actor,uid,action,reason=''):
    c.execute('INSERT OR IGNORE INTO users(id,name) VALUES(?,?)',(uid,str(uid)))
    c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',({'ban':'failed','unban':'passed','recheck':'pending'}[action],int(time.time()),uid))
    if action=='unban': c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(uid,))
    else: c.execute('DELETE FROM app_exempt_v6 WHERE uid=?',(uid,))
    c.execute("UPDATE app_appeals_v6 SET state='closed' WHERE uid=? AND state='open'",(uid,))
    audit(c,actor,uid,action+' '+reason[:1000])

def panel_keys(): return kb([[btn('Логи','adm:logs:0'),btn('Апелляции','adm:appeals:0')],[btn('Бан','adm:input:ban'),btn('Разбан','adm:input:unban')],[btn('Повторная проверка','adm:input:recheck')]])
def appeal_keys(aid): return kb([[btn('Одобрить',f'adm:approve:{aid}'),btn('Отклонить',f'adm:reject:{aid}')]])
def logtext(r):
    return '<b>Проверка #'+str(r['id'])+'</b>\n'+'\n'.join(esc(f'{k}: {r[k]}') for k in ('uid','status','ip','device','geo','platform','api_version','reason'))

@dp.callback_query(F.data.startswith('adm:'))
async def admin_button(q:CallbackQuery):
    if q.from_user.id not in ADMINS or not isinstance(q.message,Message) or q.message.chat.type!='private': await q.answer('Нет доступа'); return
    await q.answer()
    try:
        _,action,raw=q.data.split(':')
        if action=='input':
            if raw in ('ban','unban','recheck'):
                WAITING[q.from_user.id]=(raw,time.time()+300)
                await out(q.message,'Пришлите ID и причину. /cancel для отмены.')
            return
        n=max(0,int(raw))
        with db() as c:
            if action=='logs':
                r=c.execute('SELECT * FROM app_checks_v6 ORDER BY id DESC LIMIT 1 OFFSET ?',(n,)).fetchone()
                text=logtext(r) if r else 'Записей больше нет.'
                keys=kb([[btn('←',f'adm:logs:{max(0,n-1)}'),btn('→',f'adm:logs:{n+1}')]])
            elif action=='appeals':
                r=c.execute("SELECT * FROM app_appeals_v6 WHERE state='open' AND id>? ORDER BY id LIMIT 1",(n,)).fetchone()
                text=f'Апелляция #{r["id"]}\nID {r["uid"]}\n{esc(r["body"])}' if r else 'Нет заявок.'
                keys=kb([[btn('Одобрить',f'adm:approve:{r["id"]}'),btn('Отклонить',f'adm:reject:{r["id"]}')],[btn('Следующая',f'adm:appeals:{r["id"]}')]]) if r else None
            elif action in ('approve','reject'):
                c.execute('BEGIN IMMEDIATE')
                r=c.execute("SELECT * FROM app_appeals_v6 WHERE id=? AND state='open'",(n,)).fetchone()
                if not r: raise ValueError('Уже обработано.')
                if action=='approve': mod(c,q.from_user.id,r['uid'],'unban','appeal')
                c.execute('UPDATE app_appeals_v6 SET state=? WHERE id=?',(action,n))
                audit(c,q.from_user.id,r['uid'],action)
                text='Апелляция одобрена.' if action=='approve' else 'Апелляция отклонена.'; keys=None
            else: return
        await out(q.message,text,keys)
        if action in ('approve','reject'):
            try: await bot.send_message(r['uid'],text)
            except Exception: pass
    except ValueError as e: await out(q.message,esc(e))

async def admin_command(m,cmd,parts):
    uid=m.from_user.id
    if uid not in ADMINS: return
    if cmd in ('give','take'):
        if len(parts)==2 and m.reply_to_message and m.reply_to_message.from_user:
            target=m.reply_to_message.from_user
            touch(target.id,target.full_name); targetid=target.id; raw=parts[1]
        elif len(parts)==3: targetid=int(parts[1]); raw=parts[2]
        else: raise ValueError('/give ID сумма или ответом /give сумма')
        amount=int(raw)
        if not 1<=amount<=MAX_BAL or targetid<=0: raise ValueError('Неверная сумма или ID.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
            value=change(c,targetid,amount if cmd=='give' else -amount)
            audit(c,uid,targetid,f'{cmd} {amount}')
        await out(m,f'ID {targetid}, баланс {fmt(value)}'); return
    if m.chat.type!='private': return
    if cmd in ('panel','панель'):
        WAITING.pop(uid,None)
        await out(m,'<b>Панель администратора</b>\n/give ID сумма\n/take ID сумма\n/ban ID причина\n/unban ID\n/recheck ID',panel_keys()); return
    if cmd=='cancel': WAITING.pop(uid,None); await out(m,'Отменено.'); return
    if cmd in ('ban','unban','recheck'):
        if len(parts)<2: raise ValueError('Укажите ID.')
        targetid=int(parts[1])
        if not 0<targetid<10**15: raise ValueError('Неверный ID.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); mod(c,uid,targetid,cmd,' '.join(parts[2:]))
        WAITING.pop(uid,None)
        await out(m,'Выполнено.')
        try: await bot.send_message(targetid,{'ban':'Вы забанены. /appeal объяснение','unban':'Бан снят. /start','recheck':'Разрешена повторная проверка. /start'}[cmd])
        except Exception: pass

async def appeal(m):
    if m.chat.type!='private': return
    body=m.text.partition(' ')[2].strip(); uid=m.from_user.id; now=int(time.time())
    if not 5<=len(body)<=1500: raise ValueError('/appeal объяснение от 5 до 1500 символов')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM app_appeals_v6 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if old and (old['state']=='open' or now-old['ts']<86400): raise ValueError('Уже подана заявка. Не чаще раза в сутки.')
        aid=c.execute('INSERT INTO app_appeals_v6(uid,ts,body) VALUES(?,?,?)',(uid,now,body)).lastrowid
    await out(m,'Заявка сохранена.')
    await notify(f'Заявка #{aid}, ID {uid}\n{esc(body)}',appeal_keys(aid))

@dp.message(F.text)
async def messages(m:Message):
    text=m.text.strip(); parts=text.split()
    if not parts: return
    cmd=parts[0].lower().split('@')[0].lstrip('/')
    lower=[x.lower() for x in parts]
    uid=m.from_user.id
    try:
        if uid in WAITING and m.chat.type=='private' and uid in ADMINS and not text.startswith('/'):
            action,deadline=WAITING[uid]
            if time.time()>deadline: WAITING.pop(uid,None); raise ValueError('Время ввода истекло.')
            await admin_command(m,action,[action]+parts); return
        if cmd in ('panel','панель','give','take','ban','unban','recheck','cancel'):
            await admin_command(m,cmd,parts); return
        if cmd in ('appeal','оспорить'): await appeal(m); return
        if cmd=='start': await out(m,'<b>Добро пожаловать!</b>\nИгры на виртуальные бублики.',menu()); return
        if cmd in ('help','помощь'): await out(m,HELP,menu()); return
        if cmd in ('games','игры'): await out(m,'<b>Игры</b>',game_menu()); return
        if lower==['топ','б']: await top(m); return
        if cmd in ('б','balance','баланс'):
            with db() as c: amount=balance(c,uid)
            await out(m,mention(uid,m.from_user.full_name)+f'\n<b>Баланс: {fmt(amount)} бубликов</b>'); return
        if cmd in ('bonus','бонус'): await bonus(m); return
        if lower==['лог','игр']:
            with db() as c: rows=c.execute("SELECT data FROM game_v12 WHERE uid=? AND state!='active' ORDER BY created DESC,id DESC LIMIT 15",(uid,)).fetchall()
            lines=[]
            for r in rows:
                g=json.loads(r[0]); lines.append(f'{NAMES[g["kind"]]} · {g["state"]} · ставка {fmt(g["bet"])} · выплата {fmt(g["payout"])}')
            await out(m,'<b>История игр · новые сверху</b>\n'+('\n'.join(esc(x) for x in lines) or 'Пока пусто.')); return
        if cmd in ('continue','продолжить'):
            with db() as c: games=active(c,uid)
            if not games: await out(m,'Текущей игры нет.')
            for g in games:
                body,keys=render(g); await out(m,body,keys)
            return
        aliases={'золото':'gold','gold':'gold','башня':'tower','tower':'tower','хило':'hilo','хл':'hilo','hilo':'hilo','мины':'mines','mines':'mines'}
        if cmd in aliases: await start_game(m,aliases[cmd],lower); return
        if cmd in ('го','лог','ставки','отмена') and len(parts)==1 or re.fullmatch(r'\d{1,10}',cmd) and len(parts)>1:
            lower[0]=cmd; await roulette(m,lower)
    except (ValueError,OverflowError) as e: await out(m,esc(e))

# Verification: Telegram initData authentication plus approximate anti-alt signals.
# Existing policy retained: shared IP OR approximate device match can cause a ban.
# This is not proof of multiple accounts and requires an accessible appeal process.
def validate(raw):
    try:
        if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True); d=dict(pairs)
        if len(d)!=len(pairs): raise ValueError()
        signature=d.pop('hash')
        check='\n'.join(f'{k}={v}' for k,v in sorted(d.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key,check.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
        if not -30<=time.time()-int(d['auth_date'])<=600: raise ValueError()
        u=json.loads(d['user'])
        if not isinstance(u,dict) or type(u.get('id')) is not int or u['id']<=0: raise ValueError()
        return u
    except (ValueError,KeyError,TypeError): raise HTTPException(401,'Закройте и снова откройте приложение.')

def client_ip(req):
    raw=req.client.host if req.client else ''
    if TRUST and req.headers.get('x-forwarded-for'): raw=req.headers['x-forwarded-for'].split(',')[-1].strip()
    try:
        ip=ipaddress.ip_address(raw)
        if getattr(ip,'ipv4_mapped',None): ip=ip.ipv4_mapped
        return str(ip) if ip.is_global else ''
    except ValueError: return ''
def clean(s,n): return ''.join(x for x in s if x.isprintable())[:n] if isinstance(s,str) else ''
def fingerprint(ua,d):
    if not isinstance(d,dict): raise HTTPException(400,'Нет характеристик устройства')
    screen=d.get('screen'); cores=d.get('cores',0); touch=d.get('touch',0); ratio=d.get('ratio',1)
    if not isinstance(screen,list) or len(screen)!=2 or any(type(x) is not int or not 1<=x<=20000 for x in screen): raise HTTPException(400,'Некорректный экран')
    if type(cores) is not int or not 0<=cores<=1024 or type(touch) is not int or not 0<=touch<=100: raise HTTPException(400,'Некорректное устройство')
    if type(ratio) not in (int,float) or not 0<ratio<=20: raise HTTPException(400,'Некорректный масштаб')
    ua=clean(ua,700); model='Модель скрыта'; system='Не определена'; version=''
    if 'Android' in ua:
        system='Android'; r=re.search(r'Android\s+([\d.]+)',ua); version=r.group(1) if r else ''
        r=re.search(r'Android[^;)]*;\s*([^;)]+)',ua)
        if r:
            v=r.group(1).split(' Build/')[0].strip()
            if v not in ('K','wv','Mobile',''): model=v[:80]
    elif 'iPhone' in ua or 'iPad' in ua:
        system='iOS/iPadOS'; model='iPhone' if 'iPhone' in ua else 'iPad'
        r=re.search(r'OS\s+([0-9_]+)',ua); version=r.group(1).replace('_','.') if r else ''
    elif 'Windows' in ua: system='Windows'
    elif 'Macintosh' in ua: system='macOS / возможно iPadOS'
    elif 'Linux' in ua: system='Linux'
    hint=clean(d.get('model'),80)
    if hint: model=hint
    signals={'os':system,'version':version,'model':model,'screen':sorted(screen),'cores':cores,'touch':touch,'ratio':round(ratio,2)}
    fp=hmac.new(SECRET.encode(),json.dumps(signals,sort_keys=True,ensure_ascii=True).encode(),hashlib.sha256).hexdigest()
    return fp,f'{system} {version}; {model}; экран {sorted(screen)}; ядра {cores}; touch {touch}; DPR {ratio}'[:400]

def geo(ip):
    path=os.getenv('GEOIP_DB','')
    if not path: return ''
    try:
        import geoip2.database
        with geoip2.database.Reader(path) as reader:
            r=reader.city(ip)
            return ', '.join(x for x in (r.country.iso_code,r.subdivisions.most_specific.name,r.city.name) if x)[:180]
    except Exception: return ''

def check_user(uid,name,ip,fp,device,region,platform,version):
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
        if old and old['status'] in ('passed','failed'): return old['status'],None
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
        exempt=c.execute('SELECT 1 FROM app_exempt_v6 WHERE uid=?',(uid,)).fetchone()
        match=c.execute("SELECT uid,ip FROM app_checks_v6 WHERE uid<>? AND status='passed' AND ts>? AND (ip=? OR fp=?) ORDER BY id DESC LIMIT 1",(uid,now-90*86400,ip,fp)).fetchone()
        result='failed' if match and not exempt else 'passed'
        reason=(('Совпал IP' if match['ip']==ip else 'Совпали приблизительные характеристики устройства')+f'; другой ID {match["uid"]}') if result=='failed' else 'Исключение администратора' if exempt else 'Совпадений нет'
        c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(result,now,uid))
        rid=c.execute('INSERT INTO app_checks_v6(uid,ts,ip,fp,device,geo,platform,api_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',(uid,now,ip,fp,device,region,platform,version,result,reason)).lastrowid
        c.execute('DELETE FROM app_checks_v6 WHERE ts<?',(now-90*86400,))
        row=dict(c.execute('SELECT * FROM app_checks_v6 WHERE id=?',(rid,)).fetchone())
    return result,row

PAGE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><script src="https://telegram.org/js/telegram-web-app.js"></script><title>Проверка</title><style>body{background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#fff);font:16px system-ui;padding:24px}main{max-width:480px;margin:40px auto}button{width:100%;padding:16px;border:0;border-radius:12px;background:#5674ec;color:white;font-size:17px;margin:12px 0}button:disabled{opacity:.5}#result{white-space:pre-wrap}details{font-size:13px;line-height:1.5}[hidden]{display:none!important}</style></head><body><main><section id="form" hidden><h1>Проверка аккаунта</h1><p>Для проверки используются IP и приблизительные характеристики устройства.</p><details open><summary>Условия и данные</summary><p>Нажимая кнопку, вы соглашаетесь на обработку Telegram ID, имени, IP, характеристик устройства, приблизительного отпечатка и города по IP. Логи хранятся до 90 дней и отправляются администрации в Telegram. Копии этих сообщений автоматически не удаляются. Баланс, профиль и история игр сохраняются до удаления владельцем бота. Имя, ссылка на аккаунт и баланс публикуются в топе.</p><p>Совпадение IP или характеристик с другим аккаунтом приводит к автобану. Общий Wi-Fi, VPN и одинаковые телефоны могут дать ложное совпадение. Это не аппаратный ID, не GPS и не доказательство твинка. Обжалование и запрос удаления данных: /appeal объяснение в личке бота.</p></details><button id="verify">Согласен, пройти проверку</button></section><p id="result">Загрузка…</p><button id="retry" hidden>Повторить загрузку</button><button id="close">Вернуться в бот</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const form=document.getElementById('form'),result=document.getElementById('result'),button=document.getElementById('verify'),retry=document.getElementById('retry');
document.getElementById('close').onclick=()=>tg?.close();
async function api(path,extra={}){if(!tg?.initData)throw Error('Откройте приложение из бота.');const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({init_data:tg.initData,...extra})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'Ошибка сервера');return d;}
function render(s){form.hidden=s!=='pending';result.textContent=s==='passed'?'✅ Проверка пройдена!':s==='failed'?'⛔ Вы забанены. Обжалование: /appeal объяснение':'';retry.hidden=true;}
async function load(){try{render((await api('/api/status')).status)}catch(e){result.textContent=e.message;retry.hidden=false;}}
retry.onclick=load;
button.onclick=async()=>{button.disabled=true;try{let h={};try{h=await navigator.userAgentData?.getHighEntropyValues(['model'])||{}}catch(e){}const device={model:h.model||'',screen:[screen.width,screen.height],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0,ratio:devicePixelRatio};result.textContent='Проверяем…';render((await api('/api/verify',{consent:true,device,platform:tg.platform||'',api_version:tg.version||''})).status)}catch(e){result.textContent=e.message}finally{button.disabled=false}};load();
</script></body></html>'''

@asynccontextmanager
async def lifespan(app):
    global BOT_USERNAME
    init()
    me=await bot.get_me(); BOT_USERNAME=me.username or ''
    await bot.set_webhook(URL+'/telegram/webhook',secret_token=HOOK,allowed_updates=dp.resolve_used_update_types())
    try: yield
    finally: await bot.session.close()

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.get('/',response_class=HTMLResponse)
async def index(): return HTMLResponse(PAGE,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
@app.get('/health')
async def health(): return {'ok':True}
async def read_json(req,limit=32768):
    body=bytearray()
    async for chunk in req.stream():
        body.extend(chunk)
        if len(body)>limit: raise HTTPException(413,'Слишком большой запрос')
    try:
        value=json.loads(body)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (ValueError,UnicodeDecodeError): raise HTTPException(400,'Некорректный JSON')
@app.post('/telegram/webhook')
async def webhook(req:Request):
    if not secrets.compare_digest(req.headers.get('X-Telegram-Bot-Api-Secret-Token',''),HOOK): raise HTTPException(403,'Forbidden')
    await dp.feed_update(bot,Update.model_validate(await read_json(req,1048576),context={'bot':bot}))
    return {'ok':True}
@app.post('/api/status')
async def api_status(req:Request):
    data=await read_json(req); user=validate(data.get('init_data'))
    return JSONResponse({'status':await asyncio.to_thread(status,user['id'])},headers={'Cache-Control':'no-store'})
@app.post('/api/verify')
async def verify(req:Request):
    data=await read_json(req); user=validate(data.get('init_data'))
    old=await asyncio.to_thread(status,user['id'])
    if old in ('passed','failed'): return JSONResponse({'status':old},headers={'Cache-Control':'no-store'})
    if data.get('consent') is not True: raise HTTPException(400,'Нужно согласие с условиями')
    ip=client_ip(req)
    if not ip: raise HTTPException(503,'Не определён публичный IP. Проверьте TRUST_PROXY и настройки доверенного прокси.')
    fp,device=fingerprint(req.headers.get('user-agent',''),data.get('device'))
    region=await asyncio.to_thread(geo,ip)
    name=(' '.join(str(user.get(k,'')) for k in ('first_name','last_name')).strip() or str(user['id']))[:256]
    result,row=await asyncio.to_thread(check_user,user['id'],name,ip,fp,device,region,clean(data.get('platform'),50),clean(data.get('api_version'),30))
    if row: await notify(logtext(row))
    return JSONResponse({'status':result},headers={'Cache-Control':'no-store'})

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),proxy_headers=False)
