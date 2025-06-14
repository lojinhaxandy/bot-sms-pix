import telebot
import requests
import uuid
from flask import Flask, request, jsonify

API_TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
MERCADO_PAGO_TOKEN = "APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094"
SMSBOWER_API_KEY = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"
RENDER_URL = "https://bot-sms-pix.onrender.com"

bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

saldos = {}
pagamentos_pendentes = {}

servicos = {
    "Verificar Telefone Na China": 0.25,
    "Mercado Pago": 0.25,
    "PicPay": 0.25,
    "Nubank": 0.25,
    "AstroPay": 0.25
}

@bot.message_handler(commands=['start'])
def start(msg):
    bot.send_message(msg.chat.id, "👋 Bem-vindo ao bot SMS!\nUse /saldo, /recarregar ou /comprar.")

@bot.message_handler(commands=['saldo'])
def saldo(msg):
    user_id = msg.from_user.id
    bot.send_message(user_id, f"💰 Seu saldo é R$ {saldos.get(user_id, 0.0):.2f}")

@bot.message_handler(commands=['recarregar'])
def recarregar(msg):
    bot.send_message(msg.chat.id, "💸 Envie o valor que deseja recarregar (ex: 5.00)")
    bot.register_next_step_handler(msg, processa_valor)

def processa_valor(msg):
    try:
        valor = float(msg.text.replace(",", "."))
        if valor < 0.25:
            bot.send_message(msg.chat.id, "❌ Valor mínimo é R$ 0.25.")
            return

        user_id = msg.from_user.id
        idempotency_key = str(uuid.uuid4())

        headers = {
            "Authorization": f"Bearer {MERCADO_PAGO_TOKEN}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key
        }

        payload = {
            "transaction_amount": valor,
            "description": f"Recarga do usuário {user_id}",
            "payment_method_id": "pix",
            "payer": {"email": f"{user_id}@bot.com"}
        }

        r = requests.post("https://api.mercadopago.com/v1/payments", json=payload, headers=headers)
        data = r.json()

        if r.status_code != 201:
            bot.send_message(msg.chat.id, f"❌ Erro: {data.get('message', 'Tente novamente')}")
            return

        pagamento_id = data["id"]
        pix_qr = data["point_of_interaction"]["transaction_data"]["qr_code"]

        pagamentos_pendentes[str(pagamento_id)] = {"user_id": user_id, "valor": valor}

        bot.send_message(user_id, f"✅ Pague com Pix:\n```\n{pix_qr}\n```", parse_mode="Markdown")
    except:
        bot.send_message(msg.chat.id, "❌ Valor inválido.")

@bot.message_handler(commands=['comprar'])
def comprar(msg):
    texto = "📲 Serviços disponíveis:\n"
    for nome, preco in servicos.items():
        texto += f"- {nome} (R$ {preco:.2f})\n"
    texto += "\nEnvie o nome do serviço desejado:"
    bot.send_message(msg.chat.id, texto)
    bot.register_next_step_handler(msg, processa_compra)

def processa_compra(msg):
    user_id = msg.from_user.id
    servico = msg.text.strip()
    preco = servicos.get(servico)
    if not preco:
        bot.send_message(user_id, "❌ Serviço inválido.")
        return
    saldo = saldos.get(user_id, 0.0)
    if saldo < preco:
        bot.send_message(user_id, "❌ Saldo insuficiente.")
        return
    numero_fake = "+55 11999999999"
    saldos[user_id] -= preco
    bot.send_message(user_id, f"✅ Número adquirido: `{numero_fake}`\nSaldo: R$ {saldos[user_id]:.2f}", parse_mode="Markdown")

# 📩 Webhook do Mercado Pago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data.get("type") == "payment":
        payment_id = data["data"]["id"]
        check_payment(payment_id)
    return jsonify({"status": "ok"})

def check_payment(payment_id):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MERCADO_PAGO_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return
    data = r.json()
    if data["status"] == "approved":
        user_info = pagamentos_pendentes.pop(str(payment_id), None)
        if user_info:
            user_id = user_info["user_id"]
            valor = user_info["valor"]
            saldos[user_id] = saldos.get(user_id, 0.0) + valor
            bot.send_message(user_id, f"✅ Pagamento de R$ {valor:.2f} recebido!\nNovo saldo: R$ {saldos[user_id]:.2f}")

# 📍 Webhook do Telegram
@app.route(f"/{API_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = telebot.types.Update.de_json(request.stream.read().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"

# 🔁 Início do app
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_URL}/{API_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
