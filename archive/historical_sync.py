import os
import sys
import time
import datetime
import logging
import argparse
from bs4 import BeautifulSoup
from curl_cffi import requests

# Evitamos que se ejecuten notificaciones importando módulos pero anulando telegram
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

import main_scraper

logging.basicConfig(level=logging.INFO, format='%(message)s')

# --- CONFIGURACIÓN HISTÓRICA ---
main_scraper.save_stadium_db = lambda: logging.info("💾 [MOCK] Guardado de DB de estadios desactivado para script histórico.")
main_scraper.send_telegram = lambda msg: logging.debug(f"[MOCK TELEGRAM]: {msg}")

def process_matches_from_html(html_source, explicit_season_text):
    matches = []
    soup = BeautifulSoup(html_source, 'lxml')
    match_elements = soup.select(main_scraper.SELECTORS["MATCH_LINK"])
    
    for m in match_elements:
        try:
            start_iso = m.get('starttime')
            has_time_attr = m.get('hastime', "1")
            match_link = m.get('href') 
            if not start_iso: continue
            start_utc = main_scraper.parse_besoccer_date(start_iso)
            if not start_utc: continue

            # Forzamos la temporada correcta que nos pasaron para evitar bugs de diciembre/enero
            season_text = explicit_season_text

            # Extraer hora/minuto evitando crash de astimezone() en Windows para pre-1970
            try:
                local_dt = start_utc.astimezone()
                hour = local_dt.hour
                minute = local_dt.minute
            except OSError:
                hour = start_utc.hour
                minute = start_utc.minute

            is_tbd = False
            if str(has_time_attr) == "1": is_tbd = True
            elif start_utc.minute == 0 and start_utc.hour in [22, 23, 0, 1]: is_tbd = True

            if is_tbd:
                # El partido no tiene hora. La API a menudo lo pone a las 22:00 o 23:00 UTC del día anterior.
                # Le sumamos 2 horas para caer con seguridad en el día real del partido.
                real_date = start_utc + datetime.timedelta(hours=2)
                time_str = "10:00:00" if real_date.year >= 2024 else "17:00:00"
                time_end_str = "12:00:00" if real_date.year >= 2024 else "19:00:00"
                start_dt_str = f"{real_date.strftime('%Y-%m-%d')}T{time_str}"
                end_dt_str = f"{real_date.strftime('%Y-%m-%d')}T{time_end_str}"
                start_api = {'dateTime': start_dt_str, 'timeZone': 'Europe/Madrid'}
                end_api = {'dateTime': end_dt_str, 'timeZone': 'Europe/Madrid'}
            else:
                start_api = {'dateTime': start_utc.isoformat(), 'timeZone': 'UTC'}
                end_api = {'dateTime': (start_utc + datetime.timedelta(hours=2)).isoformat(), 'timeZone': 'UTC'}

            local_elem = m.select_one(main_scraper.SELECTORS["TEAM_LOCAL"])
            visit_elem = m.select_one(main_scraper.SELECTORS["TEAM_VISIT"])
            if not local_elem or not visit_elem: continue
            local = main_scraper.clean_text(local_elem.text)
            visitante = main_scraper.clean_text(visit_elem.text)
            comp_elem = m.select_one(main_scraper.SELECTORS["COMPETITION"])
            comp_raw = main_scraper.clean_text(comp_elem.text) if comp_elem else "Amistoso"
            
            score_text = None
            r1 = m.select_one(main_scraper.SELECTORS["SCORE_R1"])
            r2 = m.select_one(main_scraper.SELECTORS["SCORE_R2"])
            if r1 and r2:
                t1 = main_scraper.clean_text(r1.text)
                t2 = main_scraper.clean_text(r2.text)
                if t1.isdigit() and t2.isdigit(): score_text = f"{t1}-{t2}"

            status_tag = m.select_one(main_scraper.SELECTORS["STATUS"])
            status_text = status_tag.text.strip().lower() if status_tag else ""
            marker_elem = m.select_one(".marker")
            if marker_elem: status_text += " " + marker_elem.text.strip().lower()
            mid = f"{start_utc.strftime('%Y%m%d')}_{local[:3]}_{visitante[:3]}".lower().replace(" ", "")
            
            if main_scraper.CONFIG["TEAM_NAME"] in local.lower(): lugar = f"Estadio Local ({local})"
            else: lugar = f"Estadio Visitante ({local})"

            matches.append({
                'id': mid, 'local': local, 'visitante': visitante,
                'competicion': comp_raw, 'inicio': start_utc,
                'is_tbd': is_tbd, 'lugar': lugar,
                'start_api': start_api, 'end_api': end_api,
                'score': score_text, 'status': status_text,
                'link': match_link, 'season': season_text 
            })
        except Exception as e:
            logging.warning(f"⚠️ Error al extraer datos de un partido histórico: {e}")
            continue
    return matches

def run_historical_sync(start_year, end_year, is_dry_run):
    log_file = open("historical_sync_log.txt", "w", encoding="utf-8")
    
    def log_both(msg):
        logging.info(msg)
        log_file.write(msg + "\n")
        log_file.flush()

    log_both(f"=== INICIANDO SINCRONIZACIÓN HISTÓRICA ===")
    log_both(f"Años objetivo: Desde {start_year} hasta {end_year} | Modo Dry-Run: {is_dry_run}")
    
    main_scraper.load_stadium_db()
    
    try:
        service = main_scraper.get_calendar_service()
        if not service: 
            log_both("❌ No se pudo conectar a Google Calendar.")
            return

        log_both("☁️ Descargando eventos existentes del calendario...")
        existing_events = {}
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId=main_scraper.CONFIG["CALENDAR_ID"], singleEvents=True, showDeleted=False, pageToken=page_token
            ).execute()
            for ev in events_result.get('items', []):
                if ev.get('status') != 'cancelled':
                    eid = ev.get('extendedProperties', {}).get('shared', {}).get('match_id')
                    if eid: existing_events[eid] = ev
            page_token = events_result.get('nextPageToken')
            if not page_token: break
            
        logging.info(f"✅ {len(existing_events)} eventos indexados en el calendario.")

        # Iterar desde el año inicial al final
        # Nota: API de besoccer usa season=1924 para la temporada 1923-1924
        for year in range(start_year, end_year + 1):
            api_season = year + 1 # 1923 -> 1924
            season_text = f"{year}-{str(api_season)[-2:]}"
            
            log_both(f"\n====================================")
            log_both(f"📅 PROCESANDO TEMPORADA: {season_text} (API: {api_season})")
            log_both(f"====================================")
            
            url = f"https://es.besoccer.com/ajax/changeMatches?teamId=712&competition=&season={api_season}"
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://es.besoccer.com/equipo/partidos/celta"
            }
            
            try:
                # Usamos curl_cffi para bypassear a Cloudflare/Incapsula
                r = requests.get(url, headers=headers, impersonate="chrome110")
                if r.status_code != 200:
                    logging.error(f"❌ Error HTTP {r.status_code} al obtener datos de {season_text}")
                    time.sleep(2)
                    continue
                    
                data = r.json()
                matches_html = data.get("matches", "")
                if not matches_html:
                    log_both(f"ℹ️ API devolvió OK pero sin partidos para {season_text}.")
                    continue
                    
                matches_raw = process_matches_from_html(matches_html, season_text)
                matches = [m for m in matches_raw if abs(m['inicio'].year - api_season) <= 2]
            except Exception as e:
                logging.error(f"❌ Error al consultar API para {season_text}: {e}")
                time.sleep(2)
                continue

            if not matches:
                log_both(f"ℹ️ Sin partidos parseables detectados para {season_text}.")
                time.sleep(1)
                continue
                
            log_both(f"📊 {len(matches)} partidos encontrados. Verificando con el calendario...")
            
            for match in matches:
                ev = main_scraper.find_existing_event(match, existing_events)
                is_finished = 'fin' in match['status'].lower()
                
                s_name_cache, s_loc_cache, real_db_key = main_scraper.find_stadium_dynamic(match['local'])
                stadium_name = s_name_cache
                full_address = s_loc_cache
                
                if full_address and 'Estadio Local' in match['lugar']: match['lugar'] = full_address
                elif full_address and 'Estadio Visitante' in match['lugar']: match['lugar'] = full_address

                comp_name, icon, color = main_scraper.get_competition_details(match['competicion'])
                match_month = match['inicio'].month
                if 'amistoso' in comp_name.lower() and match_month in [7, 8]: comp_name = 'Pretemporada'
                if comp_name == 'Primera División': comp_name = 'Liga'
                
                round_tag, ko_leg_raw = main_scraper.get_round_details(match['competicion'])
                ko_leg = main_scraper.infer_ko_leg(match, matches, ko_leg_raw, round_tag)
                display_tbd = match['is_tbd']

                base_title = f"{match['local']} vs {match['visitante']}"
                if match['score'] and is_finished: base_title = f"{match['local']} {match['score']} {match['visitante']}"
                
                ko_title_suffix = ""
                full_title_suffix = f" |{icon}{comp_name}"
                if round_tag and 'amistoso' not in comp_name.lower() and 'pretemporada' not in comp_name.lower():
                    round_tag_short = round_tag.replace(" de Final", "")
                    if ko_leg == 'ida': ko_title_suffix = " (Ida)"
                    elif ko_leg == 'vuelta': ko_title_suffix = " (Vuelta)"
                    
                    if match['inicio'].year < 2012:
                        ko_title_suffix = "" # Desactivado para ligas antiguas por petición del usuario
                    
                    full_title_suffix += f" | {round_tag_short}{ko_title_suffix}"
                
                full_title = f"{base_title}{full_title_suffix}"
                if display_tbd and match['inicio'].year >= 2024:
                    full_title = f"(TBC) {full_title}"
                log_suffix = main_scraper.format_log_date(match['inicio'], display_tbd)
                
                season_display = main_scraper.format_season(match.get('season', ''))
                round_str = round_tag
                league_half = ""
                if round_str and round_str.startswith("J") and round_str[1:].isdigit(): 
                    round_num = round_str[1:]
                    league_half = main_scraper.get_league_half(comp_name, round_str)
                    if match['inicio'].year < 2012:
                        league_half = "" # Desactivado para ligas antiguas por petición del usuario
                    round_str = f"Jornada {round_num}"
                    total_rounds = main_scraper.get_euro_max_rounds(comp_name, season_display)
                    if total_rounds: round_str = f"Jornada {round_num} de {total_rounds}"

                is_playoff = 'play off' in match['competicion'].lower() or 'promoción' in match['competicion'].lower()
                clean_comp = match['competicion'].split('.')[0].strip()
                if is_playoff:
                    desc_comp_line = main_scraper.format_playoff_description(match['competicion'], round_str)
                    desc_text = f"{desc_comp_line}\n"
                else:
                    if round_str:
                        league_half_str = f"({league_half}) " if league_half and not ko_leg else ""
                        desc_text = f"{icon} {clean_comp} | ▶️ {league_half_str}{round_str}\n"
                    else:
                        desc_text = f"{icon} {clean_comp}\n"
                
                desc_text += f"📅 Temporada {season_display}\n"
                
                # --- KO DESC LOGIC (Desde 2012 en adelante) ---
                if match['inicio'].year >= 2012 and ko_leg and 'amistoso' not in comp_name.lower():
                    ko_desc = ""
                    celta_name = "Celta"
                    is_celta_local = celta_name in match['local']
                    rival_name = match['visitante'] if is_celta_local else match['local']
                    celta_goals = main_scraper.get_team_goals(match['score'], match['local'], celta_name, match['visitante'])
                    rival_goals = main_scraper.get_team_goals(match['score'], match['local'], rival_name, match['visitante'])
                    
                    if ko_leg == 'ida':
                        ko_desc = f"➡️ Ida de {round_tag}\n"
                    elif ko_leg == 'vuelta':
                        ko_desc = f"⬅️ Vuelta de {round_tag}\n"
                        ida_match = None
                        for m in matches:
                            m_comp, _, _ = main_scraper.get_competition_details(m['competicion'])
                            m_round, m_leg_raw = main_scraper.get_round_details(m['competicion'])
                            m_leg = main_scraper.infer_ko_leg(m, matches, m_leg_raw, m_round)
                            if m_comp == comp_name and m_round == round_tag and m_leg == 'ida':
                                if m['local'] == match['visitante'] and m['visitante'] == match['local']:
                                    ida_match = m; break
                        if ida_match and is_finished:
                            c_ida = main_scraper.get_team_goals(ida_match['score'], ida_match['local'], celta_name, ida_match['visitante'])
                            r_ida = main_scraper.get_team_goals(ida_match['score'], ida_match['local'], rival_name, ida_match['visitante'])
                            c_glo = celta_goals + c_ida
                            r_glo = rival_goals + r_ida
                            ko_desc += f"⏪ Resultado Ida: {ida_match['local']} {ida_match.get('score', '')} {ida_match['visitante']}\n"
                            ko_desc += f"📊 Global: {celta_name} {c_glo} - {r_glo} {rival_name}\n"
                            if c_glo > r_glo: ko_desc += f"🎉 ¡Pasamos a la siguiente ronda!\n"
                            elif r_glo > c_glo: ko_desc += f"💔 Quedamos eliminados en {round_tag}...\n"
                            else:
                                pen_c, pen_r = main_scraper.extract_penalties((match['score'] or "") + " " + match['status'])
                                if pen_c is not None:
                                    c_pen = pen_c if is_celta_local else pen_r
                                    r_pen = pen_r if is_celta_local else pen_c
                                    ko_desc += f"🎯 Penaltis: {celta_name} {c_pen} - {r_pen} {rival_name}\n"
                                    if c_pen > r_pen: ko_desc += f"🎉 ¡Pasamos de ronda en Penaltis!\n"
                                    else: ko_desc += f"💔 Eliminados en {round_tag} por Penaltis...\n"
                                else:
                                    ko_desc += "⚖️ Empate global (Pendiente de resolución oficial)\n"
                                    if not "(Resolución Pendiente)" in full_title: full_title += " (Resolución Pendiente)"
                    elif ko_leg == 'unico':
                        ko_desc = f"⚔️ Partido Único\n"
                        if is_finished:
                            is_playoff = 'play off' in match['competicion'].lower() or 'promoción' in match['competicion'].lower()
                            is_descenso = 'descenso' in match['competicion'].lower() or 'permanencia' in match['competicion'].lower()
                            
                            if celta_goals > rival_goals:
                                if is_playoff and round_tag == "Final":
                                    ko_desc += "🎉 ¡CONSEGUIMOS LA PERMANENCIA! 🎉\n" if is_descenso else "🎉 ¡SOMOS DE PRIMERA! 🎉\n"
                                elif round_tag == "Final": ko_desc += "🏆 ¡SOMOS CAMPEONES! 🏆\n"
                                else: ko_desc += f"🎉 ¡Pasamos a la siguiente ronda!\n"
                            elif rival_goals > celta_goals:
                                if is_playoff and round_tag == "Final":
                                    ko_desc += "🫧 ¡somos de Segunda...! 🫧\n" if is_descenso else "💔 No logramos el ascenso...\n"
                                elif round_tag == "Final": ko_desc += "🥈 Subcampeones...\n"
                                else: ko_desc += f"💔 Quedamos eliminados en {round_tag}...\n"
                            else:
                                pen_c, pen_r = main_scraper.extract_penalties((match['score'] or "") + " " + match['status'])
                                if pen_c is not None:
                                    c_pen = pen_c if is_celta_local else pen_r
                                    r_pen = pen_r if is_celta_local else pen_c
                                    ko_desc += f"🎯 Penaltis: {celta_name} {c_pen} - {r_pen} {rival_name}\n"
                                    if c_pen > r_pen:
                                        if is_playoff and round_tag == "Final":
                                            ko_desc += "🎉 ¡CONSEGUIMOS LA PERMANENCIA en Penaltis! 🎉\n" if is_descenso else "🎉 ¡SOMOS DE PRIMERA en Penaltis! 🎉\n"
                                        elif round_tag == "Final": ko_desc += "🏆 ¡SOMOS CAMPEONES en Penaltis! 🏆\n"
                                        else: ko_desc += f"🎉 ¡Pasamos de ronda en Penaltis!\n"
                                    else:
                                        if is_playoff and round_tag == "Final":
                                            ko_desc += "🫧 ¡somos de Segunda...! (Penaltis) 🫧\n" if is_descenso else "💔 No logramos el ascenso... (Penaltis)\n"
                                        else: ko_desc += f"💔 Eliminados en {round_tag} por Penaltis...\n"
                                else:
                                    ko_desc += "⚖️ Empate (Pendiente de penaltis/prórroga)\n"
                                    if not "(Resolución Pendiente)" in full_title: full_title += " (Resolución Pendiente)"
                    
                    if ko_desc: desc_text += "\n" + ko_desc + "\n"
                
                loc_final = full_address if full_address else match['lugar'] 
                if stadium_name: desc_text += f"🏟️ Estadio: {stadium_name}\n"
                else: desc_text += f"📍 {match['lugar']}\n"
                desc_text += f"🔗 Más Info Histórica: {match.get('link', '')}"  

                event_body = {
                    'summary': full_title,
                    'location': loc_final, 
                    'description': desc_text,
                    'start': match['start_api'],
                    'end': match['end_api'],
                    'colorId': color,
                    'extendedProperties': {'shared': {'match_id': match['id']}},
                    'reminders': {'useDefault': False, 'overrides': []} 
                }

                if ev:
                    needs_update = False
                    change_details = [] 

                    old_title_norm = main_scraper.normalize_text(ev.get('summary', ''))
                    new_title_norm = main_scraper.normalize_text(full_title)
                    
                    old_start_str = ev.get('start', {}).get('dateTime')
                    start_api_str = event_body['start'].get('dateTime')

                    if display_tbd:
                        # For TBD matches, compare local time strings (ignoring TZ)
                        def strip_tz(dt_str):
                            if not dt_str: return ""
                            if '+' in dt_str: return dt_str.split('+')[0]
                            if 'Z' in dt_str: return dt_str.split('Z')[0]
                            return dt_str
    
                        if strip_tz(old_start_str) != strip_tz(start_api_str):
                            needs_update = True
                            change_details.append(f"⏰ Hora: {old_start_str} -> {start_api_str}")
                    else:
                        # For confirmed matches, compare exact timestamps
                        old_dt = main_scraper.parse_google_iso(old_start_str)
                        if old_dt:
                            diff = abs(old_dt.timestamp() - match['inicio'].timestamp())
                            if diff > 60:
                                needs_update = True
                                change_details.append(f"⏰ Hora: {old_start_str} -> {start_api_str}")
                    
                    if old_title_norm != new_title_norm:
                        needs_update = True
                        change_details.append(f"📝 Título: '{ev.get('summary')}' -> '{full_title}'")

                    if not needs_update:
                        current_desc = main_scraper.normalize_text(ev.get('description', ''))
                        new_desc_norm = main_scraper.normalize_text(desc_text)
                        if current_desc != new_desc_norm: 
                            needs_update = True
                            change_details.append("📄 Descripción")
                            
                    if needs_update:
                        if is_dry_run:
                            log_both(f"[DRY-RUN] 🔄 ACTUALIZARÍA: {full_title} | Cambios: {', '.join(change_details)}")
                        else:
                            req = service.events().update(calendarId=main_scraper.CONFIG["CALENDAR_ID"], eventId=ev['id'], body=event_body)
                            main_scraper.execute_with_retry(req)
                            log_both(f"[+] 🔄 Actualizado: {full_title} | Cambios: {', '.join(change_details)}")
                            time.sleep(1) 
                else:
                    if is_dry_run:
                        log_both(f"[DRY-RUN] ✅ CREARÍA: {full_title} | {log_suffix}")
                    else:
                        req = service.events().insert(calendarId=main_scraper.CONFIG["CALENDAR_ID"], body=event_body)
                        main_scraper.execute_with_retry(req)
                        log_both(f"[+] ✅ Nuevo: {full_title}")
                        time.sleep(1) 
            
            log_both(f"⏳ Pausa antes de la siguiente temporada...")
            time.sleep(2)

        log_both("\n🎉 SINCRONIZACIÓN HISTÓRICA TERMINADA.")
        log_file.close()

    except Exception as e:
        log_both(f"❌ Error fatal en sincro histórica: {e}")
        log_file.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="CeltaCalendar Historical Sync")
    parser.add_argument("--start", type=int, default=1923, help="Año de inicio de la temporada (Ej. 1923 para 1923-24)")
    parser.add_argument("--end", type=int, default=datetime.datetime.now().year, help="Año de fin de la temporada (Ej. 2023 para 2023-24)")
    parser.add_argument("--dry-run", action="store_true", help="Si se incluye, no modificará Google Calendar")
    
    args = parser.parse_args()
    run_historical_sync(args.start, args.end, args.dry_run)
