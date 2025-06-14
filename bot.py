import os
import time
import threading
import requests
from flask import Flask, request, abort
import telebot

# --- Configurações e variáveis ambiente ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SMSBOWER_API_TOKEN = os.getenv("SMSBOWER_API_TOKEN")
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise Exception("Variável de ambiente TELEGRAM_BOT_TOKEN não definida!")

if not WEBHOOK_URL:
    raise Exception("Variável de ambiente WEBHOOK_URL não definida!")

if not SMSBOWER_API_TOKEN:
    raise Exception("Variável de ambiente SMSBOWER_API_TOKEN não definida!")

if not MERCADOPAGO_ACCESS_TOKEN:
    raise Exception("Variável de ambiente MERCADOPAGO_ACCESS_TOKEN não definida!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)

# Banco de dados simples em memória para demo
USERS_SALDO = {}         # chat_id : saldo em float
ACTIVATIONS = {}         # chat_id : {'activationId': str, 'expires_at': timestamp, 'number': str}

# Timeout para expiração dos números (18 minutos = 1080 segundos)
NUMBER_TIMEOUT = 1080

# --- Funções para Mercado Pago ---

def criar_cobranca(chat_id, valor):
    url = "https://api.mercadopago.com/v1/payments"
    headers = {
        "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": f"{chat_id}-{int(time.time())}"
    }
    body = {
        "transaction_amount": valor,
        "description": "Recarga SMS China Bot",
        "payment_method_id": "pix",
        "payer": {
            "email": f"user{chat_id}@example.com"
        }
    }
    resp = requests.post(url, json=body, headers=headers)
    if resp.status_code == 201:
        return resp.json()
    else:
        print("Erro criar cobrança MP:", resp.text)
        return None

# --- Funções para SMSBower ---

def solicitar_numero_sms(chat_id):
    # Solicita um número novo na API smsbower
    url = "https://smsbower.online/api/getNumber"
    params = {"apiToken": SMSBOWER_API_TOKEN, "service": "smschina"}
    resp = requests.get(url, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("status") == "success":
            return data.get("number"), data.get("activationId")
    return None, None

def consultar_sms(activationId):
    # Consulta SMS recebido pela API
    url = f"https://smsbower.online/api/getSMS?apiToken={SMSBOWER_API_TOKEN}&activationId={activationId}"
    resp = requests.get(url)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("status") == "success":
            return data.get("sms")
    return None

def cancelar_ativacao(activationId):
    url = f"https://smsbower.online/api/cancel?apiToken={SMSBOWER_API_TOKEN}&activationId={activationId}"
    requests.get(url)

# --- Comandos do bot ---

@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "👋 Bem-vindo ao SMSChina Bot!\nEnvie o valor que deseja recarregar (ex: 2.50) para comprar saldo.")

@bot.message_handler(commands=["saldo"])
def cmd_saldo(message):
    chat_id = message.chat.id
    saldo = USERS_SALDO.get(chat_id, 0.0)
    bot.send_message(chat_id, f"💰 Seu saldo atual é: R$ {saldo:.2f}")

@bot.message_handler(commands=["cancelar"])
def cmd_cancelar(message):
    chat_id = message.chat.id
    if chat_id in ACTIVATIONS:
        ativ = ACTIVATIONS.pop(chat_id)
        cancelar_ativacao(ativ['activationId'])
        bot.send_message(chat_id, "❌ Ativação cancelada e número liberado.")
    else:
        bot.send_message(chat_id, "⚠️ Você não tem ativações em andamento.")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    # Se usuário está com ativação pendente
    if chat_id in ACTIVATIONS:
        ativ = ACTIVATIONS[chat_id]
        if time.time() > ativ['expires_at']:
            # Expirou
            cancelar_ativacao(ativ['activationId'])
            ACTIVATIONS.pop(chat_id)
            bot.send_message(chat_id, "⏳ Tempo expirado para ativação. Por favor, solicite um novo número.")
            return

        # Aqui pode tratar resposta com código SMS se desejar (exemplo simplificado)
        bot.send_message(chat_id, "⏳ Estamos aguardando o SMS, aguarde...")

    else:
        # Se for número decimal para recarga
        try:
            valor = float(text.replace(",", "."))
            if valor <= 0:
                raise ValueError
            # Cria cobrança no Mercado Pago
            cobranca = criar_cobranca(chat_id, valor)
            if cobranca:
                qr_code = cobranca.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code_base64")
                link_pagamento = cobranca.get("transaction_details", {}).get("external_resource_url")
                if qr_code:
                    bot.send_photo(chat_id, photo=f"data:image/png;base64,{qr_code}", caption=f"💸 Pague via Pix para recarregar R$ {valor:.2f}\nApós o pagamento, seu saldo será atualizado automaticamente.")
                elif link_pagamento:
                    bot.send_message(chat_id, f"💸 Pague aqui para recarregar R$ {valor:.2f}:\n{link_pagamento}")
                else:
                    bot.send_message(chat_id, "❌ Não foi possível gerar o QR Code de pagamento.")
            else:
                bot.send_message(chat_id, "❌ Erro ao gerar cobrança. Tente novamente.")
        except ValueError:
            # Se não for número, pode ser pedido para novo SMS
            if text.lower() == "novo sms":
                # Solicitar número
                number, activationId = solicitar_numero_sms(chat_id)
                if number and activationId:
                    ACTIVATIONS[chat_id] = {
                        "activationId": activationId,
                        "number": number,
                        "expires_at": time.time() + NUMBER_TIMEOUT
                    }
                    bot.send_message(chat_id, f"📲 Número recebido: {number}\nVocê tem 18 minutos para receber o SMS.\nEnvie /cancelar para cancelar a ativação.")
                else:
                    bot.send_message(chat_id, "❌ Erro ao solicitar número. Tente novamente mais tarde.")
            else:
                bot.send_message(chat_id, "⚠️ Comando ou texto inválido. Envie um valor para recarga ou 'novo sms' para receber número.")

# --- Webhook Flask ---

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    else:
        abort(403)

# Setup webhook no início (apenas uma vez)
def setup_webhook():
    res = bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}")
    print("Webhook set result:", res)

# --- Thread para verificar atualizações de SMS e atualizar saldo ---

def sms_check_thread():
    while True:
        time.sleep(10)
        to_remove = []
        for chat_id, ativ in ACTIVATIONS.items():
            sms = consultar_sms(ativ["activationId"])
            if sms:
                # Extrair código do SMS, ex: "Seu código é 123456"
                codigo = None
                import re
                m = re.search(r"\b(\d{4,8})\b", sms)
                if m:
                    codigo = m.group(1)
                if codigo:
                    bot.send_message(chat_id, f"✅ Código SMS recebido: {codigo}")
                    # Aqui você pode salvar esse código para o usuário ou continuar o fluxo
                    # Exemplo simples: remover ativação
                    to_remove.append(chat_id)
            # Se tempo expirou, cancela ativação
            if time.time() > ativ["expires_at"]:
                cancelar_ativacao(ativ["activationId"])
                to_remove.append(chat_id)
                bot.send_message(chat_id, "⏳ Tempo da ativação expirou, número liberado.")

        for chat_id in to_remove:
            ACTIVATIONS.pop(chat_id, None)

# --- Inicia o servidor Flask e a thread do SMS check ---

if __name__ == "__main__":
    setup_webhook()
    threading.Thread(target=sms_check_thread, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
