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
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
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
if not TELEGRAM_BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN não definido no ambiente.")

# Ironpay Config
IRONPAY_TOKEN = os.getenv("IRONPAY_TOKEN", "sz1Rt9JITY5MuWVNnraYwOgQ3CX4vtw76u4gp4M1Y8zCqNu3AVJTJO9onjMd").strip()
IRONPAY_OFFER_HASH = os.getenv("IRONPAY_OFFER_HASH", "eijjfftylw").strip()
IRONPAY_BASE_URL = "https://api.ironpayapp.com.br/api/public/v1"

# Polling config
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))

# ──────────────────────────────────────────────────────────────────────────────
# PATHS / STORAGE / ADMINS
# ──────────────────────────────────────────────────────────────────────────────
# ID de admin padrão: 7748272760
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

# Verificação de membro (canal)
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK")
USE_CHANNEL_ID = os.getenv("USE_CHANNEL_ID", "true").lower() == "true"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

DEFAULT_DATA = {
    "operators": {},
    "pending_payments": {},
    "premium_products": {},
    "other_products": {},
    "texts": {
        "home_text": "👋 *Bem-vindo(a) ao WS Connect eSIM!*\n\nUse o botão abaixo para ver os planos disponíveis e comprar sua eSIM.",
        "plans_text": "✅ *Para gerar o Pix de pagamento é só digitar o comando abaixo:* 👇\n\n`/Pix` e colocar o valor desejado.\n👉 Exemplo: `/Pix 10`, `/Pix 20`, `/Pix 30`\n\nSeu produto será entregue após o pagamento confirmado, em PDF.",
        "no_plans_text": "⚠️ *Nenhum plano configurado no momento.*",
        "payment_text": "*Pagamento via PIX*\n*{operator} {plan_gb}* — R${price:.2f}\n\nEscaneie o QR Code ou use o código abaixo (copia e cola).",
        "pix_code_text": "*PIX copia e cola:*\n```\n{pix_code}\n```\n\nAssim que o pagamento for aprovado, vou te enviar o PDF da eSIM aqui no chat. ✅",
        "payment_success_text": "Aqui está sua eSIM {operator} {plan_gb} ✅",
        "payment_error_text": "Erro ao gerar PIX com Ironpay.",
        "admin_only_text": "Apenas administradores podem usar este comando.",
        "no_stock_text": "Sem estoque para este plano.",
        "start_text": "👋 *Bem-vindo(a) ao WS Connect eSIM!*\n\nUse o botão abaixo para ver os planos disponíveis e comprar sua eSIM.",
    }
}

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS & DATABASE
# ──────────────────────────────────────────────────────────────────────────────
def mdv2_escape(value: Any) -> str:
    if value is None: return ""
    s = str(value).replace("\\", "\\\\")
    for ch in r'_*[]()~`>#+-=|{}.!':
        s = s.replace(ch, f"\\{ch}")
    return s

def load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        save_data(DEFAULT_DATA)
        return DEFAULT_DATA
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_DATA["texts"].items():
            data.setdefault("texts", {}).setdefault(k, v)
        return data
    except Exception:
        return DEFAULT_DATA

def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

data = load_data()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preference_id TEXT,
                    payment_token TEXT UNIQUE,
                    payment_id TEXT UNIQUE,
                    telegram_id INTEGER,
                    operator TEXT,
                    plan_gb TEXT,
                    price REAL,
                    status TEXT,
                    delivered INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_bot INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_esims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE,
                    telegram_id INTEGER,
                    operator TEXT,
                    plan_gb TEXT,
                    pdf_name TEXT,
                    sent INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS client_credits (
                    telegram_id INTEGER PRIMARY KEY,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    delta_cents INTEGER,
                    balance_after_cents INTEGER,
                    reason TEXT,
                    ref TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
    finally:
        conn.close()

def get_setting(key: str, default: Any = None) -> Any:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    except: return default
    finally: conn.close()

def set_setting(key: str, value: str):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    finally: conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# IRONPAY INTEGRATION
# ──────────────────────────────────────────────────────────────────────────────
def generate_cpf():
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return '%s%s%s%s%s%s%s%s%s%s%s' % tuple(cpf)

def create_ironpay_payment(price: float, chat_id: int, payment_token: str, user: Optional[Any] = None) -> Tuple[str, str, None]:
    """Cria transação na Ironpay e retorna (hash, pix_code, None)"""
    if not IRONPAY_TOKEN:
        raise RuntimeError("IRONPAY_TOKEN não configurado.")
    
    amount_cents = int(Decimal(str(price)) * 100)
    customer_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or f"Cliente {chat_id}"
    customer_email = f"user{chat_id}@wsconnect.com"
    
    payload = {
        "amount": amount_cents,
        "offer_hash": IRONPAY_OFFER_HASH,
        "payment_method": "pix",
        "customer": {
            "name": customer_name[:100],
            "email": customer_email,
            "phone_number": "11999999999",
            "document": generate_cpf(),
        },
        "transaction_origin": "api",
        "expire_in_days": 1
    }
    
    url = f"{IRONPAY_BASE_URL}/transactions?api_token={IRONPAY_TOKEN}"
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    res_data = response.json()
    
    # Na Ironpay, o hash é usado para consulta e o pix_qr_code é o copia e cola
    transaction_hash = res_data.get("hash")
    pix_code = res_data.get("pix", {}).get("pix_qr_code")
    
    if not transaction_hash or not pix_code:
        raise RuntimeError(f"Erro na resposta da Ironpay: {res_data}")
        
    return transaction_hash, pix_code, None

def check_ironpay_status(transaction_hash: str) -> str:
    """Consulta status na Ironpay e mapeia para approved/pending/expired/error"""
    url = f"{IRONPAY_BASE_URL}/transactions/{transaction_hash}?api_token={IRONPAY_TOKEN}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 404: return "error"
        response.raise_for_status()
        res = response.json()
        # A estrutura pode variar, baseada na doc: { "success": true, "data": { "status": "paid" ... } }
        # Ou direto no objeto se for a resposta de criação
        status_raw = ""
        if "data" in res and isinstance(res["data"], dict):
            status_raw = res["data"].get("status", "").lower()
        else:
            status_raw = res.get("payment_status", res.get("status", "")).lower()

        if status_raw in ["paid", "approved", "success"]: return "approved"
        if status_raw in ["pending", "waiting", "processing"]: return "pending"
        if status_raw in ["canceled", "expired", "refunded"]: return "expired"
        return "pending"
    except Exception as e:
        logger.error(f"Erro ao consultar Ironpay {transaction_hash}: {e}")
        return "pending"

# ──────────────────────────────────────────────────────────────────────────────
# CORE LOGIC (Adapted from original)
# ──────────────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_plan_stock(operator: str, gb_label: str) -> int:
    gb_dir = STOCK_DIR / operator / gb_label
    if not gb_dir.exists(): return 0
    return len([p for p in gb_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])

def pick_one_pdf_from_stock(operator: str, plan_gb: str) -> Optional[Path]:
    gb_dir = STOCK_DIR / operator / plan_gb
    if not gb_dir.exists(): return None
    pdfs = sorted([p for p in gb_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])
    return pdfs[0] if pdfs else None

def move_to_sold(operator: str, plan_gb: str, pdf_path: Path) -> Path:
    sold_dir = SOLD_DIR / operator / plan_gb
    sold_dir.mkdir(parents=True, exist_ok=True)
    target_path = sold_dir / pdf_path.name
    shutil.move(str(pdf_path), target_path)
    return target_path

def register_payment(token, payment_id, telegram_id, operator, plan_gb, price, status="pending"):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO payments (payment_token, payment_id, telegram_id, operator, plan_gb, price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(payment_token) DO UPDATE SET
                    payment_id = excluded.payment_id,
                    status = excluded.status
            """, (token, payment_id, telegram_id, operator, plan_gb, price, status))
    finally: conn.close()

def update_payment_status(token, status):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("UPDATE payments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE payment_token = ?", (status, token))
    finally: conn.close()

def mark_delivered(payment_id):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("UPDATE payments SET delivered = 1, status = 'approved' WHERE payment_id = ?", (payment_id,))
    finally: conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# BOT HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Comprar E-SIM", callback_data="menu:plans")],
        [InlineKeyboardButton(text="👤 Meu Saldo", callback_data="menu:balance")]
    ])
    await message.answer(data["texts"]["home_text"], reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("pix"))
async def cmd_pix_manual(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Use: `/pix <valor>` (ex: `/pix 10`)", parse_mode="Markdown")
        return
    try:
        amount = float(command.args.replace(",", "."))
        if amount < 1.0: raise ValueError()
        
        token = uuid.uuid4().hex
        pay_id, pix_code, _ = create_ironpay_payment(amount, message.chat.id, token, message.from_user)
        register_payment(token, pay_id, message.from_user.id, "CREDIT", "TOPUP", amount)
        
        text = f"💠 *Pagamento de R$ {amount:.2f}*\n\nCopia e cola:\n`{pix_code}`"
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Erro no /pix")
        await message.answer("Erro ao gerar pagamento.")

@dp.callback_query(F.data == "menu:plans")
async def show_plans(callback: CallbackQuery):
    operators = data.get("operators", {})
    if not operators:
        await callback.message.edit_text(data["texts"]["no_plans_text"], parse_mode="Markdown")
        return
    
    buttons = []
    for op in operators:
        buttons.append([InlineKeyboardButton(text=f"📶 {op}", callback_data=f"op:{op}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Escolha uma operadora:", reply_markup=kb)

@dp.callback_query(F.data.startswith("op:"))
async def show_operator_plans(callback: CallbackQuery):
    op = callback.data.split(":", 1)[1]
    plans = data["operators"].get(op, {}).get("plans", {})
    buttons = []
    for gb, info in plans.items():
        stock = get_plan_stock(op, gb)
        buttons.append([InlineKeyboardButton(text=f"{gb} - R$ {info['price']:.2f} (Estoque: {stock})", callback_data=f"buy:{op}:{gb}")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="menu:plans")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(f"Planos {op}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("buy:"))
async def handle_buy(callback: CallbackQuery):
    _, op, gb = callback.data.split(":")
    price = data["operators"][op]["plans"][gb]["price"]
    stock = get_plan_stock(op, gb)
    
    if stock <= 0:
        await callback.answer("Sem estoque no momento!", show_alert=True)
        return
    
    token = uuid.uuid4().hex
    try:
        pay_id, pix_code, _ = create_ironpay_payment(price, callback.message.chat.id, token, callback.from_user)
        register_payment(token, pay_id, callback.from_user.id, op, gb, price)
        
        text = data["texts"]["payment_text"].format(operator=op, plan_gb=gb, price=price)
        text += f"\n\n`{pix_code}`"
        await callback.message.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro compra: {e}")
        await callback.answer("Erro ao processar pagamento.", show_alert=True)

# ──────────────────────────────────────────────────────────────────────────────
# POLLING LOOP
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
                    # Entrega o produto
                    telegram_id = r["telegram_id"]
                    op = r["operator"]
                    gb = r["plan_gb"]
                    
                    if op == "CREDIT" and gb == "TOPUP":
                        # Lógica de recarga de saldo simplificada
                        await bot.send_message(telegram_id, f"✅ Pagamento de R$ {r['price']:.2f} aprovado!")
                    else:
                        pdf = pick_one_pdf_from_stock(op, gb)
                        if pdf:
                            await bot.send_document(telegram_id, FSInputFile(str(pdf)), caption=f"Aqui está seu eSIM {op} {gb} ✅")
                            move_to_sold(op, gb, pdf)
                            mark_delivered(r["payment_id"])
                        else:
                            logger.error(f"SEM ESTOQUE para entrega paga: {r['payment_id']}")
                elif status == "expired" or status == "error":
                    update_payment_status(r["payment_token"], status)
                    
        except Exception as e:
            logger.error(f"Erro polling: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
async def main():
    init_db()
    asyncio.create_task(poll_payments())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
