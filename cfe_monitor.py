# cfe_monitor.py
import os
import json
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright

# ---------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------
CFE_URL = "https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/"
ESTADO_OBJETIVO = "Baja California"
STATE_FILE = "cfe_state.json"

# Horario laboral (hora de Tijuana) y número máximo de ejecuciones/día
WORK_START = 9   # 09:00
WORK_END   = 19  # 19:00
RUNS_PER_DAY = 4

# Telegram (coloca estos valores como "Secrets" en GitHub)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------
# UTILIDADES
# ---------------------------
def now_tijuana():
    return datetime.now(ZoneInfo("America/Tijuana"))

def within_business_hours():
    t = now_tijuana()
    return WORK_START <= t.hour < WORK_END

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados. Mensaje NO enviado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        if r.status_code != 200:
            print(f"[ERROR] Telegram status {r.status_code}: {r.text}")
        else:
            print("[OK] Mensaje enviado a Telegram")
    except Exception as e:
        print(f"[ERROR] Envío a Telegram: {e}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_state(data: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------------------------
# SCRAPING CON PLAYWRIGHT
# ---------------------------
async def scrape_listings():
    """
    Devuelve un diccionario {numero_procedimiento: datos} con, al menos:
      - numero
      - descripcion
      - fecha_publicacion
      - estado (si aparece)
      - adjudicado_a (si aparece)
      - monto_adjudicado (si aparece)
    Nota: El portal puede cambiar. Este scraper usa selectores robustos por texto y fallback.
    """
    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1) Ir al sitio
        await page.goto(CFE_URL, wait_until="domcontentloaded")
        # Algunos sitios ASP.NET cargan dinámico: esperemos unos segundos extra
        await page.wait_for_timeout(3000)

        # 2) Seleccionar entidad federativa "Baja California"
        # Intenta localizar el <select> por etiqueta o nombre común
        # Ajustable si el sitio cambia: inspecciona y actualiza el selector.
        select_locator_candidates = [
            "select:has(option:text('Baja California'))",
            "select[formcontrolname*='Entidad'], select[name*='Entidad'], select[id*='Entidad']",
            "select"
        ]
        select = None
        for cand in select_locator_candidates:
            loc = page.locator(cand)
            if await loc.count():
                # Verifica que la opción exista
                try:
                    await loc.select_option(label=ESTADO_OBJETIVO)
                    select = loc
                    break
                except:
                    pass

        if not select:
            # Otra estrategia: intentar hacer click en un combobox tipo Angular/PrimeNG
            # Buscamos un componente que se despliega y filtra por texto
            try:
                # Abre el combo
                await page.get_by_text("Entidad", exact=False).click(timeout=2000)
                await page.get_by_role("option", name=ESTADO_OBJETIVO, exact=True).click(timeout=2000)
            except:
                print("[WARN] No se pudo seleccionar el estado por los selectores comunes. Revisa el selector en el código.")
        
        # 3) Dar click en "Buscar"/"Consultar"
        # Tratamos nombres típicos de botones
        for button_text in ["Buscar", "Consultar", "Filtrar", "Aceptar"]:
            btn = page.get_by_role("button", name=button_text, exact=False)
            if await btn.count():
                await btn.first.click()
                break

        # Espera carga de tabla
        await page.wait_for_timeout(3000)

        # 4) Recorremos las páginas de resultados
        while True:
            # Ubicar tabla
            table = page.locator("table")
            if not await table.count():
                # A veces la tabla es un <div role="table">
                table = page.locator("[role='table']")
            
            if not await table.count():
                print("[WARN] No se encontró tabla en la página actual.")
            else:
                # Parsear filas (excluyendo encabezado)
                rows = table.locator("tr")
                nrows = await rows.count()
                for i in range(nrows):
                    row = rows.nth(i)
                    # Filtra encabezados
                    tag = await row.evaluate("(el) => el.tagName.toLowerCase()")
                    cls = (await row.get_attribute("class")) or ""
                    if "header" in cls.lower() or tag == "thead":
                        continue

                    cells = row.locator("td")
                    if not await cells.count():
                        continue

                    # Por lo general las columnas incluyen: número de procedimiento, descripción, fecha, y un enlace a detalle
                    # Intentamos mapeo por índice flexible:
                    text_cells = []
                    for c in range(await cells.count()):
                        text_cells.append((await cells.nth(c).inner_text()).strip())

                    # Heurística: buscar número de procedimiento por un patrón típico (por ej. "CFE-...")
                    numero = ""
                    for t in text_cells:
                        if "CFE-" in t or "L0" in t or "OM-" in t or "-" in t:
                            # Toma la primera "celda con guiones" como candidato
                            numero = t.split("\n")[0].strip()
                            break

                    # Descripción = la celda con más texto
                    descripcion = max(text_cells, key=len).replace("\n", " ").strip() if text_cells else ""

                    # Fecha probable: busca algo con "/" o formato dd/mm/aaaa
                    fecha_publicacion = ""
                    for t in text_cells:
                        if "/" in t or "-" in t:
                            # muy genérico; nos quedamos con el primer match que parezca fecha corta
                            if any(m in t for m in ["/202", "-202"]):
                                fecha_publicacion = t.strip()
                                break

                    # Intentar entrar al detalle (si la fila tiene un link)
                    estado = ""
                    adjudicado_a = ""
                    monto_adjudicado = ""

                    link = cells.locator("a")
                    if await link.count():
                        with context.expect_page() as new_page_info:
                            try:
                                await link.first.click()
                            except:
                                # si abre en la misma pestaña
                                new_page_info = None

                        detail_page = None
                        if new_page_info:
                            detail_page = await new_page_info.value
                        else:
                            # Puede haber navegado en la misma página
                            detail_page = page

                        # Espera a que cargue el detalle
                        await detail_page.wait_for_timeout(1500)

                        # Extrae pares campo:valor por etiquetas comunes
                        # Buscamos con contains para robustez
                        for label in ["Estado", "Estatus", "Situación"]:
                            lbl = detail_page.get_by_text(label, exact=False)
                            if await lbl.count():
                                # Toma el contenedor y extrae el siguiente texto
                                try:
                                    parent = lbl.first.locator("xpath=..")
                                    estado = (await parent.inner_text()).split(":")[-1].strip()
                                    break
                                except:
                                    pass

                        for label in ["Adjudicado a", "Empresa adjudicada", "Contratista"]:
                            lbl = detail_page.get_by_text(label, exact=False)
                            if await lbl.count():
                                try:
                                    parent = lbl.first.locator("xpath=..")
                                    adjudicado_a = (await parent.inner_text()).split(":")[-1].strip()
                                    break
                                except:
                                    pass

                        for label in ["Monto Adjudicado", "Importe adjudicado", "Monto"]:
                            lbl = detail_page.get_by_text(label, exact=False)
                            if await lbl.count():
                                try:
                                    parent = lbl.first.locator("xpath=..")
                                    monto_adjudicado = (await parent.inner_text()).split(":")[-1].strip()
                                    break
                                except:
                                    pass

                        # Volver a la lista si navegó en la misma pestaña
                        if detail_page == page:
                            await page.go_back()
                            await page.wait_for_timeout(1000)
                        else:
                            await detail_page.close()

                    # Si no detectamos un "número" razonable, generamos uno básico
                    if not numero:
                        # Usa 2 primeras celdas concatenadas como fallback
                        numero = (text_cells[0] if text_cells else f"PROC-{i}").strip()

                    key = numero
                    results[key] = {
                        "numero": numero,
                        "descripcion": descripcion,
                        "fecha_publicacion": fecha_publicacion,
                        "estado": estado,
                        "adjudicado_a": adjudicado_a,
                        "monto_adjudicado": monto_adjudicado,
                        "ultima_lectura": now_tijuana().isoformat()
                    }

            # Intentar ir a "Siguiente" página si existe
            went_next = False
            for next_text in ["Siguiente", ">", ">>", "Siguiente »", "Next"]:
                nxt = page.get_by_role("button", name=next_text, exact=False)
                if await nxt.count():
                    # Verifica si está habilitado
                    try:
                        disabled = await nxt.first.is_disabled()
                    except:
                        disabled = False
                    if not disabled:
                        await nxt.first.click()
                        went_next = True
                        await page.wait_for_timeout(1500)
                        break

            if not went_next:
                break

        await context.close()
        await browser.close()

    return results

# ---------------------------
# DIF Y ALERTAS
# ---------------------------
def compare_and_alert(old: dict, new: dict):
    """
    Compara estados, envía alertas a Telegram por:
     - nuevas licitaciones
     - cambios en descripción/estado/adjudicado/monto
    Devuelve el estado combinado actualizado.
    """
    updated = dict(old)

    # Nuevas
    for num, data in new.items():
        if num not in old:
            msg = (
                f"Licitación Nueva {data['numero']}, "
                f"{data['descripcion']}, "
                f"{data['fecha_publicacion'] or 'Fecha no disponible'}"
            )
            print("[NEW]", msg)
            send_telegram(msg)
            updated[num] = data

    # Cambios
    campos = ["descripcion", "estado", "adjudicado_a", "monto_adjudicado"]
    for num, data in new.items():
        if num in old:
            prev = old[num]
            changed = any((prev.get(c, "").strip() != data.get(c, "").strip()) for c in campos)
            if changed:
                msg = (
                    f"Cambios en {data['numero']}, "
                    f"{data.get('descripcion','')}, "
                    f"{data.get('estado','') or 'Estado no disponible'}, "
                    f"{data.get('adjudicado_a','') or 'N/D'}, "
                    f"{data.get('monto_adjudicado','') or 'N/D'}"
                )
                print("[CHG]", msg)
                send_telegram(msg)
                updated[num] = data

    # Mantener entradas previas no vistas esta vez (por si caen temporalmente)
    for num, pdata in old.items():
        if num not in new:
            updated.setdefault(num, pdata)

    return updated

# ---------------------------
# CONTROL DE FRECUENCIA DIARIA (OPCIONAL)
# ---------------------------
def runs_counter_file():
    return f".runs_{now_tijuana().date().isoformat()}.txt"

def can_run_today(max_runs=RUNS_PER_DAY):
    """
    Garantiza como máximo 'max_runs' ejecuciones por día (solo útil si programas
    el workflow más veces). Para 4 cron fijos, no es indispensable.
    """
    path = runs_counter_file()
    count = 0
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                count = int(f.read().strip())
            except:
                count = 0
    return count < max_runs

def increment_run_counter():
    path = runs_counter_file()
    count = 0
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                count = int(f.read().strip())
            except:
                count = 0
    with open(path, "w") as f:
        f.write(str(count + 1))

# ---------------------------
# MAIN
# ---------------------------
async def main():
    # 1) Limitar a horario laboral
    if not within_business_hours():
        print("[INFO] Fuera de horario laboral de 09:00–19:00 (America/Tijuana). Saliendo.")
        return

    # 2) (Opcional) Limitar número de corridas por día si programaste más triggers
    if not can_run_today():
        print("[INFO] Ya se alcanzó el máximo de ejecuciones del día. Saliendo.")
        return

    old_state = load_state()
    try:
        new_state = await scrape_listings()
    except Exception as e:
        print(f"[ERROR] Falló el scraping: {e}")
        return

    updated = compare_and_alert(old_state, new_state)
    save_state(updated)
    increment_run_counter()
    print(f"[OK] Finalizado. Total licitaciones registradas: {len(updated)}")

if __name__ == "__main__":
    asyncio.run(main())
