#!/usr/bin/env python3
"""Alerta: sale con codigo 1 si pasada la hora habitual faltan los datos de
manana en alguna zona, o si algun indice esta caducado. Se ejecuta tras cada
descarga; un job rojo en GitHub avisa por correo. Lo vigila tambien el
`tllp seo watch` del portatil (ALERTS.md), leyendo el status.json publicado."""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEADLINE_HOUR = 16  # hora local: la subasta se publica ~12:45
TZS = {"DE": "Europe/Berlin", "AT": "Europe/Vienna", "NL": "Europe/Amsterdam"}


def problems_for(status: dict, now=None) -> list:
    out = []
    for code, z in status["zones"].items():
        local = (now or datetime.now(ZoneInfo(TZS[code]))).astimezone(ZoneInfo(TZS[code]))
        if z["status"] != "ok":
            out.append(f"{code}: datos caducados (antiguedad {z['age_hours']} h, error {z['last_error']})")
        if local.hour >= DEADLINE_HOUR and not z["tomorrow_available"]:
            out.append(f"{code}: faltan los precios de manana pasadas las {DEADLINE_HOUR}:00 locales "
                       f"(ultimo dia {z['last_data_date']})")
    return out


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
    st = json.loads((root / "v1" / "status.json").read_text(encoding="utf-8"))
    probs = problems_for(st)
    if probs:
        print("ALERTA\n" + "\n".join(probs))
        sys.exit(1)
    print("OK: precios de manana presentes o aun dentro de plazo")
