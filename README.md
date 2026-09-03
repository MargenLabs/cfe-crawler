# Monitor de Licitaciones CFE — Baja California

Monitor automático de procedimientos publicados por la **Comisión Federal de Electricidad (CFE)** para **Baja California**.

El objetivo es detectar oportunamente nuevas licitaciones y cambios relevantes en procedimientos del año en curso y enviar las alertas a Telegram.

## Qué hace

El monitor:

- Entra al portal de Concursos de CFE.
- Selecciona **Baja California**.
- Lee hasta **5 páginas** de resultados por ejecución.
- Extrae, cuando están disponibles:
  - Número de procedimiento
  - Descripción
  - Fecha de publicación
  - Estado
  - Adjudicado a
  - Monto adjudicado
- Compara los resultados contra `cfe_state.json`.
- Detecta procedimientos nuevos y cambios en procedimientos existentes.
- Envía alertas a Telegram únicamente para procedimientos del **año en curso**.
- Incorpora procedimientos de años anteriores al estado sin generar alertas.
- Guarda el estado actualizado para evitar notificaciones repetidas.

## Alertas de Telegram

Se generan dos tipos principales de mensajes:

- `🚨 Nueva Licitación`: procedimiento del año en curso que no existía en el estado anterior.
- `⚠️ Actualización`: cambio en descripción, fecha de publicación, estado, adjudicatario o monto de un procedimiento del año en curso.

Si una ejecución no encuentra cambios, no se envía ningún mensaje.

## Horarios

GitHub Actions ejecuta el monitor cinco veces al día, usando la hora local de **America/Tijuana** para validar la ventana de trabajo:

- 09:00
- 12:00
- 15:00
- 18:00
- 20:00

También puede ejecutarse manualmente desde **Actions → CFE Monitor → Run workflow**.

> Los `cron` de GitHub Actions están definidos en UTC. El script valida adicionalmente la hora de Tijuana antes de ejecutar el scraping.

## Confiabilidad

El scraper valida que:

- pueda localizar el control de Entidad Federativa;
- pueda seleccionar Baja California;
- CFE muestre una tabla válida de procedimientos;
- se obtenga al menos un procedimiento.

Si CFE cambia su interfaz o no entrega resultados válidos, el script genera una excepción y GitHub Actions muestra **Failure**. Esto evita que un fallo de scraping aparezca falsamente como `Success` y evita guardar un estado incorrecto.

El código también imprime información de diagnóstico cuando no puede identificar los controles o la tabla de CFE.

## Estado

`cfe_state.json` funciona como memoria del monitor. Cada procedimiento se identifica por su número y almacena la última información conocida.

El estado permite que una segunda ejecución sobre los mismos datos termine correctamente sin volver a enviar las mismas alertas a Telegram.

## Configuración

Variables/secrets utilizados:

| Variable | Descripción |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | Chat donde se envían las alertas |
| `MAX_PAGES` | Máximo de páginas de CFE a revisar; actualmente el workflow usa `5` |

Los datos sensibles de Telegram deben permanecer en **GitHub Actions Secrets** y no escribirse directamente en el repositorio.

## Archivos principales

- `cfe_monitor.py` — scraper, comparación y alertas.
- `cfe_state.json` — estado persistente de procedimientos conocidos.
- `.github/workflows/cfe_monitor.yml` — ejecución automática mediante GitHub Actions.
- `requirements.txt` — dependencias de Python.

## Ejecución local

Con Python 3.11 y las dependencias instaladas:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python cfe_monitor.py
```

Para enviar alertas desde una ejecución local deben existir las variables de entorno `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

## Portal monitoreado

CFE — Concursos:
https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/

## Mantenimiento

Si el workflow comienza a mostrar `Failure`, revisar primero el paso **Run monitor** en GitHub Actions. Los mensajes `[DIAG]`, `[WARN]` y el traceback permiten distinguir entre un cambio en la interfaz de CFE, un problema de carga y un error de procesamiento.

Última actualización funcional importante: **septiembre de 2026**, después de adaptar el scraper a cambios en la interfaz de CFE y reforzar la validación de resultados.
