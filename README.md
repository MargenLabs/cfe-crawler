# Monitor de Licitaciones CFE (Baja California)

Este repositorio contiene un script en Python que:
- Entra a https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/
- Filtra por **Baja California**
- Extrae las licitaciones (y si es posible: Estado, Adjudicado a y Monto Adjudicado en el detalle)
- Detecta **nuevas** licitaciones y **cambios**
- Envía alertas a Telegram
- Guarda el estado en `cfe_state.json`

## Estructura
