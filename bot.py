import os
import re
import json
import uuid
import shutil
import sqlite3
import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
import random
import requests
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import Command, CommandObject

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG & LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ws-connect-esim")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8947643629:AAHplN6gVttZ361oPmN9mbmMosPDyvPQaE8").strip()
IRONPAY_TOKEN = os.getenv("IRONPAY_TOKEN", "sz1Rt9JITY5MuWVNnraYwOgQ3CX4vtw76u4gp4M1Y8zCqNu3AVJTJO9onjMd").strip()
IRONPAY_OFFER_HASH = os.getenv("IRONPAY_OFFER_HASH", "eijjfftylw").strip()
IRONPAY_BASE_URL = "https://api.ironpayapp.com.br/api/public/v1"
POLL_INTERVAL_SECONDS = 15

ADMIN_IDS = {7748272760}
env_admins = os.getenv("ADMIN_IDS", "").split(",")
for x in env_admins:
    if x.strip().isdigit(): ADMIN_IDS.add(int(x))

DATA_FILE = Path("data.json")
STOCK_DIR = Path("stock")
SOLD_DIR = Path("sold")
DB_PATH = Path("payments.db")

STOCK_DIR.mkdir(parents=True, exist_ok=True)
SOLD_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE & DATA
# ──────────────────────────────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, payment_token TEXT UNIQUE, payment_id TEXT UNIQUE, telegram_id INTEGER, operator TEXT, plan_gb TEXT, price REAL, status TEXT, delivered INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("CREATE TABLE IF NOT EXISTS stock_meta (id INTEGER PRIMARY KEY, file_path TEXT UNIQUE, caption TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, total_spent REAL DEFAULT 0.0, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    finally: conn.close()

init_db()

def load_data():
    if not DATA_FILE.exists(): return {"operators": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"operators": {}}

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(d, f, indent=2, ensure_ascii=False)

data = load_data()

# ──────────────────────────────────────────────────────────────────────────────
# USER HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def get_user(uid):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
    if not user:
        with conn: conn.execute("INSERT INTO users (telegram_id) VALUES (?)", (uid,))
        user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
    conn.close()
    return user

def update_balance(uid, amount):
    conn = get_db_connection()
    with conn: conn.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, uid))
    conn.close()

def get_setting(key, default=None):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db_connection()
    with conn: conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# IRONPAY
# ──────────────────────────────────────────────────────────────────────────────
def generate_cpf():
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def create_ironpay_payment(price, chat_id, token, user=None, product_name="Recarga de Saldo"):
    amount_cents = int(Decimal(str(price)) * 100)
    customer_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or f"Cliente {chat_id}"
    
    payload = {
        "amount": amount_cents,
        "offer_hash": IRONPAY_OFFER_HASH,
        "product_hash": IRONPAY_OFFER_HASH, # Usando offer_hash como product_hash (comum na Ironpay)
        "title": product_name,
        "operation_type": "sell",
        "payment_method": "pix",
        "customer": {
            "name": customer_name[:100],
            "email": f"user{chat_id}@wsconnect.com",
            "phone_number": "11999999999",
            "document": generate_cpf()
        },
        "cart": [
            {
                "product_name": product_name,
                "quantity": 1,
                "price": amount_cents
            }
        ],
        "transaction_origin": "api",
        "expire_in_days": 1
    }
    
    url = f"{IRONPAY_BASE_URL}/transactions?api_token={IRONPAY_TOKEN}"
    r = requests.post(url, json=payload, timeout=25)
    if r.status_code != 200:
        logger.error(f"Erro Ironpay API: {r.status_code} - {r.text}")
        raise Exception(f"API Error: {r.status_code} - {r.text}")
    res = r.json()
    return res.get("hash"), res.get("pix", {}).get("pix_qr_code")

def check_ironpay_status(transaction_hash):
    url = f"{IRONPAY_BASE_URL}/transactions/{transaction_hash}?api_token={IRONPAY_TOKEN}"
    try:
        r = requests.get(url, timeout=15)
        res = r.json()
        status_raw = res.get("payment_status", res.get("status", res.get("data", {}).get("status", ""))).lower()
        if status_raw in ["paid", "approved", "success"]: return "approved"
        if status_raw in ["canceled", "expired", "refunded"]: return "expired"
        return "pending"
    except: return "pending"

# ──────────────────────────────────────────────────────────────────────────────
# STOCK
# ──────────────────────────────────────────────────────────────────────────────
def get_plan_stock_count(op, gb):
    d = STOCK_DIR / op / gb
    if not d.exists(): return 0
    return len([f for f in d.iterdir() if f.is_file()])

def pick_from_stock(op, gb):
    d = STOCK_DIR / op / gb
    if not d.exists(): return None
    files = sorted([f for f in d.iterdir() if f.is_file()])
    return files[0] if files else None

# ──────────────────────────────────────────────────────────────────────────────
# BOT HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
admin_state = {}

def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Comprar E-SIM", callback_data="menu:plans")],
        [InlineKeyboardButton(text="👤 Meu Perfil / Saldo", callback_data="menu:profile")],
        [InlineKeyboardButton(text="💳 Adicionar Saldo", callback_data="menu:add_balance")],
        [InlineKeyboardButton(text="❓ Suporte", url="https://t.me/Mategazx")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: Message):
    get_user(message.from_user.id)
    welcome_text = "👋 *Bem-vindo(a) ao WS Connect eSIM!*\n\nA melhor conexão para sua viagem ou dia a dia. Escolha uma opção abaixo:"
    img_id = get_setting("start_image_id")
    try:
        if img_id: await message.answer_photo(photo=img_id, caption=welcome_text, reply_markup=get_main_kb(), parse_mode="Markdown")
        elif Path("start_image.png").exists():
            msg = await message.answer_photo(photo=FSInputFile("start_image.png"), caption=welcome_text, reply_markup=get_main_kb(), parse_mode="Markdown")
            set_setting("start_image_id", msg.photo[-1].file_id)
        else: await message.answer(welcome_text, reply_markup=get_main_kb(), parse_mode="Markdown")
    except: await message.answer(welcome_text, reply_markup=get_main_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

@dp.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    text = f"👤 *MEU PERFIL*\n\n"
    text += f"🆔 Seu ID: `{user['telegram_id']}`\n"
    text += f"💰 Saldo Atual: *R$ {user['balance']:.2f}*\n"
    text += f"🛍 Total Gasto: *R$ {user['total_spent']:.2f}*\n\n"
    text += "Use o botão abaixo para recarregar seu saldo via PIX."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Recarregar Saldo", callback_data="menu:add_balance")],
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:home")]
    ])
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "menu:add_balance")
async def cb_add_balance(callback: CallbackQuery):
    text = "💳 *ADICIONAR SALDO*\n\nQuanto você deseja adicionar à sua conta?\n\nDigite `/pix <valor>` para gerar um pagamento.\n_Exemplo: /pix 50_"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:profile")]])
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("pix"))
async def cmd_pix(message: Message, command: CommandObject):
    if not command.args: return await message.answer("⚠️ Use: `/pix <valor>` (Ex: `/pix 20`)", parse_mode="Markdown")
    try:
        amount = float(command.args.replace(",", "."))
        if amount < 5.0: return await message.answer("⚠️ *VALOR MÍNIMO:* O valor mínimo para pagamentos via Ironpay é de *R$ 5,00*.", parse_mode="Markdown")
        token = uuid.uuid4().hex
        pay_id, pix_code = create_ironpay_payment(amount, message.chat.id, token, message.from_user, "Recarga de Saldo")
        conn = get_db_connection()
        with conn: conn.execute("INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (token, pay_id, message.from_user.id, "RECARGA", "SALDO", amount, "pending"))
        conn.close()
        await message.answer(f"💎 *RECARGA DE R$ {amount:.2f}*\n\nEscaneie o QR Code ou copie o código abaixo:\n\n`{pix_code}`\n\n_O saldo será creditado automaticamente após o pagamento._", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao gerar PIX: {e}")
        error_msg = str(e)
        if "5,00 reais" in error_msg:
            await message.answer("⚠️ *VALOR MÍNIMO:* A Ironpay exige que o valor seja de no mínimo *R$ 5,00*.", parse_mode="Markdown")
        else:
            await message.answer("❌ Erro ao gerar pagamento. Verifique se o valor é maior que R$ 5,00 ou tente novamente mais tarde.")

@dp.callback_query(F.data == "menu:plans")
async def cb_plans(callback: CallbackQuery):
    ops = data.get("operators", {})
    if not ops: return await callback.answer("⚠️ Nenhuma operadora disponível.", show_alert=True)
    btns = [[InlineKeyboardButton(text=f"📶 {op}", callback_data=f"op:{op}")] for op in ops]
    btns.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:home")])
    if callback.message.photo:
        await callback.message.edit_caption(caption="📶 *ESCOLHA UMA OPERADORA:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")
    else:
        await callback.message.edit_text("📶 *ESCOLHA UMA OPERADORA:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("op:"))
async def cb_op_plans(callback: CallbackQuery):
    op = callback.data.split(":")[1]
    plans = data["operators"].get(op, {}).get("plans", {})
    btns = []
    for gb, info in plans.items():
        count = get_plan_stock_count(op, gb)
        btns.append([InlineKeyboardButton(text=f"{gb} - R$ {info['price']:.2f} ({count} disp.)", callback_data=f"buy:{op}:{gb}")])
    btns.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:plans")])
    await callback.message.edit_text(f"📶 *PLANOS {op.upper()}:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    user = get_user(callback.from_user.id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Pagar com Saldo", callback_data=f"pay_balance:{op}:{gb}")],
        [InlineKeyboardButton(text="💎 Gerar PIX", callback_data=f"pay_pix:{op}:{gb}")],
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data=f"op:{op}")]
    ])
    text = f"🛒 *RESUMO DA COMPRA*\n\nProduto: {op} {gb}\nValor: *R$ {price:.2f}*\n\nSeu Saldo: *R$ {user['balance']:.2f}*\n\nEscolha a forma de pagamento:"
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("pay_balance:"))
async def cb_pay_balance(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    user = get_user(callback.from_user.id)
    
    if user['balance'] < price: return await callback.answer("❌ Saldo insuficiente! Recarregue sua conta.", show_alert=True)
    if get_plan_stock_count(op, gb) <= 0: return await callback.answer("⚠️ Sem estoque!", show_alert=True)
    
    photo_path = pick_from_stock(op, gb)
    if photo_path:
        conn = get_db_connection()
        meta = conn.execute("SELECT caption FROM stock_meta WHERE file_path = ?", (str(photo_path),)).fetchone()
        caption = meta["caption"] if meta else ""
        with conn:
            conn.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE telegram_id = ?", (price, price, callback.from_user.id))
        conn.close()
        
        await callback.message.delete()
        await bot.send_photo(callback.from_user.id, photo=FSInputFile(str(photo_path)), caption=f"✅ *Compra realizada com sucesso!*\n\n{caption}", parse_mode="Markdown")
        
        dest_dir = SOLD_DIR / op / gb
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(photo_path), dest_dir / photo_path.name)
    else: await callback.answer("⚠️ Erro ao processar estoque.", show_alert=True)

@dp.callback_query(F.data.startswith("pay_pix:"))
async def cb_pay_pix(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    token = uuid.uuid4().hex
    try:
        pay_id, pix_code = create_ironpay_payment(price, callback.message.chat.id, token, callback.from_user, f"eSIM {op} {gb}")
        conn = get_db_connection()
        with conn: conn.execute("INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (token, pay_id, callback.from_user.id, op, gb, price, "pending"))
        conn.close()
        text = f"💎 *PAGAMENTO PIX*\n\nProduto: *eSIM {op} {gb}*\nValor: *R$ {price:.2f}*\n\nCopia e Cola:\n`{pix_code}`\n\n_O produto será enviado automaticamente após o pagamento._"
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data=f"buy:{op}:{gb}")]]), parse_mode="Markdown")
    except: await callback.answer("❌ Erro ao gerar PIX.")

# ──────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ──────────────────────────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    text = "🛠 *PAINEL ADMIN*\n\n/addoperator <nome>\n/addplan <op> <gb> <preço>\n/restock <op> <gb>\n/stock\n/done\n/setstartimg\n/broadcast <mensagem>"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("addoperator"))
async def admin_addop(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS or not command.args: return
    data["operators"][command.args] = {"plans": {}}
    save_data(data)
    await message.answer(f"✅ Operadora {command.args} adicionada.")

@dp.message(Command("addplan"))
async def admin_addplan(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    args = command.args.split()
    if len(args) < 3: return
    try:
        op, gb, price = args[0], args[1], float(args[2].replace(",", "."))
        if op not in data["operators"]: data["operators"][op] = {"plans": {}}
        data["operators"][op]["plans"][gb] = {"price": price}
        save_data(data)
        (STOCK_DIR / op / gb).mkdir(parents=True, exist_ok=True)
        (SOLD_DIR / op / gb).mkdir(parents=True, exist_ok=True)
        await message.answer(f"✅ Plano {gb} de {op} adicionado por R$ {price:.2f}.")
    except: await message.answer("❌ Erro no formato. Use: /addplan <op> <gb> <preço>")

@dp.message(Command("restock"))
async def admin_restock(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS or not command.args: return
    args = command.args.split()
    if len(args) < 2: return
    admin_state[message.from_user.id] = (args[0], args[1])
    await message.answer(f"📥 Mandando fotos para {args[0]} {args[1]}. Envie a foto com a legenda. Use /done para sair.")

@dp.message(Command("done"))
async def admin_done(message: Message):
    if message.from_user.id in admin_state: del admin_state[message.from_user.id]
    await message.answer("✅ Finalizado.")

@dp.message(Command("setstartimg"))
async def admin_setimg(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    admin_state[message.from_user.id] = ("SYSTEM", "IMG")
    await message.answer("🖼 Envie a nova imagem de boas-vindas.")

@dp.message(F.photo)
async def admin_photo(message: Message):
    if message.from_user.id not in admin_state: return
    op, gb = admin_state[message.from_user.id]
    
    if op == "SYSTEM":
        set_setting("start_image_id", message.photo[-1].file_id)
        del admin_state[message.from_user.id]
        return await message.answer("✅ Imagem de boas-vindas atualizada!")
    
    d = STOCK_DIR / op / gb
    d.mkdir(parents=True, exist_ok=True)
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    dest = d / f"{uuid.uuid4().hex}.jpg"
    await bot.download_file(file.file_path, dest)
    
    conn = get_db_connection()
    with conn: conn.execute("INSERT OR REPLACE INTO stock_meta (file_path, caption) VALUES (?, ?)", (str(dest), message.caption or ""))
    conn.close()
    await message.reply(f"📸 Salvo! Estoque {op} {gb}: {get_plan_stock_count(op, gb)}")

@dp.message(Command("stock"))
async def admin_stock(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    text = "📦 *ESTOQUE ATUAL*\n"
    for op, info in data.get("operators", {}).items():
        text += f"\n📶 {op}:\n"
        for gb in info["plans"]: text += f"  - {gb}: {get_plan_stock_count(op, gb)}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("broadcast"))
async def admin_broadcast(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS or not command.args: return
    conn = get_db_connection()
    users = conn.execute("SELECT telegram_id FROM users").fetchall()
    conn.close()
    count = 0
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], command.args, parse_mode="Markdown")
            count += 1
        except: pass
    await message.answer(f"📢 Mensagem enviada para {count} usuários.")

# ──────────────────────────────────────────────────────────────────────────────
# POLLING
# ──────────────────────────────────────────────────────────────────────────────
async def poll_payments():
    while True:
        try:
            conn = get_db_connection()
            rows = conn.execute("SELECT * FROM payments WHERE status = 'pending' AND delivered = 0").fetchall()
            conn.close()
            for r in rows:
                status = check_ironpay_status(r["payment_id"])
                if status == "approved":
                    uid, op, gb, price = r["telegram_id"], r["operator"], r["plan_gb"], r["price"]
                    if op == "RECARGA":
                        update_balance(uid, price)
                        await bot.send_message(uid, f"✅ *RECARGA APROVADA!*\n\nSeu saldo de R$ {price:.2f} foi creditado com sucesso.", parse_mode="Markdown")
                    else:
                        photo_path = pick_from_stock(op, gb)
                        if photo_path:
                            conn = get_db_connection()
                            meta = conn.execute("SELECT caption FROM stock_meta WHERE file_path = ?", (str(photo_path),)).fetchone()
                            caption = meta["caption"] if meta else ""
                            await bot.send_message(uid, f"✅ *PAGAMENTO APROVADO!*\n\nProduto: {op} {gb}", parse_mode="Markdown")
                            await bot.send_photo(uid, photo=FSInputFile(str(photo_path)), caption=caption, parse_mode="Markdown")
                            
                            dest_dir = SOLD_DIR / op / gb
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(photo_path), dest_dir / photo_path.name)
                        else: 
                            await bot.send_message(uid, "⚠️ Pagamento aprovado, mas ficamos sem estoque! O administrador já foi avisado. Chame o suporte: @Mategazx")
                    
                    conn = get_db_connection()
                    with conn: conn.execute("UPDATE payments SET delivered = 1, status = 'approved' WHERE telegram_id = ? AND payment_id = ?", (uid, r["payment_id"]))
                    conn.close()
                elif status == "expired":
                    conn = get_db_connection()
                    with conn: conn.execute("UPDATE payments SET status = 'expired' WHERE payment_id = ?", (r["payment_id"],))
                    conn.close()
        except Exception as e: logger.error(f"Poll error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

async def main():
    asyncio.create_task(poll_payments())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
