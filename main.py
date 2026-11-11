"""Python 3.11+. Virtual currency only; no deposits/withdrawals.
requirements.txt: aiogram>=3.20,<4 fastapi uvicorn geoip2
Start: python main.py
Env: BOT_TOKEN, APP_SECRET (32+ chars), ADMIN_IDS, PUBLIC_URL or RENDER_EXTERNAL_URL.
Optional DB_PATH (persistent disk!), GEOIP_DB (local GeoLite2-City.mmdb).
TRUST_PROXY=1 ONLY when one trusted ingress appends the real client IP to XFF
and direct connections to this application are blocked.
Existing users/balances/app_hilo_v6 are retained. Backup DB before deployment.
Roulette: arbitrary ranges pay floor(35.98 / number_count, 2 decimals),
except 1-18 and 19-36 pay 1.98. Gross payout includes the stake.
Hi-Lo: floor(12.5 / winning_outcomes, 2 decimals); ties lose.
"""
import asyncio, hashlib, hmac, html, ipaddress, json, logging, math, os, re, secrets, sqlite3, time
from contextlib import contextmanager, asynccontextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.exceptions import TelegramBadRequest
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ['BOT_TOKEN']; SECRET=os.environ['APP_SECRET']
if len(SECRET)<32: raise RuntimeError('APP_SECRET минимум 32 символа')
URL=(os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL','')).rstrip('/')
if not URL.startswith('https://'): raise RuntimeError('Нужен HTTPS PUBLIC_URL')
ADMINS={int(x.strip()) for x in os.environ['ADMIN_IDS'].split(',') if x.strip()}
DB=os.getenv('DB_PATH','bot.sqlite3'); GEO=os.getenv('GEOIP_DB','')
TRUST=os.getenv('TRUST_PROXY','0')=='1'
HOOK=hmac.new(SECRET.encode(),b'telegram-webhook',hashlib.sha256).hexdigest()
MAX_BET=10**9; MAX_BAL=9*10**15; MAX_PAY=10**12
bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode='HTML')); dp=Dispatcher()
waiting={}

@contextmanager
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
 try: yield c; c.commit()
 except Exception: c.rollback(); raise
 finally: c.close()

def init():
 Path(DB).parent.mkdir(parents=True,exist_ok=True)
 with db() as c:
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',checked_at INTEGER);
  CREATE TABLE IF NOT EXISTS balances(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
  CREATE TABLE IF NOT EXISTS app_hilo_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,card INTEGER NOT NULL,mult TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,wins INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,UNIQUE(chat,msg));
  CREATE UNIQUE INDEX IF NOT EXISTS app_hilo_one_active_v6 ON app_hilo_v6(uid) WHERE state='active';
  CREATE TABLE IF NOT EXISTS app_checks_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,ip TEXT NOT NULL,fp TEXT NOT NULL,device TEXT NOT NULL,geo TEXT NOT NULL,platform TEXT NOT NULL,api_version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS checks_fp_v9 ON app_checks_v6(fp);
  CREATE INDEX IF NOT EXISTS checks_ip_v9 ON app_checks_v6(ip);
  CREATE TABLE IF NOT EXISTS app_exempt_v6(uid INTEGER PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS app_appeals_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,ts INTEGER NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
  CREATE TABLE IF NOT EXISTS app_audit_v6(id INTEGER PRIMARY KEY AUTOINCREMENT,actor INTEGER,uid INTEGER,ts INTEGER,action TEXT);
  CREATE TABLE IF NOT EXISTS r_round9(id INTEGER PRIMARY KEY,chat INTEGER,thread INTEGER DEFAULT 0,last REAL,state TEXT DEFAULT 'open',number INTEGER,ts INTEGER);
  CREATE UNIQUE INDEX IF NOT EXISTS r_open9 ON r_round9(chat,thread) WHERE state='open';
  CREATE TABLE IF NOT EXISTS r_bet9(id INTEGER PRIMARY KEY,rid INTEGER,uid INTEGER,name TEXT,amount INTEGER,kind TEXT,rate TEXT,state TEXT DEFAULT 'active',payout INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS r_repeat9(source INTEGER,target INTEGER,uid INTEGER,PRIMARY KEY(source,target,uid));
  CREATE TABLE IF NOT EXISTS command9(chat INTEGER,msg INTEGER,PRIMARY KEY(chat,msg));
  ''')

E={'ok':'5206607081334906820','ban':'5240241223632954241','red':'5388647401865097828','green':'5388907487904674520','fire':'6008104758236156926','face':'5994473545650934240','clock':'5258258882022612173','crown':'5807868868886009920','repeat':'5877410604225924969','double':'6026261955818294779','money':'5388988070081082206'}
def esc(s): return html.escape(str(s))
def em(key,fallback): return f'<tg-emoji emoji-id="{E.get(key,key)}">{fallback}</tg-emoji>'
def fmt(n): return f'{int(n):,}'.replace(',',' ')
def num(n):
 s=format(Decimal(n),'f'); return s.rstrip('0').rstrip('.') if '.' in s else s

def b(text,data=None,icon=None,style=None,url=None):
 kw={}
 if data: kw['callback_data']=data
 if icon: kw['icon_custom_emoji_id']=E.get(icon,icon)
 if style: kw['style']=style
 if url: kw['web_app']=WebAppInfo(url=url)
 return InlineKeyboardButton(text=text,**kw)
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def link(uid,name): return f'<a href="tg://user?id={uid}">{esc(name)}</a>'
async def out(m,text,keys=None,edit=False):
 method=m.edit_text if edit else m.answer
 try: await method(text,reply_markup=keys)
 except TelegramBadRequest as e:
  if 'message is not modified' in str(e).lower(): return
  plain=re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>',r'\1',text)
  if keys:
   d=keys.model_dump(exclude_none=True)
   for row in d['inline_keyboard']:
    for item in row: item.pop('style',None); item.pop('icon_custom_emoji_id',None)
   keys=InlineKeyboardMarkup.model_validate(d)
  await method(plain,reply_markup=keys)
async def tell(text,keys=None):
 for uid in ADMINS:
  try: await bot.send_message(uid,text,reply_markup=keys)
  except Exception: logging.warning('Не удалось уведомить администратора %s',uid)

def touch(uid,name):
 with db() as c:
  c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name[:256]))
  c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)',(uid,))
def status(uid):
 with db() as c: r=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
 return r['status'] if r else 'pending'
def allowed(c,uid):
 r=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
 if r and r['status']=='failed': raise ValueError('⛔ Вы забанены. /appeal объяснение')
def balance(c,uid):
 c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)',(uid,))
 return c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()[0]
def change(c,uid,delta):
 value=balance(c,uid)+delta
 if not 0<=value<=MAX_BAL: raise ValueError('Недостаточно бубликов или превышен лимит баланса.')
 c.execute('UPDATE balances SET amount=? WHERE uid=?',(value,uid)); return value
def once(c,m):
 if c.execute('SELECT 1 FROM command9 WHERE chat=? AND msg=?',(m.chat.id,m.message_id)).fetchone(): raise ValueError('Команда уже обработана.')
 c.execute('INSERT INTO command9 VALUES(?,?)',(m.chat.id,m.message_id))
def valid_user(m): return m.from_user and not m.from_user.is_bot and not m.sender_chat
async def gate(m):
 if not valid_user(m): return False
 touch(m.from_user.id,m.from_user.full_name)
 if status(m.from_user.id)=='failed': await out(m,'<b>⛔ Вы забанены. /appeal объяснение</b>'); return False
 return True

def menu(): return kb([[b('Профиль','profile','5771887475421090729'),b('Топ','top','5913384919584741274')],[b('Проверка',icon='5877260593903177342',url=URL)]])
WELCOME='<b>'+em('5447195541345706844','👋')+' Добро пожаловать!\n\n'+em('5888584382553265934','🛡')+' Здесь вы пройдёте проверку на мульти-акк (твинк)</b>'
@dp.message(CommandStart())
async def start(m:Message):
 if m.chat.type!='private': return
 if await gate(m): await out(m,WELCOME,menu())
@dp.callback_query(F.data.in_({'home','profile','top'}))
async def nav(q:CallbackQuery):
 await q.answer(); touch(q.from_user.id,q.from_user.full_name)
 if not isinstance(q.message,Message): return
 if status(q.from_user.id)=='failed': await out(q.message,'<b>⛔ Вы забанены. /appeal объяснение</b>',edit=True); return
 if q.data=='home': await out(q.message,WELCOME,menu(),True); return
 if q.data=='profile':
  labels={'pending':'Не проверен','passed':'Проверка пройдена','failed':'Заблокирован'}
  text='<b>• Профиль</b>\n<blockquote><b>'+em('5771887475421090729','👤')+' Ник: '+esc(q.from_user.full_name)+'\n'+em('5936017305585586269','🆔')+f' ID: {q.from_user.id}\n'+em('5884366771913233289','📋')+' Статус: '+labels[status(q.from_user.id)]+'</b></blockquote>'
 else:
  with db() as c: rows=c.execute("SELECT * FROM users WHERE status='passed' ORDER BY checked_at DESC LIMIT 10").fetchall()
  text='<b>• Топ</b>\n<blockquote><b>'+('\n'.join(em('5879770735999717115','🏆')+' '+link(r['id'],r['name'][:70])+' — проверка пройдена' for r in rows) or 'Пока никого нет.')+'</b></blockquote>'
 await out(q.message,text,kb([[b('Назад','home','5875082500023258804')]]),True)

# Admin panel and moderation.
def admin(m): return valid_user(m) and m.chat.type=='private' and m.from_user.id in ADMINS
async def aq(q):
 ok=q.from_user.id in ADMINS and isinstance(q.message,Message) and q.message.chat.type=='private'
 await q.answer(None if ok else 'Нет доступа'); return ok
@dp.message(Command('panel','панель'))
async def panel(m:Message):
 if not admin(m): return
 waiting.pop(m.from_user.id,None)
 await out(m,'<b>Панель администратора</b>',kb([[b('Логи','adm:logs:0')],[b('Апелляции','adm:appeals:0')],[b('Бан','adm:input:ban'),b('Разбан','adm:input:unban')],[b('Повторная проверка','adm:input:recheck')]]))
def moderate(actor,uid,action,reason=''):
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  c.execute('INSERT OR IGNORE INTO users(id,name) VALUES(?,?)',(uid,f'ID {uid}'))
  c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',({'ban':'failed','unban':'passed','recheck':'pending'}[action],int(time.time()),uid))
  if action=='unban': c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(uid,))
  else: c.execute('DELETE FROM app_exempt_v6 WHERE uid=?',(uid,))
  c.execute("UPDATE app_appeals_v6 SET state='closed' WHERE uid=? AND state='open'",(uid,))
  c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(actor,uid,int(time.time()),action+' '+reason[:1000]))
async def mod_input(m,action,text):
 p=text.split(maxsplit=1)
 if not p or not re.fullmatch(r'[1-9][0-9]{0,14}',p[0]): await out(m,'<b>Укажи числовой ID.</b>'); return False
 uid=int(p[0]); moderate(m.from_user.id,uid,action,p[1] if len(p)>1 else '')
 await out(m,f'<b>Выполнено: {action}, ID {uid}</b>')
 try: await bot.send_message(uid,{'ban':'<b>⛔ Вы забанены. /appeal объяснение</b>','unban':'<b>✅ Бан снят. /start</b>','recheck':'<b>Повторная проверка разрешена. /start</b>'}[action])
 except Exception: pass
 return True
@dp.message(Command('ban','unban','recheck'))
async def modcommand(m:Message):
 if not admin(m): return
 p=m.text.split(maxsplit=1)
 await mod_input(m,p[0].split('@')[0][1:],p[1] if len(p)>1 else '')
@dp.message(Command('cancel'))
async def cancel_admin(m:Message):
 if admin(m): waiting.pop(m.from_user.id,None); await out(m,'<b>Отменено.</b>')
@dp.message(lambda m: admin(m) and m.from_user.id in waiting)
async def wait_admin(m:Message):
 action,deadline=waiting[m.from_user.id]
 if time.time()>deadline: waiting.pop(m.from_user.id,None); return
 if await mod_input(m,action,m.text or ''): waiting.pop(m.from_user.id,None)

def logtext(r):
 stamp=datetime.fromtimestamp(r['ts'],timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')
 return '<b>'+('\n'.join([
 em('ok','✅')+' Прошёл' if r['status']=='passed' else em('ban','⛔')+' Забанен.',
 em('5260293700088511294','📋')+f' Проверка #{r["id"]} {stamp}',
 em('5936017305585586269','🆔')+f' ID: {r["uid"]}',
 em('5775887550262546277','🌐')+' IP: '+esc(r['ip']),
 em('5967591100532134862','📱')+' Устройство/ОС: '+esc(r['device']),
 em('5994378914636500516','⚙️')+' Версия WebApp API: '+esc(r['api_version']),
 'Платформа: '+esc(r['platform']), 'Гео IP: '+esc(r['geo'] or 'не определено'),
 em('5920344347152224466','🔎')+' '+esc(r['reason'])]))+'</b>'
def appealkeys(aid): return kb([[b('Одобрить',f'adm:approve:{aid}'),b('Отклонить',f'adm:reject:{aid}')]])
@dp.callback_query(F.data.startswith('adm:'))
async def admin_actions(q:CallbackQuery):
 if not await aq(q): return
 _,action,raw=q.data.split(':')
 if action=='input':
  if raw in ('ban','unban','recheck'): waiting[q.from_user.id]=(raw,time.time()+300); await out(q.message,'<b>Пришли ID и при необходимости причину. /cancel — отмена.</b>')
  return
 try: n=max(0,int(raw))
 except ValueError: return
 if action=='logs':
  with db() as c: r=c.execute('SELECT * FROM app_checks_v6 ORDER BY id DESC LIMIT 1 OFFSET ?',(n,)).fetchone()
  if r: await out(q.message,logtext(r),kb([[b('←',f'adm:logs:{max(0,n-1)}'),b('→',f'adm:logs:{n+1}')]]))
  else: await out(q.message,'<b>Записей больше нет.</b>')
 elif action=='appeals':
  with db() as c: rows=c.execute("SELECT * FROM app_appeals_v6 WHERE state='open' AND id>? ORDER BY id LIMIT 5",(n,)).fetchall()
  for r in rows: await out(q.message,f'<b>ID {r["uid"]}\n{esc(r["body"])}</b>',appealkeys(r['id']))
  if len(rows)==5: await out(q.message,'<b>Другие заявки</b>',kb([[b('Далее',f'adm:appeals:{rows[-1]["id"]}')]]))
  elif not rows: await out(q.message,'<b>Нет заявок.</b>')
 elif action in ('approve','reject'):
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); r=c.execute("SELECT * FROM app_appeals_v6 WHERE id=? AND state='open'",(n,)).fetchone()
   if r:
    c.execute('UPDATE app_appeals_v6 SET state=? WHERE id=?',(action,n))
    if action=='approve':
     c.execute("UPDATE users SET status='passed',checked_at=? WHERE id=?",(int(time.time()),r['uid']))
     c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(r['uid'],))
    c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(q.from_user.id,r['uid'],int(time.time()),action))
  await q.message.edit_reply_markup(reply_markup=None)
  if r:
   try: await bot.send_message(r['uid'],'<b>Апелляция одобрена. /start</b>' if action=='approve' else '<b>Апелляция отклонена.</b>')
   except Exception: pass
@dp.message(Command('appeal','оспорить'))
async def appeal(m:Message):
 if m.chat.type!='private': return
 body=(m.text or '').partition(' ')[2].strip()
 if not 5<=len(body)<=1500: await out(m,'<b>/appeal объяснение (5–1500 символов)</b>'); return
 uid=m.from_user.id; now=int(time.time())
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); u=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
  r=c.execute('SELECT * FROM app_appeals_v6 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
  if not u or u['status']!='failed': error='Активного бана нет.'
  elif r and (r['state']=='open' or now-r['ts']<86400): error='Апелляция уже подана. Не чаще раза в сутки.'
  else: error=None; aid=c.execute('INSERT INTO app_appeals_v6(uid,ts,body) VALUES(?,?,?)',(uid,now,body)).lastrowid
 if error: await out(m,'<b>'+error+'</b>'); return
 await out(m,'<b>Апелляция сохранена.</b>'); await tell(f'<b>Апелляция ID {uid}\n{esc(body)}</b>',appealkeys(aid))

@dp.message(F.text.regexp(r'(?i)^\s*б\s*$'))
async def bal(m:Message):
 if not await gate(m): return
 with db() as c: value=balance(c,m.from_user.id)
 await out(m,'<b>'+link(m.from_user.id,m.from_user.full_name)+':\n'+em('money','🥯')+' Баланс: '+fmt(value)+' бубликов</b>')
@dp.message(Command('give','take'))
async def give(m:Message):
 if not valid_user(m) or m.from_user.id not in ADMINS: return
 p=m.text.split(); target=None
 if len(p)==2 and m.reply_to_message and valid_user(m.reply_to_message): target=m.reply_to_message.from_user; uid=target.id; raw=p[1]
 elif len(p)==3 and re.fullmatch(r'[1-9][0-9]{0,14}',p[1]): uid=int(p[1]); raw=p[2]
 else: await out(m,'<b>/give ID сумма, /take ID сумма или ответом /give сумма</b>'); return
 if not re.fullmatch(r'[0-9]{1,16}',raw) or not 1<=int(raw)<=MAX_BAL: return
 delta=int(raw)*(1 if p[0].split('@')[0]=='/give' else -1)
 if target: touch(uid,target.full_name)
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); once(c,m); value=change(c,uid,delta)
   c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(m.from_user.id,uid,int(time.time()),f'balance {delta:+} => {value}'))
 except ValueError as e: await out(m,'<b>'+esc(e)+'</b>'); return
 await out(m,f'<b>{em("money","🥯")} ID {uid}\nИзменение: {delta:+}\nБаланс: {fmt(value)}</b>')

# Hi-Lo.
def card(n): return {1:'A',11:'J',12:'Q',13:'K'}.get(n,str(n))
def rate(n,d):
 count=n-1 if d=='low' else 13-n
 return (Decimal('12.5')/count).quantize(Decimal('.01'),rounding=ROUND_DOWN) if count else None
HELP='<b>'+em('5886436057091673541','🎴')+' Хило [ N ]\n'+em('5897846616966041652','🔮')+' Угадать последовательность: Выше | Ниже от Туза (1) До короля(13)</b>'
def gameview(g):
 if g['state']=='paid': return '<b>'+em('ok','✅')+' Успешно</b>\n<blockquote><b>'+em('money','🥯')+' '+fmt(g['payout'])+' бубликов</b></blockquote>',None
 if g['state']!='active': return '<b>'+em('5283283384418707920','❌')+' Неправильно! Выпало: '+card(g['card'])+'</b>',None
 text='<b>'+em('5447327564345416222','🎴')+' Хило (карточная игра)</b>\n<blockquote><b>'+em('5389099885259666198','🃏')+' Карта: '+card(g['card'])+'\n'+em('5447160283959173987','✖️')+' Множитель: x'+num(g['mult'])+'</b></blockquote>'
 row=[]
 for d,label,icon,style in [('low','Ниже','5447490850412074781','danger'),('high','Выше','5449683594425410231','success')]:
  k=rate(g['card'],d)
  if k: row.append(b(label+' x'+num(k),f'hl:{g["id"]}:{g["rev"]}:{d}',icon,style))
 rows=[row]
 if g['wins']: rows.append([b('Забрать выигрыш x'+num(g['mult']),f'hl:{g["id"]}:{g["rev"]}:cash','5447184078077992135','primary')])
 return text,kb(rows)
@dp.message(F.text.regexp(r'(?i)^\s*(хл|хило)(?:\s.*)?$'))
async def hilo(m:Message):
 if not await gate(m): return
 uid=m.from_user.id; p=m.text.split()
 with db() as c: active=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active:
  text,keys=gameview(active); await out(m,text,keys); return
 if len(p)!=2 or not p[1].isdigit() or not 10<=int(p[1])<=MAX_BET: await out(m,HELP); return
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
   g=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
   if not g:
    bet=int(p[1]); change(c,uid,-bet)
    gid=c.execute("INSERT INTO app_hilo_v6(uid,bet,card,mult,chat,msg) VALUES(?,?,?,'1',?,?)",(uid,bet,secrets.randbelow(13)+1,m.chat.id,m.message_id)).lastrowid
    g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
  text,keys=gameview(g); await out(m,text,keys)
 except ValueError as e: await out(m,'<b>'+esc(e)+'</b>')
@dp.callback_query(F.data.startswith('hl:'))
async def hilo_step(q:CallbackQuery):
 try:
  _,gid,rev,action=q.data.split(':'); gid=int(gid); rev=int(rev)
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
   if not g or g['uid']!=q.from_user.id: raise ValueError('Это не твоя игра.')
   if g['state']=='active' and g['rev']==rev:
    if action=='cash':
     if not g['wins']: raise ValueError('Сначала сделай успешный ход.')
     payout=min(MAX_PAY,int(Decimal(g['bet'])*Decimal(g['mult']))); change(c,g['uid'],payout)
     c.execute("UPDATE app_hilo_v6 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
    elif action in ('low','high'):
     k=rate(g['card'],action)
     if k is None: raise ValueError('Невозможный ход.')
     n=secrets.randbelow(13)+1; win=n<g['card'] if action=='low' else n>g['card']
     if win:
      mult=min((Decimal(g['mult'])*k).quantize(Decimal('.000001'),rounding=ROUND_DOWN),Decimal(MAX_PAY)/g['bet'])
      c.execute('UPDATE app_hilo_v6 SET card=?,mult=?,wins=wins+1,rev=rev+1 WHERE id=?',(n,num(mult),gid))
     else: c.execute("UPDATE app_hilo_v6 SET card=?,state='lost',rev=rev+1 WHERE id=?",(n,gid))
    else: raise ValueError('Неизвестное действие.')
   g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
  await q.answer(); text,keys=gameview(g)
  if isinstance(q.message,Message): await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Roulette: round per chat AND forum topic. Transactions serialize bets/settlement.
RED={1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
def color(n): return em('green','🟢') if n==0 else em('red','🔴') if n in RED else '⚫️'
def parsebet(s):
 s=s.lower().replace('ё','е').strip()
 aliases={'к':'red','красный':'red','red':'red','ч':'black','черный':'black','black':'black','з':'0','зеленый':'0','green':'0','чет':'even','четное':'even','even':'even','нечет':'odd','нечетное':'odd','odd':'odd'}
 s=aliases.get(s,s)
 if s in ('red','black','even','odd'): return s,Decimal('1.98')
 if re.fullmatch(r'\d{1,2}',s) and 0<=int(s)<=36: return str(int(s)),Decimal('35.98')
 match=re.fullmatch(r'(\d{1,2})\s*-\s*(\d{1,2})',s)
 if match:
  a,z=map(int,match.groups())
  if 0<=a<z<=36:
   k=Decimal('1.98') if (a,z) in ((1,18),(19,36)) else (Decimal('35.98')/(z-a+1)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
   return f'{a}-{z}',k
 raise ValueError('Тип ставки: к, ч, з, чет, нечет, 0–36 или диапазон, например 9-12.')
def label(kind): return {'red':em('red','🔴')+' RED','black':'⚫️ BLACK','even':'EVEN','odd':'ODD','0':em('green','🟢')+' 0'}.get(kind,esc(kind))
def wins(kind,n):
 if kind=='red': return n in RED
 if kind=='black': return n!=0 and n not in RED
 if kind=='even': return n!=0 and n%2==0
 if kind=='odd': return n%2==1
 if '-' in kind:
  a,z=map(int,kind.split('-')); return a<=n<=z
 return int(kind)==n

def round_open(c,chat,thread):
 r=c.execute("SELECT * FROM r_round9 WHERE chat=? AND thread=? AND state='open'",(chat,thread)).fetchone()
 if not r:
  rid=c.execute('INSERT INTO r_round9(chat,thread,last,ts) VALUES(?,?,?,?)',(chat,thread,0,int(time.time()))).lastrowid
  r=c.execute('SELECT * FROM r_round9 WHERE id=?',(rid,)).fetchone()
 return r

def place(c,r,uid,name,bets):
 allowed(c,uid)
 count=c.execute("SELECT count(*) FROM r_bet9 WHERE rid=? AND state='active'",(r['id'],)).fetchone()[0]
 if count+len(bets)>100: raise ValueError('В раунде максимум 100 ставок.')
 if not bets or any(not 10<=amount<=MAX_BET for amount,kind,k in bets): raise ValueError('Ставка от 10 до '+fmt(MAX_BET))
 change(c,uid,-sum(v[0] for v in bets))
 for amount,kind,k in bets:
  c.execute('INSERT INTO r_bet9(rid,uid,name,amount,kind,rate) VALUES(?,?,?,?,?,?)',(r['id'],uid,name[:100],amount,kind,num(k)))
 c.execute('UPDATE r_round9 SET last=? WHERE id=?',(time.time(),r['id']))
def accepted(bets): return '<b>'+em('fire','🔥')+' Ставки приняты:</b>\n<blockquote><b>'+'\n'.join(em('face','😀')+fmt(a)+' на '+label(k) for a,k,_ in bets)+'</b></blockquote>'
@dp.message(F.text.regexp(r'^\s*\d+\s+\S'))
async def bet_message(m:Message):
 if not await gate(m): return
 try:
  amount,raw=m.text.strip().split(maxsplit=1)
  if len(amount)>10: raise ValueError('Слишком большая ставка.')
  kind,k=parsebet(raw); bets=[(int(amount),kind,k)]
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); once(c,m); r=round_open(c,m.chat.id,m.message_thread_id or 0)
   place(c,r,m.from_user.id,m.from_user.full_name,bets)
  await out(m,accepted(bets))
 except ValueError as e: await out(m,'<b>'+esc(e)+'</b>')

@dp.message(F.text.regexp(r'(?i)^\s*(го|лог|отмена|ставки)\s*$'))
async def roulette(m:Message):
 if not await gate(m): return
 uid=m.from_user.id; cmd=m.text.strip().lower(); thread=m.message_thread_id or 0
 try:
  if cmd=='лог':
   with db() as c: rows=c.execute("SELECT * FROM r_round9 WHERE chat=? AND thread=? AND state='done' ORDER BY id DESC LIMIT 20",(m.chat.id,thread)).fetchall()
   await out(m,'<b>'+em('crown','👑')+' Лог рулетки\n'+('\n'.join(f'#{r["id"]}: {color(r["number"])} {r["number"]}' for r in rows) or 'Пока пусто.')+'</b>'); return
  if cmd=='ставки':
   with db() as c: rows=c.execute("SELECT b.* FROM r_bet9 b JOIN r_round9 r ON r.id=b.rid WHERE r.chat=? AND r.thread=? AND r.state='open' AND b.uid=? AND b.state='active'",(m.chat.id,thread,uid)).fetchall()
   if not rows: await out(m,'<b>Ставок нет.</b>'); return
   for pos in range(0,len(rows),15):
    await out(m,'<b>Ваши ставки</b>\n<blockquote><b>'+'\n'.join(fmt(x['amount'])+' на '+label(x['kind']) for x in rows[pos:pos+15])+'</b></blockquote>')
   return
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
   r=round_open(c,m.chat.id,thread)
   if cmd=='отмена':
    rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND uid=? AND state='active'",(r['id'],uid)).fetchall()
    total=sum(x['amount'] for x in rows)
    if total:
     change(c,uid,total); c.execute("UPDATE r_bet9 SET state='cancelled' WHERE rid=? AND uid=? AND state='active'",(r['id'],uid))
    text='<b>'+em('repeat','🔄')+f' Ставки отменены. Возвращено {fmt(total)} бубликов.</b>'
    results=None
   else:
    rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND state='active'",(r['id'],)).fetchall()
    if not rows: raise ValueError('Ставок нет.')
    wait=math.ceil(13-(time.time()-r['last']))
    if wait>0: raise ValueError(f'⏲ Подождите {wait} секунд…')
    n=secrets.randbelow(37); totals={}; results=[]
    for x in rows:
     payout=int(Decimal(x['amount'])*Decimal(x['rate'])) if wins(x['kind'],n) else 0
     totals[x['uid']]=totals.get(x['uid'],0)+payout
     results.append((dict(x),payout))
    for who,payout in totals.items(): change(c,who,payout)
    for x,payout in results: c.execute("UPDATE r_bet9 SET state='settled',payout=? WHERE id=?",(payout,x['id']))
    c.execute("UPDATE r_round9 SET state='done',number=?,ts=? WHERE id=?",(n,int(time.time()),r['id']))
  if cmd=='отмена': await out(m,text); return
  # Small chunks keep HTML below Telegram's message limit.
  for pos in range(0,len(results),5):
   lines=[]
   for x,payout in results[pos:pos+5]:
    lines.append(link(x['uid'],x['name'])+' ставил '+fmt(x['amount'])+' на '+label(x['kind'])+' — '+('выиграл '+fmt(payout)+' бубликов' if payout else 'проиграл'))
   text='<b>'+em('crown','👑')+f' Выпало: {color(n)} {n}</b>\n<blockquote><b>'+'\n'.join(lines)+'</b></blockquote>'
   keys=kb([[b('Повторить ставку',f'rr:{r["id"]}:1','repeat'),b('Удвоить ставку',f'rr:{r["id"]}:2','double')]]) if pos+5>=len(results) else None
   await out(m,text,keys)
 except ValueError as e:
  text=esc(e)
  if text.startswith('⏲'): text=em('clock','⏲')+text[1:]
  await out(m,'<b>'+text+'</b>')

@dp.callback_query(F.data.startswith('rr:'))
async def repeat_bets(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,source,factor=q.data.split(':'); source=int(source); factor=int(factor)
  if factor not in (1,2): raise ValueError('Ошибка кнопки.')
  touch(q.from_user.id,q.from_user.full_name)
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   old=c.execute("SELECT * FROM r_round9 WHERE id=? AND state='done'",(source,)).fetchone()
   if not old or old['chat']!=q.message.chat.id: raise ValueError('Недоступный раунд.')
   last=c.execute("SELECT id FROM r_round9 WHERE chat=? AND thread=? AND state='done' ORDER BY id DESC LIMIT 1",(old['chat'],old['thread'])).fetchone()
   if last['id']!=source: raise ValueError('Это старый раунд. Используй последнюю кнопку.')
   r=round_open(c,old['chat'],old['thread'])
   if c.execute('SELECT 1 FROM r_repeat9 WHERE source=? AND target=? AND uid=?',(source,r['id'],q.from_user.id)).fetchone(): raise ValueError('Ставки уже повторены в этом раунде.')
   rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND uid=? AND state='settled'",(source,q.from_user.id)).fetchall()
   bets=[(x['amount']*factor,x['kind'],Decimal(x['rate'])) for x in rows]
   if not bets: raise ValueError('У тебя не было ставок в этом раунде.')
   place(c,r,q.from_user.id,q.from_user.full_name,bets)
   c.execute('INSERT INTO r_repeat9 VALUES(?,?,?)',(source,r['id'],q.from_user.id))
  await q.answer('Ставки приняты')
  for pos in range(0,len(bets),15): await out(q.message,accepted(bets[pos:pos+15]))
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Deliberately no catch-all response to unknown messages.
# Verification: signatures are checked on server; fingerprints are approximate.
def validate(raw):
 try:
  if not isinstance(raw,str) or len(raw)>16384: raise ValueError()
  pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True); d=dict(pairs)
  if len(d)!=len(pairs): raise ValueError()
  signature=d.pop('hash'); check='\n'.join(f'{k}={v}' for k,v in sorted(d.items()))
  key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
  if not hmac.compare_digest(hmac.new(key,check.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
  if not -30<=time.time()-int(d['auth_date'])<=600: raise ValueError()
  u=json.loads(d['user'])
  if type(u.get('id')) is not int or u['id']<=0: raise ValueError()
  return u
 except (ValueError,KeyError,TypeError,AttributeError): raise HTTPException(401,'Закрой и снова открой Mini App.')
def client_ip(req):
 raw=req.client.host if req.client else ''
 if TRUST and req.headers.get('x-forwarded-for'): raw=req.headers['x-forwarded-for'].split(',')[-1].strip()
 try:
  ip=ipaddress.ip_address(raw)
  if getattr(ip,'ipv4_mapped',None): ip=ip.ipv4_mapped
  return str(ip) if ip.is_global else ''
 except ValueError: return ''
def clean(s,n): return ''.join(ch for ch in s if ch.isprintable())[:n] if isinstance(s,str) else ''
def fingerprint(ua,d):
 if not isinstance(d,dict): raise HTTPException(400,'Нет характеристик устройства')
 screen=d.get('screen'); cores=d.get('cores',0); touchpoints=d.get('touch',0); ratio=d.get('ratio',1)
 if not isinstance(screen,list) or len(screen)!=2 or any(type(x) is not int or not 1<=x<=20000 for x in screen): raise HTTPException(400,'Некорректный экран')
 if type(cores) is not int or not 0<=cores<=1024 or type(touchpoints) is not int or not 0<=touchpoints<=100: raise HTTPException(400,'Некорректное устройство')
 if type(ratio) not in (int,float) or not 0<ratio<=20: raise HTTPException(400,'Некорректный масштаб')
 ua=clean(ua,700); model='Модель скрыта'; system='Не определена'; version=''
 if 'Android' in ua:
  system='Android'; ver=re.search(r'Android\s+([\d.]+)',ua); version=ver.group(1) if ver else ''
  match=re.search(r'Android[^;)]*;\s*([^;)]+)',ua)
  if match:
   v=match.group(1).split(' Build/')[0].strip()
   if v not in ('K','wv','Mobile',''): model=v[:80]
 elif 'iPhone' in ua or 'iPad' in ua:
  system='iOS/iPadOS'; model='iPhone' if 'iPhone' in ua else 'iPad'
  ver=re.search(r'OS\s+([0-9_]+)',ua); version=ver.group(1).replace('_','.') if ver else ''
 elif 'Windows' in ua: system='Windows'
 elif 'Macintosh' in ua: system='macOS / возможно iPadOS'
 elif 'Linux' in ua: system='Linux'
 hint=clean(d.get('model'),80)
 if hint: model=hint
 # Canonical structure shared with the supplied v6 version.
 signals={'os':system,'version':version,'model':model,'screen':sorted(screen),'cores':cores,'touch':touchpoints,'ratio':round(ratio,2)}
 fp=hmac.new(SECRET.encode(),json.dumps(signals,sort_keys=True,ensure_ascii=True).encode(),hashlib.sha256).hexdigest()
 return fp,f'{system} {version}; {model}; экран {sorted(screen)}; ядра {cores}; touch {touchpoints}; DPR {ratio}'[:400]
def geo(ip):
 if not GEO: return ''
 try:
  import geoip2.database
  with geoip2.database.Reader(GEO) as reader:
   r=reader.city(ip); return ', '.join(v for v in (r.country.iso_code,r.subdivisions.most_specific.name,r.city.name) if v)[:180]
 except Exception: return ''
def check_user(uid,name,ip,fp,device,region,platform,api):
 now=int(time.time())
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); old=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
  if old and old['status'] in ('passed','failed'): return old['status'],None
  c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name))
  exempt=c.execute('SELECT 1 FROM app_exempt_v6 WHERE uid=?',(uid,)).fetchone()
  match=c.execute("SELECT uid,ip FROM app_checks_v6 WHERE uid<>? AND status='passed' AND ts>? AND (ip=? OR fp=?) ORDER BY id DESC LIMIT 1",(uid,now-90*86400,ip,fp)).fetchone()
  result='failed' if match and not exempt else 'passed'
  reason=('Совпал IP' if match and match['ip']==ip else 'Совпали приблизительные характеристики устройства')+f'; другой ID {match["uid"]}' if result=='failed' else ('Исключение администратора' if exempt else 'Совпадений нет')
  c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(result,now,uid))
  rid=c.execute('INSERT INTO app_checks_v6(uid,ts,ip,fp,device,geo,platform,api_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',(uid,now,ip,fp,device,region,platform,api,result,reason)).lastrowid
  c.execute('DELETE FROM app_checks_v6 WHERE ts<?',(now-90*86400,))
  row=dict(c.execute('SELECT * FROM app_checks_v6 WHERE id=?',(rid,)).fetchone())
 return result,row

PAGE=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><script src="https://telegram.org/js/telegram-web-app.js"></script><title>Проверка</title><style>body{margin:0;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#fff);font:16px system-ui;padding:24px}main{max-width:480px;margin:60px auto}h1{font-size:25px}button{width:100%;padding:17px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:white;font-size:17px;font-weight:bold;margin-top:20px}button:disabled{opacity:.5}#result{font-size:22px;font-weight:700;white-space:pre-wrap}details{font-size:12px;margin-top:20px;opacity:.8}[hidden]{display:none!important}</style></head><body><main><section id="form" hidden><h1>🧹 Пройти проверку на твинк.</h1><button id="verify">Пройти проверку</button><details><summary>Данные и условия проверки</summary><p>Нажимая кнопку, вы соглашаетесь на обработку Telegram ID, имени, IP, характеристик устройства, приблизительного отпечатка и города по IP. Логи хранятся до 90 дней, доступны администрации и отправляются ей в Telegram. Копии сообщений у администраторов автоматически не удаляются. Профиль, баланс, история игр и блокировки сохраняются до удаления владельцем. Имя и ссылка прошедшего проверку публикуются в топе.</p><p>Совпадение IP или характеристик с другим прошедшим аккаунтом приводит к автобану. Общий Wi-Fi/VPN и одинаковые телефоны могут давать ложные совпадения. Это не аппаратный ID, не точная модель и не GPS. Обжалование и запрос удаления: /appeal объяснение.</p></details></section><div id="result">Загрузка…</div><button id="retry" hidden>Повторить загрузку</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const f=document.getElementById('form'),r=document.getElementById('result'),b=document.getElementById('verify'),retry=document.getElementById('retry');
async function api(path,extra={}){if(!tg?.initData)throw Error('Открой приложение из бота.');const x=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({init_data:tg.initData,...extra})});const d=await x.json();if(!x.ok)throw Error(d.detail||'Ошибка сервера');return d;}
function render(s){f.hidden=s!=='pending';r.textContent=s==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':s==='failed'?'⛔️ ВЫ ЗАБАНЕНЫ.':'';retry.hidden=true;}
async function load(){f.hidden=true;retry.hidden=true;try{render((await api('/api/status')).status)}catch(e){r.textContent=e.message;retry.hidden=false;}}
retry.onclick=load;
b.onclick=async()=>{b.disabled=true;try{let h={};try{h=await navigator.userAgentData?.getHighEntropyValues(['model'])||{}}catch(e){}const device={model:h.model||'',screen:[screen.width,screen.height],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0,ratio:devicePixelRatio};r.textContent='Проверяем…';render((await api('/api/verify',{consent:true,device,platform:tg.platform||'',api_version:tg.version||''})).status);}catch(e){r.textContent=e.message;}finally{b.disabled=false;}};load();
</script></body></html>'''

@asynccontextmanager
async def lifespan(app):
 init()
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
  d=json.loads(body)
  if not isinstance(d,dict): raise ValueError()
  return d
 except (ValueError,UnicodeDecodeError): raise HTTPException(400,'Некорректный JSON')
@app.post('/telegram/webhook')
async def webhook(req:Request):
 if not secrets.compare_digest(req.headers.get('X-Telegram-Bot-Api-Secret-Token',''),HOOK): raise HTTPException(403,'Forbidden')
 await dp.feed_update(bot,Update.model_validate(await read_json(req,1048576),context={'bot':bot}))
 return {'ok':True}
@app.post('/api/status')
async def api_status(req:Request):
 d=await read_json(req); u=validate(d.get('init_data'))
 return JSONResponse({'status':await asyncio.to_thread(status,u['id'])},headers={'Cache-Control':'no-store'})
@app.post('/api/verify')
async def verify(req:Request):
 d=await read_json(req); u=validate(d.get('init_data')); old=await asyncio.to_thread(status,u['id'])
 if old in ('passed','failed'): return {'status':old}
 if d.get('consent') is not True: raise HTTPException(400,'Нужно подтверждение условий')
 ip=client_ip(req)
 if not ip: raise HTTPException(503,'Сервер не определил публичный IP. Проверь настройки доверенного прокси.')
 fp,device=fingerprint(req.headers.get('user-agent',''),d.get('device')); region=await asyncio.to_thread(geo,ip)
 name=(' '.join(str(u.get(k,'')) for k in ('first_name','last_name')).strip() or str(u['id']))[:256]
 result,row=await asyncio.to_thread(check_user,u['id'],name,ip,fp,device,region,clean(d.get('platform'),50),clean(d.get('api_version'),30))
 if row: await tell(logtext(row))
 return JSONResponse({'status':result},headers={'Cache-Control':'no-store'})
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),proxy_headers=False)
