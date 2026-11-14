"""Complete bot, Python 3.11+. Virtual currency only.
Requirements: aiogram>=3.20,<4 fastapi uvicorn geoip2
Run: python main.py
Env: BOT_TOKEN, APP_SECRET (32+ chars), ADMIN_IDS,
PUBLIC_URL or RENDER_EXTERNAL_URL. Optional DB_PATH, GEOIP_DB, TRUST_PROXY.
Use persistent SQLite and back up DB. Existing table names are retained.
Bonus: 5000 every 43200 seconds; eat cooldown: 14400 seconds.
TRUST_PROXY=1 only for ONE trusted ingress appending actual IP to XFF,
with direct access to application blocked. Otherwise leave 0.
"""
import asyncio, hashlib, hmac, html, ipaddress, json, logging, math, os, re, secrets, sqlite3, time
from contextlib import contextmanager, asynccontextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile, ReplyParameters
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ['BOT_TOKEN'].strip(); SECRET=os.environ['APP_SECRET']
if len(SECRET)<32: raise RuntimeError('APP_SECRET: минимум 32 символа')
URL=(os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL','')).rstrip('/')
if not URL.startswith('https://'): raise RuntimeError('Нужен HTTPS PUBLIC_URL')
ADMINS={int(x.strip()) for x in os.environ['ADMIN_IDS'].split(',') if x.strip()}
DB=os.getenv('DB_PATH','bot.sqlite3'); GEO=os.getenv('GEOIP_DB',''); TRUST=os.getenv('TRUST_PROXY','0')=='1'
HOOK=hmac.new(SECRET.encode(),b'telegram-webhook',hashlib.sha256).hexdigest()
MAX_BET=10**9; MAX_BAL=9*10**15; MAX_PAY=10**12
BONUS_AMOUNT=5000; BONUS_SECONDS=43200; EAT_SECONDS=14400
bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode='HTML')); dp=Dispatcher(); waiting={}; BOT_USERNAME=''

@contextmanager
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
 try:
  yield c
  c.commit()
 except Exception:
  c.rollback(); raise
 finally: c.close()

E={'ok':'5206607081334906820','ban':'5240241223632954241','red':'5388647401865097828','green':'5388907487904674520','fire':'6008104758236156926','face':'5994473545650934240','clock':'5386367538735104399','crown':'5807868868886009920','repeat':'5877410604225924969','double':'6026261955818294779','money':'5238132025323444613','error':'5447270999626131126','limit':'5449395513789031846','none':'5192966772008966700','mine_start':'5469654973308476699','mine':'5375445874988036618','flag':'5447160283959173987','bulb':'5447175024286934438','camera':'5884290437459480896','chat':'5985630530111020079','question':'5447590953214846419','moon':'5802946896494334636','diamond':'5447415357771916752','chart':'5447233732194903146','gift':'6012618640015234346','box':'5258134813302332906','explode':'5276032951342088188','lose_mine':'5888974760720732797','check':'5877260593903177342','help':'5888584382553265934','clip':'5447376801850500535','search':'5449862149100840463','wheel':'6334498573102613995','down':'5406745015365943482','gear':'5341715473882955310','pin':'5796440171364749940','bonusgift':'5985472565508838112','map':'5883955653348692941','notice':'5775887550262546277','bonus':'5963213811597970978','paper':'5877597667231534929','shop':'5983399041197675256','key':'5330115548900501467','trident':'5388787654022148728','tower':'5388571110361020010','donut':'5211042502651432248','hashtag':'5269493627410731513','bomb_anim':'5303112793157810087','dynamite':'5469913852462242978','top_title':'5317035505173430594','top1':'5235776368905562305','top2':'5237704680372447424','top3':'5238044171767393675','your_place':'5449758837957502787','food':'5217726154663796197','smile':'5447271008216063923','comet':'5224607267797606837','yum':'5390923103171784225','bookmark':'5222444124698853913','sun':'5402477260982731644','cherries':'5388988070081082206','cake':'5265043762119199599','weather':'5399913388845322366','trend':'5449683594425410231','downtrend':'5447183459602669338','candy':'5388647101217387586','ladder_crown':'5433758796289685818'}
ICON_FALLBACK={
 E['question']:'❓',E['diamond']:'💎',E['mine']:'💣',E['explode']:'💥',
 E['green']:'🟩',E['red']:'🟥',E['flag']:'🚩',E['crown']:'👑',
 E['tower']:'🏯',E['top1']:'🥇',E['top2']:'🥈',E['top3']:'🥉',
 E['cherries']:'🍒',E['paper']:'📄',E['chart']:'📈',E['ok']:'✔️',
 E['ban']:'🚫',E['gear']:'⚙️',E['shop']:'🏪',E['money']:'💰'
}
INVISIBLE='ㅤㅤㅤㅤ '
def esc(s): return html.escape(str(s))
def em(key,fallback): return f'<tg-emoji emoji-id="{E.get(key,key)}">{fallback}</tg-emoji>'
def fmt(n): return f'{int(n):,}'.replace(',',' ')
def two(n): return Decimal(str(n)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
def num(n): return format(two(n),'f').rstrip('0').rstrip('.')
def error_text(s): return '<b>'+em('error','❗')+' '+esc(s)+'</b>'
def link(uid,name): return f'<a href="tg://user?id={uid}">{esc(name)}</a>'
def b(text,data=None,icon=None,style=None,url=None):
 kw={}
 if data: kw['callback_data']=data
 if icon: kw['icon_custom_emoji_id']=E.get(icon,icon)
 if style: kw['style']=style
 if url: kw['web_app']=WebAppInfo(url=url)
 return InlineKeyboardButton(text=text,**kw)
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def visible_len(text):
 return len(html.unescape(re.sub(r'<[^>]*>','',text)).encode('utf-16-le'))//2

def plain_markup(keys):
 if not keys: return None
 d=keys.model_dump(exclude_none=True)
 for row in d['inline_keyboard']:
  for item in row:
   item.pop('style',None); icon=item.pop('icon_custom_emoji_id',None)
   if item.get('text')==INVISIBLE: item['text']=ICON_FALLBACK.get(icon,'•')
 return InlineKeyboardMarkup.model_validate(d)
async def out(m,text,keys=None,edit=False):
 async def send(body,markup,reply=True):
  for attempt in range(4):
   try:
    if edit:
     return await m.edit_text(body,reply_markup=markup)
    kwargs={'chat_id':m.chat.id,'text':body,'reply_markup':markup}
    if getattr(m,'message_thread_id',None): kwargs['message_thread_id']=m.message_thread_id
    if reply: kwargs['reply_parameters']=ReplyParameters(message_id=m.message_id)
    return await bot.send_message(**kwargs)
   except TelegramRetryAfter as exc:
    if attempt==3: raise
    await asyncio.sleep(exc.retry_after+0.2)
 try:
  return await send(text,keys,not edit)
 except TelegramBadRequest as exc:
  low=str(exc).lower()
  if 'message is not modified' in low: return
  # A deleted/unavailable source message should not prevent the answer. Keep its topic.
  if not edit and any(x in low for x in ('reply message not found','message to be replied not found','replied message not found','message_id_invalid')):
   try: return await send(text,keys,False)
   except TelegramBadRequest as fallback_exc:
    if 'message is not modified' in str(fallback_exc).lower(): return
    exc=fallback_exc
  # First keep the original formatted text and only downgrade the keyboard.
  try: return await send(text,plain_markup(keys),not edit)
  except TelegramBadRequest as retry:
   if 'message is not modified' in str(retry).lower(): return
   if not edit and any(x in str(retry).lower() for x in ('reply message not found','message to be replied not found','replied message not found','message_id_invalid')):
    try: return await send(text,plain_markup(keys),False)
    except TelegramBadRequest as retry2: retry=retry2
   plain=re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>',r'\1',text)
   try: return await send(plain,plain_markup(keys),not edit)
   except TelegramBadRequest as final:
    if 'message is not modified' in str(final).lower(): return
    if not edit and any(x in str(final).lower() for x in ('reply message not found','message to be replied not found','replied message not found','message_id_invalid')):
     return await send(plain,plain_markup(keys),False)
    raise

async def notify(text,keys=None):
 for uid in admin_ids():
  try: await bot.send_message(uid,text,reply_markup=keys)
  except TelegramBadRequest:
   try: await bot.send_message(uid,re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>',r'\1',text),reply_markup=plain_markup(keys))
   except Exception: logging.warning('Не удалось уведомить администратора %s',uid)
  except Exception: logging.warning('Не удалось уведомить администратора %s',uid)

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
  CREATE INDEX IF NOT EXISTS bets_round_v10 ON r_bet9(rid,state,uid);
  CREATE TABLE IF NOT EXISTS r_repeat9(source INTEGER,target INTEGER,uid INTEGER,PRIMARY KEY(source,target,uid));
  CREATE TABLE IF NOT EXISTS command9(chat INTEGER,msg INTEGER,PRIMARY KEY(chat,msg));
  CREATE TABLE IF NOT EXISTS mines_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,mine_count INTEGER NOT NULL CHECK(mine_count BETWEEN 1 AND 24),mine_mask INTEGER NOT NULL,opened_mask INTEGER NOT NULL DEFAULT 0,hit INTEGER,state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,cents INTEGER NOT NULL DEFAULT 100,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,created INTEGER NOT NULL,UNIQUE(chat,msg));
  CREATE UNIQUE INDEX IF NOT EXISTS mines_active_v1 ON mines_v1(uid) WHERE state='active';
  CREATE TABLE IF NOT EXISTS daily_bonus_v1(uid INTEGER PRIMARY KEY,last_claim INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS eat_v1(uid INTEGER PRIMARY KEY,last_eat INTEGER NOT NULL DEFAULT 0,total_grams INTEGER NOT NULL DEFAULT 0,total_coins INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS eat_cooldown_v1(uid INTEGER PRIMARY KEY,last_eat INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS bublins_v1(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
  CREATE TABLE IF NOT EXISTS relationship_v1(uid1 INTEGER NOT NULL,uid2 INTEGER NOT NULL,hp INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(uid1,uid2));

  CREATE TABLE IF NOT EXISTS rake_v1(uid INTEGER PRIMARY KEY,amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0));
  CREATE TABLE IF NOT EXISTS bot_admins_v1(uid INTEGER PRIMARY KEY,added_by INTEGER NOT NULL DEFAULT 0,added_at INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS bot_admin_revoked_v1(uid INTEGER PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS gold_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,step INTEGER NOT NULL DEFAULT 0,traps TEXT NOT NULL,seed TEXT NOT NULL,hash TEXT NOT NULL,path TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,created INTEGER NOT NULL,UNIQUE(chat,msg));
  CREATE UNIQUE INDEX IF NOT EXISTS gold_active_v1 ON gold_v1(uid) WHERE state='active';
  CREATE TABLE IF NOT EXISTS tower_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,mine_count INTEGER NOT NULL CHECK(mine_count BETWEEN 1 AND 4),floor INTEGER NOT NULL DEFAULT 0,mine_masks TEXT NOT NULL,path TEXT NOT NULL DEFAULT '[]',hit_floor INTEGER,hit_cell INTEGER,state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,created INTEGER NOT NULL,UNIQUE(chat,msg));
  CREATE UNIQUE INDEX IF NOT EXISTS tower_active_v1 ON tower_v1(uid) WHERE state='active';
  CREATE TABLE IF NOT EXISTS ladder_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,bet INTEGER NOT NULL,floor INTEGER NOT NULL DEFAULT 0,mines TEXT NOT NULL,hit_row INTEGER,hit_cell INTEGER,state TEXT NOT NULL DEFAULT 'active',rev INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,chat INTEGER NOT NULL,msg INTEGER NOT NULL,created INTEGER NOT NULL,UNIQUE(chat,msg));
  CREATE UNIQUE INDEX IF NOT EXISTS ladder_active_v1 ON ladder_v1(uid) WHERE state='active';
  CREATE INDEX IF NOT EXISTS balance_top_v1 ON balances(amount DESC,uid);
  ''')
  for admin_id in ADMINS:
   c.execute('INSERT OR IGNORE INTO bot_admins_v1(uid,added_by,added_at) VALUES(?,?,?)',(admin_id,admin_id,int(time.time())))
  for r in c.execute("SELECT id,mult FROM app_hilo_v6 WHERE state='active'").fetchall():
   c.execute('UPDATE app_hilo_v6 SET mult=? WHERE id=?',(num(r['mult']),r['id']))

def admin_ids():
 with db() as c: return {r['uid'] for r in c.execute('SELECT uid FROM bot_admins_v1 WHERE uid NOT IN (SELECT uid FROM bot_admin_revoked_v1)')}
def is_admin(uid):
 with db() as c: return c.execute('SELECT 1 FROM bot_admins_v1 WHERE uid=? AND uid NOT IN (SELECT uid FROM bot_admin_revoked_v1)',(uid,)).fetchone() is not None
def touch(uid,name):
 with db() as c:
  c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(uid,name[:256]))
  c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)',(uid,))
def status(uid):
 with db() as c: row=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
 return row['status'] if row else 'pending'
def allowed(c,uid):
 row=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
 if not row or row['status']=='pending': raise ValueError('Сначала пройдите проверку!')
 if row['status']!='passed': raise ValueError('Вы забанены. /appeal объяснение')
def balance(c,uid):
 c.execute('INSERT OR IGNORE INTO balances(uid) VALUES(?)',(uid,))
 return c.execute('SELECT amount FROM balances WHERE uid=?',(uid,)).fetchone()[0]
def change(c,uid,delta):
 value=balance(c,uid)+delta
 if value<0: raise ValueError('Недостаточно средств')
 if value>MAX_BAL: raise ValueError('Превышен лимит баланса. Обратись к администратору.')
 c.execute('UPDATE balances SET amount=? WHERE uid=?',(value,uid)); return value

def active_game(uid):
 with db() as c:
  for table,key in [('app_hilo_v6','hilo'),('mines_v1','mines'),('gold_v1','gold'),('tower_v1','tower'),('ladder_v1','ladder')]:
   row=c.execute(f"SELECT * FROM {table} WHERE uid=? AND state='active'",(uid,)).fetchone()
   if row: return key,dict(row)
 return None,None

def active_game_notice(uid):
 key,g=active_game(uid)
 if not key: return None,None
 names={'hilo':'Хило','mines':'Мины','gold':'Золото','tower':'Башня','ladder':'Лесенка'}
 return '<b>'+em('notice','❗')+' У вас уже есть текущая игра: '+names[key]+'. Продолжите её или заберите выигрыш.</b>',kb([[b('Продолжить',f'resume:{key}:{g["id"]}','repeat')]])

def once(c,m):
 if c.execute('SELECT 1 FROM command9 WHERE chat=? AND msg=?',(m.chat.id,m.message_id)).fetchone(): raise ValueError('Команда уже обработана.')
 c.execute('INSERT INTO command9 VALUES(?,?)',(m.chat.id,m.message_id))
def valid_user(m): return bool(m.from_user and not m.from_user.is_bot and not m.sender_chat)

CHECK_TEXT='<b>'+em('check','🛡')+' Сначала пройдите проверку!</b>'
BLOCKED='<b>'+em('ban','⛔')+' Вы забанены.</b>\n<blockquote><b>Оспорить блокировку:</b> <code>/appeal объяснение</code></blockquote>'
def verify_keyboard(private=True):
 if private: return kb([[b('Пройти проверку',icon='check',url=URL)]])
 return kb([[InlineKeyboardButton(text='Пройти проверку',url=f'https://t.me/{BOT_USERNAME}?start=verify')]]) if BOT_USERNAME else None

class VerificationMiddleware(BaseMiddleware):
 async def __call__(self,handler,event,data):
  user=event.from_user
  if not user or user.is_bot: return
  if isinstance(event,Message) and event.sender_chat: return
  touch(user.id,user.full_name); s=status(user.id)
  private=(isinstance(event,Message) and event.chat.type=='private') or (isinstance(event,CallbackQuery) and isinstance(event.message,Message) and event.message.chat.type=='private')
  cmd=''
  if isinstance(event,Message) and event.text:
   p=event.text.split(maxsplit=1); cmd=p[0].split('@')[0].lower() if p else ''
  appeal_exception=private and s=='failed' and cmd in ('/appeal','/оспорить')
  admin_exception=private and is_admin(user.id) and (cmd in ('/panel','/панель','/ban','/unban','/recheck','/cancel','+админ','-админ') or isinstance(event,Message) and user.id in waiting or isinstance(event,CallbackQuery) and (event.data or '').startswith('adm:'))
  if s=='passed' or appeal_exception or admin_exception: return await handler(event,data)
  message=event if isinstance(event,Message) else event.message
  if isinstance(event,CallbackQuery): await event.answer('Сначала пройдите проверку!' if s=='pending' else 'Вы забанены',show_alert=True)
  if not isinstance(message,Message): return
  if s=='pending': await out(message,CHECK_TEXT,verify_keyboard(private))
  else: await out(message,BLOCKED)
dp.message.middleware(VerificationMiddleware()); dp.callback_query.middleware(VerificationMiddleware())

WELCOME='<b>'+em('5447195541345706844','👋')+' Добро пожаловать!\n\n'+em('5888584382553265934','🛡')+' Здесь вы пройдёте проверку на мульти-акк (твинк)</b>'
def menu(private=True):
 rows=[[b('Профиль','profile','5771887475421090729'),b('Топ','top','5913384919584741274')],[b('Игры','games:menu','crown'),b('Помощь','help:menu','clip')]]
 if private: rows.append([b('Проверка',icon='check',url=URL)])
 return kb(rows)
@dp.message(CommandStart())
async def start(m:Message): await out(m,WELCOME,menu(m.chat.type=='private'))
@dp.callback_query(F.data.in_({'home','profile','top'}))
async def navigation(q:CallbackQuery):
 await q.answer()
 if not isinstance(q.message,Message): return
 if q.data=='home': await out(q.message,WELCOME,menu(q.message.chat.type=='private'),True); return
 if q.data=='profile':
  name='@'+q.from_user.username if q.from_user.username else q.from_user.full_name
  text=profile_text(q.from_user.id,name)
 else:
  text=top_rich_text(q.from_user.id)
 await out(q.message,text,kb([[b('Назад','home')]]),True)



def user_game_stats(uid):
 with db() as c:
  tables=[('app_hilo_v6','bet','payout','state'),('mines_v1','bet','payout','state'),('gold_v1','bet','payout','state'),('tower_v1','bet','payout','state'),('ladder_v1','bet','payout','state')]
  bet_total=won=lost=games=wins_count=loss_count=0
  for table,betcol,paycol,statecol in tables:
   rows=c.execute(f"SELECT {betcol} bet,{paycol} payout,{statecol} state FROM {table} WHERE uid=? AND state IN ('paid','lost')",(uid,)).fetchall()
   for r in rows:
    games+=1; bet_total+=r['bet']; won+=r['payout']
    if r['state']=='lost': lost+=r['bet']; loss_count+=1
    else: wins_count+=1
  rr=c.execute("SELECT count(*) n,coalesce(sum(amount),0) bets,coalesce(sum(payout),0) payouts FROM r_bet9 WHERE uid=? AND state='settled'",(uid,)).fetchone()
  games+=rr['n']; bet_total+=rr['bets']; won+=rr['payouts']
  loss_count+=c.execute("SELECT count(*) FROM r_bet9 WHERE uid=? AND state='settled' AND payout=0",(uid,)).fetchone()[0]
  wins_count+=c.execute("SELECT count(*) FROM r_bet9 WHERE uid=? AND state='settled' AND payout>0",(uid,)).fetchone()[0]
  lost+=c.execute("SELECT coalesce(sum(amount),0) FROM r_bet9 WHERE uid=? AND state='settled' AND payout=0",(uid,)).fetchone()[0]
  return bet_total,won,lost,games,wins_count,loss_count

def profile_text(uid,name):
 bet_total,won,lost,games,wins_count,loss_count=user_game_stats(uid)
 with db() as c:
  bal=balance(c,uid)
  food=c.execute('SELECT total_grams,total_coins FROM eat_v1 WHERE uid=?',(uid,)).fetchone()
  wallet=c.execute('SELECT amount FROM bublins_v1 WHERE uid=?',(uid,)).fetchone()
  coins=wallet['amount'] if wallet else 0
  grams=food['total_grams'] if food else 0
 return ('<b>👤 Профиль: '+link(uid,name)+'</b>\n\n'
         +'<b>'+em('money','💸')+' Сумма ставок: '+fmt(bet_total)+' бубликов\n'
         +em('trend','📈')+' Выиграно: '+fmt(won)+' бубликов\n'
         +em('downtrend','📉')+' Проиграно: '+fmt(lost)+' бубликов\n\n'
         +em('bookmark','🔖')+' Всего сыграно: '+fmt(games)+' игр\n'
         +em('sun','☀️')+' Выиграно: '+fmt(wins_count)+' игр\n'
         +em('weather','🌧')+' Проиграно: '+fmt(loss_count)+' игр\n\n'
         +em('cake','🎂')+' Съедено в кафешке: '+format(grams/1000,'.2f').replace('.',',')+'кг\n\n'
         +em('cherries','🍒')+' Баланс: '+fmt(bal)+' бубликов\n'
         +em('donut','🍩')+' Бублин-коины: '+fmt(coins)+' коинов</b>')

def top_rich_text(uid):
 with db() as c:
  rows=c.execute("SELECT u.id,u.name,b.amount FROM users u JOIN balances b ON b.uid=u.id WHERE u.status='passed' ORDER BY b.amount DESC,u.id ASC LIMIT 3").fetchall()
  mine=balance(c,uid)
  rank=c.execute("SELECT count(*)+1 FROM balances b JOIN users u ON u.id=b.uid WHERE u.status='passed' AND (b.amount>? OR (b.amount=? AND b.uid<?))",(mine,mine,uid)).fetchone()[0]
 medals=('top1','top2','top3')
 lines=[]
 for i,r in enumerate(rows):
  lines.append(em(medals[i],('🥇','🥈','🥉')[i])+' '+link(r['id'],r['name'][:55])+' — <b>'+fmt(r['amount'])+'</b> '+em('cherries','🍒'))
 text='<b>'+em('top_title','🏆')+' ТОП БАЛАНСА</b>'
 if lines:
  text+='\n\n<blockquote>'+'\n'.join(lines)+'</blockquote>'
 text+='\n\n<b>'+em('your_place','📍')+' Ваше место: '+fmt(rank)+'\n'+em('cherries','🍒')+' Ваш баланс: '+fmt(mine)+'</b>'
 return text

# Rake bonus: 5% of finally lost wagers.
RAKE_RATE=Decimal('0.05')
def add_rake(c,uid,lost_amount):
 amount=int(Decimal(max(0,lost_amount))*RAKE_RATE)
 if amount:
  c.execute('INSERT INTO rake_v1(uid,amount) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET amount=amount+excluded.amount',(uid,amount))
 return amount
def rake_view(uid):
 with db() as c:
  row=c.execute('SELECT amount FROM rake_v1 WHERE uid=?',(uid,)).fetchone()
 amount=row['amount'] if row else 0
 text='<b>'+em('bonus','🎁')+' Рейк-бонус\n\n'+em('money','🥯')+' Доступно: '+fmt(amount)+' бубликов\n'+em('face','🥃')+' Вы получаете 5% с окончательно проигранных бубликов</b>'
 key=b('Забрать' if amount else 'Нету','rake:claim' if amount else 'rake:none','bonusgift' if amount else 'none','success' if amount else None)
 return text,kb([[key]])
@dp.message(F.text.regexp(r'(?i)^\s*рейк\s*$'))
async def rake_command(m:Message):
 text,keys=rake_view(m.from_user.id); await out(m,text,keys)
@dp.callback_query(F.data.startswith('rake:'))
async def rake_callback(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 if q.data=='rake:none':
  await q.answer('Пока нет доступного рейк-бонуса.',show_alert=True); return
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE')
   row=c.execute('SELECT amount FROM rake_v1 WHERE uid=?',(q.from_user.id,)).fetchone()
   amount=row['amount'] if row else 0
   if amount<=0:
    await q.answer('Нет доступного рейк-бонуса.',show_alert=True); return
   change(c,q.from_user.id,amount)
   c.execute('UPDATE rake_v1 SET amount=0 WHERE uid=?',(q.from_user.id,))
   c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(q.from_user.id,q.from_user.id,int(time.time()),f'rake_claim +{amount}'))
  await q.answer('Вы забрали '+fmt(amount)+' бубликов.',show_alert=True)
  text,keys=rake_view(q.from_user.id); await out(q.message,text,keys,True)
 except ValueError as e: await q.answer(str(e),show_alert=True)

# Help: requested compact layout, without admin commands.
def help_text(uid=None):
 return '<b>'+em('clip','📎')+' Помощь</b>\n\n<blockquote>'+em('check','⚙')+' <code>/start</code><b> — запустить бота/главное меню</b>\n'+em('chat','💬')+' <code>б</code><b> — отобразить баланс</b>\n'+em('notice','❗️')+' <code>помощь</code><b> / </b><code>игры</code><b> — навигация</b>\n'+em('pin','📌')+' <code>/appeal</code><b> — апелляция на бан в боте</b>\n'+em('bonusgift','🎁')+' <code>бонус</code><b> — получить доп. валюту (12 часов)</b>\n'+em('cake','🎂')+' <code>похавать</code><b> — получить бублин-коины (4 часа)</b>\n'+em('clock','⌛')+' <code>кд</code><b> — посмотреть кулдауны</b>\n'+em('bonus','🎁')+' <code>рейк</code><b> — рейк-бонус</b>\n'+em('food','🍔')+' <code>похавать вместе</code><b> — ответом на сообщение, HP за бублин-коины</b>\n'+em('map','🗺')+' <code>топ б</code><b> — топ-3 богачей</b>\n'+em('flag','🚩')+' <code>лесенка [сумма]</code><b> — игра на 8 рядов</b>\n'+em('money','💸')+' <code>Н [сумма]</code><b> / </b><code>П [сумма]</code><b> — передать бублики ответом на сообщение</b></blockquote>'
def games_keyboard(): return kb([[b('Хило','games:hilo','5447327564345416222')],[b('Мины','games:mines','mine_start')],[b('Золото','games:gold','trident')],[b('Башня','games:tower','tower')],[b('Рулетка','games:roulette','wheel')],[b('Лесенка','games:ladder','flag')],[b('Главное меню','home','5875082500023258804')]])
GAME_RULES={
'hilo':'<b>'+em('5447327564345416222','🎴')+' Хило | Help game</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>хило [ставка]</code>\n\n<b>'+em('search','🔍')+' Главная задача в карточной игре HiLo — угадать следующую карту: будет она выше текущей или ниже?\nЕсли вы угадаете, вы можете забрать выигрыш или продолжить.</b>',
'roulette':'<b>'+em('wheel','🎡')+' Рулетка | Help games</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>[сумма] [тип ставки]</code>\n\n<b>'+em('down','⬇️')+' Типы ставок:</b>\n'+em('gear','⚙️')+' <code>к | ч</code><b> — x1.98</b>\n'+em('gear','⚙️')+' <code>одд | евен</code><b> — x1.98</b>\n'+em('gear','⚙️')+' <code>0–36</code><b> (одно число) — x35.98</b>\n'+em('gear','⚙️')+' <code>1-18 | 19-36</code><b> — x1.98</b>',
'mines':'<b>'+em('mine_start','💣')+' Мины | Help games</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>мины [сумма] [кол-во]</code>\n\n<b>'+em('search','🔍')+' Главная задача — не подорваться на мине и забрать главный приз.</b>',
'gold':'<b>'+em('trident','🔱')+' Золото | Help game</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>золото [ставка]</code>\n\n<b>Выбирай одну из двух сторон на каждом шаге, избегай 💥 и забирай выигрыш.</b>',
 'tower':'<b>'+em('tower','🏯')+' Башня | Help game</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>башня [сумма] [мины 1-4]</code>\n\n<b>'+em('search','🔍')+' Главная задача: пройти все уровни и не нарваться ни на единую ловушку, чтобы забрать выигрыш.</b>',
 'ladder':'<b>'+em('flag','🚩')+' Лесенка | Help game</b>\n\n<b>'+em('clip','📎')+' Команда: </b><code>лесенка [сумма]</code>\n\n<b>В каждом из восьми рядов выбери безопасную клетку. Забери выигрыш до мины или пройди все ряды автоматически.</b>'}
GAMES_TITLE='<b>'+em('crown','🎮')+' Игры</b>\n\n<blockquote><b>Выбери игру, чтобы посмотреть правила и команды.</b></blockquote>'
@dp.message(Command('help','помощь'))
@dp.message(F.text.regexp(r'(?i)^\s*помощь\s*$'))
async def help_command(m:Message): await out(m,help_text(),kb([[b('Игры','games:menu','crown')]]))
@dp.message(Command('games','игры'))
@dp.message(F.text.regexp(r'(?i)^\s*игры\s*$'))
async def games_command(m:Message): await out(m,GAMES_TITLE,games_keyboard())
@dp.callback_query(F.data=='help:menu')
async def help_callback(q:CallbackQuery):
 await q.answer()
 if isinstance(q.message,Message): await out(q.message,help_text(),kb([[b('Назад','home')]]),True)
@dp.callback_query(F.data.startswith('games:'))
async def games_callback(q:CallbackQuery):
 await q.answer()
 if not isinstance(q.message,Message): return
 key=q.data.partition(':')[2]
 if key=='menu': await out(q.message,GAMES_TITLE,games_keyboard(),True)
 elif key in GAME_RULES: await out(q.message,GAME_RULES[key],kb([[b('Назад к играм','games:menu','5875082500023258804')]]),True)

# Bonus: one global cooldown per account, across all chats and restarts.
def claim_bonus(uid):
 now=int(time.time())
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
  old=c.execute('SELECT last_claim FROM daily_bonus_v1 WHERE uid=?',(uid,)).fetchone()
  if old and now<old['last_claim']+BONUS_SECONDS:
   return False,old['last_claim']+BONUS_SECONDS-now
  change(c,uid,BONUS_AMOUNT)
  c.execute('INSERT INTO daily_bonus_v1(uid,last_claim) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_claim=excluded.last_claim',(uid,now))
  c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(uid,uid,now,f'daily_bonus +{BONUS_AMOUNT}'))
 return True,0
@dp.message(Command('bonus','бонус'))
@dp.message(F.text.regexp(r'(?i)^\s*бонус\s*$'))
async def bonus(m:Message):
 try: paid,left=claim_bonus(m.from_user.id)
 except ValueError as e: await out(m,error_text(e)); return
 if paid:
  await out(m,'<b>'+em('bonus','🎁')+' Бонус</b>\n\n<blockquote><code>+5 000</code><b> бубликов</b></blockquote>')
 else:
  hours,left=divmod(left,3600); minutes,seconds=divmod(left,60)
  await out(m,'<b>'+em('clock','⏲')+f' Подождите еще {hours}ч{minutes:02d}м{seconds:02d}с чтобы получить бонус еще раз</b>')
@dp.message(F.text.regexp(r'(?i)^\s*(профиль|проф)\s*$'))
async def profile_command(m:Message):
 name='@'+m.from_user.username if m.from_user.username else m.from_user.full_name
 await out(m,profile_text(m.from_user.id,name))

@dp.message(F.text.regexp(r'(?i)^\s*топ\s+б\s*$'))
async def top_balance(m:Message):
 await out(m,top_rich_text(m.from_user.id))


# Food rewards and relationship HP
def remaining_text(seconds):
 seconds=max(0,int(seconds)); hours,rem=divmod(seconds,3600); minutes=rem//60
 return (em('clock','⌛')+' Через '+f'{hours}ч. {minutes}мин.') if hours else (em('clock','⌛')+' Через '+f'{minutes}мин.')

@dp.message(F.text.regexp(r'(?i)^\s*кд\s*$'))
async def cooldown_command(m:Message):
 uid=m.from_user.id; now=int(time.time())
 with db() as c:
  bonus=c.execute('SELECT last_claim FROM daily_bonus_v1 WHERE uid=?',(uid,)).fetchone()
  eat=c.execute('SELECT last_eat FROM eat_cooldown_v1 WHERE uid=?',(uid,)).fetchone()
 bonus_left=max(0,bonus['last_claim']+BONUS_SECONDS-now) if bonus else 0
 eat_left=max(0,eat['last_eat']+EAT_SECONDS-now) if eat else 0
 bonus_text='✅ Доступен' if not bonus_left else remaining_text(bonus_left)
 eat_text='✅ Доступно' if not eat_left else remaining_text(eat_left)
 await out(m,'<blockquote><b>'+em('gift','🎁')+' Бонус: '+bonus_text+'\n'+em('cake','🎂')+' Похавать: '+eat_text+'</b></blockquote>')

@dp.message(F.text.regexp(r'(?i)^\s*похавать\s*$'))
async def eat_command(m:Message):
 uid=m.from_user.id; now=int(time.time())
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
   row=c.execute('SELECT last_eat FROM eat_cooldown_v1 WHERE uid=?',(uid,)).fetchone()
   if row and now<row['last_eat']+EAT_SECONDS:
    raise ValueError('Подожди ещё '+remaining_text(row['last_eat']+EAT_SECONDS-now))
   grams=secrets.randbelow(4001)+1000
   coins=grams//10
   c.execute('INSERT INTO eat_v1(uid,total_grams,total_coins) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET total_grams=total_grams+excluded.total_grams,total_coins=total_coins+excluded.total_coins',(uid,grams,coins))
   c.execute('INSERT INTO bublins_v1(uid,amount) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET amount=amount+excluded.amount',(uid,coins))
   c.execute('INSERT INTO eat_cooldown_v1(uid,last_eat) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_eat=excluded.last_eat',(uid,now))
   total=c.execute('SELECT total_grams,total_coins FROM eat_v1 WHERE uid=?',(uid,)).fetchone()
  kg,rem=divmod(total['total_grams'],1000)
  text=('<b>'+em('food','🍔')+' Отлично посидели в кафешке и набрали '+f'{grams/1000:.2f}'+'кг\n'
        +em('donut','🍩')+' Бублин-коины: +'+fmt(coins)+' Бубликов\n\n'
        +em('candy','🍬')+' Всего съедено: '+str(kg)+','+f'{rem:03d}'+'кг\n'
        +'· '+em('donut','🍩')+' Бублин-коины: '+fmt(total['total_coins'])+'</b>')
  await out(m,text)
 except ValueError as e: await out(m,error_text(e))

@dp.message(F.text.regexp(r'(?i)^\s*похавать\s+вместе\s*$'))
async def eat_together(m:Message):
 if not m.reply_to_message or not m.reply_to_message.from_user:
  await out(m,error_text('Ответь командой на сообщение пользователя.')); return
 uid=m.from_user.id; other=m.reply_to_message.from_user.id
 if uid==other or m.reply_to_message.from_user.is_bot:
  await out(m,error_text('Нужно ответить другому пользователю.')); return
 hp=secrets.randbelow(900)+100
 first,second=sorted((uid,other))
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); allowed(c,other)
   row=c.execute('SELECT amount FROM bublins_v1 WHERE uid=?',(uid,)).fetchone()
   coins=row['amount'] if row else 0
   if coins<hp: raise ValueError('Недостаточно бублин-коинов. Нужно '+fmt(hp)+'.')
   c.execute('UPDATE bublins_v1 SET amount=amount-? WHERE uid=?',(hp,uid))
   c.execute('INSERT INTO relationship_v1(uid1,uid2,hp) VALUES(?,?,?) ON CONFLICT(uid1,uid2) DO UPDATE SET hp=hp+excluded.hp',(first,second,hp))
   total=c.execute('SELECT hp FROM relationship_v1 WHERE uid1=? AND uid2=?',(first,second)).fetchone()['hp']
  await out(m,'<b>'+em('food','🍔')+' Хорошо посидели\n'+em('smile','🙂')+' +'+fmt(hp)+' HP отношений\n\n'+em('comet','☄️')+' HP: '+fmt(total)+'/♾️\nДержите тонус. дальше больше. жирные свиньи.\n'+em('yum','😋')+'</b>')
 except ValueError as e: await out(m,error_text(e))

# Admin panel. Recovery moderation and appeals remain available when blocked.
def admin(m): return valid_user(m) and m.chat.type=='private' and is_admin(m.from_user.id)
async def admin_callback(q):
 ok=is_admin(q.from_user.id) and isinstance(q.message,Message) and q.message.chat.type=='private'
 await q.answer(None if ok else 'Нет доступа'); return ok
@dp.message(Command('panel','панель'))
async def panel(m:Message):
 if not admin(m): return
 waiting.pop(m.from_user.id,None)
 await out(m,'<b>Панель администратора</b>\n<blockquote><code>/give ID сумма</code><b> — выдать</b>\n<code>/take ID сумма</code><b> — списать</b>\n<code>/ban ID причина</code>\n<code>/unban ID</code>\n<code>/recheck ID</code>\n<code>+админ</code> / <code>-админ</code> ответом на сообщение</blockquote>',kb([[b('Логи','adm:logs:0')],[b('Апелляции','adm:appeals:0')],[b('Бан','adm:input:ban'),b('Разбан','adm:input:unban')],[b('Повторная проверка','adm:input:recheck')]]))
def moderate(actor,uid,action,reason):
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  c.execute('INSERT OR IGNORE INTO users(id,name) VALUES(?,?)',(uid,f'ID {uid}'))
  c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',({'ban':'failed','unban':'passed','recheck':'pending'}[action],int(time.time()),uid))
  if action=='unban': c.execute('INSERT OR IGNORE INTO app_exempt_v6 VALUES(?)',(uid,))
  else: c.execute('DELETE FROM app_exempt_v6 WHERE uid=?',(uid,))
  c.execute("UPDATE app_appeals_v6 SET state='closed' WHERE uid=? AND state='open'",(uid,))
  c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(actor,uid,int(time.time()),action+' '+reason[:1000]))
async def process_mod(m,action,text):
 parts=text.split(maxsplit=1)
 if not parts or not re.fullmatch(r'[1-9][0-9]{0,14}',parts[0]): await out(m,error_text('Укажи числовой ID')); return False
 uid=int(parts[0]); moderate(m.from_user.id,uid,action,parts[1] if len(parts)>1 else '')
 await out(m,f'<b>{em("ok","✅")} Выполнено: {action}, ID {uid}</b>')
 try: await bot.send_message(uid,{'ban':BLOCKED,'unban':'<b>✅ Бан снят. /start</b>','recheck':'<b>Разрешена повторная проверка. /start</b>'}[action])
 except Exception: pass
 return True
@dp.message(Command('ban','unban','recheck'))
async def moderation(m:Message):
 if not admin(m): return
 p=m.text.split(maxsplit=1); await process_mod(m,p[0].split('@')[0][1:],p[1] if len(p)>1 else '')
@dp.message(Command('cancel'))
async def cancel_admin(m:Message):
 if admin(m): waiting.pop(m.from_user.id,None); await out(m,'<b>Отменено.</b>')
@dp.message(lambda m: admin(m) and m.from_user.id in waiting)
async def admin_input(m:Message):
 action,deadline=waiting[m.from_user.id]
 if time.time()>deadline: waiting.pop(m.from_user.id,None); return
 if await process_mod(m,action,m.text or ''): waiting.pop(m.from_user.id,None)
def logtext(r):
 stamp=datetime.fromtimestamp(r['ts'],timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')
 lines=[em('ok','✅')+' Прошёл' if r['status']=='passed' else em('ban','⛔')+' Забанен.',em('5260293700088511294','📋')+f' Проверка #{r["id"]} {stamp}',em('5936017305585586269','🆔')+f' ID: {r["uid"]}',em('5775887550262546277','🌐')+' IP: '+esc(r['ip']),em('5967591100532134862','📱')+' Устройство/ОС: '+esc(r['device']),em('5994378914636500516','⚙️')+' Версия WebApp API: '+esc(r['api_version']),'Платформа: '+esc(r['platform']),'Гео IP: '+esc(r['geo'] or 'не определено'),em('5920344347152224466','🔎')+' '+esc(r['reason'])]
 return '<b>'+'\n'.join(lines)+'</b>'
def appealkeys(aid): return kb([[b('Одобрить',f'adm:approve:{aid}'),b('Отклонить',f'adm:reject:{aid}')]])
@dp.callback_query(F.data.startswith('adm:'))
async def admin_actions(q:CallbackQuery):
 if not await admin_callback(q): return
 try: _,action,raw=q.data.split(':')
 except ValueError: return
 if action=='input':
  if raw in ('ban','unban','recheck'):
   waiting[q.from_user.id]=(raw,time.time()+300)
   await out(q.message,'<b>Пришли ID и при необходимости причину. Отмена: /cancel</b>')
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
  c.execute('BEGIN IMMEDIATE')
  u=c.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
  old=c.execute('SELECT * FROM app_appeals_v6 WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
  if not u or u['status']!='failed': error='Активного бана нет.'
  elif old and (old['state']=='open' or now-old['ts']<86400): error='Апелляция уже подана. Не чаще раза в сутки.'
  else: error=None; aid=c.execute('INSERT INTO app_appeals_v6(uid,ts,body) VALUES(?,?,?)',(uid,now,body)).lastrowid
 if error: await out(m,error_text(error)); return
 await out(m,'<b>Апелляция сохранена.</b>'); await notify(f'<b>Апелляция ID {uid}\n{esc(body)}</b>',appealkeys(aid))


# Dynamic admin management: reply to the target user's message.
@dp.message(F.text.regexp(r'(?i)^\s*[+-]админ\s*$'))
async def manage_admin(m:Message):
 if not valid_user(m) or not is_admin(m.from_user.id): return
 if not m.reply_to_message or not valid_user(m.reply_to_message):
  await out(m,'<b>Ответь командой +админ или -админ на сообщение нужного пользователя.</b>'); return
 target=m.reply_to_message.from_user; uid=target.id
 if uid==m.from_user.id:
  await out(m,'<b>Нельзя изменить собственные права этой командой.</b>'); return
 add=m.text.strip().startswith('+')
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  if add:
   c.execute('DELETE FROM bot_admin_revoked_v1 WHERE uid=?',(uid,))
   c.execute('INSERT OR IGNORE INTO bot_admins_v1(uid,added_by,added_at) VALUES(?,?,?)',(uid,m.from_user.id,int(time.time())))
  else:
   c.execute('INSERT OR IGNORE INTO bot_admin_revoked_v1(uid) VALUES(?)',(uid,))
   c.execute('DELETE FROM bot_admins_v1 WHERE uid=?',(uid,))
 await out(m,'<b>'+em('crown','👑')+' '+link(uid,target.full_name)+(' стал админом' if add else ' больше не администратор')+'</b>')

# Balance commands.
@dp.message(F.text.regexp(r'(?i)^\s*б\s*$'))
async def bal(m:Message):
 with db() as c: value=balance(c,m.from_user.id)
 await out(m,'<b>'+link(m.from_user.id,m.from_user.full_name)+':\n'+em('cherries','🍒')+' Баланс: '+fmt(value)+' бубликов</b>')
@dp.message(F.text.regexp(r'(?i)^\s*[нп]\s+\d{1,16}\s*$'))
async def transfer_bubliks(m:Message):
 if not valid_user(m): return
 if not m.reply_to_message or not valid_user(m.reply_to_message):
  await out(m,'<b>'+em('error','❗')+' Ответь командой Н [сумма] или П [сумма] на сообщение получателя.</b>'); return
 target=m.reply_to_message.from_user; amount=int(m.text.split()[1]); uid=m.from_user.id
 if amount<1 or amount>MAX_BAL: await out(m,error_text('Некорректная сумма передачи')); return
 if target.id==uid: await out(m,error_text('Нельзя передать бублики самому себе')); return
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); allowed(c,target.id)
   change(c,uid,-amount); change(c,target.id,amount)
   c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(uid,target.id,int(time.time()),f'transfer {amount} from {uid}'))
  await out(m,'<b>'+em('money','🥯')+' '+link(uid,m.from_user.full_name)+' передал <code>'+fmt(amount)+'</code> бубликов '+link(target.id,target.full_name)+'</b>')
 except ValueError as e: await out(m,error_text(e))

@dp.message(Command('give','take'))
async def give(m:Message):
 if not valid_user(m) or not is_admin(m.from_user.id): return
 p=m.text.split(); target=None
 if len(p)==2 and m.reply_to_message and valid_user(m.reply_to_message): target=m.reply_to_message.from_user; uid=target.id; raw=p[1]
 elif len(p)==3 and re.fullmatch(r'[1-9][0-9]{0,14}',p[1]): uid=int(p[1]); raw=p[2]
 else: await out(m,'<b>/give ID сумма, /take ID сумма или ответом /give сумма</b>'); return
 if not re.fullmatch(r'[0-9]{1,16}',raw) or not 1<=int(raw)<=MAX_BAL: return
 delta=int(raw)*(1 if p[0].split('@')[0]=='/give' else -1)
 if target: touch(uid,target.full_name)
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,m.from_user.id); once(c,m); value=change(c,uid,delta)
   c.execute('INSERT INTO app_audit_v6(actor,uid,ts,action) VALUES(?,?,?,?)',(m.from_user.id,uid,int(time.time()),f'balance {delta:+} => {value}'))
 except ValueError as e: await out(m,error_text(e)); return
 await out(m,f'<b>{em("money","🥯")} ID {uid}\nИзменение: {delta:+}\nБаланс: {fmt(value)}</b>')

# Hi-Lo.
def card(n): return {1:'A',11:'J',12:'Q',13:'K'}.get(n,str(n))
def rate(n,d):
 count=n-1 if d=='low' else 13-n
 return two(Decimal('12.5')/count) if count else None
HELP='<b>'+em('5886436057091673541','🎴')+' Хило [ N ]\n'+em('5897846616966041652','🔮')+' Угадать последовательность: Выше | Ниже от Туза (1) До короля(13)</b>'
def gameview(g):
 if g['state']=='paid': return '<b>'+em('ok','✅')+' Выигрыш забран!</b>\n\n<blockquote><b>'+em('money','🥯')+' Выиграно: </b><code>'+fmt(g['payout'])+'</code><b> бубликов</b></blockquote>',None
 if g['state']=='lost':
  return '<b>'+em('error','❌')+' Неправильно! Выпало <code>'+card(g['card'])+'</code></b>\n<b>'+em('explode','💥')+' Вы проиграли <code>'+fmt(g['bet'])+'</code> бубликов</b>',kb([[b('Игра окончена',f'hl:{g["id"]}:{g["rev"]}:done','ok')]])
 if g['state']!='active': return '<b>Игра окончена.</b>',None
 available=min(MAX_PAY,int(Decimal(g['bet'])*two(g['mult'])))
 text='<b>'+em('5447327564345416222','🎴')+' Хило (карточная игра)</b>\n\n<blockquote><b>'+em('5389099885259666198','🃏')+' Карта: '+card(g['card'])+'\n'+em('5447160283959173987','✖️')+' Множитель: x'+num(g['mult'])+'\n'+em('money','🥯')+' Можно забрать: </b><code>'+fmt(available)+'</code><b> бубликов</b></blockquote>'
 row=[]
 for d,label,icon,style in [('low','Ниже','5447490850412074781','danger'),('high','Выше','5449683594425410231','success')]:
  k=rate(g['card'],d)
  if k: row.append(b(label+' x'+num(k),f'hl:{g["id"]}:{g["rev"]}:{d}',icon,style))
 rows=[row]
 if g['wins']: rows.append([b('Забрать выигрыш x'+num(g['mult']),f'hl:{g["id"]}:{g["rev"]}:cash','5447184078077992135','primary')])
 return text,kb(rows)
@dp.message(F.text.regexp(r'(?i)^\s*(хл|хило)(?:\s.*)?$'))
async def hilo(m:Message):
 uid=m.from_user.id; p=m.text.split()
 if len(p)==2 and re.fullmatch(r'-?\d{1,12}',p[1]) and int(p[1])<10: await out(m,error_text('Ставки от 10 бубликов')); return
 if len(p)>2 or (len(p)==2 and (not re.fullmatch(r'\d{1,10}',p[1]) or int(p[1])>MAX_BET)): await out(m,HELP); return
 with db() as c: active=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active:
  text,keys=gameview(active); await out(m,text,keys); return
 other,notice=active_game_notice(uid)
 if other: await out(m,other,notice); return
 if len(p)!=2: await out(m,HELP); return
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m)
   g=c.execute("SELECT * FROM app_hilo_v6 WHERE uid=? AND state='active'",(uid,)).fetchone()
   if not g:
    bet=int(p[1]); change(c,uid,-bet)
    gid=c.execute("INSERT INTO app_hilo_v6(uid,bet,card,mult,chat,msg) VALUES(?,?,?,'1',?,?)",(uid,bet,secrets.randbelow(13)+1,m.chat.id,m.message_id)).lastrowid
    g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
  text,keys=gameview(g); await out(m,text,keys)
 except ValueError as e: await out(m,error_text(e))
@dp.callback_query(F.data.startswith('hl:'))
async def hilo_step(q:CallbackQuery):
 try:
  _,gid,rev,action=q.data.split(':'); gid=int(gid); rev=int(rev); tie=False
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
   if not g or g['uid']!=q.from_user.id: raise ValueError('Это не твоя игра.')
   if g['state']=='lost' and action=='done':
    await q.answer(); return
   if g['state']=='active' and g['rev']==rev:
    if action=='cash':
     if not g['wins']: raise ValueError('Сначала сделай успешный ход.')
     payout=min(MAX_PAY,int(Decimal(g['bet'])*two(g['mult']))); change(c,g['uid'],payout)
     c.execute("UPDATE app_hilo_v6 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
    elif action in ('low','high'):
     k=rate(g['card'],action)
     if k is None: raise ValueError('Невозможный ход.')
     n=secrets.randbelow(13)+1; win=n<g['card'] if action=='low' else n>g['card']
     if n==g['card']:
      tie=True; c.execute('UPDATE app_hilo_v6 SET rev=rev+1 WHERE id=?',(gid,))
     elif win:
      mult=two(min(two(g['mult'])*k,Decimal(MAX_PAY)/g['bet']))
      c.execute('UPDATE app_hilo_v6 SET card=?,mult=?,wins=wins+1,rev=rev+1 WHERE id=?',(n,num(mult),gid))
     else:
       c.execute("UPDATE app_hilo_v6 SET card=?,state='lost',rev=rev+1 WHERE id=?",(n,gid)); add_rake(c,g['uid'],g['bet'])
    else: raise ValueError('Неизвестное действие.')
   g=c.execute('SELECT * FROM app_hilo_v6 WHERE id=?',(gid,)).fetchone()
  await q.answer('Выпала такая же карта — это ничья, вы не проиграли.' if tie else None); text,keys=gameview(g)
  if isinstance(q.message,Message): await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Mines: randomly generated immutable board; cents rounded half-up exactly.
def mines_cents(count,opened,bet):
 if opened==0: return 100
 numerator=95*math.comb(25,opened); denominator=math.comb(25-count,opened)
 cents=(2*numerator+denominator)//(2*denominator)
 return min(cents,MAX_PAY*100//bet)
def cents_text(cents):
 if cents==100: return '1.0'
 whole,part=divmod(cents,100)
 return f'{whole}.{part:02d}'.rstrip('0').rstrip('.')
def mine_value(g): return min(MAX_PAY,g['bet']*g['cents']//100)
MINES_HELP='<b>'+em('flag','🏳️')+' Мины [ставка] [кол-во мин]\n'+em('bulb','💡')+' Задача и смысл игры выбрать как можно больше безопасных клеток и не подорваться на минах</b>'
def mineview(g):
 opened=g['opened_mask'].bit_count(); mult=cents_text(g['cents'])
 if g['state']=='lost': text='<b>'+em('lose_mine','💥')+' Проигрыш. Подорвался…</b>\n\n<blockquote><b>'+em('diamond','💎')+' Проигрыш: </b><code>'+fmt(g['bet'])+'</code><b> бубликов</b></blockquote>'
 elif g['state']=='paid': text='<b>'+em('ok','✔️')+' Коэффициент успешно забран</b>\n\n<blockquote><b>'+em('diamond','💎')+' Выигрыш: </b><code>'+fmt(g['payout'])+'</code><b> бубликов\n'+em('chart','📈')+' Коэффициент: </b><code>x'+mult+'</code></blockquote>'
 elif opened==0: text='<b>'+em('mine_start','💣')+' Начало сапера (мины)</b>\n\n<blockquote><b>'+em('flag','🏳️')+f' Кол-во мин: {g["mine_count"]}\n'+em('camera','📸')+' Коэффициент: </b><code>x1.0</code>\n<b>'+em('chat','💬')+' Ставка: </b><code>'+fmt(g['bet'])+'</code><b> бубликов</b></blockquote>'
 else: text='<b>'+em('moon','🌚')+' Игра продолжается</b>\n\n<blockquote><b>'+em('diamond','💎')+f' {25-g["mine_count"]-opened} | '+em('mine','💣')+f' {g["mine_count"]}\n'+em('chart','📈')+' Текущий множитель: x'+mult+'\n'+em('gift','🎁')+' Можно забрать: </b><code>'+fmt(mine_value(g))+'</code><b> бубликов</b></blockquote>'
 rows=[]
 for y in range(5):
  row=[]
  for x in range(5):
   cell=y*5+x; opened_cell=bool(g['opened_mask']&(1<<cell)); terminal=g['state']!='active'
   if terminal and g['hit']==cell: icon='explode'
   elif terminal and g['mine_mask']&(1<<cell): icon='mine'
   elif opened_cell: icon='diamond'
   else: icon='question'
   action='noop' if terminal or opened_cell else str(cell)
   row.append(b(INVISIBLE,f'mn:{g["id"]}:{g["rev"]}:{action}',icon))
  rows.append(row)
 if g['state']=='active' and opened: rows.append([b('Забрать выигрыш',f'mn:{g["id"]}:{g["rev"]}:cash','box','primary')])
 return text,kb(rows)
def create_mines(uid,bet,count,chat,msg):
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
  same=c.execute('SELECT * FROM mines_v1 WHERE chat=? AND msg=?',(chat,msg)).fetchone()
  if same: return dict(same)
  active=c.execute("SELECT * FROM mines_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
  if active: return dict(active)
  change(c,uid,-bet); locations=secrets.SystemRandom().sample(range(25),count); mask=sum(1<<i for i in locations)
  gid=c.execute('INSERT INTO mines_v1(uid,bet,mine_count,mine_mask,chat,msg,created) VALUES(?,?,?,?,?,?,?)',(uid,bet,count,mask,chat,msg,int(time.time()))).lastrowid
  return dict(c.execute('SELECT * FROM mines_v1 WHERE id=?',(gid,)).fetchone())
@dp.message(F.text.regexp(r'(?i)^\s*мины(?:\s.*)?$'))
async def mines(m:Message):
 uid=m.from_user.id; p=m.text.split()
 if len(p)!=1:
  if len(p)!=3 or not re.fullmatch(r'-?\d{1,12}',p[1]) or not re.fullmatch(r'\d{1,2}',p[2]): await out(m,MINES_HELP); return
  bet=int(p[1]); count=int(p[2])
  if bet<10: await out(m,error_text('Ставки от 10 бубликов')); return
  if bet>MAX_BET or not 1<=count<=24: await out(m,MINES_HELP); return
 with db() as c: active=c.execute("SELECT * FROM mines_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active: text,keys=mineview(active); await out(m,text,keys); return
 other,notice=active_game_notice(uid)
 if other: await out(m,other,notice); return
 if len(p)==1: await out(m,MINES_HELP); return
 try:
  g=create_mines(uid,bet,count,m.chat.id,m.message_id); text,keys=mineview(g); await out(m,text,keys)
 except ValueError as e: await out(m,error_text(e))
def mine_move(uid,gid,revision,action):
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid); row=c.execute('SELECT * FROM mines_v1 WHERE id=?',(gid,)).fetchone()
  if not row or row['uid']!=uid: raise ValueError('Это не твоя игра.')
  g=dict(row)
  if g['state']!='active': return g,'Игра уже завершена.'
  if g['rev']!=revision: return g,'Показан актуальный ход. Нажми клетку ещё раз.'
  if action=='noop': return g,None
  if action=='cash':
   if not g['opened_mask']: raise ValueError('Сначала открой безопасную клетку.')
   payout=mine_value(g); change(c,uid,payout)
   c.execute("UPDATE mines_v1 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
  else:
   if not re.fullmatch(r'\d{1,2}',action) or not 0<=int(action)<25: raise ValueError('Неизвестная клетка.')
   cell=int(action); bit=1<<cell
   if g['opened_mask']&bit: return g,None
   if g['mine_mask']&bit:
     c.execute("UPDATE mines_v1 SET state='lost',hit=?,rev=rev+1 WHERE id=?",(cell,gid)); add_rake(c,uid,g['bet'])
   else:
    opened_mask=g['opened_mask']|bit; opened=opened_mask.bit_count(); cents=mines_cents(g['mine_count'],opened,g['bet']); final=opened==25-g['mine_count']; payout=min(MAX_PAY,g['bet']*cents//100) if final else 0
    if final: change(c,uid,payout)
    c.execute('UPDATE mines_v1 SET opened_mask=?,cents=?,state=?,payout=?,rev=rev+1 WHERE id=?',(opened_mask,cents,'paid' if final else 'active',payout,gid))
  return dict(c.execute('SELECT * FROM mines_v1 WHERE id=?',(gid,)).fetchone()),None
@dp.callback_query(F.data.startswith('mn:'))
async def mines_step(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,raw,revision,action=q.data.split(':'); g,note=mine_move(q.from_user.id,int(raw),int(revision),action)
  await q.answer(note); text,keys=mineview(g); await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Gold: 12 two-choice levels with a committed SHA-256 seed.
def gold_multiplier(step): return 2 ** step
def gold_payout(g,step=None):
 step=g['step'] if step is None else step
 return min(MAX_PAY,int(g['bet']*gold_multiplier(step)))
def gold_board(g):
 traps=json.loads(g['traps']); path=json.loads(g['path']); lines=[]
 reveal=g['state'] in ('lost','paid')
 for level in range(12,0,-1):
  idx=level-1; payout=min(MAX_PAY,g['bet']*gold_multiplier(level))
  if reveal or idx < len(path):
   trap=traps[idx]; chosen=path[idx] if idx<len(path) else None
   left=em('dynamite','🧨') if trap==0 else (em('money','💰') if chosen==0 else em('paper','🧻'))
   right=em('dynamite','🧨') if trap==1 else (em('money','💰') if chosen==1 else em('paper','🧻'))
   if g['state']=='lost' and idx==len(path)-1:
    left=em('explode','💥') if trap==0 else em('paper','🧻'); right=em('explode','💥') if trap==1 else em('paper','🧻')
  else: left=right=em('question','❓')
  lines.append(f'| {left} | {right} | <code>{fmt(payout)}</code><b> бубликов (</b><code>{gold_multiplier(level)}x</code><b>)</b>')
 return '\n'.join(lines)
def gold_view(g):
 who='<b>'+link(g['uid'],user_name(g['uid']))+'</b>'
 if g['state']=='lost':
  title='<b>'+em('explode','💥')+' Золото · Loss</b>'
  text=who+'\n'+title+'\n<code>···················</code>\n<b>'+em('paper','🧻')+' Ставка: </b><code>'+fmt(g['bet'])+'</code><b> бубликов\n'+em('trident','🔱')+' Шагов: </b><code>'+str(g['step'])+'</code><b> из 12</b>\n\n'+gold_board(g)
  return text,kb([[b('Игра окончена','gl:'+str(g['id'])+':'+str(g['rev'])+':done','ok')],[b('Честность','gl:'+str(g['id'])+':'+str(g['rev'])+':honest','key')]])
 if g['state']=='paid':
  text=who+'\n<b>'+em('crown','👑')+' Золото · Победа! '+em('ok','✔️')+'</b>\n<code>···················</code>\n<b>🔱 Пройдено: </b><code>'+str(g['step'])+' из 12</code><b>\n💰 Выигрыш: </b><code>'+fmt(g['payout'])+'</code><b> бубликов (</b><code>x'+str(gold_multiplier(g['step']))+'</code><b>)</b>\n\n'+gold_board(g)
  return text,kb([[b('Честность','gl:'+str(g['id'])+':'+str(g['rev'])+':honest','key')]])
 step=g['step']; payout=gold_payout(g)
 text='<b>'+em('trident','🔱')+' Золото | Games Rulka</b>\n\n<blockquote><b>'+em('paper','🧻')+' Ставка: </b><code>'+fmt(g['bet'])+'</code><b> бубликов\n🪤 Выигрыш: </b><code>x'+str(gold_multiplier(step))+'</code><b> | </b><code>'+fmt(payout)+'</code><b> бубликов</b></blockquote>\n'+gold_board(g)
 rows=[[b(INVISIBLE,f'gl:{g["id"]}:{g["rev"]}:0','question'),b(INVISIBLE,f'gl:{g["id"]}:{g["rev"]}:1','question')]]
 if step: rows.append([b('Забрать выигрыш',f'gl:{g["id"]}:{g["rev"]}:cash','donut','success')])
 bottom=[b('Честность',f'gl:{g["id"]}:{g["rev"]}:honest','key')]
 if step==0: bottom.append(b('Отменить',f'gl:{g["id"]}:{g["rev"]}:cancel','ban'))
 rows.append(bottom)
 return text,kb(rows)
def user_name(uid):
 with db() as c:
  r=c.execute('SELECT name FROM users WHERE id=?',(uid,)).fetchone()
 return r['name'] if r else ('ID '+str(uid))
def create_gold(uid,bet,chat,msg):
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
  old=c.execute('SELECT * FROM gold_v1 WHERE chat=? AND msg=?',(chat,msg)).fetchone()
  if old: return dict(old)
  active=c.execute("SELECT * FROM gold_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
  if active: return dict(active)
  seed=secrets.token_hex(32); traps=[hmac.new(bytes.fromhex(seed),f'gold:{i}'.encode(),hashlib.sha256).digest()[0]%2 for i in range(12)]
  change(c,uid,-bet)
  gid=c.execute('INSERT INTO gold_v1(uid,bet,traps,seed,hash,chat,msg,created) VALUES(?,?,?,?,?,?,?,?)',(uid,bet,json.dumps(traps),seed,hashlib.sha256(bytes.fromhex(seed)).hexdigest(),chat,msg,int(time.time()))).lastrowid
  return dict(c.execute('SELECT * FROM gold_v1 WHERE id=?',(gid,)).fetchone())
@dp.message(F.text.regexp(r'(?i)^\s*золото(?:\s+.*)?$'))
async def gold_command(m:Message):
 uid=m.from_user.id; p=m.text.split()
 if len(p)>2 or (len(p)==2 and not re.fullmatch(r'\d{1,10}',p[1])): await out(m,GAME_RULES['gold']); return
 with db() as c: active=c.execute("SELECT * FROM gold_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active: text,keys=gold_view(dict(active)); await out(m,text,keys); return
 other,notice=active_game_notice(uid)
 if other: await out(m,other,notice); return
 if len(p)!=2: await out(m,GAME_RULES['gold']); return
 bet=int(p[1])
 if not 10<=bet<=MAX_BET: await out(m,error_text('Ставки от 10 бубликов' if bet<10 else 'Максимальная ставка: '+fmt(MAX_BET))); return
 try:
  with db() as c: once(c,m)
  g=create_gold(uid,bet,m.chat.id,m.message_id); text,keys=gold_view(g); await out(m,text,keys)
 except ValueError as e: await out(m,error_text(e))
@dp.callback_query(F.data.startswith('gl:'))
async def gold_callback(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,raw,revision,action=q.data.split(':'); gid=int(raw); rev=int(revision)
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   row=c.execute('SELECT * FROM gold_v1 WHERE id=?',(gid,)).fetchone()
   if not row or row['uid']!=q.from_user.id: raise ValueError('Это не твоя игра.')
   g=dict(row)
   if action=='honest':
    text='<b>'+link(g['uid'],user_name(g['uid']))+'\n'+em('money','💰')+' Золото Запада · Честность</b>\n<code>····················</code>\n<b>'+em('key','🔑')+' SHA-256 Хэш:</b> <code>'+g['hash']+'</code>\n<b>'+em('key','🔑')+' Результат:</b> '+('игра не завершилась' if g['state']=='active' else ('победа' if g['state']=='paid' else 'проигрыш'))
    if g['state']!='active': text+='\n<b>Seed:</b> <code>'+g['seed']+'</code>'
    await q.answer(); await out(q.message,text,kb([[b('Назад к игре',f'gl:{gid}:{g["rev"]}:back','repeat')]]),True); return
   if action in ('done','noop'): await q.answer(); return
   if action=='back':
    await q.answer(); text,keys=gold_view(g); await out(q.message,text,keys,True); return
   if g['state']!='active': raise ValueError('Игра уже завершена.')
   if g['rev']!=rev: raise ValueError('Нажми кнопку в актуальной игре.')
   if action=='cancel':
    change(c,g['uid'],g['bet']); c.execute("UPDATE gold_v1 SET state='cancelled',rev=rev+1 WHERE id=?",(gid,)); await q.answer(); await q.message.edit_reply_markup(reply_markup=None); return
   if action=='cash':
    if not g['step']: raise ValueError('Сначала пройди хотя бы один шаг.')
    payout=gold_payout(g); change(c,g['uid'],payout); c.execute("UPDATE gold_v1 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
   elif action in ('0','1'):
    side=int(action); traps=json.loads(g['traps']); path=json.loads(g['path']); path.append(side)
    if traps[g['step']]==side:
     c.execute("UPDATE gold_v1 SET path=?,state='lost',rev=rev+1 WHERE id=?",(json.dumps(path),gid)); add_rake(c,g['uid'],g['bet'])
    else:
     step=g['step']+1; state='paid' if step==12 else 'active'; payout=gold_payout(g,step) if state=='paid' else 0
     if state=='paid': change(c,g['uid'],payout)
     c.execute('UPDATE gold_v1 SET path=?,step=?,state=?,payout=?,rev=rev+1 WHERE id=?',(json.dumps(path),step,state,payout,gid))
   elif action=='back': pass
   else: raise ValueError('Неизвестное действие.')
   g=dict(c.execute('SELECT * FROM gold_v1 WHERE id=?',(gid,)).fetchone())
  await q.answer(); text,keys=gold_view(g); await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Tower: eight floors, five cells per floor, one to four mines.
def tower_base(count): return float((Decimal('0.97')*Decimal(5)/Decimal(5-count)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))
def tower_mult(count,floor): return float((Decimal(str(tower_base(count)))**floor).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))
def tower_mult_text(count,floor): return '1.0' if floor==0 else format(Decimal(str(tower_mult(count,floor))),'f').rstrip('0').rstrip('.')
def tower_payout(g,floor=None):
 floor=g['floor'] if floor is None else floor
 return min(MAX_PAY,int(Decimal(g['bet'])*Decimal(str(tower_mult(g['mine_count'],floor)))))
def tower_view(g):
 count=g['mine_count']; floor=g['floor']; payout=tower_payout(g)
 path=json.loads(g['path']); masks=json.loads(g['mine_masks'])
 header='<b>'+em('tower','🏯')+' БАШНЯ</b>'
 details=(
  em('paper','🗒')+' <b>Ставка:</b> <code>'+fmt(g['bet'])+'</code> '+em('cherries','🍒')+
  '\n'+em('mine','💣')+' <b>Мин:</b> <code>'+str(count)+'</code>'+
  '\n'+em('shop','🏢')+' <b>Этаж:</b> <code>'+str(floor)+'/8</code>'+
  '\n'+em('chart','📈')+' <b>Коэффициент:</b> <code>x'+tower_mult_text(count,floor)+'</code>'
 )
 if g['state']=='lost':
  text=header+' · <b>ПРОИГРЫШ</b>\n\n<blockquote>'+details+'\n'+em('explode','💥')+' <b>Результат:</b> <code>−'+fmt(g['bet'])+'</code> '+em('cherries','🍒')+'\n'+em('ban','🚫')+' <b>Вы попали на мину</b></blockquote>'
  rows=[]
  for f in range(7,-1,-1):
   row=[]
   for cell in range(5):
    if f==g['hit_floor'] and cell==g['hit_cell']: icon='explode'
    elif masks[f]&(1<<cell): icon='mine'
    else: icon=None
    row.append(b(INVISIBLE,f'tw:{g["id"]}:{g["rev"]}:done',icon))
   rows.append(row)
  rows.append([b('Игра окончена',f'tw:{g["id"]}:{g["rev"]}:done','ban','danger')])
  return text,kb(rows)
 if g['state']=='paid':
  won=g['payout']
  text=header+' · <b>ПОБЕДА</b>\n\n<blockquote>'+details+'\n'+em('crown','👑')+' <b>Доступный выигрыш:</b> <code>'+fmt(won)+'</code> '+em('cherries','🍒')+'\n'+em('ok','✔️')+' <b>Результат:</b> <code>+'+fmt(won)+'</code> '+em('cherries','🍒')+'</blockquote>'
  return text,kb([[b('Игра окончена',f'tw:{g["id"]}:{g["rev"]}:done','ban','danger')]])
 text=header+' · <b>ИГРА</b>\n\n<blockquote>'+details+'\n'+em('money','💰')+' <b>Доступный выигрыш:</b> <code>'+fmt(payout)+'</code> '+em('cherries','🍒')+'</blockquote>'
 rows=[]
 # Telegram displays keyboard rows from top to bottom: highest/newest floor first.
 if floor<8:
  rows.append([b(INVISIBLE,f'tw:{g["id"]}:{g["rev"]}:{cell}','question') for cell in range(5)])
 for f in range(floor-1,-1,-1):
  row=[]
  for cell in range(5):
   icon='mine' if masks[f]&(1<<cell) else None
   row.append(b(INVISIBLE,f'tw:{g["id"]}:{g["rev"]}:done',icon))
  rows.append(row)
 if floor: rows.append([b('Забрать выигрыш',f'tw:{g["id"]}:{g["rev"]}:cash','crown','primary')])
 return text,kb(rows)

def create_tower(uid,bet,count,chat,msg):
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
  old=c.execute('SELECT * FROM tower_v1 WHERE chat=? AND msg=?',(chat,msg)).fetchone()
  if old: return dict(old)
  active=c.execute("SELECT * FROM tower_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
  if active: return dict(active)
  masks=[]
  for _ in range(8): masks.append(sum(1<<i for i in secrets.SystemRandom().sample(range(5),count)))
  change(c,uid,-bet)
  gid=c.execute('INSERT INTO tower_v1(uid,bet,mine_count,mine_masks,chat,msg,created) VALUES(?,?,?,?,?,?,?)',(uid,bet,count,json.dumps(masks),chat,msg,int(time.time()))).lastrowid
  return dict(c.execute('SELECT * FROM tower_v1 WHERE id=?',(gid,)).fetchone())
@dp.message(F.text.regexp(r'(?i)^\s*башня(?:\s+.*)?$'))
async def tower_command(m:Message):
 uid=m.from_user.id; p=m.text.split()
 if len(p)>3 or (len(p)==3 and (not re.fullmatch(r'\d{1,10}',p[1]) or not re.fullmatch(r'[1-4]',p[2]))) or (len(p)==2 and not re.fullmatch(r'\d{1,10}',p[1])): await out(m,GAME_RULES['tower']); return
 with db() as c: active=c.execute("SELECT * FROM tower_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active: text,keys=tower_view(dict(active)); await out(m,text,keys); return
 other,notice=active_game_notice(uid)
 if other: await out(m,other,notice); return
 if len(p)!=3: await out(m,GAME_RULES['tower']); return
 bet=int(p[1]); count=int(p[2])
 if not 10<=bet<=MAX_BET: await out(m,error_text('Ставки от 10 бубликов' if bet<10 else 'Максимальная ставка: '+fmt(MAX_BET))); return
 try:
  with db() as c: once(c,m)
  g=create_tower(uid,bet,count,m.chat.id,m.message_id); text,keys=tower_view(g); await out(m,text,keys)
 except ValueError as e: await out(m,error_text(e))
@dp.callback_query(F.data.startswith('tw:'))
async def tower_callback(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,raw,revision,action=q.data.split(':'); gid=int(raw); rev=int(revision)
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   row=c.execute('SELECT * FROM tower_v1 WHERE id=?',(gid,)).fetchone()
   if not row or row['uid']!=q.from_user.id: raise ValueError('Это не твоя игра.')
   g=dict(row)
   if action=='done': await q.answer(); return
   if g['state']!='active': raise ValueError('Игра уже завершена.')
   if g['rev']!=rev: raise ValueError('Нажми кнопку в актуальной игре.')
   if action=='cash':
    if not g['floor']: raise ValueError('Сначала пройди хотя бы один этаж.')
    payout=tower_payout(g); change(c,g['uid'],payout); c.execute("UPDATE tower_v1 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
   else:
    if not re.fullmatch(r'[0-4]',action): raise ValueError('Неизвестная клетка.')
    cell=int(action); masks=json.loads(g['mine_masks']); path=json.loads(g['path']); floor=g['floor']
    if masks[floor]&(1<<cell):
     c.execute("UPDATE tower_v1 SET state='lost',hit_floor=?,hit_cell=?,rev=rev+1 WHERE id=?",(floor,cell,gid)); add_rake(c,g['uid'],g['bet'])
    else:
     path.append(cell); next_floor=floor+1; state='paid' if next_floor==8 else 'active'; payout=tower_payout(g,next_floor) if state=='paid' else 0
     if state=='paid': change(c,g['uid'],payout)
     c.execute('UPDATE tower_v1 SET path=?,floor=?,state=?,payout=?,rev=rev+1 WHERE id=?',(json.dumps(path),next_floor,state,payout,gid))
   g=dict(c.execute('SELECT * FROM tower_v1 WHERE id=?',(gid,)).fetchone())
  await q.answer(); text,keys=tower_view(g); await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Ladder: persistent eight-row game. Each row contains exactly one mine.
LADDER_MULTS=tuple(Decimal(x) for x in ('1.23','1.64','2.18','2.91','3.88','5.17','6.89','9.19'))
def ladder_multiplier(floor):
 return LADDER_MULTS[max(0,min(8,int(floor)))-1] if floor else Decimal('1')
def ladder_payout(g,floor=None,uid=None):
 floor=g['floor'] if floor is None else int(floor)
 raw=int((Decimal(g['bet'])*ladder_multiplier(floor)).to_integral_value(rounding=ROUND_DOWN))
 with db() as c:
  current=balance(c,uid if uid is not None else g['uid'])
 return max(0,min(MAX_PAY,MAX_BAL-current,raw))
def bubliks(n):
 n=int(n); a=abs(n)%100; b1=a%10
 word='бублик' if 11<=a<=14 else ('бублик' if b1==1 else 'бублика' if 2<=b1<=4 else 'бубликов')
 return fmt(n)+' '+word
def ladder_view(g):
 floor=g['floor']; mines=json.loads(g['mines']); uid=g['uid']; bet=g['bet']; rev=g['rev']
 if g['state']=='active':
  balval=balance_for_view(uid)
  mult=ladder_multiplier(floor)
  if floor==0:
   text='<b>'+em('flag','🚩')+' Лесенка началась</b>\n<blockquote>'+em('money','💵')+' <b>Ставка:</b> <code>'+bubliks(bet)+'</code>\n'+em('cherries','🍒')+' <b>Баланс:</b> <code>'+bubliks(balval)+'</code>\n'+em('clip','📎')+' <b>Рядов: 8 | Клеток в ряду: 4</b>\n'+em('chart','📈')+' <b>Макс коэф:</b> <code>x9.19</code></blockquote>'
   cash=int((Decimal(bet)*Decimal('0.90')).to_integral_value(rounding=ROUND_DOWN))
   cash=min(cash,MAX_BAL-balval)
   take=b('Забрать (-10%)',f'ld:{g["id"]}:{rev}:cash0','ok','success')
  else:
   payout=ladder_payout(g,floor,uid)
   text='<b>'+em('flag','🚩')+' Дальше меньше!</b>\n<blockquote>'+em('clip','📎')+' <b>Пройдено:</b> <code>'+str(floor)+'|8 рядов</code>\n'+em('cherries','🍒')+' <b>Можно забрать:</b> <code>'+bubliks(payout)+'</code>\n'+em('chart','📈')+' <b>Коэффициент:</b> <code>x'+format(mult,'.2f')+'</code></blockquote>'
   take=b('Забрать выигрыш',f'ld:{g["id"]}:{rev}:cash','ok','success')
  rows=[]
  if floor<8: rows.append([b(INVISIBLE,f'ld:{g["id"]}:{rev}:{col}','flag') for col in range(4)])
  for r in range(floor-1,-1,-1):
   rows.append([b(INVISIBLE,f'ld:{g["id"]}:{rev}:noop','mine' if col==mines[r] else None) for col in range(4)])
  rows.append([take]); return text,kb(rows)
 if g['state']=='lost':
  completed=floor
  balval=balance_for_view(uid)
  text='<b>'+em('explode','💥')+' Хааахах!!! Попался!!</b>\n<blockquote>'+em('money','💵')+' <b>Заработал:</b> <code>-'+bubliks(bet)+'</code>\n'+em('cherries','🍒')+' <b>Баланс:</b> <code>'+bubliks(balval)+'</code>\n'+em('clip','📎')+' <b>Пройдено этапов:</b> <code>'+str(completed)+'|8</code></blockquote>'
  rows=[]
  for r in range(7,-1,-1):
   row=[]
   for col in range(4):
    hit=(r==g['hit_row'] and col==g['hit_cell'])
    mine=(col==mines[r])
    row.append(b(INVISIBLE,f'ld:{g["id"]}:{rev}:noop','explode' if hit else 'mine' if mine else None))
   rows.append(row)
  rows.append([b('Игра окончена',f'ld:{g["id"]}:{rev}:done','ban','danger')]); return text,kb(rows)
 if g['state']=='paid':
  payout=g['payout']; profit=payout-bet; balval=balance_for_view(uid)
  text='<b>'+em('crown','👑')+' Молодец, хоть что-то у тебя получилось</b>\n<blockquote>'+em('diamond','💎')+' <b>Выигрыш:</b> <code>'+bubliks(payout)+'</code>\n'+em('money','💵')+' <b>Профит:</b> <code>'+('+' if profit>=0 else '')+bubliks(profit)+' (x'+format(ladder_multiplier(floor),'.2f')+')</code>\n'+em('cherries','🍒')+' <b>Баланс:</b> <code>'+bubliks(balval)+'</code></blockquote>'
  rows=[]
  for r in range(7,-1,-1):
   rows.append([b(INVISIBLE,f'ld:{g["id"]}:{rev}:noop','mine' if col==mines[r] else None) for col in range(4)])
  rows.append([b('Игра окончена',f'ld:{g["id"]}:{rev}:done','ban','danger')]); return text,kb(rows)
 if g['state']=='cancelled':
  refund=g['payout']; profit=refund-bet; balval=balance_for_view(uid)
  text='<b>'+em('notice','ℹ️')+' Вы вышли из игры</b>\n<blockquote>'+em('money','💵')+' <b>Возврат:</b> <code>'+bubliks(refund)+'</code>\n'+em('money','💵')+' <b>Профит:</b> <code>'+('+' if profit>=0 else '')+bubliks(profit)+'</code>\n'+em('cherries','🍒')+' <b>Баланс:</b> <code>'+bubliks(balval)+'</code></blockquote>'
  return text,kb([[b('Игра окончена',f'ld:{g["id"]}:{rev}:done','ban','danger')]])
 return '<b>Игра окончена.</b>',None
def balance_for_view(uid):
 with db() as c: return balance(c,uid)
def create_ladder(uid,bet,chat,msg):
 with db() as c:
  c.execute('BEGIN IMMEDIATE'); allowed(c,uid)
  old=c.execute('SELECT * FROM ladder_v1 WHERE chat=? AND msg=?',(chat,msg)).fetchone()
  if old: return dict(old)
  active=c.execute("SELECT * FROM ladder_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
  if active: return dict(active)
  # One active individual game across all supported game tables.
  for table in ('app_hilo_v6','mines_v1','gold_v1','tower_v1'):
   if c.execute(f"SELECT 1 FROM {table} WHERE uid=? AND state='active'",(uid,)).fetchone(): raise ValueError('Сначала завершите текущую игру.')
  once(c, type('CommandRef',(),{'chat':type('Chat',(),{'id':chat})(),'message_id':msg})())
  mine_rows=[secrets.randbelow(4) for _ in range(8)]
  change(c,uid,-bet)
  gid=c.execute('INSERT INTO ladder_v1(uid,bet,mines,chat,msg,created) VALUES(?,?,?,?,?,?)',(uid,bet,json.dumps(mine_rows),chat,msg,int(time.time()))).lastrowid
  return dict(c.execute('SELECT * FROM ladder_v1 WHERE id=?',(gid,)).fetchone())
@dp.message(F.text.regexp(r'(?i)^\s*лесенка(?:\s+.*)?$'))
async def ladder_command(m:Message):
 uid=m.from_user.id; p=m.text.split()
 if len(p)>2 or (len(p)==2 and not re.fullmatch(r'\d{1,10}',p[1])):
  await out(m,'<b>'+em('clip','📎')+' Команда: <code>лесенка [сумма]</code></b>'); return
 with db() as c: active=c.execute("SELECT * FROM ladder_v1 WHERE uid=? AND state='active'",(uid,)).fetchone()
 if active:
  text,keys=ladder_view(dict(active)); await out(m,text,keys); return
 other,notice=active_game_notice(uid)
 if other: await out(m,other,notice); return
 if len(p)!=2: await out(m,'<b>'+em('clip','📎')+' Команда: <code>лесенка [сумма]</code></b>'); return
 bet=int(p[1])
 if not 10<=bet<=MAX_BET: await out(m,error_text('Ставки от 10 бубликов' if bet<10 else 'Максимальная ставка: '+fmt(MAX_BET))); return
 try:
  g=create_ladder(uid,bet,m.chat.id,m.message_id); text,keys=ladder_view(g); await out(m,text,keys)
 except ValueError as e: await out(m,error_text(e))
@dp.callback_query(F.data.startswith('ld:'))
async def ladder_callback(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,raw,revision,action=q.data.split(':'); gid=int(raw); rev=int(revision)
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   row=c.execute('SELECT * FROM ladder_v1 WHERE id=?',(gid,)).fetchone()
   if not row or row['uid']!=q.from_user.id: raise ValueError('Это не твоя игра.')
   g=dict(row)
   if action in ('noop','done'):
    await q.answer(); return
   if g['state']!='active': raise ValueError('Игра уже завершена.')
   if g['rev']!=rev: raise ValueError('Нажми кнопку в актуальной игре.')
   floor=g['floor']; mines=json.loads(g['mines'])
   if action=='cash0':
    if floor!=0: raise ValueError('Кнопка устарела.')
    balnow=balance(c,g['uid']); refund=min(int((Decimal(g['bet'])*Decimal('0.90')).to_integral_value(rounding=ROUND_DOWN)),MAX_BAL-balnow)
    change(c,g['uid'],refund)
    c.execute("UPDATE ladder_v1 SET state='cancelled',payout=?,rev=rev+1 WHERE id=?",(refund,gid))
   elif action=='cash':
    if floor<1: raise ValueError('Сначала пройди первый ряд.')
    balnow=balance(c,g['uid']); rawpay=int((Decimal(g['bet'])*ladder_multiplier(floor)).to_integral_value(rounding=ROUND_DOWN))
    payout=max(0,min(MAX_PAY,MAX_BAL-balnow,rawpay)); change(c,g['uid'],payout)
    c.execute("UPDATE ladder_v1 SET state='paid',payout=?,rev=rev+1 WHERE id=?",(payout,gid))
   else:
    if not re.fullmatch(r'[0-3]',action): raise ValueError('Неизвестная клетка.')
    col=int(action)
    if col==mines[floor]:
     c.execute("UPDATE ladder_v1 SET state='lost',hit_row=?,hit_cell=?,rev=rev+1 WHERE id=?",(floor,col,gid)); add_rake(c,g['uid'],g['bet'])
    else:
     next_floor=floor+1; state='paid' if next_floor==8 else 'active'; payout=0
     if state=='paid':
      balnow=balance(c,g['uid']); rawpay=int((Decimal(g['bet'])*ladder_multiplier(8)).to_integral_value(rounding=ROUND_DOWN)); payout=max(0,min(MAX_PAY,MAX_BAL-balnow,rawpay)); change(c,g['uid'],payout)
     c.execute('UPDATE ladder_v1 SET floor=?,state=?,payout=?,rev=rev+1 WHERE id=?',(next_floor,state,payout,gid))
   g=dict(c.execute('SELECT * FROM ladder_v1 WHERE id=?',(gid,)).fetchone())
  await q.answer(); text,keys=ladder_view(g); await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

@dp.callback_query(F.data.startswith('resume:'))
async def resume_game(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,kind,raw=q.data.split(':'); gid=int(raw)
  with db() as c:
   table={'hilo':'app_hilo_v6','mines':'mines_v1','gold':'gold_v1','tower':'tower_v1','ladder':'ladder_v1'}.get(kind)
   if not table: raise ValueError('Неизвестная игра.')
   row=c.execute(f'SELECT * FROM {table} WHERE id=? AND uid=? AND state=\'active\'',(gid,q.from_user.id)).fetchone()
  if not row: raise ValueError('Активная игра не найдена.')
  g=dict(row); text,keys={'hilo':gameview,'mines':mineview,'gold':gold_view,'tower':tower_view,'ladder':ladder_view}[kind](g)
  await q.answer(); await out(q.message,text,keys,True)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Roulette.
RED={1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
def color(n): return em('green','🟢') if n==0 else em('red','🔴') if n in RED else '⚫️'
def parsebet(s):
 s=s.lower().replace('ё','е'); aliases={'к':'red','красный':'red','red':'red','ч':'black','черный':'black','black':'black','з':'0','зеленый':'0','green':'0','чет':'even','четное':'even','even':'even','евен':'even','нечет':'odd','нечетное':'odd','odd':'odd','одд':'odd'}; s=aliases.get(s,s)
 if s in ('red','black','even','odd'): return s,Decimal('1.98')
 if re.fullmatch(r'\d{1,2}',s) and 0<=int(s)<=36: return str(int(s)),Decimal('35.98')
 match=re.fullmatch(r'(\d{1,2})-(\d{1,2})',s)
 if match:
  a,z=map(int,match.groups())
  if 0<=a<z<=36: return f'{a}-{z}',Decimal('1.98') if (a,z) in ((1,18),(19,36)) else two(Decimal('35.98')/(z-a+1))
 raise ValueError('Неверная ставка')
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

def place_partial(c,r,uid,name,bets):
 allowed(c,uid); funds=balance(c,uid); accepted=[]; report=[]
 for amount,kind,k in bets:
  if not 10<=amount<=MAX_BET: report.append((amount,kind,False,'Ставки от 10 до '+fmt(MAX_BET)+' бубликов')); continue
  if funds<amount: report.append((amount,kind,False,'Недостаточно средств')); continue
  funds-=amount; accepted.append((amount,kind,k)); report.append((amount,kind,True,''))
 for amount,kind,k in accepted: c.execute('INSERT INTO r_bet9(rid,uid,name,amount,kind,rate) VALUES(?,?,?,?,?,?)',(r['id'],uid,name[:100],amount,kind,num(k)))
 if accepted:
  c.execute('UPDATE balances SET amount=? WHERE uid=?',(funds,uid))
  c.execute('UPDATE r_round9 SET last=? WHERE id=?',(time.time(),r['id']))
 return report,len(accepted)

# Accepted rows are quoted. Rejected rows are ALWAYS outside blockquote.
# Fill each message to 4096 visible UTF-16 units; do not split every N rows.
def render_report_rows(rows):
 text='<b>'+em('fire','🔥')+' Ставки приняты:</b>\n\n'
 in_quote=False; first=True
 for accepted,line in rows:
  if accepted:
   if not in_quote:
    if not first: text+='\n\n'
    text+='<blockquote><b>'; in_quote=True
   else: text+='\n'
   text+=line
  else:
   if in_quote: text+='</b></blockquote>'; in_quote=False
   if not first: text+='\n\n' if rows and first is False else '\n'
   text+='<b>'+line+'</b>'
  first=False
 if in_quote: text+='</b></blockquote>'
 return text
async def report_bets(m,report):
 accepted=[]; other_errors=[]; insufficient=False
 for amount,kind,ok,why in report:
  if ok: accepted.append(em('face','😀')+'<code>'+fmt(amount)+'</code> на '+label(kind))
  elif why=='Недостаточно средств': insufficient=True
  else: other_errors.append(esc(why)+'.')
 parts=[]
 if accepted:
  parts.append('<b>'+em('fire','🔥')+' Ставки приняты:</b>\n\n<blockquote><b>'+'\n'.join(accepted)+'</b></blockquote>')
 if insufficient:
  parts.append('<b>'+em('error','❗')+' Недостаточно баланса.</b>')
 for error in other_errors: parts.append('<b>'+em('error','❗')+' '+error+'</b>')
 if not parts: return
 text='\n\n'.join(parts)
 await out(m,text)

def bet_filter(m):
 p=(m.text or '').split()
 if len(p)<2 or not re.fullmatch(r'\d{1,10}',p[0]): return False
 if len(p)-1>150: return True
 try:
  for token in p[1:]: parsebet(token)
  return True
 except ValueError: return False
@dp.message(bet_filter)
async def bet_message(m:Message):
 parts=m.text.split(); amount=int(parts[0]); tokens=parts[1:]
 if len(tokens)>150: await out(m,'<b>'+em('limit','❗')+' Превышен лимит ставок! лимит: 150.</b>'); return
 bets=[(amount,*parsebet(token)) for token in tokens]
 if not 10<=amount<=MAX_BET: await out(m,error_text('Ставки от 10 бубликов' if amount<10 else 'Максимальная ставка: '+fmt(MAX_BET))); return
 try:
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); once(c,m); r=round_open(c,m.chat.id,m.message_thread_id or 0)
   report,_=place_partial(c,r,m.from_user.id,m.from_user.full_name,bets)
  await report_bets(m,report)
 except ValueError as e: await out(m,error_text(e))

def repeatkeys(rid,nav=None):
 rows=[]
 if nav: rows.append(nav)
 rows.append([b('Повторить ставку',f'rr:{rid}:1','repeat'),b('Удвоить ставку',f'rr:{rid}:2','double')]); return kb(rows)
def result_view(rid,page,chat,thread):
 with db() as c:
  r=c.execute("SELECT * FROM r_round9 WHERE id=? AND state='done' AND chat=? AND thread=?",(rid,chat,thread)).fetchone()
  if not r: raise ValueError('Раунд недоступен.')
  total=c.execute("SELECT count(*) FROM r_bet9 WHERE rid=? AND state='settled' AND payout>0",(rid,)).fetchone()[0]
  page=max(0,min(page,max(0,(total-1)//5)))
  rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND state='settled' AND payout>0 ORDER BY id LIMIT 5 OFFSET ?",(rid,page*5)).fetchall()
 header='<b>'+em('crown','👑')+f' Выпало: {color(r["number"])} {r["number"]}</b>\n\n'
 lines=[link(x['uid'],x['name'])+' ставил '+fmt(x['amount'])+' на '+label(x['kind'])+' выиграл '+fmt(x['payout'])+' бубликов' for x in rows] if rows else [em('none','😔')+' Победителей в этом раунде нет.']
 text=header+'<blockquote><b>'+'\n'.join(lines)+'</b></blockquote>'; nav=[]
 if page: nav.append(b('←',f'rpage:{rid}:{page-1}'))
 if (page+1)*5<total: nav.append(b('→',f'rpage:{rid}:{page+1}'))
 return text,repeatkeys(rid,nav)
@dp.message(F.text.regexp(r'(?i)^\s*(го|лог|отмена|ставки)\s*$'))
async def roulette(m:Message):
 uid=m.from_user.id; cmd=m.text.strip().lower(); thread=m.message_thread_id or 0
 try:
  if cmd=='лог':
   with db() as c: rows=c.execute("SELECT * FROM (SELECT * FROM r_round9 WHERE chat=? AND thread=? AND state='done' ORDER BY id DESC LIMIT 10) ORDER BY id ASC",(m.chat.id,thread)).fetchall()
   await out(m,'<b>'+em('crown','👑')+' Лог рулетки\n'+('\n\n'.join('<b>'+str(i+1)+'.</b> '+color(r['number'])+' <code>'+str(r['number'])+'</code>' for i,r in enumerate(rows)) or 'Пока пусто.')+'</b>'); return
  if cmd=='ставки':
   with db() as c: r=c.execute("SELECT id FROM r_round9 WHERE chat=? AND thread=? AND state='open'",(m.chat.id,thread)).fetchone()
   if not r: await out(m,'<b>Ставок нет.</b>'); return
   text,keys=own_bets(r['id'],uid,0,m.chat.id,thread); await out(m,text,keys); return
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,uid); once(c,m); r=round_open(c,m.chat.id,thread)
   if cmd=='отмена':
    total=c.execute("SELECT coalesce(sum(amount),0) FROM r_bet9 WHERE rid=? AND uid=? AND state='active'",(r['id'],uid)).fetchone()[0]
    if total:
     change(c,uid,total); c.execute("UPDATE r_bet9 SET state='cancelled' WHERE rid=? AND uid=? AND state='active'",(r['id'],uid))
    text='<b>'+em('repeat','🔄')+f' Ставки отменены. Возвращено {fmt(total)} бубликов.</b>'
   else:
    rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND state='active'",(r['id'],)).fetchall()
    if not rows: raise ValueError('Ставок нет.')
    wait=math.ceil(5-(time.time()-r['last']))
    if wait>0: raise ValueError(f'Подождите {wait} секунд…')
    n=secrets.randbelow(37); totals={}; results=[]
    for x in rows:
     payout=int(Decimal(x['amount'])*Decimal(x['rate'])) if wins(x['kind'],n) else 0
     totals[x['uid']]=totals.get(x['uid'],0)+payout; results.append((x['id'],payout))
    for who,payout in totals.items(): change(c,who,payout)
    for bid,payout in results: c.execute("UPDATE r_bet9 SET state='settled',payout=? WHERE id=?",(payout,bid))
    losses={}
    for x in rows:
     if not wins(x['kind'],n): losses[x['uid']]=losses.get(x['uid'],0)+x['amount']
    for who,lost_amount in losses.items(): add_rake(c,who,lost_amount)
    c.execute("UPDATE r_round9 SET state='done',number=?,ts=? WHERE id=?",(n,int(time.time()),r['id']))
  if cmd=='отмена': await out(m,text)
  else:
   animation=None
   try:
    animation=await bot.send_animation(m.chat.id,FSInputFile(Path(__file__).with_name('roulette-animation.gif')),message_thread_id=m.message_thread_id,reply_parameters=ReplyParameters(message_id=m.message_id))
    await asyncio.sleep(3)
   except Exception:
    logging.exception('Не удалось отправить анимацию рулетки')
   finally:
    if animation:
     try: await bot.delete_message(m.chat.id,animation.message_id)
     except Exception: logging.warning('Не удалось удалить анимацию рулетки')
   text,keys=result_view(r['id'],0,m.chat.id,thread); await out(m,text,keys)
 except ValueError as e:
  text=str(e); await out(m,'<b>'+em('clock','⏲')+' '+esc(text)+'</b>' if text.startswith('Подождите') else ('<b>'+em('error','❗')+' Недостаточно средств.</b>' if text=='Недостаточно средств' else error_text(text)))
def own_bets(rid,uid,page,chat,thread):
 with db() as c:
  r=c.execute("SELECT id FROM r_round9 WHERE id=? AND chat=? AND thread=? AND state='open'",(rid,chat,thread)).fetchone()
  if not r: return '<b>Раунд завершён.</b>',None
  total=c.execute("SELECT count(*) FROM r_bet9 WHERE rid=? AND uid=? AND state='active'",(rid,uid)).fetchone()[0]
  page=max(0,min(page,max(0,(total-1)//10)))
  rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND uid=? AND state='active' ORDER BY id LIMIT 10 OFFSET ?",(rid,uid,page*10)).fetchall()
 text='<b>Ваши ставки</b>\n\n<blockquote><b>'+('\n'.join(fmt(x['amount'])+' на '+label(x['kind']) for x in rows) or 'Ставок нет.')+'</b></blockquote>'; nav=[]
 if page: nav.append(b('←',f'rmine:{rid}:{uid}:{page-1}'))
 if (page+1)*10<total: nav.append(b('→',f'rmine:{rid}:{uid}:{page+1}'))
 return text,kb([nav]) if nav else None
@dp.callback_query(F.data.startswith('rmine:'))
async def mine_page(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,rid,uid,page=q.data.split(':'); rid=int(rid); uid=int(uid); page=int(page)
  if q.from_user.id!=uid: raise ValueError('Это не твои ставки.')
  text,keys=own_bets(rid,uid,page,q.message.chat.id,q.message.message_thread_id or 0)
  await q.answer(); await out(q.message,text,keys,True)
 except ValueError as e: await q.answer(str(e),show_alert=True)
@dp.callback_query(F.data.startswith('rpage:'))
async def winners_page(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,rid,page=q.data.split(':'); text,keys=result_view(int(rid),int(page),q.message.chat.id,q.message.message_thread_id or 0)
  await q.answer(); await out(q.message,text,keys,True)
 except ValueError as e: await q.answer(str(e),show_alert=True)
@dp.callback_query(F.data.startswith('rr:'))
async def repeat_bets(q:CallbackQuery):
 if not isinstance(q.message,Message): await q.answer(); return
 try:
  _,source,factor=q.data.split(':'); source=int(source); factor=int(factor)
  if factor not in (1,2): raise ValueError('Ошибка кнопки.')
  with db() as c:
   c.execute('BEGIN IMMEDIATE'); allowed(c,q.from_user.id)
   old=c.execute("SELECT * FROM r_round9 WHERE id=? AND state='done'",(source,)).fetchone()
   if not old or old['chat']!=q.message.chat.id or old['thread']!=(q.message.message_thread_id or 0): raise ValueError('Раунд недоступен.')
   last=c.execute("SELECT id FROM r_round9 WHERE chat=? AND thread=? AND state='done' ORDER BY id DESC LIMIT 1",(old['chat'],old['thread'])).fetchone()
   if last['id']!=source: raise ValueError('Это старый раунд. Используй последнюю кнопку.')
   r=round_open(c,old['chat'],old['thread'])
   if c.execute('SELECT 1 FROM r_repeat9 WHERE source=? AND target=? AND uid=?',(source,r['id'],q.from_user.id)).fetchone(): raise ValueError('Ставки уже повторены в этом раунде.')
   rows=c.execute("SELECT * FROM r_bet9 WHERE rid=? AND uid=? AND state='settled' ORDER BY id",(source,q.from_user.id)).fetchall()
   if not rows: raise ValueError('У тебя не было ставок в этом раунде.')
   bets=[(x['amount']*factor,x['kind'],Decimal(x['rate'])) for x in rows]
   report,accepted=place_partial(c,r,q.from_user.id,q.from_user.full_name,bets)
   if accepted: c.execute('INSERT INTO r_repeat9 VALUES(?,?,?)',(source,r['id'],q.from_user.id))
  await q.answer('Ставки обработаны'); await report_bets(q.message,report)
 except (ValueError,TypeError) as e: await q.answer(str(e),show_alert=True)

# Verification. Final status cannot be rechecked without admin reset.
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
  if not isinstance(u,dict) or type(u.get('id')) is not int or u['id']<=0: raise ValueError()
  return u
 except (ValueError,KeyError,TypeError): raise HTTPException(401,'Закрой и снова открой Mini App.')
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
  system='Android'; ver=re.search(r'Android\s+([\d.]+)',ua); version=ver.group(1) if ver else ''; match=re.search(r'Android[^;)]*;\s*([^;)]+)',ua)
  if match:
   v=match.group(1).split(' Build/')[0].strip()
   if v not in ('K','wv','Mobile',''): model=v[:80]
 elif 'iPhone' in ua or 'iPad' in ua:
  system='iOS/iPadOS'; model='iPhone' if 'iPhone' in ua else 'iPad'; ver=re.search(r'OS\s+([0-9_]+)',ua); version=ver.group(1).replace('_','.') if ver else ''
 elif 'Windows' in ua: system='Windows'
 elif 'Macintosh' in ua: system='macOS / возможно iPadOS'
 elif 'Linux' in ua: system='Linux'
 hint=clean(d.get('model'),80)
 if hint: model=hint
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
  if result=='failed': reason=('Совпал IP' if match['ip']==ip else 'Совпали приблизительные характеристики устройства')+f'; другой ID {match["uid"]}'
  else: reason='Исключение администратора' if exempt else 'Совпадений нет'
  c.execute('UPDATE users SET status=?,checked_at=? WHERE id=?',(result,now,uid))
  rid=c.execute('INSERT INTO app_checks_v6(uid,ts,ip,fp,device,geo,platform,api_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',(uid,now,ip,fp,device,region,platform,api,result,reason)).lastrowid
  c.execute('DELETE FROM app_checks_v6 WHERE ts<?',(now-90*86400,))
  row=dict(c.execute('SELECT * FROM app_checks_v6 WHERE id=?',(rid,)).fetchone())
 return result,row

PAGE=r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><script src="https://telegram.org/js/telegram-web-app.js"></script><title>Проверка</title><style>body{margin:0;background:var(--tg-theme-bg-color,#101522);color:var(--tg-theme-text-color,#fff);font:16px system-ui;padding:24px}main{max-width:480px;margin:60px auto}h1{font-size:25px}button{width:100%;padding:17px;border:0;border-radius:14px;background:var(--tg-theme-button-color,#6274ff);color:white;font-size:17px;font-weight:bold;margin-top:20px}button:disabled{opacity:.5}#result{font-size:22px;font-weight:700;white-space:pre-wrap}details{font-size:12px;margin-top:20px;opacity:.8}[hidden]{display:none!important}</style></head><body><main><section id="form" hidden><h1>🧹 Пройти проверку на твинк.</h1><button id="verify">Пройти проверку</button><details><summary>Данные и условия проверки</summary><p>Нажимая кнопку, вы соглашаетесь на обработку Telegram ID, имени, IP, характеристик устройства, приблизительного отпечатка и города по IP. Логи хранятся до 90 дней, доступны администрации и отправляются ей в Telegram. Копии сообщений у администраторов автоматически не удаляются. Профиль, баланс, бонусы, история игр и блокировки сохраняются до удаления владельцем. Имя и ссылка прошедшего проверку публикуются в топах, баланс — в топе балансов.</p><p>Совпадение IP или характеристик с другим прошедшим аккаунтом приводит к автобану. Общий Wi-Fi/VPN и одинаковые телефоны могут давать ложные совпадения. Это не аппаратный ID, не точная модель и не GPS. Обжалование и запрос удаления: /appeal объяснение.</p></details></section><div id="result">Загрузка…</div><button id="retry" hidden>Повторить загрузку</button><button id="close" hidden>Вернуться в бот</button></main><script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const f=document.getElementById('form'),r=document.getElementById('result'),b=document.getElementById('verify'),retry=document.getElementById('retry'),closeBtn=document.getElementById('close');closeBtn.onclick=()=>tg?.close();
async function api(path,extra={}){if(!tg?.initData)throw Error('Открой приложение из бота.');const x=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({init_data:tg.initData,...extra})});const d=await x.json();if(!x.ok)throw Error(d.detail||'Ошибка сервера');return d;}
function render(s){f.hidden=s!=='pending';r.textContent=s==='passed'?'✅ ПРОВЕРКА ПРОЙДЕНА!':s==='failed'?'⛔️ ВЫ ЗАБАНЕНЫ.':'';retry.hidden=true;closeBtn.hidden=s==='pending';}
async function load(){f.hidden=true;retry.hidden=true;try{render((await api('/api/status')).status)}catch(e){r.textContent=e.message;retry.hidden=false;}}
retry.onclick=load;
b.onclick=async()=>{b.disabled=true;try{let h={};try{h=await navigator.userAgentData?.getHighEntropyValues(['model'])||{}}catch(e){}const device={model:h.model||'',screen:[screen.width,screen.height],cores:navigator.hardwareConcurrency||0,touch:navigator.maxTouchPoints||0,ratio:devicePixelRatio};r.textContent='Проверяем…';render((await api('/api/verify',{consent:true,device,platform:tg.platform||'',api_version:tg.version||''})).status);}catch(e){r.textContent=e.message;}finally{b.disabled=false;}};load();
</script></body></html>'''

@asynccontextmanager
async def lifespan(app):
 global BOT_USERNAME
 init(); me=await bot.get_me(); BOT_USERNAME=me.username or ''
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
 data=await read_json(req); user=validate(data.get('init_data')); old=await asyncio.to_thread(status,user['id'])
 if old in ('passed','failed'): return JSONResponse({'status':old},headers={'Cache-Control':'no-store'})
 if data.get('consent') is not True: raise HTTPException(400,'Нужно подтверждение условий')
 ip=client_ip(req)
 if not ip: raise HTTPException(503,'Сервер не определил публичный IP. Проверь настройки доверенного прокси.')
 fp,device=fingerprint(req.headers.get('user-agent',''),data.get('device')); region=await asyncio.to_thread(geo,ip)
 name=(' '.join(str(user.get(k,'')) for k in ('first_name','last_name')).strip() or str(user['id']))[:256]
 result,row=await asyncio.to_thread(check_user,user['id'],name,ip,fp,device,region,clean(data.get('platform'),50),clean(data.get('api_version'),30))
 if row: await notify(logtext(row))
 return JSONResponse({'status':result},headers={'Cache-Control':'no-store'})
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),proxy_headers=False)
