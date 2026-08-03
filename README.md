# SAT Quiscab

**Sistema de Alerta Temprana hidrometeorológico para la subcuenca del río Quiscab**
Sololá, Guatemala | Principal afluente del lago de Atitlán

Ing. Agr. José Faustino Chuj Matul
Maestría en Manejo Integrado de Cuencas Hidrográficas (MCHV-513), CATIE
Green Solutions

---

## Descripción

Aplicación web que monitorea la precipitación en la subcuenca del río Quiscab en
tiempo casi real y emite alertas por lluvia mediante un sistema de semáforo de
cuatro niveles. Los datos provienen de la red de estaciones meteorológicas
operada por Vivamos Mejor Guatemala a través de su Departamento de Análisis y
Monitoreo Territorial (DAT).

La subcuenca se ubica dentro de la Reserva de Uso Múltiple de la Cuenca del Lago
de Atitlán, administrada por CONAP, y constituye el mayor aporte hídrico
superficial al lago.

---

## Fundamento del cambio de arquitectura

La primera versión del sistema disparaba las alertas con CHIRPS
(`UCSB-CHG/CHIRPS/DAILY`). CHIRPS es un producto climatológico de calidad, pero
su latencia de varios días lo inhabilita como disparador operativo: una alerta
que llega cinco días después del evento no es alerta temprana.

La versión actual separa dos funciones que antes estaban mezcladas:

| Componente | Fuente | Función |
|---|---|---|
| Climatología de referencia | CHIRPS 1981-2025 vía Google Earth Engine | Percentiles P90, P95 y P99 de largo plazo. Memoria, no disparador. |
| Disparador operativo | Red de estaciones pluviométricas | Acumulados multi duración en tiempo casi real. |

Los percentiles de CHIRPS no se transfieren directamente a los pluviómetros
porque miden magnitudes distintas: un producto satélite estación de
aproximadamente 5 km frente a una medición puntual. Evitar esa transferencia
previene un sesgo de escala.

---

## Metodología

### Adquisición

Capa de adaptadores intercambiables sobre la red de estaciones. El adaptador
activo recupera el estado que el portal publica en sus páginas, sin eludir
autenticación ni controles técnicos de acceso, aplicando limitación de
frecuencia y agente de usuario identificable. El adaptador de API oficial está
implementado y su activación requiere únicamente disponer de la credencial de
contribuidor.

Cada lectura se persiste en una base de datos SQLite con clave primaria
compuesta por estación y marca temporal, lo que hace la carga idempotente: puede
ejecutarse repetidamente sin duplicar registros.

### Reconstrucción de la lámina horaria

El pluviómetro reporta un acumulado que se reinicia cada día a medianoche local.
La lluvia de cada intervalo se obtiene diferenciando ese acumulado dentro del
mismo día local de Guatemala (UTC menos 6) y anulando los valores negativos que
corresponden al reinicio del contador.

### Interpolación areal

Con una red de pocos pluviómetros se emplean **polígonos de Thiessen**, es decir
media areal ponderada por el área de influencia de cada estación, siguiendo la
recomendación de la Organización Meteorológica Mundial para redes ralas. La
interpolación por distancia inversa ponderada (IDW, potencia 2) se reserva
exclusivamente para la superficie visual del mapa.

El kriging se descarta de forma explícita: con menos de una decena de puntos el
variograma experimental no es confiable y su uso daría una falsa apariencia de
rigor.

### Motor de umbrales

Se aplica el enfoque de **umbrales empíricos de Intensidad-Duración (I-D)**,
estándar internacional para alerta por lluvia en crecidas repentinas y
deslizamientos. Se evalúan acumulados de 1, 3, 6 y 24 horas contra umbrales de
aviso y de alerta, modulados por la lluvia antecedente de 5 días como
aproximación a la humedad del suelo.

Cuando el antecedente supera el valor de saturación, los umbrales efectivos se
reducen. La escalada al nivel máximo ocurre cuando dos o más ventanas de
duración alcanzan simultáneamente el nivel de alerta, o cuando la climatología
confirma un evento por encima del P99 histórico.

---

## Limitaciones declaradas

Se documentan de forma explícita por honestidad metodológica.

**Los umbrales I-D no están calibrados localmente.** Los valores actuales son
parámetros semilla tomados de la literatura internacional y de órdenes de
magnitud operativos regionales. Su calibración rigurosa exige un inventario de
eventos históricos de la cuenca, con fechas verificadas de crecidas y
deslizamientos. El evento de referencia regional obligado es el huracán Stan de
2005 y el desastre de Panabaj. La base de datos histórica que el sistema
construye está diseñada precisamente para habilitar esa calibración.

**La red efectiva es menor que la nominal.** De las estaciones catalogadas, en
las verificaciones realizadas solo una fracción reporta datos de forma
consistente. El sistema distingue en su diagnóstico entre una estación inactiva
y un cambio en la estructura de la fuente, de modo que la degradación de la red
nunca ocurre en silencio.

**El método de acceso a los datos es transitorio.** El adaptador sin credencial
se concibe como puente mientras se gestiona el acceso formal. Para operación
institucional debe migrarse al adaptador de API.

**El almacenamiento en la nube es efímero.** Streamlit Community Cloud reinicia
su sistema de archivos, por lo que la persistencia de la serie histórica se
delega a una tarea programada que versiona la base de datos en el repositorio.

---

## Estructura del repositorio

```
SAT_Quiscab/
├── app.py                          Aplicación Streamlit (interfaz y motor de alertas)
├── sat_datos.py                    Capa de adquisición y persistencia
├── requirements.txt                Dependencias
├── cuenca_quiscab.geojson          Delimitación oficial de la subcuenca
├── .gitignore
└── .github/
    └── workflows/
        └── cosecha.yml             Cosecha automática programada
```

---

## Instalación y uso

```bash
git clone https://github.com/jchuj94/SAT_Quiscab.git
cd SAT_Quiscab
pip install -r requirements.txt

# Primera cosecha de la red
python sat_datos.py --db sat_quiscab.db

# Ejecutar la aplicación
streamlit run app.py
```

### Configuración de credenciales

Las credenciales nunca se versionan. En Streamlit Community Cloud se registran
en Settings, Secrets. En local, en `.streamlit/secrets.toml`:

```toml
# Cuenta de servicio de Google Earth Engine
[gee_service_account]
client_email = "..."
project_id   = "..."
private_key  = "..."

# Opcional: credencial de la API de estaciones
wu_api_key = "..."
```

### Cosecha automática

La tarea programada se ejecuta cada tres horas y versiona la base de datos en el
repositorio. Esta frecuencia captura la totalidad de los registros porque cada
consulta devuelve el historial de las últimas 24 horas.

Requiere habilitar permisos de escritura en Settings, Actions, General, Workflow
permissions.

---

## Fuentes de datos

| Conjunto | Identificador | Uso |
|---|---|---|
| Red de estaciones DAT | Vivamos Mejor Guatemala | Precipitación en tiempo casi real |
| CHIRPS Daily | `UCSB-CHG/CHIRPS/DAILY` | Climatología 1981-2025 |
| SRTM | `USGS/SRTMGL1_003` | Modelo digital de elevación |
| HydroBASINS | `WWF/HydroSHEDS/v1/Basins/hybas_12` | Delimitación de respaldo |

---

## Referencias

Brunetti, M. T., Peruccacci, S., Rossi, M., Luciani, S., Valigi, D. y Guzzetti,
F. (2010). Rainfall thresholds for the possible occurrence of landslides in
Italy. *Natural Hazards and Earth System Sciences*, 10(3), 447-458.

Caine, N. (1980). The rainfall intensity-duration control of shallow landslides
and debris flows. *Geografiska Annaler: Series A, Physical Geography*, 62(1-2),
23-27.

Guzzetti, F., Peruccacci, S., Rossi, M. y Stark, C. P. (2007). Rainfall
thresholds for the initiation of landslides in central and southern Europe.
*Meteorology and Atmospheric Physics*, 98(3-4), 239-267.

Guzzetti, F., Peruccacci, S., Rossi, M. y Stark, C. P. (2008). The rainfall
intensity-duration control of shallow landslides and debris flows: an update.
*Landslides*, 5(1), 3-17.

Guzzetti, F., Gariano, S. L., Peruccacci, S., Brunetti, M. T., Marchesini, I.,
Rossi, M. y Melillo, M. (2020). Geographical landslide early warning systems.
*Earth-Science Reviews*, 200, 102973.

Organización Meteorológica Mundial. (2018). *Guide to Hydrological Practices*
(WMO-No. 168). Ginebra: OMM.

---

## Créditos

Datos de la red de estaciones meteorológicas: **Vivamos Mejor Guatemala**,
Departamento de Análisis y Monitoreo Territorial (DAT).
https://vivamosmejor.org.gt/dat/

Procesamiento geoespacial: Google Earth Engine.

---

## Aviso

Este sistema es un desarrollo académico en fase de validación. Sus umbrales no
han sido calibrados con eventos locales verificados y no sustituye a los
sistemas oficiales de alerta de CONRED ni de INSIVUMEH. No debe emplearse como
única base para decisiones de evacuación o de protección civil.
