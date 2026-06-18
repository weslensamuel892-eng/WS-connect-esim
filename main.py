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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8881674626:AAHzC5Qo-HrUJ3dNaJeX4yn3INvNHlBsU6Y").strip()
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
# IRONPAY ENGINE (V4 - FIXED FIELDS)
# ──────────────────────────────────────────────────────────────────────────────
def generate_cpf():
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def create_ironpay_v4_payment(price, chat_id, token, user=None, product_name="Recarga de Saldo"):
    amount_cents = int(Decimal(str(price)) * 100)
    customer_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or f"Cliente {chat_id}"
    
    # Payload rigoroso com todos os campos que a Ironpay exige
    payload = {
        "amount": amount_cents,
        "offer_hash": IRONPAY_OFFER_HASH,
        "product_hash": IRONPAY_OFFER_HASH,
        "title": str(product_name),
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
                "product_name": str(product_name),
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
        raise Exception(f"Erro {r.status_code}: {r.text}")
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
# BOT SETUP
# ──────────────────────────────────────────────────────────────────────────────
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
admin_state = {}

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
# MENUS
# ──────────────────────────────────────────────────────────────────────────────
def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Comprar E-SIM", callback_data="menu:plans")],
        [InlineKeyboardButton(text="👤 Meu Perfil / Saldo", callback_data="menu:profile")],
        [InlineKeyboardButton(text="💳 Adicionar Saldo", callback_data="menu:add_balance")],
        [InlineKeyboardButton(text="❓ Suporte", url="https://t.me/Mategazx")]
    ])

def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Adicionar Operadora", callback_data="admin:add_op")],
        [InlineKeyboardButton(text="➕ Adicionar Plano", callback_data="admin:add_plan")],
        [InlineKeyboardButton(text="📥 Repor Estoque", callback_data="admin:restock_menu")],
        [InlineKeyboardButton(text="📦 Ver Estoque", callback_data="admin:view_stock")],
        [InlineKeyboardButton(text="🖼 Mudar Foto Início", callback_data="admin:set_img")],
        [InlineKeyboardButton(text="📢 Enviar Mensagem Geral", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🔍 Testar API Ironpay", callback_data="admin:debug_api")]
    ])

# ──────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
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

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("🛠 *PAINEL ADMINISTRATIVO*\n\nEscolha uma opção para gerenciar o bot:", reply_markup=get_admin_kb(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin:"))
async def cb_admin(callback: CallbackQuery):
    action = callback.data.split(":")[1]
    uid = callback.from_user.id
    if action == "add_op":
        admin_state[uid] = {"action": "WAIT_OP_NAME"}
        await callback.message.edit_text("⌨️ Digite o nome da nova **Operadora** (ex: Vivo, Claro):", parse_mode="Markdown")
    elif action == "add_plan":
        ops = list(data["operators"].keys())
        if not ops: return await callback.answer("⚠️ Crie uma operadora primeiro!", show_alert=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=op, callback_data=f"admin:sel_op_plan:{op}")] for op in ops])
        await callback.message.edit_text("📶 Escolha a operadora para o novo plano:", reply_markup=kb)
    elif action == "restock_menu":
        ops = list(data["operators"].keys())
        if not ops: return await callback.answer("⚠️ Sem operadoras!", show_alert=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=op, callback_data=f"admin:sel_op_stock:{op}")] for op in ops])
        await callback.message.edit_text("📥 Escolha a operadora para repor:", reply_markup=kb)
    elif action == "view_stock":
        text = "📦 *ESTOQUE ATUAL*\n"
        for op, info in data.get("operators", {}).items():
            text += f"\n📶 {op}:\n"
            for gb in info["plans"]: text += f"  - {gb}: {get_plan_stock_count(op, gb)}\n"
        await callback.message.edit_text(text, reply_markup=get_admin_kb(), parse_mode="Markdown")
    elif action == "debug_api":
        await callback.answer("🔍 Testando API...", show_alert=False)
        try:
            pay_id, _ = create_ironpay_v4_payment(10.0, uid, "DEBUG", callback.from_user, "TESTE CONEXAO")
            await callback.message.edit_text(f"✅ *API CONECTADA!*\n\nID Gerado: `{pay_id}`", reply_markup=get_admin_kb(), parse_mode="Markdown")
        except Exception as e:
            await callback.message.edit_text(f"❌ *ERRO NA API:*\n\n`{str(e)}`", reply_markup=get_admin_kb(), parse_mode="Markdown")
    elif action == "set_img":
        admin_state[uid] = {"action": "WAIT_START_IMG"}
        await callback.message.edit_text("🖼 Envie a nova foto para o menu inicial:", parse_mode="Markdown")
    elif action == "broadcast":
        admin_state[uid] = {"action": "WAIT_BROADCAST"}
        await callback.message.edit_text("📢 Digite a mensagem para TODOS:", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin:sel_op_plan:"))
async def cb_admin_sel_op(callback: CallbackQuery):
    op = callback.data.split(":")[3]
    admin_state[callback.from_user.id] = {"action": "WAIT_PLAN_GB", "op": op}
    await callback.message.edit_text(f"📶 Operadora: *{op}*\n\n⌨️ Digite o tamanho do plano:", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin:sel_op_stock:"))
async def cb_admin_sel_op_stock(callback: CallbackQuery):
    op = callback.data.split(":")[3]
    plans = list(data["operators"][op]["plans"].keys())
    if not plans: return await callback.answer("⚠️ Sem planos!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=gb, callback_data=f"admin:sel_plan_stock:{op}:{gb}")] for gb in plans])
    await callback.message.edit_text(f"📥 Escolha o plano de *{op}* para repor:", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin:sel_plan_stock:"))
async def cb_admin_sel_plan_stock(callback: CallbackQuery):
    _, _, _, op, gb = callback.data.split(":")
    admin_state[callback.from_user.id] = {"action": "WAIT_STOCK_PHOTOS", "op": op, "gb": gb}
    await callback.message.edit_text(f"📥 *REPOSIÇÃO: {op} {gb}*\n\nEnvie as fotos agora. Use `/done` para sair.", parse_mode="Markdown")

@dp.message(F.text | F.photo)
async def handle_admin_input(message: Message):
    uid = message.from_user.id
    if uid not in admin_state: return
    state = admin_state[uid]
    if state["action"] == "WAIT_OP_NAME":
        op = message.text.strip()
        data["operators"][op] = {"plans": {}}
        save_data(data)
        del admin_state[uid]
        await message.answer(f"✅ Operadora *{op}* criada!", reply_markup=get_admin_kb())
    elif state["action"] == "WAIT_PLAN_GB":
        admin_state[uid]["gb"] = message.text.strip()
        admin_state[uid]["action"] = "WAIT_PLAN_PRICE"
        await message.answer(f"💰 Digite o Preço para *{state['op']} {message.text}*:")
    elif state["action"] == "WAIT_PLAN_PRICE":
        try:
            price = float(message.text.replace(",", "."))
            op, gb = state["op"], state["gb"]
            data["operators"][op]["plans"][gb] = {"price": price}
            save_data(data)
            (STOCK_DIR / op / gb).mkdir(parents=True, exist_ok=True)
            (SOLD_DIR / op / gb).mkdir(parents=True, exist_ok=True)
            del admin_state[uid]
            await message.answer(f"✅ Plano *{op} {gb}* criado!", reply_markup=get_admin_kb())
        except: await message.answer("❌ Preço inválido!")
    elif state["action"] == "WAIT_START_IMG" and message.photo:
        set_setting("start_image_id", message.photo[-1].file_id)
        del admin_state[uid]
        await message.answer("✅ Imagem atualizada!", reply_markup=get_admin_kb())
    elif state["action"] == "WAIT_BROADCAST":
        msg = message.text
        conn = get_db_connection()
        users = conn.execute("SELECT telegram_id FROM users").fetchall()
        conn.close()
        del admin_state[uid]
        await message.answer(f"📢 Enviando para {len(users)} usuários...")
        for u in users:
            try: await bot.send_message(u["telegram_id"], msg, parse_mode="Markdown")
            except: pass
        await message.answer("✅ Enviado!", reply_markup=get_admin_kb())
    elif state["action"] == "WAIT_STOCK_PHOTOS" and message.photo:
        op, gb = state["op"], state["gb"]
        d = STOCK_DIR / op / gb
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        dest = d / f"{uuid.uuid4().hex}.jpg"
        await bot.download_file(file.file_path, dest)
        conn = get_db_connection()
        with conn: conn.execute("INSERT OR REPLACE INTO stock_meta (file_path, caption) VALUES (?, ?)", (str(dest), message.caption or ""))
        conn.close()
        await message.reply(f"📸 Foto salva!")

@dp.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

@dp.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    text = f"👤 *MEU PERFIL*\n\n🆔 Seu ID: `{user['telegram_id']}`\n💰 Saldo: *R$ {user['balance']:.2f}*\n🛍 Gasto: *R$ {user['total_spent']:.2f}*"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Recarregar", callback_data="menu:add_balance")],[InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:home")]])
    if callback.message.photo: await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
    else: await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "menu:add_balance")
async def cb_add_balance(callback: CallbackQuery):
    text = "💳 *ADICIONAR SALDO*\n\nDigite `/pix <valor>` para gerar um pagamento.\n_Exemplo: /pix 50_"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:profile")]])
    if callback.message.photo: await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
    else: await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("pix"))
async def cmd_pix(message: Message, command: CommandObject):
    if not command.args: return await message.answer("⚠️ Use: `/pix <valor>`")
    try:
        amount = float(command.args.replace(",", "."))
        if amount < 5.0: return await message.answer("⚠️ Valor mínimo: R$ 5,00.")
        token = uuid.uuid4().hex
        pay_id, pix_code = create_ironpay_v4_payment(amount, message.chat.id, token, message.from_user, "Recarga de Saldo")
        conn = get_db_connection()
        with conn: conn.execute("INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (token, pay_id, message.from_user.id, "RECARGA", "SALDO", amount, "pending"))
        conn.close()
        await message.answer(f"💎 *RECARGA R$ {amount:.2f}*\n\nCopia e Cola:\n`{pix_code}`", parse_mode="Markdown")
    except Exception as e: await message.answer(f"❌ Erro: `{str(e)}`", parse_mode="Markdown")

@dp.callback_query(F.data == "menu:plans")
async def cb_plans(callback: CallbackQuery):
    ops = data.get("operators", {})
    if not ops: return await callback.answer("⚠️ Sem operadoras.", show_alert=True)
    btns = [[InlineKeyboardButton(text=f"📶 {op}", callback_data=f"op:{op}")] for op in ops]
    btns.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:home")])
    if callback.message.photo: await callback.message.edit_caption(caption="📶 *OPERADORAS:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")
    else: await callback.message.edit_text("📶 *OPERADORAS:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("op:"))
async def cb_op_plans(callback: CallbackQuery):
    op = callback.data.split(":")[1]
    plans = data["operators"].get(op, {}).get("plans", {})
    btns = [[InlineKeyboardButton(text=f"{gb} - R$ {info['price']:.2f}", callback_data=f"buy:{op}:{gb}")] for gb, info in plans.items()]
    btns.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:plans")])
    await callback.message.edit_text(f"📶 *PLANOS {op.upper()}:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    user = get_user(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Pagar com Saldo", callback_data=f"pay_balance:{op}:{gb}")],[InlineKeyboardButton(text="💎 Gerar PIX", callback_data=f"pay_pix:{op}:{gb}")],[InlineKeyboardButton(text="⬅️ Voltar", callback_data=f"op:{op}")]])
    await callback.message.edit_text(f"🛒 *RESUMO:* {op} {gb}\nValor: *R$ {price:.2f}*\nSeu Saldo: *R$ {user['balance']:.2f}*", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("pay_balance:"))
async def cb_pay_balance(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    user = get_user(callback.from_user.id)
    if user['balance'] < price: return await callback.answer("❌ Saldo insuficiente!", show_alert=True)
    photo_path = pick_from_stock(op, gb)
    if photo_path:
        conn = get_db_connection()
        meta = conn.execute("SELECT caption FROM stock_meta WHERE file_path = ?", (str(photo_path),)).fetchone()
        caption = meta["caption"] if meta else ""
        with conn: conn.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE telegram_id = ?", (price, price, callback.from_user.id))
        conn.close()
        await callback.message.delete()
        await bot.send_photo(callback.from_user.id, photo=FSInputFile(str(photo_path)), caption=f"✅ *Compra realizada!*\n\n{caption}", parse_mode="Markdown")
        shutil.move(str(photo_path), SOLD_DIR / op / gb / photo_path.name)
    else: await callback.answer("⚠️ Sem estoque!", show_alert=True)

@dp.callback_query(F.data.startswith("pay_pix:"))
async def cb_pay_pix(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    token = uuid.uuid4().hex
    try:
        pay_id, pix_code = create_ironpay_v4_payment(price, callback.message.chat.id, token, callback.from_user, f"eSIM {op} {gb}")
        conn = get_db_connection()
        with conn: conn.execute("INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (token, pay_id, callback.from_user.id, op, gb, price, "pending"))
        conn.close()
        await callback.message.edit_text(f"💎 *PAGAMENTO PIX*\n`{pix_code}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data=f"buy:{op}:{gb}")]]), parse_mode="Markdown")
    except: await callback.answer("❌ Erro ao gerar PIX.")

@dp.message(Command("done"))
async def cmd_done(message: Message):
    if message.from_user.id in admin_state: del admin_state[message.from_user.id]
    await message.answer("✅ Finalizado.", reply_markup=get_admin_kb())

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
                        await bot.send_message(uid, f"✅ *RECARGA APROVADA!* R$ {price:.2f} creditados.")
                    else:
                        photo_path = pick_from_stock(op, gb)
                        if photo_path:
                            conn = get_db_connection()
                            meta = conn.execute("SELECT caption FROM stock_meta WHERE file_path = ?", (str(photo_path),)).fetchone()
                            caption = meta["caption"] if meta else ""
                            await bot.send_message(uid, f"✅ *PAGAMENTO APROVADO!* {op} {gb}")
                            await bot.send_photo(uid, photo=FSInputFile(str(photo_path)), caption=caption)
                            shutil.move(str(photo_path), SOLD_DIR / op / gb / photo_path.name)
                        else: await bot.send_message(uid, "⚠️ Sem estoque! @Mategazx")
                    conn = get_db_connection()
                    with conn: conn.execute("UPDATE payments SET delivered = 1, status = 'approved' WHERE payment_id = ?", (r["payment_id"],))
                    conn.close()
                elif status == "expired":
                    conn = get_db_connection()
                    with conn: conn.execute("UPDATE payments SET status = 'expired' WHERE payment_id = ?", (r["payment_id"],))
                    conn.close()
        except: pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

async def main():
    asyncio.create_task(poll_payments())
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
