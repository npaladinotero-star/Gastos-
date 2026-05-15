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
gemini = genai.GenerativeModel("gemini-2.5-flash")

DB_PATH = "gastos.db"

# ── Base de datos ──────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            descripcion TEXT,
            monto REAL,
            categoria TEXT,
            tipo TEXT DEFAULT 'gasto',
            medio_pago TEXT DEFAULT 'efectivo',
            fecha TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT
        )
    """)
    try:
        cur.execute("ALTER TABLE gastos ADD COLUMN medio_pago TEXT DEFAULT 'efectivo'")
    except:
        pass
    con.commit()
    con.close()

def register_user(user_id, username):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO usuarios (user_id, username, created_at) VALUES (?,?,?)",
                (user_id, username, datetime.now().strftime("%Y-%m-%d")))
    con.commit()
    con.close()

def get_all_users():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM usuarios")
    rows = cur.fetchall()
    con.close()
    return [r[0] for r in rows]

def save_item(user_id, descripcion, monto, categoria, tipo, medio_pago, fecha):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO gastos (user_id, descripcion, monto, categoria, tipo, medio_pago, fecha) VALUES (?,?,?,?,?,?,?)",
        (user_id, descripcion, monto, categoria, tipo, medio_pago, fecha)
    )
    con.commit()
    con.close()

def get_gastos_rango(user_id, desde, hasta):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT descripcion, monto, categoria, tipo, medio_pago, fecha FROM gastos WHERE user_id=? AND fecha>=? AND fecha<=? ORDER BY fecha DESC",
        (user_id, desde, hasta)
    )
    rows = cur.fetchall()
    con.close()
    return rows

def get_cat_totals(user_id, month):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT categoria, SUM(monto) FROM gastos WHERE user_id=? AND fecha LIKE ? AND tipo='gasto' GROUP BY categoria",
        (user_id, f"{month}%")
    )
    rows = cur.fetchall()
    con.close()
    return {cat: int(amt) for cat, amt in rows}

def get_month_total(user_id, month, tipo="gasto"):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=? AND fecha LIKE ? AND tipo=?",
        (user_id, f"{month}%", tipo)
    )
    total = cur.fetchone()[0]
    con.close()
    return int(total)

def get_medio_totals(user_id, month):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT medio_pago, SUM(monto) FROM gastos WHERE user_id=? AND fecha LIKE ? AND tipo='gasto' GROUP BY medio_pago",
        (user_id, f"{month}%")
    )
    rows = cur.fetchall()
    con.close()
    return {r[0]: int(r[1]) for r in rows}

def get_recent(user_id, limit=10):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT descripcion, monto, categoria, tipo, medio_pago, fecha FROM gastos WHERE user_id=? ORDER BY fecha DESC, id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    con.close()
    return rows

def prev_month_key(month):
    y, m = month.split("-")
    first = datetime(int(y), int(m), 1)
    prev = first - timedelta(days=1)
    return prev.strftime("%Y-%m")

def month_label(month):
    y, m = month.split("-")
    names = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    return f"{names[int(m)-1]} {y}"

# ── Generar Excel semanal ──────────────────────────────────────────────────────

def generar_excel_semanal(user_id, desde, hasta):
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.chart import BarChart, Reference
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    rows = get_gastos_rango(user_id, desde, hasta)
    if not rows:
        return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detalle"

    header_fill = PatternFill("solid", fgColor="1A1A2E")
    header_font = Font(color="FFFFFF", bold=True)
    alt_fill = PatternFill("solid", fgColor="F5F5F5")

    headers = ["Fecha", "Descripción", "Categoría", "Tipo", "Medio de pago", "Monto"]
    col_widths = [12, 35, 18, 10, 15, 14]
    for i, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = w

    total_gastos = total_ingresos = 0
    for r_idx, (desc, monto, cat, tipo, medio, fecha) in enumerate(rows, 2):
        ws.cell(row=r_idx, column=1, value=fecha)
        ws.cell(row=r_idx, column=2, value=desc)
        ws.cell(row=r_idx, column=3, value=cat)
        ws.cell(row=r_idx, column=4, value=tipo.capitalize())
        ws.cell(row=r_idx, column=5, value=(medio or "efectivo").capitalize())
        c = ws.cell(row=r_idx, column=6, value=int(monto))
        c.number_format = '"$"#,##0'
        if r_idx % 2 == 0:
            for col in range(1, 7):
                ws.cell(row=r_idx, column=col).fill = alt_fill
        if tipo == "gasto": total_gastos += int(monto)
        else: total_ingresos += int(monto)

    last = len(rows) + 2
    for label, val, color in [("TOTAL GASTOS", total_gastos, "C0392B"), ("TOTAL INGRESOS", total_ingresos, "1D9E75"), ("BALANCE", total_ingresos - total_gastos, "1D9E75" if total_ingresos >= total_gastos else "C0392B")]:
        ws.cell(row=last, column=5, value=label).font = Font(bold=True)
        c = ws.cell(row=last, column=6, value=val)
        c.font = Font(bold=True, color=color)
        c.number_format = '"$"#,##0'
        last += 1

    # Hoja resumen
    ws2 = wb.create_sheet("Resumen")
    ws2.column_dimensions["A"].width = 20
    ws2.column_dimensions["B"].width = 16
    by_cat = {}
    by_medio = {}
    for desc, monto, cat, tipo, medio, fecha in rows:
        if tipo == "gasto":
            by_cat[cat] = by_cat.get(cat, 0) + int(monto)
            by_medio[medio or "efectivo"] = by_medio.get(medio or "efectivo", 0) + int(monto)

    for c, h in enumerate(["Categoría", "Monto"], 1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.font = header_font; cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for i, (cat, amt) in enumerate(sorted(by_cat.items(), key=lambda x: -x[1]), 2):
        ws2.cell(row=i, column=1, value=cat)
        c = ws2.cell(row=i, column=2, value=amt)
        c.number_format = '"$"#,##0'

    if by_cat:
        chart = BarChart()
        chart.type = "col"
        chart.title = "Gastos por Categoría"
        chart.width = 18; chart.height = 12
        data_ref = Reference(ws2, min_col=2, min_row=1, max_row=len(by_cat)+1)
        cats_ref = Reference(ws2, min_col=1, min_row=2, max_row=len(by_cat)+1)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.series[0].graphicalProperties.solidFill = "7F77DD"
        ws2.add_chart(chart, f"A{len(by_cat)+4}")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

# ── Generar Excel mensual con comparativa ─────────────────────────────────────

def generar_excel_mensual(user_id, month):
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.chart import BarChart, Reference
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    prev = prev_month_key(month)
    y, m = month.split("-")
    import calendar
    ultimo_dia = calendar.monthrange(int(y), int(m))[1]
    desde = f"{month}-01"
    hasta = f"{month}-{ultimo_dia:02d}"

    rows = get_gastos_rango(user_id, desde, hasta)
    if not rows:
        return None

    cats_mes = get_cat_totals(user_id, month)
    cats_prev = get_cat_totals(user_id, prev)
    total_mes = get_month_total(user_id, month, "gasto")
    total_prev = get_month_total(user_id, prev, "gasto")
    ingresos_mes = get_month_total(user_id, month, "ingreso")
    medios = get_medio_totals(user_id, month)

    wb = openpyxl.Workbook()

    # ── Hoja 1: Resumen mensual ──
    ws = wb.active
    ws.title = "Resumen"
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 14

    header_fill = PatternFill("solid", fgColor="1A1A2E")
    header_font = Font(color="FFFFFF", bold=True)
    green_font = Font(color="1D9E75", bold=True)
    red_font = Font(color="C0392B", bold=True)
    alt_fill = PatternFill("solid", fgColor="F0F0F8")

    # Título
    ws.merge_cells("A1:E1")
    title_cell = ws["A1"]
    title_cell.value = f"Resumen {month_label(month)}"
    title_cell.font = Font(bold=True, size=14, color="1A1A2E")
    title_cell.alignment = Alignment(horizontal="center")

    # Métricas generales
    ws.cell(row=2, column=1, value="Ingresos del mes").font = Font(bold=True)
    c = ws.cell(row=2, column=2, value=ingresos_mes)
    c.number_format = '"$"#,##0'; c.font = green_font

    ws.cell(row=3, column=1, value="Total gastos").font = Font(bold=True)
    c = ws.cell(row=3, column=2, value=total_mes)
    c.number_format = '"$"#,##0'; c.font = red_font

    bal = ingresos_mes - total_mes
    ws.cell(row=4, column=1, value="Balance").font = Font(bold=True)
    c = ws.cell(row=4, column=2, value=bal)
    c.number_format = '"$"#,##0'
    c.font = green_font if bal >= 0 else red_font

    if total_prev > 0:
        diff_total = ((total_mes - total_prev) / total_prev * 100)
        ws.cell(row=5, column=1, value="Variación vs mes anterior").font = Font(bold=True)
        arrow = "▲" if diff_total > 0 else "▼"
        c = ws.cell(row=5, column=2, value=f"{arrow} {abs(diff_total):.1f}%  (mes anterior: ${total_prev:,})")
        c.font = red_font if diff_total > 0 else green_font

    # Tabla comparativa por categoría
    row = 7
    headers = ["Categoría", month_label(month), month_label(prev), "Diferencia $", "Variación %"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = header_font; cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    todas_cats = sorted(set(list(cats_mes.keys()) + list(cats_prev.keys())))
    for i, cat in enumerate(sorted(todas_cats, key=lambda c: -cats_mes.get(c, 0)), 1):
        r = row + i
        amt_mes = cats_mes.get(cat, 0)
        amt_prev = cats_prev.get(cat, 0)
        diff = amt_mes - amt_prev
        pct = ((amt_mes - amt_prev) / amt_prev * 100) if amt_prev > 0 else None

        ws.cell(row=r, column=1, value=cat)
        c2 = ws.cell(row=r, column=2, value=amt_mes)
        c2.number_format = '"$"#,##0'
        c3 = ws.cell(row=r, column=3, value=amt_prev if amt_prev > 0 else "—")
        if amt_prev > 0: c3.number_format = '"$"#,##0'
        c4 = ws.cell(row=r, column=4, value=diff if amt_prev > 0 else "—")
        if amt_prev > 0:
            c4.number_format = '"$"#,##0'
            c4.font = red_font if diff > 0 else green_font
        if pct is not None:
            arrow = "▲" if pct > 0 else "▼"
            c5 = ws.cell(row=r, column=5, value=f"{arrow} {abs(pct):.1f}%")
            c5.font = red_font if pct > 0 else green_font
        else:
            ws.cell(row=r, column=5, value="Nuevo")

        if i % 2 == 0:
            for col in range(1, 6):
                ws.cell(row=r, column=col).fill = alt_fill

    # Medios de pago
    last_cat_row = row + len(todas_cats) + 2
    ws.cell(row=last_cat_row, column=1, value="Medio de pago").font = Font(bold=True, size=12)
    for j, (medio, amt) in enumerate(sorted(medios.items(), key=lambda x: -x[1]), 1):
        ws.cell(row=last_cat_row+j, column=1, value=medio.capitalize())
        c = ws.cell(row=last_cat_row+j, column=2, value=amt)
        c.number_format = '"$"#,##0'

    # Gráfico comparativo
    if cats_mes and cats_prev:
        chart_row = row
        chart_data_row = last_cat_row + len(medios) + 3

        # Tabla auxiliar para el gráfico
        ws.cell(row=chart_data_row, column=1, value="Categoría")
        ws.cell(row=chart_data_row, column=2, value=month_label(month))
        ws.cell(row=chart_data_row, column=3, value=month_label(prev))
        for i, cat in enumerate(sorted(todas_cats, key=lambda c: -cats_mes.get(c, 0)), 1):
            ws.cell(row=chart_data_row+i, column=1, value=cat)
            ws.cell(row=chart_data_row+i, column=2, value=cats_mes.get(cat, 0))
            ws.cell(row=chart_data_row+i, column=3, value=cats_prev.get(cat, 0))

        chart = BarChart()
        chart.type = "col"
        chart.grouping = "clustered"
        chart.title = f"Comparativa {month_label(month)} vs {month_label(prev)}"
        chart.width = 22; chart.height = 14
        data = Reference(ws, min_col=2, max_col=3, min_row=chart_data_row, max_row=chart_data_row+len(todas_cats))
        cats_ref = Reference(ws, min_col=1, min_row=chart_data_row+1, max_row=chart_data_row+len(todas_cats))
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.series[0].graphicalProperties.solidFill = "7F77DD"
        chart.series[1].graphicalProperties.solidFill = "AAAACC"
        ws.add_chart(chart, f"G{chart_row}")

    # ── Hoja 2: Detalle ──
    ws2 = wb.create_sheet("Detalle")
    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 35
    ws2.column_dimensions["C"].width = 18
    ws2.column_dimensions["D"].width = 10
    ws2.column_dimensions["E"].width = 15
    ws2.column_dimensions["F"].width = 14

    for col, h in enumerate(["Fecha", "Descripción", "Categoría", "Tipo", "Medio", "Monto"], 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font; cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for r_idx, (desc, monto, cat, tipo, medio, fecha) in enumerate(rows, 2):
        ws2.cell(row=r_idx, column=1, value=fecha)
        ws2.cell(row=r_idx, column=2, value=desc)
        ws2.cell(row=r_idx, column=3, value=cat)
        ws2.cell(row=r_idx, column=4, value=tipo.capitalize())
        ws2.cell(row=r_idx, column=5, value=(medio or "efectivo").capitalize())
        c = ws2.cell(row=r_idx, column=6, value=int(monto))
        c.number_format = '"$"#,##0'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

# ── IA ─────────────────────────────────────────────────────────────────────────

def parse_with_ai(text: str) -> dict:
    import time
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Sos un asistente de registro de gastos e ingresos en español argentino.
Analizá el mensaje y respondé SOLO con JSON válido sin markdown ni texto extra:
{{"items":[{{"tipo":"gasto|ingreso","descripcion":"...","monto":1234,"categoria":"...","medio_pago":"efectivo|debito|credito","fecha":"YYYY-MM-DD"}}],"respuesta":"mensaje amigable en texto plano"}}
Categorías de gastos:
- Corolla: nafta, seguro, reparaciones, service del Toyota Corolla
- Focus: nafta, seguro, reparaciones, service del Ford Focus
- Peajes: peajes (aplica a ambos autos)
- Deportes: club, cuota club, estacionamiento club, 3er tiempo, after partido
- Salud: médico, farmacia, obra social, consultas médicas (NO gym)
- Gym: gym, gimnasio, entrenamiento personal
- Restaurante: salidas a comer, restaurant, parrilla, cena afuera
- Delivery: pedidosya, rappi, delivery, comida a domicilio
- Alimentación: supermercado, almacén, verdulería
- Entretenimiento: cine, streaming, salidas, juegos, Netflix, Spotify
- Hogar: alquiler, expensas, servicios, electricidad, gas, agua
- Limpieza: limpieza del hogar, productos de limpieza, mucama, empleada
- Mantenimiento: reparaciones del hogar, plomero, electricista, pintura, materiales
- Ropa: indumentaria, calzado, accesorios
- Educación: cursos, libros, colegios, universidad
- Tecnología: electrónica, apps, software, celulares
- Ahorros: ahorro, plazo fijo, inversión, dólares
- Otros: todo lo que no entra en las anteriores.
Para ingresos: categoria Ingreso, medio_pago transferencia.
medio_pago: crédito/cuotas→credito, débito→debito, efectivo/no aclara→efectivo.
Montos enteros sin decimales. Fecha hoy: {today}. Sin emojis.
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

MEDIO_EMOJI = {"efectivo": "💵", "debito": "💳", "credito": "💎", "transferencia": "🏦"}

# ── Envíos ─────────────────────────────────────────────────────────────────────

async def enviar_excel_semanal(user_id, desde, hasta, bot):
    buf = generar_excel_semanal(user_id, desde, hasta)
    if buf is None:
        await bot.send_message(chat_id=user_id, text="No hay gastos registrados esta semana.")
        return
    await bot.send_document(
        chat_id=user_id,
        document=buf,
        filename=f"gastos_semana_{desde}_{hasta}.xlsx",
        caption=f"Reporte semanal {desde} al {hasta}"
    )

async def enviar_excel_mensual(user_id, month, bot):
    buf = generar_excel_mensual(user_id, month)
    if buf is None:
        await bot.send_message(chat_id=user_id, text=f"No hay gastos en {month_label(month)}.")
        return
    await bot.send_document(
        chat_id=user_id,
        document=buf,
        filename=f"gastos_{month}.xlsx",
        caption=f"Resumen mensual {month_label(month)} con comparativa vs mes anterior"
    )

# ── Jobs automáticos ───────────────────────────────────────────────────────────

async def job_reporte_semanal(context):
    now = datetime.now()
    lunes = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    domingo = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for uid in get_all_users():
        try:
            await enviar_excel_semanal(uid, lunes, domingo, context.bot)
        except Exception as e:
            logger.error(f"Error reporte semanal {uid}: {e}")

async def job_reporte_mensual(context):
    now = datetime.now()
    # Corre el último día del mes → reporta ese mes
    month = now.strftime("%Y-%m")
    for uid in get_all_users():
        try:
            await enviar_excel_mensual(uid, month, context.bot)
        except Exception as e:
            logger.error(f"Error reporte mensual {uid}: {e}")

# ── Comandos ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user.username or str(uid))
    kb = [["/resumen", "/detalle"], ["/reporte", "/reportemes"], ["/ayuda"]]
    await update.message.reply_text(
        "Hola! Soy tu asistente de gastos.\n\n"
        "Ejemplos:\n"
        "- \"Gaste $5000 en el super\"\n"
        "- \"Pague $3000 de nafta con debito\"\n"
        "- \"Compre ropa $8000 en cuotas\"\n"
        "- \"Cobre $80000 de sueldo\"\n\n"
        "Comandos: /resumen /detalle /reporte /reportemes /ayuda",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now()
    month = now.strftime("%Y-%m")
    prev = prev_month_key(month)

    gastos_mes = get_month_total(uid, month, "gasto")
    ingresos_mes = get_month_total(uid, month, "ingreso")
    gastos_prev = get_month_total(uid, prev, "gasto")
    balance = ingresos_mes - gastos_mes
    cats_mes = get_cat_totals(uid, month)
    cats_prev = get_cat_totals(uid, prev)
    medios = get_medio_totals(uid, month)

    lines = [f"Resumen {month_label(month)}", ""]
    lines.append(f"Ingresos:  ${ingresos_mes:,}")
    lines.append(f"Gastos:    ${gastos_mes:,}")
    lines.append(f"Balance:   {'+'if balance>=0 else ''}${balance:,}")

    if gastos_prev > 0:
        diff = ((gastos_mes - gastos_prev) / gastos_prev * 100)
        arrow = "↑" if diff > 0 else "↓"
        lines.append(f"{arrow} {abs(diff):.1f}% vs {month_label(prev)} (${gastos_prev:,})")

    if medios:
        lines.append("\nMedio de pago:")
        for medio, amt in sorted(medios.items(), key=lambda x: -x[1]):
            lines.append(f"  {MEDIO_EMOJI.get(medio,'💰')} {medio.capitalize()}: ${amt:,}")

    if cats_mes:
        lines.append("\nPor categoría:")
        todas = sorted(set(list(cats_mes.keys()) + list(cats_prev.keys())), key=lambda c: -cats_mes.get(c, 0))
        for cat in todas:
            amt = cats_mes.get(cat, 0)
            if amt == 0:
                continue
            prev_amt = cats_prev.get(cat, 0)
            pct = int(amt / gastos_mes * 100) if gastos_mes > 0 else 0
            if prev_amt > 0:
                diff = ((amt - prev_amt) / prev_amt * 100)
                arrow = "↑" if diff > 0 else "↓"
                lines.append(f"  {cat}: ${amt:,} ({pct}%)  {arrow}{abs(diff):.0f}%")
            else:
                lines.append(f"  {cat}: ${amt:,} ({pct}%)")

    await update.message.reply_text("\n".join(lines))

async def cmd_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    recent = get_recent(uid, 10)
    if not recent:
        await update.message.reply_text("No hay registros aun.")
        return
    lines = ["Ultimos registros:", ""]
    for desc, monto, cat, tipo, medio, fecha in recent:
        prefix = "+" if tipo == "ingreso" else "-"
        lines.append(f"{fecha}  {prefix}${int(monto):,}  {desc}\n  {cat} · {MEDIO_EMOJI.get(medio,'💰')} {medio}")
    await update.message.reply_text("\n".join(lines))

async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now()
    lunes = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
    domingo = (now - timedelta(days=now.weekday() + 1)).strftime("%Y-%m-%d")
    await update.message.reply_text("Generando reporte semanal...")
    await enviar_excel_semanal(uid, lunes, domingo, context.bot)

async def cmd_reportemes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Si pasaron args, usar ese mes (ej: /reportemes 2025-04)
    args = context.args
    if args and len(args[0]) == 7:
        month = args[0]
    else:
        month = datetime.now().strftime("%Y-%m")
    await update.message.reply_text(f"Generando reporte de {month_label(month)}...")
    await enviar_excel_mensual(uid, month, context.bot)

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Como registrar gastos e ingresos:\n\n"
        "GASTOS:\n"
        "- \"Gaste $5000 en pizza\"\n"
        "- \"Pague el super $3200 con debito\"\n"
        "- \"Compre ropa $8000 en cuotas\"\n\n"
        "INGRESOS:\n"
        "- \"Cobre el sueldo $85000\"\n\n"
        "MEDIOS: 💵 Efectivo  💳 Debito  💎 Credito\n\n"
        "COMANDOS:\n"
        "/resumen - resumen del mes con comparativa por categoria\n"
        "/detalle - ultimos 10 registros\n"
        "/reporte - Excel de la semana pasada\n"
        "/reportemes - Excel del mes actual con comparativa\n"
        "/reportemes 2025-04 - Excel de un mes especifico\n"
        "/ayuda - este mensaje\n\n"
        "Automatico:\n"
        "- Cada lunes recibis el Excel semanal\n"
        "- El ultimo dia del mes recibis el resumen mensual"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user.username or str(uid))
    text = update.message.text
    try:
        parsed = parse_with_ai(text)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")
        return
    for item in parsed.get("items", []):
        save_item(uid, item.get("descripcion","Sin descripcion"), int(float(item.get("monto",0))),
                  item.get("categoria","Otros"), item.get("tipo","gasto"),
                  item.get("medio_pago","efectivo"), item.get("fecha", datetime.now().strftime("%Y-%m-%d")))
    await update.message.reply_text(parsed.get("respuesta","Registrado."))

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import calendar
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("detalle", cmd_detalle))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("reportemes", cmd_reportemes))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    jq = app.job_queue
    now = datetime.now()

    # Job semanal: cada lunes 9am
    days_to_monday = (7 - now.weekday()) % 7 or 7
    next_monday = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_to_monday)
    jq.run_repeating(job_reporte_semanal, interval=604800, first=(next_monday - now).total_seconds())

    # Job mensual: cada día a las 23:55, solo ejecuta si es el último día del mes
    async def job_check_fin_mes(context):
        n = datetime.now()
        ultimo = calendar.monthrange(n.year, n.month)[1]
        if n.day == ultimo:
            await job_reporte_mensual(context)

    jq.run_repeating(job_check_fin_mes, interval=86400, first=3600)

    logger.info("Bot iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()
