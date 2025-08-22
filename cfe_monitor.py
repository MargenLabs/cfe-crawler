# cfe_monitor.py
import os
import json
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright

# --- Envío a Telegram con control de rate limit 429 y pequeña pausa entre mensajes ---
def send_telegram_rate_limited(text, bot_token, chat_id):
    """
    Envía un mensaje a Telegram, respetando rate limit (429 retry_after)
    y añadiendo una pausa corta entre mensajes para no saturar el chat.
    Devuelve True si se envió, False si falló con error no recuperable.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    while True:
        r = requests.post(url, data=data, timeout=30)
        if r.ok:
            # Pausa mínima para no “floodear” (≈ 1 msg/seg)
            time.sleep(1.2)
            return True

        # Si Telegram responde 429, respeta retry_after y reintenta
        if r.status_code == 429:
            retry_after = 5
            try:
                retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                pass
            time.sleep(int(retry_after) + 1)
            continue

        # Otros errores: loguea y no reintentes infinitamente
        print(f"[ERROR] Telegram {r.status_code}: {r.text}")
        return False


# Alias por compatibilidad: si en el código se usa send_to_telegram(...),
# lo redirigimos aquí para NO tener que cambiar llamadas en otras partes.
def send_to_telegram(text, bot_token, chat_id):
    return send_telegram_rate_limited(text, bot_token, chat_id)

# ---------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------
CFE_URL = "https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/"
ESTADO_OBJETIVO = "Baja California"
STATE_FILE = "cfe_state.json"

# Horario laboral (hora de Tijuana) y número máximo de ejecuciones/día
WORK_START = 0   # 09:00
WORK_END   = 24  # 19:00
RUNS_PER_DAY = 4

# Paginación: ¿cuántas páginas revisar?
MAX_PAGES = int(os.getenv("MAX_PAGES", "1"))  # 1 = solo la primera página

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
    ok = send_telegram_rate_limited(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if ok:
        print("[OK] Mensaje enviado a Telegram")
    else:
        print("[ERROR] Falló el envío a Telegram")

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
    Lee SOLO lo que aparece en la tabla de resultados (sin abrir detalle).
    Devuelve {numero: {numero, descripcion, fecha_publicacion, estado, adjudicado_a, monto_adjudicado, ultima_lectura}}
    Respeta MAX_PAGES (por defecto 1 = primera página).
    """
    results = {}

    def norm(text: str) -> str:
        t = (text or "").lower().replace("\n", " ").strip()
        # normalización suave para buscar encabezados
        return (t
                .replace("ñ", "n")
                .replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1) Ir al sitio
        await page.goto(CFE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # 2) Seleccionar "Baja California"
        select_locator_candidates = [
            "select:has(option:text('Baja California'))",
            "select[formcontrolname*='Entidad'], select[name*='Entidad'], select[id*='Entidad']",
            "select"
        ]
        select = None
        for cand in select_locator_candidates:
            loc = page.locator(cand)
            if await loc.count():
                try:
                    await loc.select_option(label=ESTADO_OBJETIVO)
                    select = loc
                    break
                except:
                    pass
        if not select:
            try:
                await page.get_by_text("Entidad", exact=False).click(timeout=2000)
                await page.get_by_role("option", name=ESTADO_OBJETIVO, exact=True).click(timeout=2000)
            except:
                print("[WARN] No se pudo seleccionar el estado. Revisa el selector.")

        # 3) Click en Buscar/Consultar
        for button_text in ["Buscar", "Consultar", "Filtrar", "Aceptar"]:
            btn = page.get_by_role("button", name=button_text, exact=False)
            if await btn.count():
                await btn.first.click()
                break
        await page.wait_for_timeout(3000)

        # 4) Recorremos páginas (sin abrir detalle)
        page_idx = 0
        while True:
            page_idx += 1

            table = page.locator("table")
            if not await table.count():
                table = page.locator("[role='table']")

            if not await table.count():
                print("[WARN] No se encontró tabla en la página actual.")
            else:
                # --- Mapear encabezados -> índices ---
                header_cells = table.locator("thead tr th, thead tr td, tr:first-child th, tr:first-child td")
                hcount = await header_cells.count()
                headers = [norm(await header_cells.nth(i).inner_text()) for i in range(hcount)] if hcount else []

                def find_col(*needles):
                    for i, h in enumerate(headers):
                        if any(n in h for n in needles):
                            return i
                    return None

                idx_num   = find_col("numero de procedimiento", "numero", "procedimiento")
                idx_desc  = find_col("descripcion")
                idx_fecha = find_col("fecha public")
                idx_est   = find_col("estado", "estatus", "situacion")
                idx_adj   = find_col("adjudicado a", "empresa adjudicada", "contratista")
                idx_monto = find_col("monto adjudicado", "monto adjudicado en pesos", "importe")

                # --- Filas de datos ---
                body_rows = table.locator("tbody tr")
                if not await body_rows.count():
                    # fallback si no hay <tbody>
                    body_rows = table.locator("tr").nth(1)  # evitar la fila de encabezado
                    # mejor: todas menos la primera
                    all_rows = table.locator("tr")
                    start_at = 1 if (await all_rows.count()) > 1 else 0
                    rows = [all_rows.nth(i) for i in range(start_at, await all_rows.count())]
                else:
                    rows = [body_rows.nth(i) for i in range(await body_rows.count())]

                for r in rows:
                    cells = r.locator("td")
                    ccount = await cells.count()
                    if not ccount:
                        continue

                    def get_cell(idx):
                        if idx is None or idx >= ccount:
                            return ""
                        return (await cells.nth(idx).inner_text()).strip()

                    numero = get_cell(idx_num)
                    descripcion = get_cell(idx_desc)
                    fecha_publicacion = get_cell(idx_fecha)
                    estado = get_cell(idx_est)
                    adjudicado_a = get_cell(idx_adj)
                    monto_adjudicado = get_cell(idx_monto)

                    # Fallbacks mínimos si faltó mapeo:
                    if not numero:
                        numero = (await cells.nth(0).inner_text()).strip()
                    if not descripcion:
                        # toma la celda más larga como descripción aproximada
                        texts = [(await cells.nth(i).inner_text()).strip() for i in range(ccount)]
                        descripcion = max(texts, key=len).replace("\n", " ") if texts else ""

                    key = numero
                    results[key] = {
                        "numero": numero,
                        "descripcion": descripcion,
                        "fecha_publicacion": fecha_publicacion,
                        "estado": estado,
                        "adjudicado_a": adjudicado_a,
                        "monto_adjudicado": monto_adjudicado,
                        "ultima_lectura": now_tijuana().isoformat(),
                    }

            # ¿Más páginas? Solo si no alcanzamos el tope
            if page_idx >= MAX_PAGES:
                break

            went_next = False
            for next_text in ["Siguiente", ">", ">>", "Siguiente »", "Next"]:
                nxt = page.get_by_role("button", name=next_text, exact=False)
                if await nxt.count():
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
