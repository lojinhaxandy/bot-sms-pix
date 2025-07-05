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

# === CONFIG from environment ===
BOT_TOKEN            = os.getenv('BOT_TOKEN')
ALERT_BOT_TOKEN      = os.getenv('ALERT_BOT_TOKEN')
ALERT_CHAT_ID        = os.getenv('ALERT_CHAT_ID')
SMSBOWER_API_KEY     = os.getenv('SMSBOWER_API_KEY')
SMSBOWER_URL         = 'https://smsbower.online/stubs/handler_api.php'
COUNTRY_ID           = os.getenv('COUNTRY_ID', '73')
MP_ACCESS_TOKEN      = os.getenv('MP_ACCESS_TOKEN')
TELEGRAM_WEBHOOK_URL = os.getenv('TELEGRAM_WEBHOOK_URL')
MP_WEBHOOK_PATH      = os.getenv('MP_WEBHOOK_PATH', '/mp_webhook')
INFO_BOT_TOKEN       = os.getenv('INFO_BOT_TOKEN')
INFO_CHAT_ID         = os.getenv('INFO_CHAT_ID')

USERS_FILE    = 'usuarios.json'
PRAZO_MINUTOS = 23
PRAZO_SEGUNDOS = PRAZO_MINUTOS * 60

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
h = TelegramLogHandler()
h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(h)

# === STORAGE & STATE ===
data_lock    = threading.Lock()
status_lock  = threading.Lock()
status_map   = {}   # activation_id -> info dict
recharge_map = {}   # preference_id -> {user_id, amount, chat_id}

# === HELPERS ===
def safe_answer_callback(query_id, text=None, show_alert=False):
    try:
        bot.answer_callback_query(query_id, text=text, show_alert=show_alert)
    except ApiTelegramException as e:
        logger.warning(f"Callback answer failed: {e}")

def send_info_bot(text: str):
    if not INFO_BOT_TOKEN or not INFO_CHAT_ID: return
    url = f"https://api.telegram.org/bot{INFO_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": INFO_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to notify info bot: {e}")

# === USER DATA ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'w') as f: json.dump({}, f)
        with open(USERS_FILE, 'r') as f: return json.load(f)

def salvar_usuarios(us):
    with data_lock:
        with open(USERS_FILE, 'w') as f: json.dump(us, f, indent=2)

def criar_usuario(uid):
    us = carregar_usuarios()
    if str(uid) not in us:
        us[str(uid)] = {'saldo':0.0,'numeros':[]}
        salvar_usuarios(us)

def alterar_saldo(uid, novo):
    us = carregar_usuarios()
    us.setdefault(str(uid), {'saldo':0.0,'numeros':[]})['saldo'] = novo
    salvar_usuarios(us)

def adicionar_numero(uid, aid):
    us = carregar_usuarios()
    u = us.setdefault(str(uid), {'saldo':0.0,'numeros':[]})
    if aid not in u['numeros']:
        u['numeros'].append(aid)
        salvar_usuarios(us)

# === SMSBOWER API ===
def solicitar_numero(servico, max_price=None):
    params = {'api_key':SMSBOWER_API_KEY,'action':'getNumber','service':servico,'country':COUNTRY_ID}
    if max_price: params['maxPrice']=str(max_price)
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15); r.raise_for_status()
        txt = r.text.strip(); logger.info(f"GET_NUMBER→{txt}")
    except Exception as e:
        logger.error(f"getNumber error: {e}"); return {'status':'error','message':str(e)}
    if txt.startswith('ACCESS_NUMBER:'):
        _, aid, num = txt.split(':',2)
        return {'status':'success','id':aid,'number':num}
    return {'status':'error','message':txt}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL, params={'api_key':SMSBOWER_API_KEY,'action':'setStatus','status':'8','id':aid}, timeout=10)
        logger.info(f"Cancelled {aid}")
    except Exception as e:
        logger.error(f"cancel error: {e}")

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL, params={'api_key':SMSBOWER_API_KEY,'action':'getStatus','id':aid}, timeout=10)
        r.raise_for_status(); return r.text.strip()
    except Exception as e:
        logger.error(f"getStatus error: {e}"); return None

# === SMS THREAD ===
def spawn_sms_thread(aid):
    with status_lock: info = status_map.get(aid)
    if not info: return
    service, full, short, chat_id = info['service'], info['full'], info['short'], info['chat_id']
    sms_msg_id = info.get('sms_message_id'); codes = info.setdefault('codes',[])
    def check_sms():
        start = time.time()
        while time.time()-start < PRAZO_SEGUNDOS:
            s = obter_status(aid); logger.info(f"STATUS {aid}: {s}")
            if not s: time.sleep(5); continue
            if s in ('STATUS_CANCEL',):
                u = status_map[aid]
                alterar_saldo(u['user_id'], carregar_usuarios()[str(u['user_id'])]['saldo']+u['price'])
                bot.send_message(chat_id, f"❌ Cancelado pelo provedor. R${u['price']:.2f} devolvido.")
                info['processed']=True; return
            if s.startswith('STATUS_OK:') or s.startswith('ACCESS_ACTIVATION:') or s.startswith('STATUS_WAIT_RETRY:'):
                code = s.split(':',1)[1]
            else:
                time.sleep(5); continue
            if code in codes: return
            codes.append(code)
            rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            sms_lines = "\n".join(f"📩 SMS: `{c}`" for c in codes)
            text = f"📦 {service}\n☎️ `{full}` / `{short}`\n\n{sms_lines}\n🕘 {rt}"
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))
            if sms_msg_id is None:
                msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=kb)
                info['sms_message_id'] = msg.message_id
            else:
                bot.edit_message_text(text, chat_id, sms_msg_id, parse_mode='Markdown', reply_markup=kb)
            send_info_bot(f"📩 SMS recebido para {service}: `{code}`")
            info['processed']=True
            return
    threading.Thread(target=check_sms, daemon=True).start()

# === FLASK ENDPOINTS ===
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    upd = telebot.types.Update.de_json(request.get_json(force=True))
    bot.process_new_updates([upd]); return jsonify({'ok':True})

@app.route(MP_WEBHOOK_PATH, methods=['POST'])
def mp_webhook():
    d = request.get_json(force=True); logger.info(f"MP→{d}")
    if d.get('type')=='payment':
        pay = mp_sdk.payment().get(d['data']['id'])['response']
        if pay.get('status')=='approved':
            pref = pay.get('preference_id')
            ref = recharge_map.pop(pref, None)
            if ref:
                uid, amt, chat = ref['user_id'], ref['amount'], ref['chat_id']
                criar_usuario(uid)
                alterar_saldo(uid, carregar_usuarios()[str(uid)]['saldo']+amt)
                bot.send_message(chat, f"✅ Recarga R${amt:.2f} aprovada! Saldo atualizado.")
                send_info_bot(f"💰 Usuário `{uid}` recarregou R${amt:.2f}")
    return jsonify({'ok':True})

# === BOT HANDLERS ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços',callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('💰 Saldo',callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar',callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('📜 Meus números',callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte',url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id,'Escolha uma opção:',reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m): criar_usuario(m.from_user.id); send_menu(m.chat.id)

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def default_menu(m): criar_usuario(m.from_user.id); send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data=='menu_saldo')
def h_saldo(c):
    safe_answer_callback(c.id)
    criar_usuario(c.from_user.id)
    s=carregar_usuarios()[str(c.from_user.id)]['saldo']
    bot.send_message(c.message.chat.id,f"💰 Saldo: R$ {s:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data=='menu_recarregar')
def h_recarregar(c):
    safe_answer_callback(c.id)
    m=bot.send_message(c.message.chat.id,'💳 Quanto deseja recarregar? (ex:10.50)')
    bot.register_next_step_handler(m, process_recharge_amount)

@bot.callback_query_handler(lambda c: c.data=='menu_numeros')
def h_numeros(c):
    safe_answer_callback(c.id); criar_usuario(c.from_user.id)
    nums=carregar_usuarios()[str(c.from_user.id)]['numeros']
    if not nums:
        bot.send_message(c.message.chat.id,'📭 Sem números ativos.')
    else:
        text='📋 *Seus números:*'
        with status_lock:
            for aid in nums:
                inf=status_map.get(aid)
                if inf and not inf['processed']:
                    text+=f"\n`{inf['full']}` / `{inf['short']}` (ID {aid})"
        bot.send_message(c.message.chat.id,text,parse_mode='Markdown')
    send_menu(c.message.chat.id)

@bot.message_handler(commands=['comprar'])
def cmd_comprar(m): criar_usuario(m.from_user.id); send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    safe_answer_callback(c.id)
    user, cmd = c.from_user.id, c.data.split('_')[1]
    precos = {'mercado':0.75,'china':0.70,'outros':0.90}
    idsms  = {'mercado':'cq','china':'ev','outros':'ot'}
    nomes  = {'mercado':'Mercado Pago SMS','china':'SMS para China','outros':'Outros SMS'}
    criar_usuario(user)
    saldo=carregar_usuarios()[str(user)]['saldo']; price=precos[cmd]; service=nomes[cmd]
    if saldo<price:
        return bot.answer_callback_query(c.id,'❌ Saldo insuficiente.',True)
    bot.edit_message_text('⏳ Solicitando número...',c.message.chat.id,c.message.message_id)
    resp={}
    for mp_ in range(1,14):
        resp=solicitar_numero(idsms[cmd],max_price=mp_)
        if resp.get('status')=='success': break
    if resp.get('status')!='success':
        bot.send_message(c.message.chat.id,'🚫 Sem números disponíveis.')
        return
    aid,full=resp['id'],resp['number']; short=full[2:] if full.startswith('55') else full
    adicionar_numero(user,aid); alterar_saldo(user,saldo-price)
    kb_block=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton('❌ Cancelar (2m)',callback_data=f'cancel_blocked_{aid}')
    )
    kb_unb=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton('❌ Cancelar',callback_data=f'cancel_{aid}')
    )
    text=(f"📦 {service}\n☎️ `{full}` / `{short}`\n\n"
          f"🕘 Prazo: {PRAZO_MINUTOS} minutos\n"
          f"💡 Ativo por {PRAZO_MINUTOS}m; sem SMS, saldo devolvido.")
    msg=bot.send_message(c.message.chat.id,text,parse_mode='Markdown',reply_markup=kb_block)
    with status_lock:
        status_map[aid]={'user_id':user,'price':price,'chat_id':msg.chat.id,
                         'message_id':msg.message_id,'service':service,
                         'full':full,'short':short,'processed':False}
    send_info_bot(f"📦 {service} comprado: `{full}` (ID {aid})")
    spawn_sms_thread(aid)
    # countdown
    def countdown():
        remaining=PRAZO_MINUTOS
        for i in range(PRAZO_MINUTOS):
            time.sleep(60); remaining-=1
            new_text=(f"📦 {service}\n☎️ `{full}` / `{short}`\n\n"
                      f"🕘 {remaining} minuto{'s' if remaining!=1 else ''}\n"
                      f"💡 Ativo por {PRAZO_MINUTOS}m; sem SMS, saldo devolvido.")
            kb=kb_unb if i>=2 else kb_block
            try: bot.edit_message_text(new_text,msg.chat.id,msg.message_id,
                                       parse_mode='Markdown',reply_markup=kb)
            except: pass
    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        with status_lock: inf=status_map.get(aid)
        if inf and not inf['processed']:
            cancelar_numero(aid)
            alterar_saldo(inf['user_id'],carregar_usuarios()[str(inf['user_id'])]['saldo']+inf['price'])
            bot.send_message(inf['chat_id'],f"❌ Sem SMS em {PRAZO_MINUTOS}m. Cancelado e R${inf['price']:.2f} devolvido.")
            inf['processed']=True
    threading.Thread(target=countdown,daemon=True).start()
    threading.Thread(target=auto_cancel,daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    safe_answer_callback(c.id)
    aid=c.data.split('_',1)[1]
    try:
        requests.get(SMSBOWER_URL,params={'api_key':SMSBOWER_API_KEY,'action':'setStatus','status':'3','id':aid},timeout=5)
        info=status_map.get(aid)
        if info: info['processed']=False
        spawn_sms_thread(aid)
        bot.answer_callback_query(c.id,'🔄 Novo SMS solicitado.',True)
    except Exception as e:
        bot.answer_callback_query(c.id,'❌ Erro retry.',True)
        logger.error(f"Retry error: {e}")

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    safe_answer_callback(c.id,'⏳ Disponível em 2m',True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    safe_answer_callback(c.id)
    aid=c.data.split('_',1)[1]
    with status_lock: inf=status_map.get(aid)
    if not inf or inf['processed']:
        return bot.answer_callback_query(c.id,'❌ Não pode cancelar.',True)
    cancelar_numero(aid)
    alterar_saldo(inf['user_id'],carregar_usuarios()[str(inf['user_id'])]['saldo']+inf['price'])
    bot.edit_message_text('❌ Cancelado pelo usuário.',inf['chat_id'],inf['message_id'])
    bot.answer_callback_query(c.id,'Cancelado e saldo devolvido.',True)
    inf['processed']=True

# === PROCESS RECHARGE ===
def process_recharge_amount(m):
    try: amt=float(m.text.replace(',','.'))
    except: return bot.reply_to(m,'❌ Valor inválido.')
    pd={"items":[{"title":"Recarga","quantity":1,"unit_price":amt}],
        "external_reference":f"{m.from_user.id}_{int(time.time())}",
        "back_urls":{"success":TELEGRAM_WEBHOOK_URL+MP_WEBHOOK_PATH},
        "auto_return":"approved"}
    pref=mp_sdk.preference().create(pd)["response"]
    recharge_map[pref["id"]]={"user_id":m.from_user.id,"amount":amt,"chat_id":m.chat.id}
    kb=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton(f'💳 Pagar R${amt:.2f}',url=pref["init_point"]))
    bot.send_message(m.chat.id,f"🎉 Pague R${amt:.2f} via MP, será creditado automaticamente.",reply_markup=kb)
    send_info_bot(f"💳 Recarga iniciada: usuário `{m.from_user.id}` R${amt:.2f}")

# === SET WEBHOOK & RUN ===
if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=TELEGRAM_WEBHOOK_URL + '/telegram_webhook')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
