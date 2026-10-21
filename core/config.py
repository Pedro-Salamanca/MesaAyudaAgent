import os
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env local
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

# =====================================================================
# CONFIGURACIÓN DEL MODELO DE INTELIGENCIA ARTIFICIAL (GEMINI)
# =====================================================================
MODEL_NAME = "gemini-3.5-flash-lite"
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

# =====================================================================
# CATEGORÍAS OFICIALES DE SOPORTE TÉCNICO SOMOS NETWORK
# =====================================================================
CATEGORIAS_OFICIALES = [
    "Soporte Capa 3",        # Enrutamiento BGP/OSPF, Core Routers, Tránsito IP, CGNAT, Prefijos IPv4/IPv6, Enlaces Troncales
    "Switch/Capa 2",         # Puerto Down, VLANs de cliente, Switches de acceso, Bucles STP, PoE
    "Router Secundario",     # DHCP, Doble NAT, Configuración WiFi, Puertos LAN equipos de cliente
    "Acometida/8 Hilos",     # Negociación a 10/100, Cable UTP degradado, Conector RJ45, Par abierto
    "Velocidad/Plan Pro",    # Perfil ancho de banda, Simple Queues MikroTik, Saturación de canal
    "Radioenlace",           # Antena desalineada, Pérdida de paquetes inalámbrica, CCQ bajo, Ruido RF
    "Soporte General"        # Validaciones de servicio y consultas técnicas con acción verificada
]

# =====================================================================
# REGLAS DE PRIVACIDAD, FILTRADO Y APLICACIONES PERMITIDAS
# =====================================================================
ARCHIVO_SITIOS_IGNORADOS = "sitios_ignorados.txt"

# Aplicaciones y herramientas de soporte que SIEMPRE se permite observar
# Incluye soporte Capa 1, Capa 2 y Capa 3 (Core & Routing) y hojas de cálculo técnicas
APPS_PERMITIDAS = [
    "winbox", "mobaxterm", "mobaxtreme", "putty", "powershell", 
    "cmd.exe", "símbolo del sistema", "terminal", "zabbix", 
    "somos", "artemis", "mikrotik", "grafana", "openwisp", "vilo", "selene",
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
# PARÁMETROS DEL CICLO DE OBSERVACIÓN Y BÚSQUEDA
# =====================================================================
INTERVALO_VISION_SEGUNDOS = 6
LONGITUD_MINIMA_OCR = 30
UMBRAL_SIMILITUD_BM25 = 0.1
MAX_CASOS_HISTORICOS_RAG = 2
MAX_CASOS_UI_MEMORIA = 100
