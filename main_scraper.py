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
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv 

# --- IMPORTACIONES EXTRA PARA MANEJO DE ERRORES SELENIUM ---
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchWindowException

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
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# --- CARGA DE VARIABLES ---
load_dotenv() 

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

SELECTORS = {
    "MATCH_LINK": "a.match-link",
    "TEAM_LOCAL": ".team-name.team_left .name",
    "TEAM_VISIT": ".team-name.team_right .name",
    "COMPETITION": ".middle-info",
    "SCORE_R1": ".marker .r1",
    "SCORE_R2": ".marker .r2",
    "STATUS": ".match-status-label .tag",
    "COOKIE_BTN": "didomi-notice-agree-button"
}

STADIUM_DB = {}
DB_DIRTY = False

logging.basicConfig(level=logging.INFO, format='%(message)s')

# --- 1. DB & UTILS ---

def load_stadium_db():
    global STADIUM_DB
    if os.path.exists(CONFIG["DB_FILE"]):
        try:
            with open(CONFIG["DB_FILE"], 'r', encoding='utf-8') as f:
                STADIUM_DB = json.load(f)
            logging.info(f"💾 Base de datos de estadios cargada ({len(STADIUM_DB)} registros).")
        except Exception as e:
            logging.error(f"⚠️ Error cargando DB: {e}")
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

def normalize_team_key(name):
    if not name: return ""
    text = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower()
    remove_list = [" fc", " cf", " ud", " cd", " sd", "real ", "club ", "deportivo ", "atletico "]
    for item in remove_list:
        text = text.replace(item, " ")
    return " ".join(text.split()).strip()

def find_stadium_dynamic(team_name):
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
    
    for key, data in STADIUM_DB.items():
        if team_name in data.get('aliases', []):
            return data.get('stadium'), data.get('location')
    return None, None

def update_db(team_name, stadium, location):
    global STADIUM_DB, DB_DIRTY
    invalid_terms = ["campo municipal", "estadio local", "campo de futbol", "municipal"]
    if any(term in stadium.lower() for term in invalid_terms) and len(stadium) < 15: return
    
    if team_name in STADIUM_DB:
        old_stadium = STADIUM_DB[team_name].get('stadium', 'Desconocido')
        if old_stadium != stadium:
            logging.info(f"🏟️ Actualizando DB: {team_name} -> {stadium}")
    else:
        logging.info(f"🆕 Añadiendo a DB: {team_name} -> {stadium}")
        
    STADIUM_DB[team_name] = {
        "stadium": stadium,
        "location": location,
        "aliases": [],
        "last_updated": datetime.datetime.now().strftime("%Y-%m-%d")
    }
    DB_DIRTY = True

def normalize_text(text):
    """Normalización agresiva para comparación de strings."""
    if not text: return ""
    text = html.unescape(text)
    # Eliminar tags HTML
    text = re.sub('<[^<]+?>', '', text)
    # Reemplazar múltiples espacios/saltos por un solo espacio
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_competition_details(comp_text):
    text = comp_text.lower()
    if 'promoción' in text: return 'Promoción', '🏆', '3'
    if 'champions' in text: return 'Champions League', '✨', '5'
    if 'segunda' in text: return 'Segunda División', '2️⃣', '7'
    if 'liga' in text or 'primera' in text: return 'LaLiga', '⚽', '7'
    if 'copa' in text: return 'Copa del Rey', '🏆', '3'
    if 'europa' in text: return 'Europa League', '🌍', '6'
    if 'conference' in text: return 'Conference League', '🇪🇺', '6'
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
    if 'semi' in text: return "Semis"
    if 'cuartos' in text: return "Cuartos"
    if 'octavos' in text: return "Octavos"
    if 'final' in text: return "Final"
    return ""

def get_short_tv_name(full_tv_name):
    if not full_tv_name: return ""
    upper_name = full_tv_name.upper()
    if "M+" in upper_name or "MOVISTAR" in upper_name: return "M+"
    if "DAZN" in upper_name: return "DAZN"
    if "GOL" in upper_name: return "Gol"
    if "TVG" in upper_name: return "TVG"
    if "TVE" in upper_name or "LA 1" in upper_name: return "TVE"
    if len(full_tv_name) <= 6: return full_tv_name
    return "TV"

# --- 2. TV & SCRAPING ---

def fetch_tv_schedule(team_name_filter):
    tv_schedule = {}
    url = "https://www.futbolenlatv.es/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        logging.info("📺 Consultando cartelera TV (Fuente Primaria)...")
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
                # Estrategia 1: Clase listaCanales
                found_channels = match_container.select('.listaCanales a')
                if found_channels:
                    candidates = [c.get_text(strip=True) for c in found_channels]
                    dazn = next((c for c in candidates if "DAZN" in c.upper()), None)
                    channel_text = dazn if dazn else candidates[0]
                else:
                    # Estrategia 2: Texto raw
                    text_parts = list(match_container.stripped_strings)
                    candidates = [t for t in text_parts if len(t) > 2 and ":" not in t and team_name_filter.lower() not in t.lower()]
                    if candidates: channel_text = candidates[-1]

                if "(" in channel_text: channel_text = channel_text.split("(")[0].strip()

                if channel_text:
                    date_header = match_container.find_previous(['div', 'h2', 'h3'], class_=re.compile(r'date|dia|header'))
                    if date_header:
                        header_text = date_header.get_text()
                        date_match = re.search(r'(\d{1,2})/(\d{1,2})', header_text)
                        if date_match:
                            day, month = date_match.groups()
                            # Calcular año aproximado
                            year = now_ref.year
                            if now_ref.month >= 10 and int(month) <= 3: year += 1
                            match_date_str = f"{year}-{int(month):02d}-{int(day):02d}"
                            tv_schedule[match_date_str] = channel_text
            except: continue
        return tv_schedule
    except Exception as e:
        logging.warning(f"⚠️ Error TV scraper: {e}")
        return {}

def setup_driver():
    """Driver robusto con CDP Anti-USA."""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("--window-size=1280,720") 
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-pipe") # CRÍTICO PARA CI
    chrome_options.add_argument("--disable-search-engine-choice-screen")
    chrome_options.add_argument("--lang=es-ES") 
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    # Timings agresivos para fail-fast
    driver.set_page_load_timeout(15)
    driver.set_script_timeout(15)

    try:
        # Inyectar Geolocation Madrid
        driver.execute_cdp_cmd('Emulation.setGeolocationOverride', {
            'latitude': 40.4168, 'longitude': -3.7038, 'accuracy': 100
        })
        driver.execute_cdp_cmd('Emulation.setTimezoneOverride', {'timezoneId': 'Europe/Madrid'})
    except: pass

    return driver

def scrape_besoccer_info(driver, match_link):
    if not match_link: return None, None
    stadium = None
    tv_text = None

    try:
        driver.get(match_link)
        # Espera breve para carga dinámica
        WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, '.table-row-round')))
        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        box_rows = soup.select('.table-body.p10 .table-row-round')
        rows = box_rows if box_rows else soup.select('.table-row-round')
        
        for row in rows:
            text = normalize_text(row.get_text())
            
            # ESTADIO
            if "estadio" in text.lower() and not stadium:
                stadium_link = row.select_one('a[href*="#stadium"]')
                stadium = stadium_link.text.strip() if stadium_link else text

            # TV (Prioridad a iconos)
            is_tv = False
            if row.select('svg[aria-label="TV"]') or "televisión" in text.lower(): is_tv = True
            
            keywords = ["MOVISTAR", "DAZN", "LA 1", "TVG", "GOL", "TELECINCO"]
            if any(k in text.upper() for k in keywords): is_tv = True

            if is_tv and "estadio" not in text.lower():
                raw_tv = text.replace('TV', '').replace('Televisión', '')
                clean_tv = raw_tv.replace('(Esp)', '').replace('|', '/').strip()
                # Filtro Anti-USA agresivo
                if not any(x in clean_tv.upper() for x in ["USA", "ESPN", "PARAMOUNT", "FUBO"]):
                    tv_text = clean_tv

    except Exception:
        # No relanzamos la excepción aquí, dejamos que el llamador decida si es crítico
        raise 
    
    return stadium, tv_text

# --- 3. CORE LOGIC ---

def fetch_matches():
    """Fase A: Escaneo rápido."""
    logging.info("🚀 [Fase A] Obteniendo lista de partidos...")
    driver = setup_driver()
    matches = []
    try:
        url = f"{CONFIG['URL_BASE']}{CONFIG['TEAM_NAME']}"
        driver.get(url)
        
        try: 
            WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, SELECTORS["COOKIE_BTN"]))).click()
        except: pass

        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELECTORS["MATCH_LINK"])))
        soup = BeautifulSoup(driver.page_source, 'lxml')
        
        elements = soup.select(SELECTORS["MATCH_LINK"])
        for m in elements:
            try:
                start_iso = m.get('starttime')
                if not start_iso: continue
                # Parseo UTC directo
                dt_obj = datetime.datetime.fromisoformat(start_iso).astimezone(datetime.timezone.utc)
                
                has_time = m.get('hastime', "1") == "1"
                is_tbd = not has_time
                if dt_obj.hour == 0 and dt_obj.minute == 0: is_tbd = True

                local = m.select_one(SELECTORS["TEAM_LOCAL"]).text.strip()
                visit = m.select_one(SELECTORS["TEAM_VISIT"]).text.strip()
                comp = m.select_one(SELECTORS["COMPETITION"]).text.strip() if m.select_one(SELECTORS["COMPETITION"]) else "Amistoso"
                
                mid = f"{dt_obj.strftime('%Y%m%d')}_{local[:3]}_{visit[:3]}".lower().replace(" ", "")
                status = m.select_one(SELECTORS["STATUS"]).text.strip().lower() if m.select_one(SELECTORS["STATUS"]) else ""
                
                score = None
                r1 = m.select_one(SELECTORS["SCORE_R1"])
                r2 = m.select_one(SELECTORS["SCORE_R2"])
                if r1 and r2: score = f"{r1.text.strip()}-{r2.text.strip()}"

                lugar = f"Estadio Local ({local})" if CONFIG["TEAM_NAME"] in local.lower() else f"Estadio Visitante ({local})"
                
                # Temporada
                match_year = dt_obj.year
                season = f"{match_year}-{match_year+1}" if dt_obj.month >= 7 else f"{match_year-1}-{match_year}"

                matches.append({
                    'id': mid, 'local': local, 'visitante': visit,
                    'competicion': comp, 'inicio': dt_obj,
                    'is_tbd': is_tbd, 'lugar': lugar,
                    'score': score, 'status': status,
                    'link': m.get('href'), 'season': season
                })
            except: continue
    finally:
        driver.quit()
    
    logging.info(f"✅ [Fase A] Total encontrados: {len(matches)}")
    return matches

def is_event_different(old_ev, new_data):
    """
    Lógica de comparación estricta para evitar falsos positivos.
    Retorna: (needs_update, notify_telegram, changes_list)
    """
    needs_update = False
    notify = False
    changes = []

    # 1. Comparación de Tiempo (Permitir margen de 60s)
    old_start = old_ev['start'].get('dateTime')
    if old_start:
        old_dt = datetime.datetime.fromisoformat(old_start.replace('Z', '+00:00'))
        new_dt = new_data['start']['dateTime']
        if isinstance(new_dt, str): new_dt = datetime.datetime.fromisoformat(new_dt)
        
        diff = abs((old_dt - new_dt).total_seconds())
        if diff > 60:
            needs_update = True
            notify = True
            changes.append(f"⏰ Hora: {old_dt.strftime('%H:%M')} -> {new_dt.strftime('%H:%M')}")

    # 2. Comparación de Título (Normalizada)
    old_summary = normalize_text(old_ev.get('summary', ''))
    new_summary = normalize_text(new_data['summary'])
    
    if old_summary != new_summary:
        # Analizar severidad del cambio
        base_old = old_summary.split('|')[0].strip()
        base_new = new_summary.split('|')[0].strip()
        
        # Si cambia el partido base (Rival), es crítico
        if base_old != base_new:
            needs_update = True
            notify = True
            changes.append(f"🆚 Rival/Info: {base_old} -> {base_new}")
        else:
            # Cambio menor (TV, Icono, Ronda) -> Update silencioso
            needs_update = True

    # 3. Comparación de Descripción y Ubicación (Silencioso)
    if not needs_update:
        old_desc = normalize_text(old_ev.get('description', ''))
        new_desc = normalize_text(new_data['description'])
        old_loc = normalize_text(old_ev.get('location', ''))
        new_loc = normalize_text(new_data['location'])

        if old_desc != new_desc or old_loc != new_loc:
            needs_update = True

    return needs_update, notify, changes

def run_sync():
    # Setup Auth
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if creds_json and not os.path.exists(CONFIG["CREDENTIALS_FILE"]):
        with open(CONFIG["CREDENTIALS_FILE"], "w") as f: f.write(creds_json)
    
    token_b64 = os.getenv("GCP_TOKEN_JSON_B64")
    if token_b64 and not os.path.exists(CONFIG["TOKEN_FILE"]):
        with open(CONFIG["TOKEN_FILE"], "w") as f: f.write(base64.b64decode(token_b64).decode())

    load_stadium_db()
    
    # 1. Obtener datos
    tv_schedule = fetch_tv_schedule(CONFIG["TEAM_NAME"])
    matches = fetch_matches()
    if not matches: return

    # 2. Setup Google
    creds = Credentials.from_authorized_user_file(CONFIG["TOKEN_FILE"], CONFIG["SCOPES"])
    service = build('calendar', 'v3', credentials=creds)
    
    # Cache eventos existentes
    existing = {}
    page_token = None
    while True:
        res = service.events().list(calendarId=CONFIG["CALENDAR_ID"], singleEvents=True, pageToken=page_token).execute()
        for ev in res.get('items', []):
            eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
            if eid: existing[eid] = ev
        page_token = res.get('nextPageToken')
        if not page_token: break

    telegram_msgs = []
    
    # 3. PROCESAMIENTO CON SMART RETRY
    driver = None
    
    for i, match in enumerate(matches):
        mid = match['id']
        logging.info(f"⚽ Procesando {i+1}/{len(matches)}: {match['local']} vs {match['visitante']}")

        # Datos Base
        comp_name, icon, color = get_competition_details(match['competicion'])
        round_tag = get_round_details(match['competicion'])
        
        # --- LÓGICA DE ENRIQUECIMIENTO (TV/ESTADIO) ---
        stadium_info = None
        tv_info = None
        
        # 1. Buscar en DB local primero
        stadium_db, location_db = find_stadium_dynamic(match['local'])
        
        # 2. Definir si necesitamos Scraping (Solo si es futuro y no tenemos datos completos)
        need_scrape = True
        if 'fin' in match['status']: need_scrape = False
        
        # Si ya tenemos TV de la fuente externa fiable, reducimos necesidad de scraping
        date_key = match['inicio'].strftime("%Y-%m-%d")
        external_tv = tv_schedule.get(date_key)
        
        web_stadium = None
        web_tv = None

        if need_scrape:
            # --- SMART RETRY LOOP ---
            retries = 0
            max_retries = 2
            success = False
            
            while retries <= max_retries and not success:
                try:
                    if not driver: driver = setup_driver()
                    web_stadium, web_tv = scrape_besoccer_info(driver, match['link'])
                    success = True # Scrape exitoso
                except (WebDriverException, TimeoutException, NoSuchWindowException) as e:
                    logging.warning(f"   ⚠️ Driver error (Intento {retries+1}): {e}")
                    try: driver.quit()
                    except: pass
                    driver = None # Forzar recreación
                    retries += 1
                    time.sleep(2)
                except Exception as e:
                    logging.error(f"   ❌ Error no recuperable: {e}")
                    break # Salir del retry loop
            
            # Si fallaron todos los reintentos, seguimos con lo que tengamos (Graceful Degradation)

        # 3. Consolidación de Datos
        # Estadio
        if web_stadium: 
            stadium_info = web_stadium
            location_info = f"{web_stadium}, {match['local']}"
            update_db(match['local'], stadium_info, location_info)
        else:
            stadium_info = stadium_db
            location_info = location_db if location_db else match['lugar']

        # TV (Prioridad: FutbolEnLaTV > BeSoccer > Existente)
        final_tv = external_tv if external_tv else web_tv
        if not final_tv and mid in existing:
            # Intentar rescatar TV del evento existente si no encontramos nueva
            desc = existing[mid].get('description', '')
            m_tv = re.search(r'📺 Dónde ver: (.*)', desc)
            if m_tv: final_tv = m_tv.group(1).strip()

        # Construcción del Evento
        tv_short = get_short_tv_name(final_tv)
        title_suffix = f" |{icon}{comp_name}"
        if round_tag: title_suffix += f" | {round_tag}"
        if tv_short: title_suffix += f" | {tv_short}"
        
        base_title = f"{match['local']} vs {match['visitante']}"
        if match['score'] and 'fin' in match['status']: 
            base_title = f"{match['local']} {match['score']} {match['visitante']}"
        
        full_title = f"{base_title}{title_suffix}"
        if match['is_tbd']: full_title = f"(TBC) {full_title}"

        desc_text = f"{icon} {comp_name} ({match['season']})\n"
        if round_tag: desc_text += f"▶️ {round_tag}\n"
        if final_tv: desc_text += f"📺 Dónde ver: {final_tv}\n"
        if stadium_info: desc_text += f"🏟️ Estadio: {stadium_info}\n"
        else: desc_text += f"📍 {match['lugar']}\n"
        desc_text += f"🔗 Info: {match['link']}"

        if match['is_tbd']: desc_text = "⚠️ Fecha/Hora por confirmar.\n" + desc_text

        event_body = {
            'summary': full_title,
            'location': location_info,
            'description': desc_text,
            'start': {'dateTime': match['inicio'].isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': (match['inicio'] + datetime.timedelta(hours=2)).isoformat(), 'timeZone': 'UTC'},
            'colorId': color,
            'extendedProperties': {'shared': {'match_id': mid}},
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]}
        }

        # 4. Sincronización Google
        if mid in existing:
            needs_upd, notify, changes = is_event_different(existing[mid], event_body)
            if needs_upd:
                try:
                    service.events().update(calendarId=CONFIG["CALENDAR_ID"], eventId=existing[mid]['id'], body=event_body).execute()
                    logging.info(f"   🔄 Actualizado: {base_title}")
                    if notify: telegram_msgs.append(f"🔄 <b>Cambio:</b> {base_title}\n" + "\n".join(changes))
                except Exception as e: logging.error(f"   ❌ Error Update Google: {e}")
        else:
            try:
                service.events().insert(calendarId=CONFIG["CALENDAR_ID"], body=event_body).execute()
                logging.info(f"   ✅ Nuevo evento: {base_title}")
                telegram_msgs.append(f"✅ <b>Nuevo:</b> {base_title}\n📅 {match['inicio'].strftime('%d/%m %H:%M')}")
            except Exception as e: logging.error(f"   ❌ Error Insert Google: {e}")

    save_stadium_db()
    if driver: driver.quit()

    # Enviar Notificaciones
    if telegram_msgs:
        full_msg = "<b>🔔 Celta Calendar Update</b>\n\n" + "\n\n".join(telegram_msgs)
        try:
            requests.post(f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage", 
                          json={'chat_id': CONFIG['TELEGRAM_CHAT_ID'], 'text': full_msg, 'parse_mode': 'HTML'})
        except: pass

if __name__ == '__main__':
    main_scraper_start = time.time()
    try:
        run_sync()
    except Exception as e:
        logging.error(f"🔥 Fatal Error: {e}")
        traceback.print_exc()
    logging.info(f"⏱️ Tiempo total: {time.time() - main_scraper_start:.1f}s")