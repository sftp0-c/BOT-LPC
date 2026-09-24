import asyncio,os,json
from fastapi import FastAPI,Request
import database as db
from max_api import MaxAPI
api=MaxAPI(); app=FastAPI()
def B(t,p): return {"type":"callback","text":t,"payload":p}
def menu(): return [[B("📅 Расписание","schedule"),B("💬 Обратная связь","feedback")],[B("📄 Заказать справку","certificate"),B("📋 Мои обращения","tickets")],[B("👤 Профиль","profile")]]
def back(): return [[B("↩️ Назад","home")]]
def uid(u):
 m=u.get("message") or {}; return str((m.get("sender") or {}).get("user_id") or (u.get("user") or {}).get("user_id") or "")
async def send_home(x):
 a=await db.one("SELECT * FROM admins WHERE user_id=?",(x,))
 if a and a["role_type"]=="superadmin": return await api.send(x,"🔐 Панель superadmin",[[B("👥 Сотрудники","admins")],[B("📅 Расписания","schedules")],[B("📊 Статистика","stats")]])
 if a: return await api.send(x,"🏫 Кабинет сотрудника",[[B("📋 Мои обращения","staff")],[B("📊 Статистика","staffstats")],[B("📢 Рассылка","broadcast")]] if a["can_broadcast"] else [[B("📋 Мои обращения","staff")],[B("📊 Статистика","staffstats")]])
 return await api.send(x,"🏫 Бот колледжа. Выберите действие:",menu())
async def start(x):
 if await db.one("SELECT 1 FROM admins WHERE user_id=?",(x,)) or await db.one("SELECT 1 FROM users WHERE user_id=?",(x,)): return await send_home(x)
 await db.run("INSERT OR REPLACE INTO user_states VALUES(?,?,?)",(x,"name","{}")); await api.send(x,"Здравствуйте! Укажите ФИО.")
async def state(x,s,p="{}"): await db.run("INSERT OR REPLACE INTO user_states VALUES(?,?,?)",(x,s,p))
async def msg(x,t):
 if t=="/start": return await start(x)
 if t=="/supersecret_admin":
  if await db.one("SELECT 1 FROM admins WHERE user_id=? AND role_type='superadmin'",(x,)): return await send_home(x)
  return
 st=await db.one("SELECT * FROM user_states WHERE user_id=?",(x,))
 if st:
  p=json.loads(st["payload"])
  if st["state"]=="name": await state(x,"group",json.dumps({"name":t},ensure_ascii=False)); return await api.send(x,"Укажите код группы.")
  if st["state"]=="group": await db.run("INSERT OR REPLACE INTO users VALUES(?,?,?)",(x,p["name"],t)); await db.run("DELETE FROM user_states WHERE user_id=?",(x,)); return await send_home(x)
  if st["state"]=="ticket":
   tid=await db.run("INSERT INTO tickets(student_id,target_admin_id,category,text_content) VALUES(?,?,?,?)",(x,p["admin"],p["cat"],t)); await db.run("DELETE FROM user_states WHERE user_id=?",(x,)); await api.send(p["admin"],f"🔔 Обращение №{tid}\n{t}",[[B("В работу",f"st:{tid}:in_progress")],[B("Завершено",f"st:{tid}:completed"),B("Отклонить",f"st:{tid}:rejected")]]); return await api.send(x,f"✅ Обращение №{tid} отправлено.",menu())
  if st["state"]=="broadcast":
   await db.run("DELETE FROM user_states WHERE user_id=?",(x,)); rows=await db.all("SELECT user_id FROM users")
   for r in rows:
    try: await api.send(r["user_id"],"📢 Объявление колледжа:\n\n"+t)
    except Exception: pass
   return await api.send(x,"✅ Рассылка завершена.",back())
 if not await db.one("SELECT 1 FROM users WHERE user_id=?",(x,)): return await start(x)
 return await api.send(x,"Используйте кнопки меню.",menu())
async def cb(u):
 x=uid(u); c=u.get("callback") or {}; p=str(c.get("payload") or "")
 try:
  if c.get("callback_id"): await api.answer(c["callback_id"])
 except Exception: pass
 if p=="home": return await send_home(x)
 if p=="schedule":
  z=await db.one("SELECT * FROM users WHERE user_id=?",(x,)); r=await db.one("SELECT pdf_url FROM schedules WHERE group_code=?",(z["group_code"],)); return await api.send(x,r["pdf_url"] if r else "Расписание пока не настроено.",back())
 if p in ("feedback","certificate"):
  cat="feedback" if p=="feedback" else "certificates"; rows=await db.all("SELECT user_id,full_name FROM admins WHERE role_type!='superadmin' AND (ticket_category=? OR ticket_category='all')",(cat,)); return await api.send(x,"Выберите сотрудника:",[[B(r["full_name"],f"pick:{cat}:{r['user_id']}")] for r in rows] or back())
 if p.startswith("pick:"):
  _,cat,a=p.split(":",2); await state(x,"ticket",json.dumps({"admin":a,"cat":cat})); return await api.send(x,"Напишите обращение одним сообщением.",back())
 if p=="tickets":
  rows=await db.all("SELECT ticket_id,status,category,text_content FROM tickets WHERE student_id=? ORDER BY ticket_id DESC LIMIT 20",(x,)); return await api.send(x,"📋 Мои обращения:\n\n"+"\n".join(f"№{r['ticket_id']} · {r['status']} · {r['category']}\n{r['text_content'][:120]}" for r in rows) if rows else "Обращений нет.",back())
 if p=="profile":
  r=await db.one("SELECT * FROM users WHERE user_id=?",(x,)); return await api.send(x,f"👤 Профиль\nФИО: {r['full_name']}\nГруппа: {r['group_code']}",back())
 if p.startswith("st:"):
  _,tid,status=p.split(":"); t=await db.one("SELECT * FROM tickets WHERE ticket_id=?",(int(tid),)); a=await db.one("SELECT * FROM admins WHERE user_id=?",(x,))
  if t and a and (t["target_admin_id"]==x or a["role_type"]=="superadmin"): await db.run("UPDATE tickets SET status=? WHERE ticket_id=?",(status,int(tid))); await api.send(t["student_id"],f"🔔 Статус обращения №{tid}: {status}"); return await api.send(x,"✅ Статус изменён.",back())
 if p=="stats" and await db.one("SELECT 1 FROM admins WHERE user_id=? AND role_type='superadmin'",(x,)):
  u=await db.one("SELECT COUNT(*) n FROM users"); t=await db.one("SELECT COUNT(*) n FROM tickets"); return await api.send(x,f"📊 Студенты: {u['n']}\nОбращения: {t['n']}",back())
 if p=="staff":
  a=await db.one("SELECT * FROM admins WHERE user_id=?",(x,)); rows=await db.all("SELECT ticket_id,status,category,text_content FROM tickets WHERE target_admin_id=? ORDER BY ticket_id DESC",(x,)); return await api.send(x,"📋 Обращения:\n\n"+"\n".join(f"№{r['ticket_id']} · {r['status']} · {r['category']}\n{r['text_content']}" for r in rows) if rows else "Обращений нет.",back())
 if p=="staffstats":
  r=await db.one("SELECT COUNT(*) n FROM tickets WHERE target_admin_id=?",(x,)); return await api.send(x,f"📊 Ваших обращений: {r['n']}",back())
 if p=="broadcast":
  a=await db.one("SELECT * FROM admins WHERE user_id=?",(x,))
  if a and a["can_broadcast"]: await state(x,"broadcast"); return await api.send(x,"Введите текст рассылки.",back())
@app.get("/health")
async def health(): return {"ok":True,"platform":"MAX"}
@app.post("/webhook")
async def webhook(r:Request):
 u=await r.json(); asyncio.create_task(proc(u)); return {"ok":True}
async def proc(u):
 try:
  t=u.get("update_type") or u.get("type"); x=uid(u)
  if t=="bot_started": await start(x)
  elif t=="message_created":
   m=u.get("message") or {}; b=m.get("body") or {}; await msg(x,str(b.get("text") or m.get("text") or "").strip())
  elif t=="message_callback": await cb(u)
 except Exception as e: print("update error",e)
@app.on_event("startup")
async def startup():
 await db.init_db()
 if not os.getenv("MAX_WEBHOOK_URL"): asyncio.create_task(poll())
async def poll():
 marker=None
 while True:
  try:
   d=await api.updates(marker); marker=d.get("marker",marker)
   for u in d.get("updates",[]): await proc(u)
  except Exception: await asyncio.sleep(3)
