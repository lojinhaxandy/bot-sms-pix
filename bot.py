import os
import time
import threading
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"
API_KEY_SMSBOWER = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"

bot = telebot.TeleBot(TOKEN)

# Saldo dos usuários (exemplo simples, use banco de dados pra produção)
user_saldos = {}  # user_id: float

# Ativações pendentes
user_activations = {}  # user_id: {"activationId": str, "phone": str, "timestamp": float}

ACTIVATION_TIMEOUT = 18 * 60  # 18 minutos em segundos
VALOR_POR_NUMERO = 0.25  # preço unitário

# Função para verificar e limpar ativações expiradas
def limpar_ativações_expiradas():
    while True:
        now = time.time()
        to_remove = []
        for user_id, data in user_activations.items():
            if now - data["timestamp"] > ACTIVATION_TIMEOUT:
                to_remove.append(user_id)
        for user_id in to_remove:
            del user_activations[user_id]
            bot.send_message(user_id, "⏳ Sua ativação expirou após 18 minutos. Compre um novo número se desejar.")
        time.sleep(60)

threading.Thread(target=limpar_ativações_expiradas, daemon=True).start()

def get_sms_code(activationId):
    url = "https://smsbower.online/stubs/handler_api.php"
    params = {"api_key": API_KEY_SMSBOWER, "action": "getStatus", "id": activationId}
    resp = requests.get(url, params=params).text.strip()
    if resp.startswith("STATUS_OK:"):
        return resp.split(":")[1]
    elif resp == "STATUS_WAIT_CODE":
        return None
    else:
        return "error"

def cancel_activation(activationId):
    url = "https://smsbower.online/stubs/handler_api.php"
    params = {"api_key": API_KEY_SMSBOWER, "action": "setStatus", "status": "8", "id": activationId}
    resp = requests.get(url, params=params).text.strip()
    return resp

# Função simulada para comprar número na API smsbower (adicione params reais)
def comprar_numero(service="picpay", country="br", maxPrice=1):
    url = "https://smsbower.online/stubs/handler_api.php"
    params = {
        "api_key": API_KEY_SMSBOWER,
        "action": "getNumber",
        "service": service,
        "country": country,
        "maxPrice": maxPrice,
    }
    resp = requests.get(url, params=params).text.strip()
    if resp.startswith("ACCESS_NUMBER:"):
        parts = resp.split(":")
        activationId = parts[1]
        phone = parts[2]
        return activationId, phone
    else:
        return None, None

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id not in user_saldos:
        user_saldos[user_id] = 0.0
    bot.send_message(user_id, "👋 Bem-vindo! Use /saldo para ver seu saldo e /recarregar para adicionar créditos.")

@bot.message_handler(commands=['saldo'])
def saldo(message):
    user_id = message.from_user.id
    saldo = user_saldos.get(user_id, 0.0)
    bot.send_message(user_id, f"💰 Seu saldo atual: R$ {saldo:.2f}")

@bot.message_handler(commands=['recarregar'])
def recarregar(message):
    user_id = message.from_user.id
    bot.send_message(user_id, "Para recarregar, envie o valor que deseja adicionar (exemplo: 5.00)")

    @bot.message_handler(func=lambda m: m.from_user.id == user_id)
    def recebe_valor(m):
        try:
            valor = float(m.text.replace(",", "."))
            if valor <= 0:
                bot.send_message(user_id, "❌ Valor inválido. Envie um valor maior que zero.")
                return
            # Aqui deveria criar cobrança Mercado Pago e enviar QR code para pagamento
            # Para simplificar, vamos adicionar direto (simule o pagamento aprovado)
            user_saldos[user_id] = user_saldos.get(user_id, 0.0) + valor
            bot.send_message(user_id, f"✅ Saldo recarregado em R$ {valor:.2f}. Novo saldo: R$ {user_saldos[user_id]:.2f}")
            bot.register_next_step_handler(m, None)  # Para parar o handler aninhado
        except:
            bot.send_message(user_id, "❌ Por favor, envie um valor válido.")

@bot.message_handler(commands=['comprar'])
def comprar(message):
    user_id = message.from_user.id
    saldo = user_saldos.get(user_id, 0.0)
    if saldo < VALOR_POR_NUMERO:
        bot.send_message(user_id, f"❌ Saldo insuficiente. Seu saldo: R$ {saldo:.2f}. Recarregue usando /recarregar")
        return

    # Compra número (exemplo serviço picpay, país BR)
    activationId, phone = comprar_numero(service="picpay", country="br", maxPrice=1)
    if not activationId:
        bot.send_message(user_id, "❌ Erro ao comprar número, tente novamente mais tarde.")
        return

    # Descontar saldo
    user_saldos[user_id] -= VALOR_POR_NUMERO

    # Salvar ativação
    user_activations[user_id] = {"activationId": activationId, "phone": phone, "timestamp": time.time()}

    # Enviar número e botões
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📩 Novo SMS", callback_data="new_sms"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancel_activation")
    )
    bot.send_message(user_id, f"✅ Número comprado: {phone}\nSaldo descontado: R$ {VALOR_POR_NUMERO:.2f}\nUse os botões para gerenciar seu SMS.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    if user_id not in user_activations:
        bot.answer_callback_query(call.id, "Você não tem ativações pendentes.")
        return

    # Verificar timeout
    data = user_activations[user_id]
    if time.time() - data["timestamp"] > ACTIVATION_TIMEOUT:
        del user_activations[user_id]
        bot.answer_callback_query(call.id, "Sua ativação expirou. Compre um novo número.")
        bot.send_message(user_id, "⏳ Sua ativação expirou após 18 minutos. Compre um novo número se desejar.")
        return

    activationId = data["activationId"]

    if call.data == "new_sms":
        bot.answer_callback_query(call.id, "Consultando SMS...")
        codigo = get_sms_code(activationId)
        if codigo == "error":
            bot.send_message(user_id, "❌ Erro na consulta do código.")
        elif codigo is None:
            bot.send_message(user_id, "⌛ Código ainda não chegou, tente novamente em alguns segundos.")
        else:
            bot.send_message(user_id, f"✅ Código recebido: {codigo}")
            del user_activations[user_id]

    elif call.data == "cancel_activation":
        bot.answer_callback_query(call.id, "Cancelando ativação...")
        resp = cancel_activation(activationId)
        if resp == "ACCESS_CANCEL":
            bot.send_message(user_id, "❌ Ativação cancelada com sucesso.")
        else:
            bot.send_message(user_id, f"❌ Erro ao cancelar: {resp}")
        if user_id in user_activations:
            del user_activations[user_id]

if __name__ == "__main__":
    # Para rodar local com polling
    bot.infinity_polling()
