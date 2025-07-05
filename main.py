# app.py

import os
import json
import threading
import time
import logging
import re
import requests

from datetime import datetime
from flask import Flask, request

import telebot
import mercadopago

# === CONFIG VIA ENV ===
BOT_TOKEN         = os.getenv("BOT_TOKEN")
ALERT_BOT_TOKEN   = os.getenv("ALERT_BOT_TOKEN")
ALERT_CHAT_ID     = os.getenv("ALERT_CHAT_ID")
API_KEY_SMSBOWER  = os.getenv("API_KEY_SMSBOWER")
SMSBOWER_URL      = "https://smsbower.online/stubs/handler_api.php"
COUNTRY_ID        = "73"
MP_ACCESS_TOKEN   = os.getenv("MP_ACCESS_TOKEN")
SITE_URL          = os.getenv("SITE_URL").rstrip("/")

bot       = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot = telebot.TeleBot(ALERT_BOT_TOKEN)
mp_client = mercadopago.SDK(MP_ACCESS_TOKEN)

app = Flask(__name__)

# --- Logger ---
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        try: alert_bot.send_message(ALERT_CHAT_ID, msg)
        except: pass

logger = logging.getLogger("bot_sms")
logger.setLevel(logging.INFO)
h = TelegramLogHandler()
h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(h)

# --- State & locks ---
USERS_FILE       = "usuarios.json"
data_lock        = threading.Lock()
status_lock      = threading.Lock()
status_map       = {}  # aid → info
PENDING_RECHARGE = {}  # user_id → True
PRAZO_MINUTOS    = 23
PRAZO_SEGUNDOS   = PRAZO_MINUTOS * 60

# === Helpers users ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE,"w") as f: json.dump({},f)
        with open(USERS_FILE,"r") as f: return json.load(f)

def salvar_usuarios(u):
    with data_lock:
        with open(USERS_FILE,"w") as f: json.dump(u,f,indent=2)

def criar_usuario(uid):
    u = carregar_usuarios()
    if str(uid) not in u:
        u[str(uid)] = {"saldo":0.0,"numeros":[]}
        salvar_usuarios(u)
        logger.info(f"Novo usuário criado: {uid}")

def alterar_saldo(uid, novo):
    u = carregar_usuarios()
    u.setdefault(str(uid),{"saldo":0.0,"numeros":[]})["saldo"] = novo
    salvar_usuarios(u)
    logger.info(f"Saldo de {uid} = R$ {novo:.2f}")

def adicionar_numero(uid, aid):
    u = carregar_usuarios()
    usr = u.setdefault(str(uid),{"saldo":0.0,"numeros":[]})
    if aid not in usr["numeros"]:
        usr["numeros"].append(aid)
        salvar_usuarios(u)
        logger.info(f"Adicionado número {aid} a {uid}")

# === SMSBOWER API ===
def solicitar_numero(servico, max_price=None):
    params = {"api_key":API_KEY_SMSBOWER,"action":"getNumber","service":servico,"country":COUNTRY_ID}
    if max_price: params["maxPrice"] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL,params=params,timeout=15); r.raise_for_status()
        text = r.text.strip(); logger.info(f"GET_NUMBER → {r.url} → {text}")
    except Exception as e:
        logger.error(f"Erro getNumber: {e}"); return {"status":"error","message":str(e)}
    if text.startswith("ACCESS_NUMBER:"):
        _,aid,num = text.split(":",2); return {"status":"success","id":aid,"number":num}
    return {"status":"error","message":text}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL,params={"api_key":API_KEY_SMSBOWER,"action":"setStatus","status":"8","id":aid},timeout=10)
    except Exception as e: logger.error(f"Erro cancelar: {e}")

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL,params={"api_key":API_KEY_SMSBOWER,"action":"getStatus","id":aid},timeout=10)
        r.raise_for_status(); return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus: {e}"); return None

def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info: return

    service,full,short,chat_id = info["service"],info["full"],info["short"],info["chat_id"]
    msg_id = info.get("sms_message_id")
    info.setdefault("codes",[]); info["canceled_by_user"]=False

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            s = obter_status(aid); logger.info(f"CheckSMS → {aid}: {s}")
            if info["canceled_by_user"]: return
            if not s or s.startswith("STATUS_WAIT"): 
                time.sleep(5); continue
            if s=="STATUS_CANCEL":
                # provider cancel
                alterar_saldo(info["user_id"],carregar_usuarios()[str(info["user_id"])]["saldo"]+info["price"])
                bot.send_message(chat_id,f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido.")
                return
            code = s.split(":",1)[1] if ":" in s else s
            if code not in info["codes"]:
                info["codes"].append(code)
                # if codes list non-empty, user cannot cancel
                info["processed"]=True
                # build text
                rt = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                text = f"📦 {service}\n☎️ `{full}` / `{short}`\n\n"
                for i,c in enumerate(info["codes"],1):
                    text += f"📩 SMS{i}: `{c}`\n"
                text += f"🕘 {rt}"
                kb = telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("📲 Receber outro SMS",callback_data=f"retry_{aid}")
                )
                if msg_id is None:
                    msg = bot.send_message(chat_id,text,parse_mode="Markdown",reply_markup=kb)
                    info["sms_message_id"]=msg.message_id
                else:
                    bot.edit_message_text(text,chat_id,msg_id,parse_mode="Markdown",reply_markup=kb)
            time.sleep(5)
    threading.Thread(target=check_sms,daemon=True).start()

# === MENU / Handlers ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton("📲 Comprar serviços",callback_data="menu_comprar"),
        telebot.types.InlineKeyboardButton("💰 Saldo",callback_data="menu_saldo"),
        telebot.types.InlineKeyboardButton("🤑 Recarregar",callback_data="menu_recarregar"),
        telebot.types.InlineKeyboardButton("📜 Números",callback_data="menu_numeros"),
        telebot.types.InlineKeyboardButton("🆘 Suporte",url="https://t.me/cpfbotttchina")
    )
    bot.send_message(chat_id,"Escolha opção:",reply_markup=kb)

@bot.message_handler(commands=["start"])
def cmd_start(m):
    criar_usuario(m.from_user.id); send_menu(m.chat.id)

@bot.message_handler(func=lambda m:PENDING_RECHARGE.get(m.from_user.id) and re.fullmatch(r"\d+(\.\d{1,2})?",m.text))
def handle_recharge_amount(m):
    uid, amount = m.from_user.id, float(m.text)
    PENDING_RECHARGE.pop(uid,None)
    pref = mp_client.preference().create({
        "items":[{"title":"Recarga","quantity":1,"unit_price":amount}],
        "external_reference":f"{uid}:{amount}",
        "back_urls":{"success":f"{SITE_URL}/?paid=success","failure":f"{SITE_URL}/?paid=failure","pending":f"{SITE_URL}/?paid=pending"},
        "auto_return":"approved"
    })
    url = pref["response"]["init_point"]
    kb = telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton(f"💳 Pagar R$ {amount:.2f}",url=url)
    )
    bot.send_message(m.chat.id,f"Para recarregar R$ {amount:.2f}, clique:",reply_markup=kb)
    send_menu(m.chat.id)

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def default_menu(m):
    criar_usuario(m.from_user.id); send_menu(m.chat.id)

@bot.callback_query_handler(lambda c:c.data=="menu_recarregar")
def menu_recarregar(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    PENDING_RECHARGE[c.from_user.id]=True
    bot.send_message(c.message.chat.id,"Digite valor em reais para recarregar:")

@bot.callback_query_handler(lambda c:c.data=="menu_saldo")
def menu_saldo(c):
    bot.answer_callback_query(c.id); criar_usuario(c.from_user.id)
    s=carregar_usuarios()[str(c.from_user.id)]["saldo"]
    bot.send_message(c.message.chat.id,f"💰 Saldo: R$ {s:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c:c.data=="menu_numeros")
def menu_numeros(c):
    bot.answer_callback_query(c.id); criar_usuario(c.from_user.id)
    lst=carregar_usuarios()[str(c.from_user.id)]["numeros"]
    if not lst: bot.send_message(c.message.chat.id,"📭 Sem números ativos.")
    else:
        text="📋 *Seus números:*"
        for aid in lst:
            inf=status_map.get(aid)
            if inf:
                text+=f"\n*ID:* `{aid}` `{inf['full']}` / `{inf['short']}`"
        bot.send_message(c.message.chat.id,text,parse_mode="Markdown")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c:c.data=="menu_comprar")
def menu_comprar(c):
    bot.answer_callback_query(c.id); cmd_comprar(c.message)

@bot.message_handler(commands=["comprar"])
def cmd_comprar(m):
    criar_usuario(m.from_user.id)
    kb=telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton("📲 MercadoPago - R$0.75",callback_data="comprar_mercado"),
        telebot.types.InlineKeyboardButton("🇨🇳 China - R$0.70",callback_data="comprar_china"),
        telebot.types.InlineKeyboardButton("📡 Outros - R$0.90",callback_data="comprar_outros")
    )
    bot.send_message(m.chat.id,"Escolha serviço:",reply_markup=kb)

@bot.callback_query_handler(lambda c:c.data.startswith("comprar_"))
def cb_comprar(c):
    uid, svc = c.from_user.id, c.data.split("_")[1]; criar_usuario(uid)
    prices={"mercado":0.75,"china":0.70,"outros":0.90}
    names={"mercado":"MercadoPago","china":"China","outros":"Outros"}
    services={"mercado":"cq","china":"ev","outros":"ot"}
    balance=carregar_usuarios()[str(uid)]["saldo"]
    price, name = prices[svc], names[svc]
    if balance<price: return bot.answer_callback_query(c.id,"❌ Saldo insuficiente",True)

    bot.edit_message_text("⏳ Solicitando número...",c.message.chat.id,c.message.message_id)
    resp={}
    for i in range(1,14):
        resp=solicitar_numero(services[svc],max_price=i)
        if resp.get("status")=="success": break
    if resp.get("status")!="success":
        return bot.send_message(c.message.chat.id,"🚫 Sem números disponíveis.")

    aid, num = resp["id"], resp["number"]
    short = num[2:] if num.startswith("55") else num
    adicionar_numero(uid,aid); alterar_saldo(uid,balance-price)

    kb_b=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton("❌ Cancelar (2m)",callback_data=f"cancel_blocked_{aid}")
    )
    text=(f"📦 {name}\n☎️ {num} / {short}\n\n🕘 Prazo:{PRAZO_MINUTOS}min")
    msg=bot.send_message(c.message.chat.id,text,parse_mode="Markdown",reply_markup=kb_b)
    status_map[aid]={"user_id":uid,"price":price,"chat_id":msg.chat.id,
                     "message_id":msg.message_id,"service":name,
                     "full":num,"short":short}

    spawn_sms_thread(aid)

    # countdown
    def countdown():
        for _ in range(PRAZO_MINUTOS):
            time.sleep(60)
            info=status_map.get(aid)
            if not info or info.get("codes") or info.get("canceled_by_user"):
                return
            rem=PRAZO_MINUTOS-(_+1)
            new=(f"📦 {info['service']}\n☎️ {info['full']} / {info['short']}\n\n🕘 Prazo:{rem}min")
            try: bot.edit_message_text(new,info["chat_id"],info["message_id"],parse_mode="Markdown",reply_markup=kb_b)
            except: pass

    # auto-cancel
     def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        with status_lock:
            info = status_map.get(aid)
        # só cancela e devolve se NENHUM SMS foi recebido (codes vazio)
        if info and not info.get("codes") and not info.get("canceled_by_user"):
            cancelar_numero(aid)
            alterar_saldo(
                info["user_id"],
                carregar_usuarios()[str(info["user_id"])]["saldo"] + info["price"]
            )
            # remove a mensagem do chat
            try:
                bot.delete_message(info["chat_id"], info["message_id"])
            except:
                pass

    threading.Thread(target=countdown,daemon=True).start()
    threading.Thread(target=auto_cancel,daemon=True).start()

@bot.callback_query_handler(lambda c:c.data.startswith("retry_"))
def retry_sms(c):
    aid=c.data.split("_",1)[1]
    requests.get(SMSBOWER_URL,params={"api_key":API_KEY_SMSBOWER,"action":"setStatus","status":"3","id":aid},timeout=10)
    bot.answer_callback_query(c.id,"🔄 Novo SMS solicitado.",show_alert=True)
    spawn_sms_thread(aid)

@bot.callback_query_handler(lambda c:c.data.startswith("cancel_blocked_"))
def cancel_blocked(c):
    bot.answer_callback_query(c.id,"⏳ Só após 2 minutos.",show_alert=True)

@bot.callback_query_handler(lambda c:c.data.startswith("cancel_"))
def cancelar_user(c):
    aid=c.data.split("_",1)[1]
    info=status_map.get(aid)
    if not info or info.get("codes"):
        return bot.answer_callback_query(c.id,"❌ Não pode cancelar após SMS.",True)
    info["canceled_by_user"]=True
    cancelar_numero(aid)
    alterar_saldo(info["user_id"],carregar_usuarios()[str(info["user_id"])]["saldo"]+info["price"])
    try: bot.delete_message(info["chat_id"],info["message_id"])
    except: pass
    bot.answer_callback_query(c.id,"✅ Cancelado e saldo devolvido.",show_alert=True)

# === WEBHOOKS ===
@app.route("/",methods=["GET"])
def health(): return "OK",200

@app.route("/webhook/telegram",methods=["POST"])
def tg_webhook():
    upd=telebot.types.Update.de_json(request.get_data().decode())
    bot.process_new_updates([upd]); return "",200

@app.route("/webhook/mercadopago",methods=["POST"])
def mp_webhook():
    d=request.get_json()
    if d.get("type")=="payment":
        pid=d["data"]["id"]
        r=mp_client.payment().get(pid)["response"]
        if r["status"]=="approved":
            uid,amt=map(float,r["external_reference"].split(":"))
            u=carregar_usuarios().get(str(int(uid)),{}).get("saldo",0.0)
            alterar_saldo(int(uid),u+amt)
            bot.send_message(int(uid),f"✅ Recarga R$ {amt:.2f} confirmada! Saldo R$ {u+amt:.2f}")
    return "",200

if __name__=="__main__":
    bot.remove_webhook()
    bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
