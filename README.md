# ⚽ CeltaCalendar Bot

¡Bienvenido al repositorio del **CeltaCalendar Bot**! Este es un sistema automatizado de élite diseñado para sincronizar en tiempo real el calendario oficial del RC Celta de Vigo directamente con Google Calendar. 

El bot es completamente autónomo y vive en la nube (mediante GitHub Actions), patrullando la web en busca de actualizaciones de partidos, resultados, cambios de horario, estadios y clasificaciones.

---

## 🌟 Características Principales

- **🔄 Sincronización en Tiempo Real**: Rashea los datos oficiales (vía Besoccer) y actualiza automáticamente los eventos en Google Calendar.
- **🛡️ Antibloqueos y Cloudflare Bypass**: Utiliza Selenium (Headless Chrome) y `curl_cffi` para simular ser un usuario real y sobrepasar las protecciones web.
- **🏟️ Mapeo Dinámico de Estadios**: Extrae la localización de los estadios visitantes, los limpia cruzando datos con Wikipedia/Google y los guarda en su propia base de datos (`data/stadiums.json`).
- **📺 Información Televisiva**: Inyecta en el calendario las cadenas de TV donde se retransmite el partido (ej. *Movistar LaLiga*, *DAZN*).
- **💀 Cierre Automático de Temporada**: 
  - Al concluir el último partido de liga, le añade la etiqueta `💀 ÚLTIMO PARTIDO DE LIGA 💀`.
  - Pasados 3 días de la finalización de la temporada, el bot extrae la clasificación final de Liga en directo y calcula la ronda máxima alcanzada en competiciones europeas y Copa del Rey.
  - Genera un resumen `📊 BALANCE FINAL DE TEMPORADA` con animaciones personalizadas según los logros (Ascensos, Descensos o Clasificaciones Europeas).
- **⏰ Ejecución Inteligente (Scheduler)**: Ejecuta comprobaciones automáticas durante el día e incluso de madrugada (01:00 AM y 02:00 AM) para interceptar partidos nocturnos que llegan a prórroga o penaltis.

---

## 📂 Estructura del Repositorio

El proyecto ha sido limpiado y estructurado a nivel profesional:

```text
CeltaCalendar/
├── .github/
│   └── workflows/
│       └── scheduler.yml       # Configuración del servidor en la nube (GitHub Actions)
├── archive/                    # 📚 Scripts que se usaron para volcar 90 años de historia (uso archivístico)
│   ├── apply_balance.py
│   ├── balances_db.json
│   └── historical_sync.py
├── data/
│   └── stadiums.json           # 💾 Base de datos "viva" de localizaciones y estadios
├── .env                        # Variables de entorno locales
├── credentials.json            # Claves de acceso a Google OAuth (No subir a GitHub público)
├── token.json                  # Token de sesión autorizado de Google
├── main_scraper.py             # 🧠 El corazón del Bot. El script que lo hace todo.
├── requirements.txt            # Librerías de Python requeridas
└── README.md                   # Esta documentación
```

---

## ⚙️ Configuración (Setup)

Si necesitas instalar este bot en un entorno local o desplegarlo en un nuevo repositorio de GitHub, necesitas seguir estos pasos:

### 1. Variables de Entorno y Secretos
Para que el bot pueda conectarse a los servicios, debes configurar los siguientes `Secrets` en GitHub (Settings > Secrets and variables > Actions):

- `CALENDAR_ID`: El ID de tu calendario de Google (ej. `tu_correo@group.calendar.google.com`).
- `TELEGRAM_BOT_TOKEN`: El token de tu bot de Telegram creado a través de BotFather.
- `TELEGRAM_CHAT_ID`: Tu ID numérico de usuario en Telegram para recibir alertas de fallos.
- `GCP_CREDENTIALS_JSON`: El contenido de tu archivo `credentials.json` descargado de Google Cloud Console (codificado en Base64).
- `GCP_TOKEN_JSON_B64`: El contenido de tu archivo `token.json` generado al loguearte localmente la primera vez (codificado en Base64).

### 2. Dependencias Locales
Si quieres ejecutar o probar el código en tu ordenador:
```bash
pip install -r requirements.txt
```
Luego, simplemente ejecuta:
```bash
python main_scraper.py
```

---

## 🤖 El Ciclo de Vida del Bot (¿Cómo funciona?)

1. **Despertar**: GitHub Actions lanza el bot múltiples veces al día según las reglas del archivo `scheduler.yml`.
2. **Scraping Inicial**: El bot extrae la parrilla televisiva del día y la lista de todos los partidos del Celta programados y finalizados.
3. **Escaneo de Estadios**: Si hay un partido visitante inminente sin datos, abre un Chrome invisible, busca el estadio local y actualiza su base de datos interna `data/stadiums.json`. Tras esto, hace un `git push` automático para guardar el estadio en este repositorio.
4. **Comparativa**: Lee el Google Calendar y compara los eventos uno a uno. Si un partido ha cambiado de hora, ha terminado, tiene nueva televisión o se ha asignado estadio, actualiza el bloque de texto respetando los enlaces previos.
5. **Cierre de Temporada (Freno 3 Días)**: Si detecta que el último partido del año se ha jugado hace más de 3 días, entra en modo "Balance". Elude el cortafuegos de Besoccer, scrapea la posición liguera, calcula las rondas del resto de competiciones y cierra el ciclo de ese año con un bloque dorado de estadísticas.
6. **Notificación**: Si hay algún fallo crítico (cambios en el HTML de las webs, token caducado, etc.), te envía un chivatazo directo a Telegram.

---
*Desarrollado y perfeccionado para que nunca más te pierdas un partido del Real Club Celta de Vigo.* ¡Hala Celta! 🩵
