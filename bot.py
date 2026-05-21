import os
import json
import logging
import calendar
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash")

# ── Base de datos ──────────────────────────────────────────────────────────────

def get_con():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    con = get_con()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            descripcion TEXT,
            monto INTEGER,
            categoria TEXT,
            tipo TEXT DEFAULT 'gasto',
            medio_pago TEXT DEFAULT 'efectivo',
            fecha DATE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            created_at DATE
        )
    """)
    con.commit()
    cur.close()
    con.close()
    logger.info("Base de datos inicializada.")

def register_user(user_id, username):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO usuarios (user_id, username, created_at) VALUES (%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, username, datetime.now().date())
    )
    con.commit()
    cur.close(); con.close()

def get_all_users():
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM usuarios")
    rows = [r[0] for r in cur.fetchall()]
    cur.close(); con.close()
    return rows

def save_item(user_id, descripcion, monto, categoria, tipo, medio_pago, fecha):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO gastos (user_id, descripcion, monto, categoria, tipo, medio_pago, fecha) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (user_id, descripcion, monto, categoria, tipo, medio_pago, fecha)
    )
    con.commit()
    cur.close(); con.close()

def get_month_total(user_id, month, tipo="gasto"):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND to_char(fecha,'YYYY-MM')=%s AND tipo=%s",
        (user_id, month, tipo)
    )
    total = cur.fetchone()[0]
    cur.close(); con.close()
    return int(total)

def get_cat_totals(user_id, month):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT categoria, SUM(monto) FROM gastos WHERE user_id=%s AND to_char(fecha,'YYYY-MM')=%s AND tipo='gasto' GROUP BY categoria",
        (user_id, month)
    )
    rows = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.close(); con.close()
    return rows

def get_medio_totals(user_id, month):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT medio_pago, SUM(monto) FROM gastos WHERE user_id=%s AND to_char(fecha,'YYYY-MM')=%s AND tipo='gasto' GROUP BY medio_pago",
        (user_id, month)
    )
    rows = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.close(); con.close()
    return rows

def get_recent(user_id, limit=10):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT descripcion, monto, categoria, tipo, medio_pago, fecha FROM gastos WHERE user_id=%s ORDER BY fecha DESC, id DESC LIMIT %s",
        (user_id, limit)
    )
    rows = cur.fetchall()
    cur.close(); con.close()
    return rows

def get_gastos_rango(user_id, desde, hasta):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT descripcion, monto, categoria, tipo, medio_pago, fecha FROM gastos WHERE user_id=%s AND fecha>=%s AND fecha<=%s ORDER BY fecha DESC",
        (user_id, desde, hasta)
    )
    rows = cur.fetchall()
    cur.close(); con.close()
    return rows

def borrar_mes(user_id, month):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM gastos WHERE user_id=%s AND to_char(fecha,'YYYY-MM')=%s",
        (user_id, month)
    )
    deleted = cur.rowcount
    con.commit()
    cur.close(); con.close()
    return deleted

def prev_month_key(month):
    y, m = month.split("-")
    first = datetime(int(y), int(m), 1)
    prev = first - timedelta(days=1)
    return prev.strftime("%Y-%m")

def month_label(month):
    y, m = month.split("-")
    names = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    return f"{names[int(m)-1]} {y}"

# ── IA ─────────────────────────────────────────────────────────────────────────

def parse_with_ai(text):
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
- Otros: todo lo que no entra en las anteriores

Para ingresos: categoria Ingreso, medio_pago transferencia.
medio_pago: crédito/cuotas→credito, débito→debito, efectivo/no aclara→efectivo.
Montos enteros sin decimales. Fecha hoy: {today}. Sin emojis.
Mensaje: {text}"""

    for attempt in range(3):
        try:
            response = gemini.generate_content(prompt)
            raw = response.text.strip().replace("```json","").replace("```","").strip()
            return json.loads(raw)
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise

MEDIO_EMOJI = {"efectivo":"💵","debito":"💳","credito":"💎","transferencia":"🏦"}

# ── Excel ──────────────────────────────────────────────────────────────────────

def generar_excel_semanal(user_id, desde, hasta):
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.chart import BarChart, Reference
    from openpyxl.utils import get_column_letter

    rows = get_gastos_rango(user_id, desde, hasta)
    if not rows: return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detalle"
    hf = PatternFill("solid", fgColor="1A1A2E")
    hfont = Font(color="FFFFFF", bold=True)
    alt = PatternFill("solid", fgColor="F5F5F5")

    headers = ["Fecha","Descripción","Categoría","Tipo","Medio","Monto"]
    widths  = [12, 35, 18, 10, 15, 14]
    for i,(h,w) in enumerate(zip(headers,widths),1):
        c = ws.cell(row=1,column=i,value=h)
        c.font=hfont; c.fill=hf; c.alignment=Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width=w

    tg=ti=0
    for ri,(desc,monto,cat,tipo,medio,fecha) in enumerate(rows,2):
        ws.cell(row=ri,column=1,value=str(fecha))
        ws.cell(row=ri,column=2,value=desc)
        ws.cell(row=ri,column=3,value=cat)
        ws.cell(row=ri,column=4,value=tipo.capitalize())
        ws.cell(row=ri,column=5,value=(medio or "efectivo").capitalize())
        c=ws.cell(row=ri,column=6,value=int(monto)); c.number_format='"$"#,##0'
        if ri%2==0:
            for col in range(1,7): ws.cell(row=ri,column=col).fill=alt
        if tipo=="gasto": tg+=int(monto)
        else: ti+=int(monto)

    last=len(rows)+2
    for label,val,color in [("TOTAL GASTOS",tg,"C0392B"),("TOTAL INGRESOS",ti,"1D9E75"),("BALANCE",ti-tg,"1D9E75" if ti>=tg else "C0392B")]:
        ws.cell(row=last,column=5,value=label).font=Font(bold=True)
        c=ws.cell(row=last,column=6,value=val); c.font=Font(bold=True,color=color); c.number_format='"$"#,##0'
        last+=1

    ws2=wb.create_sheet("Resumen")
    ws2.column_dimensions["A"].width=20; ws2.column_dimensions["B"].width=16
    by_cat={}
    for _,monto,cat,tipo,_,_ in rows:
        if tipo=="gasto": by_cat[cat]=by_cat.get(cat,0)+int(monto)
    for i,(h) in enumerate(["Categoría","Monto"],1):
        c=ws2.cell(row=1,column=i,value=h); c.font=hfont; c.fill=hf
    for i,(cat,amt) in enumerate(sorted(by_cat.items(),key=lambda x:-x[1]),2):
        ws2.cell(row=i,column=1,value=cat)
        c=ws2.cell(row=i,column=2,value=amt); c.number_format='"$"#,##0'

    if by_cat:
        chart=BarChart(); chart.type="col"; chart.title="Gastos por Categoría"; chart.width=18; chart.height=12
        data=Reference(ws2,min_col=2,min_row=1,max_row=len(by_cat)+1)
        cats=Reference(ws2,min_col=1,min_row=2,max_row=len(by_cat)+1)
        chart.add_data(data,titles_from_data=True); chart.set_categories(cats)
        chart.series[0].graphicalProperties.solidFill="7F77DD"
        ws2.add_chart(chart,f"A{len(by_cat)+4}")

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf

def generar_excel_mensual(user_id, month):
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.chart import BarChart, Reference
    from openpyxl.utils import get_column_letter

    prev = prev_month_key(month)
    y,m = month.split("-")
    ultimo = calendar.monthrange(int(y),int(m))[1]
    desde=f"{month}-01"; hasta=f"{month}-{ultimo:02d}"
    rows=get_gastos_rango(user_id,desde,hasta)
    if not rows: return None

    cats_mes=get_cat_totals(user_id,month)
    cats_prev=get_cat_totals(user_id,prev)
    total_mes=get_month_total(user_id,month,"gasto")
    total_prev=get_month_total(user_id,prev,"gasto")
    ingresos_mes=get_month_total(user_id,month,"ingreso")
    medios=get_medio_totals(user_id,month)

    wb=openpyxl.Workbook()
    ws=wb.active; ws.title="Resumen"
    hf=PatternFill("solid",fgColor="1A1A2E")
    hfont=Font(color="FFFFFF",bold=True)
    gfont=Font(color="1D9E75",bold=True)
    rfont=Font(color="C0392B",bold=True)
    alt=PatternFill("solid",fgColor="F0F0F8")

    for col,w in zip(["A","B","C","D","E"],[22,16,16,16,14]):
        ws.column_dimensions[col].width=w

    ws.merge_cells("A1:E1")
    ws["A1"].value=f"Resumen {month_label(month)}"
    ws["A1"].font=Font(bold=True,size=14); ws["A1"].alignment=Alignment(horizontal="center")

    ws.cell(row=2,column=1,value="Ingresos del mes").font=Font(bold=True)
    c=ws.cell(row=2,column=2,value=ingresos_mes); c.number_format='"$"#,##0'; c.font=gfont
    ws.cell(row=3,column=1,value="Total gastos").font=Font(bold=True)
    c=ws.cell(row=3,column=2,value=total_mes); c.number_format='"$"#,##0'; c.font=rfont
    bal=ingresos_mes-total_mes
    ws.cell(row=4,column=1,value="Balance").font=Font(bold=True)
    c=ws.cell(row=4,column=2,value=bal); c.number_format='"$"#,##0'; c.font=gfont if bal>=0 else rfont

    if total_prev>0:
        diff=((total_mes-total_prev)/total_prev*100)
        ws.cell(row=5,column=1,value="Variación vs mes anterior").font=Font(bold=True)
        arrow="▲" if diff>0 else "▼"
        c=ws.cell(row=5,column=2,value=f"{arrow} {abs(diff):.1f}%  (ant: ${total_prev:,})")
        c.font=rfont if diff>0 else gfont

    row=7
    for col,h in enumerate(["Categoría",month_label(month),month_label(prev),"Diferencia $","Variación %"],1):
        cell=ws.cell(row=row,column=col,value=h)
        cell.font=hfont; cell.fill=hf; cell.alignment=Alignment(horizontal="center")

    todas=sorted(set(list(cats_mes.keys())+list(cats_prev.keys())),key=lambda c:-cats_mes.get(c,0))
    for i,cat in enumerate(todas,1):
        r=row+i
        amt=cats_mes.get(cat,0); prev_amt=cats_prev.get(cat,0); diff=amt-prev_amt
        ws.cell(row=r,column=1,value=cat)
        c2=ws.cell(row=r,column=2,value=amt); c2.number_format='"$"#,##0'
        c3=ws.cell(row=r,column=3,value=prev_amt if prev_amt>0 else "—")
        if prev_amt>0: c3.number_format='"$"#,##0'
        c4=ws.cell(row=r,column=4,value=diff if prev_amt>0 else "—")
        if prev_amt>0:
            c4.number_format='"$"#,##0'; c4.font=rfont if diff>0 else gfont
        if prev_amt>0:
            pct=((amt-prev_amt)/prev_amt*100)
            arrow="▲" if pct>0 else "▼"
            c5=ws.cell(row=r,column=5,value=f"{arrow} {abs(pct):.1f}%")
            c5.font=rfont if pct>0 else gfont
        else:
            ws.cell(row=r,column=5,value="Nuevo")
        if i%2==0:
            for col in range(1,6): ws.cell(row=r,column=col).fill=alt

    last_row=row+len(todas)+2
    ws.cell(row=last_row,column=1,value="Medio de pago").font=Font(bold=True,size=12)
    for j,(medio,amt) in enumerate(sorted(medios.items(),key=lambda x:-x[1]),1):
        ws.cell(row=last_row+j,column=1,value=medio.capitalize())
        c=ws.cell(row=last_row+j,column=2,value=amt); c.number_format='"$"#,##0'

    # Gráfico comparativo
    cdr=last_row+len(medios)+3
    ws.cell(row=cdr,column=1,value="Categoría")
    ws.cell(row=cdr,column=2,value=month_label(month))
    ws.cell(row=cdr,column=3,value=month_label(prev))
    for i,cat in enumerate(sorted(todas,key=lambda c:-cats_mes.get(c,0)),1):
        ws.cell(row=cdr+i,column=1,value=cat)
        ws.cell(row=cdr+i,column=2,value=cats_mes.get(cat,0))
        ws.cell(row=cdr+i,column=3,value=cats_prev.get(cat,0))
    chart=BarChart(); chart.type="col"; chart.grouping="clustered"
    chart.title=f"Comparativa {month_label(month)} vs {month_label(prev)}"
    chart.width=22; chart.height=14
    data=Reference(ws,min_col=2,max_col=3,min_row=cdr,max_row=cdr+len(todas))
    cats_ref=Reference(ws,min_col=1,min_row=cdr+1,max_row=cdr+len(todas))
    chart.add_data(data,titles_from_data=True); chart.set_categories(cats_ref)
    chart.series[0].graphicalProperties.solidFill="7F77DD"
    chart.series[1].graphicalProperties.solidFill="AAAACC"
    ws.add_chart(chart,"G7")

    # Hoja detalle
    ws2=wb.create_sheet("Detalle")
    for col,w in zip(["A","B","C","D","E","F"],[12,35,18,10,15,14]):
        ws2.column_dimensions[col].width=w
    for i,h in enumerate(["Fecha","Descripción","Categoría","Tipo","Medio","Monto"],1):
        c=ws2.cell(row=1,column=i,value=h); c.font=hfont; c.fill=hf
    for ri,(desc,monto,cat,tipo,medio,fecha) in enumerate(rows,2):
        ws2.cell(row=ri,column=1,value=str(fecha))
        ws2.cell(row=ri,column=2,value=desc)
        ws2.cell(row=ri,column=3,value=cat)
        ws2.cell(row=ri,column=4,value=tipo.capitalize())
        ws2.cell(row=ri,column=5,value=(medio or "efectivo").capitalize())
        c=ws2.cell(row=ri,column=6,value=int(monto)); c.number_format='"$"#,##0'

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf

# ── Envíos ─────────────────────────────────────────────────────────────────────

async def enviar_excel_semanal(user_id, desde, hasta, bot):
    buf=generar_excel_semanal(user_id,desde,hasta)
    if not buf:
        await bot.send_message(chat_id=user_id,text="No hay gastos registrados esta semana.")
        return
    await bot.send_document(chat_id=user_id,document=buf,filename=f"gastos_semana_{desde}_{hasta}.xlsx",caption=f"Reporte semanal {desde} al {hasta}")

async def enviar_excel_mensual(user_id, month, bot):
    buf=generar_excel_mensual(user_id,month)
    if not buf:
        await bot.send_message(chat_id=user_id,text=f"No hay gastos en {month_label(month)}.")
        return
    await bot.send_document(chat_id=user_id,document=buf,filename=f"gastos_{month}.xlsx",caption=f"Resumen mensual {month_label(month)} con comparativa")

# ── Jobs ───────────────────────────────────────────────────────────────────────

async def job_semanal(context):
    now=datetime.now()
    lunes=(now-timedelta(days=7)).strftime("%Y-%m-%d")
    domingo=(now-timedelta(days=1)).strftime("%Y-%m-%d")
    for uid in get_all_users():
        try: await enviar_excel_semanal(uid,lunes,domingo,context.bot)
        except Exception as e: logger.error(f"Error semanal {uid}: {e}")

async def job_check_fin_mes(context):
    now=datetime.now()
    ultimo=calendar.monthrange(now.year,now.month)[1]
    if now.day==ultimo:
        month=now.strftime("%Y-%m")
        for uid in get_all_users():
            try: await enviar_excel_mensual(uid,month,context.bot)
            except Exception as e: logger.error(f"Error mensual {uid}: {e}")

# ── Comandos ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    register_user(uid,update.effective_user.username or str(uid))
    kb=[["/resumen","/detalle"],["/reporte","/reportemes"],["/ayuda"]]
    await update.message.reply_text(
        "Hola! Soy tu asistente de gastos.\n\n"
        "Ejemplos:\n"
        "- \"Cargue nafta al Corolla $8000\"\n"
        "- \"Pague el seguro del Focus con debito $15000\"\n"
        "- \"Cene en un restaurante $5000 con credito\"\n"
        "- \"Pedi delivery $2500\"\n"
        "- \"Cobre el sueldo $85000\"\n\n"
        "Comandos: /resumen /detalle /reporte /reportemes /ayuda",
        reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
    )

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    now=datetime.now()
    month=now.strftime("%Y-%m")
    prev=prev_month_key(month)
    gastos=get_month_total(uid,month,"gasto")
    ingresos=get_month_total(uid,month,"ingreso")
    gastos_prev=get_month_total(uid,prev,"gasto")
    balance=ingresos-gastos
    cats=get_cat_totals(uid,month)
    cats_p=get_cat_totals(uid,prev)
    medios=get_medio_totals(uid,month)

    lines=[f"Resumen {month_label(month)}",""]
    lines.append(f"Ingresos:  ${ingresos:,}")
    lines.append(f"Gastos:    ${gastos:,}")
    lines.append(f"Balance:   {'+'if balance>=0 else ''}${balance:,}")
    if gastos_prev>0:
        diff=((gastos-gastos_prev)/gastos_prev*100)
        lines.append(f"{'↑'if diff>0 else '↓'} {abs(diff):.1f}% vs {month_label(prev)} (${gastos_prev:,})")

    if medios:
        lines.append("\nMedio de pago:")
        for medio,amt in sorted(medios.items(),key=lambda x:-x[1]):
            lines.append(f"  {MEDIO_EMOJI.get(medio,'💰')} {medio.capitalize()}: ${amt:,}")

    if cats:
        lines.append("\nPor categoría:")
        for cat,amt in sorted(cats.items(),key=lambda x:-x[1]):
            pct=int(amt/gastos*100) if gastos>0 else 0
            prev_amt=cats_p.get(cat,0)
            if prev_amt>0:
                diff=((amt-prev_amt)/prev_amt*100)
                lines.append(f"  {cat}: ${amt:,} ({pct}%)  {'↑'if diff>0 else '↓'}{abs(diff):.0f}%")
            else:
                lines.append(f"  {cat}: ${amt:,} ({pct}%)")

    await update.message.reply_text("\n".join(lines))

async def cmd_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    rows=get_recent(uid,10)
    if not rows:
        await update.message.reply_text("No hay registros aun.")
        return
    lines=["Ultimos registros:",""]
    for desc,monto,cat,tipo,medio,fecha in rows:
        prefix="+" if tipo=="ingreso" else "-"
        lines.append(f"{fecha}  {prefix}${int(monto):,}  {desc}\n  {cat} · {MEDIO_EMOJI.get(medio,'💰')} {medio}")
    await update.message.reply_text("\n".join(lines))

async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    now=datetime.now()
    lunes=(now-timedelta(days=now.weekday()+7)).strftime("%Y-%m-%d")
    domingo=(now-timedelta(days=now.weekday()+1)).strftime("%Y-%m-%d")
    await update.message.reply_text("Generando reporte semanal...")
    await enviar_excel_semanal(uid,lunes,domingo,context.bot)

async def cmd_reportemes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    args=context.args
    month=args[0] if args and len(args[0])==7 else datetime.now().strftime("%Y-%m")
    await update.message.reply_text(f"Generando reporte de {month_label(month)}...")
    await enviar_excel_mensual(uid,month,context.bot)

async def cmd_borrarmes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args
    month = args[0] if args and len(args[0]) == 7 else datetime.now().strftime("%Y-%m")
    total = query_one_local(uid, month)
    if total == 0:
        await update.message.reply_text(f"No hay gastos registrados en {month_label(month)}.")
        return
    # Guardar en context para confirmar
    context.user_data["borrar_month"] = month
    context.user_data["borrar_total"] = total
    await update.message.reply_text(
        f"ATENCION: Estas por borrar TODOS los registros de {month_label(month)}.

"
        f"Total de registros: {total}

"
        f"Respondé SI para confirmar o NO para cancelar."
    )

def query_one_local(user_id, month):
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM gastos WHERE user_id=%s AND to_char(fecha,'YYYY-MM')=%s", (user_id, month))
    count = cur.fetchone()[0]
    cur.close(); con.close()
    return count

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Como registrar gastos:\n\n"
        "AUTOS:\n"
        "- \"Cargue nafta al Corolla $8000\"\n"
        "- \"Seguro del Focus $15000 con debito\"\n"
        "- \"Pague peaje $500\"\n\n"
        "COMIDAS:\n"
        "- \"Cene afuera $5000 con credito\" → Restaurante\n"
        "- \"Pedi delivery $2500\" → Delivery\n"
        "- \"Fui al super $12000\" → Alimentación\n\n"
        "DEPORTES:\n"
        "- \"Pague la cuota del club $8000\"\n"
        "- \"3er tiempo después del partido $3000\"\n\n"
        "INGRESOS:\n"
        "- \"Cobre el sueldo $85000\"\n\n"
        "MEDIOS: 💵 Efectivo  💳 Debito  💎 Credito\n\n"
        "COMANDOS:\n"
        "/resumen - resumen del mes con comparativa\n"
        "/detalle - ultimos 10 registros\n"
        "/reporte - Excel semanal\n"
        "/reportemes - Excel mensual con comparativa\n"
        "/borrarmes - Borrar todos los registros del mes\n"
        "/reportemes 2025-04 - mes específico\n\n"
        "Automatico:\n"
        "- Cada lunes: Excel de la semana\n"
        "- Ultimo dia del mes: Excel mensual completo"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    register_user(uid,update.effective_user.username or str(uid))
    text=update.message.text

    # Confirmar borrado
    if context.user_data.get("borrar_month"):
        month = context.user_data["borrar_month"]
        if text.strip().upper() == "SI":
            deleted = borrar_mes(uid, month)
            del context.user_data["borrar_month"]
            del context.user_data["borrar_total"]
            await update.message.reply_text(f"✅ Se borraron {deleted} registros de {month_label(month)}.")
            return
        elif text.strip().upper() == "NO":
            del context.user_data["borrar_month"]
            del context.user_data["borrar_total"]
            await update.message.reply_text("Cancelado. No se borró nada.")
            return

    try:
        parsed=parse_with_ai(text)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")
        return
    for item in parsed.get("items",[]):
        save_item(uid,item.get("descripcion","Sin descripcion"),
                  int(float(item.get("monto",0))),
                  item.get("categoria","Otros"),
                  item.get("tipo","gasto"),
                  item.get("medio_pago","efectivo"),
                  item.get("fecha",datetime.now().strftime("%Y-%m-%d")))
    await update.message.reply_text(parsed.get("respuesta","Registrado."))

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("resumen",cmd_resumen))
    app.add_handler(CommandHandler("detalle",cmd_detalle))
    app.add_handler(CommandHandler("reporte",cmd_reporte))
    app.add_handler(CommandHandler("reportemes",cmd_reportemes))
    app.add_handler(CommandHandler("borrarmes",cmd_borrarmes))
    app.add_handler(CommandHandler("ayuda",cmd_ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))

    jq=app.job_queue
    now=datetime.now()
    days_to_monday=(7-now.weekday())%7 or 7
    next_monday=now.replace(hour=9,minute=0,second=0,microsecond=0)+timedelta(days=days_to_monday)
    jq.run_repeating(job_semanal,interval=604800,first=(next_monday-now).total_seconds())
    jq.run_repeating(job_check_fin_mes,interval=86400,first=3600)

    logger.info("Bot iniciado con Supabase.")
    app.run_polling()

if __name__=="__main__":
    main()
