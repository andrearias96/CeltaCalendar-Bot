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
import base64  # Añadido para decodificar el token de GitHub
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv 

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

# --- CONFIGURACIÓN DE ENTORNO (FIX CRÍTICO CI) ---
# Evita bloqueos por intentos de conexión a DBUS en entornos headless
os.environ['DBUS_SESSION_BUS_ADDRESS'] = '/dev/null'

# --- CARGA DE VARIABLES DE ENTORNO ---
load_dotenv() 

# --- CONFIGURACIÓN Y CONSTANTES ---
CONFIG = {
    "CALENDAR_ID": os.getenv("CALENDAR_ID"),
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "TEAM_NAME": "celta",
    "URL_BASE": "https://es.besoccer.com/equipo/partidos/",
    "SCOPES": ['https://www.googleapis.com/auth/calendar'],
    "CREDENTIALS_FILE": 'credentials.json',
    "TOKEN_FILE": 'token.json',
    "DB_FILE": 'stadiums.json' 
}

# Selectores CSS centralizados
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

# --- FUNCIONES DE BASE DE DATOS Y SMART MATCH ---

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

def normalize_team_key(name):
    """
    Normaliza nombres para búsqueda difusa: minusculas, sin tildes, sin FC/UD/CF.
    """
    if not name: return ""
    # 1. Unicode normalization (quitar tildes)
    text = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8")
    text = text.lower()
    # 2. Quitar términos genéricos comunes
    remove_list = [" fc", " cf", " ud", " cd", " sd", "real ", "club ", "deportivo ", "atletico "]
    for item in remove_list:
        text = text.replace(item, " ")
    return " ".join(text.split()).strip()

def find_stadium_dynamic(team_name):
    """
    Busca en la DB usando Fuzzy Matching y Alias.
    Retorna (stadium, location) o (None, None).
    """
    if not STADIUM_DB: return None, None
    
    clean_target = normalize_team_key(team_name)
    
    # 1. Búsqueda directa exacta (Keys)
    if team_name in STADIUM_DB:
        entry = STADIUM_DB[team_name]
        return entry.get('stadium'), entry.get('location')

    # 2. Búsqueda Fuzzy sobre Keys
    db_keys = list(STADIUM_DB.keys())
    # Pre-calcular claves normalizadas para matching (costoso pero necesario)
    norm_keys_map = {normalize_team_key(k): k for k in db_keys}
    
    matches = difflib.get_close_matches(clean_target, norm_keys_map.keys(), n=1, cutoff=0.85)
    
    if matches:
        real_key = norm_keys_map[matches[0]]
        # logging.info(f"⚡ Smart Match: '{team_name}' identificado como '{real_key}'") # LOG REMOVED
        entry = STADIUM_DB[real_key]
        return entry.get('stadium'), entry.get('location')
    
    # 3. Búsqueda en Aliases (Iterativa)
    for key, data in STADIUM_DB.items():
        aliases = data.get('aliases', [])
        if team_name in aliases:
            return data.get('stadium'), data.get('location')
            
    return None, None

def update_db(team_name, stadium, location):
    global STADIUM_DB, DB_DIRTY
    
    # Quality Gate
    invalid_terms = ["campo municipal", "estadio local", "campo de futbol", "municipal"]
    if any(term in stadium.lower() for term in invalid_terms) and len(stadium) < 15:
        return # No guardamos basura genérica
    
    # Check if update or new
    if team_name in STADIUM_DB:
        old_stadium = STADIUM_DB[team_name].get('stadium', 'Desconocido')
        if old_stadium != stadium:
            logging.info(f"🏟️ Cambio de estadio detectado para {team_name}: '{old_stadium}' -> '{stadium}'")
    else:
        logging.info(f"🆕 Nuevo equipo añadido a DB: {team_name} -> {stadium}")
        
    STADIUM_DB[team_name] = {
        "stadium": stadium,
        "location": location,
        "aliases": [],
        "last_updated": datetime.datetime.now().strftime("%Y-%m-%d")
    }
    DB_DIRTY = True

# --- FUNCIONES DE AYUDA (EXISTENTES) ---

def normalize_text(text):
    """
    Normaliza el texto para comparaciones estrictas (elimina HTML, espacios extra, etc).
    Esencial para evitar bucles de actualización infinitos.
    """
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

def get_short_tv_name(full_tv_name):
    if not full_tv_name: return ""
    upper_name = full_tv_name.upper()
    if "M+" in upper_name or "MOVISTAR" in upper_name: return "M+"
    if "DAZN" in upper_name: return "DAZN"
    if "GOL" in upper_name: return "Gol"
    if "TVG" in upper_name or "GALICIA" in upper_name: return "TVG"
    if "TVE" in upper_name or "LA 1" in upper_name: return "TVE"
    if "TELEICINCO" in upper_name: return "T5"
    if "CUATRO" in upper_name: return "Cuatro"
    first_word = full_tv_name.split()[0]
    if len(first_word) <= 5: return first_word
    return "TV"

def fetch_tv_schedule(team_name_filter):
    tv_schedule = {}
    url = "https://www.futbolenlatv.es/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        logging.info("📺 Consultando cartelera TV...")
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200: return {}
        soup = BeautifulSoup(res.text, 'lxml')
        team_elements = soup.find_all(string=re.compile(team_name_filter, re.IGNORECASE))
        now_ref = datetime.datetime.now()

        for elem in team_elements:
            try:
                match_container = elem.find_parent(['li', 'tr'])
                if not match_container: continue
                channel_text = ""
                found_channels = match_container.select('.listaCanales a')
                if found_channels:
                    candidates = [c.get_text(strip=True) for c in found_channels]
                    dazn_match = next((c for c in candidates if "DAZN" in c.upper()), None)
                    if dazn_match: channel_text = dazn_match
                    else: channel_text = candidates[0]
                else:
                    links = match_container.find_all('a')
                    for link in links:
                        if "canales" in link.get('href', ''): continue 
                        if len(link.text) > 2:
                            channel_text = link.text.strip()
                            break
                    if not channel_text:
                        text_parts = list(match_container.stripped_strings)
                        if len(text_parts) > 0:
                            candidates = [t for t in text_parts if len(t) > 2 and ":" not in t and team_name_filter.lower() not in t.lower()]
                            if candidates: channel_text = candidates[-1]

                if "(" in channel_text: channel_text = channel_text.split("(")[0].strip()

                if channel_text:
                    date_header = match_container.find_previous(['div', 'h2', 'h3'], class_=re.compile(r'date|dia|header'))
                    match_date_str = None
                    if date_header:
                        header_text = date_header.get_text()
                        date_match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', header_text)
                        if date_match:
                            day, month, year = date_match.groups()
                            if year: year_to_use = int(year)
                            else:
                                year_to_use = now_ref.year
                                if now_ref.month >= 10 and int(month) <= 3: year_to_use += 1
                            match_date_str = f"{year_to_use}-{int(month):02d}-{int(day):02d}"
                    if match_date_str: tv_schedule[match_date_str] = channel_text

            except Exception as e: continue
        return tv_schedule
    except Exception as e:
        logging.warning(f"⚠️ Error TV scraper: {e}")
        return {}

def scrape_besoccer_info(match_link):
    """
    Extrae Estadio y TV de la página de detalles del partido.
    Busca robustamente en las filas .table-row-round usando lógica mejorada.
    MODIFICADO: Usa Selenium en lugar de Requests para evitar error 406.
    """
    if not match_link: return None, None
    
    stadium = None
    tv_text = None
    driver = None

    try:
        # Pausa táctica para permitir limpieza de sockets del OS antes de iniciar nueva instancia
        time.sleep(2)

        # Configuración de Selenium (Hardened para CI)
        chrome_options = Options()
        chrome_options.page_load_strategy = 'eager' # <-- FIX CRÍTICO: No espera carga completa
        chrome_options.add_argument("--headless=new") 
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument("--window-size=1280,720") # Reducido para ahorrar RAM
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-infobars")
        chrome_options.add_argument("--disable-setuid-sandbox")
        
        # Flags Críticos Anti-Timeout
        chrome_options.add_argument("--remote-debugging-pipe") 
        chrome_options.add_argument("--disable-search-engine-choice-screen")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_argument("--disable-popup-blocking")
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--disable-software-rasterizer") # Renderizado software eficiente
        chrome_options.add_argument("--disable-blink-features=AutomationControlled") # Evasión básica
        chrome_options.add_argument("--dns-prefetch-disable")
        
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        
        logging.info(f"🕵️ Scrapeando detalles (Selenium Anti-406): {match_link}")

        # Iniciar Driver efímero para esta petición
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        driver.get(match_link)
        
        # Espera táctica para carga de JS y evasión de bots
        time.sleep(3)
        
        # Parsear el HTML generado por Selenium
        soup = BeautifulSoup(driver.page_source, 'lxml')
            
        # Priorizar la "caja" específica donde suele estar la info
        box_rows = soup.select('.table-body.p10 .table-row-round')
        rows = box_rows if box_rows else soup.select('.table-row-round')
        
        logging.info(f"   Filas encontradas para análisis: {len(rows)}")

        for row in rows:
            text = clean_text(row.get_text())
            
            # --- DETECTAR ESTADIO ---
            # Prioridad 1: Link específico de estadio
            stadium_link = row.select_one('a.popup_btn[href="#stadium"]')
            if stadium_link:
                stadium = clean_text(stadium_link.text)
            # Prioridad 2: Texto contiene 'estadio' y no 'TV'
            elif "estadio" in text.lower() and not stadium:
                stadium = text

            # --- DETECTAR TV (Lógica mejorada según snippet) ---
            is_tv_row = False
            
            # 1. Busqueda por icono SVG específico (aria-label o title o href)
            tv_icon_aria = row.select_one('svg[aria-label="TV"], svg[aria-label="Televisión"]')
            tv_title = row.select_one('svg title')
            use_tag = row.select_one('use')
            
            if tv_icon_aria:
                is_tv_row = True
            elif tv_title and "TV" in tv_title.text:
                is_tv_row = True
            elif use_tag and ('ic_tv' in use_tag.get('href', '') or '#tv' in use_tag.get('href', '')):
                is_tv_row = True
                
            # 2. Busqueda por palabras clave si falla el icono
            keywords = ["MOVISTAR", "DAZN", "LA 1", "TVG", "GOL PLAY", "TELECINCO", "CUATRO", "ORANGE"]
            if not is_tv_row and any(k in text.upper() for k in keywords):
                is_tv_row = True

            if is_tv_row and "estadio" not in text.lower():
                content_div = row.select_one('.ta-r')
                if content_div:
                    raw_tv = content_div.get_text(separator=' ', strip=True)
                else:
                    raw_tv = text

                raw_tv = re.sub(r'^TV\s*', '', raw_tv, flags=re.IGNORECASE)
                clean_tv = raw_tv.replace('(Esp)', '').replace('|', '/').strip()
                tv_text = " ".join(clean_tv.split())
        
        if tv_text:
            logging.info(f"   ✅ TV detectada: {tv_text}")
        else:
            logging.info("   ❌ No se detectó información de TV.")

    except Exception as e:
        logging.warning(f"⚠️ Error scraping info (Estadio/TV) from link: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
    
    return stadium, tv_text

def get_stadium_info(team_name, match_link=None, match_status="", is_tbd=False):
    """
    Lógica de obtención de estadio y TV revisada.
    """
    if not team_name: return None, None, None
    clean_name = team_name.strip()
    
    # 1. Consulta a DB
    db_stadium, db_location = find_stadium_dynamic(clean_name)
    
    final_stadium = db_stadium
    final_location = db_location
    final_tv = None
    
    # 2. Verificación con página del partido
    is_upcoming = 'fin' not in match_status.lower()
    
    # NUEVA LÓGICA: Solo scrapear si NO es TBD y NO ha finalizado (Upcoming real)
    if match_link and not is_tbd and is_upcoming:
        web_stadium, web_tv = scrape_besoccer_info(match_link)
        
        if web_tv: final_tv = web_tv

        if web_stadium:
            should_update = False
            if not final_stadium:
                should_update = True
            else:
                ratio = difflib.SequenceMatcher(None, normalize_team_key(final_stadium), normalize_team_key(web_stadium)).ratio()
                if ratio < 0.85: 
                    should_update = True
            
            if should_update:
                final_stadium = web_stadium
                final_location = f"{web_stadium}, {clean_name}" 
                update_db(clean_name, final_stadium, final_location)

    return final_stadium, final_location, final_tv

def get_euro_max_rounds(comp_name, season_str):
    """
    Retorna el número total de jornadas para competiciones europeas en fase de liga.
    """
    name = comp_name.lower()
    is_europe = any(x in name for x in ['champions', 'europa league', 'conference'])
    if not is_europe: return None
    
    try:
        start_year = int(season_str.split('-')[0])
    except:
        start_year = 0

    if start_year >= 2024:
        if 'conference' in name:
            return 6
        elif 'champions' in name or 'europa league' in name:
            return 8
            
    return 6

def parse_besoccer_date(iso_date_str):
    try:
        dt_obj = datetime.datetime.fromisoformat(iso_date_str)
        dt_utc = dt_obj.astimezone(datetime.timezone.utc)
        return dt_utc
    except Exception as e:
        return None

def parse_google_iso(date_str):
    if not date_str: return None
    try:
        if date_str.endswith('Z'):
            date_str = date_str[:-1] + '+00:00'
        dt = datetime.datetime.fromisoformat(date_str)
        return dt.astimezone(datetime.timezone.utc)
    except:
        return None

def format_log_date(dt_obj, is_tbd):
    dias = {0:"Lunes", 1:"Martes", 2:"Miércoles", 3:"Jueves", 4:"Viernes", 5:"Sábado", 6:"Domingo"}
    dt_local = dt_obj.astimezone() 
    dia_str = dias.get(dt_local.weekday(), "Día")
    fecha_str = dt_local.strftime("%d/%m")
    hora_str = dt_local.strftime("%H:%M")
    if is_tbd: return f"(Día: {dia_str} {fecha_str}, TBC | Hora: TBC)"
    else: return f"(Día: {dia_str} {fecha_str} | Hora: {hora_str}h)"

# --- HELPER: RESTORE AUTH FROM SECRETS (GITHUB ACTIONS) ---
def restore_auth_files():
    """
    Si detecta variables de entorno de GitHub Secrets, reconstruye
    los archivos credentials.json y token.json para que el script funcione
    como si estuviera en local.
    """
    # 1. Reconstruir credentials.json
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if creds_json and not os.path.exists(CONFIG["CREDENTIALS_FILE"]):
        try:
            logging.info("🔑 Detectado entorno Cloud: Restaurando credentials.json...")
            with open(CONFIG["CREDENTIALS_FILE"], "w", encoding="utf-8") as f:
                f.write(creds_json)
        except Exception as e:
            logging.error(f"❌ Error restaurando credentials.json: {e}")

    # 2. Reconstruir token.json (Base64 decoded)
    token_b64 = os.getenv("GCP_TOKEN_JSON_B64")
    if token_b64 and not os.path.exists(CONFIG["TOKEN_FILE"]):
        try:
            logging.info("🔑 Detectado entorno Cloud: Restaurando token.json...")
            # Decodificar Base64 a bytes, luego a string
            token_bytes = base64.b64decode(token_b64)
            token_str = token_bytes.decode("utf-8")
            with open(CONFIG["TOKEN_FILE"], "w", encoding="utf-8") as f:
                f.write(token_str)
        except Exception as e:
            logging.error(f"❌ Error restaurando token.json: {e}")

# --- SCRAPER ---

def fetch_matches():
    logging.info(f"🚀 Arrancando navegador para {CONFIG['TEAM_NAME']}...")
    chrome_options = Options()
    # MODIFICACIÓN CRÍTICA: Headless New Mode + Pipe (Fix ReadTimeout)
    chrome_options.page_load_strategy = 'eager' # <-- FIX CRÍTICO: No espera carga completa
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--window-size=1280,720") # Reducido
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-setuid-sandbox")
    
    # Flags Críticos Anti-Timeout
    chrome_options.add_argument("--remote-debugging-pipe")
    chrome_options.add_argument("--disable-search-engine-choice-screen") 
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--dns-prefetch-disable")

    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        url_final = f"{CONFIG['URL_BASE']}{CONFIG['TEAM_NAME']}"
        driver.get(url_final)
        wait = WebDriverWait(driver, 20)
        
        try: 
            driver.find_element(By.ID, SELECTORS["COOKIE_BTN"]).click()
            time.sleep(1)
        except: pass

        matches = []
        logging.info("🔎 Escaneando pestaña actual (Temporada Activa)...")
        try: wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELECTORS["MATCH_LINK"])))
        except: 
            logging.warning("   ! No se detectaron partidos en esta vista.")
            return []

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
                if hour == 0 and minute == 0: is_tbd = True
                elif str(has_time_attr) == "0" and hour == 0: is_tbd = True

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
        
        logging.info(f"✅ Total acumulado: {len(matches)} partidos.")
        return matches

    except Exception as e:
        raise e 
    finally: 
        if driver: driver.quit()

# --- GOOGLE ---

def get_calendar_service():
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
            if "rateLimitExceeded" in str(e) or "403" in str(e):
                wait_time = (2 ** n) + 1
                logging.warning(f"⚠️ Rate Limit Google. Esperando {wait_time}s...")
                time.sleep(wait_time)
            else: raise e
    return None

def run_sync():
    # 0. Restaurar Auth si estamos en Cloud
    restore_auth_files()

    # 1. Cargar DB al inicio
    load_stadium_db()

    tv_schedule_map = fetch_tv_schedule(CONFIG["TEAM_NAME"])
    matches = fetch_matches()
    if not matches: 
        logging.info("⚠️ No se encontraron partidos.")
        return

    logging.info("☁️ Sincronizando con Google Calendar...")
    service = get_calendar_service()
    if not service: return

    existing_events = {}
    page_token = None
    while True:
        events_result = service.events().list(
            calendarId=CONFIG["CALENDAR_ID"], 
            singleEvents=True, 
            showDeleted=False, 
            pageToken=page_token
        ).execute()
        
        for ev in events_result.get('items', []):
            if ev.get('status') != 'cancelled':
                eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
                if eid: existing_events[eid] = ev
        
        page_token = events_result.get('nextPageToken')
        if not page_token: break

    telegram_msgs = []
    console_msgs = [] 

    for match in matches:
        mid = match['id']
        comp_name, icon, color = get_competition_details(match['competicion'])
        
        match_month = match['inicio'].month
        if 'amistoso' in comp_name.lower() and match_month in [7, 8]: comp_name = 'Pretemporada'
        if match['season'] == '2025-2026' and comp_name == 'Primera División': comp_name = 'Liga'

        round_tag = get_round_details(match['competicion'])
        
        # --- ESTADIO Y TV LOGIC REVISADA ---
        stadium_name = None
        full_address = None
        existing_loc = None
        
        # Recuperar TV existente del evento para NO re-scrapear si ya existe
        match_tv_existing = None
        if mid in existing_events: 
            existing_loc = existing_events[mid].get('location')
            desc = existing_events[mid].get('description', '')
            m = re.search(r'📺 Dónde ver: (.*)', desc)
            if m: match_tv_existing = m.group(1).strip()

        stadium_name, full_address, besoccer_tv = get_stadium_info(match['local'], match.get('link'), match['status'], match['is_tbd'])
        
        if not stadium_name and existing_loc and "Estadio Local" not in existing_loc and "Estadio Visitante" not in existing_loc:
             full_address = existing_loc
             stadium_name = existing_loc.split(',')[0]

        # Lógica de Consolidación de TV (Prioridad: External > Calendar Cache > Besoccer)
        match_date_key = match['inicio'].strftime("%Y-%m-%d")
        external_tv = tv_schedule_map.get(match_date_key)
        
        tv_info_raw = None
        if external_tv:
            tv_info_raw = external_tv
        elif match_tv_existing: # Si ya tenemos TV en calendario, mantenerla
            tv_info_raw = match_tv_existing
        else:
            tv_info_raw = besoccer_tv
        
        is_finished = 'fin' in match['status'].lower()
        if is_finished:
            tv_info_raw = None

        tv_info_short = get_short_tv_name(tv_info_raw) if tv_info_raw else None
        
        display_tbd = match['is_tbd']
        if display_tbd and match.get('season') != '2025-2026': display_tbd = False

        base_title = f"{match['local']} vs {match['visitante']}"
        if match['score'] and is_finished: base_title = f"{match['local']} {match['score']} {match['visitante']}"
        
        full_title_suffix = f" |{icon}{comp_name}"
        if round_tag and 'amistoso' not in comp_name.lower() and 'pretemporada' not in comp_name.lower():
             full_title_suffix += f" | {round_tag}"
        if tv_info_short: full_title_suffix += f" | {tv_info_short}"
        
        full_title = f"{base_title}{full_title_suffix}"
        if display_tbd: full_title = f"(TBC) {base_title}{full_title_suffix}"

        log_suffix = format_log_date(match['inicio'], display_tbd)
        specific_url = match.get('link', '')
        
        round_str = round_tag
        if round_str.startswith("J") and round_str[1:].isdigit(): 
            round_num = round_str[1:]
            round_str = f"Jornada {round_num}"
            total_rounds = get_euro_max_rounds(comp_name, match['season'])
            if total_rounds:
                round_str = f"Jornada {round_num} de {total_rounds}"

        season_display = match.get('season', '')

        desc_text = f"{icon} {comp_name}\n"
        desc_text += f"📅 Temporada {season_display}\n"
        if round_str: desc_text += f"▶️ {round_str}\n"
        if tv_info_raw: desc_text += f"📺 Dónde ver: {tv_info_raw}\n"
        if stadium_name: desc_text += f"🏟️ Estadio: {stadium_name}\n"
        else: desc_text += f"📍 {match['lugar']}\n"
        desc_text += f"🔗 Más Info: {specific_url}" 
        
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
            'location': full_address if full_address else match['lugar'],
            'description': desc_text,
            'start': {'dateTime': match['inicio'].isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': (match['inicio'] + datetime.timedelta(hours=2)).isoformat(), 'timeZone': 'UTC'},
            'colorId': color,
            'extendedProperties': {'shared': {'match_id': mid}},
            'reminders': {'useDefault': False, 'overrides': custom_reminders}
        }

        if mid in existing_events:
            ev = existing_events[mid]
            if 'fin' in match['status'].lower() and match['score'] and match['score'] in clean_text(ev.get('summary', '')):
                continue

            needs_update = False
            notify_telegram = False 

            current_calendar_title = normalize_text(ev.get('summary', ''))
            new_scraper_title = normalize_text(full_title)

            if current_calendar_title != new_scraper_title: 
                needs_update = True
                if base_title in current_calendar_title and base_title in new_scraper_title:
                     notify_telegram = False
                else:
                     notify_telegram = True 
            
            if not needs_update:
                old_dt = parse_google_iso(ev['start'].get('dateTime'))
                if old_dt and abs((old_dt - match['inicio']).total_seconds()) > 60: 
                    needs_update = True
                    notify_telegram = True 
            
            current_desc = normalize_text(ev.get('description', ''))
            new_desc = normalize_text(desc_text)
            if current_desc != new_desc: 
                needs_update = True
            
            current_loc = normalize_text(ev.get('location', ''))
            new_loc = normalize_text(event_body['location'])
            if current_loc != new_loc: 
                needs_update = True

            existing_overrides = ev.get('reminders', {}).get('overrides', [])
            if existing_overrides: existing_overrides.sort(key=lambda x: x.get('minutes', 0))
            target_overrides = event_body['reminders'].get('overrides', [])

            if ev.get('reminders', {}).get('useDefault') is True: needs_update = True
            elif existing_overrides != target_overrides: needs_update = True

            if needs_update:
                req = service.events().update(calendarId=CONFIG["CALENDAR_ID"], eventId=ev['id'], body=event_body)
                execute_with_retry(req)
                log_str = f"[+] 🔄 Actualizado: {base_title} | Temporada {season_display} | {icon} {comp_name} {log_suffix}"
                console_msgs.append(log_str)
                logging.info(log_str)
                
                if notify_telegram:
                    telegram_msgs.append(f"🔄 <b>Actualizado:</b> {full_title}\n{log_suffix}")
        else:
            req = service.events().insert(calendarId=CONFIG["CALENDAR_ID"], body=event_body)
            execute_with_retry(req)
            log_str = f"[+] ✅ Nuevo: {base_title} | Temporada {season_display} | {icon} {comp_name} {log_suffix}"
            console_msgs.append(log_str)
            telegram_msgs.append(f"✅ <b>Nuevo:</b> {full_title}\n{log_suffix}")
            logging.info(log_str)

    # 4. Guardar DB si hubo cambios (Lazy Write)
    save_stadium_db()

    if telegram_msgs: 
        send_telegram("<b>🔔 Celta Calendar Update</b>\n\n" + "\n".join(telegram_msgs))
        logging.info(f"📨 Notificación enviada ({len(telegram_msgs)} cambios importantes).")
    else:
        if console_msgs: logging.info("✅ Actualizaciones realizadas (sin notificar a Telegram por ser menores/estadio/TV).")
        else: logging.info("✅ Todo sincronizado. No hubo cambios.")

def main():
    try:
        run_sync()
        logging.info("🎉 Fin.")
    except Exception as e:
        logging.error(f"❌ Error fatal: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    main()