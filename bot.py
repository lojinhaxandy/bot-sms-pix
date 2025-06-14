import os
import time
import threading
import requests
import sqlite3
from flask import Flask, request, abort
import telebot

# --- CONFIGURAÇÕES ---
TOKEN = os.getenv("TELEGRAM_API_TOKEN")
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
SMSBOWER_API_KEY = os.getenv("SMSBOWER_API_KEY")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

VALOR_UNITARIO = 0.25
TEMPO_EXPIRACAO = 18 * 60  # 18 minutos

# --- BANCO DE DADOS SQLITE ---
DB_FILE = "botdata.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            chat_id INTEGER PRIMARY KEY,
            saldo REAL DEFAULT 0,
            numero_ativo TEXT,
            activation_id TEXT,
            tempo_inicio REAL
        )
    ''')
    conn.commit()
    conn.close()

def get_usuario(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT saldo, numero_ativo, activation_id, tempo_inicio FROM usuarios WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"saldo": row[0] or 0.0, "numero_ativo": row[1], "activation_id": row[2], "tempo_inicio": row[3]}
    else:
        return None

def criar_usuario(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO usuarios(chat_id, saldo) VALUES (?, 0)", (chat_id,))
    conn.commit()
    conn.close()

def atualizar_usuario(chat_id, **kwargs):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    campos = []
    valores = []
    for k, v in kwargs.items():
        campos.append(f"{k} = ?")
        valores.append(v)
    valores.append(chat_id)
    sql = f"UPDATE usuarios SET {', '.join(campos)} WHERE chat_id = ?"
    c.execute(sql, valores)
    conn.commit()
    conn.close()

def atualizar_saldo_db(chat_id, valor):
    user = get_usuario(chat_id)
    if user:
        novo_saldo = (user["saldo"] or 0) + valor
        if novo_saldo < 0:
            novo_saldo = 0
        atualizar_usuario(chat_id, saldo=novo_saldo)
        print(f"[DB] Saldo atualizado: {chat_id} -> R$ {novo_saldo:.2f}")
        return novo_saldo
    else:
        criar_usuario(chat_id)
        atualizar_usuario(chat_id, saldo=valor)
        return valor

# ---------------- Funções API ----------------

def criar_cobranca_mercadopago(valor, chat_id):
    url = "https://api.mercadopago.com/v1/payments"
    headers = {
        "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "transaction_amount": round(float(valor), 2),
        "description": f"Recarga bot SMS - Usuário {chat_id}",
        "payment_method_id": "pix",
        "payer": {
            "email": f"user{chat_id}@bot.com"
        }
    }
    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code in [200, 201]:
        res_json = resp.json()
        pix_link = res_json.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code")
        if pix_link:
            return pix_link
    print(f"Erro Mercado Pago criar cobrança: {resp.status_code} {resp.text}")
    return None

def obter_numero_sms(service):
    url = (f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}"
           f"&action=getNumber&service={service}&country=cn")
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            text = resp.text
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":")
                if len(parts) >= 3:
                    activation_id = parts[1]
                    phone_number = parts[2]
                    return activation_id, phone_number
            else:
                return None, f"Erro API SMSBOWER: {text}"
        else:
            return None, f"Erro HTTP SMSBOWER: {resp.status_code}"
    except Exception as e:
        return None, f"Exception SMSBOWER: {str(e)}"

def cancelar_numero_sms(activation_id):
    url = (f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}"
           f"&action=cancelActivation&id={activation_id}")
    try:
        requests.get(url)
    except:
        pass

def limpar_ativos_expirados():
    while True:
        agora = time.time()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id, activation_id, tempo_inicio FROM usuarios WHERE tempo_inicio IS NOT NULL")
        rows = c.fetchall()
        for chat_id, activation_id, tempo_inicio in rows:
            if tempo_inicio and (agora - tempo_inicio > TEMPO_EXPIRACAO):
                cancelar_numero_sms(activation_id)
                atualizar_usuario(chat_id, numero_ativo=None, activation_id=None, tempo_inicio=None)
                try:
                    bot.send_message(chat_id, "⏰ Seu número SMS expirou e foi cancelado.")
                except:
                    pass
        conn.close()
        time.sleep(60)

# ------------- Telegram Handlers ---------------

SERVICOS = {
    "smschina": "Verificar Telefone Na China",
    "mercadopago": "Mercado Pago",
    "picpay": "PicPay",
    "nubank": "Nubank",
    "astropay": "Astropay",
}

@bot.message_handler(commands=['start', 'help'])
def enviar_menu(message):
    chat_id = message.chat.id
    criar_usuario(chat_id)
    markup = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = telebot.types.KeyboardButton("Comprar Número SMS")
    btn2 = telebot.types.KeyboardButton("Recarregar Saldo")
    btn3 = telebot.types.KeyboardButton("Ver Saldo")
    markup.add(btn1, btn2, btn3)
    bot.send_message(chat_id, "Olá! Escolha uma opção:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "Comprar Número SMS")
def escolher_servico(message):
    chat_id = message.chat.id
    markup = telebot.types.InlineKeyboardMarkup()
    for key, nome in SERVICOS.items():
        markup.add(telebot.types.InlineKeyboardButton(nome, callback_data=f"servico_{key}"))
    bot.send_message(chat_id, "Escolha o serviço para comprar número SMS:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("servico_"))
def processar_servico(call):
    chat_id = call.message.chat.id
    servico = call.data.split("_", 1)[1]
    user = get_usuario(chat_id)
    if not user:
        criar_usuario(chat_id)
        user = get_usuario(chat_id)

    if user["saldo"] < VALOR_UNITARIO:
        bot.send_message(chat_id, "Saldo insuficiente para comprar um número. Por favor, recarregue.")
        return

    if user["numero_ativo"]:
        bot.send_message(chat_id, "Você já tem um número ativo. Aguarde ou cancele antes de comprar outro.")
        return

    res = obter_numero_sms(servico)
    if res[0]:
        activation_id, phone_number = res
        atualizar_usuario(chat_id, numero_ativo=phone_number, activation_id=activation_id, tempo_inicio=time.time())
        atualizar_saldo_db(chat_id, -VALOR_UNITARIO)
        bot.send_message(chat_id,
                         f"✅ Número comprado: {phone_number}\n"
                         f"Use o botão abaixo para receber SMS ou cancelar a ativação.",
                         reply_markup=telebot.types.InlineKeyboardMarkup(row_width=2).add(
                             telebot.types.InlineKeyboardButton("Ver Novo SMS", callback_data="sms_novo"),
                             telebot.types.InlineKeyboardButton("Cancelar Número", callback_data="sms_cancelar")
                         ))
    else:
        bot.send_message(chat_id, f"Erro ao comprar número: {res[1]}")

@bot.callback_query_handler(func=lambda c: c.data in ["sms_novo", "sms_cancelar"])
def processar_sms_actions(call):
    chat_id = call.message.chat.id
    user = get_usuario(chat_id)
    if not user or not user.get("activation_id"):
        bot.send_message(chat_id, "Você não tem número ativo no momento.")
        return

    if call.data == "sms_cancelar":
        cancelar_numero_sms(user["activation_id"])
        atualizar_usuario(chat_id, numero_ativo=None, activation_id=None, tempo_inicio=None)
        bot.send_message(chat_id, "Número cancelado com sucesso.")
    elif call.data == "sms_novo":
        url = (f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}"
               f"&action=getStatus&id={user['activation_id']}")
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                texto = resp.text
                if texto.startswith("STATUS_OK"):
                    sms = texto.split("STATUS_OK:")[-1].strip()
                    if sms:
                        bot.send_message(chat_id, f"📩 Novo SMS recebido:\n{sms}")
                    else:
                        bot.send_message(chat_id, "Ainda não chegou nenhum SMS.")
                else:
                    bot.send_message(chat_id, f"Status SMS: {texto}")
            else:
                bot.send_message(chat_id, f"Erro ao consultar SMS: HTTP {resp.status_code}")
        except Exception as e:
            bot.send_message(chat_id, f"Erro ao consultar SMS: {str(e)}")

@bot.message_handler(func=lambda m: m.text == "Recarregar Saldo")
def pedir_valor_recarregar(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "💸 Envie o valor que deseja recarregar (ex: 2.50)")

@bot.message_handler(func=lambda m: m.text and m.text.replace('.', '', 1).isdigit())
def recarregar_saldo(message):
    chat_id = message.chat.id
    try:
        valor = float(message.text)
        if valor < 0.25:
            bot.send_message(chat_id, "O valor mínimo para recarga é R$ 0,25.")
            return
    except:
        bot.send_message(chat_id, "Por favor, envie um valor válido para recarga.")
        return

    link_pix = criar_cobranca_mercadopago(valor, chat_id)
    if link_pix:
        bot.send_message(chat_id,
                         f"✅ Para recarregar R$ {valor:.2f}, faça o pagamento via PIX neste link:\n{link_pix}\n"
                         "Após o pagamento, seu saldo será atualizado automaticamente (em até alguns minutos).")
    else:
        bot.send_message(chat_id, "❌ Erro ao gerar cobrança. Tente novamente mais tarde.")

@bot.message_handler(func=lambda m: m.text == "Ver Saldo")
def ver_saldo(message):
    chat_id = message.chat.id
    user = get_usuario(chat_id)
    saldo = user["saldo"] if user else 0.0
    bot.send_message(chat_id, f"Seu saldo atual é: R$ {saldo:.2f}")

# -------------- Flask Webhook ---------------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    else:
        abort(403)

# Webhook Mercado Pago com validação e atualização saldo
@app.route("/mercadopago_webhook", methods=["POST"])
def mercadopago_webhook():
    data = request.json
    try:
        # Validação simples da notificação Mercado Pago
        topic = request.args.get("topic") or request.args.get("type")
        if topic in ("payment", "payments"):
            payment_id = data.get("id")
            if payment_id is None:
                return "Missing payment id", 400

            url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
            headers = {"Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}"}
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"Erro ao consultar pagamento MP: {resp.status_code}")
                return "Erro consulta pagamento", 500

            payment = resp.json()
            status = payment.get("status")
            valor_pago = float(payment.get("transaction_amount", 0))
            payer_email = payment.get("payer", {}).get("email", "")

            if status == "approved" and valor_pago > 0 and payer_email.startswith("user") and payer_email.endswith("@bot.com"):
                chat_id_str = payer_email[4:-8]
                if chat_id_str.isdigit():
                    chat_id = int(chat_id_str)
                    novo_saldo = atualizar_saldo_db(chat_id, valor_pago)
                    print(f"Saldo atualizado via webhook MP: {chat_id} + R$ {valor_pago:.2f} = R$ {novo_saldo:.2f}")
                    try:
                        bot.send_message(chat_id, f"✅ Seu pagamento de R$ {valor_pago:.2f} foi confirmado! Saldo atualizado.")
                    except Exception as e:
                        print(f"Erro ao avisar usuário: {str(e)}")
                    return "", 200
            return "", 200
        else:
            return "", 400
    except Exception as e:
        print(f"Erro webhook MP: {str(e)}")
        return "", 500

# ------------ Setup inicial ------------------

def setup_webhook():
    url_webhook = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    res = bot.set_webhook(url_webhook)
    if res:
        print(f"Webhook Telegram configurado: {url_webhook}")
    else:
        print("Erro ao configurar webhook Telegram.")

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=limpar_ativos_expirados, daemon=True)
    t.start()

    setup_webhook()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
