import os
import json
import threading
import time
import logging
import requests
import re
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime
from flask import Flask, request, render_template_string
from requests.adapters import HTTPAdapter, Retry

import telebot
import mercadopago

# =========================================================
# ======================= CONFIG ==========================
# =========================================================
BOT_TOKEN         = os.getenv("BOT_TOKEN")
ALERT_BOT_TOKEN   = os.getenv("ALERT_BOT_TOKEN")
ALERT_CHAT_ID     = os.getenv("ALERT_CHAT_ID")
API_KEY_SMSBOWER  = os.getenv("API_KEY_SMSBOWER")
SMSBOWER_URL      = "https://smsbower.online/stubs/handler_api.php"
COUNTRY_ID        = "73"  # BRAZIL no provider SMSBOWER
MP_ACCESS_TOKEN   = os.getenv("MP_ACCESS_TOKEN")
SITE_URL          = (os.getenv("SITE_URL") or "").rstrip('/')
BACKUP_BOT_TOKEN  = os.getenv("BACKUP_BOT_TOKEN") or '7982928818:AAEPf9AgnSEqEL7Ay5UaMPyG27h59PdGUYs'
BACKUP_CHAT_ID    = os.getenv("BACKUP_CHAT_ID") or '6829680279'
ADMIN_BOT_TOKEN   = os.getenv("ADMIN_BOT_TOKEN") or '8011035929:AAHpztTqqAXaQ-2cQb23qklZIX4k0vVM2Uk'
ADMIN_CHAT_ID     = os.getenv("ADMIN_CHAT_ID") or '6829680279'
DATABASE_URL      = os.getenv("DATABASE_URL")
PAINEL_TOKEN      = os.getenv("PAINEL_TOKEN") or "painel2024"
SERVICES_JSON     = os.getenv("SERVICES_JSON") or "services.json"  # caminho do JSON que você já tem

# =========================================================
# ======= HTTP SESSION COM POOL + RETRIES =================
# =========================================================
HTTP = requests.Session()
HTTP.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        ),
        pool_connections=50,
        pool_maxsize=50,
    ),
)
HTTP.mount(
    "http://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        ),
        pool_connections=50,
        pool_maxsize=50,
    ),
)

# =========================================================
# =========== BOTS / SDK / FLASK (mantidos) ===============
# =========================================================
bot         = telebot.TeleBot(BOT_TOKEN, threaded=True)
alert_bot   = telebot.TeleBot(ALERT_BOT_TOKEN) if ALERT_BOT_TOKEN else None
backup_bot  = telebot.TeleBot(BACKUP_BOT_TOKEN)
admin_bot   = telebot.TeleBot(ADMIN_BOT_TOKEN)
mp_client   = mercadopago.SDK(MP_ACCESS_TOKEN)
app = Flask(__name__)

# =========================================================
# ==================== BANCO DE DADOS =====================
# =========================================================
DB_POOL = SimpleConnectionPool(
    minconn=1,
    maxconn=int(os.getenv("DB_MAXCONN", "8")),
    dsn=DATABASE_URL
)

class _PooledConn:
    """Wrapper para manter o padrão 'with get_db_conn() as conn:' e devolver ao pool."""
    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            # rollback automático se algo falhou e ficou transação aberta
            if exc_type is not None:
                try:
                    self._conn.rollback()
                except:
                    pass
        finally:
            try:
                self._pool.putconn(self._conn)
            except:
                try:
                    self._conn.close()
                except:
                    pass

def get_db_conn():
    return _PooledConn(DB_POOL)

def criar_tabela_usuarios():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios (
                    id TEXT PRIMARY KEY,
                    saldo DOUBLE PRECISION DEFAULT 0,
                    numeros TEXT NOT NULL,
                    refer TEXT,
                    indicados TEXT
                )
            """)
            conn.commit()

def criar_tabela_numeros_sms():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS numeros_sms (
                    aid TEXT PRIMARY KEY,
                    user_id TEXT,
                    price DOUBLE PRECISION,
                    cancelado BOOLEAN DEFAULT FALSE,
                    sms_recebido BOOLEAN DEFAULT FALSE,
                    data_criacao TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()

def criar_tabela_payments():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    raw JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()

# cria tabelas
criar_tabela_usuarios()
criar_tabela_numeros_sms()
criar_tabela_payments()

# =========================================================
# =================== LOG EM TELEGRAM =====================
# =========================================================
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        # manda só WARNING+ pro bot de alerta (reduz carga)
        if not alert_bot or not ALERT_CHAT_ID or record.levelno < logging.WARNING:
            return
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

# =========================================================
# ======= SERVICES JSON + MAPA DE SERVIÇOS DINÂMICO =======
# =========================================================
services_index_lock = threading.Lock()
services_index = {}  # { "service_id_str": {"title":..., "activate_org_code":...} }

def load_services_index(path=SERVICES_JSON):
    global services_index
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        index = {}
        for sid, payload in data.items():
            index[str(sid)] = {
                "title": payload.get("title"),
                "activate_org_code": payload.get("activate_org_code"),
            }
        with services_index_lock:
            services_index = index
        logger.info(f"[SERVICES] Carregado {len(index)} serviços do {path}")
    except Exception as e:
        logger.error(f"[SERVICES] Erro ao carregar {path}: {e}")

load_services_index()

SERVICE_CODE_LOCK = threading.Lock()
GLOBAL_SERVICE_MAP = {
    'mercado': 'cq',
    'china':   'ev',
    'china2':  'hw',   # será atualizado pelo scanner
    'picpay':  'ev',
    'outros':  'ot'
}

def get_service_code(key):
    with SERVICE_CODE_LOCK:
        return GLOBAL_SERVICE_MAP.get(key)

def set_china2_service_code(new_code, reason=""):
    with SERVICE_CODE_LOCK:
        old = GLOBAL_SERVICE_MAP['china2']
        if old == new_code:
            return
        GLOBAL_SERVICE_MAP['china2'] = new_code
    logger.info(f"[SCANNER] China2: {old} → {new_code} {('['+reason+']') if reason else ''}")
    try:
        if admin_bot and ADMIN_CHAT_ID:
            admin_bot.send_message(
                ADMIN_CHAT_ID,
                f"🔄 China 2 atualizado: `{old}` → `{new_code}` {reason}".strip(),
                parse_mode='Markdown'
            )
    except:
        pass

# =========================================================
# ================== VARS / LOCKS EXISTENTES ==============
# =========================================================
data_lock        = threading.Lock()
status_lock      = threading.Lock()
status_map       = {}
PENDING_RECHARGE = {}
PRAZO_MINUTOS    = 23
PRAZO_SEGUNDOS   = PRAZO_MINUTOS * 60

# =========================================================
# ======================== USUÁRIO =========================
# =========================================================
def carregar_usuario(uid):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM usuarios WHERE id=%s", (str(uid),))
            user = cur.fetchone()
            if not user:
                return None
            user['numeros'] = json.loads(user['numeros'])
            user['indicados'] = json.loads(user.get('indicados', '[]') or '[]')
            return user

def salvar_usuario(user):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM usuarios WHERE id=%s", (str(uid),))
            exists = cur.fetchone()
            if exists:
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

def get_user_ref_link(uid):
    return f"https://t.me/{bot.get_me().username}?start={uid}"

def exportar_backup_json():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM usuarios")
            users = cur.fetchall()
            for u in users:
                u['numeros'] = json.loads(u['numeros'])
                u['indicados'] = json.loads(u.get('indicados', '[]') or '[]')
            with open("usuarios_backup.json", "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            enviar_documento_bot(backup_bot, BACKUP_CHAT_ID, "usuarios_backup.json")

# =========================================================
# ============= SALDO / NÚMEROS (mantido) =================
# =========================================================
def comprar_numero_atomico(uid, aid, price):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT saldo, numeros FROM usuarios WHERE id=%s FOR UPDATE", (str(uid),))
            user = cur.fetchone()
            if not user:
                return False
            saldo = user['saldo']
            numeros = json.loads(user['numeros'])
            if saldo < price:
                return False
            if aid in numeros:
                return False
            saldo -= price
            numeros.append(aid)
            cur.execute("""
                UPDATE usuarios SET saldo=%s, numeros=%s WHERE id=%s
            """, (saldo, json.dumps(numeros), str(uid)))
            cur.execute("""
                INSERT INTO numeros_sms (aid, user_id, price, cancelado, sms_recebido)
                VALUES (%s, %s, %s, FALSE, FALSE)
                ON CONFLICT (aid) DO NOTHING
            """, (aid, str(uid), price))
            conn.commit()
    exportar_backup_json()
    logger.info(f"Saldo de {uid} atualizado. Nº {aid} associado.")
    return True

def marcar_cancelado_e_devolver(uid, aid):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT cancelado, price FROM numeros_sms WHERE aid=%s FOR UPDATE", (aid,))
            row = cur.fetchone()
            if not row or row['cancelado']:
                return False
            price = row['price']
            cur.execute("UPDATE numeros_sms SET cancelado=TRUE WHERE aid=%s", (aid,))
            cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (price, str(uid)))
            conn.commit()
    exportar_backup_json()
    return True

def registrar_sms_recebido(aid):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("UPDATE numeros_sms SET sms_recebido=TRUE WHERE aid=%s", (aid,))
            conn.commit()

# =========================================================
# ================== SMSBOWER (mantido) ===================
# =========================================================
def enviar_mensagem_bot(bot_instance, chat_id, texto, tentativas=3):
    for _ in range(tentativas):
        try:
            bot_instance.send_message(chat_id, texto)
            return True
        except Exception:
            time.sleep(1)
    return False

def enviar_documento_bot(bot_instance, chat_id, file_path, tentativas=3):
    for _ in range(tentativas):
        try:
            with open(file_path, "rb") as bf:
                bot_instance.send_document(chat_id, bf)
            return True
        except Exception:
            time.sleep(1)
    return False

def solicitar_numero(servico, max_price=None):
    params = {
        'api_key': API_KEY_SMSBOWER,
        'action': 'getNumber',
        'service': servico,
        'country': COUNTRY_ID
    }
    if max_price is not None:
        params['maxPrice'] = f"{max_price:.3f}"  # suporta 0.001
    try:
        r = HTTP.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        # só loga sucesso/erros; evita flood
        if text.startswith("ACCESS_NUMBER:"):
            logging.info(f"GET_NUMBER OK")
        else:
            logging.debug(f"GET_NUMBER → {text}")
    except Exception as e:
        logger.error(f"Erro getNumber: {e}")
        return {"status":"error","message":str(e)}
    if text.startswith("ACCESS_NUMBER:"):
        _, aid, num = text.split(":", 2)
        return {"status":"success","id":aid,"number":num}
    return {"status":"error","message":text}

def cancelar_numero(aid):
    try:
        HTTP.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER, 'action':'setStatus','status':'8','id':aid},
            timeout=10
        )
        logger.info(f"Cancelado provider: {aid}")
    except Exception as e:
        logger.error(f"Erro cancelar: {e}")

def obter_status(aid):
    try:
        r = HTTP.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER,'action':'getStatus','id':aid},
            timeout=10
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus: {e}")
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
    msg_id  = info.get('sms_message_id')
    info.setdefault('codes', [])
    info['canceled_by_user'] = False

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            status = obter_status(aid)
            if info['canceled_by_user']:
                return
            if not status or status.startswith('STATUS_WAIT'):
                time.sleep(5)
                continue
            if status == 'STATUS_CANCEL':
                if not info['codes']:
                    ok = marcar_cancelado_e_devolver(info['user_id'], aid)
                    if ok:
                        bot.send_message(
                            chat_id,
                            f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido."
                        )
                return
            code = status.split(':', 1)[1] if ':' in status else status
            if code not in info['codes']:
                info['codes'].append(code)
                registrar_sms_recebido(aid)
                rt = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
                text = (
                    f"📦 {service}\n"
                    f"☎️ Número: `{full}`\n"
                    f"☎️ Sem DDI: `{short}`\n\n"
                )
                for idx, cd in enumerate(info['codes'], 1):
                    text += f"📩 SMS{idx}: `{cd}`\n"
                text += f"🕘 {rt}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.row(
                    telebot.types.InlineKeyboardButton(
                        '📲 Receber outro SMS',
                        callback_data=f'retry_{aid}'
                    )
                )
                kb.row(
                    telebot.types.InlineKeyboardButton(
                        '📲 Comprar serviços', callback_data='menu_comprar'
                    ),
                    telebot.types.InlineKeyboardButton(
                        '📜 Menu', callback_data='menu'
                    )
                )
                try:
                    if msg_id:
                        bot.edit_message_text(
                            text, chat_id, msg_id,
                            parse_mode='Markdown',
                            reply_markup=kb
                        )
                    else:
                        m = bot.send_message(
                            chat_id, text,
                            parse_mode='Markdown',
                            reply_markup=kb
                        )
                        info['sms_message_id'] = m.message_id
                except telebot.apihelper.ApiTelegramException as e:
                    if "message is not modified" not in str(e):
                        raise
                except Exception:
                    pass
            time.sleep(5)
    threading.Thread(target=check_sms, daemon=True).start()

# =========================================================
# ====================== MENUS (iguais) ===================
# =========================================================
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton(
            '📲 Comprar serviços', callback_data='menu_comprar'
        ),
        telebot.types.InlineKeyboardButton(
            '💰 Saldo', callback_data='menu_saldo'
        ),
        telebot.types.InlineKeyboardButton(
            '🤑 Recarregar', callback_data='menu_recarregar'
        ),
        telebot.types.InlineKeyboardButton(
            '👥 Referências', callback_data='menu_refer'
        ),
        telebot.types.InlineKeyboardButton(
            '📜 Meus números', callback_data='menu_numeros'
        ),
        telebot.types.InlineKeyboardButton(
            '🆘 Suporte', url='https://t.me/cpfbotttchina'
        )
    )
    bot.send_message(chat_id, 'Escolha uma opção:', reply_markup=kb)

@bot.callback_query_handler(lambda c: c.data == 'menu')
def callback_menu(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    send_menu(c.message.chat.id)

def show_comprar_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Mercado Pago SMS - R$0.75', callback_data='comprar_mercado'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS para China   - R$0.60', callback_data='comprar_china'),
        telebot.types.InlineKeyboardButton('🇨🇳 SMS para China 2 - R$0.60', callback_data='comprar_china2'),
        telebot.types.InlineKeyboardButton('💸 PicPay SMS       - R$0.65', callback_data='comprar_picpay'),
        telebot.types.InlineKeyboardButton('📡 Outros SMS        - R$1.10', callback_data='comprar_outros')
    )
    bot.send_message(chat_id, 'Escolha serviço:', reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(m):
    refer = None
    if m.text and m.text.startswith('/start ') and m.text.split(' ', 1)[1].isdigit():
        refer = m.text.split(' ', 1)[1]
    criar_usuario(m.from_user.id, refer=refer)
    send_menu(m.chat.id)

@bot.callback_query_handler(lambda c: c.data == 'menu_comprar')
def callback_menu_comprar(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    show_comprar_menu(c.message.chat.id)

@bot.message_handler(commands=['comprar'])
def cmd_comprar(m):
    criar_usuario(m.from_user.id)
    show_comprar_menu(m.chat.id)

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

@bot.message_handler(func=lambda m: PENDING_RECHARGE.get(m.from_user.id) and re.fullmatch(r"\d+(\.\d{1,2})?", m.text or ""))
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
    bot.send_message(m.chat.id, f"Para recarregar R$ {amount:.2f}, clique abaixo:", reply_markup=kb)
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
            inf = status_map.get(aid)
            if inf:
                txt += f"\n*ID:* `{aid}` `{inf['full']}` / `{inf['short']}`"
        bot.send_message(c.message.chat.id, txt, parse_mode='Markdown')
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
            try: nomes.append(f"- {id_}")
            except: nomes.append(f"- {id_}")
        text += "\n" + "\n".join(nomes)
    bot.send_message(c.message.chat.id, text, parse_mode='Markdown')
    send_menu(c.message.chat.id)

# =========================================================
# ================ COMPRAR (mantido c/ dinâmica) ==========
# =========================================================
@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    user_id, key = c.from_user.id, c.data.split('_')[1]
    criar_usuario(user_id)
    prices = {
        'mercado':0.75,
        'china':0.60,
        'china2':0.60,
        'picpay':0.65,
        'outros':1.10
    }
    names  = {
        'mercado':'Mercado Pago SMS',
        'china':'SMS para China',
        'china2':'SMS para China 2',
        'picpay':'PicPay SMS',
        'outros':'Outros SMS'
    }
    idsms  = {
        'mercado': get_service_code('mercado'),
        'china':   get_service_code('china'),
        'china2':  get_service_code('china2'),
        'picpay':  get_service_code('picpay'),
        'outros':  get_service_code('outros')
    }
    balance = carregar_usuario(user_id)['saldo']
    price, service = prices[key], names[key]
    if balance < price:
        return bot.answer_callback_query(c.id, '❌ Saldo insuficiente.', True)
    try:
        bot.edit_message_text('⏳ Solicitando número...', c.message.chat.id, c.message.message_id)
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e): raise
    except Exception:
        pass

    attempt_prices = []
    if key == 'china2':
        attempt_prices = [round(i/1000, 3) for i in range(1, 100+1)]  # 0.001..0.100
    elif key in ('mercado', 'picpay', 'china'):
        attempt_prices = [round(i/100, 2) for i in range(1, 10+1)]    # 0.01..0.10
    else:
        attempt_prices = [round(i/100, 2) for i in range(1, 19+1)]    # 0.01..0.19

    resp = {}
    for mpv in attempt_prices:
        resp = solicitar_numero(idsms[key], max_price=mpv)
        if resp.get('status') == 'success':
            break
    if resp.get('status') != 'success':
        return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')

    aid   = resp['id']
    full  = resp['number']
    short = full[2:] if full.startswith('55') else full

    ok = comprar_numero_atomico(user_id, aid, price)
    if not ok:
        return bot.send_message(c.message.chat.id, "⚠️ Erro ao descontar saldo ou duplicidade, tente novamente.")

    kb_blocked = telebot.types.InlineKeyboardMarkup()
    kb_blocked.row(
        telebot.types.InlineKeyboardButton(f'❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}')
    )
    kb_blocked.row(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
    )
    kb_unlocked = telebot.types.InlineKeyboardMarkup()
    kb_unlocked.row(
        telebot.types.InlineKeyboardButton(f'❌ Cancelar', callback_data=f'cancel_{aid}')
    )
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
    msg = bot.send_message(c.message.chat.id, text, parse_mode='Markdown', reply_markup=kb_blocked)
    status_map[aid] = {
        'user_id':    user_id,
        'price':      price,
        'chat_id':    msg.chat.id,
        'message_id': msg.message_id,
        'service':    service,
        'full':       full,
        'short':      short
    }
    spawn_sms_thread(aid)

    def countdown():
        for minute in range(PRAZO_MINUTOS):
            time.sleep(60)
            rem = PRAZO_MINUTOS - (minute + 1)
            info = status_map.get(aid)
            if not info: return
            new_text = (
                f"📦 {service}\n"
                f"☎️ Número: `{full}`\n"
                f"☎️ Sem DDI: `{short}`\n\n"
                f"🕘 Prazo: {rem} minutos\n\n"
                f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
            )
            kb_sel = kb_blocked if minute < 2 else kb_unlocked
            try:
                bot.edit_message_text(new_text, info['chat_id'], info['message_id'], parse_mode='Markdown', reply_markup=kb_sel)
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    raise
            except Exception:
                pass

    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        info = status_map.get(aid)
        if info and not info.get('codes') and not info.get('canceled_by_user'):
            cancelar_numero(aid)
            ok2 = marcar_cancelado_e_devolver(info['user_id'], aid)
            if ok2:
                try: bot.delete_message(info['chat_id'], info['message_id'])
                except: pass

    threading.Thread(target=countdown, daemon=True).start()
    threading.Thread(target=auto_cancel, daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    aid = c.data.split('_', 1)[1]
    try:
        HTTP.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'3','id':aid},
            timeout=10
        )
    except:
        pass
    bot.answer_callback_query(c.id, '🔄 Novo SMS solicitado.', show_alert=True)
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

# =========================================================
# ================== PAINEL ADMIN (igual) =================
# =========================================================
@app.route('/admin', methods=['GET', 'POST'])
def painel_admin():
    token = request.args.get('token', '')
    if token != PAINEL_TOKEN:
        return "Acesso negado.", 401

    msg_feedback = ""
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'enviar_mensagem':
            texto = request.form.get('texto')
            if texto:
                with get_db_conn() as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute("SELECT id FROM usuarios")
                        uids = [row['id'] for row in cur.fetchall()]
                enviados = 0
                for uid in uids:
                    try:
                        bot.send_message(int(uid), texto)
                        enviados += 1
                    except: pass
                msg_feedback = f'Mensagem enviada para {enviados} usuários.'
        elif action == 'adicionar_saldo':
            val = float(request.form.get('valor', '0'))
            todos = request.form.get('todos')
            uid  = request.form.get('userid')
            with get_db_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    if todos:
                        cur.execute("UPDATE usuarios SET saldo=saldo+%s", (val,))
                        conn.commit()
                        msg_feedback = f"Saldo de R$ {val:.2f} adicionado a TODOS os usuários."
                    elif uid:
                        cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (val, str(uid)))
                        conn.commit()
                        msg_feedback = f"Saldo de R$ {val:.2f} adicionado ao usuário {uid}."
            exportar_backup_json()

    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) FROM numeros_sms")
            total = cur.fetchone()['count']
            cur.execute("SELECT count(*) FROM numeros_sms WHERE cancelado=TRUE")
            cancelados = cur.fetchone()['count']
            cur.execute("SELECT count(*) FROM numeros_sms WHERE sms_recebido=TRUE")
            recebidos = cur.fetchone()['count']

    return render_template_string("""
        <h2>Painel Admin</h2>
        <form method=post>
            <h3>Enviar mensagem para todos:</h3>
            <textarea name="texto" rows=3 cols=40 placeholder="Mensagem"></textarea><br>
            <input type="hidden" name="action" value="enviar_mensagem">
            <button type="submit">Enviar Mensagem</button>
        </form>
        <form method=post>
            <h3>Adicionar saldo</h3>
            <input type="hidden" name="action" value="adicionar_saldo">
            Valor: <input name="valor" type="number" step="0.01" required>
            <input type="checkbox" name="todos" value="1"> Para TODOS<br>
            ou ID: <input name="userid" type="text" placeholder="UserID">
            <button type="submit">Adicionar Saldo</button>
        </form>
        <hr>
        <b>{{msg_feedback}}</b>
        <hr>
        <h3>Estatísticas</h3>
        <ul>
            <li>Total de números vendidos: {{total}}</li>
            <li>Números cancelados: {{cancelados}}</li>
            <li>Números que receberam SMS: {{recebidos}}</li>
        </ul>
    """, msg_feedback=msg_feedback, total=total, cancelados=cancelados, recebidos=recebidos)

# =========================================================
# =================== HEALTH / WEBHOOKS ===================
# =========================================================
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
    data = request.get_json() or {}
    if data.get('type') == 'payment':
        pid = data['data']['id']
        try:
            with get_db_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT 1 FROM payments WHERE id=%s", (str(pid),))
                    if cur.fetchone():
                        return '', 200  # já processado

            resp = mp_client.payment().get(pid)['response']
            if resp.get('status') == 'approved':
                ext = resp.get('external_reference', '')
                if ':' in ext:
                    uid_str, amt_str = ext.split(':', 1)
                    uid = int(uid_str)
                    amt = float(amt_str)

                    with get_db_conn() as conn:
                        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                            cur.execute("INSERT INTO payments (id, raw) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                        (str(pid), json.dumps(resp)))
                            conn.commit()

                    user = carregar_usuario(uid)
                    if user:
                        current = user.get('saldo', 0.0)
                        refid = user.get("refer")
                        bonus = 0.0
                        ref_text = ""
                        if refid:
                            ref_user = carregar_usuario(refid)
                            if ref_user:
                                bonus = round(amt * 0.10, 2)
                                with get_db_conn() as conn:
                                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                                        cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (bonus, str(refid)))
                                        conn.commit()
                                try:
                                    bot.send_message(int(refid), f"🎉 Você ganhou R$ {bonus:.2f} de bônus pois seu indicado recarregou saldo!")
                                except:
                                    pass
                                ref_text = f"\nIndicado por: {refid}\nBônus enviado: R$ {bonus:.2f}"

                        with get_db_conn() as conn:
                            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                                cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (amt, str(uid)))
                                conn.commit()
                        exportar_backup_json()
                        bot.send_message(uid, f"✅ Recarga de R$ {amt:.2f} confirmada! Seu novo saldo é R$ {current + amt:.2f}")
                        enviar_mensagem_bot(
                            admin_bot,
                            ADMIN_CHAT_ID,
                            f"💰 Novo DEPÓSITO\nUser: {uid}\nValor: R$ {amt:.2f}\nData: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}{ref_text}"
                        )
        except Exception as e:
            logger.error(f"[MP] Erro webhook: {e}")
    return '', 200

# =========================================================
# ================= SCANNER 20m (China 2) =================
# =========================================================
SCANNER_INTERVAL_SEC = 20 * 60  # 20 minutos
SCANNER_MIN_PRICE = 0.001
SCANNER_MAX_PRICE = 0.100
SCANNER_MIN_COUNT = 50
SCANNER_COUNTRY_ID = "14"  # Brazil na resposta do getPricesByService
_last_china2 = None  # para não logar/atualizar repetido

def _eligible_service_ids():
    with services_index_lock:
        return [int(sid) for sid, v in services_index.items() if v.get("activate_org_code")]

def scanner_loop():
    global _last_china2
    while True:
        try:
            best = None  # (min_price, sid, aoc, title, count)
            for sid in _eligible_service_ids():
                url = f"https://smsbower.org/activations/getPricesByService?serviceId={sid}&withPopular=true&rank=1"
                try:
                    r = HTTP.get(url, timeout=12)
                    if r.status_code != 200:
                        time.sleep(0.03)
                        continue
                    payload = r.json()
                except Exception:
                    time.sleep(0.03)
                    continue

                services = payload.get("services") or {}
                # alguns retornos podem não indexar pelo sid; pega o primeiro
                svc = services.get(str(sid)) or (next(iter(services.values())) if services else None)
                if not svc:
                    time.sleep(0.03)
                    continue

                br = (svc.get("countries") or {}).get(SCANNER_COUNTRY_ID)
                if not br:
                    time.sleep(0.03)
                    continue

                min_price = br.get("min_price")
                count = br.get("count", 0)
                try:
                    mpv = float(min_price)
                except:
                    time.sleep(0.03)
                    continue

                if count > SCANNER_MIN_COUNT and (SCANNER_MIN_PRICE <= mpv <= SCANNER_MAX_PRICE):
                    with services_index_lock:
                        si = services_index.get(str(sid)) or {}
                    aoc = si.get("activate_org_code")
                    title = si.get("title") or f"serviceId {sid}"
                    if aoc and ((best is None) or (mpv < best[0])):
                        best = (mpv, sid, aoc, title, count)

                time.sleep(0.03)  # evita rajada e 429

            if best:
                mpv, sid, aoc, title, count = best
                if _last_china2 != aoc:
                    _last_china2 = aoc
                    set_china2_service_code(aoc, reason=f"(id:{sid}, {title}, count:{count}, min_price:{mpv})")
                    logger.info(f"[SCANNER] Melhor China2: serviceId={sid} title={title} aoc={aoc} count={count} min_price={mpv}")
            else:
                logger.info("[SCANNER] Nenhum candidato elegível encontrado para China2. Mantendo atual.")
        except Exception as e:
            logger.error(f"[SCANNER] erro geral: {e}")

        time.sleep(SCANNER_INTERVAL_SEC)

# =========================================================
# ============== STARTUP ÚNICO (Railway/Gunicorn) =========
# =========================================================
_scanner_started = False
def start_background_once():
    global _scanner_started
    if _scanner_started:
        return
    _scanner_started = True
    threading.Thread(target=scanner_loop, daemon=True).start()

@app.before_first_request
def _kickoff():
    start_background_once()

# =========================================================
# ======================== MAIN ===========================
# =========================================================
if __name__ == '__main__':
    start_background_once()
    try:
        bot.remove_webhook()
        if SITE_URL:
            bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    except Exception as e:
        logger.error(f"Erro set_webhook: {e}")

    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
