import os
import re
import json
import uuid
import shutil
import sqlite3
import asyncio
import contextlib
import logging
import base64
import html
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List, Callable, Awaitable
from urllib.parse import quote_plus
import random
import requests
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    FSInputFile,
    URLInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ContentType,
)
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
LOG_FILE = Path(os.getenv("BOT_LOG_FILE", "bot.log"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ws-connect-esim")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "8947643629:AAHplN6gVttZ361oPmN9mbmMosPDyvPQaE8")).strip()
IRONPAY_TOKEN = os.getenv("IRONPAY_TOKEN", "sz1Rt9JITY5MuWVNnraYwOgQ3CX4vtw76u4gp4M1Y8zCqNu3AVJTJO9onjMd").strip()
IRONPAY_OFFER_HASH = os.getenv("IRONPAY_OFFER_HASH", "eijjfftylw").strip()
IRONPAY_BASE_URL = "https://api.ironpayapp.com.br/api/public/v1"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))

# Link da imagem de boas-vindas (pode ser substituído pelo ID do arquivo no Telegram depois do primeiro envio)
START_IMAGE_URL = "https://i.imgur.com/your-image-id.jpg" # Fallback
# Como não temos um link direto estável agora, vou deixar preparado para usar o arquivo local se estiver no repo ou enviar via URL se você me passar

ADMIN_IDS = {7748272760}
env_admins = os.getenv("ADMIN_IDS", "").split(",")
for x in env_admins:
    if x.strip().isdigit():
        ADMIN_IDS.add(int(x))

DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
STOCK_DIR = Path(os.getenv("STOCK_DIR", "stock"))
SOLD_DIR = Path(os.getenv("SOLD_DIR", "sold"))
DB_PATH = Path(os.getenv("DB_PATH", "payments.db"))

STOCK_DIR.mkdir(parents=True, exist_ok=True)
SOLD_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DATA = {
    "operators": {},
    "texts": {
        "home_text": "👋 *Bem-vindo(a) ao WS Connect eSIM!*\n\nA melhor conexão para sua viagem ou dia a dia. Escolha um dos nossos planos abaixo e receba seu QR Code instantaneamente após o pagamento.",
        "plans_text": "✅ *Escolha o plano ideal para você:*",
        "payment_text": "💎 *Pagamento via PIX*\n\n📦 Produto: *{operator} {plan_gb}*\n💰 Valor: *R${price:.2f}*\n\nEscaneie o QR Code ou use o código abaixo para pagar. O envio é automático!",
        "payment_success_text": "✅ *Pagamento Aprovado!*\n\nSua eSIM {operator} {plan_gb} está chegando...",
        "no_stock_text": "⚠️ *Desculpe!* No momento estamos sem estoque para este plano. Tente novamente mais tarde ou escolha outro plano.",
        "admin_only": "❌ Acesso restrito.",
    }
}

def load_data():
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
        return DEFAULT_DATA
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            # Merge defaults
            for k, v in DEFAULT_DATA["texts"].items():
                if k not in d.get("texts", {}): d.setdefault("texts", {})[k] = v
            return d
    except: return DEFAULT_DATA

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

data = load_data()

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE
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
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    finally: conn.close()

init_db()

def get_setting(key, default=None):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally: conn.close()

def set_setting(key, value):
    conn = get_db_connection()
    try:
        with conn: conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    finally: conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# IRONPAY
# ──────────────────────────────────────────────────────────────────────────────
def generate_cpf():
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def create_ironpay_payment(price, chat_id, token, user=None):
    amount_cents = int(Decimal(str(price)) * 100)
    customer_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or f"Cliente {chat_id}"
    payload = {
        "amount": amount_cents, "offer_hash": IRONPAY_OFFER_HASH, "payment_method": "pix",
        "customer": {"name": customer_name[:100], "email": f"user{chat_id}@wsconnect.com", "phone_number": "11999999999", "document": generate_cpf()},
        "transaction_origin": "api", "expire_in_days": 1
    }
    url = f"{IRONPAY_BASE_URL}/transactions?api_token={IRONPAY_TOKEN}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    return res.get("hash"), res.get("pix", {}).get("pix_qr_code"), None

def check_ironpay_status(transaction_hash):
    url = f"{IRONPAY_BASE_URL}/transactions/{transaction_hash}?api_token={IRONPAY_TOKEN}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 404: return "error"
        res = r.json()
        status_raw = res.get("payment_status", res.get("status", res.get("data", {}).get("status", ""))).lower()
        if status_raw in ["paid", "approved", "success"]: return "approved"
        if status_raw in ["canceled", "expired", "refunded"]: return "expired"
        return "pending"
    except: return "pending"

# ──────────────────────────────────────────────────────────────────────────────
# STOCK HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def add_to_stock_meta(file_path, caption):
    conn = get_db_connection()
    try:
        with conn: conn.execute("INSERT OR REPLACE INTO stock_meta (file_path, caption) VALUES (?, ?)", (str(file_path), caption))
    finally: conn.close()

def get_stock_meta(file_path):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT caption FROM stock_meta WHERE file_path = ?", (str(file_path),)).fetchone()
        return row["caption"] if row else ""
    finally: conn.close()

def get_plan_stock_count(op, gb):
    d = STOCK_DIR / op / gb
    if not d.exists(): return 0
    return len([f for f in d.iterdir() if f.is_file() and f.suffix.lower() in ['.jpg', '.jpeg', '.png']])

def pick_from_stock(op, gb):
    d = STOCK_DIR / op / gb
    if not d.exists(): return None
    files = sorted([f for f in d.iterdir() if f.is_file() and f.suffix.lower() in ['.jpg', '.jpeg', '.png']])
    return files[0] if files else None

# ──────────────────────────────────────────────────────────────────────────────
# BOT HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
admin_restock_state = {}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Comprar E-SIM", callback_data="menu:plans")],
        [InlineKeyboardButton(text="👤 Meu Saldo", callback_data="menu:balance")]
    ])
    
    start_img_id = get_setting("start_image_id")
    welcome_text = data["texts"]["home_text"]
    
    try:
        if start_img_id:
            await message.answer_photo(photo=start_img_id, caption=welcome_text, reply_markup=kb, parse_mode="Markdown")
        elif Path("start_image.png").exists():
            msg = await message.answer_photo(photo=FSInputFile("start_image.png"), caption=welcome_text, reply_markup=kb, parse_mode="Markdown")
            set_setting("start_image_id", msg.photo[-1].file_id)
        else:
            await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar imagem de start: {e}")
        await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not message.from_user.id in ADMIN_IDS: return
    text = "🛠 *PAINEL DO ADMINISTRADOR*\n\n"
    text += "✨ *Configuração:*\n"
    text += "• `/addoperator <nome>`\n  _Ex: /addoperator Vivo_\n"
    text += "• `/addplan <op> <gb> <preço>`\n  _Ex: /addplan Vivo 10GB 35.90_\n"
    text += "• `/setstartimg` (Mande este comando e depois a imagem)\n\n"
    text += "📦 *Estoque:*\n"
    text += "• `/restock <op> <gb>`\n  _Inicia a reposição. Mande as fotos com legenda._\n"
    text += "• `/stock` - Veja o que tem no estoque.\n"
    text += "• `/done` - Para de repor.\n\n"
    text += "📊 *Outros:*\n"
    text += "• `/broadcast <mensagem>` - Avisa todo mundo."
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("setstartimg"))
async def cmd_setstartimg(message: Message):
    if not message.from_user.id in ADMIN_IDS: return
    admin_restock_state[message.from_user.id] = ("SYSTEM", "START_IMG")
    await message.answer("🖼 Agora envie a imagem que você quer no topo do /start.")

@dp.message(Command("addoperator"))
async def cmd_addop(message: Message, command: CommandObject):
    if not message.from_user.id in ADMIN_IDS: return
    if not command.args: return await message.answer("⚠️ Use: /addoperator <nome>")
    op_name = command.args.strip()
    data["operators"][op_name] = {"plans": {}}
    save_data(data)
    await message.answer(f"✅ Operadora *{op_name}* cadastrada!", parse_mode="Markdown")

@dp.message(Command("addplan"))
async def cmd_addplan(message: Message, command: CommandObject):
    if not message.from_user.id in ADMIN_IDS: return
    args = command.args.split() if command.args else []
    if len(args) < 3: return await message.answer("⚠️ Use: /addplan <operadora> <gb> <preço>")
    op, gb, price = args[0], args[1], float(args[2].replace(",", "."))
    if op not in data["operators"]: data["operators"][op] = {"plans": {}}
    data["operators"][op]["plans"][gb] = {"price": price}
    save_data(data)
    await message.answer(f"✅ Plano *{gb}* da *{op}* criado por *R$ {price:.2f}*.", parse_mode="Markdown")

@dp.message(Command("restock"))
async def cmd_restock(message: Message, command: CommandObject):
    if not message.from_user.id in ADMIN_IDS: return
    args = command.args.split() if command.args else []
    if len(args) < 2: return await message.answer("⚠️ Use: /restock <operadora> <gb>")
    admin_restock_state[message.from_user.id] = (args[0], args[1])
    await message.answer(f"📥 *MODO REPOSIÇÃO ATIVO*\n\nOperadora: {args[0]}\nPlano: {args[1]}\n\nAgora envie as fotos dos QR Codes. O texto que você escrever na legenda da foto será o que o cliente vai receber.\n\nUse `/done` para finalizar.", parse_mode="Markdown")

@dp.message(Command("done"))
async def cmd_done(message: Message):
    if message.from_user.id in admin_restock_state:
        del admin_restock_state[message.from_user.id]
        await message.answer("✅ Operação finalizada com sucesso.")

@dp.message(F.photo)
async def handle_photo(message: Message):
    if message.from_user.id not in admin_restock_state: return
    op, gb = admin_restock_state[message.from_user.id]
    
    if op == "SYSTEM" and gb == "START_IMG":
        set_setting("start_image_id", message.photo[-1].file_id)
        del admin_restock_state[message.from_user.id]
        return await message.answer("✅ Imagem de boas-vindas atualizada!")

    d = STOCK_DIR / op / gb
    d.mkdir(parents=True, exist_ok=True)
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    dest = d / f"{uuid.uuid4().hex}.jpg"
    await bot.download_file(file.file_path, dest)
    add_to_stock_meta(dest, message.caption or "")
    await message.reply(f"📸 Item salvo! Estoque {op} {gb}: {get_plan_stock_count(op, gb)}")

@dp.message(Command("stock"))
async def cmd_stock(message: Message):
    if not message.from_user.id in ADMIN_IDS: return
    text = "📦 *RELATÓRIO DE ESTOQUE*\n\n"
    for op, info in data["operators"].items():
        text += f"📶 *{op}:*\n"
        for gb in info["plans"]:
            count = get_plan_stock_count(op, gb)
            text += f"  └ {gb}: {count} unidades\n"
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "menu:plans")
async def show_plans(callback: CallbackQuery):
    ops = data.get("operators", {})
    if not ops: return await callback.message.edit_text("⚠️ Nenhuma operadora disponível no momento.")
    btns = [[InlineKeyboardButton(text=f"📶 {op}", callback_data=f"op:{op}")] for op in ops]
    await callback.message.edit_text(data["texts"]["plans_text"], reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("op:"))
async def show_op_plans(callback: CallbackQuery):
    op = callback.data.split(":")[1]
    plans = data["operators"].get(op, {}).get("plans", {})
    btns = []
    for gb, info in plans.items():
        count = get_plan_stock_count(op, gb)
        btns.append([InlineKeyboardButton(text=f"{gb} - R$ {info['price']:.2f} ({count} disp.)", callback_data=f"buy:{op}:{gb}")])
    btns.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:plans")])
    await callback.message.edit_text(f"Planos disponíveis para *{op}*:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy:"))
async def handle_buy(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    if get_plan_stock_count(op, gb) <= 0: return await callback.answer(data["texts"]["no_stock_text"], show_alert=True)
    
    token = uuid.uuid4().hex
    try:
        pay_id, pix_code, _ = create_ironpay_payment(price, callback.message.chat.id, token, callback.from_user)
        conn = get_db_connection()
        with conn: conn.execute("INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (token, pay_id, callback.from_user.id, op, gb, price, "pending"))
        conn.close()
        text = data["texts"]["payment_text"].format(operator=op, plan_gb=gb, price=price)
        text += f"\n\n`{pix_code}`"
        await callback.message.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro PIX: {e}")
        await callback.answer("❌ Erro ao gerar pagamento. Tente novamente.", show_alert=True)

async def poll_payments():
    while True:
        try:
            conn = get_db_connection()
            rows = conn.execute("SELECT * FROM payments WHERE status = 'pending' AND delivered = 0").fetchall()
            conn.close()
            for r in rows:
                status = check_ironpay_status(r["payment_id"])
                if status == "approved":
                    op, gb, uid = r["operator"], r["plan_gb"], r["telegram_id"]
                    photo_path = pick_from_stock(op, gb)
                    if photo_path:
                        caption = get_stock_meta(photo_path)
                        await bot.send_message(uid, data["texts"]["payment_success_text"].format(operator=op, plan_gb=gb), parse_mode="Markdown")
                        await bot.send_photo(uid, FSInputFile(str(photo_path)), caption=caption)
                        sold_dir = SOLD_DIR / op / gb
                        sold_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(photo_path), sold_dir / photo_path.name)
                        conn = get_db_connection()
                        with conn: conn.execute("UPDATE payments SET delivered = 1, status = 'approved' WHERE payment_id = ?", (r["payment_id"],))
                        conn.close()
                    else:
                        logger.error(f"SEM ESTOQUE PARA ENTREGA: {r['payment_id']}")
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
