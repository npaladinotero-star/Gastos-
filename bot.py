import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash-preview-04-17")

def init_db():
    con = sqlite3.connect("gastos.db")
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            descripcion TEXT,
            monto REAL,
            categoria TEXT,
            tipo TEXT DEFAULT 'gasto',
            fecha TEXT
        )
    """)
    con.commit()
    con.close()

def save_item(user_id, descripcion, monto, categoria, tipo, fecha):
    con = sqlite3.connect("gastos.db")
    cur = con.cursor()
    cur.execute(
        "INSERT INTO gastos (user_id, descripcion, monto, categoria, tipo, fecha) VALUES (?,?,?,?,?,?)",
        (user_id, descripcion, monto, categoria, tipo, fecha)
    )
    con.commit()
    con.close()

def get_summary(user_id, month=None):
    con = sqlite3.connect("gastos.db")
    cur = con.cursor()
    if month:
        cur.execute(
            "SELECT tipo, categoria, SUM(monto) FROM gastos WHERE user_id=? AND fecha LIKE ? GROUP BY tipo, categoria",
            (user_id, f"{month}%")
        )
    else:
        cur.execute(
            "SELECT tipo, categoria, SUM(monto) FROM gastos WHERE user_id=? GROUP BY tipo, categoria",
            (user_id,)
        )
    rows = cur.fetchall()
    con.close()
    return rows

def get_month_total(user_id, month, tipo="gasto"):
    con = sqlite3.connect("gastos.db")
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=? AND fecha LIKE ? AND tipo=?",
        (user_id, f"{month}%", tipo)
    )
    total = cur.fetchone()[0]
    con.close()
    return total

def get_recent(user_id, limit=10):
    con = sqlite3.connect("gastos.db")
    cur = con.cursor()
    cur.execute(
        "SELECT descripcion, monto, categoria, tipo, fecha FROM gastos WHERE user_id=? ORDER BY fecha DESC, id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    con.close()
    return rows

def parse_with_ai(text: str) -> dict:
    import time
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Sos un asistente de registro de gastos e ingresos en español argentino.
Analizá el mensaje y respondé SOLO con JSON válido sin markdown ni texto extra:
{{"items":[{{"tipo":"gasto|ingreso","descripcion":"...","monto":123.45,"categoria":"...","fecha":"YYYY-MM-DD"}}],"respuesta":"mensaje amigable en texto plano"}}
Categorías para gastos: Alimentación, Transporte, Entretenimiento, Salud, Hogar, Ropa, Educación, Tecnología, Otros.
Para ingresos usá categoria Ingreso.
Si no hay monto claro devolvé items vacío y pedí aclaración.
Fecha hoy: {today}. No uses emojis en la respuesta.

Mensaje: {text}"""

    for attempt in range(3):
        try:
            response = gemini.generate_content(prompt)
            raw = response.text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["/resumen", "/detalle"], ["/ayuda"]]
    await update.message.reply_text(
        "Hola! Soy tu asistente de gastos.\n\n"
        "Contame tus gastos o ingresos:\n"
        "- \"Gaste $500 en el super\"\n"
        "- \"Cobre $80000 de sueldo\"\n"
        "- \"Pague $350 de nafta y $200 de cafe\"\n\n"
        "Comandos: /resumen /detalle /ayuda",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now()
    month = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    gastos_mes = get_month_total(uid, month, "gasto")
    ingresos_mes = get_month_total(uid, month, "ingreso")
    gastos_prev = get_month_total(uid, prev_month, "gasto")
    balance = ingresos_mes - gastos_mes

    rows = get_summary(uid, month)
    by_cat = {}
    for tipo, cat, total in rows:
        if tipo == "gasto":
            by_cat[cat] = total

    lines = [f"Resumen de {now.strftime('%B %Y').capitalize()}", ""]
    lines.append(f"Ingresos:  ${ingresos_mes:,.0f}")
    lines.append(f"Gastos:    ${gastos_mes:,.0f}")
    lines.append(f"Balance:   {'+'if balance>=0 else ''}${balance:,.0f}")
    lines.append("")

    if gastos_prev > 0:
        diff = ((gastos_mes - gastos_prev) / gastos_prev) * 100
        arrow = "↑" if diff > 0 else "↓"
        lines.append(f"{arrow} {abs(diff):.1f}% vs mes anterior (${gastos_prev:,.0f})")
        lines.append("")

    if by_cat:
        lines.append("Por categoria:")
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            pct = (amt / gastos_mes * 100) if gastos_mes > 0 else 0
            lines.append(f"  {cat}: ${amt:,.0f} ({pct:.0f}%)")

    await update.message.reply_text("\n".join(lines))

async def cmd_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    recent = get_recent(uid, 10)
    if not recent:
        await update.message.reply_text("No hay registros aun.")
        return
    lines = ["Ultimos registros:", ""]
    for desc, monto, cat, tipo, fecha in recent:
        prefix = "+" if tipo == "ingreso" else "-"
        lines.append(f"{fecha}  {prefix}${monto:,.0f}  {desc} ({cat})")
    await update.message.reply_text("\n".join(lines))

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Como registrar gastos e ingresos:\n\n"
        "GASTOS:\n"
        "- \"Gaste $500 en pizza\"\n"
        "- \"Pague el super $3200\"\n"
        "- \"Compre ropa por $2000 y fui al cine $800\"\n\n"
        "INGRESOS:\n"
        "- \"Cobre el sueldo $85000\"\n"
        "- \"Entre $12000 de freelance\"\n\n"
        "COMANDOS:\n"
        "/resumen - resumen del mes con comparativa\n"
        "/detalle - ultimos 10 registros\n"
        "/ayuda - este mensaje"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    try:
        parsed = parse_with_ai(text)
    except Exception as e:
        logger.error(f"AI parse error: {type(e).__name__}: {e}")
        await update.message.reply_text(f"Error: {type(e).__name__}: {str(e)[:200]}")
        return

    for item in parsed.get("items", []):
        save_item(
            user_id=uid,
            descripcion=item.get("descripcion", "Sin descripcion"),
            monto=float(item.get("monto", 0)),
            categoria=item.get("categoria", "Otros"),
            tipo=item.get("tipo", "gasto"),
            fecha=item.get("fecha", datetime.now().strftime("%Y-%m-%d"))
        )

    await update.message.reply_text(parsed.get("respuesta", "Registrado."))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("detalle", cmd_detalle))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
