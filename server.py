import sqlite3, os, uuid, shlex, time, threading, json, io, base64
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template_string, g, session, 
    redirect, url_for, Response, make_response
)
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, 
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_talisman import Talisman
from apscheduler.schedulers.background import BackgroundScheduler
# Zmieniamy 'sqlalchemy' na 'memory' dla prostszego trybu testowego
# from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.memory import MemoryJobStore

# Nowe importy dla MFA
import pyotp
import qrcode
from itsdangerous import URLSafeSerializer, BadSignature

# --- Szablon Agenta (v5 - kompatybilny z v6) ---
# Ten szablon jest dynamicznie wypełniany i serwowany przez serwer
AGENT_TEMPLATE_V6 = """
import socketio, time, subprocess, psutil, platform, os, requests, threading, shlex, sys, json, base64

# --- Konfiguracja Agenta (WSTRZYKNIĘTA PRZEZ SERWER) ---
AGENT_ID = "{agent_id}"
API_KEY = "{api_key}"
SERVER_PROTO = "{server_proto}"
WS_PROTO = "{ws_proto}"
SERVER_HOST = "{server_host}"
# -----------------------------------------------------

SERVER_URL = f"{{SERVER_PROTO}}://{{SERVER_HOST}}"
WS_URL = f"{{WS_PROTO}}://{{SERVER_HOST}}"
AGENT_VERSION = "v5.0" # Agent v5 jest kompatybilny z serwerem v6
METRICS_INTERVAL = 10

sio = socketio.Client(reconnection_delay_max=10)
metrics_thread = None
stop_metrics = threading.Event()
agent_session = requests.Session()
agent_session.headers.update({{'X-API-Key': API_KEY}})
# agent_session.verify = False # Odkomentuj, jeśli używasz samopodpisanego certyfikatu

def get_agent_details():
    return {{"agent_version": AGENT_VERSION, "agent_os_details": platform.platform(), "hostname": platform.node()}}

def run_shell_command(command_payload):
    print(f"[AGENT] Wykonywanie (BEZPIECZNE) polecenia shell: {{command_payload}}")
    try:
        # Użyj powershell dla Windows, shlex dla reszty
        if platform.system() == "Windows":
            args = ['powershell.exe', '-NoProfile', '-Command', command_payload]
            shell_mode = False # PowerShell obsługuje polecenie jako argument
        else:
            args = shlex.split(command_payload)
            shell_mode = False
            
        result = subprocess.run(
            args, shell=shell_mode, capture_output=True, text=True, 
            timeout=120, encoding='utf-8', errors='ignore'
        )
        return (result.stdout or result.stderr).strip()
    except Exception as e:
        print(f"[AGENT] Błąd wykonania polecenia shell: {{e}}")
        return f"Błąd agenta: {{str(e)}}"

def run_custom_command(command_payload):
    print(f"[AGENT] Pobieranie własnej komendy: {{command_payload}}")
    try:
        response = agent_session.get(f"{{SERVER_URL}}/api/agents/get_custom_command/{{command_payload}}")
        if response.status_code == 200:
            script_content = response.json().get('script_content')
            print(f"[AGENT] Wykonywanie własnej komendy...")
            return run_shell_command(script_content)
        else:
            return f"Błąd: Nie znaleziono własnej komendy '{{command_payload}}' na serwerze."
    except Exception as e:
        print(f"[AGENT] Błąd pobierania własnej komendy: {{e}}")
        return f"Błąd agenta: {{str(e)}}"

def run_self_update(command_payload):
    print(f"[AGENT] Rozpoczynanie samoczynnej aktualizacji...")
    try:
        response = agent_session.get(f"{{SERVER_URL}}/api/agents/get_self_update/{{command_payload}}")
        if response.status_code != 200:
            return "Błąd pobierania nowego agenta z serwera."
        
        new_agent_content = response.text
        
        # Zapisz nowy skrypt do tymczasowego pliku
        script_path = os.path.realpath(__file__)
        temp_path = script_path + ".new"
        with open(temp_path, "w", encoding='utf-8') as f:
            f.write(new_agent_content)
        
        # Zastąp stary skrypt nowym
        os.replace(temp_path, script_path)
        
        print("[AGENT] Aktualizacja zakończona. Restartuję...")
        
        # Uruchom nowy proces i zamknij stary
        # sys.executable to ścieżka do interpretera Pythona
        subprocess.Popen([sys.executable, script_path])
        sio.disconnect()
        sys.exit(0)
        
    except Exception as e:
        return f"Błąd podczas aktualizacji: {{str(e)}}"

def run_file_download(command_payload):
    print(f"[AGENT] Wykonywanie pobierania pliku: {{command_payload}}")
    try:
        filepath = command_payload
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            return f"Błąd: Plik nie istnieje lub nie jest plikiem: {{filepath}}"
        
        with open(filepath, "rb") as f:
            file_data = f.read()
        
        # Zwróć plik zakodowany w Base64
        return base64.b64encode(file_data).decode('utf-8')
        
    except Exception as e:
        return f"Błąd odczytu pliku: {{str(e)}}"

def get_metrics():
    try:
        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        
        # Sieć (proste statystyki)
        net_io = psutil.net_io_counters()
        
        return {{
            "cpu": psutil.cpu_percent(interval=None),
            "memory": psutil.virtual_memory().percent,
            "disk": psutil.disk_usage('/').percent,
            "uptime_seconds": uptime_seconds,
            "net_bytes_sent": net_io.bytes_sent,
            "net_bytes_recv": net_io.bytes_recv
        }}
    except Exception as e:
        print(f"[AGENT] Błąd pobierania metryk: {{e}}")
        return {{"cpu": 0, "memory": 0, "disk": 0, "uptime_seconds": 0, "net_bytes_sent": 0, "net_bytes_recv": 0}}

def send_metrics_loop():
    while not stop_metrics.is_set():
        try:
            if sio.connected:
                sio.emit('agent_metrics', get_metrics())
        except Exception as e:
            print(f"[AGENT] Błąd w pętli metryk: {{e}}")
        stop_metrics.wait(METRICS_INTERVAL)

# --- Definicje Eventów SocketIO dla Agenta ---

@sio.event
def connect():
    global metrics_thread, stop_metrics
    print("[AGENT] Połączono z serwerem. Uwierzytelniam...")
    auth_data = get_agent_details()
    auth_data['api_key'] = API_KEY
    sio.emit('agent_authenticate', auth_data)
    
    stop_metrics.clear()
    if metrics_thread is None or not metrics_thread.is_alive():
        metrics_thread = threading.Thread(target=send_metrics_loop, daemon=True)
        metrics_thread.start()

@sio.event
def connect_error(data):
    print(f"[AGENT] Błąd połączenia: {{data}}")

@sio.event
def disconnect():
    global stop_metrics
    print("[AGENT] Rozłączono z serwerem.")
    stop_metrics.set()

@sio.on('auth_success')
def on_auth_success(data):
    print(f"[AGENT] Uwierzytelnienie pomyślne. Agent ID: {{data['agent_id']}}")

@sio.on('auth_failed')
def on_auth_failed(data):
    print(f"[AGENT] Błąd uwierzytelnienia: {{data['message']}}. Sprawdź klucz API i adres serwera.")
    sio.disconnect()

@sio.on('execute_command')
def on_execute_command(data):
    command_type = data.get('command_type')
    command_payload = data.get('command_payload')
    command_db_id = data.get('command_db_id')
    
    result = ""
    try:
        if command_type == 'shell':
            result = run_shell_command(command_payload)
        elif command_type == 'custom':
            result = run_custom_command(command_payload)
        elif command_type == 'self_update':
            result = run_self_update(command_payload)
        elif command_type == 'file_download':
            result = run_file_download(command_payload)
        else:
            result = f"Błąd: Nieznany typ polecenia '{{command_type}}'"
    except Exception as e:
        result = f"Błąd krytyczny agenta: {{str(e)}}"

    sio.emit('agent_command_result', {{
        'command_db_id': command_db_id,
        'result': result
    }})

# --- Pętla Terminala (Uproszczona, Feat. 8) ---
# Prawdziwa implementacja wymaga PTY (pseudo-terminal)
@sio.on('shell_pty_start')
def on_shell_pty_start(data):
    sio.emit('shell_pty_data', {{'data': 'Interaktywny terminal (PTY) nie jest jeszcze zaimplementowany w tym agencie.\\n'}})

@sio.on('shell_pty_input')
def on_shell_pty_input(data):
    # Uproszczona emulacja: wykonaj komendę i zwróć wynik
    command = data.get('input', '')
    if command.strip().lower() in ['exit', 'quit']:
        sio.emit('shell_pty_data', {{'data': '\\n[Proces zakończony]\\n'}})
        return
        
    result = run_shell_command(command)
    sio.emit('shell_pty_data', {{'data': f"\\n{result}\\n> "}})

def main():
    print(f"[AGENT] Uruchamiam agenta v{{AGENT_VERSION}} (ID: {{AGENT_ID}})")
    print(f"[AGENT] Próbuję połączyć się z {{WS_URL}}...")
    try:
        sio.connect(WS_URL, transports=['websocket'])
        sio.wait()
    except socketio.exceptions.ConnectionError as e:
        print(f"[AGENT] Nie udało się połączyć z serwerem: {{e}}")
    except KeyboardInterrupt:
        print("[AGENT] Zamykanie...")
    finally:
        if sio.connected:
            sio.disconnect()

if __name__ == "__main__":
    main()
"""

# --- Konfiguracja Aplikacji ---
app = Flask(__name__)
# KLUCZ GŁÓWNY musi być ustawiony jako zmienna środowiskowa!
# Używany do szyfrowania sekretów MFA i tokenów pobierania.
# Ustaw go w terminalu: export APP_MASTER_KEY='twoj_dlugi_tajny_klucz'
app.config['SECRET_KEY'] = os.environ.get('APP_MASTER_KEY')
if not app.config['SECRET_KEY']:
    print("OSTRZEŻENIE: Brak APP_MASTER_KEY. Używam tymczasowego klucza. NIE UŻYWAĆ W PRODUKCJI.")
    app.config['SECRET_KEY'] = 'tymczasowy-klucz-tylko-do-testow'

app.config['DATABASE'] = 'agents_v6.db'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False # Zmień na True w produkcji z HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Inicjalizacja Serializera (do tokenów MFA i pobierania)
token_serializer = URLSafeSerializer(app.config['SECRET_KEY'])
MFA_SECRET_SALT = 'mfa-setup-salt'
DOWNLOAD_TOKEN_SALT = 'download-agent-salt'

# Konfiguracja Bezpieczeństwa (Talisman)
# W trybie debug CSP jest wyłączony, aby umożliwić ładowanie skryptów
is_debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
csp = {
    'default-src': "'self'",
    'script-src': [
        "'self'",
        "https://cdnjs.cloudflare.com" # Dla SocketIO i Chart.js
    ],
    'style-src': [
        "'self'",
        "'unsafe-inline'" # Dla dynamicznych stylów
    ],
    'connect-src': [
        "'self'",
        "wss://*.ngrok.io", # Dla testów z ngrok (opcjonalne)
        "ws://*.ngrok.io"  # Dla testów z ngrok (opcjonalne)
    ],
    'img-src': [
        "'self'",
        "data:" # Dla kodów QR (MFA)
    ]
}
Talisman(app, content_security_policy=csp if not is_debug else None)

# Inicjalizacja SocketIO i LoginManager
# 'eventlet' jest potrzebny do obsługi WebSockets w trybie debug Flaska
socketio = SocketIO(app, async_mode='eventlet') 
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Przekieruj niezalogowanych do /login
login_manager.login_message = "Musisz się zalogować, aby zobaczyć tę stronę."

# --- Konfiguracja Schedulera (APScheduler) ---
jobstores = {
    'default': MemoryJobStore()
    # W produkcji zamień na:
    # 'default': SQLAlchemyJobStore(url=f'sqlite:///{app.config["DATABASE"]}')
}
scheduler = BackgroundScheduler(jobstores=jobstores, daemon=True)
# scheduler.start() # Uruchomimy go po zainicjowaniu bazy

# === Zarządzanie Bazą Danych ===

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(app.config['DATABASE'])
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    if os.path.exists(app.config['DATABASE']):
        print("Baza danych już istnieje.")
        # Sprawdź, czy istnieje domyślny admin
        with app.app_context():
            db = get_db()
            admin = db.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            if not admin:
                print("Tworzenie domyślnego użytkownika admin...")
                create_default_admin(db)
            
            # Wypełnij domyślne komendy
            populate_default_custom_commands(db)
        return

    print("Tworzę nową bazę danych...")
    with app.app_context():
        db = get_db()
        try:
            with app.open_resource('schema_v6.sql', mode='r') as f:
                db.cursor().executescript(f.read())
            db.commit()
            print("Baza danych utworzona pomyślnie.")
            # Utwórz domyślnego admina
            create_default_admin(db)
            # Wypełnij domyślne komendy
            populate_default_custom_commands(db)
        except FileNotFoundError:
            print("KRYTYCZNY BŁĄD: Nie znaleziono pliku 'schema_v6.sql'.")
            
def create_default_admin(db):
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ('admin', generate_password_hash('admin'), 'admin')
        )
        db.commit()
        print("Utworzono domyślnego użytkownika: admin / hasło: admin")
    except sqlite3.IntegrityError:
        print("Użytkownik 'admin' już istnieje.")
    except Exception as e:
        print(f"Błąd podczas tworzenia admina: {e}")

def populate_default_custom_commands(db):
    print("Wypełniam domyślne własne komendy...")
    admin_id = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin_id:
        print("Nie można wypełnić komend, brak użytkownika 'admin'.")
        return
    admin_id = admin_id['id']
    
    commands = [
        ('setup_persistence_linux', 'Tworzy usługę systemd dla agenta (Linux).', 'linux', 
         'SCRIPT_PATH=$(realpath $0)\n'
         'PYTHON_PATH=$(which python3 || which python)\n'
         'SERVICE_NAME=rmm_agent\n'
         'SERVICE_FILE_PATH="/etc/systemd/system/$SERVICE_NAME.service"\n'
         'echo "[Unit]\nDescription=RMM Agent Service\nAfter=network.target\n\n[Service]\nUser=root\nWorkingDirectory=$(dirname $SCRIPT_PATH)\nExecStart=$PYTHON_PATH $SCRIPT_PATH\nRestart=always\n\n[Install]\nWantedBy=multi-user.target" | sudo tee $SERVICE_FILE_PATH > /dev/null\n'
         'sudo systemctl daemon-reload\n'
         'sudo systemctl enable $SERVICE_NAME\n'
         'sudo systemctl start $SERVICE_NAME\n'
         'echo "Usługa systemd $SERVICE_NAME została utworzona i uruchomiona."'),
         
        ('setup_persistence_windows', 'Tworzy zadanie harmonogramu dla agenta (Windows).', 'windows',
         '$scriptPath = $MyInvocation.MyCommand.Path\n'
         '$pythonPath = (Get-Command python).Source\n'
         '$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $scriptPath\n'
         '$trigger = New-ScheduledTaskTrigger -AtLogOn\n'
         '$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\\SYSTEM" -RunLevel Highest\n'
         '$settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable\n'
         'Register-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -TaskName "RMM_Agent" -Description "Uruchamia agenta RMM przy starcie systemu." -Force\n'
         'Start-ScheduledTask -TaskName "RMM_Agent"\n'
         'Write-Output "Zadanie harmonogramu RMM_Agent zostało utworzone i uruchomione."'),
        
        ('list_processes_linux', 'Listuje 20 procesów najbardziej obciążających CPU (Linux)', 'linux',
         'ps -eo pid,ppid,%cpu,%mem,cmd --sort=-%cpu | head -n 20'),
         
        ('list_processes_windows', 'Listuje 20 procesów najbardziej obciążających CPU (Windows)', 'windows',
         'Get-Process | Sort-Object CPU -Descending | Select-Object -First 20 | Format-Table Id, ProcessName, CPU, WorkingSet -AutoSize'),
         
        ('self_update', 'Polecenie wewnętrzne do samoczynnej aktualizacji agenta', 'any',
         'Agent zostanie zaktualizowany przy następnym pobraniu.')
    ]
    
    for name, desc, platform, script in commands:
        try:
            db.execute(
                "INSERT INTO custom_commands (name, description, platform, script_content, created_by_user_id) VALUES (?, ?, ?, ?, ?)",
                (name, desc, platform, script, admin_id)
            )
        except sqlite3.IntegrityError:
            pass # Komenda już istnieje
    db.commit()

# === Zarządzanie Użytkownikami i Sesją (Flask-Login) ===

class User(UserMixin):
    def __init__(self, id, username, role, mfa_enabled):
        self.id = id
        self.username = username
        self.role = role
        self.mfa_enabled = mfa_enabled
    
    def has_role(self, *roles):
        return self.role in roles

@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    user_data = db.execute("SELECT id, username, role, mfa_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
    if user_data:
        return User(user_data['id'], user_data['username'], user_data['role'], user_data['mfa_enabled'])
    return None

def role_required(*roles):
    """Dekorator do sprawdzania ról użytkownika."""
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if not current_user.has_role(*roles):
                return jsonify({"error": "Brak uprawnień"}), 403
            return f(*args, **kwargs)
        return decorated_function
    return wrapper

# === Szyfrowanie (dla sekretów MFA) ===
# Używamy prostego szyfrowania symetrycznego. W produkcji rozważ użycie Vault.
from cryptography.fernet import Fernet
# Klucz Fernet musi być 32-bajtowy, zakodowany w base64.
# Używamy hasha klucza aplikacji jako klucza Fernet.
import hashlib
fernet_key = base64.urlsafe_b64encode(hashlib.sha256(app.config['SECRET_KEY'].encode()).digest())
cipher_suite = Fernet(fernet_key)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return None
    return cipher_suite.decrypt(encrypted_data.encode()).decode()

# === Dziennik Audytu ===
def log_audit(action, user_id=None, username=None, ip_address=None, target_type=None, target_id=None, details=None):
    try:
        db = get_db()
        db.execute(
            """INSERT INTO audit_log 
               (user_id, username, ip_address, action, target_type, target_id, details) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, ip_address, action, target_type, target_id, details)
        )
        db.commit()
    except Exception as e:
        print(f"Błąd logowania audytu: {e}")

# === Stan Serwera ===
connected_agents = {} # Mapowanie: sid -> agent_id
agent_sids = {} # Mapowanie: agent_id -> sid

# === Główne Endpointy HTTP (Panel Webowy) ===

@app.route('/')
@login_required
def index():
    return render_template_string(open("index_v6.html", encoding="utf-8").read())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        mfa_code = data.get('mfa_code')
        
        db = get_db()
        user_data = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        
        if not user_data or not check_password_hash(user_data['password_hash'], password):
            log_audit('login_fail_password', username=username, ip_address=request.remote_addr)
            return jsonify({"error": "Nieprawidłowa nazwa użytkownika lub hasło"}), 401
            
        user_obj = User(user_data['id'], user_data['username'], user_data['role'], user_data['mfa_enabled'])
        
        # Sprawdzenie MFA
        if user_obj.mfa_enabled:
            if not mfa_code:
                # Krok 1: Hasło poprawne, wymagane MFA
                session['mfa_user_id'] = user_obj.id # Przechowaj ID do weryfikacji
                return jsonify({"mfa_required": True}), 200
            
            # Krok 2: Weryfikacja kodu MFA
            if session.get('mfa_user_id') != user_obj.id:
                return jsonify({"error": "Błąd sesji MFA"}), 401
            
            mfa_secret = decrypt_data(user_data['mfa_secret_encrypted'])
            if not pyotp.TOTP(mfa_secret).verify(mfa_code):
                # Spróbuj kodów zapasowych
                backup_codes_enc = user_data['mfa_backup_codes_encrypted']
                if not backup_codes_enc:
                    log_audit('login_fail_mfa', user_id=user_obj.id, username=username, ip_address=request.remote_addr)
                    return jsonify({"error": "Nieprawidłowy kod MFA"}), 401
                
                backup_codes = decrypt_data(backup_codes_enc).split(',')
                if mfa_code in backup_codes:
                    backup_codes.remove(mfa_code) # Użyty kod zapasowy jest usuwany
                    new_codes_enc = encrypt_data(','.join(backup_codes))
                    db.execute("UPDATE users SET mfa_backup_codes_encrypted = ? WHERE id = ?", (new_codes_enc, user_obj.id))
                    db.commit()
                    # Sukces (kod zapasowy)
                else:
                    log_audit('login_fail_mfa', user_id=user_obj.id, username=username, ip_address=request.remote_addr)
                    return jsonify({"error": "Nieprawidłowy kod MFA lub kod zapasowy"}), 401
            
            # Sukces (kod TOTP)
            login_user(user_obj)
            session.pop('mfa_user_id', None)
            log_audit('login_success', user_id=user_obj.id, username=username, ip_address=request.remote_addr)
            return jsonify({"success": True, "redirect": url_for('index')}), 200

        else:
            # Logowanie bez MFA
            login_user(user_obj)
            log_audit('login_success', user_id=user_obj.id, username=username, ip_address=request.remote_addr)
            return jsonify({"success": True, "redirect": url_for('index')}), 200

    # Metoda GET
    return render_template_string(open("index_v6.html", encoding="utf-8").read())

@app.route('/logout')
@login_required
def logout():
    log_audit('logout', user_id=current_user.id, username=current_user.username, ip_address=request.remote_addr)
    logout_user()
    return redirect(url_for('login'))

# === API: Pobieranie Danych ===

@app.route('/api/get_user_session')
@login_required
def get_user_session():
    """Zwraca dane o bieżącej sesji użytkownika."""
    return jsonify({
        "username": current_user.username,
        "role": current_user.role,
        "mfa_enabled": current_user.mfa_enabled
    })

@app.route('/api/get_dashboard_data')
@login_required
def get_dashboard_data():
    """Zwraca wszystkie dane potrzebne do zainicjowania panelu."""
    db = get_db()
    agents = [dict(row) for row in db.execute("SELECT id, name, platform, status, last_seen, alert_status FROM agents").fetchall()]
    custom_commands = [dict(row) for row in db.execute("SELECT id, name, description, platform FROM custom_commands").fetchall()]
    return jsonify({
        "agents": agents,
        "custom_commands": custom_commands
    })

# === API: Zarządzanie (Admin i Onboarding) ===

# --- Zarządzanie Agentami ---
@app.route('/api/admin/agents', methods=['POST'])
@login_required
@role_required('admin', 'onboarding_manager')
def create_agent():
    data = request.json
    name = data.get('name')
    platform = data.get('platform', 'linux')
    if not name:
        return jsonify({"error": "Brak nazwy (name)"}), 400
    
    agent_id = str(uuid.uuid4())
    api_key = str(uuid.uuid4())
    
    db = get_db()
    db.execute(
        "INSERT INTO agents (id, name, platform, api_key, created_by_user_id) VALUES (?, ?, ?, ?, ?)",
        (agent_id, name, platform, api_key, current_user.id)
    )
    db.commit()
    
    new_agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    
    # Wygeneruj token pobierania
    token_data = {'id': agent_id, 'key': api_key, 'platform': platform, 'ts': int(time.time())}
    download_token = token_serializer.dumps(token_data, salt=DOWNLOAD_TOKEN_SALT)
    
    log_audit('create_agent', user_id=current_user.id, ip_address=request.remote_addr, target_id=agent_id, details=f"Nazwa: {name}")
    
    return jsonify({
        "agent": dict(new_agent),
        "download_token": download_token
    }), 201

@app.route('/api/admin/agents/<agent_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_agent(agent_id):
    db = get_db()
    db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    db.execute("DELETE FROM commands WHERE agent_id = ?", (agent_id,)) # Usuń historię
    db.commit()
    log_audit('delete_agent', user_id=current_user.id, ip_address=request.remote_addr, target_id=agent_id)
    return jsonify({"success": True, "agent_id": agent_id})

@app.route('/download_agent/<token>')
def download_agent(token):
    try:
        # Sprawdź ważność tokenu (10 minut)
        token_data = token_serializer.loads(token, salt=DOWNLOAD_TOKEN_SALT, max_age=600)
    except BadSignature:
        return "Link wygasł lub jest nieprawidłowy.", 403
    
    agent_id = token_data['id']
    api_key = token_data['key']
    platform = token_data['platform']
    
    # Sprawdź, czy agent wciąż istnieje
    db = get_db()
    agent = db.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return "Agent został usunięty.", 404
        
    # Ustal host serwera
    server_host = request.host
    server_proto = 'http'
    ws_proto = 'ws'
    
    # Wypełnij szablon
    agent_content = AGENT_TEMPLATE_V6.format(
        agent_id=agent_id,
        api_key=api_key,
        server_proto=server_proto,
        ws_proto=ws_proto,
        server_host=server_host
    )
    
    filename = f"agent_{platform}_{agent['name'].replace(' ', '_')}.py"
    if platform == 'windows':
        # Dla Windows generujemy wrapper .ps1
        powershell_wrapper = f"""
# Ten skrypt zapisze i uruchomi agenta Python.
# Upewnij się, że Python 3 jest zainstalowany i dostępny w PATH.

$agentContent = @'
{agent_content}
'@

$installDir = "$env:ProgramData\\RMM_Agent"
if (-not (Test-Path $installDir)) {{
    New-Item -ItemType Directory -Path $installDir -Force
}}
$scriptPath = Join-Path $installDir "agent.py"
Set-Content -Path $scriptPath -Value $agentContent -Encoding UTF8 -Force

Write-Output "Agent zapisany w $scriptPath"
Write-Output "Uruchamiam agenta..."

# Uruchom agenta w nowym procesie
Start-Process "python" -ArgumentList "$scriptPath" -WindowStyle Hidden
"""
        filename = f"install_agent_{agent['name'].replace(' ', '_')}.ps1"
        response_content = powershell_wrapper
    else:
        response_content = agent_content

    # Utwórz odpowiedź do pobrania
    response = make_response(response_content)
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    response.headers['Content-Type'] = 'text/plain'
    
    log_audit('download_agent', ip_address=request.remote_addr, target_id=agent_id, details=f"Platforma: {platform}")
    
    return response

# --- Zarządzanie Użytkownikami (Feat. 7) ---
@app.route('/api/admin/users', methods=['GET'])
@login_required
@role_required('admin')
def get_users():
    db = get_db()
    users = [dict(row) for row in db.execute("SELECT id, username, role, email, created_at, mfa_enabled FROM users").fetchall()]
    return jsonify(users)

@app.route('/api/admin/users', methods=['POST'])
@login_required
@role_required('admin')
def create_user():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    role = data.get('role', 'viewer')
    email = data.get('email')
    
    if not username or not password:
        return jsonify({"error": "Brak nazwy użytkownika lub hasła"}), 400
        
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, role, email) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, email)
        )
        db.commit()
        log_audit('create_user', user_id=current_user.id, ip_address=request.remote_addr, target_type='user', details=f"Nowy użytkownik: {username}, Rola: {role}")
    except sqlite3.IntegrityError:
        return jsonify({"error": "Użytkownik o tej nazwie już istnieje"}), 409
        
    return jsonify({"success": True}), 201

@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_user(user_id):
    if user_id == current_user.id and data.get('role') != current_user.role:
        return jsonify({"error": "Nie możesz zmienić własnej roli"}), 403
        
    data = request.json
    role = data.get('role')
    email = data.get('email')
    password = data.get('password') # Do resetowania hasła
    
    db = get_db()
    updates = []
    params = []
    
    if role:
        updates.append("role = ?")
        params.append(role)
    if email:
        updates.append("email = ?")
        params.append(email)
    if password:
        updates.append("password_hash = ?")
        params.append(generate_password_hash(password))
        
    if not updates:
        return jsonify({"error": "Brak danych do aktualizacji"}), 400
        
    params.append(user_id)
    db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
    db.commit()
    
    log_audit('update_user', user_id=current_user.id, ip_address=request.remote_addr, target_type='user', target_id=user_id, details=f"Zaktualizowano: {', '.join(updates)}")
    return jsonify({"success": True})

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_user(user_id):
    if user_id == current_user.id:
        return jsonify({"error": "Nie możesz usunąć samego siebie"}), 403
    if user_id == 1: # Ochrona domyślnego admina
        return jsonify({"error": "Nie można usunąć domyślnego administratora"}), 403
        
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    
    log_audit('delete_user', user_id=current_user.id, ip_address=request.remote_addr, target_type='user', target_id=user_id)
    return jsonify({"success": True})

# --- Zarządzanie MFA (Feat. 11) ---
@app.route('/api/mfa/generate_setup', methods=['POST'])
@login_required
def generate_mfa_setup():
    """Generuje sekret i kod QR do konfiguracji MFA."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
    if user['mfa_enabled']:
        return jsonify({"error": "MFA jest już włączone"}), 400
        
    # Wygeneruj nowy sekret
    mfa_secret = pyotp.random_base32()
    
    # Zapisz zaszyfrowany sekret w sesji na czas konfiguracji
    session['mfa_setup_secret'] = encrypt_data(mfa_secret)
    
    # Wygeneruj URI dla Google Authenticator
    uri = pyotp.totp.TOTP(mfa_secret).provisioning_uri(
        name=current_user.username,
        issuer_name="RMM v6"
    )
    
    # Wygeneruj kod QR jako obraz Data URL
    qr_img = qrcode.make(uri)
    buffered = io.BytesIO()
    qr_img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    return jsonify({
        "qr_code_data_url": f"data:image/png;base64,{img_str}",
        "manual_setup_key": mfa_secret
    })

@app.route('/api/mfa/verify_setup', methods=['POST'])
@login_required
def verify_mfa_setup():
    """Weryfikuje kod TOTP i aktywuje MFA dla użytkownika."""
    data = request.json
    mfa_code = data.get('mfa_code')
    
    encrypted_secret = session.get('mfa_setup_secret')
    if not encrypted_secret:
        return jsonify({"error": "Sesja konfiguracji MFA wygasła"}), 400
        
    try:
        mfa_secret = decrypt_data(encrypted_secret)
    except Exception:
        return jsonify({"error": "Błąd deszyfrowania sekretu"}), 500
        
    if not pyotp.TOTP(mfa_secret).verify(mfa_code):
        return jsonify({"error": "Nieprawidłowy kod weryfikacyjny"}), 400
        
    # Aktywacja pomyślna. Generowanie kodów zapasowych.
    backup_codes = [f"{uuid.uuid4().hex[:5]}-{uuid.uuid4().hex[:5]}" for _ in range(5)]
    
    # Zapisz w bazie
    db = get_db()
    db.execute(
        "UPDATE users SET mfa_enabled = 1, mfa_secret_encrypted = ?, mfa_backup_codes_encrypted = ? WHERE id = ?",
        (encrypted_secret, encrypt_data(','.join(backup_codes)), current_user.id)
    )
    db.commit()
    
    # Wyczyść sesję
    session.pop('mfa_setup_secret', None)
    
    log_audit('mfa_enabled', user_id=current_user.id, ip_address=request.remote_addr)
    
    return jsonify({
        "success": True,
        "backup_codes": backup_codes
    })

@app.route('/api/mfa/disable', methods=['POST'])
@login_required
def disable_mfa():
    """Wyłącza MFA dla bieżącego użytkownika (wymaga hasła)."""
    data = request.json
    password = data.get('password')
    
    db = get_db()
    user = db.execute("SELECT password_hash FROM users WHERE id = ?", (current_user.id,)).fetchone()
    
    if not check_password_hash(user['password_hash'], password):
        log_audit('mfa_disable_fail', user_id=current_user.id, ip_address=request.remote_addr, details="Złe hasło")
        return jsonify({"error": "Nieprawidłowe hasło"}), 403
        
    db.execute(
        "UPDATE users SET mfa_enabled = 0, mfa_secret_encrypted = NULL, mfa_backup_codes_encrypted = NULL WHERE id = ?",
        (current_user.id,)
    )
    db.commit()
    
    log_audit('mfa_disabled', user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify({"success": True})

@app.route('/api/admin/mfa/disable_for_user', methods=['POST'])
@login_required
@role_required('admin')
def admin_disable_mfa_for_user():
    """Admin wyłącza MFA dla innego użytkownika (bez hasła)."""
    data = request.json
    user_id = data.get('user_id')
    
    db = get_db()
    db.execute(
        "UPDATE users SET mfa_enabled = 0, mfa_secret_encrypted = NULL, mfa_backup_codes_encrypted = NULL WHERE id = ?",
        (user_id,)
    )
    db.commit()
    
    log_audit('mfa_disabled_by_admin', user_id=current_user.id, ip_address=request.remote_addr, target_type='user', target_id=user_id)
    return jsonify({"success": True})

# --- Zarządzanie (Własne Komendy, Grupy, Alerty, Zadania) ---
# (Dodano uproszczone endpointy CRUD)

@app.route('/api/admin/custom_commands', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def manage_custom_commands():
    db = get_db()
    if request.method == 'GET':
        commands = [dict(row) for row in db.execute("SELECT * FROM custom_commands").fetchall()]
        return jsonify(commands)
    
    if request.method == 'POST':
        data = request.json
        db.execute(
            "INSERT INTO custom_commands (name, description, platform, script_content, created_by_user_id) VALUES (?, ?, ?, ?, ?)",
            (data['name'], data.get('description'), data.get('platform', 'any'), data['script_content'], current_user.id)
        )
        db.commit()
        return jsonify({"success": True}), 201

# ... (Podobnie uproszczone CRUD dla Grup, Alertów, Zadań) ...
# (Pominięto dla zwięzłości - logika byłaby identyczna jak dla /users i /custom_commands)

@app.route('/api/agents/get_custom_command/<name>')
@login_required(optional=True) # Dostęp dla agentów przez klucz API
def get_custom_command(name):
    """Zwraca treść skryptu dla Własnej Komendy. Używane przez agenta."""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({"error": "Brak klucza API"}), 401
        
    db = get_db()
    agent = db.execute("SELECT id FROM agents WHERE api_key = ?", (api_key,)).fetchone()
    if not agent:
        return jsonify({"error": "Nieprawidłowy klucz API"}), 401
        
    command = db.execute("SELECT script_content FROM custom_commands WHERE name = ?", (name,)).fetchone()
    if not command:
        return jsonify({"error": "Nie znaleziono komendy"}), 404
        
    return jsonify({"script_content": command['script_content']})
    
@app.route('/api/agents/get_self_update/<platform>')
@login_required(optional=True)
def get_self_update(platform):
    """Zwraca najnowszą wersję szablonu agenta."""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({"error": "Brak klucza API"}), 401
        
    db = get_db()
    agent = db.execute("SELECT id FROM agents WHERE api_key = ?", (api_key,)).fetchone()
    if not agent:
        return jsonify({"error": "Nieprawidłowy klucz API"}), 401
    
    # W produkcji, tutaj moglibyśmy znaleźć hosta na podstawie agent.id,
    # ale dla uproszczenia zakładamy, że agent łączy się z tym samym hostem.
    server_host = request.host
    server_proto = 'http'
    ws_proto = 'ws'
    
    agent_content = AGENT_TEMPLATE_V6.format(
        agent_id=agent['id'],
        api_key=api_key,
        server_proto=server_proto,
        ws_proto=ws_proto,
        server_host=server_host
    )
    return Response(agent_content, mimetype='text/plain')

# === Eventy WebSocket (SocketIO) ===

def check_agent_auth(data):
    """Funkcja pomocnicza do weryfikacji agenta w evencie SocketIO."""
    api_key = data.get('api_key')
    if not api_key:
        emit('auth_failed', {'message': 'Brak klucza API'})
        disconnect()
        return None
        
    db = get_db()
    agent = db.execute("SELECT * FROM agents WHERE api_key = ?", (api_key,)).fetchone()
    
    if agent:
        return agent
    else:
        emit('auth_failed', {'message': 'Nieprawidłowy klucz API'})
        disconnect()
        return None

@socketio.on('connect')
def handle_connect():
    print(f"[SERWER] Nowy klient połączony: {request.sid}")
    # Na razie nie wiemy, czy to panel webowy, czy agent

@socketio.on('disconnect')
def handle_disconnect():
    print(f"[SERWER] Klient rozłączony: {request.sid}")
    if request.sid in connected_agents:
        agent_id = connected_agents[request.sid]
        
        with app.app_context():
            db = get_db()
            db.execute("UPDATE agents SET status = 'offline', sid = NULL WHERE id = ?", (agent_id,))
            db.commit()
            
        del connected_agents[request.sid]
        if agent_id in agent_sids:
            del agent_sids[agent_id]
        
        print(f"[SERWER] Agent {agent_id} przeszedł w tryb offline.")
        socketio.emit('agent_status_update', {'id': agent_id, 'status': 'offline', 'sid': None})

@socketio.on('agent_authenticate')
def handle_agent_auth(data):
    """Agent łączy się i uwierzytelnia."""
    agent = check_agent_auth(data)
    if not agent:
        return # check_agent_auth już obsłużył rozłączenie
    
    agent_id = agent['id']
    sid = request.sid
    
    connected_agents[sid] = agent_id
    agent_sids[agent_id] = sid
    
    # Zaktualizuj bazę
    db = get_db()
    db.execute(
        "UPDATE agents SET status = 'online', last_seen = CURRENT_TIMESTAMP, sid = ?, agent_version = ?, agent_os_details = ? WHERE id = ?",
        (sid, data.get('agent_version'), data.get('agent_os_details'), agent_id)
    )
    db.commit()
    
    print(f"[SERWER] Pomyślne uwierzytelnienie agenta: {agent_id} (SID: {sid})")
    emit('auth_success', {'agent_id': agent_id})
    
    # Poinformuj wszystkie panele webowe
    full_agent_data = dict(db.execute("SELECT id, name, platform, status, last_seen, alert_status FROM agents WHERE id = ?", (agent_id,)).fetchone())
    socketio.emit('agent_status_update', full_agent_data)

@socketio.on('agent_metrics')
def handle_agent_metrics(data):
    """Agent przesyła metryki."""
    if request.sid not in connected_agents:
        return # Ignoruj nieautoryzowanych
    
    agent_id = connected_agents[request.sid]
    data['agent_id'] = agent_id
    
    # TODO: Logika sprawdzania alertów (Feat. 5)
    
    # Prześlij metryki do wszystkich paneli webowych
    socketio.emit('metrics_update', data)

@socketio.on('web_subscribe')
def handle_web_subscribe(data):
    """Panel webowy subskrybuje eventy (po zalogowaniu)."""
    # Sprawdzenie, czy użytkownik jest zalogowany w sesji Flaska
    if not current_user.is_authenticated:
        print(f"[SERWER] Niezalogowany klient (SID: {request.sid}) próbował subskrybować.")
        return
    
    # Dołącz do pokoju dla paneli (jeśli kiedyś będziemy chcieli rozdzielić)
    # join_room('web_clients')
    print(f"[SERWER] Zalogowany panel webowy (Użytkownik: {current_user.username}) subskrybuje eventy (SID: {request.sid})")
    # Wyślij potwierdzenie subskrypcji
    emit('web_subscribed')

@socketio.on('web_execute_command')
def handle_web_execute(data):
    """Panel webowy wysyła polecenie do agenta."""
    if not current_user.is_authenticated:
        return emit('command_error', {'message': 'Brak autoryzacji'})
    if not current_user.has_role('admin', 'command_sender'):
        return emit('command_error', {'message': 'Brak uprawnień'})
        
    agent_id = data.get('agent_id')
    command_type = data.get('command_type')
    command_payload = data.get('command_payload')
    
    if agent_id not in agent_sids:
        return emit('command_error', {'message': f'Agent {agent_id} jest offline.'})
        
    target_sid = agent_sids[agent_id]
    
    db = get_db()
    cursor = db.execute(
        "INSERT INTO commands (agent_id, user_id, command_type, command_payload, status) VALUES (?, ?, ?, ?, ?)",
        (agent_id, current_user.id, command_type, command_payload, 'sent')
    )
    db.commit()
    command_db_id = cursor.lastrowid
    
    # Logowanie audytu
    log_audit('execute_command', user_id=current_user.id, ip_address=request.remote_addr, 
              target_type='agent', target_id=agent_id, 
              details=f"Typ: {command_type}, Polecenie: {command_payload[:50]}...")
    
    # Wyślij do agenta
    socketio.emit(
        'execute_command', 
        {'command_type': command_type, 'command_payload': command_payload, 'command_db_id': command_db_id},
        room=target_sid
    )
    
    # Poinformuj UI, że polecenie zostało wysłane
    emit('command_sent', {'command_db_id': command_db_id, 'agent_id': agent_id})

@socketio.on('agent_command_result')
def handle_agent_result(data):
    """Agent odsyła wynik."""
    if request.sid not in connected_agents:
        return
    
    agent_id = connected_agents[request.sid]
    command_db_id = data.get('command_db_id')
    result = data.get('result')
    
    db = get_db()
    db.execute(
        "UPDATE commands SET result = ?, status = 'completed', timestamp = CURRENT_TIMESTAMP WHERE id = ?",
        (result, command_db_id)
    )
    db.commit()
    
    command_data = db.execute("SELECT * FROM commands WHERE id = ?", (command_db_id,)).fetchone()
    
    print(f"[SERWER] Otrzymano wynik od {agent_id} dla polecenia {command_db_id}")
    # Wyślij wynik do wszystkich paneli webowych
    socketio.emit('new_command_result', dict(command_data))

# --- Obsługa Terminala (Feat. 8 - Uproszczone) ---
@socketio.on('web_shell_pty_start')
@login_required
@role_required('admin', 'command_sender')
def handle_web_shell_start(data):
    agent_id = data.get('agent_id')
    if agent_id not in agent_sids:
        return emit('shell_pty_data', {'data': '\r\nBłąd: Agent jest offline.\r\n'})
    
    target_sid = agent_sids[agent_id]
    # Powiąż SID panelu z SID agenta na czas sesji terminala
    # TODO: W prawdziwym systemie potrzebna jest solidniejsza multipleksacja
    session[f'pty_agent_sid_for_{request.sid}'] = target_sid
    session[f'pty_web_sid_for_{target_sid}'] = request.sid
    
    log_audit('interactive_shell_start', user_id=current_user.id, ip_address=request.remote_addr, target_id=agent_id)
    socketio.emit('shell_pty_start', {}, room=target_sid)

@socketio.on('web_shell_pty_input')
@login_required
def handle_web_shell_input(data):
    target_sid = session.get(f'pty_agent_sid_for_{request.sid}')
    if not target_sid:
        return emit('shell_pty_data', {'data': '\r\nBłąd: Sesja terminala wygasła.\r\n'})
    
    socketio.emit('shell_pty_input', {'input': data.get('input')}, room=target_sid)

@socketio.on('shell_pty_data')
def handle_agent_shell_data(data):
    """Agent odsyła dane z terminala."""
    target_sid = session.get(f'pty_web_sid_for_{request.sid}')
    if not target_sid:
        return # Ten agent nie jest powiązany z żadnym panelem
        
    emit('shell_pty_data', {'data': data.get('data')}, room=target_sid)


# === Uruchomienie ===
if __name__ == '__main__':
    if not os.environ.get('APP_MASTER_KEY'):
        print("="*50)
        print("OSTRZEŻENIE: ZMIENNA ŚRODOWISKOWA 'APP_MASTER_KEY' NIE JEST USTAWIONA.")
        print("Funkcje MFA będą używać nietrwałego, tymczasowego klucza.")
        print("Ustaw ją poleceniem: export APP_MASTER_KEY='$(python3 -c \"import os; print(os.urandom(32).hex())\")'")
        print("="*50)

    init_db()
    # TODO: Wczytaj zaplanowane zadania z bazy i dodaj je do schedulera
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        
    print("[SERWER] Uruchamiam serwer Flask-SocketIO (w trybie testowym) na http://0.0.0.0:5000")
    # Używamy `debug=True` do testowania, ale wyłączamy go w `start-gunicorn.sh`
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
