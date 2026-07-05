import json
import logging
import datetime
import os
import argparse
from main_scraper import get_calendar_service, CONFIG, parse_google_iso, execute_with_retry, format_liga_balance
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
        
        # Limpiar tags erróneos previos en toda la temporada
        for e in ev_list:
            e_desc = e['event'].get('description', '')
            if "💀 ÚLTIMO PARTIDO DE LIGA 💀" in e_desc:
                # Quitamos la etiqueta para empezar de cero
                clean_desc = e_desc.replace("💀 ÚLTIMO PARTIDO DE LIGA 💀", "").strip()
                # También quitamos cualquier línea en blanco extra que haya quedado
                clean_desc = '\n'.join([line for line in clean_desc.split('\n') if line.strip() != ''])
                e['event']['description'] = clean_desc
                if not dry_run:
                    try:
                        execute_with_retry(service.events().update(
                            calendarId=CONFIG["CALENDAR_ID"],
                            eventId=e['event']['id'],
                            body=e['event']
                        ))
                    except Exception as exc:
                        pass
                        
        # Identificar el último partido EXCLUSIVAMENTE DE LIGA
        league_matches = []
        for e in ev_list:
            e_title = e['event'].get('summary', '').lower()
            # Coger solo la primera línea de la descripción para evitar que el balance antiguo confunda al bot
            e_desc_first_line = e['event'].get('description', '').split('\n')[0].lower()
            if ('⚽ liga' in e_desc_first_line or 'segunda división' in e_desc_first_line or 'primera división' in e_desc_first_line or 'división b' in e_desc_first_line or 'tercera' in e_desc_first_line) and 'play-off' not in e_title and 'promoción' not in e_title:
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
            
            # Deducir si se clasificaron a Europa basándonos en la temporada siguiente
            try:
                y1, y2 = season.split('/')
                y1_int, y2_int = int(y1), int(y2)
                next_db_season = f"{y1_int+1}-{str(y2_int+1)[-2:].zfill(2)}"
                if next_db_season in balances_db:
                    next_euro = balances_db[next_db_season]['europa'].lower()
                    if next_euro not in ["consultar", "-", "no clasificado", ""]:
                        if 'champions' in next_euro:
                            liga_balance += " (champions)"
                        elif 'uefa' in next_euro or 'europa league' in next_euro:
                            liga_balance += " (uefa)"
                        elif 'conference' in next_euro:
                            liga_balance += " (conference)"
            except Exception as e:
                pass

        else:
            # Intento de extraer información a partir de los eventos del calendario para el pasado
            if not ev_league:
                continue
                
            logging.info(f"Temporada {season} no en DB. Deduciendo desde Google Calendar...")
            
            # Extraer división del evento de liga
            division = "1ª División"
            import re
            m_desc = ev_league.get('description', '')
            match = re.search(r'🚨 (.*?) \|', m_desc)
            if match:
                c_name = match.group(1).lower().strip()
                if 'segunda' in c_name:
                    if 'b' in c_name: division = "2ª División B"
                    else: division = "2ª División"
                elif 'tercera' in c_name: division = "3ª División"
                elif 'primera federación' in c_name or 'primera rfe' in c_name: division = "1ª Federación"
            else:
                if 'primera' in m_desc.lower() or '⚽ liga' in m_desc.lower():
                    division = "1ª División"
                elif 'segunda' in m_desc.lower():
                    division = "2ª División"
            
            scraped_pos = "1º"
            match_url = extract_besoccer_url(m_desc)
            if match_url:
                try:
                    pos = scrape_live_classification(match_url)
                    if pos: scraped_pos = pos
                except: pass
                
            liga_balance = f"{division} - {scraped_pos}"
            copa_balance = "No clasificado"
            europa_balance = "No clasificado"
            
            # Extraer rondas de otras competiciones buscando en los eventos de la temporada
            for e_dict in ev_list:
                e_sum = e_dict['event'].get('summary', '')
                if 'Copa del Rey' in e_sum:
                    parts = e_sum.split('|')
                    if len(parts) >= 3:
                        copa_balance = parts[2].replace('Ronda', '').strip()
                elif 'Europa League' in e_sum or 'Champions' in e_sum or 'UEFA' in e_sum:
                    parts = e_sum.split('|')
                    if len(parts) >= 3:
                        europa_balance = parts[2].replace('Ronda', '').strip()

        # Asegurar que el string termine en " de final" si tiene fracciones
        if '/' in copa_balance and 'final' not in copa_balance.lower():
            copa_balance = copa_balance + " de final"

        # liga_balance format (this might add emojis)
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
