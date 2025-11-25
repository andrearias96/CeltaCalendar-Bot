import os
import time
import datetime
import logging
import requests
import traceback
import locale
import re 
import json 
import difflib 
import unicodedata 
import base64
import subprocess
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv 
from bs4 import BeautifulSoup

# --- LIBRERÍAS SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager

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
    "BESOCCER_URL": "https://es.besoccer.com/equipo/partidos/celta",
    "TV_URL": "https://www.futbolenlatv.es/equipo/celta",
    "SCOPES": ['https://www.googleapis.com/auth/calendar'],
    "CREDENTIALS_FILE": 'credentials.json',
    "TOKEN_FILE": 'token.json',
    "DB_FILE": 'stadiums.json' 
}

# --- GLOBAL STATE ---
STADIUM_DB = {}
DB_DIRTY = False

logging.basicConfig(level=logging.INFO, format='%(message)s')

# --- DB FUNCTIONS ---

def load_stadium_db():
    global STADIUM_DB
    if os.path.exists(CONFIG["DB_FILE"]):
        try:
            with open(CONFIG["DB_FILE"], 'r', encoding='utf-8') as f:
                STADIUM_DB = json.load(f)
            logging.info(f"💾 DB Estadios cargada ({len(STADIUM_DB)} registros).")
        except:
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
            logging.error(f"❌ Error guardando DB: {e}")

def normalize_key(name):
    if not name: return ""
    text = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower()
    remove = [" fc", " cf", " ud", " cd", " real ", " club ", " deportivo "]
    for item in remove: text = text.replace(item, " ")
    return " ".join(text.split()).strip()

def find_stadium_in_db(team_name):
    if not STADIUM_DB: return None, None
    key = normalize_key(team_name)
    
    # 1. Exacto
    if team_name in STADIUM_DB: return STADIUM_DB[team_name].get('stadium'), STADIUM_DB[team_name].get('location')
    
    # 2. Fuzzy Key
    db_keys = list(STADIUM_DB.keys())
    norm_map = {normalize_key(k): k for k in db_keys}
    matches = difflib.get_close_matches(key, norm_map.keys(), n=1, cutoff=0.85)
    if matches:
        real_key = norm_map[matches[0]]
        return STADIUM_DB[real_key].get('stadium'), STADIUM_DB[real_key].get('location')
    
    return None, None

def update_db_entry(team_name, stadium):
    global STADIUM_DB, DB_DIRTY
    if not team_name or not stadium: return
    
    invalid = ["campo municipal", "estadio local", "campo de futbol"]
    if any(i in stadium.lower() for i in invalid) and len(stadium) < 15: return

    location = f"{stadium}, {team_name}"
    
    if team_name in STADIUM_DB:
        if STADIUM_DB[team_name].get('stadium') != stadium:
            logging.info(f"🏟️ Actualizando estadio {team_name}: {stadium}")
            STADIUM_DB[team_name]['stadium'] = stadium
            STADIUM_DB[team_name]['location'] = location
            STADIUM_DB[team_name]['last_updated'] = datetime.datetime.now().strftime("%Y-%m-%d")
            DB_DIRTY = True
    else:
        logging.info(f"🆕 Nuevo estadio {team_name}: {stadium}")
        STADIUM_DB[team_name] = {
            "stadium": stadium, "location": location, "aliases": [],
            "last_updated": datetime.datetime.now().strftime("%Y-%m-%d")
        }
        DB_DIRTY = True

# --- DRIVER SETUP (ROBUST) ---

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
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--lang=es-ES")
    
    import socket
    socket.setdefaulttimeout(120)

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except:
        force_kill_chrome()
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    driver.set_page_load_timeout(30)
    
    try:
        driver.execute_cdp_cmd('Emulation.setGeolocationOverride', {
            'latitude': 40.4168, 'longitude': -3.7038, 'accuracy': 100
        })
        driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {
            'headers': {'Accept-Language': 'es-ES,es;q=0.9'}
        })
    except: pass
    
    return driver

# --- SCRAPING LOGIC ---

def clean_text(text):
    if not text: return ""
    return " ".join(text.strip().split())

def fetch_matches_besoccer(driver):
    """Obtiene la lista base de partidos desde Besoccer (IDs y fechas fiables)."""
    logging.info("⚽ Obteniendo calendario base (Besoccer)...")
    matches = []
    try:
        driver.get(CONFIG["BESOCCER_URL"])
        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.match-link")))
        except:
            pass # Puede que no haya partidos o cargue lento
        
        soup = BeautifulSoup(driver.page_source, 'lxml')
        items = soup.select("a.match-link")
        
        for m in items:
            try:
                start_iso = m.get('starttime')
                if not start_iso: continue
                
                # Parse Fecha
                dt = datetime.datetime.fromisoformat(start_iso).replace(tzinfo=datetime.timezone.utc)
                
                # Parse Equipos
                local = clean_text(m.select_one(".team-name.team_left .name").text)
                visit = clean_text(m.select_one(".team-name.team_right .name").text)
                
                # ID Único
                mid = f"{dt.strftime('%Y%m%d')}_{local[:3]}_{visit[:3]}".lower().replace(" ", "")
                
                # Info Extra
                comp = clean_text(m.select_one(".middle-info").text) if m.select_one(".middle-info") else "Amistoso"
                score = None
                r1 = m.select_one(".marker .r1")
                r2 = m.select_one(".marker .r2")
                if r1 and r2 and r1.text.strip().isdigit(): score = f"{r1.text.strip()}-{r2.text.strip()}"
                
                status = m.select_one(".match-status-label .tag").text.strip().lower() if m.select_one(".match-status-label .tag") else ""
                link = m.get('href')
                
                # Check TBD (Besoccer suele marcarlo con hora 00:00 o atributo hastime=0)
                is_tbd = m.get('hastime') == '0' or (dt.hour == 0 and dt.minute == 0)

                matches.append({
                    "id": mid, "local": local, "visitante": visit,
                    "inicio": dt, "competicion": comp, "score": score,
                    "status": status, "link": link, "is_tbd": is_tbd,
                    "date_key": dt.strftime("%Y-%m-%d") # Clave para cruzar con TV
                })
            except Exception as e: continue
            
    except Exception as e:
        logging.error(f"⚠️ Error Besoccer: {e}")
    
    return matches

def fetch_tv_data_bulk(driver):
    """
    Scrapea futbolenlatv.es/equipo/celta.
    Abre todos los desplegables y devuelve Dict { 'YYYY-MM-DD': {'short': '...', 'long': '...'} }
    """
    logging.info("📺 Obteniendo cartelera TV completa...")
    tv_data = {}
    
    try:
        driver.get(CONFIG["TV_URL"])
        wait = WebDriverWait(driver, 10)
        
        # 1. Desplegar todos los días ("Más días")
        # Buscamos botones visibles que contengan "Más días"
        attempts = 0
        while attempts < 10:
            try:
                # Buscar botones de paginación visibles
                buttons = driver.find_elements(By.XPATH, "//a[contains(@id, 'btnMoreThan') and not(contains(@style,'display: none'))]")
                if not buttons: break # No hay más botones visibles
                
                clicked = False
                for btn in buttons:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        time.sleep(0.5) # Pequeña pausa para que renderice el JS
                
                if not clicked: break
                attempts += 1
            except: break
            
        # 2. Parsear HTML
        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        # Las tablas tienen clase 'tablaPrincipal'
        tables = soup.select("table.tablaPrincipal")
        current_date_str = None
        
        for table in tables:
            # Buscar cabecera de fecha (a veces está en la tabla anterior o en la primera fila)
            # La estructura es: tr.cabeceraTabla -> td -> "Domingo, 30/11/2025"
            header = table.find("tr", class_="cabeceraTabla")
            if header:
                date_text = header.get_text(strip=True)
                # Extraer fecha DD/MM/YYYY
                match_date = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_text)
                if match_date:
                    d, m, y = match_date.groups()
                    current_date_str = f"{y}-{int(m):02d}-{int(d):02d}"
            
            if not current_date_str: continue

            # Buscar filas de partidos dentro de la tabla
            rows = table.find_all("tr")
            for row in rows:
                if "cabeceraTabla" in row.get("class", []): continue
                
                # Verificar si es el Celta (para evitar errores en tablas compartidas)
                text_row = row.get_text().lower()
                if CONFIG["TEAM_NAME"] not in text_row: continue
                
                # Extraer Canales
                channels_ul = row.select_one("ul.listaCanales")
                if not channels_ul: continue
                
                raw_channels = []
                for li in channels_ul.find_all("li"):
                    t = li.get_text(strip=True)
                    # Filtros de exclusión
                    if "hellotickets" in t.lower(): continue
                    if "laliga tv bar" in t.lower(): continue
                    if "apostar" in t.lower(): continue
                    if "confirmar" in t.lower(): continue
                    if t: raw_channels.append(t)
                
                if not raw_channels: continue
                
                # Lógica de Prioridad y Formateo
                # 1. Gratis, 2. DAZN, 3. M+, 4. Otros
                
                free_keywords = ["teledeporte", "rtve", "la 1", "tvg", "youtube", "gol", "cuatro", "telecinco"]
                dazn_keywords = ["dazn"]
                movistar_keywords = ["movistar", "m+", "campeones", "vamos"]
                
                sorted_channels = []
                
                # Buckets
                free = [c for c in raw_channels if any(k in c.lower() for k in free_keywords)]
                dazn = [c for c in raw_channels if any(k in c.lower() for k in dazn_keywords) and c not in free]
                movistar = [c for c in raw_channels if any(k in c.lower() for k in movistar_keywords) and c not in free and c not in dazn]
                others = [c for c in raw_channels if c not in free and c not in dazn and c not in movistar]
                
                final_list = free + dazn + movistar + others
                if not final_list: continue

                # Construir Short Name (para el título)
                top_channel = final_list[0]
                short_name = "TV"
                if any(k in top_channel.lower() for k in ["teledeporte", "tdp"]): short_name = "TDP"
                elif any(k in top_channel.lower() for k in ["rtve", "la 1", "tve"]): short_name = "TVE"
                elif any(k in top_channel.lower() for k in ["tvg"]): short_name = "TVG"
                elif "dazn" in top_channel.lower(): short_name = "DAZN"
                elif any(k in top_channel.lower() for k in ["movistar", "m+"]): short_name = "M+"
                elif "gol" in top_channel.lower(): short_name = "Gol"
                
                full_desc = ", ".join(final_list)
                
                tv_data[current_date_str] = {
                    "short": short_name,
                    "long": full_desc
                }
                
    except Exception as e:
        logging.warning(f"⚠️ Error scraping TV Bulk: {e}")
    
    return tv_data

def update_next_stadium(driver, matches):
    """
    Busca el SIGUIENTE partido (fecha futura más cercana) y actualiza su estadio en DB.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    next_match = None
    
    # Encontrar el partido futuro más cercano
    sorted_matches = sorted(matches, key=lambda x: x['inicio'])
    for m in sorted_matches:
        if m['inicio'] > now and not m['is_tbd']: # Evitar TBDs para estadios si es posible
            next_match = m
            break
    
    if not next_match: return
    
    logging.info(f"🏟️ Verificando estadio para próximo partido: {next_match['local']} vs {next_match['visitante']}")
    
    try:
        driver.get(next_match['link'])
        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        # Lógica Besoccer para estadio
        stadium = None
        box_rows = soup.select('.table-body.p10 .table-row-round') or soup.select('.table-row-round')
        
        for row in box_rows:
            txt = row.get_text(strip=True)
            if "estadio" in txt.lower():
                link = row.select_one('a.popup_btn[href="#stadium"]')
                if link: stadium = clean_text(link.text)
                else: stadium = clean_text(txt)
                break
        
        if stadium:
            # Normalizar para evitar falsas actualizaciones
            if "estadio" in stadium.lower() and ":" in stadium:
                stadium = stadium.split(":")[-1].strip()
            
            update_db_entry(next_match['local'], stadium)
            
    except Exception as e:
        logging.warning(f"⚠️ Error actualizando estadio: {e}")

# --- GOOGLE CALENDAR & SYNC ---

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
    try: requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", json={'chat_id': CONFIG['TELEGRAM_CHAT_ID'], 'text': msg, 'parse_mode': 'HTML'})
    except: pass

def run_sync():
    # 0. Setup
    load_stadium_db()
    driver = setup_driver()
    
    try:
        # 1. Fetch Data
        matches = fetch_matches_besoccer(driver)
        if not matches: return
        
        tv_data = fetch_tv_data_bulk(driver) # Scraping único de TV
        
        # 2. Update Next Stadium (Optimization)
        update_next_stadium(driver, matches)
        save_stadium_db()
        
        driver.quit() # Ya no necesitamos browser
        
        # 3. Calendar Sync
        logging.info("☁️ Sincronizando Google Calendar...")
        service = get_calendar_service()
        
        # Obtener eventos existentes
        existing = {}
        page_token = None
        while True:
            events = service.events().list(calendarId=CONFIG["CALENDAR_ID"], singleEvents=True, pageToken=page_token).execute()
            for ev in events.get('items', []):
                eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
                if eid: existing[eid] = ev
            page_token = events.get('nextPageToken')
            if not page_token: break

        msgs_telegram = []
        
        for m in matches:
            mid = m['id']
            
            # Enrich with TV
            tv_info = tv_data.get(m['date_key'])
            
            # Enrich with Stadium (from DB)
            stadium, location = find_stadium_in_db(m['local'])
            if not stadium: 
                if CONFIG["TEAM_NAME"] in m['local'].lower(): location = f"Estadio Balaídos, Vigo"
                else: location = f"Estadio {m['local']}"
            
            # Build Title/Desc
            icon = "🏆"
            if "liga" in m['competicion'].lower(): icon = "⚽"
            elif "copa" in m['competicion'].lower(): icon = "🏆"
            elif "amistoso" in m['competicion'].lower(): icon = "🤝"
            
            title_suffix = f" | {icon} {m['competicion']}"
            if tv_info: title_suffix += f" | 📺 {tv_info['short']}"
            
            base_title = f"{m['local']} vs {m['visitante']}"
            if m['score']: base_title = f"{m['local']} {m['score']} {m['visitante']}"
            
            full_title = f"{base_title}{title_suffix}"
            if m['is_tbd']: full_title = f"(TBC) {full_title}"
            
            desc = f"{icon} {m['competicion']}\n"
            if tv_info: desc += f"📺 Dónde ver: {tv_info['long']}\n"
            desc += f"📍 Lugar: {location}\n"
            desc += f"🔗 Info: {m['link']}"

            # Event Body
            start_str = m['inicio'].isoformat().replace("+00:00", "Z")
            end_dt = m['inicio'] + datetime.timedelta(hours=2)
            end_str = end_dt.isoformat().replace("+00:00", "Z")

            body = {
                'summary': full_title,
                'location': location,
                'description': desc,
                'start': {'dateTime': start_str, 'timeZone': 'UTC'},
                'end': {'dateTime': end_str, 'timeZone': 'UTC'},
                'extendedProperties': {'shared': {'match_id': mid}},
                'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]}
            }
            
            # Insert/Update Logic
            if mid in existing:
                ev = existing[mid]
                # Check changes
                old_desc = clean_text(ev.get('description', ''))
                new_desc = clean_text(desc)
                old_title = clean_text(ev.get('summary', ''))
                new_title = clean_text(full_title)
                
                # Check Time Change (>60s)
                old_time = ev['start'].get('dateTime')
                time_changed = False
                if old_time:
                    dt_old = datetime.datetime.fromisoformat(old_time.replace('Z', '+00:00'))
                    if abs((dt_old - m['inicio']).total_seconds()) > 60: time_changed = True
                
                if old_desc != new_desc or old_title != new_title or time_changed:
                    service.events().update(calendarId=CONFIG["CALENDAR_ID"], eventId=ev['id'], body=body).execute()
                    logging.info(f"🔄 Update: {full_title}")
                    if time_changed or "TBC" in old_title and "TBC" not in new_title:
                        msgs_telegram.append(f"🔄 <b>Cambio:</b> {full_title}")
            else:
                service.events().insert(calendarId=CONFIG["CALENDAR_ID"], body=body).execute()
                logging.info(f"✅ Nuevo: {full_title}")
                msgs_telegram.append(f"✅ <b>Nuevo:</b> {full_title}")
        
        if msgs_telegram:
            send_telegram("<b>📅 Celta Calendar Update</b>\n\n" + "\n".join(msgs_telegram))
            
    except Exception as e:
        logging.error(f"❌ Error Fatal: {e}")
        traceback.print_exc()
    finally:
        try: driver.quit()
        except: pass

if __name__ == '__main__':
    run_sync()