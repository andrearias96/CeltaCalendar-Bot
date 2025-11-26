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
import subprocess # Hardening: Gestión de procesos
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv 

# --- IMPORTACIONES EXTRA PARA MANEJO DE ERRORES SELENIUM ---
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- CONFIGURACIÓN DE ENTORNO ---
os.environ['DBUS_SESSION_BUS_ADDRESS'] = '/dev/null'
os.environ['TZ'] = 'Europe/Madrid' 
try:
    time.tzset() 
except:
    pass

# --- LIBRERÍAS SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select 
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# --- CARGA DE VARIABLES DE ENTORNO ---
load_dotenv() 

# --- CONFIGURACIÓN Y CONSTANTES ---
CONFIG = {
    "CALENDAR_ID": os.getenv("CALENDAR_ID"),
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "TEAM_NAME": "celta",
    "URL_BASE": "https://es.besoccer.com/equipo/partidos/",
    "URL_TV_CELTA": "https://www.futbolenlatv.es/equipo/celta", 
    "SCOPES": ['https://www.googleapis.com/auth/calendar'],
    "CREDENTIALS_FILE": 'credentials.json',
    "TOKEN_FILE": 'token.json',
    "DB_FILE": 'stadiums.json' 
}

# Selectores CSS centralizados (Besoccer)
SELECTORS = {
    "MATCH_LINK": "a.match-link",
    "TEAM_LOCAL": ".team-name.team_left .name",
    "TEAM_VISIT": ".team-name.team_right .name",
    "COMPETITION": ".middle-info",
    "SCORE_R1": ".marker .r1",
    "SCORE_R2": ".marker .r2",
    "STATUS": ".match-status-label .tag",
    "COOKIE_BTN": "didomi-notice-agree-button",
    "SEASON_DROP": "#season" 
}

# --- GLOBAL STATE FOR DB ---
STADIUM_DB = {}
ALIAS_CACHE = {} # Nueva Cache de búsqueda rápida O(1)
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

# --- FUNCIONES DE BASE DE DATOS Y SMART MATCH (OPTIMIZADAS) ---

def normalize_team_key(name):
    """Normaliza nombres de equipos para búsquedas insensibles a mayúsculas/acentos/prefijos."""
    if not name: return ""
    text = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8")
    text = text.lower()
    # Eliminar prefijos comunes que ensucian la búsqueda
    remove_list = [" fc", " cf", " ud", " cd", " sd", "real ", "club ", "deportivo ", "atletico ", "sporting "]
    for item in remove_list:
        text = text.replace(item, " ")
    return " ".join(text.split()).strip()

def load_stadium_db():
    global STADIUM_DB, ALIAS_CACHE
    ALIAS_CACHE = {} # Reset cache
    
    if os.path.exists(CONFIG["DB_FILE"]):
        try:
            with open(CONFIG["DB_FILE"], 'r', encoding='utf-8') as f:
                STADIUM_DB = json.load(f)
            
            # Construir índice inverso (Cache)
            for team_key, data in STADIUM_DB.items():
                # Indexar la clave principal
                norm_key = normalize_team_key(team_key)
                ALIAS_CACHE[norm_key] = team_key
                
                # Indexar todos los alias
                for alias in data.get('aliases', []):
                    ALIAS_CACHE[normalize_team_key(alias)] = team_key
                    
            logging.info(f"💾 Base de datos cargada: {len(STADIUM_DB)} estadios, {len(ALIAS_CACHE)} referencias.")
        except Exception as e:
            logging.error(f"⚠️ Error cargando DB estadios: {e}")
            STADIUM_DB = {}
    else:
        logging.warning("⚠️ No se encontró stadiums.json. Se iniciará vacío.")
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

def find_stadium_dynamic(team_name):
    """
    Busca estadio usando Cache O(1) primero, luego Fuzzy Search.
    Retorna: (Stadium, Location, RealKey) - RealKey es útil para actualizaciones.
    """
    if not STADIUM_DB or not team_name: return None, None, None
    
    clean_target = normalize_team_key(team_name)
    
    # 1. Búsqueda Exacta en Cache (O(1))
    if clean_target in ALIAS_CACHE:
        real_key = ALIAS_CACHE[clean_target]
        entry = STADIUM_DB[real_key]
        return entry.get('stadium'), entry.get('location'), real_key

    # 2. Fuzzy Match (Difflib) sobre las claves normalizadas
    # Usamos las keys del cache porque ya están normalizadas
    matches = difflib.get_close_matches(clean_target, ALIAS_CACHE.keys(), n=1, cutoff=0.85)
    if matches:
        matched_norm_key = matches[0]
        real_key = ALIAS_CACHE[matched_norm_key]
        entry = STADIUM_DB[real_key]
        return entry.get('stadium'), entry.get('location'), real_key

    return None, None, None

def update_db(team_name, stadium, location):
    global STADIUM_DB, ALIAS_CACHE, DB_DIRTY
    
    # Validación básica de calidad de datos
    invalid_terms = ["campo municipal", "estadio local", "campo de futbol", "municipal", "ciudad deportiva"]
    stadium_lower = stadium.lower()
    if any(term == stadium_lower for term in invalid_terms) or len(stadium) < 4:
        logging.info(f"⚠️ Estadio '{stadium}' descartado por ser genérico.")
        return 

    # Buscar si el equipo ya existe (incluso bajo otro nombre/alias)
    _, _, existing_key = find_stadium_dynamic(team_name)
    
    target_key = team_name
    is_new_entry = True

    if existing_key:
        target_key = existing_key
        is_new_entry = False
    
    if is_new_entry:
        logging.info(f"🆕 Nuevo equipo añadido a DB: {team_name} -> {stadium}")
        STADIUM_DB[target_key] = {
            "stadium": stadium,
            "location": location,
            "aliases": [team_name], # Auto-add self as alias
            "last_updated": datetime.datetime.now().strftime("%Y-%m-%d")
        }
    else:
        # Verificar si cambia el estadio
        current_data = STADIUM_DB[target_key]
        old_stadium = current_data.get('stadium', '')
        
        # Actualizar datos si son diferentes
        if old_stadium != stadium:
            logging.info(f"🏟️ Actualizando estadio para '{target_key}': '{old_stadium}' -> '{stadium}'")
            STADIUM_DB[target_key]["stadium"] = stadium
            STADIUM_DB[target_key]["location"] = location
            STADIUM_DB[target_key]["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # Añadir el nombre actual como alias si no existe
        if team_name not in current_data.get('aliases', []):
            if normalize_team_key(team_name) != normalize_team_key(target_key):
                STADIUM_DB[target_key].setdefault('aliases', []).append(team_name)
                logging.info(f"🏷️ Nuevo alias añadido para '{target_key}': {team_name}")

    # Actualizar Cache y Flag
    norm_name = normalize_team_key(team_name)
    ALIAS_CACHE[norm_name] = target_key
    DB_DIRTY = True

# --- FUNCIONES DE AYUDA ---

def normalize_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n')
    text = re.sub('<[^<]+?>', '', text)
    text = " ".join(text.split())
    return text.strip()

def clean_text(text):
    if not text: return ""
    return " ".join(text.strip().split())

def get_competition_details(comp_text):
    text = comp_text.lower()
    if 'promoción' in text: return 'Promoción de ascenso a Primera', '🏆', '3'
    if 'champions' in text: return 'Champions League', '✨', '5'
    if 'intertoto' in text: return 'Copa Intertoto', '🏆', '3'
    if 'segunda división b' in text: return 'Segunda División B', '🅱️', '7'
    if 'segunda división' in text: return 'Segunda División', '2️⃣', '7'
    if 'liga' in text or 'primera' in text: return 'Primera División', '⚽', '7'
    if 'copa' in text or 'rey' in text: return 'Copa del Rey', '🏆', '3'
    if 'europa' in text or 'uefa' in text: return 'Europa League', '🌍', '6'
    return 'Amistoso', '🤝', '8'

def get_round_details(comp_raw):
    if not comp_raw or 'amistoso' in comp_raw.lower(): return ""
    parts = comp_raw.split('.')
    if len(parts) < 2: return "" 
    raw_detail = parts[-1].strip()
    text = raw_detail.lower()
    if 'jornada' in text:
        nums = [s for s in raw_detail.split() if s.isdigit()]
        if nums: return f"J{nums[0]}"
    if 'semi' in text or '1/2' in text: return "Semis"
    if 'cuartos' in text or '1/4' in text: return "Cuartos"
    if 'octavos' in text or '1/8' in text: return "Octavos"
    if '/' in text:
        for word in raw_detail.split():
            if '/' in word: return f"Ronda {word}"
    if 'final' in text: return "Final"
    return ""

# --- NUEVA LÓGICA DE TV ---

def parse_tv_channels(ul_element):
    if not ul_element: return None, None
    channels = []
    items = ul_element.find_all('li')
    for li in items:
        raw_text = li.get_text(separator=" ").strip()
        if any(x in raw_text for x in ["Hellotickets", "LaLiga TV Bar", "Entrada"]): continue
        if "confirmar" in raw_text.lower(): continue
        if "(" in raw_text: raw_text = raw_text.split("(")[0].strip()
        channels.append(clean_text(raw_text))
    
    if not channels: return None, None
    
    free_keywords = ["La 1", "TVE", "Teledeporte", "TVG", "Galicia", "Gol", "Cuatro", "Telecinco"]
    dazn_keywords = ["DAZN"]
    movistar_keywords = ["M+", "Movistar"]
    
    bucket_free, bucket_dazn, bucket_movistar, bucket_others = [], [], [], []
    
    for ch in channels:
        upper_ch = ch.upper()
        if any(k.upper() in upper_ch for k in free_keywords): bucket_free.append(ch)
        elif any(k.upper() in upper_ch for k in dazn_keywords): bucket_dazn.append(ch)
        elif any(k.upper() in upper_ch for k in movistar_keywords): bucket_movistar.append(ch)
        else: bucket_others.append(ch)
            
    final_list = bucket_free + bucket_dazn + bucket_movistar + bucket_others
    if not final_list: return None, None
    full_string = ", ".join(final_list)
    top_channel = final_list[0].upper()
    short_code = "TV"
    
    if any(k.upper() in top_channel for k in free_keywords):
        if "TVG" in top_channel or "GALICIA" in top_channel: short_code = "TVG"
        elif "TELEDEPORTE" in top_channel: short_code = "tdp"
        elif "LA 1" in top_channel or "TVE" in top_channel: short_code = "La1"
        elif "RTVE" in top_channel: short_code = "RTVEPlay"
        else: short_code = "Abierto"
    elif "DAZN" in top_channel: short_code = "DAZN"
    elif "M+" in top_channel or "MOVISTAR" in top_channel: short_code = "M+"
        
    return short_code, full_string

def fetch_tv_summary_from_url(driver):
    tv_data = {}
    logging.info(f"📺 Obteniendo guía TV completa desde {CONFIG['URL_TV_CELTA']}...")
    try:
        driver.get(CONFIG['URL_TV_CELTA'])
        time.sleep(2) 
        while True:
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, "a[id^='btnMoreThan'].btnPrincipal")
                clicked_any = False
                for btn in buttons:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.5)
                        btn.click()
                        logging.info(f"🖱️ Click en '{btn.get_attribute('id')}' para cargar más partidos...")
                        clicked_any = True
                        time.sleep(3) 
                        break 
                if not clicked_any:
                    logging.info("ℹ️ No quedan botones 'Más días' visibles. Carga finalizada.")
                    break
            except Exception as e:
                logging.warning(f"⚠️ Error iterando botones 'Más días': {e}. Deteniendo carga.")
                break

        soup = BeautifulSoup(driver.page_source, 'lxml')
        tables = soup.find_all('table', class_='tablaPrincipal')
        for table in tables:
            rows = table.find_all('tr')
            current_date_str = None
            for row in rows:
                if 'cabeceraTabla' in row.get('class', []):
                    date_text = row.get_text(strip=True)
                    match_date = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_text)
                    if match_date:
                        d, m, y = match_date.groups()
                        current_date_str = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    continue
                if not current_date_str: continue
                canales_td = row.find('td', class_='canales')
                if canales_td:
                    ul_canales = canales_td.find('ul', class_='listaCanales')
                    short, full = parse_tv_channels(ul_canales)
                    if short and full: tv_data[current_date_str] = {'short': short, 'full': full}
                    else: tv_data[current_date_str] = {'short': None, 'full': 'Canal sin confirmar'}
        logging.info(f"📺 Guía TV procesada: {len(tv_data)} fechas encontradas.")
        return tv_data
    except Exception as e:
        logging.warning(f"⚠️ Error obteniendo guía TV global: {e}")
        return {}

# --- DRIVER HARDENING ---

def force_kill_chrome():
    try:
        if os.name == 'posix':
            subprocess.run(['pkill', '-f', 'chrome'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['pkill', '-f', 'chromedriver'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2) 
    except Exception: pass

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
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    chrome_options.add_argument("--lang=es-ES") 
    chrome_options.add_argument("--accept-lang=es-ES")

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        logging.warning(f"⚠️ Error iniciando driver optimizado: {e}. Reintentando básico.")
        force_kill_chrome()
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    driver.set_page_load_timeout(20) 
    driver.set_script_timeout(20)
    return driver

def scrape_besoccer_info(driver, match_link):
    if not match_link: return None, None
    stadium = None
    try:
        driver.set_page_load_timeout(10)
        time.sleep(1)
        driver.get(match_link)
        soup = BeautifulSoup(driver.page_source, 'lxml')
        box_rows = soup.select('.table-body.p10 .table-row-round')
        rows = box_rows if box_rows else soup.select('.table-row-round')
        for row in rows:
            text = clean_text(row.get_text())
            stadium_link = row.select_one('a.popup_btn[href="#stadium"]')
            if stadium_link: stadium = clean_text(stadium_link.text)
            elif "estadio" in text.lower() and not stadium: stadium = text
    except TimeoutException: raise TimeoutException("Timeout interno Selenium")
    except Exception as e: raise WebDriverException(f"Wrapper Error: {e}")
    finally: driver.set_page_load_timeout(20)
    return stadium, None

def get_stadium_info(driver, team_name, match_link=None):
    if not team_name: return None, None
    clean_name = team_name.strip()
    db_stadium, db_location, _ = find_stadium_dynamic(clean_name)
    
    if match_link:
        web_stadium, _ = scrape_besoccer_info(driver, match_link)
        if web_stadium:
            should_update = False
            if not db_stadium: should_update = True
            else:
                ratio = difflib.SequenceMatcher(None, normalize_team_key(db_stadium), normalize_team_key(web_stadium)).ratio()
                if ratio < 0.85: should_update = True
            if should_update: return web_stadium, f"{web_stadium}, {clean_name}" 
    return db_stadium, db_location

def get_euro_max_rounds(comp_name, season_str):
    name = comp_name.lower()
    is_europe = any(x in name for x in ['champions', 'europa league', 'conference'])
    if not is_europe: return None
    try: start_year = int(season_str.split('-')[0])
    except: start_year = 0
    if start_year >= 2024:
        if 'conference' in name: return 6
        elif 'champions' in name or 'europa league' in name: return 8
    return 6

def parse_besoccer_date(iso_date_str):
    try:
        dt_obj = datetime.datetime.fromisoformat(iso_date_str)
        return dt_obj.astimezone(datetime.timezone.utc)
    except Exception: return None

def parse_google_iso(date_str):
    if not date_str: return None
    try:
        if date_str.endswith('Z'): date_str = date_str[:-1] + '+00:00'
        dt = datetime.datetime.fromisoformat(date_str)
        return dt.astimezone(datetime.timezone.utc)
    except: return None

def format_log_date(dt_obj, is_tbd):
    dias = {0:"Lunes", 1:"Martes", 2:"Miércoles", 3:"Jueves", 4:"Viernes", 5:"Sábado", 6:"Domingo"}
    dt_local = dt_obj.astimezone() 
    dia_str = dias.get(dt_local.weekday(), "Día")
    fecha_str = dt_local.strftime("%d/%m")
    hora_str = dt_local.strftime("%H:%M")
    if is_tbd: return f"(Día: {dia_str} {fecha_str}, TBC | Hora: TBC)"
    else: return f"(Día: {dia_str} {fecha_str} | Hora: {hora_str}h)"
    
# --- MAIN LOGIC ---

def fetch_matches(driver):
    logging.info(f"🚀 [Fase A] Obteniendo lista de partidos desde Besoccer...")
    try:
        url_final = f"{CONFIG['URL_BASE']}{CONFIG['TEAM_NAME']}"
        driver.get(url_final)
        wait = WebDriverWait(driver, 20)
        try: 
            driver.find_element(By.ID, SELECTORS["COOKIE_BTN"]).click()
            time.sleep(1)
        except: pass

        matches = []
        try: wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELECTORS["MATCH_LINK"])))
        except: return []

        soup = BeautifulSoup(driver.page_source, 'lxml')
        match_elements = soup.select(SELECTORS["MATCH_LINK"])
        
        for m in match_elements:
            try:
                start_iso = m.get('starttime')
                has_time_attr = m.get('hastime', "1")
                match_link = m.get('href') 
                if not start_iso: continue
                start_utc = parse_besoccer_date(start_iso)
                if not start_utc: continue

                match_year = start_utc.year
                match_month = start_utc.month
                if match_month >= 7: season_text = f"{match_year}-{match_year + 1}"
                else: season_text = f"{match_year - 1}-{match_year}"

                hour = start_utc.astimezone().hour
                minute = start_utc.astimezone().minute
                is_tbd = False
                if str(has_time_attr) == "1": is_tbd = True
                elif hour == 0 and minute == 0: is_tbd = True

                local_elem = m.select_one(SELECTORS["TEAM_LOCAL"])
                visit_elem = m.select_one(SELECTORS["TEAM_VISIT"])
                if not local_elem or not visit_elem: continue
                local = clean_text(local_elem.text)
                visitante = clean_text(visit_elem.text)
                comp_elem = m.select_one(SELECTORS["COMPETITION"])
                comp_raw = clean_text(comp_elem.text) if comp_elem else "Amistoso"
                
                score_text = None
                r1 = m.select_one(SELECTORS["SCORE_R1"])
                r2 = m.select_one(SELECTORS["SCORE_R2"])
                if r1 and r2:
                    t1 = clean_text(r1.text)
                    t2 = clean_text(r2.text)
                    if t1.isdigit() and t2.isdigit(): score_text = f"{t1}-{t2}"

                status_tag = m.select_one(SELECTORS["STATUS"])
                status_text = status_tag.text.strip().lower() if status_tag else ""
                mid = f"{start_utc.strftime('%Y%m%d')}_{local[:3]}_{visitante[:3]}".lower().replace(" ", "")
                
                if CONFIG["TEAM_NAME"] in local.lower(): lugar = f"Estadio Local ({local})"
                else: lugar = f"Estadio Visitante ({local})"

                matches.append({
                    'id': mid, 'local': local, 'visitante': visitante,
                    'competicion': comp_raw, 'inicio': start_utc,
                    'is_tbd': is_tbd, 'lugar': lugar,
                    'score': score_text, 'status': status_text,
                    'link': match_link, 'season': season_text 
                })
            except: continue
        return matches
    except Exception as e: raise e 

def get_calendar_service():
    if os.getenv("GCP_CREDENTIALS_JSON_B64"):
        try:
            with open(CONFIG["CREDENTIALS_FILE"], "wb") as f: f.write(base64.b64decode(os.getenv("GCP_CREDENTIALS_JSON_B64")))
        except Exception as e: logging.error(f"Error decoding credentials: {e}")
    if os.getenv("GCP_TOKEN_JSON_B64"):
        try:
            with open(CONFIG["TOKEN_FILE"], "wb") as f: f.write(base64.b64decode(os.getenv("GCP_TOKEN_JSON_B64")))
        except Exception as e: logging.error(f"Error decoding token: {e}")

    creds = None
    if os.path.exists(CONFIG["TOKEN_FILE"]): creds = Credentials.from_authorized_user_file(CONFIG["TOKEN_FILE"], CONFIG["SCOPES"])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CONFIG["CREDENTIALS_FILE"], CONFIG["SCOPES"])
            creds = flow.run_local_server(port=0)
        with open(CONFIG["TOKEN_FILE"], 'w') as token: token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

def send_telegram(msg):
    if not CONFIG["TELEGRAM_TOKEN"] or not CONFIG["TELEGRAM_CHAT_ID"]: return
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
    try: requests.post(url, json={'chat_id': CONFIG['TELEGRAM_CHAT_ID'], 'text': msg, 'parse_mode': 'HTML'})
    except: pass

def execute_with_retry(request):
    for n in range(0, 5):
        try: return request.execute()
        except Exception as e:
            err_str = str(e).lower()
            if any(x in err_str for x in ["ratelimitexceeded", "403", "ssl", "eof", "connection", "broken pipe"]):
                wait_time = (2 ** n) + 1
                logging.warning(f"⚠️ Error de conexión/API ({e}). Reintentando en {wait_time}s...")
                time.sleep(wait_time)
            else: raise e
    return None

def run_sync():
    load_stadium_db()
    driver = setup_driver() 
    try:
        tv_schedule_map = fetch_tv_summary_from_url(driver)
        matches = fetch_matches(driver)
        if not matches: return

        logging.info("☁️ Sincronizando con Google Calendar...")
        service = get_calendar_service()
        if not service: return

        existing_events = {}
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId=CONFIG["CALENDAR_ID"], singleEvents=True, showDeleted=False, pageToken=page_token
            ).execute()
            for ev in events_result.get('items', []):
                if ev.get('status') != 'cancelled':
                    eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
                    if eid: existing_events[eid] = ev
            page_token = events_result.get('nextPageToken')
            if not page_token: break

        telegram_msgs = []
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        next_match_processed = False 
        
        for i, match in enumerate(matches):
            is_finished = 'fin' in match['status'].lower()
            if match['inicio'] < now_utc and not is_finished and match['id'] not in existing_events: continue

            # --- STADIUM ---
            stadium_name = None
            full_address = None
            
            is_future = match['inicio'] > now_utc
            should_scan_stadium = False
            
            if is_future and not next_match_processed and not match['is_tbd']:
                should_scan_stadium = True
                next_match_processed = True 
            
            # Recuperar key real desde la función optimizada
            s_name_cache, s_loc_cache, real_db_key = find_stadium_dynamic(match['local'])
            
            if should_scan_stadium:
                for attempt in range(3):
                    try:
                        if attempt > 0:
                            logging.info(f"🔄 Reiniciando navegador (Intento {attempt+1})...")
                            try: driver.quit()
                            except: pass
                            force_kill_chrome() 
                            driver = setup_driver()

                        s_name, s_loc = get_stadium_info(driver, match['local'], match.get('link'))
                        stadium_name = s_name
                        full_address = s_loc
                        if s_name: update_db(match['local'], s_name, f"{s_name}, {match['local']}")
                        break 
                    except Exception as e:
                        logging.warning(f"⚠️ Error scraping estadio (Intento {attempt+1}/3): {e}")
                        if attempt == 2: 
                            stadium_name = s_name_cache
                            full_address = s_loc_cache
            else:
                stadium_name = s_name_cache
                full_address = s_loc_cache
                if full_address and 'Estadio Local' in match['lugar']: match['lugar'] = full_address
                elif full_address and 'Estadio Visitante' in match['lugar']: match['lugar'] = full_address

            # --- TV ASSIGNMENT ---
            match_date_key = match['inicio'].strftime("%Y-%m-%d")
            tv_info_full = None
            tv_info_short = None
            if not match['is_tbd'] and not is_finished:
                tv_data_entry = tv_schedule_map.get(match_date_key)
                if tv_data_entry:
                    tv_info_full = tv_data_entry['full']
                    tv_info_short = tv_data_entry['short']
            
            # --- TITLE & DESC ---
            comp_name, icon, color = get_competition_details(match['competicion'])
            match_month = match['inicio'].month
            if 'amistoso' in comp_name.lower() and match_month in [7, 8]: comp_name = 'Pretemporada'
            if match['season'] == '2025-2026' and comp_name == 'Primera División': comp_name = 'Liga'
            
            round_tag = get_round_details(match['competicion'])
            display_tbd = match['is_tbd']
            if display_tbd and match.get('season') != '2025-2026': display_tbd = False

            base_title = f"{match['local']} vs {match['visitante']}"
            if match['score'] and is_finished: base_title = f"{match['local']} {match['score']} {match['visitante']}"
            
            full_title_suffix = f" |{icon}{comp_name}"
            if round_tag and 'amistoso' not in comp_name.lower() and 'pretemporada' not in comp_name.lower():
                full_title_suffix += f" | {round_tag}"
            if tv_info_short and not is_finished: full_title_suffix += f" | {tv_info_short}"
            
            full_title = f"{base_title}{full_title_suffix}"
            if display_tbd: full_title = f"(TBC) {base_title}{full_title_suffix}"
            log_suffix = format_log_date(match['inicio'], display_tbd)
            
            round_str = round_tag
            if round_str.startswith("J") and round_str[1:].isdigit(): 
                round_num = round_str[1:]
                round_str = f"Jornada {round_num}"
                total_rounds = get_euro_max_rounds(comp_name, match['season'])
                if total_rounds: round_str = f"Jornada {round_num} de {total_rounds}"
            season_display = match.get('season', '')

            desc_text = f"{icon} {comp_name}\n"
            desc_text += f"📅 Temporada {season_display}\n"
            if round_str: desc_text += f"▶️ {round_str}\n"
            if tv_info_full and not is_finished: desc_text += f"📺 Dónde ver: {tv_info_full}\n"
            
            loc_final = full_address if full_address else match['lugar'] 
            if stadium_name: desc_text += f"🏟️ Estadio: {stadium_name}\n"
            else: desc_text += f"📍 {match['lugar']}\n"
            desc_text += f"🔗 Más Info: {match.get('link', '')}" 
            if display_tbd: desc_text = "⚠️ Fecha y hora por confirmar (TBC)\n" + desc_text

            custom_reminders = [
                {'method': 'popup', 'minutes': 60},    
                {'method': 'popup', 'minutes': 180},   
                {'method': 'popup', 'minutes': 1440},  
                {'method': 'popup', 'minutes': 4320}   
            ]
            if 'amistoso' in comp_name.lower() or 'pretemporada' in comp_name.lower(): 
                custom_reminders = [{'method': 'popup', 'minutes': 60}]
            custom_reminders.sort(key=lambda x: x['minutes'])

            event_body = {
                'summary': full_title,
                'location': loc_final, 
                'description': desc_text,
                'start': {'dateTime': match['inicio'].isoformat(), 'timeZone': 'UTC'},
                'end': {'dateTime': (match['inicio'] + datetime.timedelta(hours=2)).isoformat(), 'timeZone': 'UTC'},
                'colorId': color,
                'extendedProperties': {'shared': {'match_id': match['id']}},
                'reminders': {'useDefault': False, 'overrides': custom_reminders}
            }

            mid = match['id']
            if mid in existing_events:
                ev = existing_events[mid]
                if is_finished and match['score'] and match['score'] in clean_text(ev.get('summary', '')): continue

                needs_update = False
                notify_telegram = False 
                change_details = [] 

                old_dt = parse_google_iso(ev['start'].get('dateTime'))
                if old_dt:
                    diff = abs(old_dt.timestamp() - match['inicio'].timestamp())
                    if diff > 60: 
                        needs_update = True
                        notify_telegram = True
                        change_details.append(f"⏰ Hora: {old_dt.strftime('%H:%M')} -> {match['inicio'].strftime('%H:%M')}")
                
                old_title_norm = normalize_text(ev.get('summary', ''))
                new_title_norm = normalize_text(full_title)
                if old_title_norm != new_title_norm:
                    needs_update = True
                    if "TBC" in old_title_norm != "TBC" in new_title_norm: notify_telegram = True
                    if match['score'] and match['score'] not in old_title_norm: notify_telegram = True
                    change_details.append(f"📝 Título: '{ev.get('summary')}' -> '{full_title}'")

                if not needs_update:
                    current_desc = normalize_text(ev.get('description', ''))
                    new_desc_norm = normalize_text(desc_text)
                    if current_desc != new_desc_norm: 
                        needs_update = True
                        change_details.append("📄 Descripción (TV/Info)")
                if not needs_update:
                    current_loc = normalize_text(ev.get('location', ''))
                    new_loc_norm = normalize_text(event_body['location'])
                    if current_loc != new_loc_norm: 
                        needs_update = True
                        change_details.append(f"📍 Lugar: '{ev.get('location')}' -> '{event_body['location']}'")
                
                if not needs_update:
                    existing_overrides = ev.get('reminders', {}).get('overrides', []) or []
                    existing_overrides.sort(key=lambda x: x['minutes'])
                    target_overrides = event_body['reminders'].get('overrides', [])
                    if existing_overrides != target_overrides: 
                        needs_update = True
                        change_details.append(f"🔔 Recordatorios (Google: {len(existing_overrides)} vs Local: {len(target_overrides)})")

                if needs_update:
                    req = service.events().update(calendarId=CONFIG["CALENDAR_ID"], eventId=ev['id'], body=event_body)
                    execute_with_retry(req)
                    changes_str = ", ".join(change_details)
                    log_str = f"[+] 🔄 Actualizado: {base_title} | Temporada {season_display} | {icon} {comp_name} {log_suffix} | Cambios: {changes_str}"
                    logging.info(log_str)
                    if notify_telegram: telegram_msgs.append(f"🔄 <b>Actualizado:</b> {full_title}\n{log_suffix}\n<i>Cambios: {changes_str}</i>")
            else:
                req = service.events().insert(calendarId=CONFIG["CALENDAR_ID"], body=event_body)
                execute_with_retry(req)
                log_str = f"[+] ✅ Nuevo: {base_title} | Temporada {season_display} | {icon} {comp_name} {log_suffix}"
                logging.info(log_str)
                telegram_msgs.append(f"✅ <b>Nuevo:</b> {full_title}\n{log_suffix}")

        save_stadium_db()
        if telegram_msgs: 
            send_telegram("<b>🔔 Celta Calendar Update</b>\n\n" + "\n".join(telegram_msgs))
            logging.info(f"📨 Notificación enviada ({len(telegram_msgs)} cambios importantes).")
        else: logging.info("✅ Todo sincronizado. No hubo cambios.")

    except Exception as e: raise e
    finally:
        if driver:
            logging.info("🏁 Cerrando driver...")
            try: driver.quit()
            except Exception: pass 
            force_kill_chrome()

def main():
    try:
        run_sync()
        logging.info("🎉 Fin.")
    except Exception as e:
        logging.error(f"❌ Error fatal: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    main()