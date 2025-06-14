import os
import time
import threading
import requests
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# === CONFIGURAÇÕES ===

API_TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
WEBHOOK_URL_BASE = "https://bot-sms-pix.onrender.com"
WEBHOOK_URL_PATH = f"/{API_TOKEN}/"

SMSBOWER_API_KEY = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"
MERCADOPAGO_ACCESS_TOKEN = "APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094"

# Serviços disponíveis, preço por unidade em reais
SERVICOS = {
    "china": {"nome": "Verificar Telefone Na China", "codigo": "picpaychina", "preco": 0.25},
    "mercadopago": {"nome": "Mercado Pago", "codigo": "mercadopago", "preco": 0.25},
    "picpay": {"nome": "PicPay", "codigo": "picpay", "preco": 0.25},
    "nubank": {"nome": "Nubank", "codigo": "nubank", "preco": 0.25},
    "astropay": {"nome": "AstroPay", "codigo": "astropay", "preco": 0.25},
}

# Timeout para expiração do número em segundos (18 minutos)
EXPIRACAO_NUMERO = 18 * 60

app = Flask(__name__)
bot = telebot.TeleBot(API_TOKEN)

# Base de dados simples em memória (troque por banco real depois)
usuarios = {}  
# Exemplo:
# usuarios = {
#   chat_id: {
#       "saldo": float,
#       "ativacao": {
#           "activation_id": str,
#           "numero": str,
#           "servico": str,
#           "inicio": timestamp
#       }
#   }
# }

# ==================== FUNÇÕES AUXILIARES ====================

def criar_cobranca(valor, chat_id):
    """Cria uma cobrança no Mercado Pago e retorna o link de pagamento"""
    headers = {
        "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(time.time())  # evita duplicidade
    }
    data = {
        "transaction_amount": float(valor),
        "description": f"Recarga bot SMS - Usuário {chat_id}",
        "payment_method_id": "pix",
        "payer": {
            "email": f"user{chat_id}@bot.com"
        }
    }
    response = requests.post("https://api.mercadopago.com/v1/payments", json=data, headers=headers)
    if response.status_code == 201 or response.status_code == 200:
        resp_json = response.json()
        # Link QRCode pix
        pix_link = resp_json.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code")
        return pix_link
    else:
        print("Erro Mercado Pago:", response.text)
        return None

def comprar_numero(servico_codigo):
    """Compra número no SMSBower para o serviço"""
    url = (
        f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}"
        f"&action=getNumber&service={servico_codigo}&country=br&maxPrice=10"
    )
    r = requests.get(url)
    if r.ok and r.text.startswith("ACCESS_NUMBER"):
        parts = r.text.split(":")
        activation_id = parts[1]
        numero = parts[2]
        return activation_id, numero
    else:
        print("Erro comprar número:", r.text)
        return None, None

def consultar_sms(activation_id):
    """Consulta SMS recebido para o activation_id"""
    url = f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getStatus&id={activation_id}"
    r = requests.get(url)
    if r.ok:
        return r.text
    else:
        return None

def cancelar_numero(activation_id):
    """Cancela a ativação no SMSBower"""
    url = f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=cancelActivation&id={activation_id}"
    r = requests.get(url)
    return r.ok

def descontar_saldo(chat_id, valor):
    if chat_id not in usuarios:
        usuarios[chat_id] = {"saldo": 0.0, "ativacao": None}
    if usuarios[chat_id]["saldo"] >= valor:
        usuarios[chat_id]["saldo"] -= valor
        return True
    return False

def saldo_usuario(chat_id):
    return usuarios.get(chat_id, {}).get("saldo", 0.0)

# ==================== HANDLERS ====================

@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    if chat_id not in usuarios:
        usuarios[chat_id] = {"saldo": 0.0, "ativacao": None}
    bot.send_message(chat_id, "Bem-vindo! Seu saldo atual é R$ %.2f\nEnvie o valor para recarregar (ex: 2.50)" % saldo_usuario(chat_id))

@bot.message_handler(commands=["saldo"])
def mostra_saldo(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, f"Seu saldo atual é R$ {saldo_usuario(chat_id):.2f}")

@bot.message_handler(func=lambda m: m.text and m.text.replace('.', '', 1).isdigit())
def recarregar(message):
    chat_id = message.chat.id
    try:
        valor = float(message.text)
        if valor < 0.25:
            bot.send_message(chat_id, "O valor mínimo para recarga é R$ 0,25")
            return
    except:
        bot.send_message(chat_id, "Valor inválido.")
        return

    link = criar_cobranca(valor, chat_id)
    if link:
        bot.send_message(chat_id, f"Para recarregar R$ {valor:.2f}, faça o pagamento via PIX neste link:\n{link}\nApós o pagamento, seu saldo será atualizado em alguns minutos.")
    else:
        bot.send_message(chat_id, "❌ Erro ao gerar cobrança. Tente novamente.")

# Botão para escolher serviço
@bot.message_handler(commands=["servicos"])
def mostrar_servicos(message):
    chat_id = message.chat.id
    markup = InlineKeyboardMarkup(row_width=2)
    for key, svc in SERVICOS.items():
        markup.add(InlineKeyboardButton(svc["nome"], callback_data=f"servico_{key}"))
    bot.send_message(chat_id, "Escolha o serviço desejado:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("servico_"))
def servico_selecionado(call):
    chat_id = call.message.chat.id
    servico_key = call.data.split("_")[1]
    servico = SERVICOS.get(servico_key)

    if not servico:
        bot.answer_callback_query(call.id, "Serviço inválido.")
        return

    preco = servico["preco"]
    saldo = saldo_usuario(chat_id)

    if saldo < preco:
        bot.answer_callback_query(call.id, "Saldo insuficiente. Recarregue antes.")
        return

    # Compra número SMS
    activation_id, numero = comprar_numero(servico["codigo"])
    if not activation_id:
        bot.answer_callback_query(call.id, "❌ Erro ao comprar número. Tente novamente.")
        return

    # Desconta saldo
    if not descontar_saldo(chat_id, preco):
        bot.answer_callback_query(call.id, "Saldo insuficiente para compra.")
        return

    # Armazena ativação
    usuarios[chat_id]["ativacao"] = {
        "activation_id": activation_id,
        "numero": numero,
        "servico": servico_key,
        "inicio": time.time()
    }

    # Monta teclado com opções para o número comprado
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Novo SMS", callback_data="novo_sms"),
        InlineKeyboardButton("Cancelar Número", callback_data="cancelar_numero")
    )

    bot.edit_message_text(
        f"Número comprado: {numero}\nServiço: {servico['nome']}\nSaldo descontado: R$ {preco:.2f}",
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

def checar_expiracao(chat_id):
    ativ = usuarios.get(chat_id, {}).get("ativacao")
    if ativ and time.time() - ativ["inicio"] > EXPIRACAO_NUMERO:
        cancelar_numero(ativ["activation_id"])
        usuarios[chat_id]["ativacao"] = None
        bot.send_message(chat_id, "⏰ O número expirou e foi cancelado automaticamente.")

@bot.callback_query_handler(func=lambda call: call.data == "cancelar_numero")
def cancelar_numero_cb(call):
    chat_id = call.message.chat.id
    ativ = usuarios.get(chat_id, {}).get("ativacao")
    if not ativ:
        bot.answer_callback_query(call.id, "Você não tem número ativo.")
        return
    cancelar_numero(ativ["activation_id"])
    usuarios[chat_id]["ativacao"] = None
    bot.edit_message_text("Número cancelado com sucesso.", chat_id=chat_id, message_id=call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "novo_sms")
def novo_sms_cb(call):
    chat_id = call.message.chat.id
    ativ = usuarios.get(chat_id, {}).get("ativacao")
    if not ativ:
        bot.answer_callback_query(call.id, "Você não tem número ativo.")
        return

    checar_expiracao(chat_id)

    ativ = usuarios.get(chat_id, {}).get("ativacao")
    if not ativ:
        bot.answer_callback_query(call.id, "Seu número expirou. Compre outro serviço.")
        return

    sms = consultar_sms(ativ["activation_id"])
    if sms == "STATUS_WAIT_CODE":
        bot.answer_callback_query(call.id, "⌛ Aguardando SMS...")
    elif sms and sms != "STATUS_WAIT_CODE":
        bot.edit_message_text(f"SMS recebido:\n{sms}", chat_id=chat_id, message_id=call.message.message_id)
        # Considera finalizada ativação e remove
        usuarios[chat_id]["ativacao"] = None
    else:
        bot.answer_callback_query(call.id, "Erro ao consultar SMS. Tente novamente.")

# ==================== FLASK E WEBHOOK ====================

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "", 200

if __name__ == "__main__":
    webhook_url = WEBHOOK_URL_BASE + WEBHOOK_URL_PATH
    if bot.set_webhook(url=webhook_url):
        print(f"Webhook configurado em: {webhook_url}")
    else:
        print("Falha ao configurar webhook")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
