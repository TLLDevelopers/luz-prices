#!/usr/bin/env python3
"""Descarga los precios day-ahead de Energy-Charts (DE-LU, AT, NL) y los guarda
como JSON estatico versionado en site/v1/.

Respeta el limite de 2 peticiones por minuto de la API: una peticion por zona,
con 35 s entre ellas. Si una descarga falla o no pasa la validacion se conserva
lo ultimo valido y se marca la antiguedad en index.json y status.json.
Solo biblioteca estandar.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = 1
API = "https://api.energy-charts.info/price"
LICENSE = {"name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}
SOURCE = {
    "name": "Energy-Charts.info (Fraunhofer ISE)",
    "url": "https://www.energy-charts.info",
    "data_origin": "Bundesnetzagentur | SMARD.de",
}
ZONES = {
    "DE": {"bzn": "DE-LU", "tz": "Europe/Berlin"},
    "AT": {"bzn": "AT", "tz": "Europe/Vienna"},
    "NL": {"bzn": "NL", "tz": "Europe/Amsterdam"},
}
KEEP_DAYS = 40
PAUSE = 35  # s entre peticiones (limite 2/min)
STALE_HOURS = 30  # sin descarga correcta desde hace mas: marcado como stale
PRICE_MIN, PRICE_MAX = -1000.0, 5000.0  # EUR/MWh


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(bzn: str, start: date, end: date, opener=urllib.request.urlopen) -> dict:
    url = f"{API}?bzn={bzn}&start={start.isoformat()}&end={end.isoformat()}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "luz-prices-cache/1 (TLL Developers; github.com/TLLDevelopers/luz-prices)"},
    )
    last = None
    for attempt in range(3):
        try:
            with opener(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            last = e
            if e.code == 429:
                time.sleep(max(61, int(e.headers.get("Retry-After", "61") or 61)))
            else:
                time.sleep(10 * (attempt + 1))
        except Exception as e:  # red, JSON roto, timeout
            last = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"fetch {bzn}: {last}")


def validate(raw: dict) -> tuple[list, list]:
    """Devuelve (tiempos, precios) validados o lanza ValueError."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("respuesta vacia o no es un objeto")
    lic = str(raw.get("license_info", ""))
    if "CC BY 4.0" not in lic:
        raise ValueError(f"licencia no apta: {lic!r}")
    unit = str(raw.get("unit", ""))
    if unit and "MWh" not in unit:
        raise ValueError(f"unidad inesperada: {unit!r}")
    t, p = raw.get("unix_seconds"), raw.get("price")
    if not isinstance(t, list) or not isinstance(p, list):
        raise ValueError("faltan series")
    if len(t) != len(p):
        raise ValueError("series de distinta longitud")
    for i, (ts, v) in enumerate(zip(t, p)):
        if not isinstance(ts, int) or (i and ts <= t[i - 1]):
            raise ValueError("tiempos no crecientes")
        if v is not None and (not isinstance(v, (int, float)) or not PRICE_MIN <= v <= PRICE_MAX):
            raise ValueError(f"precio fuera de rango: {v!r}")
    return t, p


def split_days(t: list, p: list, tz: ZoneInfo) -> dict:
    days: dict = {}
    for ts, v in zip(t, p):
        if v is None:
            continue
        d = datetime.fromtimestamp(ts, tz).date().isoformat()
        a, b = days.setdefault(d, ([], []))
        a.append(ts)
        b.append(v)
    return days


def day_doc(code: str, d: str, ts: list, pr: list, fetched: str) -> dict:
    step = (ts[1] - ts[0]) // 60 if len(ts) > 1 else 60
    return {
        "schema_version": SCHEMA,
        "zone": code,
        "bidding_zone": ZONES[code]["bzn"],
        "date": d,
        "timezone": ZONES[code]["tz"],
        "unit": "EUR/MWh",
        "resolution_minutes": step,
        "slots": len(ts),
        "unix_seconds": ts,
        "price": pr,
        "fetched_at": fetched,
        "source": SOURCE,
        "license": LICENSE,
        "modified": "Split by local calendar day; otherwise unmodified.",
    }


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def update_zone(code: str, root: Path, now: datetime, fetcher=fetch) -> dict:
    cfg = ZONES[code]
    tz = ZoneInfo(cfg["tz"])
    zdir = root / "v1" / code
    old = read_json(zdir / "index.json") or {}
    today = now.astimezone(tz).date()
    have = list(zdir.glob("????-??-??.json")) if zdir.exists() else []
    start = today - timedelta(days=KEEP_DAYS if len(have) < 5 else 3)
    end = today + timedelta(days=2)
    idx = dict(old)
    idx.update({"schema_version": SCHEMA, "zone": code, "bidding_zone": cfg["bzn"],
                "timezone": cfg["tz"], "checked_at": iso(now), "license": LICENSE, "source": SOURCE})
    try:
        raw = fetcher(cfg["bzn"], start, end)
        fetched = iso(now)
        if not raw:
            # El origen contesta pero no tiene nada (404 / vacio): la comprobacion
            # cuenta como hecha. Si no llegan dias nuevos, last_data_date se queda
            # atras y la app lo interpreta como "el mercado dejo de publicarse".
            idx["fetched_at"] = fetched
            idx["last_error"] = "upstream returned no content"
            raise StopIteration
        t, p = validate(raw)
        days = split_days(t, p, tz)
        if not days:
            raise ValueError("sin precios")
        for d, (ts, pr) in days.items():
            path = zdir / f"{d}.json"
            prev = read_json(path)
            if prev and prev.get("slots", 0) > len(ts):
                continue  # no sustituir un dia completo por uno con menos tramos
            if prev and prev.get("unix_seconds") == ts and prev.get("price") == pr:
                continue
            write_json(path, day_doc(code, d, ts, pr, fetched))
        idx["fetched_at"] = fetched
        idx["last_error"] = None
    except StopIteration:
        pass
    except Exception as e:
        idx["last_error"] = f"{type(e).__name__}: {e}"[:300]
    limite = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for f in (list(zdir.glob("????-??-??.json")) if zdir.exists() else []):
        if f.stem < limite:
            f.unlink()
    dates = sorted(f.stem for f in zdir.glob("????-??-??.json")) if zdir.exists() else []
    idx["dates"] = dates
    idx["last_data_date"] = dates[-1] if dates else None
    idx["tomorrow_available"] = (today + timedelta(days=1)).isoformat() in dates
    fa = idx.get("fetched_at")
    age = None
    if fa:
        ft = datetime.strptime(fa, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = round((now - ft).total_seconds() / 3600, 2)
    idx["age_hours"] = age
    idx["status"] = "ok" if age is not None and age <= STALE_HOURS else "stale"
    if dates:
        write_json(zdir / "index.json", idx)
    return idx


def main(root: str = "site") -> int:
    rootp = Path(root)
    now = now_utc()
    zones = {}
    for i, code in enumerate(ZONES):
        if i:
            time.sleep(PAUSE)
        zones[code] = update_zone(code, rootp, now)
    keys = ("status", "fetched_at", "age_hours", "last_data_date", "tomorrow_available", "last_error")
    write_json(rootp / "v1" / "status.json", {
        "schema_version": SCHEMA, "generated_at": iso(now), "license": LICENSE, "source": SOURCE,
        "zones": {c: {k: z.get(k) for k in keys} for c, z in zones.items()},
    })
    for c, z in zones.items():
        if z["status"] != "ok" or z.get("last_error"):
            print(f"WARN {c}: status={z['status']} error={z.get('last_error')}", file=sys.stderr)
    return 0  # se conserva lo valido; la alerta la da check_tomorrow.py


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
