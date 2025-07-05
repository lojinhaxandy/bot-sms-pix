import os
import json
import threading
import time
import logging
import requests
from datetime import datetime

from flask import Flask, request, jsonify
import telebot
import mercadopago
from telebot.apihelper import ApiTelegramException

# === CONFIGURAÇÃO via ENVIRONMENT VARIABLES ===
BOT_TOKEN            = os.getenv('BOT_TOKEN')
ALERT_BOT_TOKEN      = os.getenv('ALERT_BOT_TOKEN')
ALERT_CHAT_ID        = os.getenv('ALERT_CHAT_ID')
SMSBOWER_API_KEY     = os.getenv('SMSBOWER_API_KEY')
COUNTRY_ID           = os.getenv('COUNTRY_ID', '73')
MP_ACCESS_TOKEN      = os.getenv('MP_ACCESS_TOKEN')
TELEGRAM_WEBHOOK_URL = os.getenv('TELEGRAM_WEBHOOK_URL')       # ex: https://bot-sms-pix.onrender.com
MP_WEBHOOK_PATH      = os.getenv('MP_WEBHOOK_PATH', '/mp_webhook')
INFO_BOT_TOKEN       = os.getenv('INFO_BOT_TOKEN')
INFO_CHAT_ID         = os.getenv('INFO_CHAT_ID')

# === ARQUIVOS de dados ===
USERS_FILE       = 'usuarios.json'
RECHARGES_FILE   = 'recharges.json'

# === Constantes ===
SMSBOWER_URL     = 'https://smsbower.online/stubs/handler_api.php'
PRAZO_MINUTOS    = 23
PRAZO_SEGUNDOS   = PRAZO_MINUTOS * 60

# === CLIENTES e APP ===
bot       = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot = telebot.TeleBot(ALERT_BOT_TOKEN)
mp_sdk    = mercadopago.SDK(MP_ACCESS_TOKEN)
app       = Flask(__name__)

# === LOGGING para Telegram (alertas) ===
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        try:
            alert_bot.send_message(ALERT_CHAT_ID, self.format(record))
        except:
            pass

logger = logging.getLogger('bot_sms')
logger.setLevel(logging.INFO)
handler = TelegramLogHandler()
handler.setLevel(logging.WARNING)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

# === Helper para persistir recargas ===
def carregar_recargas():
    if not os.path.exists(RECHARGES_FILE):
        with open(RECHARGES_FILE, 'w') as f:
            json.dump([], f)
    with open(RECHARGES_FILE, 'r') as f:
        return json.load(f)

def salvar_recargas(recargas):
    with open(RECHARGES_FILE, 'w') as f:
        json.dump(recargas, f, indent=2)

def adicionar_recarga(pref_id, ext_ref, user_id, amount, chat_id):
    recs = carregar_recargas()
    recs.append({
        'pref_id': pref_id,
        'ext_ref': ext_ref,
        'user_id': user_id,
        'amount': amount,
        'chat_id': chat_id
    })
    salvar_recargas(recs)

def remover_recarga(pref_id=None, ext_ref=None):
    recs = carregar_recargas()
    for r in recs:
        if (pref_id and r['pref_id'] == pref_id) or (ext_ref and r['ext_ref'] == ext_ref):
            recs.remove(r)
            salvar_recargas(recs)
            return r
    return None

# === HELPERS ===
def safe_answer_callback(query_id, text=None, show_alert=False):
    try:
        bot.answer_callback_query(query_id, text=text, show_alert=show_alert)
    except ApiTelegramException as e:
        logger.warning(f"Resposta callback falhou: {e}")

def send_info_bot(text: str):
    if not INFO_BOT_TOKEN or not INFO_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{INFO_BOT_TOKEN}/sendMessage",
            json={"chat_id": INFO_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=5
        ).raise_for_status()
    except Exception as e:
        logger.error(f"Falha ao notificar info bot: {e}")

# === DADOS DO USUÁRIO ===
data_lock = threading.Lock()
status_lock = threading.Lock()
status_map = {}

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
    u = us.setdefault(str(uid), {'saldo': 0.0, 'numeros': []})
    if aid not in u['numeros']:
        u['numeros'].append(aid)
        salvar_usuarios(us)

# === SMSBOWER API ===
def solicitar_numero(servico, max_price=None):
    params = {'api_key': SMSBOWER_API_KEY, 'action': 'getNumber', 'service': servico, 'country': COUNTRY_ID}
    if max_price: params['maxPrice'] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        txt = r.text.strip()
    except Exception as e:
        logger.error(f"getNumber erro: {e}")
        return {'status': 'error', 'message': str(e)}
    if txt.startswith('ACCESS_NUMBER:'):
        _, aid, num = txt.split(':', 2)
        return {'status': 'success', 'id': aid, 'number': num}
    return {'status': 'error', 'message': txt}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL, params={
            'api_key': SMSBOWER_API_KEY, 'action': 'setStatus', 'status': '8', 'id': aid
        }, timeout=10)
    except Exception as e:
        logger.error(f"cancel erro: {e}")

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL, params={'api_key': SMSBOWER_API_KEY, 'action': 'getStatus', 'id': aid}, timeout=10)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"getStatus erro: {e}")
        return None

# === THREAD PARA SMS ===
def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info:
        return
    service, full, short, chat_id = info['service'], info['full'], info['short'], info['chat_id']
    sms_msg_id = info.get('sms_message_id')
    codes = info.setdefault('codes', [])

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            s = obter_status(aid)
            if not s:
                time.sleep(5)
                continue
            if s == 'STATUS_CANCEL':
                u = status_map[aid]
                alterar_saldo(u['user_id'], carregar_usuarios()[str(u['user_id'])]['saldo'] + u['price'])
                bot.send_message(chat_id, f"❌ Cancelado pelo provedor. R${u['price']:.2f} devolvido.")
                info['processed'] = True
                return
            if any(s.startswith(pref) for pref in ('STATUS_OK:', 'ACCESS_ACTIVATION:', 'STATUS_WAIT_RETRY:')):
                code = s.split(':', 1)[1]
            else:
                time.sleep(5)
                continue

            if code in codes:
                return
            codes.append(code)

            rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            text = f"📦 {service}\n☎️ `{full}` / `{short}`\n\n" + "\n".join(f"📩 `{c}`" for c in codes) + f"\n🕘 {rt}"
            kb = telebot.types.InlineKeyboardMarkup().add(
                telebot.types.InlineKeyboardButton('📲 Outro SMS', callback_data=f'retry_{aid}')
            )
            if sms_msg_id is None:
                msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=kb)
                info['sms_message_id'] = msg.message_id
            else:
                bot.edit_message_text(text, chat_id, sms_msg_id, parse_mode='Markdown', reply_markup=kb)
            info['processed'] = True
            return

    threading.Thread(target=check_sms, daemon=True).start()

# === FLASK WEBHOOKS ===
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    upd = telebot.types.Update.de_json(request.get_json(force=True))
    bot.process_new_updates([upd])
    return jsonify({'ok': True})

@app.route(MP_WEBHOOK_PATH, methods=['POST'])
def mp_webhook():
    data = request.get_json(force=True)
    logger.info(f"=== MP WEBHOOK RECEIVED ===\n{json.dumps(data, indent=2)}")

    if data.get('type') == 'payment':
        payment_id = data['data']['id']
        pay = mp_sdk.payment().get(payment_id)['response']
        status = pay.get('status')
        pref_id = pay.get('preference_id')
        ext_ref = pay.get('external_reference')
        logger.info(f"Payment {payment_id}: status={status}, pref_id={pref_id}, ext_ref={ext_ref}")

        # remove e obtém a recarga persistida
        ref = remover_recarga(pref_id=pref_id) or remover_recarga(ext_ref=ext_ref)
        if not ref:
            logger.warning(f"No recharge found for pref_id={pref_id} ext_ref={ext_ref}")
        elif status == 'approved':
            uid, amt, chat = ref['user_id'], ref['amount'], ref['chat_id']
            criar_usuario(uid)
            before = carregar_usuarios()[str(uid)]['saldo']
            alterar_saldo(uid, before + amt)
            after = carregar_usuarios()[str(uid)]['saldo']

            bot.send_message(
                chat,
                f"✅ Recarga de *R${amt:.2f}* aprovada!\nSaldo: R${before:.2f} → R${after:.2f}",
                parse_mode='Markdown'
            )
            send_info_bot(f"💰 *Recarga aprovada*\nUsuário: `{uid}`\nValor: R${amt:.2f}\nSaldo: R${after:.2f}")
        else:
            logger.info(f"Pagamento {payment_id} status `{status}`, aguardando approved.")

    return jsonify({'status':'ok'})

# === TELEGRAM HANDLERS ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('💰 Saldo',               callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar',           callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('📜 Meus números',         callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte',              url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id, 'Escolha uma opção:', reply_markup=kb)

def show_purchase_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 MercadoPago SMS - R$0.75', callback_data='comprar_mercado'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS China       - R$0.70', callback_data='comprar_china'),
        telebot.types.InlineKeyboardButton('📡 Outros SMS      - R$0.90', callback_data='comprar_outros')
    )
    bot.send_message(chat_id, 'Escolha o serviço:', reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def default_menu(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_comprar')
def menu_comprar(c):
    safe_answer_callback(c.id)
    criar_usuario(c.from_user.id)
    show_purchase_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_saldo')
def menu_saldo(c):
    safe_answer_callback(c.id)
    criar_usuario(c.from_user.id)
    s = carregar_usuarios()[str(c.from_user.id)]['saldo']
    bot.send_message(c.message.chat.id, f"💰 Saldo: R$ {s:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_recarregar')
def menu_recarregar(c):
    safe_answer_callback(c.id)
    msg = bot.send_message(c.message.chat.id, '💳 Informe o valor (ex: 10.50):')
    bot.register_next_step_handler(msg, process_recharge_amount)

@bot.callback_query_handler(lambda c: c.data == 'menu_numeros')
def menu_numeros(c):
    safe_answer_callback(c.id)
    lst = carregar_usuarios()[str(c.from_user.id)]['numeros']
    if not lst:
        bot.send_message(c.message.chat.id, '📭 Sem números ativos.')
    else:
        text = '📋 *Seus números:*'
        with status_lock:
            for aid in lst:
                inf = status_map.get(aid)
                if inf and not inf['processed']:
                    text += f"\n`{inf['full']}` / `{inf['short']}` (ID {aid})"
        bot.send_message(c.message.chat.id, text, parse_mode='Markdown')
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    safe_answer_callback(c.id)
    user, serv = c.from_user.id, c.data.split('_')[1]
    precos = {'mercado':0.75,'china':0.70,'outros':0.90}
    idsms  = {'mercado':'cq','china':'ev','outros':'ot'}
    nomes  = {'mercado':'MercadoPago SMS','china':'SMS China','outros':'Outros SMS'}
    criar_usuario(user)
    saldo = carregar_usuarios()[str(user)]['saldo']
    price = precos[serv]; service = nomes[serv]
    if saldo < price:
        return bot.answer_callback_query(c.id,'❌ Saldo insuficiente.',True)

    bot.edit_message_text('⏳ Solicitando número...', c.message.chat.id, c.message.message_id)
    resp = {}
    for mp_ in range(1, 14):
        resp = solicitar_numero(idsms[serv], max_price=mp_)
        if resp.get('status') == 'success':
            break
    if resp.get('status') != 'success':
        bot.send_message(c.message.chat.id,'🚫 Sem números disponíveis.')
        return

    aid, full = resp['id'], resp['number']
    short = full[2:] if full.startswith('55') else full
    adicionar_numero(user, aid)
    alterar_saldo(user, saldo - price)

    kb_block = telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton('❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}')
    )
    kb_unblock = telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton('❌ Cancelar', callback_data=f'cancel_{aid}')
    )

    text = (f"📦 {service}\n☎️ `{full}` / `{short}`\n\n"
            f"🕘 Prazo: {PRAZO_MINUTOS} minutos\n"
            "_💡 Ativo por 23m; sem SMS, saldo devolvido automaticamente._")
    msg = bot.send_message(c.message.chat.id, text, parse_mode='Markdown', reply_markup=kb_block)

    with status_lock:
        status_map[aid] = {'user_id':user,'price':price,'chat_id':msg.chat.id,
                          'message_id':msg.message_id,'service':service,
                          'full':full,'short':short,'processed':False}

    send_info_bot(f"📦 Serviço `{service}` comprado: `{full}`")
    spawn_sms_thread(aid)

    def countdown():
        rem = PRAZO_MINUTOS
        for i in range(PRAZO_MINUTOS):
            time.sleep(60); rem -= 1
            new = (f"📦 {service}\n☎️ `{full}` / `{short}`\n\n"
                   f"🕘 Prazo: {rem} minuto{'s' if rem!=1 else ''}\n"
                   "_💡 Ativo por 23m; sem SMS, saldo devolvido automaticamente._")
            kb = kb_unblock if i >= 2 else kb_block
            try:
                bot.edit_message_text(new, msg.chat.id, msg.message_id,
                                      parse_mode='Markdown', reply_markup=kb)
            except:
                pass

    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        with status_lock:
            inf = status_map.get(aid)
        if inf and not inf['processed']:
            cancelar_numero(aid)
            alterar_saldo(inf['user_id'],
                          carregar_usuarios()[str(inf['user_id'])]['saldo'] + inf['price'])
            bot.send_message(inf['chat_id'],
                             f"❌ Sem SMS em {PRAZO_MINUTOS}m. Cancelado e R${inf['price']:.2f} devolvido.")
            inf['processed'] = True

    threading.Thread(target=countdown, daemon=True).start()
    threading.Thread(target=auto_cancel, daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    safe_answer_callback(c.id)
    aid = c.data.split('_',1)[1]
    try:
        requests.get(SMSBOWER_URL, params={'api_key':SMSBOWER_API_KEY,'action':'setStatus','status':'3','id':aid},timeout=10)
        inf = status_map.get(aid)
        if inf: inf['processed'] = False
        spawn_sms_thread(aid)
        bot.answer_callback_query(c.id,'🔄 Novo SMS solicitado.',True)
    except Exception as e:
        safe_answer_callback(c.id,'❌ Falha retry.',True)
        logger.error(f"Retry error: {e}")

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    safe_answer_callback(c.id,'⏳ Disponível em 2 minutos.',True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    safe_answer_callback(c.id)
    aid = c.data.split('_',1)[1]
    with status_lock:
        inf = status_map.get(aid)
    if not inf or inf['processed']:
        return bot.answer_callback_query(c.id,'❌ Não pode cancelar.',True)
    cancelar_numero(aid)
    alterar_saldo(inf['user_id'],carregar_usuarios()[str(inf['user_id'])]['saldo']+inf['price'])
    bot.edit_message_text('❌ Cancelado pelo usuário.',inf['chat_id'],inf['message_id'])
    bot.answer_callback_query(c.id,'Cancelado e devolvido.',True)
    inf['processed'] = True

# === PROCESSO DE RECARGA MP ===
def process_recharge_amount(m):
    try:
        amt = float(m.text.replace(',', '.'))
    except:
        return bot.reply_to(m,'❌ Valor inválido. Use ex: 10.50')
    ext_ref = f"{m.from_user.id}_{int(time.time())}"
    pref_data = {
        "items": [{"title": "Recarga de saldo", "quantity": 1, "unit_price": amt}],
        "external_reference": ext_ref,
        "back_urls": {"success": TELEGRAM_WEBHOOK_URL + MP_WEBHOOK_PATH},
        "auto_return": "approved"
    }
    pref = mp_sdk.preference().create(pref_data)["response"]
    # persiste em arquivo
    adicionar_recarga(pref["id"], ext_ref, m.from_user.id, amt, m.chat.id)

    kb = telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton(f'💳 Pagar R${amt:.2f}', url=pref["init_point"])
    )
    bot.send_message(m.chat.id,
        f"🎉 Pague R${amt:.2f} via Mercado Pago. Você será creditado automaticamente.",
        reply_markup=kb)
    send_info_bot(f"💳 Recarga iniciada: Usuário `{m.from_user.id}` R${amt:.2f}")

# === INICIALIZAÇÃO ===
if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=TELEGRAM_WEBHOOK_URL + '/telegram_webhook')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
