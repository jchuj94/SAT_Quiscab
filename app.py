#!/usr/bin/env python3
"""
================================================================================
 SISTEMA DE ALERTA TEMPRANA (SAT) EN TIEMPO REAL - SUBCUENCA DEL RIO QUISCAB
 Solola, Guatemala | Principal afluente del Lago de Atitlan
================================================================================

Actividad Semana 3+ (MCHV-513) - Evolucion del dashboard hidroclimatico:
de una climatologia satelital (CHIRPS) a un SAT operativo alimentado por una
red de estaciones meteorologicas en tiempo real.

Autor : Ing. Agr. Jose Faustino Chuj Matul  (Green Solutions)

--------------------------------------------------------------------------------
CAMBIO DE PARADIGMA RESPECTO A LA VERSION ANTERIOR
--------------------------------------------------------------------------------
La version 1 disparaba el semaforo con CHIRPS (UCSB-CHG/CHIRPS/DAILY). CHIRPS
es un excelente producto CLIMATOLOGICO pero su version "final" tiene una latencia
de ~4-6 semanas y la "preliminar" de ~2-5 dias: inservible como GATILLO operativo.

Esta version 2 separa dos funciones que antes estaban mezcladas:

  (A) CLIMATOLOGIA  -> CHIRPS 1981-2025 sigue definiendo los percentiles de
      referencia (P90/P95/P99) de largo plazo. Es memoria, no gatillo.

  (B) GATILLO OPERATIVO -> red de estaciones (Weather Underground PWS / Ambient
      Weather) consultada por API cada pocos minutos. Interpolacion a la cuenca,
      acumulados multi-duracion y umbrales Intensidad-Duracion (I-D).

--------------------------------------------------------------------------------
MARCO METODOLOGICO Y RESPALDO ACADEMICO
--------------------------------------------------------------------------------
El motor del SAT usa el enfoque de UMBRALES EMPIRICOS DE INTENSIDAD-DURACION,
estandar internacional para alerta por lluvia de crecidas repentinas y
deslizamientos:

  - Caine, N. (1980). The rainfall intensity-duration control of shallow
    landslides and debris flows. Geografiska Annaler, 62A, 23-27.
  - Guzzetti, F., Peruccacci, S., Rossi, M., Stark, C.P. (2007/2008). The
    rainfall intensity-duration control of shallow landslides and debris flows.
    Landslides, 5, 3-17.
  - Brunetti, M.T. et al. (2010). Rainfall thresholds for the possible
    occurrence of landslides in Italy. NHESS, 10, 447-458.
  - Guzzetti, F. et al. (2020). Geographical landslide early warning systems.
    Earth-Science Reviews, 200, 102973.
  - WMO (2018). Guide to Hydrological Practices (WMO-No.168) - interpolacion
    areal de precipitacion con pocas estaciones (Thiessen).

Interpolacion: con 4-6 pluviometros se usan POLIGONOS DE THIESSEN (media areal
ponderada por area de influencia) e IDW para la superficie visual. El Kriging se
descarta de forma explicita: el variograma no es fiable con tan pocos puntos.

--------------------------------------------------------------------------------
DEPENDENCIAS  (requirements.txt)
--------------------------------------------------------------------------------
    streamlit>=1.36
    earthengine-api
    folium
    streamlit-folium
    numpy
    pandas
    plotly
    shapely
    requests
    matplotlib
    streamlit-autorefresh      # opcional (hay fallback si no esta)

--------------------------------------------------------------------------------
CONFIGURACION DE SECRETOS  (.streamlit/secrets.toml)
--------------------------------------------------------------------------------
    # Llave de la API de Weather Underground PWS (gratuita para duenos de PWS,
    # 1500 llamadas/dia). Vivamos Mejor puede generarla en su cuenta WU.
    wu_api_key = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

    # (Opcional) Credenciales Ambient Weather si se quieren integrar CEDRACC y
    # Chuacruz. Se obtienen en la cuenta Ambient del propietario.
    ambient_application_key = "..."
    ambient_api_key         = "..."

    # Cuenta de servicio de Google Earth Engine (igual que la version anterior).
    [gee_service_account]
    client_email = "..."
    project_id   = "..."
    private_key  = "..."
    # ... resto de campos del JSON de la service account ...
================================================================================
"""

from __future__ import annotations

import os
import json
import time
import shutil
import tempfile
import datetime as dt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import ee
import folium
from streamlit_folium import st_folium
from shapely.geometry import shape, Point, Polygon, MultiPolygon

# Capa de adquisicion y persistencia (scraping + base de datos SQLite)
import sat_datos as sd

# Autorefresco opcional: si el paquete no esta instalado, se usa un fallback
try:
    from streamlit_autorefresh import st_autorefresh
    _TIENE_AUTOREFRESH = True
except Exception:
    _TIENE_AUTOREFRESH = False


# =============================================================================
# 1. CONFIGURACION GENERAL
# =============================================================================
st.set_page_config(
    page_title="SAT Quiscab - Tiempo real",
    page_icon="🌧️",
    layout="wide",
)

# --- Google Earth Engine (solo para geometria de cuenca + climatologia CHIRPS)
SA_EMAIL = "service-ee-cydata@ee-cydata.iam.gserviceaccount.com"
JSON_PATH = "/content/drive/MyDrive/gee_keys/service-key.json"
PROYECTO_GEE = "ee-josechujmatul"

# Punto de anclaje dentro de la subcuenca del Quiscab (Solola), respaldo si no
# hay vector oficial de la cuenca.
PUNTO_QUISCAB = [-91.190, 14.770]

# --- Parametros operativos del SAT ------------------------------------------
BUFFER_CUENCA_KM = 5.0        # estaciones dentro de este halo entran al analisis
IDW_POTENCIA = 2.0            # exponente de la distancia en IDW (2 = clasico)
GRID_N = 140                  # resolucion de la malla de interpolacion (celdas)
TTL_TIEMPO_REAL = 600         # segundos de cache para datos en vivo (10 min)
REFRESCO_MS = 15 * 60 * 1000  # autorefresco de la pagina (15 min)
# --- Rutas de la base de datos -------------------------------------------
# En Streamlit Community Cloud el repositorio se monta en SOLO LECTURA, por lo
# que no se puede escribir directamente sobre el archivo versionado. Se usan
# dos copias con funciones distintas:
#   RUTA_DB_REPO    : copia canonica, versionada por GitHub Actions. Es el
#                     archivo historico acumulado; en la nube es de solo lectura.
#   RUTA_DB_TRABAJO : copia efimera y escribible donde la app registra las
#                     cosechas de la sesion. Se siembra con la copia del repo.
# En ejecucion local, si el directorio es escribible, se usa el archivo del repo
# directamente y no hace falta la copia.
RUTA_DB_REPO = "sat_quiscab.db"
RUTA_DB_TRABAJO = os.path.join(tempfile.gettempdir(), "sat_quiscab.db")
MIN_ENTRE_COSECHAS = 10       # minutos minimos entre consultas a la red

# Fuente de datos: "scraping" (sin llave) o "api" (requiere wu_api_key).
# Cambiar esta linea es todo lo necesario para migrar a la API oficial.
MODO_FUENTE = "scraping"

# --- API Weather Underground PWS --------------------------------------------
WU_BASE = "https://api.weather.com/v2/pws"

# Registro de estaciones de la red de Vivamos Mejor / DAT.
# Las coordenadas se obtienen AUTOMATICAMENTE de la API; aqui solo el catalogo.
# 'red' controla que adaptador se usa. Las Ambient requieren llaves del dueno.
ESTACIONES = [
    {"id": "ISOLOL4",   "red": "wu", "nombre": "EFA Solola"},
    {"id": "IPANAJ5",   "red": "wu", "nombre": "Oficina Central Panajachel"},
    {"id": "ISANAN84",  "red": "wu", "nombre": "Lomas de Atitlan, San Andres Semetabaj"},
    {"id": "ISANTA535", "red": "wu", "nombre": "Casa Santa Rita, Santa Lucia Utatlan"},
    {"id": "ISANTACL3", "red": "wu", "nombre": "Santa Clara La Laguna"},
    {"id": "ISANTI135", "red": "wu", "nombre": "CoAtitlan, Santiago Atitlan"},
    # --- Ambient Weather (opcionales; requieren application_key + api_key) ---
    # {"id": "MAC_CEDRACC", "red": "ambient", "nombre": "CEDRACC Santa Cruz La Laguna"},
]


def _wu_api_key() -> Optional[str]:
    """Devuelve la API key de WU desde Secrets o variable de entorno."""
    if "wu_api_key" in st.secrets:
        return st.secrets["wu_api_key"]
    return os.environ.get("WU_API_KEY")


# =============================================================================
# 2. EARTH ENGINE  (geometria de la cuenca + climatologia de referencia)
# =============================================================================
@st.cache_resource(show_spinner="Conectando con Google Earth Engine...")
def inicializar_gee() -> str:
    """
    Inicializa Earth Engine. Prioriza Secrets (Streamlit Cloud), luego la llave
    JSON en Drive (Colab) y por ultimo credenciales de usuario (local).
    GEE aqui es OPCIONAL: solo aporta la geometria de la cuenca y la
    climatologia CHIRPS. El SAT dispara con las estaciones, no con GEE.
    """
    try:
        if "gee_service_account" in st.secrets:
            info = dict(st.secrets["gee_service_account"])
            cred = ee.ServiceAccountCredentials(
                info["client_email"], key_data=json.dumps(info))
            ee.Initialize(cred, project=info.get("project_id", PROYECTO_GEE))
            return "secrets"
        if os.path.exists(JSON_PATH):
            cred = ee.ServiceAccountCredentials(SA_EMAIL, JSON_PATH)
            ee.Initialize(cred, project=PROYECTO_GEE)
            return "service_account"
        ee.Initialize(project=PROYECTO_GEE)
        return "usuario"
    except Exception as e:
        st.warning(f"Earth Engine no disponible ({e}). "
                   "El mapa de relieve y la climatologia CHIRPS quedaran "
                   "deshabilitados, pero el SAT en tiempo real sigue operando.")
        return "sin_gee"


@st.cache_resource(show_spinner="Delimitando la subcuenca del Quiscab...")
def cargar_cuenca(_modo_gee: str):
    """
    Devuelve (geometria_ee_o_None, shapely_polygon, fuente_texto).
    Prioriza el vector oficial (GeoJSON local); si no, usa HydroBASINS via GEE.
    El poligono shapely se usa para el filtrado de estaciones y la interpolacion
    sin depender de GEE.
    """
    ruta_geojson = "cuenca_quiscab.geojson"
    if os.path.exists(ruta_geojson):
        with open(ruta_geojson) as f:
            gj = json.load(f)
        # Toma el primer feature/geometry del GeoJSON
        geom = gj["features"][0]["geometry"] if "features" in gj else gj
        poly = shape(geom)
        geom_ee = ee.Geometry(geom) if _modo_gee != "sin_gee" else None
        return geom_ee, poly, "vector oficial (GeoJSON)"

    if _modo_gee == "sin_gee":
        # Sin GEE y sin GeoJSON: cuenca aproximada como circulo alrededor del
        # punto de anclaje (respaldo de ultima instancia).
        c = Point(PUNTO_QUISCAB).buffer(0.06)  # ~6.6 km de radio
        return None, c, "aproximacion circular (sin GEE ni vector)"

    punto = ee.Geometry.Point(PUNTO_QUISCAB)
    cuenca = (ee.FeatureCollection("WWF/HydroSHEDS/v1/Basins/hybas_12")
                .filterBounds(punto).first())
    geom_ee = cuenca.geometry()
    poly = shape(geom_ee.getInfo())
    return geom_ee, poly, "HydroBASINS hybas_12 (anzuelo espacial)"


@st.cache_data(show_spinner="Calculando climatologia CHIRPS 1981-2025...")
def climatologia_chirps(_geom_ee_json: str) -> dict:
    """
    Percentiles climatologicos de referencia (P90/P95/P99) de la lluvia diaria
    y P95 del acumulado movil de 5 dias, sobre CHIRPS 1981-2025.
    NOTA METODOLOGICA: son referencia de LARGO PLAZO, no el gatillo operativo.
    Se recibe la geometria serializada para poder cachear.
    """
    geom = ee.Geometry(json.loads(_geom_ee_json))
    chirps = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                .filterDate("1981-01-01", "2025-12-31")
                .select("precipitation"))

    def media_areal(imagen):
        media = imagen.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=1000, maxPixels=1e9)
        return ee.Feature(None, {
            "t": imagen.get("system:time_start"),
            "mm": media.get("precipitation")})

    fc = ee.FeatureCollection(chirps.map(media_areal))
    datos = fc.reduceColumns(ee.Reducer.toList(2), ["t", "mm"]).get("list").getInfo()
    df = pd.DataFrame(datos, columns=["t", "mm"])
    df["mm"] = pd.to_numeric(df["mm"], errors="coerce")
    df = df.dropna().sort_values("t").reset_index(drop=True)
    df["acum5"] = df["mm"].rolling(5, min_periods=1).sum()
    lluvia = df.loc[df["mm"] > 1, "mm"]
    return {
        "p90": float(lluvia.quantile(0.90)),
        "p95": float(lluvia.quantile(0.95)),
        "p99": float(lluvia.quantile(0.99)),
        "acum5_p95": float(df["acum5"].quantile(0.95)),
    }


# =============================================================================
# 3. CLIENTE DE DATOS EN TIEMPO REAL  (Weather Underground PWS)
# =============================================================================
def _wu_get(endpoint: str, params: dict) -> Optional[dict]:
    """Llamada generica a la API WU PWS con manejo de errores."""
    key = _wu_api_key()
    if not key:
        return None
    params = {**params, "format": "json", "units": "m", "apiKey": key}
    try:
        r = requests.get(f"{WU_BASE}/{endpoint}", params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        return None
    except requests.RequestException:
        return None


@st.cache_data(ttl=TTL_TIEMPO_REAL, show_spinner=False)
def wu_actual(station_id: str) -> Optional[dict]:
    """
    Observacion actual de una estacion PWS.
    Devuelve dict con lat, lon, elev, precip_rate (mm/h), precip_total (mm dia),
    temp, hum y marca de tiempo. None si no hay dato.
    """
    js = _wu_get("observations/current", {"stationId": station_id})
    if not js or "observations" not in js or not js["observations"]:
        return None
    o = js["observations"][0]
    m = o.get("metric", {}) or {}
    return {
        "id": station_id,
        "lat": o.get("lat"),
        "lon": o.get("lon"),
        "elev_m": m.get("elev"),
        "precip_rate": m.get("precipRate"),    # mm/h instantaneo
        "precip_total": m.get("precipTotal"),  # mm acumulado del dia
        "temp": m.get("temp"),
        "hum": o.get("humidity"),
        "obs_utc": o.get("obsTimeUtc"),
        "obs_local": o.get("obsTimeLocal"),
    }


@st.cache_data(ttl=TTL_TIEMPO_REAL, show_spinner=False)
def wu_historial_horario(station_id: str) -> Optional[pd.DataFrame]:
    """
    Historial HORARIO de los ultimos 7 dias de una estacion PWS.
    Devuelve DataFrame [fecha_utc, precip_total_mm]. Este es el insumo para los
    acumulados multi-duracion (1h, 3h, 6h, 24h) y el antecedente de 5 dias.
    """
    js = _wu_get("observations/hourly/7day", {"stationId": station_id})
    if not js or "observations" not in js or not js["observations"]:
        return None
    filas = []
    for o in js["observations"]:
        m = o.get("metric", {}) or {}
        filas.append({
            "fecha": pd.to_datetime(o.get("obsTimeUtc"), utc=True),
            # precipTotal en historial horario = acumulado del dia hasta esa
            # hora; se diferencia despues para obtener la lluvia por hora.
            "precip_total_dia": m.get("precipTotal"),
            "precip_rate": m.get("precipRate"),
        })
    df = pd.DataFrame(filas).dropna(subset=["fecha"]).sort_values("fecha")
    df["precip_total_dia"] = pd.to_numeric(df["precip_total_dia"], errors="coerce")
    return df.reset_index(drop=True)


def lluvia_horaria(df_hist: pd.DataFrame) -> pd.DataFrame:
    """
    Convierte el acumulado-del-dia horario en lluvia POR HORA.
    Como precipTotal se reinicia cada dia a medianoche local, se diferencia
    dentro de cada dia y se corrigen los reinicios (diferencias negativas).
    Devuelve [fecha, mm] con la lamina caida en cada hora.
    """
    if df_hist is None or df_hist.empty:
        return pd.DataFrame(columns=["fecha", "mm"])
    d = df_hist.copy()
    d["dia"] = d["fecha"].dt.date
    d["mm"] = d.groupby("dia")["precip_total_dia"].diff()
    # Primer registro de cada dia: la lluvia es el propio acumulado inicial.
    primeras = d.groupby("dia").head(1).index
    d.loc[primeras, "mm"] = d.loc[primeras, "precip_total_dia"]
    # Reinicios o ruido -> no negativos.
    d["mm"] = d["mm"].clip(lower=0)
    return d[["fecha", "mm"]].dropna().reset_index(drop=True)


def acumulados_multiduracion(df_horaria: pd.DataFrame) -> dict:
    """
    Acumulados moviles al instante mas reciente para las duraciones clave del
    enfoque Intensidad-Duracion, mas el antecedente de 5 dias.
    """
    if df_horaria is None or df_horaria.empty:
        return {"1h": 0.0, "3h": 0.0, "6h": 0.0, "24h": 0.0, "ant5d": 0.0}
    s = (df_horaria.set_index("fecha")["mm"]
                   .resample("1h").sum().fillna(0.0))
    ahora = s.index.max()
    def suma(horas):
        return float(s.loc[ahora - pd.Timedelta(hours=horas - 1): ahora].sum())
    return {
        "1h":  suma(1),
        "3h":  suma(3),
        "6h":  suma(6),
        "24h": suma(24),
        # Antecedente: lluvia de los 5 dias PREVIOS a las ultimas 24 h.
        "ant5d": float(s.loc[ahora - pd.Timedelta(hours=24 + 120):
                             ahora - pd.Timedelta(hours=24)].sum()),
    }


# =============================================================================
# 4. CAPA ESPACIAL  (filtrado, Thiessen e IDW)
# =============================================================================
def _poly_exterior(poly) -> np.ndarray:
    """Devuelve los vertices exteriores (lon,lat) del poligono/multipoligono."""
    if isinstance(poly, MultiPolygon):
        poly = max(poly.geoms, key=lambda g: g.area)
    return np.array(poly.exterior.coords)


def estaciones_en_cuenca(estados: list[dict], poly, buffer_km: float) -> list[dict]:
    """
    Filtra las estaciones que caen dentro de la cuenca o de su halo (buffer).
    Marca cada estado con 'dentro' (True/False) y conserva solo las relevantes.
    El buffer en grados se aproxima segun la latitud media de la cuenca.
    """
    lat0 = _poly_exterior(poly)[:, 1].mean()
    buffer_deg = buffer_km / (111.0 * np.cos(np.radians(lat0)))
    zona = poly.buffer(buffer_deg)
    relevantes = []
    for e in estados:
        if e.get("lat") is None or e.get("lon") is None:
            continue
        p = Point(e["lon"], e["lat"])
        if zona.contains(p) or zona.intersects(p):
            e["dentro"] = poly.contains(p)
            relevantes.append(e)
    return relevantes


def _malla_cuenca(poly, n: int = GRID_N):
    """
    Malla regular que cubre la cuenca; devuelve coords de celdas internas,
    su peso de area (correccion por latitud) y la extension [W,E,S,N].
    """
    minx, miny, maxx, maxy = poly.bounds
    xs = np.linspace(minx, maxx, n)
    ys = np.linspace(miny, maxy, n)
    XX, YY = np.meshgrid(xs, ys)
    mask = np.zeros(XX.shape, dtype=bool)
    # prepared geometry para acelerar el point-in-polygon
    from shapely.prepared import prep
    pp = prep(poly)
    for j in range(XX.shape[0]):
        for i in range(XX.shape[1]):
            if pp.contains(Point(XX[j, i], YY[j, i])):
                mask[j, i] = True
    peso = np.cos(np.radians(YY))  # area diferencial ~ cos(lat)
    return XX, YY, mask, peso, [minx, maxx, miny, maxy]


def pesos_thiessen(estaciones_val: list[dict], poly) -> dict:
    """
    Pondera cada estacion por su AREA DE INFLUENCIA (poligonos de Thiessen)
    dentro de la cuenca. Devuelve {id: peso_normalizado}. Es la base de la
    media areal recomendada por la OMM para pocas estaciones.
    """
    XX, YY, mask, peso, _ = _malla_cuenca(poly)
    lat0 = np.radians(YY[mask].mean())
    ptos = np.array([[e["lon"], e["lat"]] for e in estaciones_val])
    ids = [e["id"] for e in estaciones_val]
    # distancia euclidiana corregida por latitud (grados -> ~km relativos)
    gx = (XX[mask] * np.cos(lat0))
    gy = YY[mask]
    sx = (ptos[:, 0] * np.cos(lat0))
    sy = ptos[:, 1]
    d = np.sqrt((gx[:, None] - sx[None, :]) ** 2 +
                (gy[:, None] - sy[None, :]) ** 2)
    asignacion = d.argmin(axis=1)
    w_area = peso[mask]
    pesos = {}
    total = w_area.sum()
    for k, sid in enumerate(ids):
        pesos[sid] = float(w_area[asignacion == k].sum() / total) if total else 0.0
    return pesos


def media_areal_thiessen(estaciones_val: list[dict], pesos: dict, campo: str) -> float:
    """Media areal ponderada por Thiessen de un campo (ej. '24h')."""
    num = 0.0
    for e in estaciones_val:
        v = e["acum"].get(campo) if "acum" in e else e.get(campo)
        if v is not None:
            num += pesos.get(e["id"], 0.0) * float(v)
    return num


def superficie_idw(estaciones_val: list[dict], poly, campo: str,
                   potencia: float = IDW_POTENCIA):
    """
    Superficie interpolada por IDW (para visualizacion). Devuelve grid enmascarado
    y extension. Solo estetico/diagnostico: la cifra oficial es la de Thiessen.
    """
    XX, YY, mask, _, extent = _malla_cuenca(poly)
    lat0 = np.radians(YY[mask].mean())
    ptos = np.array([[e["lon"], e["lat"]] for e in estaciones_val])
    vals = np.array([float(e["acum"].get(campo, 0.0)) if "acum" in e
                     else float(e.get(campo, 0.0)) for e in estaciones_val])
    grid = np.full(XX.shape, np.nan)
    gx = XX[mask] * np.cos(lat0)
    gy = YY[mask]
    sx = ptos[:, 0] * np.cos(lat0)
    sy = ptos[:, 1]
    d = np.sqrt((gx[:, None] - sx[None, :]) ** 2 +
                (gy[:, None] - sy[None, :]) ** 2)
    d = np.where(d < 1e-9, 1e-9, d)
    w = 1.0 / d ** potencia
    interp = (w * vals[None, :]).sum(axis=1) / w.sum(axis=1)
    grid[mask] = interp
    return grid, extent


def grid_a_png(grid: np.ndarray, vmax: float) -> Optional[str]:
    """Colorea el grid IDW a un PNG base64 (transparente fuera de la cuenca)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.colors as mcolors
        import base64, io
    except Exception:
        return None
    norm = mcolors.Normalize(vmin=0, vmax=max(vmax, 1e-3))
    try:
        cmap = matplotlib.colormaps["YlGnBu"]          # matplotlib >= 3.7
    except Exception:
        import matplotlib.cm as cm
        cmap = cm.get_cmap("YlGnBu")                    # compat. versiones viejas
    rgba = cmap(norm(grid))
    rgba[np.isnan(grid)] = [0, 0, 0, 0]      # transparente fuera de la cuenca
    rgba[..., 3] = np.where(np.isnan(grid), 0, 0.65)
    from matplotlib import pyplot as plt
    buf = io.BytesIO()
    plt.imsave(buf, np.flipud(rgba), format="png")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# =============================================================================
# 5. MOTOR DE UMBRALES  (el "cerebro" del SAT: I-D + antecedente + climatologia)
# =============================================================================
#
# UMBRALES INTENSIDAD-DURACION (I-D) POR DEFECTO
# -----------------------------------------------------------------------------
# Valores SEMILLA parametrizables, no calibrados localmente todavia. Deben
# ajustarse cuando se compile el inventario de eventos historicos de la cuenca
# (inundaciones/deslizamientos del Quiscab; p.ej. Stan 2005 - Panabaj). Se
# expresan como lamina acumulada (mm) para cada ventana de duracion, en dos
# niveles: AVISO (vigilancia) y ALERTA (accion). Referencias: Caine (1980),
# Guzzetti et al. (2007/2008), umbrales operativos INSIVUMEH/CONRED para lluvias
# convectivas de montana en Guatemala (orden de magnitud).
#
UMBRALES_ID = {
    #  duracion : (mm AVISO/amarillo, mm ALERTA/rojo)
    "1h":  (20.0, 35.0),
    "3h":  (35.0, 60.0),
    "6h":  (50.0, 90.0),
    "24h": (75.0, 130.0),
}

# Antecedente de 5 dias (mm) que indica suelo saturado. Por encima de este valor
# el umbral efectivo de disparo se REDUCE por el factor de saturacion.
ANTECEDENTE_SATURACION = 60.0
FACTOR_SATURACION = 0.80   # los umbrales bajan al 80% con suelo saturado

# Codigos de nivel del semaforo (orden = severidad)
NIVELES = ["VERDE", "AMARILLO", "ROJO", "ROJO EXTREMO"]
COLORES = {"VERDE": "#1a9641", "AMARILLO": "#f4a300",
           "ROJO": "#d7191c", "ROJO EXTREMO": "#8b0000"}


def evaluar_id(acum: dict, antecedente: float,
               umbrales: dict = UMBRALES_ID) -> tuple[str, list[str]]:
    """
    Evalua los acumulados multi-duracion contra los umbrales I-D, modulando por
    el antecedente de 5 dias. Devuelve (nivel, motivos).
    Regla: se toma el nivel MAS severo alcanzado en cualquier duracion.
    """
    saturado = antecedente >= ANTECEDENTE_SATURACION
    factor = FACTOR_SATURACION if saturado else 1.0
    nivel = "VERDE"
    motivos = []
    orden = {n: i for i, n in enumerate(NIVELES)}
    for dur, (aviso, alerta) in umbrales.items():
        v = acum.get(dur, 0.0)
        a_eff, r_eff = aviso * factor, alerta * factor
        if v >= r_eff:
            cand, txt = "ROJO", f"{dur}={v:.0f}mm ≥ alerta {r_eff:.0f}mm"
        elif v >= a_eff:
            cand, txt = "AMARILLO", f"{dur}={v:.0f}mm ≥ aviso {a_eff:.0f}mm"
        else:
            continue
        motivos.append(txt + (" [suelo saturado]" if saturado else ""))
        if orden[cand] > orden[nivel]:
            nivel = cand
    # Escalada a ROJO EXTREMO: dos o mas duraciones en ROJO simultaneamente
    rojos = sum(1 for dur, (a, r) in umbrales.items()
                if acum.get(dur, 0.0) >= r * factor)
    if rojos >= 2:
        nivel = "ROJO EXTREMO"
        motivos.append(f"{rojos} ventanas en nivel ALERTA simultaneas")
    return nivel, motivos


def evaluar_climatologico(acum24: float, clim: Optional[dict]) -> tuple[str, str]:
    """
    Contraste con la climatologia CHIRPS 1981-2025: ubica el acumulado de 24 h
    frente a los percentiles historicos. Es una segunda opinion, no el gatillo.
    """
    if not clim:
        return "SIN DATO", "Climatologia CHIRPS no disponible."
    if acum24 >= clim["p99"]:
        return "P99", f"24h ({acum24:.0f}mm) supera el P99 historico ({clim['p99']:.0f}mm): evento raro."
    if acum24 >= clim["p95"]:
        return "P95", f"24h ({acum24:.0f}mm) entre P95 y P99 ({clim['p95']:.0f}-{clim['p99']:.0f}mm): significativo."
    if acum24 >= clim["p90"]:
        return "P90", f"24h ({acum24:.0f}mm) entre P90 y P95: moderadamente inusual."
    return "NORMAL", f"24h ({acum24:.0f}mm) por debajo del P90 climatologico."


def nivel_combinado(nivel_id: str, cat_clim: str) -> str:
    """
    Fusiona el gatillo operativo I-D (dominante) con la senal climatologica.
    Si la climatologia marca P99 y el I-D ya esta en ROJO, confirma EXTREMO.
    """
    orden = {n: i for i, n in enumerate(NIVELES)}
    nivel = nivel_id
    if cat_clim == "P99" and orden[nivel] >= orden["ROJO"]:
        nivel = "ROJO EXTREMO"
    return nivel


# =============================================================================
# 6. ORQUESTADOR  (reune estaciones, acumulados e interpolacion)
# =============================================================================
def _directorio_escribible(ruta_dir: str = ".") -> bool:
    """Comprueba si se puede escribir en el directorio (falla en Streamlit Cloud)."""
    try:
        prueba = Path(ruta_dir) / ".sat_prueba_escritura"
        prueba.write_text("x")
        prueba.unlink()
        return True
    except Exception:
        return False


@st.cache_resource(show_spinner="Abriendo la base de datos del SAT...")
def abrir_db():
    """
    Devuelve (conexion, ruta_en_uso, modo).

    Estrategia de dos copias:
      - Si el directorio del repositorio es escribible (ejecucion local), se
        trabaja directamente sobre el archivo versionado.
      - Si no lo es (Streamlit Cloud), se copia el archivo del repositorio a un
        directorio temporal escribible y se trabaja sobre esa copia. La serie
        historica permanente la mantiene GitHub Actions en el repositorio.
    """
    if _directorio_escribible():
        return sd.conectar(RUTA_DB_REPO), RUTA_DB_REPO, "repositorio (escribible)"

    # Entorno de solo lectura: sembrar la copia de trabajo una sola vez
    if os.path.exists(RUTA_DB_REPO) and not os.path.exists(RUTA_DB_TRABAJO):
        try:
            shutil.copy2(RUTA_DB_REPO, RUTA_DB_TRABAJO)
        except Exception:
            pass
    return sd.conectar(RUTA_DB_TRABAJO), RUTA_DB_TRABAJO, "copia temporal (repo de solo lectura)"


def cosechar_si_toca(con, minutos: int = 10, forzar: bool = False) -> Optional[dict]:
    """
    Ejecuta la cosecha de la red solo si paso el intervalo minimo desde la
    ultima. Evita golpear el servidor en cada recarga de Streamlit.
    El control de frecuencia vive en st.session_state.
    """
    ahora = time.time()
    ultima = st.session_state.get("ultima_cosecha", 0.0)
    if not forzar and (ahora - ultima) < minutos * 60:
        return None
    api_key = _wu_api_key()
    try:
        with st.spinner("Actualizando datos de las estaciones..."):
            if api_key and MODO_FUENTE == "api":
                res = sd.cosechar_red_api(con, api_key)
            else:
                res = sd.cosechar_red(con)
    except Exception as e:
        # Un fallo de adquisicion o de escritura NUNCA debe tumbar el SAT: la
        # aplicacion sigue operando con los datos ya almacenados en la base.
        st.session_state["ultima_cosecha"] = ahora
        st.session_state["error_cosecha"] = f"{e.__class__.__name__}: {e}"
        return None
    st.session_state.pop("error_cosecha", None)
    st.session_state["ultima_cosecha"] = ahora
    st.session_state["ultimo_resumen"] = res
    return res


@st.cache_data(ttl=120, show_spinner=False)
def leer_estado_red(_con, _sello: float) -> list[dict]:
    """
    Estado consolidado de la red leido de la base de datos.
    `_sello` invalida la cache cuando hay una cosecha nueva.
    """
    return sd.estado_red(_con)


# =============================================================================
# 7. INTERFAZ - BARRA LATERAL
# =============================================================================
modo_gee = inicializar_gee()
geom_ee, poly_cuenca, fuente_cuenca = cargar_cuenca(modo_gee)

st.sidebar.title("⚙️ Panel de control del SAT")
st.sidebar.caption(f"GEE: {modo_gee}  |  Cuenca: {fuente_cuenca}")

buffer_km = st.sidebar.slider(
    "Halo de estaciones alrededor de la cuenca (km)",
    min_value=0.0, max_value=15.0, value=BUFFER_CUENCA_KM, step=1.0)

st.sidebar.markdown("### Umbrales Intensidad-Duracion (mm)")
st.sidebar.caption("Ajustables. Formato: aviso (amarillo) / alerta (rojo).")
umbrales_editables = {}
for dur, (aviso, alerta) in UMBRALES_ID.items():
    c1, c2 = st.sidebar.columns(2)
    a = c1.number_input(f"{dur} aviso", value=float(aviso), step=5.0, key=f"a_{dur}")
    r = c2.number_input(f"{dur} alerta", value=float(alerta), step=5.0, key=f"r_{dur}")
    umbrales_editables[dur] = (a, r)

st.sidebar.markdown("---")
if _TIENE_AUTOREFRESH:
    auto = st.sidebar.checkbox("Autorefresco cada 15 min", value=True)
    if auto:
        st_autorefresh(interval=REFRESCO_MS, key="sat_refresh")
else:
    st.sidebar.caption("Instala `streamlit-autorefresh` para refresco automatico.")
if st.sidebar.button("🔄 Actualizar ahora"):
    st.session_state["forzar_cosecha"] = True
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Fuente de datos: **{MODO_FUENTE}**"
                   + ("" if MODO_FUENTE == "api" else " (sin API key)"))

st.sidebar.markdown("---")
st.sidebar.caption(
    "Datos en vivo: red PWS Weather Underground (Vivamos Mejor / DAT). "
    "Climatologia: CHIRPS 1981-2025 via Earth Engine.")


# =============================================================================
# 8. INTERFAZ - ENCABEZADO Y ESTADO GLOBAL
# =============================================================================
st.title("🌧️ Sistema de Alerta Temprana en Tiempo Real - Rio Quiscab")
st.caption("Solola, Guatemala  |  Principal afluente del Lago de Atitlan  |  "
           "Gatillo: estaciones PWS en vivo  ·  Referencia: CHIRPS + SRTM")

# Base de datos y cosecha de la red
con_db, ruta_db_activa, modo_db = abrir_db()
resumen_cosecha = cosechar_si_toca(con_db, MIN_ENTRE_COSECHAS,
                                   forzar=st.session_state.pop("forzar_cosecha", False))

if st.session_state.get("error_cosecha"):
    st.warning(
        "No se pudo completar la ultima actualizacion en vivo: "
        f"`{st.session_state['error_cosecha']}`. El SAT continua operando con "
        "los datos ya almacenados en la base. Revisa la pestana Base de datos.")

# Estado de la red leido de la base de datos
sello = st.session_state.get("ultima_cosecha", 0.0)
estados_todos = leer_estado_red(con_db, sello)

if not estados_todos:
    st.error(
        "No hay datos en la base todavia. Pulsa **Actualizar ahora** en el panel "
        "lateral para ejecutar la primera cosecha de la red. Si el problema "
        "persiste, revisa la conectividad o consulta la pestana de Diagnostico.")
    st.stop()

# Filtrado espacial: estaciones dentro de la cuenca o de su halo
estados = estaciones_en_cuenca(estados_todos, poly_cuenca, buffer_km)

if not estados:
    st.warning(
        f"Hay {len(estados_todos)} estaciones con datos, pero ninguna cae dentro "
        f"de la cuenca ni de su halo de {buffer_km:.0f} km. Amplia el halo en el "
        "panel lateral.")
    st.stop()

# Interpolacion areal (Thiessen) para cada duracion
pesos = pesos_thiessen(estados, poly_cuenca)
areal = {d: media_areal_thiessen(estados, pesos, d)
         for d in ["1h", "3h", "6h", "24h", "ant5d"]}

# Climatologia de referencia (si hay GEE)
clim = None
if geom_ee is not None:
    try:
        clim = climatologia_chirps(json.dumps(geom_ee.getInfo()))
    except Exception:
        clim = None

# Evaluacion del semaforo sobre la MEDIA AREAL de la cuenca
nivel_id, motivos = evaluar_id(areal, areal["ant5d"], umbrales_editables)
cat_clim, txt_clim = evaluar_climatologico(areal["24h"], clim)
nivel = nivel_combinado(nivel_id, cat_clim)
color = COLORES[nivel]


# =============================================================================
# 9. INTERFAZ - INDICADORES (KPIs) Y BANNER DE ALERTA
# =============================================================================
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Lluvia 1 h (areal)", f"{areal['1h']:.1f} mm",
          f"alerta {umbrales_editables['1h'][1]:.0f} mm")
c2.metric("Lluvia 6 h (areal)", f"{areal['6h']:.1f} mm",
          f"alerta {umbrales_editables['6h'][1]:.0f} mm")
c3.metric("Lluvia 24 h (areal)", f"{areal['24h']:.1f} mm",
          f"alerta {umbrales_editables['24h'][1]:.0f} mm")
c4.metric("Antecedente 5 d", f"{areal['ant5d']:.1f} mm",
          f"saturacion {ANTECEDENTE_SATURACION:.0f} mm")
c5.metric("Estaciones activas", f"{len(estados)}",
          f"{sum(1 for e in estados if e.get('dentro'))} dentro")

st.markdown(
    f"<div style='padding:14px;border-radius:10px;background:{color};"
    f"color:white;text-align:center;font-size:24px;font-weight:bold'>"
    f"ALERTA {nivel}</div>", unsafe_allow_html=True)

if motivos:
    st.markdown("**Motivos del gatillo (Intensidad-Duracion):** " +
                "  ·  ".join(motivos))
else:
    st.markdown("**Sin superacion de umbrales I-D en la media areal.**")
st.caption(f"Contraste climatologico CHIRPS: {txt_clim}")

# Marca de tiempo del dato mas reciente
try:
    ultima = max([pd.to_datetime(e["obs_utc"]) for e in estados if e.get("obs_utc")])
    st.caption(f"🕒 Observacion mas reciente (UTC): {ultima}  |  "
               f"Cache: {TTL_TIEMPO_REAL // 60} min.")
except Exception:
    pass


# =============================================================================
# 10. INTERFAZ - PESTANAS
# =============================================================================
tab_mapa, tab_serie, tab_estac, tab_umbrales, tab_bd, tab_metodo = st.tabs(
    ["🗺️ Mapa e interpolacion", "📈 Series por estacion",
     "📋 Tabla de estaciones", "🎚️ Umbrales del SAT",
     "🗄️ Base de datos", "📚 Metodologia"])

# ---- 10.1 MAPA -------------------------------------------------------------
with tab_mapa:
    st.subheader("Interpolacion de lluvia 24 h sobre la subcuenca")
    ex = _poly_exterior(poly_cuenca)
    centro = [ex[:, 1].mean(), ex[:, 0].mean()]
    m = folium.Map(location=centro, zoom_start=12, tiles="CartoDB positron")

    # Superficie IDW como overlay (solo visual)
    try:
        grid, extent = superficie_idw(estados, poly_cuenca, "24h")
        vmax = max(areal["24h"] * 1.5, np.nanmax(grid) if np.isfinite(np.nanmax(grid)) else 1.0)
        png = grid_a_png(grid, vmax)
        if png:
            folium.raster_layers.ImageOverlay(
                image=png,
                bounds=[[extent[2], extent[0]], [extent[3], extent[1]]],
                name="Lluvia 24 h interpolada (IDW)", opacity=0.7).add_to(m)
    except Exception as e:
        st.caption(f"(Superficie IDW no disponible: {e})")

    # DEM de fondo si hay GEE
    if geom_ee is not None:
        try:
            dem = ee.Image("USGS/SRTMGL1_003").clip(geom_ee)
            mapid = dem.getMapId({"min": 1500, "max": 3200,
                                  "palette": ["#2c7bb6", "#ffffbf", "#d7191c"]})
            folium.TileLayer(tiles=mapid["tile_fetcher"].url_format,
                             attr="Google Earth Engine", name="Relieve (DEM)",
                             overlay=True, control=True, opacity=0.4).add_to(m)
        except Exception:
            pass

    # Limite de la cuenca
    folium.GeoJson(
        json.loads(json.dumps(poly_cuenca.__geo_interface__)),
        name="Limite de la cuenca",
        style_function=lambda x: {"color": "black", "weight": 2, "fillOpacity": 0}
    ).add_to(m)

    # Estaciones: color por nivel individual, radio por lluvia 24 h
    for e in estados:
        n_e, _ = evaluar_id(e["acum"], e["acum"].get("ant5d", 0.0), umbrales_editables)
        col = COLORES[n_e]
        r = 6 + min(e["acum"].get("24h", 0.0) / 5.0, 20)
        folium.CircleMarker(
            location=[e["lat"], e["lon"]], radius=r,
            color=col, fill=True, fill_color=col, fill_opacity=0.85,
            popup=folium.Popup(
                f"<b>{e['nombre']}</b> ({e['id']})<br>"
                f"Peso Thiessen: {pesos.get(e['id'], 0)*100:.0f}%<br>"
                f"1h: {e['acum'].get('1h', 0):.1f} mm<br>"
                f"6h: {e['acum'].get('6h', 0):.1f} mm<br>"
                f"24h: {e['acum'].get('24h', 0):.1f} mm<br>"
                f"Antec. 5d: {e['acum'].get('ant5d', 0):.1f} mm<br>"
                f"Nivel: <b>{n_e}</b>", max_width=260),
            tooltip=f"{e['nombre']}: {e['acum'].get('24h', 0):.0f} mm/24h"
        ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=None, height=560, returned_objects=[])
    st.caption("El circulo colorea el nivel individual de cada estacion; el "
               "tamano es proporcional a la lluvia de 24 h. La cifra oficial de "
               "la cuenca es la MEDIA AREAL de Thiessen, no un punto aislado.")

# ---- 10.2 SERIES -----------------------------------------------------------
with tab_serie:
    st.subheader("Precipitacion horaria por estacion (ultimos 7 dias)")
    fig = go.Figure()
    for e in estados:
        h = sd.lluvia_horaria_desde_db(con_db, e["id"], dias=10)
        if h.empty:
            continue
        fig.add_trace(go.Bar(x=h["fecha"], y=h["mm"], name=e["nombre"], opacity=0.7))
    fig.update_layout(barmode="overlay", height=420,
                      xaxis_title="Fecha (UTC)", yaxis_title="Lluvia horaria (mm)",
                      legend=dict(orientation="h", y=-0.25), margin=dict(t=30))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Comparativa de acumulados actuales (media areal vs umbrales)")
    dfc = pd.DataFrame({
        "Duracion": ["1h", "3h", "6h", "24h"],
        "Areal (mm)": [areal["1h"], areal["3h"], areal["6h"], areal["24h"]],
        "Aviso (mm)": [umbrales_editables[d][0] for d in ["1h", "3h", "6h", "24h"]],
        "Alerta (mm)": [umbrales_editables[d][1] for d in ["1h", "3h", "6h", "24h"]],
    })
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(x=dfc["Duracion"], y=dfc["Areal (mm)"], name="Lluvia areal",
                          marker_color="#225ea8"))
    fig2.add_trace(go.Scatter(x=dfc["Duracion"], y=dfc["Aviso (mm)"], name="Aviso",
                              mode="lines+markers", line=dict(color="#f4a300", dash="dot")))
    fig2.add_trace(go.Scatter(x=dfc["Duracion"], y=dfc["Alerta (mm)"], name="Alerta",
                              mode="lines+markers", line=dict(color="#d7191c", dash="dash")))
    fig2.update_layout(height=380, yaxis_title="mm", margin=dict(t=30))
    st.plotly_chart(fig2, use_container_width=True)

# ---- 10.3 TABLA DE ESTACIONES ---------------------------------------------
with tab_estac:
    st.subheader("Estado de la red de estaciones")
    filas = []
    for e in estados:
        n_e, _ = evaluar_id(e["acum"], e["acum"].get("ant5d", 0.0), umbrales_editables)
        filas.append({
            "Estacion": e["nombre"],
            "ID": e["id"],
            "En cuenca": "Si" if e.get("dentro") else "Halo",
            "Peso Thiessen %": round(pesos.get(e["id"], 0) * 100, 1),
            "Elev (m)": e.get("elev_m"),
            "1h": round(e["acum"].get("1h", 0), 1),
            "6h": round(e["acum"].get("6h", 0), 1),
            "24h": round(e["acum"].get("24h", 0), 1),
            "Antec 5d": round(e["acum"].get("ant5d", 0), 1),
            "Nivel": n_e,
            "Ultima obs (local)": e.get("obs_local"),
        })
    st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
    st.caption("El peso Thiessen indica la fraccion del area de la cuenca "
               "representada por cada estacion. Las estaciones del halo aportan "
               "al mapa pero su peso areal interno puede ser bajo.")

# ---- 10.4 UMBRALES ---------------------------------------------------------
with tab_umbrales:
    st.subheader("Logica de umbrales del SAT (doble via)")
    st.markdown("**Via operativa - Intensidad-Duracion (gatillo en vivo):**")
    tabla_id = pd.DataFrame([
        {"Duracion": d, "Aviso (amarillo) mm": umbrales_editables[d][0],
         "Alerta (rojo) mm": umbrales_editables[d][1],
         "Lluvia areal actual mm": round(areal[d], 1)}
        for d in ["1h", "3h", "6h", "24h"]
    ])
    st.dataframe(tabla_id, use_container_width=True, hide_index=True)

    st.markdown(
        f"- **Modulacion por antecedente:** si la lluvia de los 5 dias previos "
        f"supera **{ANTECEDENTE_SATURACION:.0f} mm** (suelo saturado), los "
        f"umbrales se reducen al **{FACTOR_SATURACION*100:.0f}%**. "
        f"Actual: **{areal['ant5d']:.1f} mm** "
        f"({'SATURADO' if areal['ant5d'] >= ANTECEDENTE_SATURACION else 'no saturado'}).\n"
        f"- **Escalada a ROJO EXTREMO:** dos o mas ventanas de duracion en nivel "
        f"ALERTA simultaneamente, o confirmacion por climatologia P99.")

    st.markdown("**Via climatologica - percentiles CHIRPS 1981-2025 (referencia):**")
    if clim:
        st.dataframe(pd.DataFrame([
            {"Indicador": "P90 diario", "Valor mm": round(clim["p90"], 1)},
            {"Indicador": "P95 diario", "Valor mm": round(clim["p95"], 1)},
            {"Indicador": "P99 diario", "Valor mm": round(clim["p99"], 1)},
            {"Indicador": "P95 acum. 5 d", "Valor mm": round(clim["acum5_p95"], 1)},
        ]), use_container_width=True, hide_index=True)
        st.caption(txt_clim)
    else:
        st.info("Climatologia CHIRPS no disponible (GEE inactivo).")

# ---- 10.5 BASE DE DATOS Y DIAGNOSTICO --------------------------------------
with tab_bd:
    st.subheader("Base de datos historica del SAT")
    res = sd.resumen_base(con_db)
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Observaciones", f"{res['observaciones']:,}")
    b2.metric("Estaciones registradas", res["estaciones"])
    b3.metric("Longitud de serie", f"{res['dias_serie']:.2f} dias")
    b4.metric("Ultimo dato (UTC)",
              (res["hasta_utc"] or "-")[-8:-6] + "h" if res["hasta_utc"] else "-")
    st.caption(f"Cobertura temporal: {res['desde_utc']} a {res['hasta_utc']} (UTC)")

    st.caption(f"Archivo en uso: `{ruta_db_activa}`  |  Modo: {modo_db}")
    if "temporal" in modo_db:
        st.info(
            "La aplicacion trabaja sobre una **copia temporal** porque el "
            "repositorio se monta en solo lectura. Las cosechas de esta sesion "
            "se pierden al reiniciarse la aplicacion. La **serie historica "
            "permanente** la construye la tarea programada de GitHub Actions, "
            "que versiona el archivo en el repositorio cada 3 horas.")

    st.markdown("#### Ultima cosecha de la red")
    ultimo_res = st.session_state.get("ultimo_resumen")
    if ultimo_res:
        st.success(f"Registros nuevos incorporados: **{ultimo_res['nuevos']}** "
                   f"({ultimo_res['ts']} UTC)")
        for m in ultimo_res["mensajes"]:
            if "sin observaciones" in m or "HTTP" in m or "sin respuesta" in m:
                st.warning(m)
            elif "cambio de estructura" in m:
                st.error(m + "  <- requiere revisar el extractor")
            else:
                st.write("✓ " + m)
    else:
        st.info("Aun no se ha ejecutado una cosecha en esta sesion. "
                "Los datos mostrados provienen de la base existente.")

    st.markdown("#### Bitacora de cargas")
    try:
        bit = pd.read_sql_query(
            "SELECT ts, evento, detalle FROM bitacora ORDER BY ts DESC LIMIT 20", con_db)
        st.dataframe(bit, use_container_width=True, hide_index=True)
    except Exception:
        st.caption("Sin bitacora disponible.")

    st.markdown("#### Respaldo de la serie")
    st.warning(
        "**Importante:** Streamlit Community Cloud usa almacenamiento efimero. "
        "La base se pierde al reiniciarse la aplicacion. Descarga el respaldo "
        "periodicamente para conservar la serie historica que alimentara la "
        "calibracion local de umbrales.")
    try:
        df_exp = pd.read_sql_query(
            "SELECT * FROM observaciones ORDER BY station_id, epoch", con_db)
        st.download_button(
            "⬇️ Descargar serie completa (CSV)",
            data=df_exp.to_csv(index=False).encode("utf-8"),
            file_name=f"sat_quiscab_{dt.date.today().isoformat()}.csv",
            mime="text/csv")
    except Exception as e:
        st.caption(f"No se pudo preparar la exportacion: {e}")


# ---- 10.6 METODOLOGIA ------------------------------------------------------
with tab_metodo:
    st.subheader("Fundamento metodologico y respaldo academico")
    st.markdown(
        """
**1. Fuente de datos en tiempo real.** La red de estaciones de Vivamos Mejor /
DAT publica sus datos a traves de *Weather Underground* (PWS) y *Ambient
Weather*. La aplicacion implementa una **capa de adquisicion con adaptadores
intercambiables**: extraccion del estado publicado en el portal (sin llave) o
**API oficial de WU PWS** (`api.weather.com/v2/pws`) cuando se dispone de una
apiKey de contribuidor. Ambos entregan observaciones cada pocos minutos con
coordenadas incluidas. Se reemplaza asi a CHIRPS como gatillo, cuya latencia de
varios dias lo dejaba fuera de un SAT operativo.

**1b. Persistencia y serie propia.** Cada lectura se almacena en una base de
datos SQLite con clave primaria (estacion, marca temporal), lo que hace la carga
**idempotente**: se puede ejecutar cuantas veces se quiera sin duplicar. La
lamina horaria se reconstruye diferenciando el acumulado diario del pluviometro
dentro de cada dia local (Guatemala, UTC-6) y anulando los reinicios del
contador. Esta serie propia es la que permitira, con el tiempo, **calibrar
localmente los umbrales I-D**, superando la limitacion de usar valores tomados
de la literatura.

**1c. Transparencia sobre el metodo de acceso.** El adaptador sin llave extrae
el estado que el propio servidor incrusta en sus paginas publicas. No elude
autenticacion ni controles tecnicos, pero los Terminos de Servicio de la
plataforma desaconsejan la extraccion automatizada; se aplica limitacion de
frecuencia y agente identificable. **Se concibe como puente temporal**: para
operacion institucional debe migrarse al adaptador de API. Esta limitacion se
declara explicitamente por honestidad metodologica.

**2. Separacion climatologia / gatillo.** CHIRPS 1981-2025 se conserva como
**memoria climatologica** (percentiles P90/P95/P99). El disparo de alerta lo
gobierna la red en vivo. No se transfieren directamente los percentiles de
CHIRPS a los pluviometros porque miden cosas distintas (satelite-estacion a
~5 km vs punto): se evita ese sesgo de escala.

**3. Interpolacion areal.** Con 4-6 pluviometros se usan **poligonos de
Thiessen** (media areal ponderada por area de influencia), recomendados por la
OMM para redes ralas; IDW se reserva para la superficie visual. Se descarta el
Kriging: el variograma no es fiable con tan pocos puntos.

**4. Umbrales Intensidad-Duracion (I-D).** Estandar internacional para alerta
por lluvia. Se evaluan acumulados de **1, 3, 6 y 24 h**, modulados por el
**antecedente de 5 dias** (proxy de humedad del suelo). Referencias:

- Caine (1980) — control I-D de deslizamientos superficiales y flujos de detritos.
- Guzzetti et al. (2007, 2008) — actualizacion del control I-D.
- Brunetti et al. (2010) — umbrales de lluvia para deslizamientos.
- Guzzetti et al. (2020) — sistemas geograficos de alerta temprana.
- WMO-No.168 — practicas hidrologicas, interpolacion areal.

**5. Honestidad metodologica (pendiente de calibracion local).** Los umbrales
I-D actuales son valores **semilla parametrizables**, no calibrados con eventos
del Quiscab. Para volverlos operativos se requiere un **inventario de eventos**
(fechas de crecidas/deslizamientos historicos en la cuenca; p.ej. Stan 2005 -
Panabaj). El codigo esta estructurado para recalibrarlos con el propio historial
de las estaciones a medida que se acumule y con ese inventario.

**6. Ambient Weather.** Las estaciones CEDRACC y Chuacruz publican en Ambient
Weather. Se pueden integrar agregando las credenciales del propietario
(`ambient_application_key`, `ambient_api_key`). El SAT ya cubre el Quiscab con la
estacion de **Solola (ISOLOL4)** dentro de la cuenca y Panajachel / San Andres en
el halo.
        """)

st.caption("SAT Quiscab v2 - gatillo en tiempo real (PWS) + climatologia CHIRPS. "
           f"Fuente de cuenca: {fuente_cuenca}. "
           "Actualiza automaticamente segun el refresco configurado.")
