#!/usr/bin/env python3
"""Обновляет исходный main.py без удаления функций бота.
Python 3.11+, без сторонних зависимостей.
Положите рядом с исходным main.py и выполните: python update_bot.py
"""
import ast
import html
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

TOP = r'''def top_rich_text(uid):
 with db() as c:
  rows=c.execute("SELECT u.id,u.name,b.amount FROM users u JOIN balances b ON b.uid=u.id WHERE u.status='passed' ORDER BY b.amount DESC,u.id ASC LIMIT 3").fetchall()
  mine=balance(c,uid)
  eligible=c.execute("SELECT 1 FROM users WHERE id=? AND status='passed'",(uid,)).fetchone()
  rank=c.execute("SELECT count(*)+1 FROM balances b JOIN users u ON u.id=b.uid WHERE u.status='passed' AND (b.amount>? OR (b.amount=? AND b.uid<?))",(mine,mine,uid)).fetchone()[0] if eligible else None
 medals=(('top1','🥇'),('top2','🥈'),('top3','🥉'))
 lines=[]
 for i,r in enumerate(rows):
  key,fallback=medals[i]
  lines.append(em(key,fallback)+' <b>'+link(r['id'],r['name'][:55])+'</b>\n    '+em('cherries','🍒')+' <code>'+fmt(r['amount'])+'</code> <b>бубликов</b>')
 body='\n\n'.join(lines) if lines else em('none','😔')+' <b>Пока здесь никого нет.</b>'
 place=fmt(rank) if rank is not None else 'вне рейтинга'
 return ('<b>'+em('top_title','🏆')+' ТОП БАЛАНСА</b>\n\n'
         +'<blockquote>'+body+'</blockquote>\n\n'
         +'<b>'+em('your_place','📍')+' Ваше место: '+place+'</b>\n'
         +em('cherries','🍒')+' <b>Ваш баланс:</b> <code>'+fmt(mine)+'</code> <b>бубликов</b>')
'''

TOWER = r'''def tower_view(g):
 count=g['mine_count']; floor=g['floor']; state=g['state']
 path=json.loads(g['path']); masks=json.loads(g['mine_masks'])
 terminal=state!='active'
 mult=tower_mult_text(count,floor)
 if state=='lost':
  heading=em('explode','💥')+' Башня · Проигрыш'
  summary=em('mine','💣')+' <b>Проиграно:</b> <code>'+fmt(g['bet'])+'</code> <b>бубликов</b>'
 elif state=='paid':
  heading=em('crown','👑')+' Башня · Выигрыш забран'
  summary=em('gift','🎁')+' <b>Выигрыш:</b> <code>'+fmt(g['payout'])+'</code> <b>бубликов</b>'
 else:
  heading=em('tower','🏯')+' Башня'
  summary=(em('gift','🎁')+' <b>Можно забрать:</b> <code>'+fmt(tower_payout(g))+'</code> <b>бубликов</b>') if floor else (em('bulb','💡')+' <b>Открой безопасную клетку первого этажа.</b>')
 text=('<b>'+heading+'</b>\n\n<blockquote>'
       +em('money','💰')+' <b>Ставка:</b> <code>'+fmt(g['bet'])+'</code> <b>бубликов</b>\n'
       +em('mine','💣')+' <b>Мин на этаже:</b> <code>'+str(count)+'</code>\n'
       +em('tower','🏯')+' <b>Пройдено этажей:</b> <code>'+str(floor)+'/8</code>\n'
       +em('chart','📈')+' <b>Коэффициент:</b> <code>x'+mult+'</code>\n'
       +summary+'</blockquote>')
 if state=='active':
  text+='\n\n'+em('down','⬇️')+' <b>Выбери клетку на этаже '+str(floor+1)+'.</b>'
 elif state=='lost':
  text+='\n\n'+em('lose_mine','💥')+' <b>Попалась ловушка. Поле раскрыто.</b>'
 else:
  text+='\n\n'+em('ok','✅')+' <b>Выигрыш зачислен на баланс.</b>'
 rows=[]
 # В активной игре показываем текущий и пройденные этажи.
 # После завершения раскрываем все восемь этажей.
 levels=range(7,-1,-1) if terminal else range(floor,-1,-1)
 for f in levels:
  row=[]
  for cell in range(5):
   chosen=f<len(path) and path[f]==cell
   playable=not terminal and f==floor
   if playable:
    icon='question'; style=None; action=str(cell)
   elif terminal and state=='lost' and f==g['hit_floor'] and cell==g['hit_cell']:
    icon='explode'; style='danger'; action='done'
   elif chosen:
    icon='diamond'; style='success'; action='done'
   elif terminal and masks[f] & (1<<cell):
    icon='mine'; style=None; action='done'
   else:
    icon='green'; style=None; action='done'
   row.append(b(INVISIBLE,f'tw:{g["id"]}:{g["rev"]}:{action}',icon,style))
  rows.append(row)
 if state=='active' and floor:
  rows.append([b('Забрать '+fmt(tower_payout(g)),f'tw:{g["id"]}:{g["rev"]}:cash','gift','success')])
 elif terminal:
  rows.append([b('Игра окончена',f'tw:{g["id"]}:{g["rev"]}:done','ok')])
 return text,kb(rows)
'''


def parse_source(raw):
    # Обычный файл не декодируется: иначе можно повредить HTML Mini App.
    try:
        return raw, ast.parse(raw)
    except SyntaxError:
        decoded = html.unescape(raw)
        return decoded, ast.parse(decoded)


def replace_functions(source, tree):
    replacements = {'top_rich_text': TOP, 'tower_view': TOWER}
    changes = []
    for name, code in replacements.items():
        found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(found) != 1:
            raise RuntimeError(f'Не найдена единственная функция {name}. Нужен исходный main.py из переписки.')
        node = found[0]
        changes.append((node.lineno - 1, node.end_lineno, code))
    lines = source.splitlines(keepends=True)
    for start, end, code in sorted(changes, reverse=True):
        lines[start:end] = [code.rstrip() + '\n']
    result = ''.join(lines)
    compile(result, 'main.py', 'exec')
    return result


def extract_schema(tree):
    """Берёт полную SQL-схему из init(), не импортируя и не запуская бота."""
    init = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'init'), None)
    if init is None:
        raise RuntimeError('В исходнике нет функции init().')
    scripts = []
    for node in ast.walk(init):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'executescript' and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            scripts.append(node.args[0].value)
    if not scripts:
        raise RuntimeError('Не найдена SQL-схема в init().')
    schema = '\n\n'.join(scripts)
    # Проверка всей схемы до изменения файлов.
    connection = sqlite3.connect(':memory:')
    try:
        connection.executescript(schema)
        result = connection.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError('Ошибка SQLite: ' + result)
    finally:
        connection.close()
    return '-- Полная схема из init() исходного main.py. Пользовательских данных нет.\n' + schema.strip() + '\n'


def atomic_write(path, text):
    handle, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    folder = Path(__file__).resolve().parent
    target = folder / 'main.py'
    if not target.is_file():
        raise RuntimeError('Положи update_bot.py в одну папку с исходным main.py.')
    original_bytes = target.read_bytes()
    source, tree = parse_source(original_bytes.decode('utf-8-sig'))
    updated = replace_functions(source, tree)
    schema = extract_schema(tree)
    # Время + случайный суффикс: никогда не затираем предыдущую копию.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    backup = folder / ('main.backup_' + stamp + '_' + os.urandom(3).hex() + '.py')
    with backup.open('xb') as stream:
        stream.write(original_bytes)
    atomic_write(folder / 'database.sql', schema)
    database = folder / 'bot.sqlite3'
    if database.exists():
        db_note = 'bot.sqlite3 уже существует — база не изменена.'
    else:
        # Создаём базу во временном файле. Данные старой базы не перезаписываем.
        fd, temp_db = tempfile.mkstemp(prefix='empty_bot_', suffix='.sqlite3', dir=folder)
        os.close(fd)
        try:
            connection = sqlite3.connect(temp_db)
            try:
                connection.executescript(schema)
                connection.commit()
                tables = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            finally:
                connection.close()
            # Эксклюзивное создание защищает от перезаписи существующей базы.
            with database.open('xb') as stream:
                stream.write(Path(temp_db).read_bytes())
            db_note = f'Создана пустая bot.sqlite3, таблиц: {tables}.'
        finally:
            Path(temp_db).unlink(missing_ok=True)
    ignore = folder / '.gitignore'
    previous = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    entries = ['.env', '*.sqlite3', '*.sqlite3-wal', '*.sqlite3-shm', '*.sqlite3-journal',
               '*.db', '*.db-wal', '*.db-shm', '__pycache__/', '.venv/', 'main.backup_*.py']
    existing = set(previous.splitlines())
    additions = [entry for entry in entries if entry not in existing]
    if additions:
        atomic_write(ignore, previous.rstrip() + '\n\n# Локальные данные бота\n' + '\n'.join(additions) + '\n')
    atomic_write(target, updated)
    print('\nГотово!')
    print('main.py — полный обновлённый исходник. Все остальные функции сохранены.')
    print('database.sql — полная схема базы из исходного бота.')
    print(db_note)
    print('Резервная копия: ' + backup.name)
    print('Синтаксис Python и SQL-схема проверены локально этим скриптом.')
    print('Взаимодействие с Telegram не тестировалось.')
    print('\nВ GitHub загрузи main.py и database.sql, но не живую bot.sqlite3.')
    print('Бот также сам создаёт базу при запуске. Для хранения после деплоя нужен постоянный диск.')
    print('Если задан DB_PATH, бот использует именно этот путь, а не обязательно bot.sqlite3 рядом с кодом.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('\nОбновление остановлено: ' + str(exc))
        raise SystemExit(1)
