import os
import re
import difflib
from typing import Optional, Tuple
from PIL import Image, ImageChops
from core.config import (
    TERMINOS_LOGIN_TITULO,
    FRASES_LOGIN_OCR,
    APPS_PERMITIDAS,
    NAVEGADORES_PERMITIDOS,
    PALABRAS_PROHIBIDAS_DEFECTO,
    ARCHIVO_SITIOS_IGNORADOS,
    UMBRAL_PIXEL_GLOBAL,
    UMBRAL_PIXEL_CUADRANTE
)
from core.logger import log


class PreFilterEngine:
    """
    Motor de Pre-Filtrado, Detección por Cuadrantes 4x4, Extracción de Anclas y
    Pre-Clasificación Heurística (<0.5 ms) para MesaAyudaAgent v4.0.
    """

    def __init__(self):
        self.last_pil_thumb: Optional[Image.Image] = None
        self.last_text = ""
        self.last_ocr_hash = ""
        self.last_filtered_reason = ""

    @staticmethod
    def make_thumb(image: Optional[Image.Image]) -> Optional[Image.Image]:
        """Genera miniatura 120x80 en escala de grises con filtro BILINEAR (<2 ms) preservando trazos de texto nuevos."""
        if image is None:
            return None
        try:
            return image.convert("L").resize((120, 80), Image.Resampling.BILINEAR)
        except Exception:
            return image.convert("L").resize((120, 80))

    @staticmethod
    def compare_thumbs(
        thumb_a: Optional[Image.Image],
        thumb_b: Optional[Image.Image],
        global_threshold: float = UMBRAL_PIXEL_GLOBAL,
        zone_threshold: float = UMBRAL_PIXEL_CUADRANTE
    ) -> bool:
        """
        Compara dos miniaturas 120x80 combinando:
        1. Diferencia global promedio (>= 0.25%).
        2. Diferencia localizada en cuadrícula 4x4 (16 sub-zonas de 30x20 píxeles):
           Si CUALQUIERA de las 16 zonas cambia >= 0.35% o presenta >= 10 píxeles con variación > 12 niveles
           de gris (ignorando parpadeo de cursor de 1-3 píxeles), retorna True.
        Esto garantiza que incluso una sola línea de texto nueva dentro del mismo chat de Selene/Artemis
        o la edición de un parámetro en OpenWISP (bajo el mismo título de pestaña) sea detectada al 100%.
        """
        if thumb_a is None or thumb_b is None:
            return True

        try:
            diff = ImageChops.difference(thumb_a, thumb_b)
            stat = diff.getextrema()
            if stat == (0, 0):
                return False

            # 1. Evaluación global
            histogram = diff.histogram()
            total_pixels = 120 * 80
            diff_sum = sum(i * count for i, count in enumerate(histogram))
            avg_diff = diff_sum / (total_pixels * 255.0)
            if avg_diff >= global_threshold:
                return True

            # 2. Evaluación por cuadrantes 4x4 (16 zonas de 30x20 px = 600 px por zona)
            zone_w, zone_h = 30, 20
            zone_pixels = zone_w * zone_h * 255.0
            for gx in range(4):
                for gy in range(4):
                    box = (gx * zone_w, gy * zone_h, (gx + 1) * zone_w, (gy + 1) * zone_h)
                    z_hist = diff.crop(box).histogram()
                    z_sum = sum(i * count for i, count in enumerate(z_hist))
                    if (z_sum / zone_pixels) >= zone_threshold:
                        return True
                    # Contar píxeles con cambio perceptible (>12 niveles de gris) en este cuadrante
                    active_px = sum(z_hist[12:])
                    if active_px >= 10:
                        return True

            return False
        except Exception as e:
            log.warning(f"Error comparando miniaturas por cuadrantes: {e}")
            return True

    def has_significant_pixel_change(
        self,
        new_image: Optional[Image.Image],
        threshold: float = UMBRAL_PIXEL_GLOBAL,
        zone_threshold: float = UMBRAL_PIXEL_CUADRANTE
    ) -> bool:
        """
        Evalúa si `new_image` cambió significativamente respecto a la última imagen de referencia
        usando detección global + cuadrícula 4x4.
        """
        if new_image is None:
            return False

        thumb = self.make_thumb(new_image)
        if thumb is None:
            return False

        if self.last_pil_thumb is None:
            self.last_pil_thumb = thumb
            return True

        changed = self.compare_thumbs(
            self.last_pil_thumb,
            thumb,
            global_threshold=threshold,
            zone_threshold=zone_threshold
        )
        if changed:
            self.last_pil_thumb = thumb
        return changed

    def is_duplicate_text(self, new_text: str, threshold: float = 0.92) -> bool:
        """
        Determina si el texto OCR es virtualmente idéntico al anterior usando SequenceMatcher.
        Si la similitud léxica es >= 92%, se clasifica como sin cambio significativo.
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
        o si coincide con sitios prohibidos o con la propia ventana del agente.
        """
        if not title:
            return False, "Título de ventana vacío"

        t_lower = title.lower()

        AGENT_WINDOW_KEYWORDS = [
            "mesa de ayuda", "mesaayudaagent", "agentemesaayuda",
            "evaluar aprendizaje", "observación y cubetas", "memoria oficial",
            "agente mesa", "the eye", "cubetas activas"
        ]
        if any(kw in t_lower for kw in AGENT_WINDOW_KEYWORDS):
            return False, "Ventana propia del agente (Mesa de Ayuda)"

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

        tiene_usuario = any(w in t_text for w in ["usuario", "username", "email", "correo corporativo", "nombre de usuario"])
        tiene_password = any(w in t_text for w in ["contraseña", "contrasena", "password", "clave de acceso", "pin"])
        tiene_accion = any(w in t_text for w in ["login", "ingresar", "iniciar sesión", "iniciar sesion", "sign in", "acceder", "autenticar"])

        if tiene_password and (tiene_usuario or tiene_accion):
            if any(w in t_text for w in ["ssid", "wlan", "2.4 ghz", "5 ghz", "chat con", "cpe"]):
                return False, ""
            return True, "Formulario con campos de usuario y contraseña detectado"

        return False, ""

    @staticmethod
    def _strip_browser_chrome_lines(text: str) -> str:
        """
        Elimina líneas residuales de la barra de pestañas y marcadores del navegador Chrome/Edge
        para evitar que nombres de pestañas inactivas de otros clientes envenenen la extracción de anclas.
        Conserva la barra de URL (admin.somosinternet.com / isp.somosinternet.com / luci) y todo el cuerpo.
        """
        if not text:
            return ""
        cleaned_lines = []
        for idx, line in enumerate(text.splitlines()):
            l_low = line.lower()
            # En las primeras 5 líneas del OCR, descartar barra de pestañas (múltiples 'x' o 'pregunta a gemini')
            # y barra de marcadores corporativos ('sites', 'templates', 'trazabilidad', 'soliequipos')
            if idx < 5:
                if "pregunta a gemini" in l_low:
                    continue
                if l_low.count(" x ") >= 2 and ("somos" in l_low or "[rb]" in l_low or "[b]" in l_low):
                    continue
                bookmarks_hits = sum(
                    1 for kw in ("sites", "templates", "trazabilidad", "soliequipos", "topologias", "finanzas", "validacion")
                    if kw in l_low
                )
                if bookmarks_hits >= 3:
                    continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines)

    @staticmethod
    def _clean_and_validate_technician(raw_candidate: str) -> str:
        """
        Limpia y valida estrictamente un nombre candidato de técnico en campo:
        - Corta inmediatamente antes de botones/etiquetas de la UI de Selene ('Información del técnico', 'Tickets', 'Notas').
        - Rechaza frases de prosa técnica o descripciones ('Referente A Internet O Tv', 'En Sitio Reporta', etc.).
        """
        if not raw_candidate:
            return ""

        import unicodedata

        # 1. Truncar inmediatamente antes de etiquetas de UI en la misma línea de Selene/Artemis
        # (Tolera corrupciones OCR como 'Informacién', 'Informaci6n', 'Información del técnico v')
        cut = re.split(
            r"\b(?:informac\w*|del\s+t[eé]cnic\w*|tickets?|notas?|conversaciones?|"
            r"meta\s+user|est[aá]s\s+conectado|cola|estado|ver\s+perfil|cerrar|pendientes|activos|en\s+l[ií]nea)\b",
            raw_candidate,
            maxsplit=1,
            flags=re.IGNORECASE
        )[0].strip()

        # 2. Quitar prefijos redundantes ('TS ', 'Técnico ', 'Chat con ') y flechitas de dropdown (' v')
        cut = re.sub(r"^(?:ts|tec|t[eé]cnico|chat\s+con)\b[:\s]*", "", cut, flags=re.IGNORECASE).strip()
        cut = re.sub(r"\s+[vx>|-]$", "", cut, flags=re.IGNORECASE).strip()

        words = [w for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+", cut) if w]
        if not words or len(words) > 4:
            return ""

        # 3. Palabras de prosa, conectores o jerga técnica que JAMÁS forman parte del nombre de una persona
        forbidden_tokens = {
            "referente", "internet", "tv", "television", "soporte", "servicio", "servicios",
            "informacion", "informacien", "tecnico", "tecnicos", "reporta", "confirma", "indica",
            "solicita", "valida", "realiza", "presenta", "requiere", "necesita", "cliente",
            "usuario", "suscriptor", "equipo", "equipos", "router", "switch", "puerto", "puertos",
            "cable", "fibra", "falla", "problema", "cambio", "instalacion", "visita", "asignado",
            "asignada", "conectado", "cola", "mesa", "ayuda", "tickets", "ticket", "notas",
            "conversaciones", "pendientes", "activos", "desconocido", "somos", "network",
            "operador", "sistema", "disponible", "available", "total", "para", "por", "con",
            "sin", "sobre", "hacia", "desde", "como", "que", "cual", "donde", "cuando", "pero",
            "porque", "esta", "este", "esto", "estos", "estas", "uno", "una", "unos", "unas",
            "los", "las", "al", "en", "se", "su", "sus", "no", "si", "ya", "muy", "mas", "solo",
            "todo", "todos", "otro", "otra", "aqui", "alli", "ahora", "luego", "antes", "despues",
            "favor", "gracias", "hola", "buenas", "buenos", "dias", "tardes", "noches", "compa",
            "companero", "dale", "listo", "ok", "bien", "mal", "igual", "nada", "algo", "puede",
            "puedo", "tiene", "tengo", "hay", "ver", "revisar", "validar", "probar", "conectar",
            "desconectar", "reiniciar", "apagar", "prender", "cpe", "orb", "vilo", "phantom",
            "poe", "wan", "lan", "wifi", "dhcp", "ip", "mac", "edge", "openwisp", "luci",
            "zabbix", "grafana", "winbox", "artemis", "selene", "linfo", "info", "warning",
            "error", "cubeta", "chaty", "ar", "mts", "mib", "mbps", "gbps", "a", "o", "y", "e", "u"
        }

        norm_words = []
        for w in words:
            nfkd = unicodedata.normalize("NFKD", w)
            w_ascii = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
            if w_ascii in forbidden_tokens:
                return ""
            norm_words.append(w_ascii)

        # Permitir preposiciones de apellido ('de', 'del', 'la') solo si están acompañadas de nombres reales
        core_words = [w for w in norm_words if w not in {"de", "del", "la"} and len(w) >= 3]
        if not core_words:
            return ""

        full_norm = " ".join(norm_words)
        if full_norm in {"carlos gemez", "carlos gomez prueba", "mesa ayuda"}:
            return ""

        return " ".join(words).title()

    def extract_anchors(self, text: str, window_title: str = "") -> dict:
        """
        Extrae anclas y parámetros delimitados de la captura mediante expresiones regulares:
        - técnico en campo (solo encabezado 'Chat con ...', etiqueta 'Técnico: ...' o 'TS Nombre Propio')
        - CUN / ID de servicio / ID de Visita explícitamente etiquetado
        - código de nodo/sitio (libre de pestañas inactivas de Chrome y normalizado contra ruido OCR)
        - direcciones MAC
        - direcciones IPv4
        - herramienta origen (distinguiendo estrictamente OpenWISP/LuCI de Artemis/Selene)
        """
        anchors = {
            "tecnico_campo": "",
            "id_servicio_cun": "",
            "codigo_sitio": "",
            "mac_addresses": [],
            "ip_addresses": [],
            "herramienta_origen": "General"
        }

        clean_text = self._strip_browser_chrome_lines(text or "")
        full_content = f"{window_title}\n{clean_text}"

        # 1. Técnico de campo (Extracción priorizada y estricta para cero falsos positivos de prosa)
        tec_patterns = [
            # Prioridad 1: Encabezado oficial del chat activo en Selene/Artemis ('Chat con Fander Estrada Herrera Información del técnico')
            (r"\bChat\s+con\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:[^\S\r\n]+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,6})", re.IGNORECASE),
            # Prioridad 2: Etiqueta explícita con dos puntos ('Técnico: Diego Leon Hinestroza Escobar')
            (r"\bT[ée]cnico\s*:\s*([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:[^\S\r\n]+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,4})", re.IGNORECASE),
            # Prioridad 3: Prefijo TS/Ts seguido ESTRICTAMENTE de Nombre Propio capitalizado ('Ts Nicolas Emerson Cano')
            (r"\bT[Ss]\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}(?:[^\S\r\n]+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,}){1,3})", 0),
        ]
        for pat, flags in tec_patterns:
            for m_tec in re.finditer(pat, full_content, flags):
                validated = self._clean_and_validate_technician(m_tec.group(1))
                if validated:
                    anchors["tecnico_campo"] = validated
                    break
            if anchors["tecnico_campo"]:
                break

        # 2. CUN, Radicado o ID de Visita/Appointment explícitamente etiquetado
        # (Evita capturar fragmentos de hostnames como -11-9603-1 o números sueltos de contadores)
        m_cun = re.search(
            r"\b(?:CUN|Radicado|ID\s*Servicio|Contrato|Appointment(?:\s+contd)?|Cita|Visita|Orden|ID|Id)[:\s.#]+([0-9]{4,6}(?:-[0-9]{1,6})+(?:-[0-9]{2,12})?|\d{5,10})\b",
            full_content,
            re.IGNORECASE
        )
        if m_cun:
            anchors["id_servicio_cun"] = m_cun.group(1).strip()

        # 3. Código de Sitio / Nodo SOMOS con normalización de ruido OCR
        # Prioriza coincidencia en título activo o cuerpo limpio (jamás pestañas inactivas)
        m_sitio = re.search(r"(SOMOS-[A-Z]{3}-[A-Z0-9]+(?:-[A-Z0-9]+)*|[A-Z0-9]{3,4}-T\d+-\d{3,4}(?:-\d+)?)", full_content, re.IGNORECASE)
        if m_sitio:
            raw_site = m_sitio.group(1).strip().upper()
            # Normalizar confusiones OCR clásicas: SOMOS-STA- -> SOMOS-BTA-, -11- -> -T1-, -TI -> -T1
            raw_site = re.sub(r"^SOMOS-STA-", "SOMOS-BTA-", raw_site)
            raw_site = re.sub(r"^(SOMOS-[A-Z]{3}-[A-Z0-9]{3,5})-1(\d)(-\d{3,4}(?:-[A-Z0-9]+)*)$", r"\1-T\2\3", raw_site)
            raw_site = re.sub(r"^(SOMOS-[A-Z]{3}-[A-Z0-9]{3,5})-TI(?=-|\b)", r"\1-T1", raw_site)
            anchors["codigo_sitio"] = raw_site

        # 4. Direcciones MAC (con separadores)
        macs = re.findall(r"\b([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})\b", full_content)
        if macs:
            anchors["mac_addresses"] = list(set([m.upper().replace("-", ":") for m in macs]))

        # 5. Direcciones IPv4 válidas
        ips = re.findall(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b", full_content)
        valid_ips = []
        for ip in set(ips):
            if not ip.startswith("0.") and not ip.startswith("255.255.") and not ip.startswith("127."):
                valid_ips.append(ip)
        anchors["ip_addresses"] = valid_ips

        # 6. Herramienta de soporte identificada (OpenWISP/LuCI evaluado ANTES de Artemis para no confundir isp.somosinternet.com)
        fc_low = full_content.lower()
        wt_low = (window_title or "").lower()
        if any(k in wt_low for k in ["openwisp", "luci", "change device"]) or any(k in fc_low for k in ["isp.somosinternet.com", "ispsomosinternet.com", "cgi-bin/luci", "openwisp", "openwrt"]):
            anchors["herramienta_origen"] = "OpenWisp"
        elif any(k in wt_low for k in ["selene", "conversations", "artemis"]) or any(k in fc_low for k in ["admin.somosinternet.com", "adminsomosintemet.com", "conversation_queues", "mesa de ayuda técnicos", "cola asignada", "artemis"]):
            anchors["herramienta_origen"] = "Selene" if "selene" in wt_low or "conversations" in wt_low or "conversation_queues" in fc_low else "Artemis"
        elif "putty" in wt_low or "speedtest" in fc_low:
            anchors["herramienta_origen"] = "Putty"
        elif "winbox" in wt_low or "mikrotik" in fc_low or "dhcp server" in fc_low:
            anchors["herramienta_origen"] = "WinBox"
        elif "moba" in wt_low:
            anchors["herramienta_origen"] = "MobaXterm"
        elif "zabbix" in wt_low or "monitoreo" in fc_low:
            anchors["herramienta_origen"] = "Zabbix"
        elif "vilo" in fc_low:
            anchors["herramienta_origen"] = "ViloLiving"
        elif "grafana" in wt_low or "grafana" in fc_low or "subsite validate" in fc_low:
            anchors["herramienta_origen"] = "Grafana"
        elif any(nav in wt_low for nav in ["chrome", "edge", "firefox"]):
            anchors["herramienta_origen"] = "Web Browser"

        return anchors

    def has_strong_anchor(self, anchors: dict) -> tuple[bool, str]:
        """
        Valida si la captura contiene al menos un ancla fuerte (Técnico, CUN/ID Visita, Sitio o MAC).
        """
        if anchors.get("tecnico_campo"):
            return True, f"Ancla: Técnico '{anchors['tecnico_campo']}'"
        if anchors.get("id_servicio_cun"):
            return True, f"Ancla: ID/CUN '{anchors['id_servicio_cun']}'"
        if anchors.get("codigo_sitio"):
            return True, f"Ancla: Sitio '{anchors['codigo_sitio']}'"
        if anchors.get("mac_addresses"):
            return True, f"Ancla: MAC '{anchors['mac_addresses'][0]}'"
        return False, "Captura sin ancla identificable (Descartada para no contaminar datos)"

    def classify_category(self, text: str, window_title: str = "", anchors: Optional[dict] = None) -> Tuple[str, float]:
        """
        Pre-Clasificador Heurístico Local (<0.5 ms) hacia las 7 Categorías Oficiales de SOMOS.
        Respeta la regla oficial: CPE aplica a TODOS los routers de cliente (no solo radioenlaces).
        Pondera con prioridad la causa raíz confirmada en los eventos más recientes de la cubeta.
        Retorna (categoria_oficial, confianza).
        """
        full = f"{window_title} {text}".lower()
        recent = (text or "")[-450:].lower()
        tool = (anchors or {}).get("herramienta_origen", "").lower()

        # 0. Ponderación por causa raíz confirmada en el tramo más reciente de la cubeta (cierre limpio en Memoria Oficial)
        if re.search(
            r"(puerto\s+(?:f[ií]sico\s+)?\d*\s*(?:del\s+)?(?:switch|sw)\b.{0,40}(?:dañad|danad|malo|averiad|fallando|sin\s+servicio))"
            r"|(?:(?:cambi[eéoóa]|pas[eéoóa]|migr[eéoóa])\w*\s+(?:de\s+puerto|al\s+puerto|la\s+acometida\s+al\s+puerto).{0,50}(?:switch|sw)\b)"
            r"|(?:(?:switch|sw)\s+(?:de\s+piso\s*)?.{0,30}(?:puerto\s+(?:f[ií]sico\s+)?\d*\s*(?:dañad|danad|averiad|malo)))",
            recent,
            re.IGNORECASE | re.DOTALL
        ):
            return "Switch/Capa 2", 0.93

        # 1. Radioenlace (solo cuando hay términos explícitos de enlace inalámbrico exterior/antena/CCQ)
        if any(w in full for w in ["radioenlace", "antena", "ccq", "alineación", "alineacion", "estación base", "estacion base", "litebeam", "nanobeam", "mimosa"]):
            return "Radioenlace", 0.92

        # 2. Acometida/8 Hilos (negociación 100M, par abierto, conector RJ45, ponchado, sulfatado, puerto WAN caído)
        if any(w in full for w in ["100 megas", "100m", "100 mbps", "base 100", "linkea a 100", "linkea a base", "8 hilos", "ocho hilos", "ponchado", "reponchar", "rj45", "sulfatado", "par abierto", "conector malo", "puerto wan", "wan caido", "wan caído"]):
            return "Acometida/8 Hilos", 0.90

        # 3. Soporte Capa 3 (WinBox DHCP Leases, estado offered, cambio de IP, falla masiva NOC/energía edificio)
        if tool == "winbox" or any(w in full for w in ["dhcp server", "leases", "offered", "remove lease", "remover lease", "no toma ip", "no agarra ip", "sin ip", "no da ip", "mikrotik chr", "ip pública", "ip publica", "dmz", "afectación por energía", "todos esos sw están apagados"]):
            return "Soporte Capa 3", 0.90

        # 4. Switch/Capa 2 (switch de piso, montante, cascada, Zabbix Edge, VLAN, PoE de piso, switch copado)
        if tool == "zabbix" or any(w in full for w in ["switch", "sw ", "swicht", "montante", "cascada", "vlan", "registrar este poe", "registrar sw", "sw copado", "puerto de consola", "temperatura"]):
            return "Switch/Capa 2", 0.88

        # 5. Velocidad/Plan Pro (speedtest Artemis bloqueado, 2000 megas/2G, 900 megas, No Radius / Autenticar)
        if any(w in full for w in ["speedtest", "test de velocidad", "2000", "2gb", "plan pro", "900mb", "900 megas", "no radius", "aparece en radius", "autenticar"]):
            return "Velocidad/Plan Pro", 0.86

        # 6. Router Secundario (OpenWISP, Vilo, LuCI, CPE secundario/auxiliar/mesh, plantilla 2026/VPN2, SSID/bandas Wi-Fi)
        if tool in ["openwisp", "viloliving"] or any(w in full for w in ["secundario", "auxiliar", "mesh", "emparejar", "openwisp", "plantilla", "vpn2", "ssid", "bandas", "2.4", "5g", "5 ghz", "wafi", "wifi", "wi-fi", "vilo", "luci", "orb", "phantom", "e8:"]):
            return "Router Secundario", 0.85

        # 7. Fallback seguro a Soporte General (incluye Grafana Subsite Validate y consultas generales)
        return "Soporte General", 0.70

    def has_actionable_technical_signal(self, text: str, window_title: str = "") -> bool:
        """
        Verifica que el texto contenga un síntoma técnico, parámetro de red o consola diagnóstica
        y NO sea únicamente un saludo inicial ('Hola', 'Buenos días', 'En qué puedo ayudarte').
        Evita las interrupciones prematuras del pasado ('Punto 1').
        """
        full = f"{window_title} {text}".lower()
        technical_signals = [
            "sin navegacion", "sin navegación", "no navega", "no suben", "no sube", "bandas",
            "wifi", "wafi", "ssid", "plantilla", "openwisp", "vilo", "luci", "zabbix", "grafana",
            "winbox", "leases", "offered", "bound", "switch", "sw ", "poe", "puerto", "100m",
            "100 megas", "1g", "2gb", "900", "speedtest", "radius", "autenticar", "auxiliar",
            "secundario", "mesh", "orb", "phantom", "offline", "caido", "caído", "intermitencia",
            "historial", "cascada", "montante", "vlan", "ip ", "dhcp", "mac ", "ponchado", "rj45"
        ]
        return any(sig in full for sig in technical_signals)
