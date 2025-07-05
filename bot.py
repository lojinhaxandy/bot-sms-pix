import os
import json
import threading
import time
import logging
import requests
import telebot
import mercadopago
from datetime import datetime
from flask import Flask, request, jsonify

# === CONFIG from environment ===
BOT_TOKEN             = os.getenv('BOT_TOKEN', '8086140451:AAFKRaaiF3yiFCxcmgzA0UhP_XGpOoXTx0c')
ALERT_BOT_TOKEN       = os.getenv('ALERT_BOT_TOKEN', '6883479940:AAG0qtvaBNjoV0o7ugrxYmraqPEwZThmmJc')
ALERT_CHAT_ID         = os.getenv('ALERT_CHAT_ID', '6829680279')
SMSBOWER_API_KEY      = os.getenv('SMSBOWER_API_KEY', '6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G')
SMSBOWER_URL          = 'https://smsbower.online/stubs/handler_api.php'
COUNTRY_ID            = os.getenv('COUNTRY_ID', '73')
MP_ACCESS_TOKEN       = os.getenv('MP_ACCESS_TOKEN', 'APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094')
TELEGRAM_WEBHOOK_URL  = os.getenv('TELEGRAM_WEBHOOK_URL')   # e.g. "https://<your-app>.onrender.com"
MP_WEBHOOK_PATH       = os.getenv('MP_WEBHOOK_PATH', '/mp_webhook')
USERS_FILE            = 'usuarios.json'
PRAZO_MINUTOS         = 23
PRAZO_SEGUNDOS        = PRAZO_MINUTOS * 60

# === TELEGRAM & MP SDK & FLASK ===
bot       = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot = telebot.TeleBot(ALERT_BOT_TOKEN)
mp_sdk    = mercadopago.SDK(MP_ACCESS_TOKEN)
app       = Flask(__name__)

# === LOGGING to Telegram ===
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        try:
            alert_bot.send_message(ALERT_CHAT_ID, self.format(record))
        except:
            pass

logger = logging.getLogger('bot_sms')
logger.setLevel(logging.INFO)
handler = TelegramLogHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

# === STORAGE & STATE ===
data_lock    = threading.Lock()
status_lock  = threading.Lock()
status_map   = {}  # activation_id -> info dict
recharge_map = {}  # preference_id -> {'user_id', 'amount', 'chat_id'}

# === USER DATA FUNCTIONS ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'w') as f:
                json.dump({}, f)
        with open(USERS_FILE, 'r') as f:
            return json.load(f)

def salvar_usuarios(us):
    with data_lock:
        with open(USERS_FILE, 'w') as f:
            json.dump(us, f, indent=2)

def criar_usuario(uid):
    us = carregar_usuarios()
    if str(uid) not in us:
        us[str(uid)] = {'saldo': 0.0, 'numeros': []}
        salvar_usuarios(us)

def alterar_saldo(uid, novo):
    us = carregar_usuarios()
    us.setdefault(str(uid), {'saldo': 0.0, 'numeros': []})['saldo'] = novo
    salvar_usuarios(us)

def adicionar_numero(uid, aid):
    us = carregar_usuarios()
    user = us.setdefault(str(uid), {'saldo': 0.0, 'numeros': []})
    if aid not in user['numeros']:
        user['numeros'].append(aid)
        salvar_usuarios(us)

# === SMSBOWER API FUNCTIONS ===
def solicitar_numero(servico, max_price=None):
    params = {
        'api_key': SMSBOWER_API_KEY,
        'action': 'getNumber',
        'service': servico,
        'country': COUNTRY_ID
    }
    if max_price:
        params['maxPrice'] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        txt = r.text.strip()
        logger.info(f"GET_NUMBER → {r.url} → {txt}")
    except Exception as e:
        logger.error(f'Erro getNumber: {e}')
        return {'status':'error','message':str(e)}
    if txt.startswith('ACCESS_NUMBER:'):
        _, aid, num = txt.split(':', 2)
        return {'status':'success','id':aid,'number':num}
    return {'status':'error','message':txt}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL, params={
            'api_key': SMSBOWER_API_KEY,
            'action': 'setStatus',
            'status': '8',
            'id': aid
        }, timeout=10)
    except:
        pass

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL, params={
            'api_key': SMSBOWER_API_KEY,
            'action': 'getStatus',
            'id': aid
        }, timeout=10)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f'Erro getStatus: {e}')
        return None

# === SPAWN SMS CHECKER THREAD (with history) ===
def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info:
        return
    service    = info['service']
    full       = info['full']
    short      = info['short']
    chat_id    = info['chat_id']
    sms_msg_id = info.get('sms_message_id')
    codes      = info.setdefault('codes', [])

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            s = obter_status(aid)
            logger.info(f"CheckSMS → {aid}: {s}")
            if not s:
                time.sleep(5); continue

            if s.startswith('STATUS_OK:') or s.startswith('ACCESS_ACTIVATION:'):
                code = s.split(':', 1)[1]
            elif s.startswith('STATUS_WAIT_RETRY:'):
                code = s.split(':', 1)[1]
            elif s == 'STATUS_CANCEL':
                u = status_map[aid]
                alterar_saldo(u['user_id'],
                              carregar_usuarios()[str(u['user_id'])]['saldo'] + u['price'])
                bot.send_message(chat_id,
                    f"❌ Provedor cancelou. R${u['price']:.2f} devolvido.")
                info['processed'] = True
                return
            else:
                time.sleep(5); continue

            if code in codes:
                return
            codes.append(code)

            rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            sms_lines = "\n".join([f"📩 SMS: `{c}`" for c in codes])
            text = (
                f"📦 {service}\n"
                f"☎️ Número: `{full}`\n"
                f"☎️ Sem DDI: `{short}`\n\n"
                f"{sms_lines}\n"
                f"🕘 {rt}"
            )
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton('📲 Receber outro SMS',
                                                      callback_data=f'retry_{aid}'))

            if sms_msg_id is None:
                msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=kb)
                info['sms_message_id'] = msg.message_id
            else:
                bot.edit_message_text(text, chat_id, sms_msg_id,
                                      parse_mode='Markdown', reply_markup=kb)
            info['processed'] = True
            return

    threading.Thread(target=check_sms, daemon=True).start()

# === FLASK ENDPOINTS ===
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = telebot.types.Update.de_json(request.get_json(force=True))
    bot.process_new_updates([update])
    return jsonify({'ok': True})

@app.route(MP_WEBHOOK_PATH, methods=['POST'])
def mp_webhook():
    data = request.get_json(force=True)
    logger.info(f"MP Webhook → {data}")
    if data.get('type') == 'payment':
        pid = data['data']['id']
        pay = mp_sdk.payment().get(pid)['response']
        if pay.get('status') == 'approved':
            pref_id = pay.get('preference_id')
            ref     = recharge_map.pop(pref_id, None)
            if ref:
                uid  = ref['user_id']
                amt  = ref['amount']
                chat = ref['chat_id']
                criar_usuario(uid)
                alterar_saldo(uid, carregar_usuarios()[str(uid)]['saldo'] + amt)
                bot.send_message(chat,
                    f"✅ Pagamento de R${amt:.2f} aprovado! Saldo atualizado.")
    return jsonify({'status':'ok'})

# === TELEGRAM HANDLERS (webhook mode) ===
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

@bot.callback_query_handler(lambda c: c.data == 'menu_saldo')
def h_saldo(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    saldo = carregar_usuarios()[str(c.from_user.id)]['saldo']
    bot.send_message(c.message.chat.id, f"💰 Saldo: R$ {saldo:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_recarregar')
def h_recarregar(c):
    bot.answer_callback_query(c.id)
    msg = bot.send_message(c.message.chat.id,
                           '💳 Quanto deseja recarregar? Digite o valor (ex: 10.50):')
    bot.register_next_step_handler(msg, process_recharge_amount)

@bot.callback_query_handler(lambda c: c.data == 'menu_numeros')
def h_numeros(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    lst = carregar_usuarios()[str(c.from_user.id)]['numeros']
    if not lst:
        bot.send_message(c.message.chat.id, '📭 Você não tem números ativos.')
    else:
        text = '📋 *Seus números:*\n'
        with status_lock:
            for aid in lst:
                inf = status_map.get(aid)
                if inf and not inf['processed']:
                    text += f"\n*ID:* `{aid}`\n`{inf['full']}` / `{inf['short']}`\n"
        bot.send_message(c.message.chat.id, text, parse_mode='Markdown')
    send_menu(c.message.chat.id)

def process_recharge_amount(m):
    try:
        amt = float(m.text.replace(',', '.'))
    except:
        return bot.reply_to(m, '❌ Valor inválido. Use ex: 10.50')
    pref_data = {
        "items": [{"title": "Recarga de saldo", "quantity": 1, "unit_price": amt}],
        "external_reference": f"{m.from_user.id}_{int(time.time())}",
        "back_urls": {"success": TELEGRAM_WEBHOOK_URL + MP_WEBHOOK_PATH},
        "auto_return": "approved"
    }
    pref = mp_sdk.preference().create(pref_data)["response"]
    recharge_map[pref["id"]] = {"user_id": m.from_user.id, "amount": amt, "chat_id": m.chat.id}
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton(f'💳 Pagar R${amt:.2f}', url=pref["init_point"]))
    bot.send_message(m.chat.id,
        f"🎉 Pague R${amt:.2f} via Mercado Pago. Você será creditado automaticamente.",
        reply_markup=kb)
    send_menu(m.chat.id)

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
    # ... same purchase logic with spawn_sms_thread ...
    pass

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    # ... same retry logic ...
    pass

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    bot.answer_callback_query(c.id, '⏳ Disponível após 2 minutos.', show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    # ... same cancel logic ...
    pass

# === SET WEBHOOK & RUN ===
if __name__ == '__main__':
    if TELEGRAM_WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=TELEGRAM_WEBHOOK_URL + '/telegram_webhook')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
