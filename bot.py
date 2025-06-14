import os
import time
import threading
import requests
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================
# Configurações iniciais
# ==========================
TOKEN = "7571534692:AAHLebRcTLA0x0XoDRXqKHpFev3tcePBC84"  # Seu token Telegram
WEBHOOK_URL_BASE = "https://bot-sms-pix.onrender.com"    # Sua URL Render
WEBHOOK_URL_PATH = f"/{TOKEN}"

SMSBOWER_API_KEY = "6lkWWVDjjTSCpfMGLtQZvD0Uwd1LQk5G"    # API SMSBOWER

# Serviços disponíveis e preço (R$)
SERVICOS = {
    "china": {"nome": "Verificar Telefone na China", "preco": 0.25, "smsbower_service": "picpaychina"},
    "mercadopago": {"nome": "Mercado Pago", "preco": 0.25, "smsbower_service": "mercadopago"},
    "picpay": {"nome": "PicPay", "preco": 0.25, "smsbower_service": "picpay"},
    "nubank": {"nome": "Nubank", "preco": 0.25, "smsbower_service": "nubank"},
    "astropay": {"nome": "Astropay", "preco": 0.25, "smsbower_service": "astropay"},
}

# Tempo de expiração do número em segundos (18 minutos)
EXPIRACAO_NUMERO = 18 * 60

# ==========================
# Variáveis globais (exemplo simples)
# ==========================
user_saldos = {}  # chat_id -> saldo (float)
user_numeros = {} # chat_id -> {"numero": ..., "activationId": ..., "servico": ..., "timestamp": ...}

# ==========================
# Inicializa bot e Flask
# ==========================
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ==========================
# Funções auxiliares
# ==========================
def get_saldo(chat_id):
    return user_saldos.get(chat_id, 0.0)

def add_saldo(chat_id, valor):
    saldo = get_saldo(chat_id)
    saldo += valor
    user_saldos[chat_id] = saldo

def descontar_saldo(chat_id, valor):
    saldo = get_saldo(chat_id)
    if saldo >= valor:
        user_saldos[chat_id] = saldo - valor
        return True
    else:
        return False

def compra_numero_sms(servico):
    """Chama API do smsbower para comprar número do serviço."""
    params = {
        "api_key": SMSBOWER_API_KEY,
        "action": "getNumber",
        "service": servico,
        "country": "cn",      # Exemplo fixo China, pode alterar
        "maxPrice": "10",     # Limite max preço
    }
    try:
        r = requests.get("https://smsbower.online/stubs/handler_api.php", params=params)
        if r.status_code == 200:
            text = r.text.strip()
            # Retorno esperado: ACCESS_NUMBER:activationId:phoneNumber
            if text.startswith("ACCESS_NUMBER"):
                parts = text.split(":")
                activationId = parts[1]
                phoneNumber = parts[2]
                return activationId, phoneNumber
            else:
                return None, text  # Retorna erro da API
        else:
            return None, f"Erro HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

def consulta_sms(activationId):
    """Consulta SMS recebido pela API do smsbower."""
    params = {
        "api_key": SMSBOWER_API_KEY,
        "action": "getStatus",
        "id": activationId
    }
    try:
        r = requests.get("https://smsbower.online/stubs/handler_api.php", params=params)
        if r.status_code == 200:
            text = r.text.strip()
            # Pode ser STATUS_CANCEL ou STATUS_OK:message ou STATUS_WAIT_CODE
            return text
        else:
            return f"Erro HTTP {r.status_code}"
    except Exception as e:
        return str(e)

# ==========================
# Comandos do bot
# ==========================

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    chat_id = message.chat.id
    texto = (
        "🤖 Bem-vindo ao Bot SMS com saldo!\n\n"
        "Você pode recarregar saldo e comprar números SMS para serviços.\n\n"
        "Use /saldo para ver seu saldo.\n"
        "Use /recarregar para adicionar saldo.\n"
        "Use /comprar para escolher um serviço e comprar número SMS.\n"
    )
    bot.send_message(chat_id, texto)

@bot.message_handler(commands=["saldo"])
def mostrar_saldo(message):
    chat_id = message.chat.id
    saldo = get_saldo(chat_id)
    bot.send_message(chat_id, f"Seu saldo atual é: R$ {saldo:.2f}")

@bot.message_handler(commands=["recarregar"])
def recarregar_saldo(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "💸 Envie o valor que deseja recarregar (ex: 2.50)")

    # Próxima mensagem do usuário será o valor, vamos usar next_step_handler
    bot.register_next_step_handler(message, processar_recarregar)

def processar_recarregar(message):
    chat_id = message.chat.id
    try:
        valor = float(message.text.replace(",", "."))
        if valor <= 0:
            bot.send_message(chat_id, "Valor inválido. Envie um número positivo.")
            return
    except:
        bot.send_message(chat_id, "Valor inválido. Envie um número válido como 2.50")
        return

    # Aqui você integraria Mercado Pago para gerar cobrança e confirmar
    # Para demo vamos adicionar direto (assumindo pagamento OK)
    add_saldo(chat_id, valor)
    bot.send_message(chat_id, f"✅ Saldo recarregado em R$ {valor:.2f} com sucesso!\nSeu saldo atual: R$ {get_saldo(chat_id):.2f}")

@bot.message_handler(commands=["comprar"])
def escolher_servico(message):
    chat_id = message.chat.id

    markup = InlineKeyboardMarkup(row_width=2)
    botoes = []
    for key, info in SERVICOS.items():
        botoes.append(InlineKeyboardButton(text=f"{info['nome']} - R$ {info['preco']:.2f}", callback_data=f"comprar_{key}"))
    markup.add(*botoes)

    bot.send_message(chat_id, "Escolha o serviço para comprar número SMS:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("comprar_"))
def comprar_numero_callback(call):
    chat_id = call.message.chat.id
    servico_key = call.data[len("comprar_"):]
    if servico_key not in SERVICOS:
        bot.answer_callback_query(call.id, "Serviço inválido.")
        return
    info = SERVICOS[servico_key]

    # Verifica saldo
    if get_saldo(chat_id) < info['preco']:
        bot.answer_callback_query(call.id, "Saldo insuficiente. Recarregue com /recarregar")
        return

    bot.answer_callback_query(call.id, "Comprando número... aguarde.")
    # Compra número
    activationId, resp = compra_numero_sms(info['smsbower_service'])
    if activationId:
        descontar_saldo(chat_id, info['preco'])
        user_numeros[chat_id] = {
            "numero": resp,
            "activationId": activationId,
            "servico": servico_key,
            "timestamp": time.time()
        }
        bot.send_message(chat_id, f"✅ Número comprado: {resp}\nAguarde o SMS de confirmação.")
        # Começar thread para monitorar SMS
        threading.Thread(target=monitorar_sms, args=(chat_id,), daemon=True).start()
    else:
        bot.send_message(chat_id, f"❌ Erro ao comprar número: {resp}")

def monitorar_sms(chat_id):
    # Espera e verifica SMS até 18 minutos
    start = time.time()
    while True:
        if chat_id not in user_numeros:
            break
        data = user_numeros[chat_id]
        if time.time() - data["timestamp"] > EXPIRACAO_NUMERO:
            bot.send_message(chat_id, "⏳ Tempo para receber SMS expirou. Número cancelado.")
            user_numeros.pop(chat_id, None)
            break
        resultado = consulta_sms(data["activationId"])
        if resultado.startswith("STATUS_OK:"):
            codigo = resultado.split("STATUS_OK:")[1]
            bot.send_message(chat_id, f"📩 SMS recebido: {codigo}")
            user_numeros.pop(chat_id, None)
            break
        elif resultado == "STATUS_CANCEL":
            bot.send_message(chat_id, "❌ Número foi cancelado pelo provedor.")
            user_numeros.pop(chat_id, None)
            break
        time.sleep(15)  # aguarda 15 seg antes de checar de novo

# ==========================
# Webhook Flask Routes
# ==========================
@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

@app.before_first_request
def setup_webhook():
    webhook_url = WEBHOOK_URL_BASE + WEBHOOK_URL_PATH
    if bot.set_webhook(url=webhook_url):
        print(f"Webhook configurado: {webhook_url}")
    else:
        print("Falha ao configurar webhook")

# ==========================
# Run app
# ==========================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
