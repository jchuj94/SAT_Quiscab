"""
Dashboard hidroclimatico - Subcuenca del rio Quiscab, Solola, Guatemala
Actividad Semana 3 (MCHV-513) - De la cuenca al mapa interactivo

Autor: Ing. Agr. Jose Faustino Chuj Matul

Stack: Streamlit + Google Earth Engine + Folium + CHIRPS + SRTM.
Autenticacion automatizada mediante Service Account (llave JSON en Google Drive),
de modo que la app se actualiza sola en cada ejecucion sin login manual.
"""

import os
import json
import datetime as dt

import ee
import folium
import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

# ---------------------------------------------------------------------------
# Configuracion general de la pagina
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="SAT Quiscab - Dashboard hidroclimatico",
    page_icon="warning",
    layout="wide",
)

# Parametros del proyecto. Ajustar si se usa otra cuenta de servicio.
SA_EMAIL = "service-ee-cydata@ee-cydata.iam.gserviceaccount.com"
JSON_PATH = "/content/drive/MyDrive/gee_keys/service-key.json"
PROYECTO_GEE = "tutorial-177615"

# Punto de anclaje dentro de la subcuenca del Quiscab (Solola).
# Se usa solo si no se carga el vector oficial.
PUNTO_QUISCAB = [-91.190, 14.770]


# ---------------------------------------------------------------------------
# Autenticacion a Earth Engine (Service Account, cacheada por sesion)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Conectando con Google Earth Engine...")
def inicializar_gee():
    """
    Inicializa Earth Engine. En Streamlit Cloud usa la llave guardada en
    Secrets; en Colab usa la llave JSON de Drive; en local, credenciales
    de usuario.
    """
    try:
        # 1. Streamlit Community Cloud: llave en Secrets (camino principal)
        if "gee_service_account" in st.secrets:
            info = dict(st.secrets["gee_service_account"])
            cred = ee.ServiceAccountCredentials(
                info["client_email"], key_data=json.dumps(info))
            ee.Initialize(cred, project=info.get("project_id", PROYECTO_GEE))
            return "secrets"
        # 2. Colab: llave JSON en Google Drive
        if os.path.exists(JSON_PATH):
            cred = ee.ServiceAccountCredentials(SA_EMAIL, JSON_PATH)
            ee.Initialize(cred, project=PROYECTO_GEE)
            return "service_account"
        # 3. Local: credenciales de usuario
        ee.Initialize(project=PROYECTO_GEE)
        return "usuario"
    except Exception as e:
        st.error(f"No se pudo inicializar Earth Engine: {e}")
        st.stop()


modo_auth = inicializar_gee()


# ---------------------------------------------------------------------------
# Geometria de la cuenca
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Delimitando la subcuenca...")
def cargar_cuenca():
    """
    Devuelve la geometria de la subcuenca del Quiscab.
    Prioriza el vector oficial (asset o GeoJSON local) y usa el anzuelo
    espacial sobre HydroBASINS como respaldo.
    """
    ruta_geojson = "cuenca_quiscab.geojson"
    if os.path.exists(ruta_geojson):
        with open(ruta_geojson) as f:
            gj = json.load(f)
        fc = ee.FeatureCollection(gj)
        return fc.geometry(), "vector oficial"

    punto = ee.Geometry.Point(PUNTO_QUISCAB)
    cuenca = (ee.FeatureCollection("WWF/HydroSHEDS/v1/Basins/hybas_12")
                .filterBounds(punto)
                .first())
    return cuenca.geometry(), "HydroBASINS (anzuelo)"


geom_cuenca, fuente_cuenca = cargar_cuenca()


# ---------------------------------------------------------------------------
# Funciones de datos (cacheadas por rango de fechas)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Consultando CHIRPS en Earth Engine...")
def serie_precipitacion(fecha_ini, fecha_fin):
    """Serie diaria de lluvia media areal sobre la cuenca para el rango dado."""
    chirps = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                .filterDate(str(fecha_ini), str(fecha_fin))
                .select("precipitation"))

    def media_areal(imagen):
        media = imagen.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom_cuenca,
            scale=1000,
            maxPixels=1e9,
        )
        return ee.Feature(None, {
            "timestamp_ms": imagen.get("system:time_start"),
            "lluvia_mm": media.get("precipitation"),
        })

    fc = ee.FeatureCollection(chirps.map(media_areal))
    datos = fc.reduceColumns(
        ee.Reducer.toList(2), ["timestamp_ms", "lluvia_mm"]
    ).get("list").getInfo()

    df = pd.DataFrame(datos, columns=["timestamp_ms", "lluvia_mm"])
    df["fecha"] = pd.to_datetime(df["timestamp_ms"], unit="ms")
    df["lluvia_mm"] = pd.to_numeric(df["lluvia_mm"], errors="coerce")
    df = df.dropna().sort_values("fecha").reset_index(drop=True)
    df["acum_5d"] = df["lluvia_mm"].rolling(5, min_periods=1).sum()
    return df[["fecha", "lluvia_mm", "acum_5d"]]


@st.cache_data(show_spinner="Calculando umbrales historicos (1981-2025)...")
def umbrales_historicos():
    """Percentiles P90/P95/P99 y umbral de saturacion sobre la serie completa."""
    df = serie_precipitacion("1981-01-01", "2025-12-31")
    dias_lluvia = df.loc[df["lluvia_mm"] > 1, "lluvia_mm"]
    return {
        "p90": float(dias_lluvia.quantile(0.90)),
        "p95": float(dias_lluvia.quantile(0.95)),
        "p99": float(dias_lluvia.quantile(0.99)),
        "acum_p95": float(df["acum_5d"].quantile(0.95)),
    }


def clasificar_alerta(lluvia, acum, u):
    if lluvia >= u["p99"]:
        return "ROJO EXTREMO", "#8b0000"
    if lluvia >= u["p95"] or acum >= u["acum_p95"]:
        return "ROJO", "#d7191c"
    if lluvia >= u["p90"]:
        return "AMARILLO", "#f4a300"
    return "VERDE", "#1a9641"


def add_ee_layer(mapa, imagen, vis, nombre):
    """Puente de una imagen de Earth Engine a una capa de Folium."""
    mapid = ee.Image(imagen).getMapId(vis)
    folium.TileLayer(
        tiles=mapid["tile_fetcher"].url_format,
        attr="Google Earth Engine",
        name=nombre,
        overlay=True,
        control=True,
    ).add_to(mapa)


# ---------------------------------------------------------------------------
# Barra lateral: controles
# ---------------------------------------------------------------------------
st.sidebar.title("Panel de control")
st.sidebar.caption(f"GEE: modo {modo_auth} | Cuenca: {fuente_cuenca}")

hoy = dt.date(2025, 12, 31)
rango = st.sidebar.date_input(
    "Rango de analisis",
    value=(dt.date(2024, 1, 1), hoy),
    min_value=dt.date(1981, 1, 1),
    max_value=hoy,
)
if isinstance(rango, tuple) and len(rango) == 2:
    fecha_ini, fecha_fin = rango
else:
    fecha_ini, fecha_fin = dt.date(2024, 1, 1), hoy

st.sidebar.markdown("---")
st.sidebar.caption("Los umbrales se calculan sobre la serie historica "
                   "completa (1981-2025) y se cachean.")

# ---------------------------------------------------------------------------
# Encabezado
# ---------------------------------------------------------------------------
st.title("Sistema de Alerta Temprana - Subcuenca del rio Quiscab")
st.caption("Solola, Guatemala | Mayor afluente del lago de Atitlan | "
           "Datos: CHIRPS + SRTM via Google Earth Engine")

# Carga de datos
u = umbrales_historicos()
df = serie_precipitacion(fecha_ini, fecha_fin)

# Estado actual: ultimo dia del rango
ultimo = df.iloc[-1]
nivel, color = clasificar_alerta(ultimo["lluvia_mm"], ultimo["acum_5d"], u)

# ---------------------------------------------------------------------------
# Fila de indicadores (KPIs)
# ---------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Ultima lluvia diaria", f"{ultimo['lluvia_mm']:.1f} mm",
          f"{ultimo['fecha']:%d-%b-%Y}")
c2.metric("Acumulado 5 dias", f"{ultimo['acum_5d']:.1f} mm",
          f"umbral P95: {u['acum_p95']:.0f} mm")
c3.metric("Umbral critico P95", f"{u['p95']:.1f} mm/dia",
          f"extremo P99: {u['p99']:.0f} mm")
c4.metric("Estado del SAT", nivel)
st.markdown(
    f"<div style='padding:10px;border-radius:8px;background:{color};"
    f"color:white;text-align:center;font-size:20px;font-weight:bold'>"
    f"Alerta {nivel}</div>", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Pestanas: mapa, serie, umbrales
# ---------------------------------------------------------------------------
tab_mapa, tab_serie, tab_umbrales = st.tabs(
    ["Mapa de la cuenca", "Serie de precipitacion", "Umbrales del SAT"])

with tab_mapa:
    st.subheader("Relieve y lluvia acumulada del periodo")
    dem = ee.Image("USGS/SRTMGL1_003").clip(geom_cuenca)
    lluvia_acum = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                     .filterDate(str(fecha_ini), str(fecha_fin))
                     .select("precipitation").sum().clip(geom_cuenca))

    centro = geom_cuenca.centroid(maxError=1).coordinates().getInfo()
    m = folium.Map(location=[centro[1], centro[0]], zoom_start=12,
                   tiles="CartoDB positron")
    add_ee_layer(m, dem, {"min": 1500, "max": 3200,
                          "palette": ["#2c7bb6", "#ffffbf", "#d7191c"]}, "DEM (m)")
    add_ee_layer(m, lluvia_acum, {"min": 0, "max": 1200,
                 "palette": ["#ffffff", "#41b6c4", "#225ea8"]}, "Lluvia acumulada (mm)")
    folium.GeoJson(geom_cuenca.getInfo(), name="Limite cuenca",
                   style_function=lambda x: {"color": "black", "weight": 2,
                                             "fillOpacity": 0}).add_to(m)
    folium.LayerControl().add_to(m)
    st_folium(m, width=None, height=520)

with tab_serie:
    st.subheader(f"Precipitacion diaria media ({fecha_ini} a {fecha_fin})")
    fig = px.bar(df, x="fecha", y="lluvia_mm",
                 labels={"fecha": "Fecha", "lluvia_mm": "Lluvia (mm)"})
    fig.add_hline(y=u["p90"], line_dash="dot", line_color="#f4a300",
                  annotation_text="P90")
    fig.add_hline(y=u["p95"], line_dash="dash", line_color="#e0662b",
                  annotation_text="P95")
    fig.add_hline(y=u["p99"], line_dash="dash", line_color="#d7191c",
                  annotation_text="P99")
    fig.update_layout(height=420, margin=dict(t=30))
    st.plotly_chart(fig, use_container_width=True)

    eventos = df[df["lluvia_mm"] >= u["p95"]]
    st.info(f"Dias del periodo que igualan o superan el P95: {len(eventos)}")

with tab_umbrales:
    st.subheader("Umbrales criticos y logica del semaforo")
    tabla = pd.DataFrame([
        ["Lluvia diaria P90", f"{u['p90']:.1f} mm", "AMARILLO - vigilancia"],
        ["Lluvia diaria P95", f"{u['p95']:.1f} mm", "ROJO - riesgo alto"],
        ["Lluvia diaria P99", f"{u['p99']:.1f} mm", "ROJO - riesgo extremo"],
        ["Acumulada 5 dias P95", f"{u['acum_p95']:.1f} mm", "ROJO - saturacion"],
    ], columns=["Indicador", "Valor", "Accion"])
    st.dataframe(tabla, use_container_width=True, hide_index=True)

    with st.expander("Metodologia del cerebro del SAT"):
        st.markdown(
            "- Los umbrales de lluvia diaria se derivan de los percentiles "
            "P90, P95 y P99 de los dias con lluvia apreciable en la serie "
            "historica 1981 a 2025.\n"
            "- El umbral de saturacion es el P95 de la lluvia acumulada movil "
            "de 5 dias, que aproxima la condicion antecedente del suelo.\n"
            "- El semaforo combina ambos: rojo si se supera el P95 diario o el "
            "P95 acumulado, amarillo entre P90 y P95, verde por debajo.")

st.caption("Actualizacion automatica: cada ejecucion recalcula con los datos "
           "mas recientes de CHIRPS. Fuente cuenca: " + fuente_cuenca)
