# app.py

import os
import json
import threading
import time
import logging
import requests
import re

from datetime import datetime
from flask import Flask, request

import telebot
import mercadopago

# === CONFIGURAÇÃO VIA ENV ===
BOT_TOKEN         = os.getenv("BOT_TOKEN")
ALERT_BOT_TOKEN   = os.getenv("ALERT_BOT_TOKEN")
ALERT_CHAT_ID     = os.getenv("ALERT_CHAT_ID")
API_KEY_SMSBOWER  = os.getenv("API_KEY_SMSBOWER")
SMSBOWER_URL      = "https://smsbower.online/stubs/handler_api.php"
COUNTRY_ID        = "73"
MP_ACCESS_TOKEN   = os.getenv("MP_ACCESS_TOKEN")
SITE_URL          = os.getenv("SITE_URL").rstrip('/')
# Bot para backup de usuarios.json
BACKUP_BOT_TOKEN  = '7982928818:AAEPf9AgnSEqEL7Ay5UaMPyG27h59PdGUYs'
BACKUP_CHAT_ID    = '6829680279'

# === INIT BOTS & SDKs ===
bot         = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot   = telebot.TeleBot(ALERT_BOT_TOKEN)
backup_bot  = telebot.TeleBot(BACKUP_BOT_TOKEN)
mp_client   = mercadopago.SDK(MP_ACCESS_TOKEN)

# === FLASK APP ===
app = Flask(__name__)

# --- Logger para alertas no Telegram ---
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        try:
            alert_bot.send_message(ALERT_CHAT_ID, msg)
        except:
            pass

logger = logging.getLogger("bot_sms")
logger.setLevel(logging.INFO)
handler = TelegramLogHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)

# --- Estado e locks ---
USERS_FILE       = "usuarios.json"
data_lock        = threading.Lock()
status_lock      = threading.Lock()
status_map       = {}            # aid -> activation info
PENDING_RECHARGE = {}            # user_id -> aguardando valor
PRAZO_MINUTOS    = 23
PRAZO_SEGUNDOS   = PRAZO_MINUTOS * 60

# === Funções de usuário ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE, "w") as f:
                json.dump({}, f)
        with open(USERS_FILE, "r") as f:
            return json.load(f)

def salvar_usuarios(u):
    with data_lock:
        with open(USERS_FILE, "w") as f:
            json.dump(u, f, indent=2)
    # Backup via Telegram
    try:
        with open(USERS_FILE, "rb") as bf:
            backup_bot.send_document(BACKUP_CHAT_ID, bf)
    except Exception as e:
        logger.error(f"Erro ao enviar backup: {e}")

def criar_usuario(uid):
    u = carregar_usuarios()
    if str(uid) not in u:
        u[str(uid)] = {"saldo": 0.0, "numeros": []}
        salvar_usuarios(u)
        logger.info(f"Novo usuário criado: {uid}")

def alterar_saldo(uid, novo):
    u = carregar_usuarios()
    u.setdefault(str(uid), {"saldo":0.0,"numeros":[]})["saldo"] = novo
    salvar_usuarios(u)
    logger.info(f"Saldo de {uid} = R$ {novo:.2f}")

def adicionar_numero(uid, aid):
    u = carregar_usuarios()
    user = u.setdefault(str(uid), {"saldo":0.0,"numeros":[]})
    if aid not in user["numeros"]:
        user["numeros"].append(aid)
        salvar_usuarios(u)
        logger.info(f"Número {aid} adicionado a {uid}")

# === Integração com SMSBOWER ===
def solicitar_numero(servico, max_price=None):
    params = {
        "api_key": API_KEY_SMSBOWER,
        "action": "getNumber",
        "service": servico,
        "country": COUNTRY_ID
    }
    if max_price:
        params["maxPrice"] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        logger.info(f"GET_NUMBER → {text}")
    except Exception as e:
        logger.error(f"Erro getNumber: {e}")
        return {"status":"error","message":str(e)}
    if text.startswith("ACCESS_NUMBER:"):
        _, aid, num = text.split(":", 2)
        return {"status":"success","id":aid,"number":num}
    return {"status":"error","message":text}

def cancelar_numero(aid):
    try:
        requests.get(
            SMSBOWER_URL,
            params={
                "api_key": API_KEY_SMSBOWER,
                "action": "setStatus",
                "status": "8",
                "id": aid
            },
            timeout=10
        )
        logger.info(f"Cancelado provider: {aid}")
    except Exception as e:
        logger.error(f"Erro cancelar: {e}")

def obter_status(aid):
    try:
        r = requests.get(
            SMSBOWER_URL,
            params={
                "api_key": API_KEY_SMSBOWER,
                "action": "getStatus",
                "id": aid
            },
            timeout=10
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus: {e}")
        return None

# === Thread para monitorar SMS ===
def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info:
        return

    service = info["service"]
    full    = info["full"]
    short   = info["short"]
    chat_id = info["chat_id"]
    msg_id  = info.get("sms_message_id")
    info.setdefault("codes", [])
    info["canceled_by_user"] = False

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            status = obter_status(aid)
            if info["canceled_by_user"]:
                return
            if not status or status.startswith("STATUS_WAIT"):
                time.sleep(5)
                continue
            if status == "STATUS_CANCEL":
                if not info["codes"]:
                    alterar_saldo(
                        info["user_id"],
                        carregar_usuarios()[str(info["user_id"])]["saldo"] + info["price"]
                    )
                    bot.send_message(
                        chat_id,
                        f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido."
                    )
                return
            code = status.split(":",1)[1] if ":" in status else status
            if code not in info["codes"]:
                info["codes"].append(code)
                rt = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                text = (
                    f"📦 {service}\n"
                    f"☎️ Número: `{full}`\n"
                    f"☎️ Sem DDI: `{short}`\n\n"
                )
                for idx, cd in enumerate(info["codes"],1):
                    text += f"📩 SMS{idx}: `{cd}`\n"
                text += f"🕘 {rt}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.row(
                    telebot.types.InlineKeyboardButton(
                        "📲 Receber outro SMS", callback_data=f"retry_{aid}"
                    )
                )
                kb.row(
                    telebot.types.InlineKeyboardButton(
                        "📲 Comprar serviço", callback_data="menu_comprar"
                    ),
                    telebot.types.InlineKeyboardButton(
                        "📜 Menu", callback_data="menu"
                    )
                )
                if msg_id:
                    bot.edit_message_text(
                        text, chat_id, msg_id,
                        parse_mode="Markdown",
                        reply_markup=kb
                    )
                else:
                    m = bot.send_message(
                        chat_id, text,
                        parse_mode="Markdown", reply_markup=kb
                    )
                    info["sms_message_id"] = m.message_id
            time.sleep(5)

    threading.Thread(target=check_sms, daemon=True).start()

# === Menu e handlers ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton("📲 Comprar serviços", callback_data="menu_comprar"),
        telebot.types.InlineKeyboardButton("💰 Saldo", callback_data="menu_saldo"),
        telebot.types.InlineKeyboardButton("🤑 Recarregar", callback_data="menu_recarregar"),
        telebot.types.InlineKeyboardButton("📜 Meus números", callback_data="menu_numeros"),
        telebot.types.InlineKeyboardButton("🆘 Suporte", url="https://t.me/cpfbotttchina")
    )
    bot.send_message(chat_id, "Escolha uma opção:", reply_markup=kb)

@bot.callback_query_handler(lambda c: c.data=="menu")
def callback_menu(c):
    bot.answer_callback_query(c.id)
    send_menu(c.message.chat.id)

@bot.message_handler(commands=["start"])
def cmd_start(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data=="menu_recarregar")
def menu_recarregar(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    PENDING_RECHARGE[c.from_user.id] = True
    bot.send_message(c.message.chat.id, "Digite o valor (em reais) que deseja recarregar:")

@bot.message_handler(func=lambda m: PENDING_RECHARGE.get(m.from_user.id) and re.fullmatch(r"\d+(\.\d{1,2})?", m.text))
def handle_recharge_amount(m):
    uid = m.from_user.id
    amount = float(m.text)
    PENDING_RECHARGE.pop(uid, None)

    pref = mp_client.preference().create({
        "items":[{"title":"Recarga de saldo","quantity":1,"unit_price":amount}],
        "external_reference":f"{uid}:{amount}",
        "back_urls":{
            "success":f"{SITE_URL}/?paid=success",
            "failure":f"{SITE_URL}/?paid=failure",
            "pending":f"{SITE_URL}/?paid=pending"
        },
        "auto_return":"approved"
    })
    pay_url = pref["response"]["init_point"]
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(f"💳 Pagar R$ {amount:.2f}", url=pay_url))
    kb.row(
        telebot.types.InlineKeyboardButton("📲 Comprar serviço", callback_data="menu_comprar"),
        telebot.types.InlineKeyboardButton("📜 Menu", callback_data="menu")
    )
    bot.send_message(m.chat.id, f"Para recarregar R$ {amount:.2f}, clique abaixo:", reply_markup=kb)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data=="menu_saldo")
def menu_saldo(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    s = carregar_usuarios()[str(c.from_user.id)]["saldo"]
    bot.send_message(c.message.chat.id, f"💰 Saldo: R$ {s:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data=="menu_numeros")
def menu_numeros(c):
    bot.answer_callback
