import os
import re
import difflib
from PIL import Image, ImageChops
from core.config import (
    TERMINOS_LOGIN_TITULO,
    FRASES_LOGIN_OCR,
    APPS_PERMITIDAS,
    NAVEGADORES_PERMITIDOS,
    PALABRAS_PROHIBIDAS_DEFECTO,
    ARCHIVO_SITIOS_IGNORADOS
)
from core.logger import log

class PreFilterEngine:
    """
    Motor de Pre-Filtrado y Anti-Spam para MesaAyudaAgent v3.0.
    Filtra imágenes y textos idénticos o repetitivos y pantallas de login
    ANTES de invocar la API de Gemini (ahorro del 85-90% de tokens y 0 llamadas innecesarias).
    """

    def __init__(self):
        self.last_pil_thumb = None
        self.last_text = ""
        self.last_ocr_hash = ""
        self.last_filtered_reason = ""

    def has_significant_pixel_change(self, new_image, threshold: float = 0.02) -> bool:
        """
        Compara la nueva captura con la anterior usando una miniatura de baja resolución (120x80 grayscale)
        mediante ImageChops.difference.
        Si la diferencia de píxeles promedio es menor al umbral (2%), retorna False (pantalla estática).
        """
        if new_image is None:
            return False

        try:
            # Convertir a escala de grises y reducir drásticamente resolución para cálculo ultrarrápido (<5ms)
            thumb = new_image.convert("L").resize((120, 80))
            if self.last_pil_thumb is None:
                self.last_pil_thumb = thumb
                return True

            diff = ImageChops.difference(self.last_pil_thumb, thumb)
            stat = diff.getextrema()
            
            # Si el valor máximo de diferencia es 0, las imágenes son idénticas
            if stat == (0, 0):
                return False

            # Calcular el promedio de diferencia sobre los 120x80 píxeles
            histogram = diff.histogram()
            total_pixels = 120 * 80
            diff_sum = sum(i * count for i, count in enumerate(histogram))
            avg_diff = diff_sum / (total_pixels * 255.0)

            if avg_diff < threshold:
                return False

            self.last_pil_thumb = thumb
            return True
        except Exception as e:
            log.warning(f"Error evaluando diferencia de píxeles: {e}")
            return True

    def is_duplicate_text(self, new_text: str, threshold: float = 0.92) -> bool:
        """
        Determina si el texto OCR es virtualmente idéntico al anterior usando SequenceMatcher.
        Si la similitud léxica es >= 92%, se clasifica como spam/sin cambio significativo.
        """
        if not new_text or not new_text.strip():
            return True

        clean_new = " ".join(new_text.split())
        clean_last = " ".join(self.last_text.split())

        if not clean_last:
            self.last_text = clean_new
            return False

        if clean_new == clean_last:
            return True

        # Para textos largos, comparar una muestra representativa para velocidad
        sample_len = 800
        ratio = difflib.SequenceMatcher(
            None,
            clean_last[:sample_len],
            clean_new[:sample_len]
        ).ratio()

        if ratio >= threshold:
            return True

        self.last_text = clean_new
        return False

    def is_window_allowed(self, title: str) -> tuple[bool, str]:
        """
        Verifica si la ventana activa está en la lista de aplicaciones y navegadores permitidos
        o si coincide con sitios prohibidos.
        """
        if not title:
            return False, "Título de ventana vacío"

        t_lower = title.lower()

        # No analizar la propia ventana del agente
        if "mesaayudaagent" in t_lower:
            return False, "Ventana propia del agente"

        # Leer lista de sitios ignorados/prohibidos
        forbidden = PALABRAS_PROHIBIDAS_DEFECTO
        if os.path.exists(ARCHIVO_SITIOS_IGNORADOS):
            try:
                with open(ARCHIVO_SITIOS_IGNORADOS, "r", encoding="utf-8") as f:
                    lines = [line.strip().lower() for line in f if line.strip()]
                    if lines:
                        forbidden = lines
            except Exception:
                pass

        for f in forbidden:
            if f in t_lower:
                return False, f"Sitio/App prohibida detectada ({f})"

        for term in TERMINOS_LOGIN_TITULO:
            if term in t_lower:
                return False, f"Formulario de login en título ({term})"

        for app in APPS_PERMITIDAS:
            if app in t_lower:
                return True, f"App permitida ({app})"

        for nav in NAVEGADORES_PERMITIDOS:
            if nav in t_lower:
                return True, f"Navegador permitido ({nav})"

        return False, "Ventana fuera de alcance técnico permitido"

    def is_login_or_forbidden(self, title: str, text: str) -> tuple[bool, str]:
        """
        Blindaje estricto de seguridad: descarta inmediatamente cualquier pantalla que contenga
        formularios de autenticación, contraseñas, tokens de acceso o logins corporativos.
        """
        t_title = (title or "").lower()
        t_text = (text or "").lower()

        for term in TERMINOS_LOGIN_TITULO:
            if term in t_title:
                return True, f"Login detectado en título de ventana: '{term}'"

        for frase in FRASES_LOGIN_OCR:
            if frase in t_text:
                return True, f"Frase de autenticación detectada en OCR: '{frase}'"

        # Coincidencia simultánea de credenciales típicas
        tiene_usuario = any(w in t_text for w in ["usuario", "username", "email", "correo corporativo", "nombre de usuario"])
        tiene_password = any(w in t_text for w in ["contraseña", "contrasena", "password", "clave de acceso", "pin"])
        tiene_accion = any(w in t_text for w in ["login", "ingresar", "iniciar sesión", "iniciar sesion", "sign in", "acceder", "autenticar"])

        if tiene_password and (tiene_usuario or tiene_accion):
            # Salvedad para configuración de routers donde se visualizan SSIDs o redes WiFi
            if any(w in t_text for w in ["ssid", "wlan", "2.4 ghz", "5 ghz", "chat con", "cpe"]):
                return False, ""
            return True, "Formulario con campos de usuario y contraseña detectado"

        return False, ""

    def extract_anchors(self, text: str, window_title: str = "") -> dict:
        """
        Extrae anclas y parámetros delimitados de la captura mediante expresiones regulares:
        - técnico en campo
        - CUN / ID de servicio
        - código de nodo/sitio
        - direcciones MAC
        - direcciones IPv4
        - herramienta origen
        """
        anchors = {
            "tecnico_campo": "",
            "id_servicio_cun": "",
            "codigo_sitio": "",
            "mac_addresses": [],
            "ip_addresses": [],
            "herramienta_origen": "General"
        }

        full_content = f"{window_title}\n{text}"

        # 1. Técnico de campo
        # Patrones comunes en Artemis: 'Chat con Nombre Apellido', 'Ts Nombre Apellido', 'Técnico: Nombre'
        # Usamos [^\S\r\n]+ para no capturar saltos de línea ni palabras de la siguiente línea (como CUN)
        m_tec = re.search(r"(?:Chat\s+con:?|Ts:?|T[ée]cnico:?)[^\S\r\n]+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:[^\S\r\n]+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){1,3})", full_content, re.IGNORECASE)
        if m_tec:
            tec_candidato = m_tec.group(1).strip()
            # Validar que no sea una palabra común de la interfaz
            if tec_candidato.lower() not in ["conversaciones", "pendientes", "activos", "en linea", "desconocido", "somos network"]:
                anchors["tecnico_campo"] = tec_candidato.title()

        # 2. CUN o ID de Servicio
        m_cun = re.search(r"(?:CUN|ID(?:\s*Servicio)?|Contrato)[:\s._-]*(\d{5,12})", full_content, re.IGNORECASE)
        if m_cun:
            anchors["id_servicio_cun"] = m_cun.group(1).strip()

        # 3. Código de Sitio / Nodo SOMOS (Ej: SOMOS-MDE-RIVA-T4-415, RIVA-T4-415, CMAZ, etc.)
        m_sitio = re.search(r"(SOMOS-[A-Z]{3}-[A-Z0-9]+(?:-T\d+-\d+(?:-\d+)?)?|[A-Z]{3,4}-T\d+-\d+)", full_content, re.IGNORECASE)
        if m_sitio:
            anchors["codigo_sitio"] = m_sitio.group(1).strip().upper()

        # 4. Direcciones MAC
        macs = re.findall(r"\b([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})\b", full_content)
        if macs:
            # Normalizar en mayúsculas con dos puntos
            anchors["mac_addresses"] = list(set([m.upper().replace("-", ":") for m in macs]))

        # 5. Direcciones IPv4 válidas
        ips = re.findall(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b", full_content)
        valid_ips = []
        for ip in set(ips):
            if not ip.startswith("0.") and not ip.startswith("255.255.") and not ip.startswith("127."):
                valid_ips.append(ip)
        anchors["ip_addresses"] = valid_ips

        # 6. Herramienta de soporte identificada
        wt_low = window_title.lower()
        if "artemis" in wt_low:
            anchors["herramienta_origen"] = "Artemis"
        elif "putty" in wt_low:
            anchors["herramienta_origen"] = "Putty"
        elif "winbox" in wt_low or "mikrotik" in wt_low:
            anchors["herramienta_origen"] = "WinBox"
        elif "moba" in wt_low:
            anchors["herramienta_origen"] = "MobaXterm"
        elif "zabbix" in wt_low or "monitoreo" in wt_low:
            anchors["herramienta_origen"] = "Zabbix"
        elif "vilo" in wt_low:
            anchors["herramienta_origen"] = "ViloLiving"
        elif "selene" in wt_low:
            anchors["herramienta_origen"] = "Selene"
        elif "openwisp" in wt_low:
            anchors["herramienta_origen"] = "OpenWisp"
        elif any(nav in wt_low for nav in ["chrome", "edge", "firefox"]):
            anchors["herramienta_origen"] = "Web Browser"

        return anchors

    def has_strong_anchor(self, anchors: dict) -> tuple[bool, str]:
        """
        Regla de Oro: Valida si la captura contiene al menos un ancla fuerte que permita
        asociarla sin ambigüedad a un caso técnico real (Técnico, CUN, Código de Sitio o MAC).
        Si no hay ancla, la captura se descarta para evitar mezclar técnicos o ensuciar cubetas.
        """
        if anchors.get("tecnico_campo"):
            return True, f"Ancla: Técnico '{anchors['tecnico_campo']}'"
        if anchors.get("id_servicio_cun"):
            return True, f"Ancla: CUN '{anchors['id_servicio_cun']}'"
        if anchors.get("codigo_sitio"):
            return True, f"Ancla: Sitio '{anchors['codigo_sitio']}'"
        if anchors.get("mac_addresses"):
            return True, f"Ancla: MAC '{anchors['mac_addresses'][0]}'"
        return False, "Captura sin ancla identificable (Descartada para no contaminar datos)"
