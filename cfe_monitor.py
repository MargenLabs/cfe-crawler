import os
import json
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

CFE_URL = "https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/"
ESTADO_OBJETIVO = "Baja California"
STATE_FILE = "cfe_state.json"
WORK_START = 9
WORK_END = 20
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def now_tijuana():
    return datetime.now(ZoneInfo("America/Tijuana"))


def within_business_hours():
    return WORK_START <= now_tijuana().hour < WORK_END


def send_telegram_rate_limited(text, bot_token, chat_id):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    while True:
        r = requests.post(url, data=data, timeout=30)
        if r.ok:
            time.sleep(1.2)
            return True
        if r.status_code == 429:
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = 5
            time.sleep(retry_after + 1)
            continue
        print(f"[ERROR] Telegram {r.status_code}: {r.text}")
        return False


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram no configurado; mensaje no enviado.")
        return
    if send_telegram_rate_limited(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID):
        print("[OK] Mensaje enviado a Telegram")
    else:
        print("[ERROR] Falló el envío a Telegram")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception as exc:
            raise RuntimeError(f"No se pudo leer {STATE_FILE}: {exc}") from exc


def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def norm(text):
    return ((text or "").lower().replace("\n", " ").strip()
            .replace("ñ", "n").replace("á", "a").replace("é", "e")
            .replace("í", "i").replace("ó", "o").replace("ú", "u"))


async def find_results_table(page):
    """Devuelve la tabla que contiene el encabezado Numero de Procedimiento."""
    tables = page.locator("table, [role='table']")
    for i in range(await tables.count()):
        table = tables.nth(i)
        try:
            text = norm(await table.inner_text(timeout=2000))
        except Exception:
            continue
        if "numero de procedimiento" in text and "fecha publicacion" in text:
            return table
    return None


async def wait_for_results_table(page, timeout_ms=30000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        table = await find_results_table(page)
        if table is not None:
            rows = table.locator("tr")
            if await rows.count() >= 2:
                return table
        await page.wait_for_timeout(500)
    return None


async def select_baja_california(page):
    # La pagina de CFE ha cambiado de tiempos/markup; buscamos el select por sus opciones.
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            options = norm(await sel.locator("option").all_inner_texts())
        except Exception:
            continue
        if "baja california" in options:
            await sel.select_option(label=ESTADO_OBJETIVO)
            try:
                selected = (await sel.locator("option:checked").inner_text()).strip()
            except Exception:
                selected = ""
            if norm(selected) != norm(ESTADO_OBJETIVO):
                raise RuntimeError(f"Entidad seleccionada inesperada: {selected!r}")
            print(f"[INFO] Entidad seleccionada: {selected}")
            return
    raise RuntimeError("No se encontró el selector de Entidad Federativa con Baja California")


async def click_search(page):
    for text in ["Buscar", "Consultar", "Filtrar", "Aceptar"]:
        btn = page.get_by_role("button", name=text, exact=False)
        if await btn.count():
            await btn.first.click()
            print(f"[INFO] Botón de búsqueda: {text}")
            return
    # Fallback para inputs tipo submit.
    submit = page.locator("input[type='submit'], button[type='submit']")
    if await submit.count():
        await submit.first.click()
        print("[INFO] Botón de búsqueda: submit")
        return
    raise RuntimeError("No se encontró el botón Buscar/Consultar")


async def parse_table(table, results):
    rows = table.locator("tr")
    row_count = await rows.count()
    header_idx = None
    headers = []

    for j in range(min(row_count, 8)):
        cells = rows.nth(j).locator("th,td")
        texts = [norm(await cells.nth(k).inner_text()) for k in range(await cells.count())]
        if any("numero de procedimiento" in h for h in texts):
            header_idx = j
            headers = texts
            break

    if header_idx is None:
        raise RuntimeError("Se encontró una tabla, pero no el encabezado 'Número de Procedimiento'")

    def find_idx(*opts):
        for idx, header in enumerate(headers):
            if any(norm(opt) in header for opt in opts):
                return idx
        return None

    idx_numero = find_idx("número de procedimiento", "numero de procedimiento")
    idx_desc = find_idx("descripción", "descripcion")
    idx_fecha = find_idx("fecha publicación", "fecha publicacion")
    idx_estado = find_idx("estado")
    idx_adj = find_idx("adjudicado a")
    idx_monto = find_idx("monto adjudicado en pesos", "monto adjudicado", "monto")

    if idx_numero is None:
        raise RuntimeError("No se pudo mapear la columna Número de Procedimiento")

    async def cell_text(cells, idx):
        if idx is None or idx >= await cells.count():
            return ""
        return (await cells.nth(idx).inner_text()).strip()

    found = 0
    for r in range(header_idx + 1, row_count):
        cells = rows.nth(r).locator("td")
        if not await cells.count():
            continue
        numero = await cell_text(cells, idx_numero)
        if not numero:
            continue
        results[numero] = {
            "numero": numero,
            "descripcion": (await cell_text(cells, idx_desc)).replace("\n", " ").strip(),
            "fecha_publicacion": await cell_text(cells, idx_fecha),
            "estado": await cell_text(cells, idx_estado),
            "adjudicado_a": await cell_text(cells, idx_adj),
            "monto_adjudicado": await cell_text(cells, idx_monto),
            "ultima_lectura": now_tijuana().isoformat(),
        }
        found += 1
    return found


async def go_next_page(page, current_table):
    # Preferimos controles cuyo texto/aria-label indique siguiente.
    candidates = page.locator("a, button")
    for i in range(await candidates.count()):
        el = candidates.nth(i)
        try:
            text = norm((await el.inner_text()) + " " + (await el.get_attribute("aria-label") or "") + " " + (await el.get_attribute("title") or ""))
        except Exception:
            continue
        if not any(token in text for token in ["siguiente", "next"]):
            continue
        try:
            if await el.is_disabled():
                continue
        except Exception:
            pass
        cls = norm(await el.get_attribute("class") or "")
        if "disabled" in cls:
            continue
        old_text = await current_table.inner_text()
        await el.click()
        try:
            await page.wait_for_function(
                "oldText => Array.from(document.querySelectorAll('table')).some(t => t.innerText !== oldText && /procedimiento/i.test(t.innerText))",
                old_text,
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(1500)
        return True
    return False


async def scrape_listings():
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1200}, locale="es-MX")
        page = await context.new_page()
        page.set_default_timeout(15000)

        response = await page.goto(CFE_URL, wait_until="domcontentloaded", timeout=45000)
        if response and response.status >= 400:
            raise RuntimeError(f"CFE respondió HTTP {response.status}")

        await page.wait_for_load_state("networkidle", timeout=20000)
        await select_baja_california(page)
        await click_search(page)

        table = await wait_for_results_table(page, 30000)
        if table is None:
            title = await page.title()
            body = (await page.locator("body").inner_text())[:1000]
            raise RuntimeError(f"CFE no mostró la tabla de resultados. title={title!r}; body={body!r}")

        for page_idx in range(MAX_PAGES):
            if page_idx:
                table = await wait_for_results_table(page, 15000)
                if table is None:
                    raise RuntimeError(f"La tabla desapareció al paginar a página {page_idx + 1}")
            found = await parse_table(table, results)
            print(f"[INFO] Página {page_idx + 1}: {found} procedimientos; acumulados: {len(results)}")
            if page_idx >= MAX_PAGES - 1:
                break
            if not await go_next_page(page, table):
                break

        await context.close()
        await browser.close()

    if not results:
        raise RuntimeError("El scraping terminó con 0 procedimientos; se aborta para no registrar un falso éxito")

    sample = list(results)[:10]
    print(f"[INFO] Scraping válido: {len(results)} procedimientos. Primeros: {', '.join(sample)}")
    return results


def compare_and_alert(old, new):
    updated = dict(old)
    for num, data in new.items():
        if num not in old:
            msg = f"🚨 Nueva Licitación\n{data['descripcion']}\n{data['numero']}\n{data['fecha_publicacion'] or 'Fecha no disponible'}"
            print("[NEW]", msg)
            send_telegram(msg)
            updated[num] = data

    campos = ["descripcion", "fecha_publicacion", "estado", "adjudicado_a", "monto_adjudicado"]
    for num, data in new.items():
        if num not in old:
            continue
        prev = old[num]
        diffs = []
        for c in campos:
            pv = (prev.get(c, "") or "").strip()
            nv = (data.get(c, "") or "").strip()
            if pv != nv:
                pv_show = pv[:180] + "…" if len(pv) > 180 else pv
                nv_show = nv[:180] + "…" if len(nv) > 180 else nv
                diffs.append(f"{c}: '{pv_show}' → '{nv_show}'")
        if diffs:
            msg = f"⚠️ Actualización\n{data['descripcion']}\n{data['numero']}\nCambios:\n- " + "\n- ".join(diffs)
            print("[CHG]", msg)
            send_telegram(msg)
            updated[num] = data

    return updated


async def main():
    if not within_business_hours():
        print("[INFO] Fuera de horario laboral de 09:00–20:00 (America/Tijuana). Saliendo.")
        return

    old_state = load_state()
    new_state = await scrape_listings()  # una excepción debe hacer fallar GitHub Actions
    updated = compare_and_alert(old_state, new_state)
    save_state(updated)
    print(f"[OK] Finalizado. Leídos ahora: {len(new_state)}; total registrados: {len(updated)}")


if __name__ == "__main__":
    asyncio.run(main())
