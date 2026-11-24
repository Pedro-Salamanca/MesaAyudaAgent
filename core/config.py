import os
import sys
from dotenv import load_dotenv

# Directorio raíz del proyecto activo o bundle PyInstaller (_MEIPASS / carpeta del .exe)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else BASE_DIR

# Cargar variables de entorno buscando en carpeta del .exe, padre del .exe, BASE_DIR y cwd
for _env_candidate in [
    os.path.join(EXE_DIR, ".env"),
    os.path.join(os.path.dirname(EXE_DIR), ".env"),
    os.path.join(BASE_DIR, ".env"),
    os.path.join(os.getcwd(), ".env")
]:
    if os.path.exists(_env_candidate):
        load_dotenv(_env_candidate)
load_dotenv()

# =====================================================================
# CONFIGURACIÓN DE MEMORIA Y ALMACENAMIENTO COMPARTIDO (GOOGLE DRIVE)
# =====================================================================
MEMORY_DIR = os.getenv("MEMORY_PATH", r"G:\Mi unidad\MesaAyuda_Memoria")
CASES_DIR = os.path.join(MEMORY_DIR, "Cases")
IMAGES_DIR = os.path.join(MEMORY_DIR, "Images")
MANUALS_DIR = os.path.join(MEMORY_DIR, "Manuales_Base")

# Nombre o alias del técnico que opera este agente
TECNICO_NOMBRE_DEFAULT = os.getenv("TECNICO_NOMBRE", "Técnico")

# Identificador único de aislamiento por PC y Operador (100% aislado entre compañeros)
_pc_name = os.environ.get("COMPUTERNAME", "PC")
_win_user = os.environ.get("USERNAME", "USER")
_raw_slug = f"{_pc_name}_{_win_user}_{TECNICO_NOMBRE_DEFAULT}"
OPERADOR_PC_SLUG = "".join(c if c.isalnum() or c == "_" else "_" for c in _raw_slug)

# Espejo local de lectura rápida y bases de estado locales en SSD (%LOCALAPPDATA%\MesaAyudaAgent\)
# Garantiza CERO interferencia entre operadores que comparten Google Drive
LOCAL_APPDATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.join(BASE_DIR, "data")),
    "MesaAyudaAgent"
)
LOCAL_CACHE_DB_PATH = os.path.join(LOCAL_APPDATA_DIR, "cache_v4.db")
LOCAL_STAGING_DB_PATH = os.path.join(LOCAL_APPDATA_DIR, f"staging_local_{OPERADOR_PC_SLUG}.db")
LOCAL_CHECKPOINT_PATH = os.path.join(LOCAL_APPDATA_DIR, f"active_buckets_{OPERADOR_PC_SLUG}.json")

# Ruta del catálogo determinista de árboles de diagnóstico v4.0
_arbol_candidates = [
    os.path.join(EXE_DIR, "data", "arboles_diagnostico.json"),
    os.path.join(os.path.dirname(EXE_DIR), "data", "arboles_diagnostico.json"),
    os.path.join(BASE_DIR, "data", "arboles_diagnostico.json"),
    os.path.join(os.getcwd(), "data", "arboles_diagnostico.json")
]
ARBOLES_DIAGNOSTICO_PATH = next((p for p in _arbol_candidates if os.path.exists(p)), _arbol_candidates[2])

# =====================================================================
# CONFIGURACIÓN DEL MODELO DE INTELIGENCIA ARTIFICIAL (GEMINI)
# =====================================================================
MODEL_NAME = "gemini-3.5-flash-lite"
FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
]
API_HTTP_TIMEOUT_MS = 20000
API_HTTP_RETRY_ATTEMPTS = 1
COOLDOWN_429_DEFAULT_S = 50
COOLDOWN_503_DEFAULT_S = 6
TEMPERATURE_ANALISIS = 0.1
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# =====================================================================
# CONFIGURACIÓN DEL MOTOR OCR (TESSERACT)
# =====================================================================
TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe")
]
TESSERACT_INSTALLER_URL = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
TESSERACT_FAST_CONFIG = "--oem 1 --psm 6"

# =====================================================================
# CATEGORÍAS OFICIALES DE SOPORTE TÉCNICO SOMOS NETWORK
# (Regla SOMOS: CPE aplica a TODOS los routers de cliente; WinBox solo DHCP Leases)
# =====================================================================
CATEGORIAS_OFICIALES = [
    "Soporte Capa 3",        # DHCP Leases en WinBox CHR (Remove Lease), Estado Offered, Cambio IP, Fallas Masivas NOC
    "Switch/Capa 2",         # Switches de piso, Cascadas en montante, Pre-registro MAC, Puerto Down, VLANs, PoE, Temperatura
    "Router Secundario",     # CPEs (Principal/Secundario/Mesh), Plantillas OpenWISP (2026 + VPN2), Vilo (E8:), LuCI, SSID/WiFi
    "Acometida/8 Hilos",     # Negociación física a 10/100 Mbps vs 1G, Cable UTP 8 hilos, Conector RJ45 sulfatado, Par abierto
    "Velocidad/Plan Pro",    # Velocidad degradada (~90M en planes 900M/2G), Bloqueo Speedtest Artemis, Autenticación RADIUS
    "Radioenlace",           # Enlaces inalámbricos exteriores, Antenas, Alineación RF, CCQ bajo, Ruido RF
    "Soporte General"        # Historial de caídas en Grafana (Subsite Validate) y validaciones generales con acción verificada
]

# Mapeo directo Categoría Oficial -> Manuales Base v2.0 (Inyección selectiva: -75% a -83% tokens)
CATEGORIA_A_MANUALES = {
    "Router Secundario": ["protocolo_openwisp_phantom_orb", "protocolo_vilo_inventory"],
    "Switch/Capa 2": ["protocolo_switches_montante_poe", "protocolo_zabbix_monitoreo"],
    "Acometida/8 Hilos": ["checklist_validaciones_sitio"],
    "Velocidad/Plan Pro": ["protocolo_aprovisionamiento_radius_artemis", "checklist_validaciones_sitio"],
    "Soporte Capa 3": ["protocolo_winbox_gestion_chr", "protocolo_zabbix_monitoreo"],
    "Radioenlace": ["protocolo_aprovisionamiento_radius_artemis", "checklist_validaciones_sitio"],
    "Soporte General": ["protocolo_grafana_monitoreo", "checklist_validaciones_sitio"]
}

# =====================================================================
# MODOS OPERATIVOS v4.0 (TRI-MODO)
# =====================================================================
MODO_APRENDIZAJE = "learning"          # 🎓 Solo Aprendizaje (0 inferencias en vivo -> Tarjetas)
MODO_GUIA_CONTINUA = "continuous"      # 🧭 Guía Continua (Copiloto automático en hitos técnicos)
MODO_SILENCIOSO_ASISTIR = "on_demand"  # ⚡ Silencioso + Asistir (Encubetado continuo + Botón/Atajo Asistir)

# =====================================================================
# REGLAS DE PRIVACIDAD, FILTRADO Y APLICACIONES PERMITIDAS
# =====================================================================
ARCHIVO_SITIOS_IGNORADOS = os.path.join(BASE_DIR, "sitios_ignorados.txt")

# Aplicaciones y herramientas de soporte que SIEMPRE se permite observar
APPS_PERMITIDAS = [
    "winbox", "mobaxterm", "mobaxtreme", "putty", "powershell",
    "cmd.exe", "símbolo del sistema", "terminal", "zabbix",
    "somos", "artemis", "mikrotik", "grafana", "openwisp", "vilo", "selene", "luci",
    "bgp", "ospf", "core", "traceroute", "mtr", "sheets", "google sheets", "spreadsheet"
]

# Navegadores web permitidos para plataformas internas
NAVEGADORES_PERMITIDOS = ["chrome", "edge", "firefox", "brave", "opera"]

# Lista de reserva en caso de que sitios_ignorados.txt sea eliminado
PALABRAS_PROHIBIDAS_DEFECTO = [
    "whatsapp", "telegram", "slack", "mesaayudaagent", "agentemesaayuda",
    "mesa de ayuda", "evaluar aprendizaje", "observación y cubetas",
    "discord", "messenger",
    "facebook", "youtube", "twitter", "instagram", "netflix",
    "spotify", "tiktok", "login", "iniciar sesión", "iniciar sesion",
    "sign in", "signin", "autenticación", "autenticacion", "accounts.google"
]

# Términos en el título de la ventana que indican un formulario de autenticación/login
TERMINOS_LOGIN_TITULO = [
    "login", "log in", "iniciar sesión", "iniciar sesion", "inicio de sesión",
    "inicio de sesion", "sign in", "signin", "autenticación", "autenticacion",
    "acceso al sistema", "accounts.google", "portal cautivo"
]

# Frases y combinaciones en el texto OCR que identifican formularios de inicio de sesión
FRASES_LOGIN_OCR = [
    "iniciar sesión", "iniciar sesion", "inicio de sesión", "inicio de sesion",
    "sign in", "enter your password", "ingrese su contraseña",
    "ingrese su contrasena", "olvidó su contraseña", "olvido su contrasena",
    "olvidaste tu contraseña", "olvidaste tu contrasena", "forgot password",
    "two-factor authentication", "código de verificación", "codigo de verificacion",
    "recordar usuario y contraseña", "nombre de usuario o correo",
    "ingresa tus credenciales", "inicie sesión con su cuenta"
]

# Líneas y palabras clave de notificaciones emergentes a censurar de la captura
CENSURA_NOTIFICACIONES = ["whatsapp", "telegram", "slack", "nuevo mensaje"]

# =====================================================================
# PARÁMETROS DEL CICLO DE OBSERVACIÓN, DEBOUNCE 3.5s Y BÚSQUEDA v4.0
# =====================================================================
INTERVALO_VISION_SEGUNDOS = 6            # Mantenido por retrocompatibilidad con scripts de test
INTERVALO_POLL_LIGERO_SEGUNDOS = 1.0     # Chequeo ligero de título + miniatura 120x80 (3.5 ms, 0% OCR)
DEBOUNCE_ESTABILIDAD_SEGUNDOS = 3.5      # Delay aprobado: 3.5s de pantalla quieta antes de disparar OCR
MAX_DEBOUNCE_CEILING_SEGUNDOS = 8.0      # Techo máximo si el operador interactúa continuamente sin pausa
MIN_FLUSH_TAB_SWITCH_SEGUNDOS = 1.5      # Si vio una pantalla >=1.5s con cambios y salta de pestaña, procesa el frame
UMBRAL_PIXEL_GLOBAL = 0.0025             # 0.25% de cambio global
UMBRAL_PIXEL_CUADRANTE = 0.0035          # 0.35% de cambio en cualquiera de los 16 cuadrantes (4x4)
LONGITUD_MINIMA_OCR = 30
UMBRAL_SIMILITUD_BM25 = 0.1
MAX_CASOS_HISTORICOS_RAG = 3
MAX_CASOS_UI_MEMORIA = 100
