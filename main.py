# app.py

import os
import json
import threading
import time
import logging
import requests

from datetime import datetime
from flask import Flask, request

import telebot
import mercadopago

# === CONFIG FROM ENV ===
BOT_TOKEN         = os.getenv('BOT_TOKEN')
ALERT_BOT_TOKEN   = os.getenv('ALERT_BOT_TOKEN')
ALERT_CHAT_ID     = os.getenv('ALERT_CHAT_ID')
API_KEY_SMSBOWER  = os.getenv('API_KEY_SMSBOWER')
SMSBOWER_URL      = 'https://smsbower.online/stubs/handler_api.php'
COUNTRY_ID        = '73'   # Brasil
MP_ACCESS_TOKEN   = os.getenv('MP_ACCESS_TOKEN')
SITE_URL          = os.getenv('SITE_URL').rstrip('/')

# === INIT BOTS & SDKS ===
bot         = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot   = telebot.TeleBot(ALERT_BOT_TOKEN)
mp_client   = mercadopago.SDK(MP_ACCESS_TOKEN)

# === FLASK APP ===
app = Flask(__name__)

# --- Logging to Telegram ---
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        try:
            alert_bot.send_message(ALERT_CHAT_ID, msg)
        except:
            pass

logger = logging.getLogger('bot_sms')
logger.setLevel(logging.INFO)
handler = TelegramLogHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

# --- Thread locks & state ---
USERS_FILE     = 'usuarios.json'
data_lock      = threading.Lock()
status_lock    = threading.Lock()
status_map     = {}   # activation_id → info dict
PRAZO_MINUTOS  = 23
PRAZO_SEGUNDOS = PRAZO_MINUTOS * 60

# === HELPER FUNCTIONS ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'w') as f:
                json.dump({}, f)
        with open(USERS_FILE, 'r') as f:
            return json.load(f)

def salvar_usuarios(usuarios):
    with data_lock:
        with open(USERS_FILE, 'w') as f:
            json.dump(usuarios, f, indent=2)

def criar_usuario(uid):
    usuarios = carregar_usuarios()
    if str(uid) not in usuarios:
        usuarios[str(uid)] = {'saldo': 0.0, 'numeros': []}
        salvar_usuarios(usuarios)
        logger.info(f'Novo usuário criado: {uid}')

def alterar_saldo(uid, novo):
    usuarios = carregar_usuarios()
    usuarios.setdefault(str(uid), {'saldo': 0.0, 'numeros': []})['saldo'] = novo
    salvar_usuarios(usuarios)
    logger.info(f'Saldo de {uid} = R$ {novo:.2f}')

def adicionar_numero(uid, aid):
    usuarios = carregar_usuarios()
    user = usuarios.setdefault(str(uid), {'saldo': 0.0, 'numeros': []})
    if aid not in user['numeros']:
        user['numeros'].append(aid)
        salvar_usuarios(usuarios)
        logger.info(f'Adicionado número {aid} a {uid}')

# --- SMSBOWER API ---
def solicitar_numero(servico, max_price=None):
    params = {'api_key': API_KEY_SMSBOWER, 'action': 'getNumber', 'service': servico, 'country': COUNTRY_ID}
    if max_price:
        params['maxPrice'] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        logger.info(f"GET_NUMBER → {r.url} → {text}")
    except Exception as e:
        logger.error(f'Erro getNumber: {e}')
        return {'status':'error','message':str(e)}
    if text.startswith('ACCESS_NUMBER:'):
        _, aid, num = text.split(':',2)
        logger.info(f'Número {num} (ID {aid}) recebido')
        return {'status':'success','id':aid,'number':num}
    return {'status':'error','message':text}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL, params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'8','id':aid},timeout=10)
        logger.info(f'Cancelado provider → {aid}')
    except Exception as e:
        logger.error(f'Erro cancelar: {e}')

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL, params={'api_key':API_KEY_SMSBOWER,'action':'getStatus','id':aid}, timeout=10)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f'Erro getStatus: {e}')
        return None

def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info:
        return

    service = info['service']
    full    = info['full']
    short   = info['short']
    chat_id = info['chat_id']
    sms_msg_id = info.get('sms_message_id')

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            s = obter_status(aid)
            logger.info(f"CheckSMS → {aid}: {s}")
            if not s:
                time.sleep(5)
                continue

            code = None
            if s.startswith('STATUS_OK:') or s.startswith('ACCESS_ACTIVATION:'):
                code = s.split(':',1)[1]
            elif s.startswith('STATUS_WAIT_RETRY:'):
                code = s.split(':',1)[1]
            elif s == 'STATUS_CANCEL':
                with status_lock:
                    u = status_map[aid]
                alterar_saldo(u['user_id'], carregar_usuarios()[str(u['user_id'])]['saldo'] + u['price'])
                bot.send_message(chat_id, f"❌ Cancelou. R${u['price']:.2f} devolvido.")
                with status_lock:
                    info['processed'] = True
                return
            else:
                time.sleep(5)
                continue

            rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            text = (
                f"📦 {service}\\n"
                f"☎️ Número: `{full}`\\n"
                f"☎️ Sem DDI: `{short}`\\n\\n"
                f"📩 SMS: `{code}`\\n"
                f"🕘 {rt}"
            )
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))

            if sms_msg_id is None:
                msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=kb)
                with status_lock:
                    info['sms_message_id'] = msg.message_id
            else:
                bot.edit_message_text(text, chat_id, sms_msg_id, parse_mode='Markdown', reply_markup=kb)

            with status_lock:
                info['processed'] = True
            return

    threading.Thread(target=check_sms, daemon=True).start()

# === MENU & HANDLERS ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('💰 Saldo',            callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar',        callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('📜 Meus números',      callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte',           url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id, 'Escolha uma opção:', reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def default_menu(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_recarregar')
def menu_recarregar(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)

    pref_data = {
        "items": [{"title": "Recarga de saldo", "quantity": 1, "unit_price": 1.00}],
        "external_reference": str(c.from_user.id),
        "back_urls": {"success": f"{SITE_URL}/?paid=success", "failure": f"{SITE_URL}/?paid=failure", "pending": f"{SITE_URL}/?paid=pending"},
        "auto_return": "approved"
    }
    pref = mp_client.preference().create(pref_data)
    pay_url = pref["response"]["init_point"]
    bot.send_message(c.message.chat.id, f"💳 Para recarregar, acesse este link:\\n{pay_url}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_saldo')
def menu_saldo(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    u = carregar_usuarios()[str(c.from_user.id)]
    bot.send_message(c.message.chat.id, f"💰 Saldo: R$ {u['saldo']:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_numeros')
def menu_numeros(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    lst = carregar_usuarios()[str(c.from_user.id)]['numeros']
    if not lst:
        bot.send_message(c.message.chat.id, '📭 Você não tem números ativos.')
    else:
        text = '📋 *Seus números:*\\n'
        with status_lock:
            for aid in lst:
                inf = status_map.get(aid)
                if inf and not inf['processed']:
                    text += f"\\n*ID:* `{aid}`\\n`{inf['full']}` / `{inf['short']}`\\n"
        bot.send_message(c.message.chat.id, text, parse_mode='Markdown')
    send_menu(c.message.chat.id)

@bot.message_handler(commands=['comprar'])
def cmd_comprar(m):
    criar_usuario(m.from_user.id)
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Mercado Pago SMS - R$0.75', callback_data='comprar_mercado'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS para China   - R$0.70', callback_data='comprar_china'),
        telebot.types.InlineKeyboardButton('📡 Outros SMS        - R$0.90', callback_data='comprar_outros')
    )
    bot.send_message(m.chat.id, 'Escolha serviço:', reply_markup=kb)

@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    user_id, serv = c.from_user.id, c.data.split('_')[1]
    criar_usuario(user_id)
    precos = {'mercado':0.75,'china':0.70,'outros':0.90}
    nomes  = {'mercado':'Mercado Pago SMS','china':'SMS para China','outros':'Outros SMS'}
    idsms  = {'mercado':'cq','china':'ev','outros':'ot'}
    saldo  = carregar_usuarios()[str(user_id)]['saldo']
    price, service = precos[serv], nomes[serv]
    if saldo < price:
        return bot.answer_callback_query(c.id, '❌ Saldo insuficiente.', True)
    bot.edit_message_text('⏳ Solicitando número...', c.message.chat.id, c.message.message_id)
    resp = {}
    for max_p in range(1, 14):
        resp = solicitar_numero(idsms[serv], max_price=max_p)
        if resp.get('status') == 'success':
            break
    if resp.get('status') != 'success':
        return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')
    aid, full = resp['id'], resp['number']
    short = full[2:] if full.startswith('55') else full
    adicionar_numero(user_id, aid)
    alterar_saldo(user_id, saldo - price)
    kb_blocked = telebot.types.InlineKeyboardMarkup()
    kb_blocked.add(telebot.types.InlineKeyboardButton('❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}'))
    kb_unlocked = telebot.types.InlineKeyboardMarkup()
    kb_unlocked.add(telebot.types.InlineKeyboardButton('❌ Cancelar', callback_data=f'cancel_{aid}'))
    text = (
        f"📦 {service}\\n"
        f"☎️ Número: `{full}`\\n"
        f"☎️ Sem DDI: `{short}`\\n\\n"
        f"🕘 Prazo: {PRAZO_MINUTOS} minutos\\n\\n"
        f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
    )
    msg = bot.send_message(c.message.chat.id, text, parse_mode='Markdown', reply_markup=kb_blocked)
    with status_lock:
        status_map[aid] = {
            'user_id':    user_id,
            'price':      price,
            'chat_id':    msg.chat.id,
            'message_id': msg.message_id,
            'service':    service,
            'full':       full,
            'short':      short,
            'processed':  False,
            'sms_message_id': None
        }
    spawn_sms_thread(aid)
    def countdown():
        remaining = PRAZO_MINUTOS
        for minute in range(PRAZO_MINUTOS):
            time.sleep(60)
            remaining -= 1
            new_text = (
                f"📦 {service}\\n"
                f"☎️ Número: `{full}`\\n"
                f"☎️ Sem DDI: `{short}`\\n\\n"
                f"🕘 Prazo: {remaining} minuto{'s' if remaining != 1 else ''}\\n\\n"
                f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
            )
            markup = kb_unlocked if minute >= 2 else kb_blocked
            try:
                bot.edit_message_text(new_text, msg.chat.id, msg.message_id, parse_mode='Markdown', reply_markup=markup)
            except:
                pass
    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        with status_lock:
            info = status_map.get(aid)
        if info and not info['processed']:
            cancelar_numero(aid)
            alterar_saldo(info['user_id'], carregar_usuarios()[str(info['user_id'])]['saldo'] + info['price'])
            bot.send_message(info['chat_id'], f"❌ Sem SMS em {PRAZO_MINUTOS} minutos. Cancelado e R${info['price']:.2f} devolvido.")
            with status_lock:
                info['processed'] = True
    threading.Thread(target=countdown, daemon=True).start()
    threading.Thread(target=auto_cancel, daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    aid = c.data.split('_',1)[1]
    try:
        requests.get(SMSBOWER_URL, params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'3','id':aid}, timeout=10)
        bot.answer_callback_query(c.id, '🔄 Novo SMS solicitado.', show_alert=True)
        with status_lock:
            info = status_map.get(aid)
            if info: info['processed'] = False
        spawn_sms_thread(aid)
    except Exception as e:
        logger.error(f'Retry error: {e}')
        bot.answer_callback_query(c.id, '❌ Falha ao solicitar retry.', show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    bot.answer_callback_query(c.id, '⏳ Disponível após 2 minutos.', show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    aid = c.data.split('_',1)[1]
    with status_lock:
        info = status_map.get(aid)
    if not info or info['processed']:
        return bot.answer_callback_query(c.id, '❌ Não é possível cancelar.', True)
    cancelar_numero(aid)
    alterar_saldo(info['user_id'], carregar_usuarios()[str(info['user_id'])]['saldo'] + info['price'])
    bot.edit_message_text('❌ Cancelado pelo usuário.', info['chat_id'], info['message_id'])
    bot.answer_callback_query(c.id, 'Número cancelado e saldo devolvido.', show_alert=True)
    with status_lock:
        info['processed'] = True

# === WEBHOOK ENDPOINTS ===

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    json_str = request.get_data().decode('utf-8')
    update   = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '', 200

@app.route('/webhook/mercadopago', methods=['POST'])
def mp_webhook():
    data = request.get_json()
    if data.get('type') == 'payment':
        payment_id = data['data']['id']
        payment = mp_client.payment().get(payment_id)["response"]
        if payment["status"] == "approved":
            chat_id = int(payment["external_reference"])
            amount  = float(payment["transaction_amount"])
            current = carregar_usuarios().get(str(chat_id),{}).get('saldo',0.0)
            alterar_saldo(chat_id, current + amount)
            bot.send_message(chat_id, f"✅ Pagamento de R$ {amount:.2f} confirmado!\\nSeu novo saldo é R$ {current + amount:.2f}")
    return '', 200

# === BOOTSTRAP ===

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
