import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests

API_TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
API_KEY_SMSBOWER = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"  # sua API key do smsbower

bot = telebot.TeleBot(API_TOKEN)

# Serviços e seus preços
servicos = {
    "Verificar Telefone Na China": 0.25,
    "Mercado Pago": 0.25,
    "PicPay": 0.25,
    "Nubank": 0.25,
    "AstroPay": 0.25,
}

# Saldo dos usuários (em memória; para produção usar BD)
saldos = {}

@bot.message_handler(commands=['start'])
def start(msg):
    bot.send_message(msg.chat.id, "Olá! Use /recargar para adicionar saldo e /comprar para comprar um serviço.")

@bot.message_handler(commands=['recargar'])
def recargar(msg):
    bot.send_message(msg.chat.id, "💸 Envie o valor que deseja recarregar (ex: 2.50)")

@bot.message_handler(func=lambda m: m.text and m.text.replace('.', '', 1).isdigit())
def handle_recharge(msg):
    user_id = msg.from_user.id
    try:
        valor = float(msg.text)
        if valor <= 0:
            bot.send_message(user_id, "Valor inválido. Envie um número positivo.")
            return
        saldos[user_id] = saldos.get(user_id, 0) + valor
        bot.send_message(user_id, f"✅ Saldo recarregado: R$ {valor:.2f}\nSaldo atual: R$ {saldos[user_id]:.2f}")
    except Exception:
        bot.send_message(user_id, "Erro ao processar o valor. Envie um número válido.")

@bot.message_handler(commands=['saldo'])
def saldo(msg):
    user_id = msg.from_user.id
    saldo_atual = saldos.get(user_id, 0.0)
    bot.send_message(user_id, f"Seu saldo atual é: R$ {saldo_atual:.2f}")

@bot.message_handler(commands=['comprar'])
def comprar(msg):
    markup = InlineKeyboardMarkup(row_width=1)
    for nome_servico in servicos.keys():
        markup.add(InlineKeyboardButton(nome_servico, callback_data=f"comprar:{nome_servico}"))
    bot.send_message(msg.chat.id, "📲 Escolha o serviço desejado:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("comprar:"))
def callback_compra(call):
    user_id = call.from_user.id
    servico = call.data.split(":", 1)[1]
    preco = servicos.get(servico)

    if not preco:
        bot.answer_callback_query(call.id, "Serviço inválido.")
        return

    saldo = saldos.get(user_id, 0.0)
    if saldo < preco:
        bot.send_message(user_id, f"❌ Saldo insuficiente. Preço: R$ {preco:.2f}")
        bot.answer_callback_query(call.id)
        return

    # Chamar API smsbower para comprar número
    params = {
        "api_key": API_KEY_SMSBOWER,
        "action": "getNumber",
        "service": servico.lower().replace(" ", ""),
        "country": "br",
        "maxPrice": preco,
    }

    resposta = requests.get("https://smsbower.online/stubs/handler_api.php", params=params)
    texto = resposta.text.strip()

    if texto.startswith("ACCESS_NUMBER:"):
        _, activationId, phoneNumber = texto.split(":")
        saldos[user_id] -= preco
        bot.send_message(user_id,
                         f"✅ Serviço *{servico}* comprado!\n"
                         f"Número: `{phoneNumber}`\n"
                         f"Ativação ID: `{activationId}`\n"
                         f"Novo saldo: R$ {saldos[user_id]:.2f}",
                         parse_mode="Markdown")
        # Aqui pode guardar activationId para checar SMS depois
    else:
        bot.send_message(user_id, f"❌ Erro ao comprar número: {texto}")

    bot.answer_callback_query(call.id)

if __name__ == "__main__":
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_URL}/{API_TOKEN}")

    from flask import Flask, request

    app = Flask(__name__)

    @app.route(f"/{API_TOKEN}", methods=["POST"])
    def webhook():
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "", 200

    @app.route("/")
    def index():
        return "Bot SMS online!"

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
