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

# === CONFIGURAÇÃO VIA ENV ===
BOT_TOKEN         = os.getenv('BOT_TOKEN')
ALERT_BOT_TOKEN   = os.getenv('ALERT_BOT_TOKEN')
ALERT_CHAT_ID     = os.getenv('ALERT_CHAT_ID')
API_KEY_SMSBOWER  = os.getenv('API_KEY_SMSBOWER')
SMSBOWER_URL      = 'https://smsbower.online/stubs/handler_api.php'
COUNTRY_ID        = '73'  # Brasil
MP_ACCESS_TOKEN   = os.getenv('MP_ACCESS_TOKEN')
SITE_URL          = os.getenv('SITE_URL').rstrip('/')

# === INICIALIZA BOTS E SDKs ===
bot       = telebot.TeleBot(BOT_TOKEN, threaded=False)
alert_bot = telebot.TeleBot(ALERT_BOT_TOKEN)
mp_client = mercadopago.SDK(MP_ACCESS_TOKEN)

# === FLASK APP ===
app = Flask(__name__)

# --- Log via Telegram ---
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

# --- Estado e travas ---
USERS_FILE     = 'usuarios.json'
data_lock      = threading.Lock()
status_lock    = threading.Lock()
status_map     = {}   # activation_id → info dict
PRAZO_MINUTOS  = 23
PRAZO_SEGUNDOS = PRAZO_MINUTOS * 60

# === FUNÇÕES AUXILIARES ===
def carregar_usuarios():
    with data_lock:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE,'w') as f: json.dump({},f)
        with open(USERS_FILE,'r') as f:
            return json.load(f)

def salvar_usuarios(usuarios):
    with data_lock:
        with open(USERS_FILE,'w') as f: json.dump(usuarios,f,indent=2)

def criar_usuario(uid):
    usuarios = carregar_usuarios()
    if str(uid) not in usuarios:
        usuarios[str(uid)] = {'saldo':0.0,'numeros':[]}
        salvar_usuarios(usuarios)
        logger.info(f'Novo usuário criado: {uid}')

def alterar_saldo(uid, novo):
    usuarios = carregar_usuarios()
    usuarios.setdefault(str(uid),{'saldo':0.0,'numeros':[]})['saldo'] = novo
    salvar_usuarios(usuarios)
    logger.info(f'Saldo de {uid} = R$ {novo:.2f}')

def adicionar_numero(uid, aid):
    usuarios = carregar_usuarios()
    user = usuarios.setdefault(str(uid),{'saldo':0.0,'numeros':[]})
    if aid not in user['numeros']:
        user['numeros'].append(aid)
        salvar_usuarios(usuarios)
        logger.info(f'Adicionado número {aid} a {uid}')

# === INTEGRAÇÃO SMSBOWER ===
def solicitar_numero(servico, max_price=None):
    params = {
        'api_key':API_KEY_SMSBOWER,
        'action':'getNumber',
        'service':servico,
        'country':COUNTRY_ID
    }
    if max_price: params['maxPrice'] = str(max_price)
    try:
        r = requests.get(SMSBOWER_URL,params=params,timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        logger.info(f"GET_NUMBER → {r.url} → {text}")
    except Exception as e:
        logger.error(f'Erro getNumber: {e}')
        return {'status':'error','message':str(e)}
    if text.startswith('ACCESS_NUMBER:'):
        _, aid, num = text.split(':',2)
        return {'status':'success','id':aid,'number':num}
    return {'status':'error','message':text}

def cancelar_numero(aid):
    try:
        requests.get(SMSBOWER_URL,params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'8','id':aid},timeout=10)
    except Exception as e:
        logger.error(f'Erro cancelar: {e}')

def obter_status(aid):
    try:
        r = requests.get(SMSBOWER_URL,params={'api_key':API_KEY_SMSBOWER,'action':'getStatus','id':aid},timeout=10)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f'Erro getStatus: {e}')
        return None

def spawn_sms_thread(aid):
    info = status_map.get(aid)
    if not info: return
    service, full, short, chat_id = info['service'], info['full'], info['short'], info['chat_id']
    sms_msg_id = info.get('sms_message_id')

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            s = obter_status(aid)
            logger.info(f"CheckSMS → {aid}: {s}")
            if not s or s.startswith('STATUS_WAIT'):  # ignora STATUS_WAIT_CODE
                time.sleep(5)
                continue
            if s == 'STATUS_CANCEL':
                alterar_saldo(info['user_id'], carregar_usuarios()[str(info['user_id'])]['saldo'] + info['price'])
                bot.send_message(chat_id, f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido.")
                info['processed'] = True
                return
            # aqui vem código real
            code = s.split(':',1)[1] if ':' in s else s
            rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            text = (f"📦 {service}\n☎️ Número: `{full}`\n☎️ Sem DDI: `{short}`\n\n"
                    f"📩 SMS: `{code}`\n🕘 {rt}")
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))
            if sms_msg_id is None:
                msg = bot.send_message(chat_id,text,parse_mode='Markdown',reply_markup=kb)
                info['sms_message_id'] = msg.message_id
            else:
                bot.edit_message_text(text,chat_id,sms_msg_id,parse_mode='Markdown',reply_markup=kb)
            info['processed'] = True
            return

    threading.Thread(target=check_sms,daemon=True).start()

# === MENU E HANDLERS PADRÃO ===
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('💰 Saldo',            callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar',        callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('📜 Meus números',      callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte',           url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id,'Escolha uma opção:',reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def default_menu(m):
    criar_usuario(m.from_user.id)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data=='menu_comprar')
def menu_comprar(c):
    bot.answer_callback_query(c.id)
    cmd_comprar(c.message)

@bot.message_handler(commands=['comprar'])
def cmd_comprar(m):
    criar_usuario(m.from_user.id)
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Mercado Pago SMS - R$0.75', callback_data='comprar_mercado'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS para China   - R$0.70', callback_data='comprar_china'),
        telebot.types.InlineKeyboardButton('📡 Outros SMS        - R$0.90', callback_data='comprar_outros')
    )
    bot.send_message(m.chat.id,'Escolha serviço:',reply_markup=kb)

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
        return bot.answer_callback_query(c.id,'❌ Saldo insuficiente.',True)
    bot.edit_message_text('⏳ Solicitando número...',c.message.chat.id,c.message.message_id)
    resp = {}
    for tentativa in range(1,14):
        resp = solicitar_numero(idsms[serv],max_price=tentativa)
        if resp.get('status')=='success': break
    if resp.get('status')!='success':
        return bot.send_message(c.message.chat.id,'🚫 Sem números disponíveis.')
    aid, full = resp['id'], resp['number']
    short = full[2:] if full.startswith('55') else full
    adicionar_numero(user_id,aid)
    alterar_saldo(user_id,saldo-price)
    kb_b = telebot.types.InlineKeyboardMarkup()
    kb_b.add(telebot.types.InlineKeyboardButton('❌ Cancelar (2m)',callback_data=f'cancel_blocked_{aid}'))
    kb_u = telebot.types.InlineKeyboardMarkup()
    kb_u.add(telebot.types.InlineKeyboardButton('❌ Cancelar',callback_data=f'cancel_{aid}'))
    texto = (f"📦 {service}\n☎️ Número: `{full}`\n☎️ Sem DDI: `{short}`\n\n"
             f"🕘 Prazo: {PRAZO_MINUTOS} minutos\n\n"
             f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente.")
    msg = bot.send_message(c.message.chat.id,texto,parse_mode='Markdown',reply_markup=kb_b)
    status_map[aid] = {
        'user_id':user_id,'price':price,'chat_id':msg.chat.id,'message_id':msg.message_id,
        'service':service,'full':full,'short':short,'processed':False,'sms_message_id':None
    }
    spawn_sms_thread(aid)

    def countdown():
        rem = PRAZO_MINUTOS
        for _ in range(PRAZO_MINUTOS):
            time.sleep(60); rem-=1
            novo = (f"📦 {service}\n☎️ Número: `{full}`\n☎️ Sem DDI: `{short}`\n\n"
                   f"🕘 Prazo: {rem} minuto{'s' if rem!=1 else ''}\n\n"
                   f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente.")
            mk = kb_u if rem< PRAZO_MINUTOS-1 else kb_b
            try: bot.edit_message_text(novo,msg.chat.id,msg.message_id,parse_mode='Markdown',reply_markup=mk)
            except: pass

    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        info = status_map.get(aid)
        if info and not info['processed']:
            cancelar_numero(aid)
            alterar_saldo(info['user_id'],carregar_usuarios()[str(info['user_id'])]['saldo']+info['price'])
            bot.send_message(info['chat_id'],f"❌ Sem SMS em {PRAZO_MINUTOS} minutos. Cancelado e R${info['price']:.2f} devolvido.")
            info['processed']=True

    threading.Thread(target=countdown,daemon=True).start()
    threading.Thread(target=auto_cancel,daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    aid = c.data.split('_',1)[1]
    try:
        requests.get(SMSBOWER_URL,params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'3','id':aid},timeout=10)
        bot.answer_callback_query(c.id,'🔄 Novo SMS solicitado.',show_alert=True)
        status_map[aid]['processed']=False
        spawn_sms_thread(aid)
    except Exception as e:
        logger.error(f'Retry: {e}')
        bot.answer_callback_query(c.id,'❌ Falha ao solicitar retry.',show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    bot.answer_callback_query(c.id,'⏳ Disponível após 2 minutos.',show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    aid = c.data.split('_',1)[1]
    info = status_map.get(aid)
    if not info or info['processed']: return bot.answer_callback_query(c.id,'❌ Não é possível cancelar.',True)
    cancelar_numero(aid)
    alterar_saldo(info['user_id'],carregar_usuarios()[str(info['user_id'])]['saldo']+info['price'])
    bot.edit_message_text('❌ Cancelado pelo usuário.',info['chat_id'],info['message_id'])
    bot.answer_callback_query(c.id,'Número cancelado e saldo devolvido.',show_alert=True)
    info['processed']=True

# === RECARREGAR via Mercado Pago ===
@bot.callback_query_handler(lambda c: c.data=='menu_recarregar')
def menu_recarregar(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    pref = mp_client.preference().create({
        "items":[{"title":"Recarga de saldo","quantity":1,"unit_price":1.00}],
        "external_reference":str(c.from_user.id),
        "back_urls":{"success":f"{SITE_URL}/?paid=success","failure":f"{SITE_URL}/?paid=failure","pending":f"{SITE_URL}/?paid=pending"},
        "auto_return":"approved"
    })
    pay_url = pref["response"]["init_point"]
    bot.send_message(c.message.chat.id,f"💳 Para recarregar, acesse este link:\n{pay_url}",disable_web_page_preview=False)
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data=='menu_saldo')
def menu_saldo(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    u = carregar_usuarios()[str(c.from_user.id)]
    bot.send_message(c.message.chat.id,f"💰 Saldo: R$ {u['saldo']:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data=='menu_numeros')
def menu_numeros(c):
    bot.answer_callback_query(c.id)
    criar_usuario(c.from_user.id)
    lst = carregar_usuarios()[str(c.from_user.id)]['numeros']
    if not lst:
        bot.send_message(c.message.chat.id,'📭 Você não tem números ativos.')
    else:
        txt='📋 *Seus números:*'
        for aid in lst:
            inf = status_map.get(aid)
            if inf and not inf['processed']:
                txt+=f"\n*ID:* `{aid}` ` {inf['full']} / {inf['short']}`"
        bot.send_message(c.message.chat.id,txt,parse_mode='Markdown')
    send_menu(c.message.chat.id)

# === WEBHOOKS ===
@app.route('/',methods=['GET'])
def health(): return 'OK',200

@app.route('/webhook/telegram',methods=['POST'])
def tg_webhook():
    upd=telebot.types.Update.de_json(request.get_data().decode())
    bot.process_new_updates([upd])
    return '',200

@app.route('/webhook/mercadopago',methods=['POST'])
def mp_webhook():
    d=request.get_json()
    if d.get('type')=='payment':
        pay_id=d['data']['id']
        pay=mp_client.payment().get(pay_id)["response"]
        if pay["status"]=="approved":
            cid=int(pay["external_reference"])
            amt=float(pay["transaction_amount"])
            cur=carregar_usuarios().get(str(cid),{}).get('saldo',0.0)
            alterar_saldo(cid,cur+amt)
            bot.send_message(cid,f"✅ Pagamento de R$ {amt:.2f} confirmado!\\nSeu novo saldo é R$ {cur+amt:.2f}")
    return '',200

# === INICIALIZAÇÃO ===
if __name__=='__main__':
    bot.remove_webhook()
    bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)))
