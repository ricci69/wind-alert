import requests
import json
import time
from datetime import datetime, timedelta
from telegram import Bot
from telegram.constants import ParseMode
import asyncio

# Leggi le credenziali dal file.env
def load_env():
    env_vars = {}
    try:
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
        print("[DEBUG] Variabili d'ambiente caricate:", list(env_vars.keys()))
    except FileNotFoundError:
        print("[ERRORE] File.env non trovato")
    except Exception as e:
        print(f"[ERRORE] Impossibile leggere.env: {e}")
    return env_vars

# Coordinate di Francavilla al Mare (corrette)
LAT = 42.42158
LON = 14.28217

# Valore soglia di vento (km/h)
WIND_THRESHOLD = 40

# Headers personalizzati per evitare blocchi antibot
HEADERS = {
    "User-Agent": "wind-alert-script/1.0 (+https://github.com/ricci69/wind-alert)"
}

def get_wind_forecast():
    """Recupera le previsioni del vento dall'API di Open-Meteo con retry e header personalizzato"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "windspeed_10m,windgusts_10m",
        "forecast_days": 2,  # 48 ore = 2 giorni
        "timezone": "Europe/Rome"
    }
    print(f"[DEBUG] Richiesta API: {url} con parametri {params}")
    max_retries = 3
    backoff_factor = 2  # secondi
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=10)
            print(f"[DEBUG] Status code risposta: {response.status_code}")
            # Se il codice è 200, procedi; se è 5xx, prova nuovamente
            if response.status_code == 200:
                data = response.json()
                print(f"[DEBUG] Dati ricevuti: {json.dumps(data)[:200]}...")
                return data
            elif 500 <= response.status_code < 600:
                print(f"[ATTENZIONE] Errore server {response.status_code}, tentativo {attempt}/{max_retries}")
            else:
                # Altri errori (4xx) non meritano retry
                response.raise_for_status()
        except requests.RequestException as e:
            print(f"[ERRORE] Eccezione durante la richiesta: {e}")
            if attempt == max_retries:
                print("[ERRORE] Numero massimo di tentativi raggiunto")
                return None
        # Attesa esponenziale prima del retry
        if attempt < max_retries:
            wait_time = backoff_factor ** (attempt - 1)
            print(f"[INFO] Attendo {wait_time}s prima del prossimo tentativo...")
            time.sleep(wait_time)
    print("[ERRORE] Impossibile ottenere dati dopo diversi tentativi")
    return None

def check_wind_thresholds(data):
    """Verifica se il vento medio o le raffiche superano la soglia impostata nelle diverse finestre temporali"""
    if not data or 'hourly' not in data:
        print("[ERRORE] Dati mancanti o senza sezione 'hourly'")
        return None
    times = data['hourly']['time']
    wind_speeds = data['hourly']['windspeed_10m']
    gust_speeds = data['hourly'].get('windgusts_10m', [])

    # Crea una lista di tuple (timestamp, vento, raffica) ordinata per timestamp
    hourly_data = []
    for i, time_str in enumerate(times):
        wind_speed = wind_speeds[i]
        gust_speed = gust_speeds[i] if i < len(gust_speeds) else 0
        hourly_data.append((time_str, wind_speed, gust_speed))
    
    print(f"[DEBUG] Numero di record orari: {len(hourly_data)}")

    # Ordina i dati per timestamp
    hourly_data.sort(key=lambda x: x[0])

    # Definisci le finestre temporali da controllare
    now = datetime.now()
    thresholds = {}
    for hours in [6, 12, 24, 48]:
        # Calcola l'intervallo di tempo da controllare
        start_time = now
        end_time = now + timedelta(hours=hours)
        
        # Trova il valore massimo di vento e raffica nell'intervallo
        max_wind = 0
        max_gust = 0
        
        for time_str, wind_speed, gust_speed in hourly_data:
            time_dt = datetime.strptime(time_str, '%Y-%m-%dT%H:%M')
            if start_time <= time_dt <= end_time:
                if wind_speed > max_wind:
                    max_wind = wind_speed
                if gust_speed > max_gust:
                    max_gust = gust_speed
        
        exceeds_wind = max_wind > WIND_THRESHOLD
        exceeds_gust = max_gust > WIND_THRESHOLD
        
        thresholds[f"{hours}h_wind"] = exceeds_wind
        thresholds[f"{hours}h_wind_value"] = max_wind
        thresholds[f"{hours}h_gust"] = exceeds_gust
        thresholds[f"{hours}h_gust_value"] = max_gust
        
        print(f"[DEBUG] Controllo {hours}h vento: max {max_wind} km/h -> supera {WIND_THRESHOLD}? {exceeds_wind}")
        print(f"[DEBUG] Controllo {hours}h raffica: max {max_gust} km/h -> supera {WIND_THRESHOLD}? {exceeds_gust}")

    return thresholds

def load_previous_state():
    """Carica lo stato precedente da file"""
    try:
        with open('wind_state.json', 'r') as f:
            state = json.load(f)
        print(f"[DEBUG] Stato precedente caricato: {state}")
        return state
    except FileNotFoundError:
        print("[DEBUG] Nessun file di stato precedente trovato")
        return None
    except Exception as e:
        print(f"[ERRORE] Impossibile leggere lo stato precedente: {e}")
        return None

def save_current_state(state):
    """Salva lo stato corrente su file"""
    try:
        with open('wind_state.json', 'w') as f:
            json.dump(state, f, indent=2)
        print(f"[DEBUG] Stato corrente salvato: {state}")
    except Exception as e:
        print(f"[ERRORE] Impossibile salvare lo stato: {e}")

async def send_telegram_alert(env_vars, message):
    """Invia un messaggio tramite Telegram (versione async)"""
    try:
        bot_token = env_vars.get('TELEGRAM_BOT_TOKEN')
        chat_id = env_vars.get('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            print("[ERRORE] Credenziali Telegram non trovate nel file.env")
            return False
        print("[DEBUG] Invio messaggio Telegram...")
        bot = Bot(token=bot_token)
        await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
        print("[DEBUG] Messaggio Telegram inviato con successo")
        return True
    except Exception as e:
        print(f"[ERRORE] Errore nell'invio del messaggio: {e}")
        return False

def main():
    print(f"=== Avvio script wind_alert === {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Carica credenziali
    env_vars = load_env()
    if not env_vars:
        print("[ERRORE] Nessuna variabile d'ambiente caricata, interrompo")
        return

    # Recupera previsioni
    data = get_wind_forecast()
    if not data:
        print("[ERRORE] Impossibile recuperare le previsioni, interrompo")
        return

    # Verifica soglie
    current_state = check_wind_thresholds(data)
    if not current_state:
        print("[ERRORE] Impossibile verificare le soglie, interrompo")
        return

    # Carica stato precedente
    previous_state = load_previous_state()

    # Genera messaggio di allerta
    alert_message = "⚠️ <b>Allerta vento a Francavilla al Mare</b>\n\n"
    alert_message += f"Nelle prossime ore, il vento medio o le raffiche supereranno i {WIND_THRESHOLD} km/h:\n"
    has_alert = False
    for key, exceeds in current_state.items():
        if key.endswith('_wind') and exceeds:
            has_alert = True
            # key format: "6h_wind" -> extract numeric part
            hours_part = key.split('_')[0]  # e.g., "6h"
            hours = hours_part[:-1]  # remove trailing "h"
            value = current_state.get(f"{hours}h_wind_value", 0)
            alert_message += f"• {hours}h: vento medio {value:.1f} km/h\n"
        elif key.endswith('_gust') and exceeds:
            has_alert = True
            # key format: "6h_gust" -> extract numeric part
            hours_part = key.split('_')[0]  # e.g., "6h"
            hours = hours_part[:-1]  # remove trailing "h"
            value = current_state.get(f"{hours}h_gust_value", 0)
            alert_message += f"• {hours}h: raffica {value:.1f} km/h\n"
    print(f"[DEBUG] Messaggio di allerta generato (has_alert={has_alert}):\n{alert_message}")

    # Verifica se lo stato è cambiato
    state_changed = False
    if previous_state is None:
        state_changed = True
        print("[DEBUG] Nessuno stato precedente, considerato cambiato")
    else:
        for key in current_state:
            if key.endswith('_wind') or key.endswith('_gust'):
                if current_state[key] != previous_state.get(key):
                    state_changed = True
                    break
        if not state_changed:
            print("[DEBUG] Stato invariato rispetto al precedente")

    # Invia notifica solo se necessario
    if has_alert and state_changed:
        alert_message += "\n⚠️ Rischio vento forte!"
        print("[INFO] Condizioni soddisfatte: invio notifica Telegram")
        asyncio.run(send_telegram_alert(env_vars, alert_message))
        print("Notifica inviata")
    elif not has_alert and previous_state and not any(previous_state.get(k, False) for k in previous_state if k.endswith('_wind') or k.endswith('_gust')):
        print("[INFO] Nessun allerta da segnalare e nessun precedente allerta")
    elif has_alert and not state_changed:
        print("[INFO] Allerta presente ma stato non cambiato, nessuna notifica inviata")
    else:
        print("[DEBUG] Nessuna condizione di invio notifica soddisfatta")

    # Salva stato corrente
    save_current_state(current_state)
    print("=== Fine script wind_alert ===")

if __name__ == '__main__':
    main()
