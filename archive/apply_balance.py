import json
import logging
import datetime
import os
import argparse
from main_scraper import get_calendar_service, CONFIG, parse_google_iso, execute_with_retry
from googleapiclient.discovery import build
import re
from curl_cffi import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(message)s')

def get_season(dt):
    year = dt.year
    month = dt.month
    if month >= 7:
        return f"{year}/{year+1}"
    else:
        return f"{year-1}/{year}"

def scrape_live_classification(match_url):
    class_url = match_url.rstrip('/') + '/clasificacion'
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(class_url, headers=headers, impersonate="chrome110")
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        
        tables = soup.find_all('table')
        celta_pos = None
        celta_color = None
        for t in tables:
            for row in t.find_all('tr'):
                tds = row.find_all(['td', 'th'])
                row_text = " ".join([td.text.strip() for td in tds])
                if 'celta' in row_text.lower():
                    if tds:
                        num_td = tds[0]
                        celta_pos = num_td.text.strip() + "º"
                        div = num_td.find('div')
                        if div and div.get('data-color'):
                            celta_color = div.get('data-color').upper()
                    break
            if celta_pos: break
            
        if not celta_pos: return None
        
        leyenda_text = ""
        for leg in soup.find_all(class_='legend-item'):
            box = leg.find(class_='box')
            if box and box.get('data-color') and celta_color:
                if box.get('data-color').upper() == celta_color:
                    text_span = leg.find('span')
                    if text_span:
                        leyenda_text = f" ({text_span.text.strip()})"
                        break
                        
        # Detectar división por el texto del título en the event (lo manejaremos arriba)
        return celta_pos + leyenda_text
    except Exception as e:
        logging.error(f"Error scraping live class: {e}")
        return None

def extract_besoccer_url(desc):
    match = re.search(r'(https://es\.besoccer\.com/partido/[^\s\n<"]+)', desc)
    if match: return match.group(1)
    return None

def format_liga_balance(liga_balance, season):
    # season is like "2024/2025"
    parts = season.split('/')
    next_season = f"{int(parts[0])+1}/{int(parts[1])+1}"
    
    lower_bal = liga_balance.lower()
    
    if '(ascenso' in lower_bal:
        return re.sub(r'\(.*?\)', '(🎉 ¡SOMOS DE PRIMERA DIVISIÓN! 🎉)', liga_balance)
    elif '(descenso' in lower_bal:
        return re.sub(r'\(.*?\)', '(🫧 ¡somos de segunda división...! 🫧)', liga_balance)
    elif 'uefa' in lower_bal or 'europa league' in lower_bal:
        return re.sub(r'\(.*?\)', f'(🎆 CLASIFICADOS A LA EUROPA LEAGUE {next_season} 🎆)', liga_balance)
    elif 'champions' in lower_bal:
        return re.sub(r'\(.*?\)', f'(🎆 CLASIFICADOS A LA CHAMPIONS LEAGUE {next_season} 🎆)', liga_balance)
        
    return liga_balance

def main(dry_run=False):
    # Cargar Base de Datos
    try:
        with open("balances_db.json", "r", encoding="utf-8") as f:
            balances_db = json.load(f)
    except Exception as e:
        logging.error(f"Error cargando balances_db.json: {e}")
        return

    service = get_calendar_service()
    if not service:
        logging.error("No se pudo conectar a Google Calendar")
        return

    logging.info("Descargando eventos del calendario...")
    events_by_season = {}
    page_token = None
    total_events = 0

    while True:
        events_result = service.events().list(
            calendarId=CONFIG["CALENDAR_ID"], singleEvents=True, showDeleted=False, pageToken=page_token
        ).execute()
        
        for ev in events_result.get('items', []):
            if ev.get('status') != 'cancelled' and ev.get('extendedProperties', {}).get('shared', {}).get('match_id'):
                start_str = ev.get('start', {}).get('dateTime') or ev.get('start', {}).get('date')
                if not start_str: continue
                
                dt = parse_google_iso(start_str)
                if not dt: continue
                
                title = ev.get('summary', '').lower()
                # Excluir pretemporada y amistosos del cálculo del último partido
                if 'pretemporada' in title or 'amistoso' in title:
                    continue
                
                season = get_season(dt)
                if season not in events_by_season:
                    events_by_season[season] = []
                
                events_by_season[season].append({
                    'event': ev,
                    'dt': dt
                })
                total_events += 1

        page_token = events_result.get('nextPageToken')
        if not page_token:
            break

    logging.info(f"✅ Se han procesado {total_events} partidos oficiales agrupados en {len(events_by_season)} temporadas.")

    updates = 0
    for season, ev_list in events_by_season.items():
        # Encontrar el último partido oficial ordenando por fecha
        ev_list.sort(key=lambda x: x['dt'])
        last_match = ev_list[-1]
        ev = last_match['event']
        
        # Identificar el último partido EXCLUSIVAMENTE DE LIGA
        league_matches = []
        for e in ev_list:
            e_title = e['event'].get('summary', '').lower()
            e_desc = e['event'].get('description', '').lower()
            if ('⚽ liga' in e_desc or 'segunda división' in e_desc or 'primera división' in e_desc) and 'play-off' not in e_title and 'promoción' not in e_title:
                league_matches.append(e)
                
        if league_matches:
            league_matches.sort(key=lambda x: x['dt'])
            last_league = league_matches[-1]
            ev_league = last_league['event']
            
            l_desc = ev_league.get('description', '')
            if "💀 ÚLTIMO PARTIDO DE LIGA 💀" not in l_desc:
                lines = l_desc.split('\n')
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if 'jornada' in line.lower() and '▶️' in line:
                        new_lines.append("💀 ÚLTIMO PARTIDO DE LIGA 💀")
                
                new_l_desc = '\n'.join(new_lines)
                if new_l_desc == l_desc:
                    if "📊 BALANCE" in new_l_desc:
                        parts = new_l_desc.split("---", 1)
                        new_l_desc = parts[0] + "\n💀 ÚLTIMO PARTIDO DE LIGA 💀\n\n---" + parts[1]
                    else:
                        new_l_desc += "\n\n💀 ÚLTIMO PARTIDO DE LIGA 💀"
                        
                ev_league['description'] = new_l_desc
                logging.info(f" -> Marcado como ÚLTIMO DE LIGA: {ev_league.get('summary')}")
                
                if not dry_run:
                    try:
                        execute_with_retry(service.events().update(
                            calendarId=CONFIG["CALENDAR_ID"],
                            eventId=ev_league['id'],
                            body=ev_league
                        ))
                    except Exception as e:
                        logging.error(f"Error actualizando evento de liga: {e}")
        else:
            ev_league = None

        db_season = f"{season.split('/')[0]}-{season.split('/')[1][-2:]}"

        if db_season in balances_db:
            balance_data = balances_db[db_season]
            liga_balance = balance_data['liga']
            copa_balance = balance_data['copa']
            europa_balance = balance_data['europa']
        else:
            # Intento de scrape en vivo (solo para nuevas temporadas no en DB)
            # Y SOLO lo hacemos para la actual (o una en la que estemos corriendo el bot sin json)
            if not ev_league:
                continue
                
            logging.info(f"Temporada {season} no en DB. Scrapeando en vivo desde Besoccer...")
            match_url = extract_besoccer_url(ev_league.get('description', ''))
            if not match_url:
                logging.info(f" -> No se encontró URL en el evento: {ev_league.get('summary')}")
                continue
                
            scraped_pos = scrape_live_classification(match_url)
            if not scraped_pos:
                logging.info(" -> No se pudo extraer la clasificación en vivo.")
                continue
                
            division = "1ª División" if 'primera' in ev_league.get('description', '').lower() or '⚽ liga' in ev_league.get('description', '').lower() else "2ª División"
            liga_balance = f"{division} - {scraped_pos}"
            copa_balance = "Consultar" # Omitido en scrape en vivo automático
            europa_balance = "Consultar"

        liga_balance = format_liga_balance(liga_balance, season)

        balance_text = f"\n---\n📊 BALANCE FINAL DE TEMPORADA {season}:\n"
        balance_text += f"⚽ Liga: {liga_balance}\n"
        
        if copa_balance.lower() not in ["no clasificado", "consultar", ""]:
            balance_text += f"🏆 Copa del Rey: {copa_balance}\n"
        if europa_balance.lower() not in ["no clasificado", "consultar", ""]:
            balance_text += f"🌍 Europa: {europa_balance}\n"

        desc = ev.get('description', '')
        if "📊 BALANCE FINAL DE TEMPORADA" in desc:
            # Si ya tiene un balance, revisar si es igual
            if balance_text.strip() not in desc:
                # Si es distinto, reemplazar la parte del balance
                parts = desc.split("📊 BALANCE FINAL DE TEMPORADA")
                desc = parts[0].rsplit("---", 1)[0].strip() # Remover el --- previo
                desc += "\n" + balance_text
                
                if not dry_run:
                    ev['description'] = desc
                    try:
                        execute_with_retry(service.events().update(
                            calendarId=CONFIG["CALENDAR_ID"],
                            eventId=ev['id'],
                            body=ev
                        ))
                        updates += 1
                    except Exception as e:
                        logging.error(f"Error actualizando evento final: {e}")
                else:
                    updates += 1
        else:
            desc += balance_text
            if not dry_run:
                ev['description'] = desc
                try:
                    execute_with_retry(service.events().update(
                        calendarId=CONFIG["CALENDAR_ID"],
                        eventId=ev['id'],
                        body=ev
                    ))
                    updates += 1
                except Exception as e:
                    logging.error(f"Error actualizando evento final: {e}")
            else:
                updates += 1

        logging.info(f"\n[Temporada {season}] Último partido: {ev.get('summary')}")
        logging.info(f" -> Añadiendo balance: {liga_balance}")

    mode = "DRY-RUN" if dry_run else "REAL"
    logging.info(f"\n✅ Proceso completado en modo {mode}. {updates} temporadas actualizadas.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', help='Ejecutar modificaciones reales')
    args = parser.parse_args()
    main(dry_run=not args.run)
