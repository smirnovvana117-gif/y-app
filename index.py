# -*- coding: utf-8 -*-
"""
Применяет все исправления к вашему файлу бота.

Запуск:   python apply_fix.py путь/к/index.py
Результат: рядом появится файл index_fixed.py (оригинал не меняется).
Если какой-то кусок не найден, скрипт остановится и скажет какой.
"""
import re
import sys

if len(sys.argv) < 2:
    sys.exit("Использование: python apply_fix.py путь/к/вашему_файлу.py")

src_path = sys.argv[1]
with open(src_path, encoding="utf-8") as f:
    code = f.read()


def rep(old, new, what):
    global code
    if code.count(old) != 1:
        sys.exit(f"[ОШИБКА] не найден (или найден не один раз) блок: {what}")
    code = code.replace(old, new)
    print(f"[ok] {what}")


# 1. версия схемы -> добавятся новые колонки
rep('SCHEMA_V = "10"', 'SCHEMA_V = "11"', "SCHEMA_V")

# 2. колонка gone
rep('"flyer_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT"},',
    '"flyer_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT", "gone": "INTEGER DEFAULT 0"},',
    "колонка gone у flyer_tasks")
rep('"tgrass_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT"},',
    '"tgrass_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT", "gone": "INTEGER DEFAULT 0"},',
    "колонка gone у tgrass_tasks")
rep('"ext_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT"},',
    '"ext_tasks": {"penalized": "INTEGER DEFAULT 0", "checked_at": "TEXT", "gone": "INTEGER DEFAULT 0"},',
    "колонка gone у ext_tasks")

# 3. sponsors_done
pat = re.compile(r"async def sponsors_done\(uid: int\) -> int:.*?(?=\n\nasync def try_pay_referrer)", re.S)
if len(pat.findall(code)) != 1:
    sys.exit("[ОШИБКА] не найдена функция sponsors_done")
code = pat.sub('''async def sponsors_done(uid: int) -> int:
    """Выполненные спонсоры + спонсоры из окна подписки, ставшие недоступными (gone).
    Свои каналы и задания админа не считаются."""
    cond = "user_id=? AND ((status='done' AND COALESCE(penalized,0)=0) OR (status='skip' AND COALESCE(gone,0)=1))"
    r = await fetchone(
        f"SELECT (SELECT COUNT(*) FROM flyer_tasks WHERE {cond}) + "
        f"(SELECT COUNT(*) FROM tgrass_tasks WHERE {cond}) + "
        f"(SELECT COUNT(*) FROM ext_tasks WHERE {cond}) AS c",
        (uid, uid, uid))
    return r["c"]''', code)
print("[ok] sponsors_done")

# 4. try_pay_referrer: логи причин
rep('''    if not u or not u["referrer_id"] or u["ref_paid"] or u["ref_penalized"] or not u["gate_ok"]:
        return
    referrer = await get_user(u["referrer_id"])
    need = await ref_tasks_for(referrer)
    if need > 0 and await sponsors_done(uid) < need:
        return''',
    '''    if not u or not u["referrer_id"] or u["ref_paid"] or u["ref_penalized"] or not u["gate_ok"]:
        logging.info("ref skip user=%s: ref=%s paid=%s pen=%s gate=%s", uid,
                     u and u["referrer_id"], u and u["ref_paid"], u and u["ref_penalized"], u and u["gate_ok"])
        return
    referrer = await get_user(u["referrer_id"])
    need = await ref_tasks_for(referrer)
    have = await sponsors_done(uid)
    if need > 0 and have < need:
        logging.info("ref wait user=%s: спонсоров %s из %s", uid, have, need)
        return''', "try_pay_referrer")

# 5. gate_mark
rep('async def gate_mark(src: str, row, status: str, uid: int = None) -> int:',
    'async def gate_mark(src: str, row, status: str, uid: int = None, gone: bool = False) -> int:',
    "сигнатура gate_mark")
rep('''    cur = await db.execute(f"UPDATE {GATE_TABLES[src]} SET status=?, day=? WHERE id=? AND status='shown'",
                           (status, today(), row["id"]))''',
    '''    cur = await db.execute(
        f"UPDATE {GATE_TABLES[src]} SET status=?, day=?, gone=? WHERE id=? AND status='shown'",
        (status, today(), 1 if gone else 0, row["id"]))''', "запрос gate_mark")

# 6. fake у TGrass
rep('            return "gone" if res.get("is_fake") else "ok"',
    '            return "fake" if res.get("is_fake") else "ok"', "TGrass is_fake")

# 7. gate_check
rep('''        elif st == "gone":
            if src != "own":
                await gate_mark(src, r, "skip", uid)''',
    '''        elif st == "gone":
            if src != "own":
                await gate_mark(src, r, "skip", uid, gone=True)   # недоступный спонсор идёт в счёт порога
        elif st == "fake":
            if src != "own":
                await gate_mark(src, r, "skip", uid)              # фейк не засчитываем''', "gate_check")

# 8. ложные штрафы Flowly
rep('lambda r, x: x[0] == "unsub" or (r["provider"] == "flowly" and x[0] == "no"),',
    'lambda r, x: x[0] == "unsub",', "штрафы Flowly")

# 9. /refdbg
rep('# ---------- Статистика ----------', '''@admin.message(Command("refdbg"))
async def a_refdbg(m: Message, command: CommandObject):
    """/refdbg <ID реферала> — почему не выдана награда пригласившему."""
    try:
        uid = int(command.args)
    except Exception:
        return await m.answer("Использование: /refdbg 123456789")
    u = await get_user(uid)
    if not u:
        return await m.answer("Пользователь не найден")
    ref = await get_user(u["referrer_id"]) if u["referrer_id"] else None
    need = await ref_tasks_for(ref)
    done = await sponsors_done(uid)
    parts = []
    for t in ("flyer_tasks", "tgrass_tasks", "ext_tasks"):
        rows = await fetchall(
            f"SELECT status, COALESCE(penalized,0) p, COALESCE(gone,0) g, COUNT(*) c FROM {t} "
            f"WHERE user_id=? GROUP BY 1,2,3", (uid,))
        parts.append(f"{t}: " + (", ".join(f"{r['status']}/pen={r['p']}/gone={r['g']}x{r['c']}" for r in rows) or "-"))
    await m.answer("<pre>" + html.escape(
        f"referrer_id: {u['referrer_id']}\\n"
        f"gate_ok: {u['gate_ok']} | ref_paid: {u['ref_paid']} | ref_penalized: {u['ref_penalized']}\\n"
        f"спонсоров засчитано: {done} / нужно: {need}\\n\\n" + "\\n".join(parts)) + "</pre>")


# ---------- Статистика ----------''', "/refdbg")

out = src_path.rsplit(".", 1)[0] + "_fixed.py"
with open(out, "w", encoding="utf-8") as f:
    f.write(code)
print("\nГотово ->", out)
