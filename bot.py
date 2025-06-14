import telebot
import os
import requests
from flask import Flask, request

API_TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
MERCADO_PAGO_TOKEN = "APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094"
SMSBOWER_API_KEY = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"

bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

usuarios = {}      # user_id: saldo
cobrancas = {}     # pagamento_id: user_id

SERVICOS = {
    "1": {"nome": "Verificar Telefone Na China", "api_service": "picpay"},
    "2": {"nome": "Mercado Pago", "api_service": "mercadopago"},
    "3": {"nome": "PicPay", "api_service": "picpay"},
    "4": {"nome": "Nubank", "api_service": "nubank"},
    "5": {"nome": "Astropay", "api_service": "astropay"}
}

VALOR_POR_NUMERO = 0.25

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id not in usuarios:
        usuarios[user_id] = 0.0
    bot.send_message(user_id, f"👋 Olá! Seu saldo é R$ {usuarios[user_id]:.2f}")

@bot.message_handler(commands=['saldo'])
def saldo(message):
    user_id = message.from_user.id
    bot.send_message(user_id, f"💰 Seu saldo é: R$ {usuarios.get(user_id, 0.0):.2f}")

@bot.message_handler(commands=['recarregar'])
def pedir_valor(message):
    bot.send_message(message.chat.id, "💸 Envie o valor que deseja recarregar (ex: 2.50)")
    bot.register_next_step_handler(message, gerar_pix)

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
            erro_msg = r.get('message') or str(r)
            bot.send_message(user_id, f"❌ Erro ao gerar cobrança: {erro_msg}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❗ Valor inválido ou erro interno: {str(e)}")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if data and "type" in data and data["type"] == "payment":
        payment_id = int(data["data"]["id"])
        verificar_pagamento(payment_id)
    return "OK", 200

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

@bot.message_handler(commands=['comprar'])
def comprar_numero(message):
    user_id = message.from_user.id
    if usuarios.get(user_id, 0) < VALOR_POR_NUMERO:
        bot.send_message(user_id, f"❌ Saldo insuficiente para comprar um número. Seu saldo atual é R$ {usuarios.get(user_id,0):.2f}")
        return

    texto = "Escolha o serviço para comprar o número:\n"
    for k, v in SERVICOS.items():
        texto += f"{k}. {v['nome']}\n"
    bot.send_message(user_id, texto)
    bot.register_next_step_handler(message, processar_compra)

def processar_compra(message):
    user_id = message.from_user.id
    escolha = message.text.strip()

    if escolha not in SERVICOS:
        bot.send_message(user_id, "❌ Opção inválida. Use /comprar para tentar novamente.")
        return

    servico = SERVICOS[escolha]["api_service"]

    if usuarios.get(user_id, 0) < VALOR_POR_NUMERO:
        bot.send_message(user_id, f"❌ Saldo insuficiente para comprar o número. Saldo: R$ {usuarios.get(user_id,0):.2f}")
        return

    bot.send_message(user_id, f"⏳ Comprando número para o serviço {SERVICOS[escolha]['nome']}...")

    url = (
        f"https://smsbower.online/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}"
        f"&action=getNumber&service={servico}&country=BR&maxPrice={VALOR_POR_NUMERO}"
    )

    try:
        r = requests.get(url)
        texto = r.text.strip()
        if texto.startswith("ACCESS_NUMBER"):
            parts = texto.split(":")
            activation_id = parts[1]
            phone_number = parts[2]
            usuarios[user_id] -= VALOR_POR_NUMERO
            bot.send_message(user_id, f"✅ Número comprado:\nNúmero: {phone_number}\nID Ativação: {activation_id}\nSaldo restante: R$ {usuarios[user_id]:.2f}")
        else:
            bot.send_message(user_id, f"❌ Erro na compra: {texto}")
    except Exception as e:
        bot.send_message(user_id, f"❌ Erro ao acessar API: {str(e)}")

@app.route(f"/{API_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print(f"Erro processando update: {e}")
    return "OK", 200

if __name__ == "__main__":
    modo = os.environ.get("BOT_MODE", "polling")  # define pelo Render (webhook) ou local (polling)
    if modo == "webhook":
        bot.remove_webhook()
        bot.set_webhook(url='https://bot-sms-pix.onrender.com/' + API_TOKEN)
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        print("Rodando em modo polling (local/teste)")
        bot.remove_webhook()
        bot.polling()
