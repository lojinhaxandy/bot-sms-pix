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
from flask import Flask, request, render_template_string

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

# >>> NOVO: canal público/privado para histórico de recargas
# se o canal for privado, defina HIST_CHANNEL como o ID numérico (-100xxxxxxxxxx)
HIST_CHANNEL      = os.getenv("HIST_CHANNEL", "@historico_recarregas")

# >>> API sms24h (Servidor 2)
SMS24H_URL        = "https://api.sms24h.org/stubs/handler_api"
API_KEY_SMS24H    = os.getenv("API_KEY_SMS24H")  # defina no ambiente

# =========================================================
# =========== BOTS / SDK / FLASK (mantidos) ===============
# =========================================================
bot         = telebot.TeleBot(BOT_TOKEN, threaded=True)
alert_bot   = telebot.TeleBot(ALERT_BOT_TOKEN) if ALERT_BOT_TOKEN else None
backup_bot  = telebot.TeleBot(BACKUP_BOT_TOKEN)
admin_bot   = telebot.TeleBot(ADMIN_BOT_TOKEN)
mp_client   = mercadopago.SDK(MP_ACCESS_TOKEN)
app = Flask(__name__)
PENDING_REACT = {}

# =========================================================
# ==================== BANCO DE DADOS =====================
# =========================================================
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def criar_tabela_usuarios():
    with get_db_conn() as conn, conn.cursor() as cur:
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
        with conn.cursor() as cur:
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
def criar_tabela_api_tokens():
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                user_id TEXT PRIMARY KEY,
                token TEXT UNIQUE NOT NULL
            )
        """)
        conn.commit()

criar_tabela_api_tokens()

def criar_tabela_payments():
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                raw JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()

# >>> NOVO: tabela para persistir configurações (preços, emojis, caps)
def criar_tabela_config():
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL
            )
        """)
        conn.commit()

criar_tabela_usuarios()
criar_tabela_numeros_sms()
criar_tabela_payments()
criar_tabela_config()

# =========================================================
# =================== LOG EM TELEGRAM =====================
# =========================================================
class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        if not alert_bot or not ALERT_CHAT_ID:
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

# Helper para logs administrativos (saldo novo, compra/cancelamento)
def log_admin(msg: str):
    try:
        if alert_bot and ALERT_CHAT_ID:
            alert_bot.send_message(ALERT_CHAT_ID, msg)
    except Exception:
        pass

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
    'china2':  'ki',   # atualizado pelo scanner
    'picpay':  'ev',
    'outros':  'ot'
}

def get_service_code(key):
    with SERVICE_CODE_LOCK:
        return GLOBAL_SERVICE_MAP.get(key)

# >>> preço atual escolhido pelo scanner p/ china2
SCANNER_LAST_PRICE = None
SCANNER_PRICE_LOCK = threading.Lock()

def set_china2_service_code(new_code, reason="", price=None):
    with SERVICE_CODE_LOCK:
        old = GLOBAL_SERVICE_MAP['china2']
        GLOBAL_SERVICE_MAP['china2'] = new_code
    if price is not None:
        try:
            p = float(price)
        except:
            p = None
        if p is not None:
            with SCANNER_PRICE_LOCK:
                global SCANNER_LAST_PRICE
                SCANNER_LAST_PRICE = p
    logger.info(f"[SCANNER] China 2: {old} → {new_code} {('['+reason+']') if reason else ''}")
    try:
        if alert_bot and ALERT_CHAT_ID:
            alert_bot.send_message(
                ALERT_CHAT_ID,
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

# ====== NOVO: controle do scanner (liga/desliga via painel) ======
SCANNER_ENABLED = True   # padrão ligado

# ====== NOVO: preços dinâmicos editáveis no painel ======
SERVICE_PRICES = {
    'mercado':  0.75,
    'mpsrv2':   0.90,
    'china':    0.60,
    'china2':   0.60,
    'picpay':   0.65,
    'picsrv2':  0.70,
    'wa1':      6.50,
    'wa2':      7.00,
    'outros':   1.10,
    'srv2':     0.77,
    # sms24h:
    'nubank':   0.90,
    'c6':       0.45,
    'neon':     0.39,
    # Servidor 1 / SMSBower:
    'c6srv1':   0.64,
    'google':   0.90,
    # Servidor 2 / sms24h:
    'googlesrv2': 0.90,
}

# >>> rótulos
SERVICE_NAMES = {
    'mercado': 'Mercado Pago SMS',
    'mpsrv2':  'Mercado Pago SMS Servidor 2',
    'china':   'SMS para China',
    'china2':  'SMS para China 2',
    'picpay':  'PicPay SMS',
    'picsrv2': 'PicPay SMS Servidor 2',
    'wa1':     'WhatsApp',
    'wa2':     'WhatsApp Servidor 2',
    'outros':  'Outros SMS',
    'srv2':    'Outros SMS Servidor 2',
    # sms24h:
    'nubank':  'Nubank SMS Servidor 2',
    'c6':      'C6 Bank SMS Servidor 2',
    'neon':    'Neon SMS Servidor 2',
    # Servidor 1:
    'c6srv1':  'C6 Bank SMS',
    'google':  'Google SMS',
    # Servidor 2:
    'googlesrv2': 'Google SMS Servidor 2',
}

# >>> emojis
SERVICE_EMOJIS = {
    'mercado': '📲',
    'mpsrv2':  '🛰️',
    'china':   '🇨🇳',
    'china2':  '🇨🇳',
    'picpay':  '💸',
    'picsrv2': '🛰️',
    'wa1':     '💬',
    'wa2':     '🛰️',
    'outros':  '📡',
    'srv2':    '🛰️',
    'nubank':  '🏦',
    'c6':      '🏦',
    'neon':    '🏦',
    'c6srv1':  '🏦',
    'google':  '🔍',
    'googlesrv2': '🔍',
}

# >>> CAP global editável para maxPrice no SMSBower
SMSBOWER_MAX_PRICE_CAP = 0.1754  # USD

# >>> NOVO: CAPs por serviço (Servidor 1 / SMSBower) - EDITÁVEIS NO PAINEL
DEFAULT_S1_CAPS = {
    'mercado': 0.10,
    'china':   0.10,
    'china2':  0.10,
    'picpay':  0.10,
    'wa1':     0.70,  # WA segue regra especial, mas limitado por este cap
    'outros':  0.20,
    'c6srv1':  0.08,  # solicitado
    'google':  0.11,  # solicitado
}
S1_CAPS = DEFAULT_S1_CAPS.copy()
import secrets
@bot.message_handler(func=lambda m: PENDING_REACT.get(m.from_user.id))
def handle_reactivate(m):
    uid = m.from_user.id
    aid_old = m.text.strip()
    PENDING_REACT.pop(uid, None)

    # Preço igual serviço 'srv2' (Servidor 2)
    price = SERVICE_PRICES.get('srv2', 0.90)

    user = carregar_usuario(uid)
    if user['saldo'] < price:
        return bot.send_message(m.chat.id, "❌ Saldo insuficiente.")

    # Chama API getExtraActivation
    try:
        r = requests.get(
            SMS24H_URL,
            params={
                'api_key': API_KEY_SMS24H,
                'action': 'getExtraActivation',
                'activationId': aid_old
            },
            timeout=12
        )
        r.raise_for_status()
        txt = r.text.strip()
    except Exception as e:
        return bot.send_message(m.chat.id, f"❌ Erro na API: {e}")

    if txt.startswith("ACCESS_NUMBER:"):
        _a, new_aid, new_phone = txt.split(":", 2)

        ok = comprar_numero_atomico(uid, new_aid, price)
        if not ok:
            return bot.send_message(m.chat.id, "⚠ Erro ao descontar saldo, tente novamente.")

        short = new_phone[2:] if new_phone.startswith("55") else new_phone

        bot.send_message(
            m.chat.id,
            f"♻ *Reativado com sucesso!*\n\n"
            f"🆔 *ID de ativação:* `{new_aid}`\n"
            f"☎️ Número: `{new_phone}`\n"
            f"☎️ Sem DDI: `{short}`",
            parse_mode="Markdown"
        )

        status_map[new_aid] = {
            'user_id': uid,
            'price': price,
            'chat_id': m.chat.id,
            'message_id': None,
            'service': SERVICE_NAMES['srv2'],
            'service_key': 'srv2',
            'full': new_phone,
            'short': short,
            'provider': 'sms24h',
            'codes': []
        }

        spawn_sms_thread(new_aid)
        return

    # Erros da API:
    bot.send_message(m.chat.id, f"❌ Não foi possível reativar.\nAPI: {txt}")

def get_or_create_api_token(user_id):
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT token FROM api_tokens WHERE user_id=%s", (str(user_id),))
        row = cur.fetchone()
        if row:
            return row['token']

        token = secrets.token_hex(32)
        cur.execute("INSERT INTO api_tokens (user_id, token) VALUES (%s, %s)", (str(user_id), token))
        conn.commit()
        return token

# ---------- Persistência de preços, emojis, caps ----------
def save_prices_emojis_to_db():
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_config (key, value) VALUES ('service_prices', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(SERVICE_PRICES),))
            cur.execute("""
                INSERT INTO app_config (key, value) VALUES ('service_emojis', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(SERVICE_EMOJIS),))
            conn.commit()
    except Exception as e:
        logger.error(f"[config] erro ao salvar preços/emojis: {e}")

def load_prices_emojis_from_db():
    global SERVICE_PRICES, SERVICE_EMOJIS
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config WHERE key IN ('service_prices','service_emojis')")
            rows = cur.fetchall()
        store = {r['key']: r['value'] for r in rows}
        if 'service_prices' in store and isinstance(store['service_prices'], dict):
            SERVICE_PRICES.update(store['service_prices'])
        else:
            save_prices_emojis_to_db()
        if 'service_emojis' in store and isinstance(store['service_emojis'], dict):
            SERVICE_EMOJIS.update(store['service_emojis'])
        else:
            save_prices_emojis_to_db()
    except Exception as e:
        logger.error(f"[config] erro ao carregar preços/emojis: {e}")

def save_smsg_cap_to_db():
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_config (key, value) VALUES ('smsbower_max_price_cap', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(SMSBOWER_MAX_PRICE_CAP),))
            conn.commit()
    except Exception as e:
        logger.error(f"[config] erro ao salvar cap do SMSBower: {e}")

def load_smsg_cap_from_db():
    global SMSBOWER_MAX_PRICE_CAP
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='smsbower_max_price_cap'")
            row = cur.fetchone()
        if row is not None:
            val = row['value']
            try:
                SMSBOWER_MAX_PRICE_CAP = float(val if not isinstance(val, dict) else val.get('cap', SMSBOWER_MAX_PRICE_CAP))
            except:
                pass
    except Exception as e:
        logger.error(f"[config] erro ao carregar cap do SMSBower: {e}")

# >>> NOVO: persistir/ler caps por serviço (Servidor 1)
def save_s1_caps_to_db():
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_config (key, value) VALUES ('smsbower_service_caps', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(S1_CAPS),))
            conn.commit()
    except Exception as e:
        logger.error(f"[config] erro ao salvar caps por serviço: {e}")

def load_s1_caps_from_db():
    global S1_CAPS
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='smsbower_service_caps'")
            row = cur.fetchone()
        if row and isinstance(row['value'], dict):
            S1_CAPS.update(row['value'])
        else:
            save_s1_caps_to_db()
    except Exception as e:
        logger.error(f"[config] erro ao carregar caps por serviço: {e}")

# carregar persistidos
load_prices_emojis_from_db()
load_smsg_cap_from_db()
load_s1_caps_from_db()

# =========================================================
# ======================== USUÁRIO =========================
# =========================================================
def carregar_usuario(uid):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE id=%s", (str(uid),))
            user = cur.fetchone()
            if not user:
                return None
            user['numeros'] = json.loads(user['numeros'])
            user['indicados'] = json.loads(user.get('indicados', '[]') or '[]')
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
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios")
            users = cur.fetchall()
            for u in users:
                u['numeros'] = json.loads(u['numeros'])
                u['indicados'] = json.loads(u.get('indicados', '[]') or '[]')
            with open("usuarios_backup.json", "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            enviar_documento_bot(backup_bot, BACKUP_CHAT_ID, "usuarios_backup.json")

# =========================================================
# ============= serviço api =================
# =========================================================
@bot.message_handler(commands=['token'])
def cmd_token(m):
    criar_usuario(m.from_user.id)
    tk = get_or_create_api_token(m.from_user.id)
    bot.send_message(m.chat.id, f"🔑 Seu token API:\n`{tk}`", parse_mode='Markdown')
@app.route('/api/buy', methods=['POST'])
def api_buy():
    data = request.json or {}

    token = data.get("token")
    service = data.get("service")  # exemplo: mercado, china, google...

    if not token or not service:
        return {"error": "token e service são obrigatórios"}, 400

    # validar token
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user_id = row['user_id']

    # validar user
    user = carregar_usuario(user_id)
    if not user:
        return {"error": "usuário inexistente"}, 404

    # validar serviço
    if service not in SERVICE_PRICES:
        return {"error": "serviço inválido"}, 400

    price = SERVICE_PRICES[service]
    saldo = user['saldo']

    if saldo < price:
        return {"error": "saldo insuficiente", "saldo": saldo}, 402

    # identificar provider
    idsms = {
        'mercado': get_service_code('mercado'),
        'mpsrv2':  'cq',
        'china':   get_service_code('china'),
        'china2':  get_service_code('china2'),
        'picpay':  get_service_code('picpay'),
        'picsrv2': 'ev',
        'wa1':     'wa',
        'wa2':     'wa',
        'outros':  get_service_code('outros'),
        'srv2':    'ot',
        'nubank':  'aaa',
        'c6':      'aff',
        'neon':    'aex',
        'c6srv1':  get_service_code('outros'),
        'google':  'go',
        'googlesrv2': 'go',
    }

    service_code = idsms.get(service)

    if not service_code:
        return {"error": "serviço sem configuração"}, 400

    # servidor SMS24H
    if service in ('srv2', 'mpsrv2', 'picsrv2', 'wa2', 'nubank', 'c6', 'neon', 'googlesrv2'):
        resp = solicitar_numero_sms24h(service_code)
        provider = 'sms24h'
        serviodr = 'servidor 2'
    else:
        resp = solicitar_numero_smsbower(service_code)
        provider = 'smsbower'
        serviodr = 'servidor 1'
    if resp.get('status') != 'success':
        return {"error": "sem números disponíveis"}, 503

    aid = resp['id']
    full = resp['number']
    short = full[2:] if full.startswith('55') else full

    ok = comprar_numero_atomico(user_id, aid, price)

    if not ok:
        return {"error": "erro ao descontar saldo / duplicidade"}, 500

    # registrar para thread de SMS
    status_map[aid] = {
        "user_id": user_id,
        "price": price,
        "service": service,
        "service_key": service,
        "full": full,
        "short": short,
        "provider": provider,
        "chat_id": None,  # Sem envio para Telegram
        "message_id": None,
        "is_api": True
    }

    spawn_sms_thread(aid)

    return {
        "status": "success",
        "aid": aid,
        "number": full,
        "short": short,
        "price": price,
        "saldo_restante": float(carregar_usuario(user_id)['saldo']),
        "provider": serviodr
    }, 200

@app.route('/api/status', methods=['POST'])
def api_status():
    data = request.json or {}

    token = data.get("token")
    aid   = data.get("aid")

    if not token or not aid:
        return {"error": "token e aid são obrigatórios"}, 400

    # validar token
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user_id = row['user_id']

    info = status_map.get(aid)

    if not info:
        return {"error": "aid inválido ou expirado"}, 404

    if str(info['user_id']) != str(user_id):
        return {"error": "este número não pertence ao usuário"}, 403

    status_resp = obter_status(aid, info['provider'])

    # nada ainda?
    if not status_resp or status_resp.startswith("STATUS_WAIT"):
        return {
            "status": "waiting",
            "sms": info.get("codes", [])
        }

    # cancelado pelo provider
    if status_resp == "STATUS_CANCEL":
        return {
            "status": "canceled",
            "sms": info.get("codes", [])
        }

    # recebeu SMS
    if "STATUS_OK" in status_resp or ":" in status_resp:
        code = status_resp.split(":", 1)[1]
        if code not in info['codes']:
            info['codes'].append(code)
            registrar_sms_recebido(aid)

        return {
            "status": "received",
            "sms": info['codes']
        }

    return {"status": "unknown", "raw": status_resp}

@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    data = request.json or {}

    token = data.get("token")
    aid   = data.get("aid")

    if not token or not aid:
        return {"error": "token e aid são obrigatórios"}, 400

    # validar token
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user_id = row['user_id']
    info = status_map.get(aid)

    if not info:
        return {"error": "aid inválido ou expirado"}, 404

    if str(info['user_id']) != str(user_id):
        return {"error": "este número não pertence ao usuário"}, 403

    if info.get("codes"):
        return {"error": "não pode cancelar após receber SMS"}, 403

    info['canceled_by_user'] = True
    cancelar_numero(aid, info['provider'])
    marcar_cancelado_e_devolver(user_id, aid)

    return {"status": "canceled", "saldo": carregar_usuario(user_id)['saldo']}
@app.route('/api/retry', methods=['POST'])
def api_retry():
    data = request.json or {}

    token = data.get("token")
    aid   = data.get("aid")

    if not token or not aid:
        return {"error": "token e aid são obrigatórios"}, 400

    # validar token
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user_id = str(row['user_id'])

    info = status_map.get(aid)

    if not info:
        return {"error": "aid inválido ou expirado"}, 404

    if str(info['user_id']) != user_id:
        return {"error": "este número não pertence ao usuário"}, 403

    provider = info.get("provider", "smsbower")

    # - Se já recebeu SMS, permite pedir outro (igual ao Telegram)
    # - Se não recebeu ainda, também permite
    try:
        if provider == "smsbower":
            requests.get(
                SMSBOWER_URL,
                params={
                    'api_key': API_KEY_SMSBOWER,
                    'action': 'setStatus',
                    'status': '3',
                    'id': aid
                },
                timeout=10
            )
        else:
            set_status_sms24h(aid, 3)

        # reinicia o monitor de SMS
        spawn_sms_thread(aid)

        return {
            "status": "retry_sent",
            "message": "Outro SMS foi solicitado com sucesso.",
            "aid": aid,
        }

    except Exception as e:
        return {"error": "falha ao solicitar novo SMS", "details": str(e)}, 500
@app.route('/api/balance', methods=['POST'])
def api_balance():
    data = request.json or {}
    token = data.get("token")

    if not token:
        return {"error": "token é obrigatório"}, 400

    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user = carregar_usuario(row['user_id'])

    return {
        "user_id": row['user_id'],
        "saldo": float(user['saldo'])
    }
@app.route('/api/wait', methods=['POST'])
def api_wait():
    data = request.json or {}

    token = data.get("token")
    aid   = data.get("aid")
    timeout = int(data.get("timeout", 90))  # padrão: 90 segundos

    if not token or not aid:
        return {"error": "token e aid são obrigatórios"}, 400

    # validar token
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM api_tokens WHERE token=%s", (token,))
        row = cur.fetchone()

    if not row:
        return {"error": "token inválido"}, 401

    user_id = str(row['user_id'])

    info = status_map.get(aid)
    if not info:
        return {"error": "aid inválido ou expirado"}, 404

    if str(info['user_id']) != user_id:
        return {"error": "este número não pertence ao usuário"}, 403

    provider = info['provider']

    # 1. Se já tem SMS, retorna imediatamente
    if info.get("codes"):
        return {
            "status": "received",
            "sms": info["codes"]
        }

    # 2. Loop de long polling
    end_time = time.time() + timeout

    while time.time() < end_time:
        status_resp = obter_status(aid, provider)

        # CANCELADO
        if status_resp == "STATUS_CANCEL":
            return {
                "status": "canceled",
                "sms": info.get("codes", [])
            }

        # RECEBEU SMS
        if status_resp and ":" in status_resp:
            code = status_resp.split(":", 1)[1]

            if code not in info["codes"]:
                info["codes"].append(code)
                registrar_sms_recebido(aid)

            return {
                "status": "received",
                "sms": info["codes"]
            }

        time.sleep(3)

    # 3. Timeout atingido → retorna que está aguardando
    return {
        "status": "waiting",
        "sms": info.get("codes", []),
        "timeout": True
    }

# =========================================================
# ============= SALDO / NÚMEROS (mantido) =================
# =========================================================
def comprar_numero_atomico(uid, aid, price):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
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
    # >>> LOG ADMIN: compra com saldo novo
    log_admin(f"🧾 *COMPRA*\nUser: `{uid}`\nAID: `{aid}`\nPreço: R$ {price:.2f}\nNovo saldo: R$ {saldo:.2f}")
    return True

def marcar_cancelado_e_devolver(uid, aid):
    novo_saldo = None
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cancelado, price FROM numeros_sms WHERE aid=%s FOR UPDATE", (aid,))
            row = cur.fetchone()
            if not row or row['cancelado']:
                return False
            price = row['price']
            cur.execute("UPDATE numeros_sms SET cancelado=TRUE WHERE aid=%s", (aid,))
            cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (price, str(uid)))
            # pegar saldo atualizado
            cur.execute("SELECT saldo FROM usuarios WHERE id=%s", (str(uid),))
            res = cur.fetchone()
            if res:
                novo_saldo = float(res['saldo'])
            conn.commit()
    exportar_backup_json()
    # >>> LOG ADMIN: cancelamento/reembolso com saldo novo
    if novo_saldo is not None:
        log_admin(f"↩️ *CANCELAMENTO / REEMBOLSO*\nUser: `{uid}`\nAID: `{aid}`\nValor devolvido: R$ {price:.2f}\nNovo saldo: R$ {novo_saldo:.2f}")
    return True

def registrar_sms_recebido(aid):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
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

def solicitar_numero_smsbower(servico, max_price=None):
    params = {
        'api_key': API_KEY_SMSBOWER,
        'action': 'getNumber',
        'service': servico,
        'country': COUNTRY_ID
    }
    if max_price is not None:
        params['maxPrice'] = f"{max_price:.4f}"
    try:
        r = requests.get(SMSBOWER_URL, params=params, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        logger.info(f"GET_NUMBER (smsbower) → {text}")
    except Exception as e:
        logger.error(f"Erro getNumber smsbower: {e}")
        return {"status":"error","message":str(e)}
    if text.startswith("ACCESS_NUMBER:"):
        _, aid, num = text.split(":", 2)
        return {"status":"success","id":aid,"number":num}
    return {"status":"error","message":text}

def cancelar_numero_smsbower(aid):
    try:
        requests.get(
            SMSBOWER_URL,
            params={'api_key':API_KEY_SMSBOWER, 'action':'setStatus','status':'8','id':aid},
            timeout=10
        )
        logger.info(f"Cancelado provider (smsbower): {aid}")
    except Exception as e:
        logger.error(f"Erro cancelar smsbower: {e}")

def obter_status_smsbower(aid):
    try:
        r = requests.get(
            SMSBOWER_URL,
            params={'api_key': API_KEY_SMSBOWER,'action':'getStatus','id':aid},
            timeout=10
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus smsbower: {e}")
        return None

# ============== SMS24H (Servidor 2) ======================
def sms24h_key_ok():
    if not API_KEY_SMS24H:
        logger.error("[sms24h] API_KEY_SMS24H não definido no ambiente")
        return False
    return True

def solicitar_numero_sms24h(service_code, operator="any", country="73"):
    if not sms24h_key_ok():
        return {"status":"error","message":"NO_KEY"}
    for op in [operator, "any"]:
        try:
            r = requests.get(
                SMS24H_URL,
                params={
                    'api_key': API_KEY_SMS24H,
                    'action': 'getNumber',
                    'service': service_code,
                    'operator': op,
                    'country': country
                },
                timeout=15
            )
            r.raise_for_status()
            text = r.text.strip()
            logger.info(f"GET_NUMBER (sms24h {op}) → {text}")
        except Exception as e:
            logger.error(f"Erro getNumber sms24h ({op}): {e}")
            continue
        if text.startswith("ACCESS_NUMBER:"):
            _, aid, num = text.split(":", 2)
            return {"status":"success","id":aid,"number":num}
    return {"status":"error","message":"NO_NUMBERS"}

def obter_status_sms24h(aid):
    if not sms24h_key_ok():
        return None
    try:
        r = requests.get(
            SMS24H_URL,
            params={'api_key': API_KEY_SMS24H, 'action': 'getStatus', 'id': aid},
            timeout=10
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.error(f"Erro getStatus sms24h: {e}")
        return None

def set_status_sms24h(aid, status):
    """
    status: 1 (SMS enviado), 3 (repetir SMS), 6 (finalizar), 8 (cancelar)
    """
    if not sms24h_key_ok():
        return None
    try:
        r = requests.get(
            SMS24H_URL,
            params={'api_key': API_KEY_SMS24H, 'action': 'setStatus', 'status': status, 'id': aid},
            timeout=10
        )
        r.raise_for_status()
        txt = r.text.strip()
        logger.info(f"setStatus sms24h({status}) → {txt}")
        return txt
    except Exception as e:
        logger.error(f"Erro setStatus sms24h: {e}")
        return None

# >>> dispatcher de status por provider
def obter_status(aid, provider):
    if provider == 'sms24h':
        return obter_status_sms24h(aid)
    return obter_status_smsbower(aid)

def cancelar_numero(aid, provider):
    if provider == 'sms24h':
        set_status_sms24h(aid, 8)  # cancelar
        return
    cancelar_numero_smsbower(aid)

# ========= menor preço via getPricesV2 (smsbower) =========
def obter_menor_preco_v2(service_code, country_id):
    try:
        r = requests.get(
            SMSBOWER_URL,
            params={
                'api_key': API_KEY_SMSBOWER,
                'action': 'getPricesV2',
                'service': service_code,
                'country': country_id
            },
            timeout=12
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"[getPricesV2] erro: {e}")
        return None

    country_map = data.get(str(country_id)) or data.get(country_id) or {}
    svc_map = country_map.get(service_code) or {}
    if not isinstance(svc_map, dict) or not svc_map:
        return None

    candidatos = []
    for price_str, qty in svc_map.items():
        try:
            p = float(str(price_str).replace(',', '.'))
        except:
            continue
        try:
            q = int(qty)
        except:
            q = 0
        if q > 4:
            candidatos.append(p)

    if not candidatos:
        return None

    candidatos.sort()
    return candidatos[0]

# ========= preço WA especial: decrescente até <= cap com qty>1 =========
def obter_preco_wa_desc_v2(service_code, country_id, max_usd=0.7):
    try:
        r = requests.get(
            SMSBOWER_URL,
            params={
                'api_key': API_KEY_SMSBOWER,
                'action': 'getPricesV2',
                'service': service_code,
                'country': country_id
            },
            timeout=12
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"[getPricesV2/WA] erro: {e}")
        return None

    country_map = data.get(str(country_id)) or data.get(country_id) or {}
    svc_map = country_map.get(service_code) or {}
    if not isinstance(svc_map, dict) or not svc_map:
        return None

    candidatos = []
    for price_str, qty in svc_map.items():
        try:
            p = float(str(price_str).replace(',', '.'))
        except:
            continue
        try:
            q = int(qty)
        except:
            q = 0
        if p <= max_usd and q > 5:
            candidatos.append((p, q))

    if not candidatos:
        return None

    candidatos.sort(key=lambda x: x[0], reverse=True)
    escolhido = candidatos[0][0]
    logger.info(f"[WA] preço escolhido (<= {max_usd} c/ qty>5): {escolhido}")
    return escolhido

# =========================================================
# ====================== MENUS (iguais) ===================
# =========================================================
def send_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
        telebot.types.InlineKeyboardButton('♻ Reativar número Servidor 2', callback_data='reativar_s2'),
        telebot.types.InlineKeyboardButton('🔑 Token API', callback_data='menu_token'),
        telebot.types.InlineKeyboardButton('🆔 Meu ID', callback_data='menu_id'),
        telebot.types.InlineKeyboardButton('💰 Saldo', callback_data='menu_saldo'),
        telebot.types.InlineKeyboardButton('🤑 Recarregar', callback_data='menu_recarregar'),
        telebot.types.InlineKeyboardButton('👥 Referências', callback_data='menu_refer'),
        telebot.types.InlineKeyboardButton('📜 Meus números', callback_data='menu_numeros'),
        telebot.types.InlineKeyboardButton('🆘 Suporte', url='https://t.me/cpfbotttchina')
    )
    bot.send_message(chat_id, 'Escolha uma opção:', reply_markup=kb)


@bot.callback_query_handler(lambda c: c.data == 'menu')
def callback_menu(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    send_menu(c.message.chat.id)
@bot.callback_query_handler(lambda c: c.data == 'menu_token')
def menu_token(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    tok = get_or_create_api_token(c.from_user.id)
    bot.send_message(c.message.chat.id, f"🔑 *Seu Token API:*\n`{tok}`", parse_mode="Markdown")


@bot.callback_query_handler(lambda c: c.data == 'menu_id')
def menu_id(c):
    try: bot.answer_callback_query(c.id)
    except: pass
    bot.send_message(c.message.chat.id, f"🆔 *Seu ID:* `{c.from_user.id}`", parse_mode="Markdown")

def show_comprar_menu(chat_id):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)

    def add_btn(key):
        price = SERVICE_PRICES.get(key, 0.0)
        label = f"{SERVICE_EMOJIS.get(key, '•')} {SERVICE_NAMES.get(key, key)} - R${price:.2f}"
        kb.add(telebot.types.InlineKeyboardButton(label, callback_data=f'comprar_{key}'))

    add_btn('mercado')
    add_btn('mpsrv2')
    add_btn('china')
    add_btn('china2')
    add_btn('picpay')
    add_btn('picsrv2')
    add_btn('wa1')
    add_btn('wa2')

    # Google
    add_btn('google')
    add_btn('googlesrv2')

    # Novos (todos sms24h) + C6 (Srv1 via SMSBower) ACIMA do C6 Srv2
    add_btn('nubank')
    add_btn('c6srv1')
    add_btn('c6')
    add_btn('neon')

    add_btn('outros')
    add_btn('srv2')

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
    bot.send_message(c.message.chat.id, 'Digite o valor (em reais) que deseja recarregar:')

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
    kb.row(telebot.types.InlineKeyboardButton(f"💳 Pagar R$ {amount:.2f}", url=pay_url))
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
@bot.callback_query_handler(lambda c: c.data == 'reativar_s2')
def reativar_s2(c):
    try: bot.answer_callback_query(c.id)
    except: pass

    bot.send_message(
        c.message.chat.id,
        "♻ *Reativar número – Servidor 2*\n\n"
        "Cole abaixo o *ID de ativação (AID)* do número anterior.\n\n"
        "⚠ *Aviso:* a reativação NÃO é garantida.\n"
        "Se funcionar, o valor será descontado automaticamente.",
        parse_mode="Markdown"
    )

    PENDING_REACT[c.from_user.id] = True

# =========================================================
# ================ COMPRAR (com V2 + srv2) ================
# =========================================================
@bot.callback_query_handler(lambda c: c.data.startswith('comprar_'))
def cb_comprar(c):
    user_id, key = c.from_user.id, c.data.split('_')[1]
    criar_usuario(user_id)

    prices = SERVICE_PRICES
    names  = SERVICE_NAMES

    idsms = {
        'mercado': get_service_code('mercado'),  # smsbower
        'mpsrv2':  'cq',                         # sms24h - Mercado
        'china':   get_service_code('china'),    # smsbower
        'china2':  get_service_code('china2'),   # smsbower
        'picpay':  get_service_code('picpay'),   # smsbower
        'picsrv2': 'ev',                         # sms24h - PicPay
        'wa1':     'wa',                         # smsbower - WhatsApp
        'wa2':     'wa',                         # sms24h  - WhatsApp
        'outros':  get_service_code('outros'),   # smsbower
        'srv2':    'ot',                         # sms24h - Outros
        # sms24h:
        'nubank':  'aaa',
        'c6':      'aff',
        'neon':    'aex',
        # Servidor 1:
        'c6srv1':  get_service_code('outros'),
        'google':  'go',
        # Servidor 2:
        'googlesrv2': 'go',
    }

    balance = carregar_usuario(user_id)['saldo']
    price   = prices.get(key)
    service = names.get(key)

    if (price is None) or (service is None) or (key not in idsms):
        return bot.answer_callback_query(c.id, '❌ Opção inválida.', True)

    if balance < price:
        return bot.answer_callback_query(c.id, '❌ Saldo insuficiente.', True)

    # ============ fluxo sms24h (Servidor 2) ============
    if key in ('srv2', 'mpsrv2', 'picsrv2', 'wa2', 'nubank', 'c6', 'neon', 'googlesrv2'):
        resp = solicitar_numero_sms24h(idsms[key], operator="any", country=COUNTRY_ID)
        if resp.get('status') != 'success':
            return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')
        aid   = resp['id']
        full  = resp['number']
        short = full[2:] if full.startswith('55') else full

        ok = comprar_numero_atomico(user_id, aid, price)
        if not ok:
            return bot.send_message(c.message.chat.id, "⚠️ Erro ao descontar saldo ou duplicidade, tente novamente.")

        kb_blocked = telebot.types.InlineKeyboardMarkup()
        kb_blocked.row(telebot.types.InlineKeyboardButton('❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}'))
        kb_blocked.row(telebot.types.InlineKeyboardButton('🛒 Comprar mesmo serviço', callback_data=f'comprar_{key}'))
        kb_blocked.row(telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                       telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu'))
        kb_unlocked = telebot.types.InlineKeyboardMarkup()
        kb_unlocked.row(telebot.types.InlineKeyboardButton('❌ Cancelar', callback_data=f'cancel_{aid}'))
        kb_unlocked.row(telebot.types.InlineKeyboardButton('🛒 Comprar mesmo serviço', callback_data=f'comprar_{key}'))
        kb_unlocked.row(telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                        telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu'))
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
            'service_key': key,
            'full':       full,
            'short':      short,
            'provider':   'sms24h'
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
                    f"🆔 *ID de ativação:* `{aid}`\n"
                    f"☎️ Número: `{full}`\n"
                    f"☎️ Sem DDI: `{short}`\n\n"
                    f"🕘 Prazo: {PRAZO_MINUTOS} minutos\n\n"
                    f"💡 Ativo por {PRAZO_MINUTOS} minutos; sem SMS, saldo devolvido automaticamente."
                )
                kb_sel = kb_blocked if minute < 2 else kb_unlocked
                try:
                    bot.edit_message_text(new_text, info['chat_id'], info['message_id'],
                                          parse_mode='Markdown', reply_markup=kb_sel)
                except telebot.apihelper.ApiTelegramException as e:
                    if "message is not modified" not in str(e): raise
                except Exception:
                    pass

        def auto_cancel():
            time.sleep(PRAZO_SEGUNDOS)
            info = status_map.get(aid)
            if info and not info.get('codes') and not info.get('canceled_by_user'):
                cancelar_numero(aid, provider='sms24h')
                ok2 = marcar_cancelado_e_devolver(info['user_id'], aid)
                if info.get("is_api"):
                    # salvar log opcional
                    logger.info(f"[API] Cancelamento automático devolveu saldo para {info['user_id']} (AID: {aid})")
                    return
                if ok2:
                    try: bot.delete_message(info['chat_id'], info['message_id'])
                    except: pass

        threading.Thread(target=countdown, daemon=True).start()
        threading.Thread(target=auto_cancel, daemon=True).start()
        return

    # ================== fluxo smsbower (Servidor 1) ==================
    s1_effective_cap = min(float(SMSBOWER_MAX_PRICE_CAP), float(S1_CAPS.get(key, 0.10)))

    # Descobrir o "menor" preço (ou regra especial do WA)
    if key == 'wa1':
        base_max_price = obter_preco_wa_desc_v2(idsms[key], COUNTRY_ID, max_usd=s1_effective_cap)
    elif key == 'china2':
        with SCANNER_PRICE_LOCK:
            mp = SCANNER_LAST_PRICE
        base_max_price = float(mp) if mp is not None else obter_menor_preco_v2(idsms[key], COUNTRY_ID)
    else:
        base_max_price = obter_menor_preco_v2(idsms[key], COUNTRY_ID)

    if (base_max_price is None) or (float(base_max_price) > s1_effective_cap):
        return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')

    # Forçar compra no preço mínimo encontrado (para realmente “comprar o menor”)
    resp = solicitar_numero_smsbower(idsms[key], max_price=float(base_max_price))
    if resp.get('status') != 'success':
        return bot.send_message(c.message.chat.id, '🚫 Sem números disponíveis.')

    aid   = resp['id']
    full  = resp['number']
    short = full[2:] if full.startswith('55') else full

    ok = comprar_numero_atomico(user_id, aid, price)
    if not ok:
        return bot.send_message(c.message.chat.id, "⚠️ Erro ao descontar saldo ou duplicidade, tente novamente.")

    kb_blocked = telebot.types.InlineKeyboardMarkup()
    kb_blocked.row(telebot.types.InlineKeyboardButton('❌ Cancelar (2m)', callback_data=f'cancel_blocked_{aid}'))
    kb_blocked.row(telebot.types.InlineKeyboardButton('🛒 Comprar mesmo serviço', callback_data=f'comprar_{key}'))
    kb_blocked.row(telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                   telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu'))
    kb_unlocked = telebot.types.InlineKeyboardMarkup()
    kb_unlocked.row(telebot.types.InlineKeyboardButton('❌ Cancelar', callback_data=f'cancel_{aid}'))
    kb_unlocked.row(telebot.types.InlineKeyboardButton('🛒 Comprar mesmo serviço', callback_data=f'comprar_{key}'))
    kb_unlocked.row(telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                    telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu'))
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
        'service_key': key,
        'full':       full,
        'short':      short,
        'provider':   'smsbower'
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
                bot.edit_message_text(new_text, info['chat_id'], info['message_id'],
                                      parse_mode='Markdown', reply_markup=kb_sel)
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e): raise
            except Exception:
                pass

    def auto_cancel():
        time.sleep(PRAZO_SEGUNDOS)
        info = status_map.get(aid)
        if info and not info.get('codes') and not info.get('canceled_by_user'):
            cancelar_numero(aid, provider='smsbower')
            ok2 = marcar_cancelado_e_devolver(info['user_id'], aid)
            if info.get("is_api"):
                # salvar log opcional
                logger.info(f"[API] Cancelamento automático devolveu saldo para {info['user_id']} (AID: {aid})")
                return
            if ok2:
                try: bot.delete_message(info['chat_id'], info['message_id'])
                except: pass

    threading.Thread(target=countdown, daemon=True).start()
    threading.Thread(target=auto_cancel, daemon=True).start()

# =========================================================
# ===================== STATUS / CANCEL ===================
# =========================================================
def spawn_sms_thread(aid):
    with status_lock:
        info = status_map.get(aid)
    if not info:
        return

    provider = info.get('provider', 'smsbower')
    service  = info['service']
    full     = info['full']
    short    = info['short']
    chat_id  = info['chat_id']
    msg_id   = info.get('sms_message_id')
    service_key = info.get('service_key', 'outros')
    info.setdefault('codes', [])
    info['canceled_by_user'] = False

    def check_sms():
        start = time.time()
        while time.time() - start < PRAZO_SEGUNDOS:
            status = obter_status(aid, provider)
            if info['canceled_by_user']:
                return
            if not status or status.startswith('STATUS_WAIT'):
                time.sleep(5)
                continue
            if status == 'STATUS_CANCEL':
                if not info['codes']:
                    ok = marcar_cancelado_e_devolver(info['user_id'], aid)
                    if info.get("is_api"):
                        logger.info(f"[API] STATUS_CANCEL devolveu saldo: AID {aid}")
                        return
                    if ok:
                        bot.send_message(chat_id, f"❌ Cancelado pelo provider. R${info['price']:.2f} devolvido.")
                return

            payload = status.split(':', 1)[1] if ':' in status else status
            display = payload

            if display not in info['codes']:
                info['codes'].append(display)
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
                kb.row(telebot.types.InlineKeyboardButton('📲 Receber outro SMS', callback_data=f'retry_{aid}'))
                kb.row(telebot.types.InlineKeyboardButton('🛒 Comprar mesmo serviço', callback_data=f'comprar_{service_key}'))
                kb.row(
                    telebot.types.InlineKeyboardButton('📲 Comprar serviços', callback_data='menu_comprar'),
                    telebot.types.InlineKeyboardButton('📜 Menu', callback_data='menu')
                )
                try:
                    if msg_id:
                        bot.edit_message_text(text, chat_id, msg_id, parse_mode='Markdown', reply_markup=kb)
                    else:
                        m = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=kb)
                        info['sms_message_id'] = m.message_id
                except telebot.apihelper.ApiTelegramException as e:
                    if "message is not modified" not in str(e):
                        raise
                except Exception:
                    pass
            time.sleep(5)
    threading.Thread(target=check_sms, daemon=True).start()

@bot.callback_query_handler(lambda c: c.data.startswith('retry_'))
def retry_sms(c):
    aid = c.data.split('_', 1)[1]
    info = status_map.get(aid) or {}
    provider = info.get('provider', 'smsbower')
    if provider == 'smsbower':
        try:
            requests.get(
                SMSBOWER_URL,
                params={'api_key':API_KEY_SMSBOWER,'action':'setStatus','status':'3','id':aid},
                timeout=10
            )
        except:
            pass
        bot.answer_callback_query(c.id, '🔄 Novo SMS solicitado.', show_alert=True)
    else:
        set_status_sms24h(aid, 3)
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
    provider = info.get('provider', 'smsbower')
    cancelar_numero(aid, provider)
    ok = marcar_cancelado_e_devolver(info['user_id'], aid)
    if ok:
        try: bot.delete_message(info['chat_id'], info['message_id'])
        except: pass
        bot.answer_callback_query(c.id, '✅ Cancelado e saldo devolvido.', show_alert=True)
    else:
        bot.answer_callback_query(c.id, '❌ Já cancelado anteriormente.', show_alert=True)

# =========================================================
# ================== PAINEL ADMIN (ATUALIZADO) ============
# =========================================================
@app.route('/admin', methods=['GET', 'POST'])
def painel_admin():
    token = request.args.get('token', '')
    if token != PAINEL_TOKEN:
        return "Acesso negado.", 401

    global SCANNER_ENABLED
    global SERVICE_PRICES, SERVICE_EMOJIS
    global SMSBOWER_MAX_PRICE_CAP, S1_CAPS

    msg_feedback = ""
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'enviar_mensagem':
            texto = request.form.get('texto')
            if texto:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
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
                with conn.cursor() as cur:
                    if todos:
                        cur.execute("UPDATE usuarios SET saldo=saldo+%s", (val,))
                        conn.commit()
                        msg_feedback = f"Saldo de R$ {val:.2f} adicionado a TODOS os usuários."
                    elif uid:
                        cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (val, str(uid)))
                        conn.commit()
                        msg_feedback = f"Saldo de R$ {val:.2f} adicionado ao usuário {uid}."
            exportar_backup_json()

        elif action == 'scanner_onoff':
            op = request.form.get('op')
            if op == 'on':
                SCANNER_ENABLED = True
                msg_feedback = "Scanner China 2 LIGADO."
            elif op == 'off':
                SCANNER_ENABLED = False
                msg_feedback = "Scanner China 2 DESLIGADO."
            else:
                msg_feedback = "Opção inválida para o scanner."

        elif action == 'china2_manual':
            manual_code = (request.form.get('manual_code') or '').strip()
            if manual_code:
                set_china2_service_code(manual_code, reason="(manual via painel)")
                msg_feedback = f"China 2 definido manualmente para '{manual_code}'."
            else:
                msg_feedback = "Informe um código de serviço válido (ex.: ki, ev, ...)."

        elif action == 'update_prices':
            changed = []
            for key in SERVICE_PRICES.keys():
                field = f"price_{key}"
                if field in request.form:
                    val_str = (request.form.get(field) or "").strip()
                    if val_str:
                        try:
                            new_val = float(val_str)
                            SERVICE_PRICES[key] = new_val
                            changed.append(f"{SERVICE_NAMES.get(key, key)} → R${new_val:.2f}")
                        except:
                            pass
            if changed:
                save_prices_emojis_to_db()
                msg_feedback = "Preços atualizados:\n" + "\n".join(changed)
            else:
                msg_feedback = "Nenhum preço alterado."

        elif action == 'update_emojis':
            changed = []
            for key in SERVICE_EMOJIS.keys():
                field = f"emoji_{key}"
                if field in request.form:
                    val_str = (request.form.get(field) or "").strip()
                    if val_str:
                        SERVICE_EMOJIS[key] = val_str
                        changed.append(f"{SERVICE_NAMES.get(key, key)} → {val_str}")
            if changed:
                save_prices_emojis_to_db()
                msg_feedback = "Emojis atualizados:\n" + "\n".join(changed)
            else:
                msg_feedback = "Nenhum emoji alterado."

        elif action == 'update_smsg_cap':
            val_str = (request.form.get('smsg_cap') or '').strip()
            try:
                new_cap = float(val_str)
                if new_cap <= 0:
                    raise ValueError()
                SMSBOWER_MAX_PRICE_CAP = new_cap
                save_smsg_cap_to_db()
                msg_feedback = f"Limite máximo GLOBAL do SMSBower atualizado para US$ {SMSBOWER_MAX_PRICE_CAP:.4f}"
            except:
                msg_feedback = "Valor inválido para CAP global do SMSBower."

        # >>> NOVO: atualizar caps por serviço (Servidor 1)
        elif action == 'update_s1_caps':
            changed = []
            for key in DEFAULT_S1_CAPS.keys():
                field = f"cap_{key}"
                if field in request.form:
                    vs = (request.form.get(field) or "").strip()
                    if vs:
                        try:
                            val = float(vs)
                            if val <= 0:
                                continue
                            S1_CAPS[key] = val
                            changed.append(f"{SERVICE_NAMES.get(key, key)} → US$ {val:.4f}")
                        except:
                            pass
            if changed:
                save_s1_caps_to_db()
                msg_feedback = "Limites por serviço (Servidor 1) atualizados:\n" + "\n".join(changed)
            else:
                msg_feedback = "Nenhum limite por serviço alterado."

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM numeros_sms")
            total = cur.fetchone()['count']
            cur.execute("SELECT count(*) FROM numeros_sms WHERE cancelado=TRUE")
            cancelados = cur.fetchone()['count']
            cur.execute("SELECT count(*) FROM numeros_sms WHERE sms_recebido=TRUE")
            recebidos = cur.fetchone()['count']

    with SERVICE_CODE_LOCK:
        china2_code = GLOBAL_SERVICE_MAP.get('china2')

    items_sorted = sorted(SERVICE_PRICES.items(), key=lambda kv: SERVICE_NAMES.get(kv[0], kv[0]).lower())

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
        <h3>Scanner China 2</h3>
        <p>Status: <b>{{ 'Ligado' if scanner_enabled else 'Desligado' }}</b></p>
        <p>Serviço China 2 atual: <code>{{ china2_code }}</code></p>
        <form method="post" style="display:inline-block; margin-right: 10px;">
            <input type="hidden" name="action" value="scanner_onoff">
            <input type="hidden" name="op" value="{{ 'off' if scanner_enabled else 'on' }}">
            <button type="submit">{{ 'Desligar' if scanner_enabled else 'Ligar' }} scanner</button>
        </form>
        <form method="post" style="display:inline-block;">
            <input type="hidden" name="action" value="china2_manual">
            Definir manualmente código: 
            <input name="manual_code" type="text" placeholder="ex.: ki, ev, ..." required>
            <button type="submit">Aplicar</button>
        </form>

        <hr>
        <h3>CAP Global do SMSBower (servidor "any")</h3>
        <form method="post" style="margin-bottom: 16px;">
            <input type="hidden" name="action" value="update_smsg_cap">
            Máximo em dólar para compras no SMSBower: 
            <input name="smsg_cap" type="number" step="0.0001" value="{{ '%.4f'|format(smsg_cap) }}" required>
            <button type="submit">Salvar CAP Global</button>
        </form>

        <hr>
        <h3>Limites (USD) por serviço - Servidor 1 (SMSBower)</h3>
        <form method="post">
            <input type="hidden" name="action" value="update_s1_caps">
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Serviço</th><th>Limite USD (maxPrice)</th></tr>
                {% for key, label in s1_caps_labels %}
                <tr>
                    <td>{{ label }}</td>
                    <td><input name="cap_{{key}}" type="number" step="0.0001" value="{{ '%.4f'|format(s1_caps[key]) }}"></td>
                </tr>
                {% endfor %}
            </table>
            <button type="submit" style="margin-top:10px;">Salvar Limites</button>
        </form>

        <hr>
        <h3>Preços dos serviços (R$ no bot)</h3>
        <form method="post">
            <input type="hidden" name="action" value="update_prices">
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Serviço</th><th>Preço (R$)</th></tr>
                {% for key, val in items_sorted %}
                <tr>
                    <td>{{ service_names.get(key, key) }}</td>
                    <td>
                        <input name="price_{{key}}" type="number" step="0.01" value="{{ '%.2f'|format(val) }}">
                    </td>
                </tr>
                {% endfor %}
            </table>
            <button type="submit" style="margin-top:10px;">Salvar Preços</button>
        </form>

        <hr>
        <h3>Emojis dos botões de compra</h3>
        <form method="post">
            <input type="hidden" name="action" value="update_emojis">
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Serviço</th><th>Emoji</th></tr>
                {% for key, _ in items_sorted %}
                <tr>
                    <td>{{ service_names.get(key, key) }}</td>
                    <td>
                        <input name="emoji_{{key}}" type="text" value="{{ service_emojis.get(key, '') }}" maxlength="4" style="width:4em; text-align:center;">
                    </td>
                </tr>
                {% endfor %}
            </table>
            <button type="submit" style="margin-top:10px;">Salvar Emojis</button>
        </form>

        <hr>
        <b style="white-space: pre-wrap;">{{msg_feedback}}</b>
        <hr>
        <h3>Estatísticas</h3>
        <ul>
            <li>Total de números vendidos: {{total}}</li>
            <li>Números cancelados: {{cancelados}}</li>
            <li>Números que receberam SMS: {{recebidos}}</li>
        </ul>
    """, msg_feedback=msg_feedback, total=total, cancelados=cancelados, recebidos=recebidos,
       scanner_enabled=SCANNER_ENABLED, china2_code=china2_code,
       items_sorted=items_sorted, service_names=SERVICE_NAMES, service_emojis=SERVICE_EMOJIS,
       smsg_cap=SMSBOWER_MAX_PRICE_CAP,
       s1_caps=S1_CAPS,
       s1_caps_labels=[
           ('mercado', SERVICE_NAMES['mercado']),
           ('china', SERVICE_NAMES['china']),
           ('china2', SERVICE_NAMES['china2']),
           ('picpay', SERVICE_NAMES['picpay']),
           ('wa1', SERVICE_NAMES['wa1']),
           ('outros', SERVICE_NAMES['outros']),
           ('c6srv1', SERVICE_NAMES['c6srv1']),
           ('google', SERVICE_NAMES['google']),
       ])

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
            with get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM payments WHERE id=%s", (str(pid),))
                if cur.fetchone():
                    return '', 200

            resp = mp_client.payment().get(pid)['response']
            if resp.get('status') == 'approved':
                ext = resp.get('external_reference', '')
                if ':' in ext:
                    uid_str, amt_str = ext.split(':', 1)
                    uid = int(uid_str)
                    amt = float(amt_str)

                    with get_db_conn() as conn, conn.cursor() as cur:
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
                                with get_db_conn() as conn, conn.cursor() as cur:
                                    cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (bonus, str(refid)))
                                    conn.commit()
                                try:
                                    bot.send_message(int(refid), f"🎉 Você ganhou R$ {bonus:.2f} de bônus pois seu indicado recarregou saldo!")
                                except:
                                    pass
                                ref_text = f"\nIndicado por: {refid}\nBônus enviado: R$ {bonus:.2f}"

                        with get_db_conn() as conn, conn.cursor() as cur:
                            cur.execute("UPDATE usuarios SET saldo=saldo+%s WHERE id=%s", (amt, str(uid)))
                            conn.commit()
                        exportar_backup_json()
                        bot.send_message(uid, f"✅ Recarga de R$ {amt:.2f} confirmada! Seu novo saldo é R$ {current + amt:.2f}")

                        msg_dep = (
                            f"💰 Novo DEPÓSITO\n"
                            f"User: {uid}\n"
                            f"Valor: R$ {amt:.2f}\n"
                            f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}{ref_text}"
                        )
                        enviar_mensagem_bot(admin_bot, ADMIN_CHAT_ID, msg_dep)
                        try:
                            enviar_mensagem_bot(admin_bot, HIST_CHANNEL, msg_dep)
                        except Exception as e:
                            logger.error(f"Falha ao publicar no canal de histórico: {e}")

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

def scanner_loop():
    while True:
        try:
            if not SCANNER_ENABLED:
                logger.info("[SCANNER] Pausado (desligado pelo admin).")
                time.sleep(SCANNER_INTERVAL_SEC)
                continue

            best = None  # (min_price, service_id, activate_org_code, title, count)
            for sid in range(1, 1320):  # 1..1319
                url = f"https://smsbower.org/activations/getPricesByService?serviceId={sid}&withPopular=true&rank=1"
                try:
                    r = requests.get(url, timeout=15)
                    r.raise_for_status()
                except Exception:
                    continue
                try:
                    payload = r.json()
                except Exception:
                    continue
                services = payload.get("services") or {}
                svc = services.get(str(sid))
                if not svc:
                    continue
                countries = (svc.get("countries") or {})
                br = countries.get(SCANNER_COUNTRY_ID)  # "14"
                if not br:
                    continue
                min_price = br.get("min_price")
                count = br.get("count", 0)
                if min_price is None:
                    continue
                try:
                    mp = float(min_price)
                except:
                    continue
                if count > SCANNER_MIN_COUNT and (SCANNER_MIN_PRICE <= mp <= SCANNER_MAX_PRICE):
                    with services_index_lock:
                        si = services_index.get(str(sid))
                    if not si:
                        title = f"serviceId {sid}"
                        aoc = None
                    else:
                        title = si.get("title") or f"serviceId {sid}"
                        aoc = si.get("activate_org_code")
                    if aoc:
                        if (best is None) or (mp < best[0]):
                            best = (mp, sid, aoc, title, count)

            if best:
                mp, sid, aoc, title, count = best
                set_china2_service_code(aoc, reason=f"(id:{sid}, {title}, count:{count}, min_price:{mp})", price=mp)
                logger.info(f"[SCANNER] Melhor China2: serviceId={sid} title={title} aoc={aoc} count={count} min_price={mp}")
            else:
                logger.info("[SCANNER] Nenhum candidato elegível encontrado para China2. Mantendo atual.")
        except Exception as e:
            logger.error(f"[SCANNER] erro geral: {e}")

        time.sleep(SCANNER_INTERVAL_SEC)

# =========================================================
# ======================== MAIN ===========================
# =========================================================
if __name__ == '__main__':
    threading.Thread(target=scanner_loop, daemon=True).start()
    try:
        bot.remove_webhook()
        if SITE_URL:
            bot.set_webhook(f"{SITE_URL}/webhook/telegram")
    except Exception as e:
        logger.error(f"Erro set_webhook: {e}")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
