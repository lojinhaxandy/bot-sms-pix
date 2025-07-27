import os
import json
import threading
import time
import logging
import requests
import re
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, request
import telebot
import mercadopago

# === CONFIG ===
BOT_TOKEN         = os.getenv("BOT_TOKEN")
ALERT_BOT_TOKEN   = os.getenv("ALERT_BOT_TOKEN")
ALERT_CHAT_ID     = os.getenv("ALERT_CHAT_ID")
API_KEY_SMSBOWER  = os.getenv("API_KEY_SMSBOWER")
SMSBOWER_URL      = "https://smsbower.online/stubs/handler_api.php"
COUNTRY_ID        = "73"
MP_ACCESS_TOKEN   = os.getenv("MP_ACCESS_TOKEN")
SITE_URL          = os.getenv("SITE_URL").rstrip('/')
BACKUP_BOT_TOKEN  = os.getenv("BACKUP_BOT_TOKEN") or '7982928818:AAEPf9AgnSEqEL7Ay5UaMPyG27h59PdGUYs'
BACKUP_CHAT_ID    = os.getenv("BACKUP_CHAT_ID") or '6829680279'
ADMIN_BOT_TOKEN   = os.getenv("ADMIN_BOT_TOKEN") or '8011035929:AAHpztTqqAXaQ-2cQb23qklZIX4k0vVM2Uk'
ADMIN_CHAT_ID     = os.getenv("ADMIN_CHAT_ID") or '6829680279'
DATABASE_URL      = os.getenv("DATABASE_URL")

bot         = telebot.TeleBot(BOT_TOKEN, threaded=True)
alert_bot   = telebot.TeleBot(ALERT_BOT_TOKEN)
backup_bot  = telebot.TeleBot(BACKUP_BOT_TOKEN)
admin_bot   = telebot.TeleBot(ADMIN_BOT_TOKEN)
mp_client   = mercadopago.SDK(MP_ACCESS_TOKEN)
app = Flask(__name__)

def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def criar_tabelas():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id TEXT PRIMARY KEY,
                saldo DOUBLE PRECISION,
                numeros TEXT,
                refer TEXT,
                indicados TEXT
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS numeros_sms (
                aid TEXT PRIMARY KEY,
                user_id TEXT,
                price DOUBLE PRECISION,
                cancelado BOOLEAN DEFAULT FALSE,
                data_criacao TIMESTAMP DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS pagamentos_processados (
                pid TEXT PRIMARY KEY,
                uid TEXT,
                valor DOUBLE PRECISION,
                data TIMESTAMP DEFAULT NOW()
            );
            """)
            conn.commit()
criar_tabelas()

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

data_lock        = threading.Lock()
status_lock      = threading.Lock()
status_map       = {}
mensagens_codigo_sms = {}
PENDING_RECHARGE = {}
PRAZO_MINUTOS    = 23
PRAZO_SEGUNDOS   = PRAZO_MINUTOS * 60

def carregar_usuario(uid):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE id=%s", (str(uid),))
            user = cur.fetchone()
            if not user:
                return None
            user['numeros'] = json.loads(user['numeros'])
            user['indicados'] = json.loads(user.get('indicados', '[]'))
            return user

def salvar_usuario(user):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE usuarios SET saldo=%s, numeros=%s, refer=%s, indicados=%s WHERE id=%s
            """, (
                user['saldo'],
                json.dumps(user['numeros']),
                user.get('refer'),
                json.dumps(user.get('indicados', [])),
                str(user['id'])
            ))
            conn.commit()
    try:
        exportar_backup_json()
    except Exception as e:
        logger.error(f"Erro ao enviar backup: {e}")

def criar_usuario(uid, refer=None):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM usuarios WHERE id=%s", (str(uid),))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO usuarios (id, saldo, numeros, refer, indicados)
                VALUES (%s, %s, %s, %s, %s)
            """, (str(uid), 0.0, json.dumps([]), refer, json.dumps([])))
            conn.commit()
            logger.info(f"Novo usuário criado: {uid}")
            if refer and str(refer) != str(uid):
                cur.execute("SELECT indicados FROM usuarios WHERE id=%s", (str(refer),))
                result = cur.fetchone()
                if result:
                    indicados = json.loads(result['indicados'] or "[]")
                    if str(uid) not in indicados:
                        indicados.append(str(uid))
                        cur.execute("UPDATE usuarios SET indicados=%s WHERE id=%s", (json.dumps(indicados), str(refer)))
                        conn.commit()

def exportar_backup_json():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios")
            users = cur.fetchall()
            for u in users:
                u['numeros'] = json.loads(u['numeros'])
                u['indicados'] = json.loads(u.get('indicados', '[]'))
            with open("usuarios_backup.json", "w") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            with open("usuarios_backup.json", "rb") as bf:
                backup_bot.send_document(BACKUP_CHAT_ID, bf)

def registrar_numero_sms(aid, user_id, price):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO numeros_sms (aid, user_id, price, cancelado) VALUES (%s, %s, %s, FALSE) ON CONFLICT (aid) DO NOTHING",
                (aid, str(user_id), price)
            )
            conn.commit()

def marcar_cancelado_e_devolver(uid, aid):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cancelado, price FROM numeros_sms WHERE aid=%s FOR UPDATE", (aid,))
            row = cur.fetchone()
            if not row or row['cancelado']:
                return False
            price = row['price']
            cur.execute("UPDATE numeros_sms SET cancelado=TRUE WHERE aid=%s", (aid,))
            cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (price, str(uid)))
            conn.commit()
    return True

def desconto_saldo_atomic(uid, valor_descontar):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT saldo FROM usuarios WHERE id=%s FOR UPDATE", (str(uid),))
            res = cur.fetchone()
            if not res:
                return False
            saldo_atual = float(res['saldo'])
            if saldo_atual < valor_descontar:
                return False
            saldo_novo = saldo_atual - valor_descontar
            cur.execute("UPDATE usuarios SET saldo=%s WHERE id=%s", (saldo_novo, str(uid)))
            conn.commit()
    logger.info(f"Saldo de {uid} = R$ {saldo_novo:.2f}")
    return True

def registrar_pagamento(pid, uid, valor):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO pagamentos_processados (pid, uid, valor) VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """, (pid, str(uid), valor))
            conn.commit()

def ja_processou_pagamento(pid):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pagamentos_processados WHERE pid=%s", (pid,))
            return cur.fetchone() is not None

def adicionar_numero(uid, aid):
    user = carregar_usuario(uid)
    if not user:
        return
    if aid not in user['numeros']:
        user['numeros'].append(aid)
        salvar_usuario(user)
        logger.info(f"Número {aid} adicionado a {uid}")

def get_user_ref_link(uid):
    return f"https://t.me/{bot.get_me().username}?start={uid}"

# ============ SMSBOWER ===============
def solicitar_numero(servico, max_price=None):
    params = {
        'api_key': API_KEY_SMSBOWER,
        'action': 'getNumber',
        'service': servico,
        'country': COUNTRY_ID
    }
    if max_price:
        params['maxPrice'] = str(max_price)
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
            params={'api_key':API_KEY_SMSBOWER, 'action':'setStatus','status':'8','id':aid},
            timeout=10
        )
        logger.info(f"Cancelado provider: {aid}")
    except Exception as e:
        logger.error(f"Erro cancelar: {e}")

def obter_status(aid):
    try:
        r = requests.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER,'action':'getStatus','id':aid},
            timeout=10
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus: {e}")
        return None

# =========== MENUS ===========

def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('💰 Saldo', callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar', callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('👥 Referências', callback_data='menu_refer'),
        telebot.types.InlineKeyboardButton('📜 Meus números', callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte', url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id, 'Escolha uma opção:', reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    refer = None
    if m.text and m.text.startswith('/start ') and m.text.split(' ', 1)[1].isdigit():
        refer = m.text.split(' ', 1)[1]
    criar_usuario(m.from_user.id, refer=refer)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu')
def callback_menu(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_comprar')
def callback_menu_comprar(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Mercado Pago SMS - R$0.75', callback_data='comprar_mercado'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS para China   - R$0.60', callback_data='comprar_china'),
        telebot.types.InlineKeyboardButton('💸 PicPay SMS       - R$0.65', callback_data='comprar_picpay'),
        telebot.types.InlineKeyboardButton('📡 Outros SMS        - R$0.90', callback_data='comprar_outros')
    )
    bot.send_message(c.message.chat.id, 'Escolha serviço:', reply_markup=kb)

@bot.message_handler(commands=['comprar'])
def cmd_comprar(m):
    criar_usuario(m.from_user.id)
    callback_menu_comprar(type('obj', (object,), {'id': None, 'message': m})())

@bot.callback_query_handler(lambda c: c.data == 'menu_recarregar')
def menu_recarregar(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    criar_usuario(c.from_user.id)
    PENDING_RECHARGE[c.from_user.id] = True
    bot.send_message(
        c.message.chat.id,
        'Digite o valor (em reais) que deseja recarregar:'
    )

@bot.message_handler(func=lambda m: PENDING_RECHARGE.get(m.from_user.id) and re.fullmatch(r"\d+(\.\d{1,2})?", m.text))
def handle_recharge_amount(m):
    uid = m.from_user.id
    amount = float(m.text)
    PENDING_RECHARGE.pop(uid, None)
    pref = mp_client.preference().create({
        "items": [{"title": "Recarga de saldo", "quantity": 1, "unit_price": amount}],
        "external_reference": f"{uid}:{amount}",
        "back_urls": {
            "success": f"{SITE_URL}/?paid=success",
            "failure": f"{SITE_URL}/?paid=failure",
            "pending": f"{SITE_URL}/?paid=pending"
        },
        "auto_return": "approved"
    })
    pay_url = pref["response"]["init_point"]
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"💳 Pagar R$ {amount:.2f}", url=pay_url
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
    )
    bot.send_message(
        m.chat.id,
        f"Para recarregar R$ {amount:.2f}, clique abaixo:",
        reply_markup=kb
    )
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_saldo')
def menu_saldo(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    criar_usuario(c.from_user.id)
    s = carregar_usuario(c.from_user.id)['saldo']
    bot.send_message(c.message.chat.id, f"💰 Saldo: R$ {s:.2f}")
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_numeros')
def menu_numeros(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    criar_usuario(c.from_user.id)
    nums = carregar_usuario(c.from_user.id)['numeros']
    if not nums:
        bot.send_message(c.message.chat.id, '📭 Sem números ativos.')
    else:
        txt = '📋 *Seus números:*'
        for aid in nums:
            txt += f"\n*ID:* `{aid}`"
        bot.send_message(
            c.message.chat.id,
            txt,
            parse_mode='Markdown'
        )
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_refer')
def menu_refer(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    u = carregar_usuario(c.from_user.id)
    link = get_user_ref_link(c.from_user.id)
    indicados = u.get("indicados", [])
    text = (
        f"📢 *Indique amigos e ganhe 10% de todas as recargas deles!*\n\n"
        f"Seu link exclusivo:\n`{link}`\n\n"
        f"*Indicações ativas:* {len(indicados)}\n"
    )
    if indicados:
        nomes = []
        for id_ in indicados:
            nomes.append(f"- {id_}")
        text += "\n" + "\n".join(nomes)
    bot.send_message(c.message.chat.id, text, parse_mode='Markdown')
    send_menu(c.message.chat.id)

@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    user_id, key = c.from_user.id, c.data.split('_')[1]
    criar_usuario(user_id)
    prices = {
        'mercado':0.75, 'china':0.6, 'picpay':0.65, 'outros':0.90
    }
    names  = {
        'mercado':'Mercado Pago SMS','china':'SMS para China',
        'picpay':'PicPay SMS','outros':'Outros SMS'
    }
    idsms  = {
        'mercado':'cq','china':'ev','picpay':'ev','outros':'ot'
    }
    price, service = prices[key], names[key]
    ok = desconto_saldo_atomic(user_id, price)
    if not ok:
        return bot.answer_callback_query(c.id, '❌ Saldo insuficiente.', True)
    try:
        bot.edit_message_text('⏳ Solicitando número...', c.message.chat.id, c.message.message_id)
    except Exception: pass
    resp = {}
    for attempt in range(1, 14):
        resp = solicitar_numero(idsms[key], max_price=attempt)
        if resp.get('status') == 'success':
            break
    if not resp.get('status') == 'success':
        return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')
    aid   = resp['id']
    full  = resp['number']
    short = full[2:] if full.startswith('55') else full
    adicionar_numero(user_id, aid)
    registrar_numero_sms(aid, user_id, price)
    kb_blocked = telebot.types.InlineKeyboardMarkup()
    kb_blocked.row(telebot.types.InlineKeyboardButton(f'❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}'))
    kb_blocked.row(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
    )
    kb_unlocked = telebot.types.InlineKeyboardMarkup()
    kb_unlocked.row(telebot.types.InlineKeyboardButton(f'❌ Cancelar', callback_data=f'cancel_{aid}'))
    kb_unlocked.row(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
    )
    text = (
        f"📦 {service}\n"
        f"☎️ Número: `{full}`\n"
        f"☎️ Sem DDI: `{short}`\n\n"
        f"🕘 Prazo: {PRAZO_MINUTOS} minutos\n\n"
        f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
    )
    msg = bot.send_message(
        c.message.chat.id,
        text, parse_mode='Markdown', reply_markup=kb_blocked
    )
    status_map[aid] = {

def atualizar_mensagem_codigo(user_id, text, kb):
    try:
        if user_id in mensagens_codigo_sms:
            bot.edit_message_text(
                text,
                chat_id=mensagens_codigo_sms[user_id]['chat_id'],
                message_id=mensagens_codigo_sms[user_id]['message_id'],
                parse_mode='Markdown',
                reply_markup=kb
            )
        else:
            msg = bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=kb)
            mensagens_codigo_sms[user_id] = {
                'chat_id': msg.chat.id,
                'message_id': msg.message_id
            }
    except Exception as e:
        print("Erro ao atualizar mensagem:", e)

        'user_id':    user_id,
        'price':      price,
        'chat_id':    msg.chat.id,
        'message_id': msg.message_id,
        'service':    service,
        'full':       full,
        'short':      short
    }
    def spawn_sms_thread(aid):
        info = status_map.get(aid)
        if not info: return
        def check_sms():
            start = time.time()
            while time.time() - start < PRAZO_SEGUNDOS:
                status = obter_status(aid)
                if info.get('canceled_by_user'):
                    return
                if not status or status.startswith('STATUS_WAIT'):
                    time.sleep(5)
                    continue
                if status == 'STATUS_CANCEL':
                    if not info.get('codes'):
                        ok = marcar_cancelado_e_devolver(info['user_id'], aid)
                        if ok:
                            bot.send_message(info['chat_id'], f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido.")
                    return
                code = status.split(':', 1)[1] if ':' in status else status
                info.setdefault('codes', [])
                if code not in info['codes']:
                    info['codes'].append(code)
                    rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
                    text = (
                        f"📦 {info['service']}\n"
                        f"☎️ Número: `{info['full']}`\n"
                        f"☎️ Sem DDI: `{info['short']}`\n\n"
                    )
                    for idx, cd in enumerate(info['codes'], 1):
                        text += f"📩 SMS{idx}: `{cd}`\n"
                    text += f"🕘 {rt}"
                    kb = telebot.types.InlineKeyboardMarkup()
                    kb.row(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))
                    kb.row(
                        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
                    )
                    try:
                        atualizar_mensagem_codigo(info['user_id'], text, kb)
                    except Exception: pass
                time.sleep(5)
        threading.Thread(target=check_sms, daemon=True).start()
    spawn_sms_thread(aid)
    def countdown():
        for minute in range(PRAZO_MINUTOS):
            time.sleep(60)
            rem = PRAZO_MINUTOS - (minute + 1)
            info = status_map.get(aid)
            if not info:
                return
            new_text = (
                f"📦 {service}\n"
                f"☎️ Número: `{full}`\n"
                f"☎️ Sem DDI: `{short}`\n\n"
                f"🕘 Prazo: {rem} minutos\n\n"
                f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
            )
            kb_sel = kb_blocked if minute < 2 else kb_unlocked
            try:
                bot.edit_message_text(
                    new_text,
                    info['chat_id'],
                    info['message_id'],
                    parse_mode='Markdown',
                    reply_markup=kb_sel
                )
            except Exception:
                pass
    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        info = status_map.get(aid)
        if info and not info.get('codes') and not info.get('canceled_by_user'):
            cancelar_numero(aid)
            ok = marcar_cancelado_e_devolver(info['user_id'], aid)
            if ok:
                try:
                    bot.delete_message(info['chat_id'], info['message_id'])
                except:
                    pass
    threading.Thread(target=countdown, daemon=True).start()
    threading.Thread(target=auto_cancel, daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    aid = c.data.split('_', 1)[1]
    try:
        requests.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'3','id':aid},
            timeout=10
        )
    except:
        pass
    bot.answer_callback_query(c.id, '🔄 Novo SMS solicitado.', show_alert=True)
    # Reinicia thread SMS!
    def spawn_sms_thread(aid):
        info = status_map.get(aid)
        if not info: return
        def check_sms():
            start = time.time()
            while time.time() - start < PRAZO_SEGUNDOS:
                status = obter_status(aid)
                if info.get('canceled_by_user'):
                    return
                if not status or status.startswith('STATUS_WAIT'):
                    time.sleep(5)
                    continue
                if status == 'STATUS_CANCEL':
                    if not info.get('codes'):
                        ok = marcar_cancelado_e_devolver(info['user_id'], aid)
                        if ok:
                            bot.send_message(info['chat_id'], f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido.")
                    return
                code = status.split(':', 1)[1] if ':' in status else status
                info.setdefault('codes', [])
                if code not in info['codes']:
                    info['codes'].append(code)
                    rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
                    text = (
                        f"📦 {info['service']}\n"
                        f"☎️ Número: `{info['full']}`\n"
                        f"☎️ Sem DDI: `{info['short']}`\n\n"
                    )
                    for idx, cd in enumerate(info['codes'], 1):
                        text += f"📩 SMS{idx}: `{cd}`\n"
                    text += f"🕘 {rt}"
                    kb = telebot.types.InlineKeyboardMarkup()
                    kb.row(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))
                    kb.row(
                        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
                    )
                    try:
                        atualizar_mensagem_codigo(info['user_id'], text, kb)
                    except Exception: pass
                time.sleep(5)
        threading.Thread(target=check_sms, daemon=True).start()
    spawn_sms_thread(aid)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_blocked_'))
def cancel_blocked(c):
    bot.answer_callback_query(c.id, '⏳ Disponível após 2 minutos.', show_alert=True)

@bot.callback_query_handler(lambda c: c.data.startswith('cancel_'))
def cancelar_user(c):
    aid = c.data.split('_', 1)[1]
    info = status_map.get(aid)
    if not info or info.get('codes'):
        return bot.answer_callback_query(c.id, '❌ Não pode cancelar após receber SMS.', True)
    if info.get('canceled_by_user'):
        return bot.answer_callback_query(c.id, '❌ Já cancelado.', True)
    info['canceled_by_user'] = True
    cancelar_numero(aid)
    ok = marcar_cancelado_e_devolver(info['user_id'], aid)
    if ok:
        try: bot.delete_message(info['chat_id'], info['message_id'])
        except: pass
        bot.answer_callback_query(c.id, '✅ Cancelado e saldo devolvido.', show_alert=True)
    else:
        bot.answer_callback_query(c.id, '❌ Já cancelado anteriormente.', show_alert=True)

# === FLASK APP WEBHOOKS ===

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    json_str = request.get_data().decode('utf-8')
    upd = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([upd])
    return '', 200

@app.route('/webhook/mercadopago', methods=['POST'])
def mp_webhook():
    data = request.get_json()
    if data.get('type') == 'payment':
        pid = str(data['data']['id'])
        if ja_processou_pagamento(pid):  # NÃO DUPLICAR saldo!
            return '', 200
        resp = mp_client.payment().get(pid)['response']
        if resp.get('status') == 'approved':
            ext = resp.get('external_reference', '')
            if ':' in ext:
                uid_str, amt_str = ext.split(':', 1)
                try:
                    uid = int(uid_str)
                    amt = float(amt_str)
                    user = carregar_usuario(uid)
                    if user:
                        current = user.get('saldo', 0.0)
                        refid = user.get("refer")
                        bonus = 0
                        ref_text = ""
                        if refid:
                            ref_user = carregar_usuario(refid)
                            if ref_user:
                                bonus = round(amt * 0.10, 2)
                                desconto_saldo_atomic(refid, -bonus) # adiciona bônus
                                try:
                                    bot.send_message(
                                        int(refid),
                                        f"🎉 Você ganhou R$ {bonus:.2f} de bônus pois seu indicado recarregou saldo!"
                                    )
                                except:
                                    pass
                                ref_text = f"\nIndicado por: {refid}\nBônus enviado: R$ {bonus:.2f}"
                        desconto_saldo_atomic(uid, -amt) # adiciona recarga
                        registrar_pagamento(pid, uid, amt)
                        bot.send_message(
                            uid,
                            f"✅ Recarga de R$ {amt:.2f} confirmada! Seu novo saldo é R$ {current + amt:.2f}"
                        )
                        try:
                            admin_bot.send_message(
                                ADMIN_CHAT_ID,
                                f"💰 Novo DEPÓSITO\nUser: {uid}\nValor: R$ {amt:.2f}\nData: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}{ref_text}"
                            )
                        except Exception as e:
                            logger.error(f"Erro envio admin recarga: {e}")
                except Exception as e:
                    logger.error(f"Erro external_reference: {e}")
    return '', 200

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
