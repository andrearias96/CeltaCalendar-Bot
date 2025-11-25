import os
import time
import datetime
import logging
import requests
import traceback
import locale
import urllib.parse
import re 
import html
import json 
import difflib 
import unicodedata 
import base64
import subprocess # Para matar procesos zombis

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv 

# --- IMPORTACIONES SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# --- CONFIGURACIÓN DE ENTORNO ---
os.environ['DBUS_SESSION_BUS_ADDRESS'] = '/dev/null'
os.environ['TZ'] = 'Europe/Madrid'
try:
    time.tzset()
except:
    pass

load_dotenv() 

# --- CONSTANTES ---
CONFIG = {
    "CALENDAR_ID": os.getenv("CALENDAR_ID"),
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "TEAM_NAME": "celta",
    "URL_BESOCCER": "https://es.besoccer.com/equipo/partidos/celta",
    "URL_TV": "https://www.futbolenlatv.es/equipo/celta",
    "SCOPES": ['https://www.googleapis.com/auth/calendar'],
    "CREDENTIALS_FILE": 'credentials.json',
    "TOKEN_FILE": 'token.json',
    "DB_FILE": 'stadiums.json' 
}

# --- GLOBAL STATE ---
STADIUM_DB = {}
DB_DIRTY = False

# Configurar locale
try:
    locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_TIME, 'es_ES')
    except:
        pass 

logging.basicConfig(level=logging.INFO, format='%(message)s')

# --- DB & HELPERS ---

def load_stadium_db():
    global STADIUM_DB
    if os.path.exists(CONFIG["DB_FILE"]):
        try:
            with open(CONFIG["DB_FILE"], 'r', encoding='utf-8') as f:
                STADIUM_DB = json.load(f)
            logging.info(f"💾 Base de datos de estadios cargada ({len(STADIUM_DB)} registros).")
        except Exception as e:
            logging.error(f"⚠️ Error cargando DB estadios: {e}")
            STADIUM_DB = {}
    else:
        STADIUM_DB = {}

def save_stadium_db():
    global DB_DIRTY
    if DB_DIRTY:
        try:
            with open(CONFIG["DB_FILE"], 'w', encoding='utf-8') as f:
                json.dump(STADIUM_DB, f, ensure_ascii=False, indent=4)
            logging.info("💾 Cambios guardados en stadiums.json.")
            DB_DIRTY = False
        except Exception as e:
            logging.error(f"❌ Error guardando DB estadios: {e}")

def normalize_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub('<[^<]+?>', '', text)
    return " ".join(text.split()).strip()

def normalize_team_key(name):
    if not name: return ""
    text = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8")
    text = text.lower()
    remove_list = [" fc", " cf", " ud", " cd", " sd", "real ", "club ", "deportivo ", "atletico "]
    for item in remove_list:
        text = text.replace(item, " ")
    return " ".join(text.split()).strip()

def find_stadium_in_db(team_name):
    """Busca estadio en DB local."""
    if not STADIUM_DB: return None, None
    clean_target = normalize_team_key(team_name)
    
    if team_name in STADIUM_DB:
        entry = STADIUM_DB[team_name]
        return entry.get('stadium'), entry.get('location')

    db_keys = list(STADIUM_DB.keys())
    norm_keys_map = {normalize_team_key(k): k for k in db_keys}
    matches = difflib.get_close_matches(clean_target, norm_keys_map.keys(), n=1, cutoff=0.85)
    
    if matches:
        real_key = norm_keys_map[matches[0]]
        entry = STADIUM_DB[real_key]
        return entry.get('stadium'), entry.get('location')
    return None, None

def update_db(team_name, stadium, location):
    global STADIUM_DB, DB_DIRTY
    # Validaciones básicas
    invalid_terms = ["campo municipal", "estadio local", "campo de futbol"]
    if any(term in stadium.lower() for term in invalid_terms) and len(stadium) < 15: return
    
    STADIUM_DB[team_name] = {
        "stadium": stadium,
        "location": location,
        "last_updated": datetime.datetime.now().strftime("%Y-%m-%d")
    }
    DB_DIRTY = True

# --- DRIVER HARDENING (TU VERSIÓN ROBUSTA) ---

def force_kill_chrome():
    try:
        if os.name == 'posix':
            subprocess.run(['pkill', '-f', 'chrome'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['pkill', '-f', 'chromedriver'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
    except: pass

def setup_driver():
    force_kill_chrome()
    
    chrome_options = Options()
    chrome_options.page_load_strategy = 'eager' 
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    chrome_options.add_argument("--lang=es-ES") 
    chrome_options.add_argument("--accept-lang=es-ES")

    import socket
    socket.setdefaulttimeout(120)

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except:
        force_kill_chrome()
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    driver.set_page_load_timeout(30) 
    driver.set_script_timeout(30)

    try:
        driver.execute_cdp_cmd('Target.createTarget', {'url': 'about:blank'})
        driver.execute_cdp_cmd('Emulation.setGeolocationOverride', {
            'latitude': 40.4168, 'longitude': -3.7038, 'accuracy': 100
        })
        driver.execute_cdp_cmd('Emulation.setTimezoneOverride', {'timezoneId': 'Europe/Madrid'})
        driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {
            'headers': {
                'Accept-Language': 'es-ES,es;q=0.9',
                'Upgrade-Insecure-Requests': '1',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            }
        })
    except: pass

    return driver

# --- LÓGICA DE CANALES (NUEVA ESTRATEGIA) ---

def process_channels(channel_list):
    """
    Procesa lista cruda de canales, aplica prioridades y formateo.
    Retorna (short_name_for_title, full_string_for_desc).
    """
    clean_channels = []
    
    # 1. Limpieza y Filtrado
    ignored = ["hellotickets", "laliga tv bar", "entradas", "ticket"]
    
    for ch in channel_list:
        ch_lower = ch.lower()
        if any(x in ch_lower for x in ignored): continue
        if "confirmar" in ch_lower: continue
        
        # Limpieza de textos basura (paréntesis de diales, etc.)
        clean = re.sub(r'\(.*?\)', '', ch).strip()
        clean = clean.replace('Ver en directo', '').strip()
        if clean: clean_channels.append(clean)
    
    if not clean_channels: return None, None

    # 2. Categorización por Prioridad
    # Prio 1: Gratuitos
    free = []
    # Prio 2: DAZN
    dazn = []
    # Prio 3: Movistar
    movistar = []
    # Resto
    others = []

    for c in clean_channels:
        up = c.upper()
        if any(x in up for x in ["TVE", "LA 1", "LA1", "TELEDEPORTE", "TDP", "RTVE", "TVG", "GALICIA"]):
            free.append(c)
        elif "DAZN" in up:
            dazn.append(c)
        elif any(x in up for x in ["MOVISTAR", "M+", "LIGA DE CAMPEONES", "VAMOS"]):
            movistar.append(c)
        else:
            others.append(c)
            
    # 3. Selección de Short Name (El mejor disponible)
    short_name = ""
    if free:
        # Preferencia de nombre corto para gratuitos
        top = free[0].upper()
        if "TVG" in top or "GALICIA" in top: short_name = "TVG"
        elif "TELEDEPORTE" in top: short_name = "tdp"
        elif "RTVE" in top or "LA 1" in top: short_name = "La1"
        else: short_name = free[0]
    elif dazn:
        short_name = "DAZN"
    elif movistar:
        short_name = "M+"
    elif others:
        short_name = "TV"
        
    # 4. Construcción de String Completo (Unir únicos)
    # Ordenamos para la descripción: Free -> DAZN -> Movistar -> Otros
    final_list = sorted(list(set(free + dazn + movistar + others)))
    full_desc = ", ".join(final_list)
    
    return short_name, full_desc

def fetch_tv_schedule_centralized():
    """
    Scrapea futbolenlatv.es/equipo/celta una sola vez.
    Hace click en 'Más días' para cargar todo.
    Retorna diccionario: { 'YYYY-MM-DD': {'short': '...', 'full': '...'} }
    """
    schedule = {}
    driver = setup_driver()
    logging.info(f"📺 Cargando guía TV centralizada: {CONFIG['URL_TV']}")
    
    try:
        driver.get(CONFIG["URL_TV"])
        wait = WebDriverWait(driver, 10)
        
        # Intentar clickar "Más días" varias veces para cargar todo
        for _ in range(3):
            try:
                # Buscamos botones visibles de "Más días"
                btns = driver.find_elements(By.CSS_SELECTOR, ".btnPrincipal")
                clicked = False
                for btn in btns:
                    if btn.is_displayed() and "más días" in btn.text.lower():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5) # Esperar carga AJAX
                        clicked = True
                if not clicked: break # No hay más botones
            except: break

        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        # Procesar tablas de días
        tables = soup.select('table.tablaPrincipal')
        
        for table in tables:
            try:
                # Extraer Fecha
                header = table.select_one('.cabeceraTabla')
                if not header: continue
                date_text = header.get_text(strip=True) # "Jueves, 27/11/2025"
                match_date = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', date_text)
                if not match_date: continue
                
                day, month, year = match_date.group(1).split('/')
                iso_date = f"{year}-{month}-{day}"
                
                # Extraer Canales
                row = table.select_one('tr:not(.cabeceraTabla)')
                if not row: continue
                
                channel_elems = row.select('td.canales ul.listaCanales li')
                raw_channels = [c.get_text(strip=True) for c in channel_elems]
                
                short, full = process_channels(raw_channels)
                
                if short:
                    schedule[iso_date] = {'short': short, 'full': full}
                    
            except Exception as e:
                continue
                
        logging.info(f"📺 Guía TV procesada: {len(schedule)} fechas encontradas.")
        return schedule

    except Exception as e:
        logging.error(f"❌ Error scraping TV: {e}")
        return {}
    finally:
        driver.quit()

# --- LÓGICA DE ESTADIOS (NUEVA ESTRATEGIA) ---

def scrape_besoccer_stadium(match_link):
    """Scrapea un partido individual de BeSoccer para sacar estadio."""
    if not match_link: return None
    driver = setup_driver()
    stadium = None
    try:
        driver.get(match_link)
        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        # Intentar sacar estadio
        box_rows = soup.select('.table-row-round')
        for row in box_rows:
            stadium_link = row.select_one('a.popup_btn[href="#stadium"]')
            if stadium_link:
                stadium = normalize_text(stadium_link.text)
                break
            text = normalize_text(row.get_text())
            if "estadio" in text.lower():
                stadium = text
                break
    except Exception as e:
        logging.warning(f"⚠️ Error scraping estadio: {e}")
    finally:
        driver.quit()
        
    return stadium

# --- MAIN FETCH & SYNC ---

def fetch_matches_list_besoccer():
    """Obtiene la lista base de partidos (IDs, Fechas, Rivales)."""
    driver = setup_driver()
    matches = []
    try:
        logging.info(f"📋 Obteniendo lista de partidos: {CONFIG['URL_BESOCCER']}")
        driver.get(CONFIG["URL_BESOCCER"])
        
        # Click cookie si existe
        try:
            wait = WebDriverWait(driver, 5)
            wait.until(EC.element_to_be_clickable((By.ID, "didomi-notice-agree-button"))).click()
        except: pass

        soup = BeautifulSoup(driver.page_source, 'lxml')
        match_elements = soup.select("a.match-link")

        for m in match_elements:
            try:
                start_iso = m.get('starttime')
                if not start_iso: continue
                
                # Parse fecha UTC
                dt_obj = datetime.datetime.fromisoformat(start_iso)
                dt_utc = dt_obj.astimezone(datetime.timezone.utc)
                
                # Estado y Score
                status_tag = m.select_one(".match-status-label .tag")
                status_text = status_tag.text.strip().lower() if status_tag else ""
                
                r1 = m.select_one(".marker .r1")
                r2 = m.select_one(".marker .r2")
                score = f"{r1.text.strip()}-{r2.text.strip()}" if (r1 and r2 and r1.text.isdigit()) else None

                local = normalize_text(m.select_one(".team-name.team_left .name").text)
                visit = normalize_text(m.select_one(".team-name.team_right .name").text)
                comp = normalize_text(m.select_one(".middle-info").text)
                
                # Determinación de TBD (Hora 00:00 o flag hastime=0)
                has_time = m.get('hastime', '1') == '1'
                if dt_utc.hour == 0 and dt_utc.minute == 0: has_time = False
                
                # ID Único
                mid = f"{dt_utc.strftime('%Y%m%d')}_{local[:3]}_{visit[:3]}".lower().replace(" ", "")

                matches.append({
                    'id': mid,
                    'local': local,
                    'visitante': visit,
                    'competicion': comp,
                    'inicio': dt_utc,
                    'is_tbd': not has_time,
                    'status': status_text,
                    'score': score,
                    'link': m.get('href'),
                    'season': '2025-2026' # Asumido por contexto
                })
            except: continue
            
        return matches
    except Exception as e:
        logging.error(f"❌ Error fetching list: {e}")
        return []
    finally:
        driver.quit()

def get_calendar_service():
    restore_auth_files() # Restaurar secretos si es entorno Cloud
    creds = None
    if os.path.exists(CONFIG["TOKEN_FILE"]): 
        creds = Credentials.from_authorized_user_file(CONFIG["TOKEN_FILE"], CONFIG["SCOPES"])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: 
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CONFIG["CREDENTIALS_FILE"], CONFIG["SCOPES"])
            creds = flow.run_local_server(port=0)
        with open(CONFIG["TOKEN_FILE"], 'w') as token: token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

def restore_auth_files():
    """Restaura credentials.json y token.json desde ENV VARS (GitHub Secrets)."""
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if creds_json and not os.path.exists(CONFIG["CREDENTIALS_FILE"]):
        with open(CONFIG["CREDENTIALS_FILE"], "w") as f: f.write(creds_json)
        
    token_b64 = os.getenv("GCP_TOKEN_JSON_B64")
    if token_b64 and not os.path.exists(CONFIG["TOKEN_FILE"]):
        with open(CONFIG["TOKEN_FILE"], "w") as f: 
            f.write(base64.b64decode(token_b64).decode("utf-8"))

def send_telegram(msg):
    if not CONFIG["TELEGRAM_TOKEN"]: return
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
    requests.post(url, json={'chat_id': CONFIG['TELEGRAM_CHAT_ID'], 'text': msg, 'parse_mode': 'HTML'})

def run_sync():
    load_stadium_db()
    
    # 1. Obtener datos TV (Fuente: FutbolEnLaTV)
    tv_map = fetch_tv_schedule_centralized()
    
    # 2. Obtener lista partidos (Fuente: BeSoccer)
    matches = fetch_matches_list_besoccer()
    if not matches: return

    # 3. Identificar el "Próximo Partido" para actualización de estadio
    now = datetime.datetime.now(datetime.timezone.utc)
    next_match_index = -1
    
    # Ordenar por fecha
    matches.sort(key=lambda x: x['inicio'])
    
    for i, m in enumerate(matches):
        if m['inicio'] > now and 'fin' not in m['status']:
            next_match_index = i
            break
            
    # 4. Sincronizar Calendar
    service = get_calendar_service()
    if not service: return
    
    # Cargar eventos existentes para comparar
    existing_events = {}
    page_token = None
    while True:
        events = service.events().list(calendarId=CONFIG["CALENDAR_ID"], singleEvents=True, pageToken=page_token).execute()
        for ev in events.get('items', []):
            eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
            if eid: existing_events[eid] = ev
        page_token = events.get('nextPageToken')
        if not page_token: break

    telegram_msgs = []
    
    for i, match in enumerate(matches):
        mid = match['id']
        is_next = (i == next_match_index)
        
        # --- Lógica TV ---
        # Cruce por fecha (YYYY-MM-DD)
        date_key = match['inicio'].strftime("%Y-%m-%d")
        tv_data = tv_map.get(date_key)
        
        tv_short = tv_data['short'] if tv_data else None
        tv_full = tv_data['full'] if tv_data else None
        
        # Si el partido ya acabó, borramos info TV
        if 'fin' in match['status']: 
            tv_short = None
            tv_full = None

        # --- Lógica Estadio ---
        # Por defecto de DB
        stadium, location = find_stadium_in_db(match['local'])
        
        # SOLO si es el próximo partido, intentamos actualizar scraping
        if is_next and match['link']:
            logging.info(f"🏟️ Verificando estadio próximo partido: {match['local']}")
            web_stadium = scrape_besoccer_stadium(match['link'])
            if web_stadium:
                # Si es diferente o no teníamos, actualizamos
                if not stadium or difflib.SequenceMatcher(None, stadium, web_stadium).ratio() < 0.85:
                    stadium = web_stadium
                    location = f"{web_stadium}, {match['local']}"
                    update_db(match['local'], stadium, location)
        
        final_location = location if location else f"Estadio Local ({match['local']})"
        if CONFIG["TEAM_NAME"] not in match['local'].lower():
            # Si somos visitantes y no tenemos dato, ponemos generico
            if not location: final_location = f"Estadio Visitante ({match['local']})"

        # --- Construcción Evento ---
        icon_comp = '🏆'
        if 'liga' in match['competicion'].lower(): icon_comp = '⚽'
        elif 'champions' in match['competicion'].lower(): icon_comp = '✨'
        
        base_title = f"{match['local']} vs {match['visitante']}"
        if match['score']: base_title = f"{match['local']} {match['score']} {match['visitante']}"
        
        title_suffix = f" | {icon_comp} {match['competicion']}"
        if tv_short: title_suffix += f" | 📺 {tv_short}"
        
        full_title = base_title + title_suffix
        if match['is_tbd']: full_title = "(TBC) " + full_title
        
        desc = f"{icon_comp} {match['competicion']}\n"
        if tv_full: desc += f"📺 Dónde ver: {tv_full}\n"
        else: 
            if not 'fin' in match['status']: desc += "📺 Dónde ver: Canal por confirmar\n"
            
        desc += f"🏟️ Estadio: {stadium if stadium else 'Por confirmar'}\n"
        desc += f"🔗 Info: {match['link']}"

        # Horarios (Google Cal usa ISO string)
        start_dt = match['inicio']
        end_dt = start_dt + datetime.timedelta(hours=2)

        event_body = {
            'summary': full_title,
            'location': final_location,
            'description': desc,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'UTC'},
            'extendedProperties': {'shared': {'match_id': mid}},
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]}
        }
        
        # --- Update or Insert ---
        if mid in existing_events:
            ev = existing_events[mid]
            
            # Detectar cambios
            old_title = normalize_text(ev.get('summary', ''))
            new_title = normalize_text(full_title)
            
            old_desc = normalize_text(ev.get('description', ''))
            new_desc = normalize_text(desc)
            
            old_start = ev['start'].get('dateTime')
            new_start = start_dt.isoformat().replace('+00:00', 'Z') # Normalizar UTC string
            if old_start and old_start.endswith('Z'): old_start = old_start[:-1] # simple hack check
            
            # Check de tiempo (diff > 60s)
            time_diff = 0
            if old_start:
                try:
                    t1 = datetime.datetime.fromisoformat(ev['start']['dateTime'].replace('Z', '+00:00')).timestamp()
                    t2 = start_dt.timestamp()
                    time_diff = abs(t1 - t2)
                except: time_diff = 100
            
            if old_title != new_title or old_desc != new_desc or time_diff > 60:
                service.events().update(calendarId=CONFIG["CALENDAR_ID"], eventId=ev['id'], body=event_body).execute()
                logging.info(f"🔄 Actualizado: {full_title}")
                if time_diff > 60: telegram_msgs.append(f"🔄 <b>Cambio Hora/Día:</b> {full_title}")
                elif "TBC" not in full_title and "TBC" in old_title: telegram_msgs.append(f"✅ <b>Horario Confirmado:</b> {full_title}")
        else:
            service.events().insert(calendarId=CONFIG["CALENDAR_ID"], body=event_body).execute()
            logging.info(f"✅ Nuevo Evento: {full_title}")
            telegram_msgs.append(f"✅ <b>Nuevo Partido:</b> {full_title}")

    save_stadium_db()
    
    if telegram_msgs:
        send_telegram("<b>🔔 Celta Calendar Update</b>\n\n" + "\n".join(telegram_msgs))

def main():
    try:
        run_sync()
        logging.info("🏁 Sincronización finalizada.")
    except Exception as e:
        logging.error(f"❌ Error fatal en main: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    main()