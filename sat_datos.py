#!/usr/bin/env python3
"""
================================================================================
 sat_datos.py - CAPA DE ADQUISICION Y PERSISTENCIA DEL SAT QUISCAB
================================================================================

Modulo de adquisicion de datos de la red de estaciones meteorologicas de
Vivamos Mejor / DAT (Solola, Guatemala) y persistencia en base de datos SQLite,
para alimentar el Sistema de Alerta Temprana de la subcuenca del rio Quiscab.

Autor : Ing. Agr. Jose Faustino Chuj Matul  (Green Solutions)

--------------------------------------------------------------------------------
ARQUITECTURA DE ADAPTADORES
--------------------------------------------------------------------------------
La capa de adquisicion es INTERCAMBIABLE. Se implementan dos adaptadores que
devuelven exactamente la misma estructura de datos:

  1. AdaptadorWebScraping  -> lee el estado incrustado en el dashboard publico
                              de Weather Underground. No requiere API key.
                              Uso: puente temporal / academico.

  2. AdaptadorAPI          -> consume la API oficial de WU PWS. Requiere una
                              apiKey de contribuidor. Uso: produccion.

Cambiar de uno a otro es cambiar una linea. El resto del SAT no se entera.

--------------------------------------------------------------------------------
NOTA DE TRANSPARENCIA (declarar en el informe academico)
--------------------------------------------------------------------------------
El adaptador de web scraping extrae el estado JSON que el propio servidor de
Weather Underground incrusta en el HTML de sus paginas publicas. No elude
autenticacion ni controles tecnicos de acceso, pero los Terminos de Servicio de
la plataforma desaconsejan la extraccion automatizada. Se implementa con
limitacion de frecuencia y agente de usuario identificable, y se concibe como
puente temporal mientras se gestiona el acceso formal a la API. Para operacion
institucional debe migrarse al AdaptadorAPI.

--------------------------------------------------------------------------------
VALOR METODOLOGICO DE LA BASE DE DATOS
--------------------------------------------------------------------------------
Al acumular observaciones propias, el SAT construye su PROPIA serie historica de
pluviometros dentro de la cuenca. Esto habilita, con el tiempo, la CALIBRACION
LOCAL de los umbrales Intensidad-Duracion, que es precisamente la limitacion
metodologica de usar valores semilla tomados de la literatura.

--------------------------------------------------------------------------------
PERSISTENCIA EN LA NUBE (limitacion importante)
--------------------------------------------------------------------------------
Streamlit Community Cloud usa un sistema de archivos EFIMERO: la base SQLite se
pierde cuando la app se reinicia o duerme. Estrategias, de menor a mayor
robustez:
  (a) SQLite local  -> valido para demostracion y sesiones de trabajo.
  (b) Respaldo periodico descargable (boton de exportacion CSV/SQLite).
  (c) Base gestionada externa (Postgres gratuito: Supabase o Neon) -> cambiar
      solo la cadena de conexion en `conectar()`.
Para la entrega academica (a)+(b) es suficiente; para operacion real, usar (c).

Dependencias: requests, pandas  (sqlite3 es de la biblioteca estandar)
================================================================================
"""

from __future__ import annotations

import re
import json
import time
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional

import requests
import pandas as pd


# =============================================================================
# 1. CATALOGO DE ESTACIONES  (red Vivamos Mejor / DAT)
# =============================================================================
ESTACIONES_WU = [
    {"id": "ISOLOL4",   "nombre": "EFA Solola"},
    {"id": "IPANAJ5",   "nombre": "Oficina Central Panajachel"},
    {"id": "ISANAN84",  "nombre": "Lomas de Atitlan, San Andres Semetabaj"},
    {"id": "ISANTA535", "nombre": "Casa Santa Rita, Santa Lucia Utatlan"},
    {"id": "ISANTACL3", "nombre": "Santa Clara La Laguna"},
    {"id": "ISANTI135", "nombre": "CoAtitlan, Santiago Atitlan"},
]

URL_DASHBOARD = "https://www.wunderground.com/dashboard/pws/{sid}"

# Agente de usuario identificable y honesto sobre el proposito del acceso.
CABECERAS = {
    "User-Agent": (
        "SAT-Quiscab/2.0 (Sistema de Alerta Temprana academico, "
        "subcuenca rio Quiscab, Solola, Guatemala; investigacion hidrologica) "
        "Mozilla/5.0 (compatible)"
    ),
    "Accept-Language": "es-GT,es;q=0.9,en;q=0.8",
}

PAUSA_ENTRE_ESTACIONES = 3.0   # segundos: cortesia con el servidor
TIMEOUT = 30
REINTENTOS = 3                 # el servidor agota el tiempo con cierta frecuencia


# =============================================================================
# 2. BASE DE DATOS
# =============================================================================
RUTA_DB_DEFECTO = Path("sat_quiscab.db")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS observaciones (
    station_id      TEXT    NOT NULL,
    epoch           INTEGER NOT NULL,
    obs_utc         TEXT,
    lat             REAL,
    lon             REAL,
    temp_c          REAL,
    humedad         REAL,
    precip_rate_mmh REAL,
    precip_total_mm REAL,
    fuente          TEXT,
    PRIMARY KEY (station_id, epoch)
);

CREATE INDEX IF NOT EXISTS idx_obs_epoch   ON observaciones(epoch);
CREATE INDEX IF NOT EXISTS idx_obs_station ON observaciones(station_id, epoch);

CREATE TABLE IF NOT EXISTS estaciones (
    station_id   TEXT PRIMARY KEY,
    nombre       TEXT,
    lat          REAL,
    lon          REAL,
    ultima_carga TEXT
);

CREATE TABLE IF NOT EXISTS bitacora (
    ts        TEXT,
    evento    TEXT,
    detalle   TEXT
);
"""


def conectar(ruta: str | Path = RUTA_DB_DEFECTO) -> sqlite3.Connection:
    """
    Abre (y crea si hace falta) la base de datos del SAT.
    Para migrar a Postgres gestionado, sustituir esta funcion manteniendo la
    misma interfaz; el resto del modulo no cambia.
    """
    con = sqlite3.connect(str(ruta), check_same_thread=False)
    con.executescript(ESQUEMA)
    con.commit()
    return con


def registrar_bitacora(con: sqlite3.Connection, evento: str, detalle: str = "") -> None:
    """Deja constancia de cargas y errores: trazabilidad para el informe."""
    con.execute("INSERT INTO bitacora (ts, evento, detalle) VALUES (?,?,?)",
                (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), evento, detalle))
    con.commit()


def guardar_observaciones(con: sqlite3.Connection, obs: list[dict]) -> int:
    """
    Inserta observaciones evitando duplicados (clave: station_id + epoch).
    Devuelve el numero de registros REALMENTE nuevos.
    INSERT OR IGNORE hace idempotente la carga: se puede ejecutar cuantas veces
    se quiera sin corromper la serie.
    """
    if not obs:
        return 0
    antes = con.execute("SELECT COUNT(*) FROM observaciones").fetchone()[0]
    con.executemany(
        """INSERT OR IGNORE INTO observaciones
           (station_id, epoch, obs_utc, lat, lon, temp_c, humedad,
            precip_rate_mmh, precip_total_mm, fuente)
           VALUES (:station_id, :epoch, :obs_utc, :lat, :lon, :temp_c,
                   :humedad, :precip_rate_mmh, :precip_total_mm, :fuente)""",
        obs)
    con.commit()
    despues = con.execute("SELECT COUNT(*) FROM observaciones").fetchone()[0]
    return despues - antes


def actualizar_estacion(con: sqlite3.Connection, sid: str, nombre: str,
                        lat: Optional[float], lon: Optional[float]) -> None:
    """Guarda o refresca los metadatos y coordenadas de una estacion."""
    con.execute(
        """INSERT INTO estaciones (station_id, nombre, lat, lon, ultima_carga)
           VALUES (?,?,?,?,?)
           ON CONFLICT(station_id) DO UPDATE SET
             nombre=excluded.nombre,
             lat=COALESCE(excluded.lat, estaciones.lat),
             lon=COALESCE(excluded.lon, estaciones.lon),
             ultima_carga=excluded.ultima_carga""",
        (sid, nombre, lat, lon,
         dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    con.commit()


# =============================================================================
# 3. ADAPTADOR A: WEB SCRAPING DEL DASHBOARD PUBLICO
# =============================================================================
def _f(valor):
    """Convierte a float tolerando None y cadenas vacias."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _f_a_c(f):
    """Fahrenheit -> Celsius."""
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def _pulg_a_mm(p):
    """Pulgadas -> milimetros."""
    return None if p is None else p * 25.4


def _extraer_bloques_json(html: str, sid: str) -> list[dict]:
    """
    Extrae los objetos de observacion incrustados en el HTML.

    Weather Underground renderiza del lado del servidor e incrusta el estado de
    la aplicacion como JSON dentro del documento. Cada objeto de observacion
    empieza por {"stationID":"<ID>" y contiene marca temporal, coordenadas y un
    subobjeto de unidades ("imperial" o "metric") con precipRate/precipTotal.

    Se localiza cada ocurrencia y se recorta el objeto balanceando llaves, que
    es mas robusto que una expresion regular sobre JSON anidado.
    """
    obs = []
    patron = f'{{"stationID":"{sid}"'
    inicio = 0
    while True:
        i = html.find(patron, inicio)
        if i == -1:
            break
        # Recorte balanceado de llaves, respetando cadenas y escapes
        prof, j, en_cadena, escape = 0, i, False, False
        while j < len(html):
            c = html[j]
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = not en_cadena
            elif not en_cadena:
                if c == "{":
                    prof += 1
                elif c == "}":
                    prof -= 1
                    if prof == 0:
                        break
            j += 1
        fragmento = html[i:j + 1]
        inicio = j + 1
        try:
            obs.append(json.loads(fragmento))
        except json.JSONDecodeError:
            continue
    return obs


def _normalizar(o: dict, sid: str) -> Optional[dict]:
    """
    Lleva un objeto crudo de WU al esquema unico del SAT, en unidades metricas.
    El dashboard puede entregar el subobjeto en 'imperial' o en 'metric'.
    """
    epoch = o.get("epoch")
    if epoch is None:
        return None

    metrica = o.get("metric")
    imperial = o.get("imperial")

    if isinstance(metrica, dict) and metrica:
        temp = _f(metrica.get("tempAvg", metrica.get("temp")))
        p_rate = _f(metrica.get("precipRate"))
        p_total = _f(metrica.get("precipTotal"))
    elif isinstance(imperial, dict) and imperial:
        temp = _f_a_c(_f(imperial.get("tempAvg", imperial.get("temp"))))
        p_rate = _pulg_a_mm(_f(imperial.get("precipRate")))
        p_total = _pulg_a_mm(_f(imperial.get("precipTotal")))
    else:
        return None

    return {
        "station_id": sid,
        "epoch": int(epoch),
        "obs_utc": o.get("obsTimeUtc"),
        "lat": _f(o.get("lat")),
        "lon": _f(o.get("lon")),
        "temp_c": temp,
        "humedad": _f(o.get("humidityAvg", o.get("humidity"))),
        "precip_rate_mmh": p_rate,
        "precip_total_mm": p_total,
        "fuente": "scraping",
    }


def descargar_estacion_scraping(sid: str, sesion: Optional[requests.Session] = None
                                ) -> tuple[list[dict], str]:
    """
    Descarga y normaliza las observaciones de UNA estacion por scraping.
    Devuelve (lista_de_observaciones, mensaje_de_estado).
    Nunca lanza excepcion: un fallo de una estacion no debe tumbar el SAT.
    """
    s = sesion or requests.Session()
    url = URL_DASHBOARD.format(sid=sid)

    # Reintentos con espera creciente: el servidor a veces agota el tiempo.
    r = None
    for intento in range(REINTENTOS):
        try:
            r = s.get(url, headers=CABECERAS, timeout=TIMEOUT)
            if r.status_code == 200:
                break
        except requests.RequestException:
            r = None
        if intento < REINTENTOS - 1:
            time.sleep(2.0 * (intento + 1))

    if r is None:
        return [], f"{sid}: sin respuesta tras {REINTENTOS} intentos"
    if r.status_code != 200:
        return [], f"{sid}: HTTP {r.status_code}"

    crudos = _extraer_bloques_json(r.text, sid)
    if not crudos:
        # Diagnostico diferenciado: distinguir estacion inactiva de un cambio
        # de estructura del sitio. Si la pagina existe pero no trae ninguna
        # observacion de NINGUNA estacion, lo mas probable es que el
        # pluviometro no este reportando.
        hay_algun_dato = '"stationID":"' in r.text
        if hay_algun_dato:
            return [], (f"{sid}: la pagina cambio de estructura. "
                        "Revisar el extractor.")
        return [], (f"{sid}: sin observaciones publicadas "
                    "(estacion probablemente inactiva o en mantenimiento)")

    obs, vistos = [], set()
    for o in crudos:
        n = _normalizar(o, sid)
        if n and n["epoch"] not in vistos:
            vistos.add(n["epoch"])
            obs.append(n)
    obs.sort(key=lambda x: x["epoch"])
    return obs, f"{sid}: {len(obs)} observaciones"


def cosechar_red(con: sqlite3.Connection,
                 estaciones: list[dict] = None,
                 pausa: float = PAUSA_ENTRE_ESTACIONES) -> dict:
    """
    Recorre toda la red, descarga por scraping y persiste en la base de datos.
    Esta es la funcion que debe llamarse periodicamente.
    Devuelve un resumen con nuevos registros y mensajes por estacion.
    """
    estaciones = estaciones or ESTACIONES_WU
    sesion = requests.Session()
    resumen = {"nuevos": 0, "mensajes": [], "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

    for k, est in enumerate(estaciones):
        obs, msg = descargar_estacion_scraping(est["id"], sesion)
        resumen["mensajes"].append(msg)
        if obs:
            nuevos = guardar_observaciones(con, obs)
            resumen["nuevos"] += nuevos
            ultimo = obs[-1]
            actualizar_estacion(con, est["id"], est["nombre"],
                                ultimo.get("lat"), ultimo.get("lon"))
        # Cortesia: no martillear el servidor
        if k < len(estaciones) - 1:
            time.sleep(pausa)

    registrar_bitacora(con, "cosecha",
                       f"nuevos={resumen['nuevos']} | " + " ; ".join(resumen["mensajes"]))
    return resumen


# =============================================================================
# 4. ADAPTADOR B: API OFICIAL WU PWS  (activar cuando exista la apiKey)
# =============================================================================
WU_API = "https://api.weather.com/v2/pws"


def descargar_estacion_api(sid: str, api_key: str) -> tuple[list[dict], str]:
    """
    Misma salida que el adaptador de scraping, pero via API oficial.
    Trae el historial horario de 7 dias, que es mas completo y estable.
    """
    try:
        r = requests.get(f"{WU_API}/observations/hourly/7day",
                         params={"stationId": sid, "format": "json",
                                 "units": "m", "apiKey": api_key},
                         timeout=TIMEOUT)
    except requests.RequestException as e:
        return [], f"{sid}: error de red ({e.__class__.__name__})"
    if r.status_code != 200:
        return [], f"{sid}: HTTP {r.status_code}"

    js = r.json()
    obs = []
    for o in js.get("observations", []):
        n = _normalizar(o, sid)
        if n:
            n["fuente"] = "api_wu"
            obs.append(n)
    obs.sort(key=lambda x: x["epoch"])
    return obs, f"{sid}: {len(obs)} observaciones (API)"


def cosechar_red_api(con: sqlite3.Connection, api_key: str,
                     estaciones: list[dict] = None) -> dict:
    """Version API de `cosechar_red`. Interfaz identica."""
    estaciones = estaciones or ESTACIONES_WU
    resumen = {"nuevos": 0, "mensajes": [],
               "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for est in estaciones:
        obs, msg = descargar_estacion_api(est["id"], api_key)
        resumen["mensajes"].append(msg)
        if obs:
            resumen["nuevos"] += guardar_observaciones(con, obs)
            actualizar_estacion(con, est["id"], est["nombre"],
                                obs[-1].get("lat"), obs[-1].get("lon"))
    registrar_bitacora(con, "cosecha_api", f"nuevos={resumen['nuevos']}")
    return resumen


# =============================================================================
# 5. CONSULTA HIDROLOGICA SOBRE LA BASE DE DATOS
# =============================================================================
def lluvia_horaria_desde_db(con: sqlite3.Connection, sid: str,
                            dias: int = 10) -> pd.DataFrame:
    """
    Reconstruye la LAMINA HORARIA (mm caidos en cada hora) de una estacion a
    partir del acumulado diario `precip_total_mm` almacenado.

    Fundamento: el pluviometro reporta un acumulado que se reinicia cada dia a
    medianoche local. La lluvia de un intervalo es la DIFERENCIA del acumulado
    dentro del mismo dia. Las diferencias negativas corresponden al reinicio del
    contador (o a ruido del sensor) y se anulan.

    Devuelve DataFrame [fecha (UTC), mm].
    """
    desde = int(time.time()) - dias * 86400
    q = """SELECT epoch, obs_utc, precip_total_mm
           FROM observaciones
           WHERE station_id = ? AND epoch >= ? AND precip_total_mm IS NOT NULL
           ORDER BY epoch"""
    df = pd.read_sql_query(q, con, params=(sid, desde))
    if df.empty:
        return pd.DataFrame(columns=["fecha", "mm"])

    df["fecha"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    # Dia LOCAL de Guatemala (UTC-6): el contador se reinicia en hora local
    df["dia_local"] = (df["fecha"] - pd.Timedelta(hours=6)).dt.date
    df["mm"] = df.groupby("dia_local")["precip_total_mm"].diff()
    # Primer registro de cada dia: el acumulado ya es la lluvia caida
    primeras = df.groupby("dia_local").head(1).index
    df.loc[primeras, "mm"] = df.loc[primeras, "precip_total_mm"]
    df["mm"] = df["mm"].clip(lower=0)

    horaria = (df.set_index("fecha")["mm"].resample("1h").sum()
                 .reset_index().rename(columns={"mm": "mm"}))
    return horaria


def acumulados_estacion(con: sqlite3.Connection, sid: str) -> dict:
    """
    Acumulados multi-duracion (1, 3, 6, 24 h) y antecedente de 5 dias para una
    estacion, calculados sobre la serie almacenada. Insumo directo del motor de
    umbrales Intensidad-Duracion del SAT.
    """
    h = lluvia_horaria_desde_db(con, sid, dias=10)
    vacio = {"1h": 0.0, "3h": 0.0, "6h": 0.0, "24h": 0.0, "ant5d": 0.0}
    if h.empty:
        return vacio
    s = h.set_index("fecha")["mm"].fillna(0.0)
    ahora = s.index.max()

    def suma(desde_h, hasta_h=0):
        ini = ahora - pd.Timedelta(hours=desde_h - 1 if hasta_h == 0 else desde_h)
        fin = ahora - pd.Timedelta(hours=hasta_h)
        return float(s.loc[ini:fin].sum())

    return {
        "1h": suma(1), "3h": suma(3), "6h": suma(6), "24h": suma(24),
        # Antecedente: los 5 dias PREVIOS a las ultimas 24 h
        "ant5d": float(s.loc[ahora - pd.Timedelta(hours=144):
                             ahora - pd.Timedelta(hours=24)].sum()),
    }


def estado_red(con: sqlite3.Connection) -> list[dict]:
    """
    Estado consolidado de toda la red: metadatos, coordenadas, acumulados y
    ultima observacion. Es la estructura que consume el motor del SAT.
    """
    filas = con.execute(
        "SELECT station_id, nombre, lat, lon FROM estaciones").fetchall()
    estados = []
    for sid, nombre, lat, lon in filas:
        if lat is None or lon is None:
            continue
        ult = con.execute(
            """SELECT obs_utc, precip_rate_mmh, precip_total_mm, temp_c, humedad
               FROM observaciones WHERE station_id = ?
               ORDER BY epoch DESC LIMIT 1""", (sid,)).fetchone()
        estados.append({
            "id": sid, "nombre": nombre, "lat": lat, "lon": lon,
            "acum": acumulados_estacion(con, sid),
            "obs_utc": ult[0] if ult else None,
            "precip_rate": ult[1] if ult else None,
            "precip_total": ult[2] if ult else None,
            "temp": ult[3] if ult else None,
            "hum": ult[4] if ult else None,
        })
    return estados


def resumen_base(con: sqlite3.Connection) -> dict:
    """Metricas de la base: util para el panel de diagnostico y el informe."""
    n = con.execute("SELECT COUNT(*) FROM observaciones").fetchone()[0]
    est = con.execute("SELECT COUNT(*) FROM estaciones").fetchone()[0]
    rango = con.execute(
        "SELECT MIN(epoch), MAX(epoch) FROM observaciones").fetchone()
    ini = (dt.datetime.fromtimestamp(rango[0], dt.timezone.utc).isoformat(timespec="minutes")
           if rango[0] else None)
    fin = (dt.datetime.fromtimestamp(rango[1], dt.timezone.utc).isoformat(timespec="minutes")
           if rango[1] else None)
    dias = round((rango[1] - rango[0]) / 86400, 2) if rango[0] and rango[1] else 0
    return {"observaciones": n, "estaciones": est,
            "desde_utc": ini, "hasta_utc": fin, "dias_serie": dias}


def exportar_csv(con: sqlite3.Connection, ruta: str | Path = "sat_quiscab_export.csv") -> Path:
    """
    Exporta toda la serie a CSV. IMPRESCINDIBLE en Streamlit Cloud, cuyo disco es
    efimero: descargar periodicamente preserva la serie historica que se va
    construyendo y que servira para calibrar los umbrales.
    """
    df = pd.read_sql_query("SELECT * FROM observaciones ORDER BY station_id, epoch", con)
    ruta = Path(ruta)
    df.to_csv(ruta, index=False)
    return ruta


# =============================================================================
# 6. EJECUCION AUTONOMA  (para cron, GitHub Actions o prueba manual)
# =============================================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Cosecha de la red de estaciones del SAT Quiscab")
    ap.add_argument("--db", default=str(RUTA_DB_DEFECTO), help="ruta de la base SQLite")
    ap.add_argument("--api-key", default=None, help="apiKey de WU PWS (usa la API en vez de scraping)")
    ap.add_argument("--exportar", action="store_true", help="exporta la serie a CSV al terminar")
    args = ap.parse_args()

    con = conectar(args.db)
    if args.api_key:
        res = cosechar_red_api(con, args.api_key)
    else:
        res = cosechar_red(con)

    print(f"\n[{res['ts']}] Registros nuevos: {res['nuevos']}")
    for m in res["mensajes"]:
        print("  -", m)
    print("\nEstado de la base:", resumen_base(con))
    if args.exportar:
        print("Exportado a:", exportar_csv(con))
