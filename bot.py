import telebot
import os
import requests
from flask import Flask, request

API_TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
MERCADO_PAGO_TOKEN = "APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094"

bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

usuarios = {}
cobrancas = {}

# --- Comando /start
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id not in usuarios:
        usuarios[user_id] = 0.0
    bot.send_message(user_id, f"👋 Olá! Seu saldo é R$ {usuarios[user_id]:.2f}")

# --- Comando /saldo
@bot.message_handler(commands=['saldo'])
def saldo(message):
    user_id = message.from_user.id
    bot.send_message(user_id, f"💰 Seu saldo é: R$ {usuarios.get(user_id, 0.0):.2f}")

# --- Comando /recarregar
@bot.message_handler(commands=['recarregar'])
def pedir_valor(message):
    bot.send_message(message.chat.id, "💸 Envie o valor que deseja recarregar (ex: 2.50)")
    bot.register_next_step_handler(message, gerar_pix)

# --- Gerar cobrança Pix via Mercado Pago
def gerar_pix(message):
    try:
        valor = float(message.text.replace(",", "."))
        user_id = message.from_user.id

        url = "https://api.mercadopago.com/v1/payments"
        headers = {
            "Authorization": f"Bearer {MERCADO_PAGO_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "transaction_amount": valor,
            "description": f"Recarga Telegram user {user_id}",
            "payment_method_id": "pix",
            "payer": {
                "email": f"user{user_id}@email.com"
            }
        }

        r = requests.post(url, json=payload, headers=headers).json()
        if 'point_of_interaction' in r:
            pix_code = r['point_of_interaction']['transaction_data']['qr_code']
            pagamento_id = r['id']
            cobrancas[pagamento_id] = user_id
            bot.send_message(user_id, f"✅ Pague com Pix:\n\n🔢 Copia e Cola:\n`{pix_code}`", parse_mode="Markdown")
            bot.send_message(user_id, "📌 Após o pagamento, o saldo será atualizado automaticamente.")
        else:
            bot.send_message(user_id, "❌ Erro ao gerar cobrança. Tente novamente.")
    except:
        bot.send_message(message.chat.id, "❗ Valor inválido. Tente novamente com um número (ex: 5.00).")

# --- Webhook Mercado Pago
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if data and "type" in data and data["type"] == "payment":
        payment_id = int(data["data"]["id"])
        verificar_pagamento(payment_id)
    return "OK", 200

# --- Verifica pagamento
def verificar_pagamento(payment_id):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {
        "Authorization": f"Bearer {MERCADO_PAGO_TOKEN}"
    }
    r = requests.get(url, headers=headers).json()

    if r.get("status") == "approved":
        user_id = cobrancas.get(payment_id)
        valor = r["transaction_amount"]
        if user_id:
            usuarios[user_id] = usuarios.get(user_id, 0.0) + valor
            bot.send_message(user_id, f"✅ Pagamento de R$ {valor:.2f} aprovado! Seu novo saldo é R$ {usuarios[user_id]:.2f}")
            del cobrancas[payment_id]

# --- Webhook do Telegram
@app.route(f"/{API_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(update)
    bot.process_new_updates([update])
    return "OK", 200

# --- Inicializa Flask no Render
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url='https://bot-sms-pix.onrender.com/' + API_TOKEN)  # ⬅️ ALTERE AQUI com sua URL do Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
