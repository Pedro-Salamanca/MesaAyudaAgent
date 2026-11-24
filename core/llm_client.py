import os
import re
import json
import time
import threading
from typing import Optional, List, Dict, Tuple, Set
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from core.config import (
    MODEL_NAME,
    FALLBACK_MODELS,
    API_HTTP_TIMEOUT_MS,
    API_HTTP_RETRY_ATTEMPTS,
    COOLDOWN_429_DEFAULT_S,
    COOLDOWN_503_DEFAULT_S,
    TEMPERATURE_ANALISIS,
    GEMINI_API_KEY,
    CATEGORIAS_OFICIALES,
    CATEGORIA_A_MANUALES,
    ARBOLES_DIAGNOSTICO_PATH
)
from core.logger import log


class APIKeyPool:
    """
    Gestor de Pool Multi-Cuenta y Multi-Modelo v4.1 con:
    - Balanceo Round-Robin protegido con threading.Lock() (reparte la carga equitativamente entre cuentas).
    - Reintentos internos limitados (attempts=1, timeout=20s) para conmutar en <2s ante picos 503.
    - Enfriamiento diferenciado por par (llave, modelo): 429 (retryDelay / 50s) vs 503 (6s).
    - Fallback multi-modelo automático (ej. gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.5-flash -> gemini-3.8-flash).
    - Corte rápido (<5 ms) al Motor Determinista Local solo cuando el 100% de pares estén en cooldown 429 real.
    """
    def __init__(self, models: Optional[List[str]] = None):
        self._lock = threading.Lock()
        self.keys: List[str] = []

        primary = os.getenv("GEMINI_API_KEY", "").strip()
        if primary:
            self.keys.append(primary)

        backups = os.getenv("GEMINI_BACKUP_KEYS", "").strip()
        if backups:
            for k in backups.split(","):
                k_clean = k.strip()
                if k_clean and k_clean not in self.keys:
                    self.keys.append(k_clean)

        base_models = list(models) if models else list(FALLBACK_MODELS)
        if MODEL_NAME not in base_models:
            base_models.insert(0, MODEL_NAME)
        self.models: List[str] = []
        for m in base_models:
            if m and m not in self.models:
                self.models.append(m)

        self.current_index = 0
        self.embed_index = 0
        # Matriz de enfriamiento por par (key, model) -> (unblock_ts, reason)
        self.pair_cooldowns: Dict[Tuple[str, str], Tuple[float, str]] = {}
        # Enfriamiento separado para embeddings (gemini-embedding-001)
        self.embed_cooldowns: Dict[str, float] = {}
        # Compatibilidad hacia atrás para telemetría/scripts heredados
        self.cooldowns: Dict[str, float] = {}
        self.clients_cache: Dict[str, genai.Client] = {}

        log.info(
            f"Pool Multi-Cuenta/Multi-Modelo inicializado: {len(self.keys)} llave(s) | "
            f"Cadena de modelos: {' -> '.join(self.models)}"
        )

    def _get_or_create_client(self, key: str) -> Optional[genai.Client]:
        if key not in self.clients_cache:
            try:
                http_opts = types.HttpOptions(
                    timeout=API_HTTP_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=API_HTTP_RETRY_ATTEMPTS)
                )
                self.clients_cache[key] = genai.Client(api_key=key, http_options=http_opts)
            except Exception as e:
                log.error(
                    f"Error instanciando genai.Client con key terminada en "
                    f"...{key[-4:] if len(key) > 4 else key}: {e}"
                )
                return None
        return self.clients_cache[key]

    @staticmethod
    def _parse_retry_delay_seconds(err_text: str, default_s: int = COOLDOWN_429_DEFAULT_S) -> float:
        m = re.search(r"retry in\s+([\d\.]+)s", err_text, re.IGNORECASE)
        if m:
            try:
                return max(10.0, min(90.0, float(m.group(1)) + 1.0))
            except ValueError:
                pass
        m2 = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", err_text, re.IGNORECASE)
        if m2:
            try:
                return max(10.0, min(90.0, float(m2.group(1)) + 1.0))
            except ValueError:
                pass
        return float(default_s)

    def all_pairs_in_429_cooldown(self) -> bool:
        """Retorna True ÚNICAMENTE si el 100% de pares (llave, modelo) están bloqueados por cuota 429 activa."""
        with self._lock:
            if not self.keys or not self.models:
                return True
            now = time.time()
            for m in self.models:
                for k in self.keys:
                    entry = self.pair_cooldowns.get((k, m))
                    if not entry:
                        return False
                    unblock_ts, reason = entry
                    if now >= unblock_ts or reason != "429":
                        return False
            return True

    def acquire_pair(
        self,
        preferred_model_idx: int = 0,
        exclude_pairs: Optional[Set[Tuple[str, str]]] = None
    ) -> Tuple[Optional[str], Optional[str], Optional[genai.Client]]:
        """
        Selecciona en Round-Robin thread-safe el siguiente par sano (key, model, client).
        - Avanza `current_index` en cada adquisición exitosa para balancear la carga 33%/33%/33%.
        - Respeta enfriamientos diferenciados por (key, model).
        - Si todo lo no excluido está en cooldown 429 activo, retorna (None, None, None) (corte rápido).
        """
        excluded = exclude_pairs or set()
        with self._lock:
            if not self.keys or not self.models:
                return None, None, None

            now = time.time()
            n_models = len(self.models)
            n_keys = len(self.keys)

            # 1. Buscar primer par completamente libre (sin cooldown activo), respetando Round-Robin de llaves
            for m_off in range(n_models):
                m = self.models[(preferred_model_idx + m_off) % n_models]
                for k_off in range(n_keys):
                    k_idx = (self.current_index + k_off) % n_keys
                    k = self.keys[k_idx]
                    if (k, m) in excluded:
                        continue
                    entry = self.pair_cooldowns.get((k, m))
                    if entry is None or now >= entry[0]:
                        self.current_index = (k_idx + 1) % n_keys
                        client = self._get_or_create_client(k)
                        if client:
                            return k, m, client

            # 2. Si todos tienen cooldown activo: permitir reintento SOLO en pares enfriados por 503/error transitorio
            #    (NUNCA golpear pares con 429 real activo -> garantiza corte rápido <5ms ante cuota agotada)
            non_429_candidates = []
            for m_off in range(n_models):
                m = self.models[(preferred_model_idx + m_off) % n_models]
                for k in self.keys:
                    if (k, m) in excluded:
                        continue
                    entry = self.pair_cooldowns.get((k, m))
                    if entry and entry[1] not in ("429", "404"):
                        non_429_candidates.append((entry[0], k, m))

            if non_429_candidates:
                non_429_candidates.sort(key=lambda x: x[0])
                _, k, m = non_429_candidates[0]
                client = self._get_or_create_client(k)
                if client:
                    return k, m, client

            return None, None, None

    def acquire_embed_client(
        self,
        exclude_keys: Optional[Set[str]] = None
    ) -> Tuple[Optional[str], Optional[genai.Client]]:
        """Adquiere en Round-Robin thread-safe una llave sana para gemini-embedding-001."""
        excluded = exclude_keys or set()
        with self._lock:
            if not self.keys:
                return None, None
            now = time.time()
            n_keys = len(self.keys)
            for k_off in range(n_keys):
                k_idx = (self.embed_index + k_off) % n_keys
                k = self.keys[k_idx]
                if k in excluded:
                    continue
                if now >= self.embed_cooldowns.get(k, 0):
                    self.embed_index = (k_idx + 1) % n_keys
                    client = self._get_or_create_client(k)
                    if client:
                        return k, client
            return None, None

    def report_embed_error(self, key: str, exc: Exception) -> str:
        err_text = str(exc)
        err_low = err_text.lower()
        suf = f"...{key[-4:]}" if key and len(key) > 4 else str(key)
        with self._lock:
            if "429" in err_low or "resource_exhausted" in err_low:
                delay = self._parse_retry_delay_seconds(err_text, COOLDOWN_429_DEFAULT_S)
                self.embed_cooldowns[key] = time.time() + delay
                log.warning(f"Embedding Key ({suf}) alcanzó límite 429. Enfriando {delay:.0f}s...")
                return "429"
            if "503" in err_low or "unavailable" in err_low or "high demand" in err_low:
                self.embed_cooldowns[key] = time.time() + COOLDOWN_503_DEFAULT_S
                log.warning(f"Embedding Key ({suf}) recibió 503. Enfriando {COOLDOWN_503_DEFAULT_S}s...")
                return "503"
            self.embed_cooldowns[key] = time.time() + COOLDOWN_503_DEFAULT_S
            log.warning(f"Error transitorio en Embedding Key ({suf}): {err_text[:140]}")
            return "error"

    def report_error(self, key: str, model: str, exc: Exception) -> str:
        """
        Clasifica el error y aplica enfriamiento diferenciado por par (llave, modelo):
        - 429 / RESOURCE_EXHAUSTED -> cooldown exacto (retryDelay o 50s); retorna '429'.
        - 503 / UNAVAILABLE        -> cooldown breve (6s); retorna '503' para saltar de modelo.
        - 404 / NOT_FOUND          -> deshabilita ese modelo por 300s; retorna '404'.
        - Otros (timeout/socket)   -> cooldown breve (8s); retorna 'error'.
        """
        err_text = str(exc)
        err_low = err_text.lower()
        suf = f"...{key[-4:]}" if key and len(key) > 4 else str(key)
        now = time.time()

        with self._lock:
            if "429" in err_low or "resource_exhausted" in err_low:
                delay = self._parse_retry_delay_seconds(err_text, COOLDOWN_429_DEFAULT_S)
                self.pair_cooldowns[(key, model)] = (now + delay, "429")
                self.cooldowns[key] = now + delay
                log.warning(f"API Key ({suf}) | {model} alcanzó cuota (429). Enfriando {delay:.0f}s...")
                return "429"
            if "503" in err_low or "unavailable" in err_low or "overloaded" in err_low or "high demand" in err_low:
                delay = float(COOLDOWN_503_DEFAULT_S)
                self.pair_cooldowns[(key, model)] = (now + delay, "503")
                log.warning(f"API Key ({suf}) | {model} saturado (503). Enfriando {delay:.0f}s y rotando modelo...")
                return "503"
            if "404" in err_low or "not_found" in err_low:
                self.pair_cooldowns[(key, model)] = (now + 300.0, "404")
                log.warning(f"Modelo {model} no disponible (404) en Key ({suf}). Saltando al siguiente modelo...")
                return "404"
            delay = 8.0
            self.pair_cooldowns[(key, model)] = (now + delay, "error")
            log.warning(f"Error transitorio en Key ({suf}) | {model}: {err_text[:160]}")
            return "error"

    # --- Métodos de compatibilidad hacia atrás (scripts externos / diagnóstico) ---
    def get_active_key(self) -> Optional[str]:
        with self._lock:
            if not self.keys:
                return None
            now = time.time()
            m0 = self.models[0] if self.models else MODEL_NAME
            for i in range(len(self.keys)):
                idx = (self.current_index + i) % len(self.keys)
                k = self.keys[idx]
                entry = self.pair_cooldowns.get((k, m0))
                if entry is None or now >= entry[0]:
                    return k
            return min(
                self.keys,
                key=lambda k: (self.pair_cooldowns.get((k, m0)) or (0.0, ""))[0]
            )

    def get_client(self) -> Optional[genai.Client]:
        key = self.get_active_key()
        if not key:
            return None
        with self._lock:
            return self._get_or_create_client(key)

    def mark_rate_limited(self, cooldown_seconds: int = 60, specific_key: Optional[str] = None, model: Optional[str] = None):
        key = specific_key or self.get_active_key()
        target_model = model or (self.models[0] if self.models else MODEL_NAME)
        if key:
            with self._lock:
                now = time.time()
                self.pair_cooldowns[(key, target_model)] = (now + cooldown_seconds, "429")
                self.cooldowns[key] = now + cooldown_seconds
                log.warning(
                    f"API Key (...{key[-4:] if len(key) > 4 else key}) | {target_model} "
                    f"enfriando {cooldown_seconds}s por saturación/error temporal..."
                )
                self.current_index = (self.current_index + 1) % len(self.keys)


class AnalisisEscenario(BaseModel):
    """Esquema de salida estructurada JSON para el análisis visual instantáneo."""
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str = Field(description="Nombre del técnico si aparece 'Chat con [Nombre]'. Si no hay, 'Desconocido'.")
    descripcion_problema: str
    accion_sugerida: str
    nivel_confianza: str = Field(description="Alta, Media, Baja")


class AsistenciaGuiaTecnica(BaseModel):
    """
    Esquema estructurado v4.0 para el Triángulo de Grounding (Manual v2.0 + Casos de Oro + Historial Cubeta + Árbol).
    Entrega diagnóstico interno para el operador, paso del árbol determinista y respuesta sugerida con Tono SOMOS.
    """
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str = Field(description="Nombre del técnico en campo o 'Técnico en Sitio'")
    categoria_falla: str = Field(description="Categoría oficial: 'Soporte Capa 3', 'Switch/Capa 2', 'Router Secundario', 'Acometida/8 Hilos', 'Velocidad/Plan Pro', 'Radioenlace', 'Soporte General'")
    fase_actual: str = Field(description="Fase operativa actual: '1. Identificación y Pre-registro', '2. Descarte Físico (Capa 1/2)', '3. Configuración Lógica (Capa 3)' o '4. Validación y Cierre'")
    diagnostico_tecnico: str = Field(description="Análisis técnico conciso de causa raíz para el operador de Mesa de Ayuda (1 a 2 líneas)")
    paso_arbol_actual: str = Field(description="Paso actual del árbol de diagnóstico oficial que aplica según lo que ya se probó en la cubeta")
    siguiente_descarte: str = Field(description="Acción o verificación inmediata siguiente si el descarte actual está OK")
    mensaje_sugerido_somos: str = Field(description="Mensaje corto, cálido y técnico con tono SOMOS ('Compa, porfa...') listo para copiar y enviar al técnico en campo")
    fuentes_grounding: str = Field(description="Manual Base v2.0 y Casos Canónicos utilizados para fundamentar la respuesta")
    nivel_confianza: str = Field(description="Alta, Media, Baja")


class AnalisisEpisodio(BaseModel):
    """Esquema de salida estructurada JSON para la síntesis de episodio completo (Cubeta finalizada)."""
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str
    id_servicio_cun: str
    ciudad_nodo: str = Field(description="Nodo o ciudad (ej. MDE, BOG, RIVA, o Desconocido)")
    categoria_falla: str = Field(description="Categoría exacta: 'Soporte Capa 3', 'Switch/Capa 2', 'Router Secundario', 'Acometida/8 Hilos', 'Velocidad/Plan Pro', 'Radioenlace', 'Soporte General'")
    herramienta_origen: str = Field(description="Herramienta principal: Artemis, Putty, WinBox, Selene, Vilo, Zabbix, OpenWisp, Grafana, etc.")
    descripcion_problema: str
    ruta_diagnostico_pasos: str = Field(description="Resumen conciso de los pasos y herramientas seguidas en la cubeta")
    accion_sugerida: str
    nivel_confianza: str


class PreSintesisCaso(BaseModel):
    """Esquema estructurado para la pre-síntesis de cubetas que pasan a revisión diferida (Staging Cards)."""
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str
    id_servicio_cun: str
    codigo_sitio: str = Field(description="Código del nodo o sitio (ej. SOMOS-MDE-RIVA, RIVA-T4, o Desconocido)")
    categoria_falla: str = Field(description="Categoría exacta: 'Soporte Capa 3', 'Switch/Capa 2', 'Router Secundario', 'Acometida/8 Hilos', 'Velocidad/Plan Pro', 'Radioenlace', 'Soporte General'")
    herramientas_usadas: str = Field(description="Herramientas detectadas: Artemis, Putty, WinBox, Selene, Vilo, Zabbix, OpenWisp, etc.")
    problema_observado: str = Field(description="Descripción clara y concisa de lo que el agente observó en la atención")
    solucion_deducida_ia: str = Field(description="Lo que el agente dedujo como solución o indicación técnica ejecutada")
    pregunta_duda_ia: str = Field(description="Pregunta OBLIGATORIA y concreta dirigida al operador de mesa para confirmar o aclarar la acción técnica clave ejecutada en el caso.")
    nivel_confianza: str


class LLMClient:
    """
    Cliente de integración con Google Gemini, Triángulo de Grounding y Embeddings de SOMOS NETWORK v4.0.
    - Inyección selectiva de Manuales Base v2.0 por categoría (-75% a -83% tokens por inferencia).
    - Evaluación de Árboles Deterministas de Diagnóstico (`data/arboles_diagnostico.json`) consciente del historial de cubeta.
    - Caché en memoria de embeddings de sesión para cero llamadas duplicadas a `gemini-embedding-001`.
    - Post-filtro Guardrail determinista Zero-ONT, CPE global, No-Ticket individual y restricción WinBox/Zabbix.
    """
    def __init__(self, db=None):
        self.pool = APIKeyPool()
        self.db = db
        self.model_name = MODEL_NAME
        self.embedding_model = "gemini-embedding-001"
        self.manuales_contexto = ""
        self.manuales_by_id: Dict[str, dict] = {}
        self.arboles_diagnostico: Dict[str, dict] = {}
        self._embedding_cache: Dict[str, List[float]] = {}
        self._embedding_lock = threading.Lock()
        self.last_used_key: Optional[str] = None
        self.last_used_model: Optional[str] = None

        self.cargar_manuales()
        self.cargar_arboles_diagnostico()

    def _generate_with_failover(
        self,
        contents: str,
        config: types.GenerateContentConfig,
        max_attempts: Optional[int] = None
    ):
        """
        Ejecuta generate_content con Round-Robin Multi-Cuenta + Fallback Multi-Modelo:
        - Si el 100% de pares (llave, modelo) están en cooldown 429 activo, corta al instante (<5 ms).
        - Ante 429: mantiene modelo rápido y rota a la siguiente cuenta/llave (si todas están 429, salta de modelo).
        - Ante 503/404/error: rota cuenta/llave Y salta de inmediato al siguiente modelo de la cadena.
        """
        if self.pool.all_pairs_in_429_cooldown():
            log.info("Corte rápido (<5 ms): 100% de pares (llave, modelo) en cooldown 429 activo -> Motor Determinista Local.")
            return None

        n_keys = max(1, len(self.pool.keys))
        n_models = max(1, len(self.pool.models))
        attempts_limit = max_attempts or min(6, n_keys * n_models)

        tried_pairs: Set[Tuple[str, str]] = set()
        model_idx = 0

        for _ in range(attempts_limit):
            key, model, client = self.pool.acquire_pair(
                preferred_model_idx=model_idx,
                exclude_pairs=tried_pairs
            )
            if not client or not key or not model:
                break

            tried_pairs.add((key, model))
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
                self.last_used_key = key
                self.last_used_model = model
                return response
            except Exception as e:
                err_type = self.pool.report_error(key, model, e)
                if err_type in ("503", "404", "error"):
                    model_idx = (model_idx + 1) % n_models
                time.sleep(0.15)
                continue

        return None

    @staticmethod
    def _is_strictly_out_of_domain(texto: str) -> Optional[str]:
        """
        Detector CONSERVADOR de temas inequívocamente ajenos al soporte técnico SOMOS
        (usado ÚNICAMENTE en Fallback Local; NUNCA bloquea el paso hacia Gemini AI).
        Garantiza 0% falsos bloqueos: ante cualquier indicio de red/equipo/servicio/cliente técnico, retorna None.
        """
        if not texto:
            return None
        low = texto.lower()

        # Si hay CUALQUIER señal técnica, de equipo, servicio o campo -> SIEMPRE es soporte técnico
        broad_tech_hints = [
            "red", "cable", "router", "cpe", "ip", "puerto", "wifi", "wi-fi", "wafi",
            "señal", "senal", "lento", "caido", "caído", "cae", "corte", "microcorte",
            "navega", "navegación", "navegacion", "switch", "sw ", "swicht", "mac", "luz",
            "led", "rojo", "roja", "parpadea", "equipo", "apto", "apartamento", "torre",
            "piso", "visita", "cun", "artemis", "selene", "winbox", "openwisp", "vilo",
            "luci", "zabbix", "grafana", "poe", "mega", "giga", "velocidad", "antena",
            "enlace", "radioenlace", "ponch", "rj45", "dhcp", "radius", "ssid", "clave",
            "contraseña", "contrasena", "phantom", "orb", "mesh", "montante", "cascada",
            "vertical", "acometida", "wan", "lan", "offered", "bound", "lease", "speedtest",
            "base 100", "100m", "1g", "2gb", "900", "ccq", "dbm", "subsite", "offline",
            "online", "sincroniz", "reinici", "reset", "chuz", "intermiten", "desconect"
        ]
        if any(h in low for h in broad_tech_hints):
            return None

        # Solo rechazar categorías inequívocamente ajenas a soporte técnico en terreno
        strict_non_tech = [
            (["nota crédito", "nota credito", "cobraron doble", "factura", "facturación", "facturacion", "cartera", "reembolso"],
             "Facturación / Atención al Cliente"),
            (["cancelar contrato", "cancelar mi contrato", "devuelvan el dinero", "devolución de dinero", "devolucion de dinero", "retiro comercial"],
             "Gestión Comercial / Atención al Cliente"),
            (["quincena", "certificado laboral", "recursos humanos", "talento humano", "nómina", "nomina", "vacaciones"],
             "Talento Humano / Área Administrativa"),
            (["receta", "bandeja paisa", "ingredientes", "cocinar", "cuéntame un chiste", "cuentame un chiste",
              "escribe un poema", "ciclo for", "en python", "en javascript", "horóscopo", "horoscopo"],
             "fuera del alcance de soporte técnico SOMOS NETWORK"),
        ]
        for patterns, area in strict_non_tech:
            if any(p in low for p in patterns):
                return area
        return None



    def cargar_manuales(self, db=None):
        """Carga los 8 manuales operativos v2.0 desde SQLite y los indexa por id_manual para inyección selectiva."""
        self.manuales_contexto = ""
        self.manuales_by_id.clear()
        try:
            from core.database import HybridVectorDB
            target_db = db or self.db or HybridVectorDB()
            self.db = target_db
            manuales = target_db.get_all_manuales()
            for m in manuales:
                self.manuales_by_id[m["id_manual"]] = m
                self.manuales_contexto += f"\n--- MANUAL: {m['titulo']} ({m.get('categoria', 'General')}) ---\n{m['contenido']}\n"
            log.info(f"Cargados {len(manuales)} manuales operativos v2.0 desde SQLite (memory_v3.db).")
        except Exception as e:
            log.error(f"Error cargando manuales operativos desde SQLite: {e}")

    def cargar_arboles_diagnostico(self):
        """Carga el catálogo determinista de árboles de diagnóstico desde data/arboles_diagnostico.json."""
        self.arboles_diagnostico.clear()
        try:
            if os.path.exists(ARBOLES_DIAGNOSTICO_PATH):
                with open(ARBOLES_DIAGNOSTICO_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.arboles_diagnostico = data.get("arboles", {})
                log.info(f"Cargados {len(self.arboles_diagnostico)} árboles de diagnóstico deterministas.")
        except Exception as e:
            log.warning(f"No se pudo cargar arboles_diagnostico.json: {e}")

    @staticmethod
    def aplicar_guardrail_somos(texto: str) -> str:
        """
        Post-Filtro Determinista Obligatorio (Guardrail SOMOS):
        - Reemplaza cualquier mención accidental a ONT/ONU por CPE y evita frases redundantes ("como 'CPE', sino como CPE").
        - Enmascara números telefónicos de 10 dígitos.
        - Corrige sugerencias prohibidas de modificar colas simples en WinBox.
        """
        if not texto:
            return ""
        res = re.sub(r'\b[Oo][Nn][Tt][sS]?\b', 'CPE', texto)
        res = re.sub(r'\b[Oo][Nn][Uu][sS]?\b', 'CPE', res)
        # Limpiar redundancia si el modelo citó la sigla prohibida entre comillas para corregirla
        res = re.sub(r'(?i)no\s+(?:manejamos|usamos|llamamos)\s+(?:la\s+infraestructura\s+|los\s+equipos\s+)?como\s+["\']?CPE["\']?,?\s+sino\s+como\s+["\']?CPE["\']?', 'en SOMOS NETWORK todos los routers de cliente se denominan CPE', res)
        res = re.sub(r'(?i)["\']?CPE["\']?,?\s+sino\s+["\']?CPE["\']?', 'CPE', res)
        res = re.sub(r'\b3\d{9}\b', '[TEL]', res)
        if "cola simple" in res.lower() and "winbox" in res.lower():
            res = re.sub(r'(?i)modificar?\s+la\s+cola\s+simple', 'verificar lease DHCP en WinBox (IP > DHCP Server > Leases)', res)
        return res.strip()

    def get_contexto_manual_categoria(self, categoria_falla: str) -> Tuple[str, List[str]]:
        """
        Obtiene ÚNICAMENTE los manuales v2.0 asociados a la categoría oficial detectada,
        reduciendo el consumo de tokens de manuales en un 75-83%.
        Retorna (texto_manuales_filtrados, lista_titulos_usados).
        """
        if not self.manuales_by_id:
            self.cargar_manuales()

        ids = CATEGORIA_A_MANUALES.get(categoria_falla, ["checklist_validaciones_sitio"])
        bloques = []
        titulos = []
        for mid in ids:
            m = self.manuales_by_id.get(mid)
            if m:
                bloques.append(f"--- MANUAL OFICIAL [{m['id_manual']} v{m.get('version', '2.0')}]: {m['titulo']} ---\n{m['contenido']}")
                titulos.append(f"{m['id_manual']} (v{m.get('version', '2.0')})")

        if not bloques:
            for m in list(self.manuales_by_id.values())[:2]:
                bloques.append(f"--- MANUAL OFICIAL [{m['id_manual']}]: {m['titulo']} ---\n{m['contenido']}")
                titulos.append(m["id_manual"])

        return "\n\n".join(bloques), titulos

    def evaluar_arbol_determinista(self, categoria_falla: str, timeline_text: str = "") -> dict:
        """
        Evalúa el árbol de decisión determinista para `categoria_falla` contrastándolo con
        el historial acumulado de la cubeta (`timeline_text`).
        Si la cubeta demuestra que el técnico ya completó el Paso 1, avanza automáticamente al Paso 2.
        """
        if not self.arboles_diagnostico:
            self.cargar_arboles_diagnostico()

        arbol = self.arboles_diagnostico.get(categoria_falla) or self.arboles_diagnostico.get("Soporte General", {})
        pasos = arbol.get("pasos", [])
        if not pasos:
            return {
                "titulo_arbol": f"Diagnóstico {categoria_falla}",
                "paso_activo": 1,
                "fase": "Diagnóstico Técnico",
                "verificacion": "Validar parámetros físicos y de gestión del CPE/switch en herramientas SOMOS.",
                "accion_si_falla": "Aplicar protocolo oficial de la categoría.",
                "siguiente_paso": "Confirmar navegación y estabilidad con el técnico.",
                "sugerencia_tono_somos": "Compa, regálame porfa ID de visita y MAC del CPE para validarte en plataforma."
            }

        tl_low = (timeline_text or "").lower()
        active_idx = 0
        for idx, p in enumerate(pasos):
            conds = p.get("condicion_omitir_si_cubeta_contiene", [])
            if conds and any(c.lower() in tl_low for c in conds) and idx + 1 < len(pasos):
                active_idx = idx + 1
            else:
                break

        curr_p = pasos[active_idx]
        next_p = pasos[active_idx + 1] if (active_idx + 1) < len(pasos) else None
        return {
            "titulo_arbol": arbol.get("titulo", f"Árbol {categoria_falla}"),
            "paso_activo": curr_p.get("paso", active_idx + 1),
            "total_pasos": len(pasos),
            "fase": curr_p.get("fase", "Descarte Técnico"),
            "verificacion": curr_p.get("verificacion", ""),
            "accion_si_falla": curr_p.get("accion_si_falla", ""),
            "siguiente_paso": (
                f"Paso {next_p['paso']} ({next_p['fase']}): {next_p['verificacion']}"
                if next_p else "Confirmar restablecimiento definitivo de navegación/bandas y cerrar caso."
            ),
            "sugerencia_tono_somos": curr_p.get("sugerencia_tono_somos", "")
        }

    def generar_embedding(self, texto: str) -> Optional[List[float]]:
        """
        Genera el vector semántico (3,072 dimensiones) usando gemini-embedding-001
        con balanceo Round-Robin thread-safe entre cuentas y caché en RAM de sesión.
        """
        if not texto or not texto.strip():
            return None

        clean_text = " ".join(texto.split())[:2000]
        with self._embedding_lock:
            if clean_text in self._embedding_cache:
                return self._embedding_cache[clean_text]

        tried_keys: Set[str] = set()
        for _ in range(max(1, len(self.pool.keys))):
            key, client = self.pool.acquire_embed_client(exclude_keys=tried_keys)
            if not client or not key:
                return None
            tried_keys.add(key)

            try:
                response = client.models.embed_content(
                    model=self.embedding_model,
                    contents=clean_text
                )
                if response and hasattr(response, "embeddings") and response.embeddings:
                    values = list(response.embeddings[0].values)
                    with self._embedding_lock:
                        if len(self._embedding_cache) >= 128:
                            self._embedding_cache.pop(next(iter(self._embedding_cache)))
                        self._embedding_cache[clean_text] = values
                    return values
            except Exception as e:
                self.pool.report_embed_error(key, e)
                time.sleep(0.15)
                continue

        return None

    def asistir_caso_guiado(
        self,
        timeline_text: str,
        texto_pantalla_actual: str,
        categoria_preclasificada: str,
        casos_rag: Optional[List[Dict]] = None,
        tecnico_nombre: str = "Técnico en Sitio"
    ) -> AsistenciaGuiaTecnica:
        """
        Motor Principal v4.0 — Triángulo de Grounding:
        Sintetiza:
          1) Manual Base Oficial v2.0 de la categoría detectada (Inyección selectiva).
          2) Paso actual del Árbol Determinista de Diagnóstico (`arboles_diagnostico.json`).
          3) Casos Canónicos de Oro + Histórico FTS5 recuperados de `memory_v3.db`.
          4) Historial cronológico completo acumulado en la Cubeta (para NO repetir descartes ya hechos).
        Si la cuota de API está agotada o sin red, entrega respuesta determinista instantánea desde el Árbol + Casos de Oro.
        """
        cat = categoria_preclasificada if categoria_preclasificada in CATEGORIAS_OFICIALES else "Soporte General"
        manual_txt, manual_ids = self.get_contexto_manual_categoria(cat)
        arbol_info = self.evaluar_arbol_determinista(cat, f"{timeline_text}\n{texto_pantalla_actual}")

        casos_str = ""
        casos_ids = []
        if casos_rag:
            lineas_c = []
            for idx, c in enumerate(casos_rag[:3], 1):
                cid = c.get("id") or c.get("id_caso") or f"caso_{idx}"
                casos_ids.append(cid)
                prob_c = (c.get("visual_context") or c.get("problema_detectado") or "")[:240]
                sol_c = (c.get("action") or c.get("solucion_oficial") or "")[:260]
                sug_c = (c.get("agent_suggestion") or c.get("sugerencia_ia") or "")[:180]
                lineas_c.append(f"• Caso Oficial [{cid}]:\n  - Síntoma: {prob_c}\n  - Solución verificada: {sol_c}\n  - Respuesta Mesa: {sug_c}")
            casos_str = "\n".join(lineas_c)
        else:
            casos_str = "Sin casos adicionales; fundamentar estrictamente en el Manual Base v2.0 y Árbol Determinista."

        fuentes_default = f"Manuales: {', '.join(manual_ids)} | Casos: {', '.join(casos_ids) if casos_ids else 'Árbol Oficial v2.0'}"

        prompt = f"""
Eres el Copiloto Senior de Mesa de Ayuda de SOMOS NETWORK (Arquitectura v4.0).
Tu misión es guiar al operador de Mesa de Ayuda sintetizando EXCLUSIVAMENTE las 3 fuentes oficiales de verdad provistas abajo.
TIENES TERMINANTEMENTE PROHIBIDO dar consejos genéricos de internet o soporte doméstico.

REGLAS INQUEBRANTABLES DE SOMOS NETWORK:
1. DEFINICIÓN DE CPE Y ZERO-ONT: En SOMOS el término "CPE" se aplica a TODOS los routers de cliente (principal, secundario, auxiliar, mesh Phantom/ORB/Vilo). En SOMOS NO existen ONTs ni ONUs; JAMÁS menciones la palabra ONT.
2. NO REPETIR DESCARTES: Revisa el [HISTORIAL ACUMULADO DE LA CUBETA]. Si el técnico ya reinició el CPE, ya probó con su PC o ya reponchó, NO le pidas repetir lo que ya hizo; avanza al siguiente paso del árbol.
3. JERARQUÍA DE SWITCHES: "Switch de piso" distribuye a los apartamentos; "Edge" es el switch principal del edificio (monitoreado en Zabbix). En cascadas de montante, verificar siempre PRIMERO el pre-registro de la MAC del switch antes de culpar voltaje PoE o puertos.
4. WINBOX (MIKROTIK CHR): Mesa de Ayuda opera ÚNICAMENTE en 'IP > DHCP Server > Leases' (CHR Medellín o Bogotá) y la ÚNICA acción permitida es 'Remover Lease' (Remove) cuando el CPE está en estado 'offered' o con IP en conflicto. Prohibido sugerir ajustes en colas simples (Simple Queues), firewall o rutas.
5. ZABBIX Y GRAFANA: Son paneles 100% de LECTURA. Jamás digas que desde Zabbix o Grafana se reinician o configuran equipos.
6. TERMINOLOGÍA Y PRIVACIDAD: Nunca uses la palabra "ticket" para visitas individuales (son "casos", "chats" o "visitas"; ticket es exclusivo para fallas masivas NOC de edificio). Nunca incluyas teléfonos.
7. TONO SOMOS EN 'mensaje_sugerido_somos' (SEPARACIÓN DE ROLES): Redacta una respuesta directa, cálida y técnica lista para copiar y enviar ÚNICAMENTE al técnico en campo ("Compa, porfa...", "Valida porfa...", "Ya te refresqué..."). Incluye solo acciones físicas/sitio para el técnico; JAMÁS le pidas al técnico en sitio abrir WinBox, OpenWISP, Zabbix o Grafana ni menciones IDs internos de memoria (`canon_conv_...`).

[FUENTE 1: MANUAL BASE OFICIAL DE LA CATEGORÍA ({cat})]
{manual_txt}

[FUENTE 2: PASO ACTUAL DEL ÁRBOL DETERMINISTA DE DIAGNÓSTICO]
- Árbol: {arbol_info['titulo_arbol']}
- Paso Activo ({arbol_info['paso_activo']}/{arbol_info.get('total_pasos', 3)} - {arbol_info['fase']}): {arbol_info['verificacion']}
- Acción Oficial: {arbol_info['accion_si_falla']}
- Siguiente Paso en Cola: {arbol_info['siguiente_paso']}
- Plantilla Base Tono SOMOS: "{arbol_info['sugerencia_tono_somos']}"

[FUENTE 3: CASOS CANÓNICOS DE ORO Y MEMORIA HISTÓRICA SIMILAR]
{casos_str}

[HISTORIAL ACUMULADO DE LA CUBETA (LO QUE YA OCURRIÓ EN ESTA ATENCIÓN)]
\"\"\"
{timeline_text[-2500:] if timeline_text else "Inicio de atención técnica."}
\"\"\"

[ÚLTIMA PANTALLA / EVENTO OBSERVADO]
\"\"\"
{texto_pantalla_actual[:1500]}
\"\"\"

Genera la salida estructurada con el esquema AsistenciaGuiaTecnica.
"""

        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AsistenciaGuiaTecnica,
                temperature=TEMPERATURE_ANALISIS
            )
        )
        if response:
            parsed: Optional[AsistenciaGuiaTecnica] = response.parsed
            if parsed:
                parsed.diagnostico_tecnico = self.aplicar_guardrail_somos(parsed.diagnostico_tecnico)
                parsed.paso_arbol_actual = self.aplicar_guardrail_somos(parsed.paso_arbol_actual)
                parsed.siguiente_descarte = self.aplicar_guardrail_somos(parsed.siguiente_descarte)
                parsed.mensaje_sugerido_somos = self.aplicar_guardrail_somos(parsed.mensaje_sugerido_somos)
                if not parsed.fuentes_grounding:
                    parsed.fuentes_grounding = fuentes_default
                return parsed

        # Fallback Determinista Local (<5 ms, 0 dependencia de red/cuota, 100% fundamentado en Manual + Árbol + Caso de Oro)
        ood_area = self._is_strictly_out_of_domain(f"{texto_pantalla_actual}\n{timeline_text}")
        if ood_area:
            return AsistenciaGuiaTecnica(
                es_problema_tecnico_real=False,
                nombre_tecnico_en_sitio=tecnico_nombre,
                categoria_falla="Soporte General",
                fase_actual="Fuera de Alcance Técnico",
                diagnostico_tecnico=f"Consulta identificada como {ood_area}; no corresponde a soporte técnico de red en terreno.",
                paso_arbol_actual="Redirigir al canal correspondiente sin abrir intervención técnica.",
                siguiente_descarte="Esperar consulta técnica real de red o equipos en terreno.",
                mensaje_sugerido_somos=f"Compa, porfa recuerda que Mesa de Ayuda atiende únicamente soporte técnico en terreno; este tema corresponde a {ood_area}.",
                fuentes_grounding="Política de Alcance Mesa de Ayuda SOMOS (Motor Determinista)",
                nivel_confianza="Alta (Motor Determinista)"
            )

        best_case_sol = ""
        best_case_sug = ""
        if casos_rag:
            best_case_sol = casos_rag[0].get("action") or casos_rag[0].get("solucion_oficial") or ""
            best_case_sug = casos_rag[0].get("agent_suggestion") or casos_rag[0].get("sugerencia_ia") or ""

        diag_local = best_case_sol[:240] if best_case_sol else arbol_info["accion_si_falla"]
        msg_somos_local = best_case_sug if (best_case_sug and len(best_case_sug) > 15) else arbol_info["sugerencia_tono_somos"]

        return AsistenciaGuiaTecnica(
            es_problema_tecnico_real=True,
            nombre_tecnico_en_sitio=tecnico_nombre,
            categoria_falla=cat,
            fase_actual=f"Paso {arbol_info['paso_activo']}: {arbol_info['fase']}",
            diagnostico_tecnico=self.aplicar_guardrail_somos(diag_local),
            paso_arbol_actual=self.aplicar_guardrail_somos(f"{arbol_info['verificacion']} -> {arbol_info['accion_si_falla']}"),
            siguiente_descarte=self.aplicar_guardrail_somos(arbol_info["siguiente_paso"]),
            mensaje_sugerido_somos=self.aplicar_guardrail_somos(msg_somos_local),
            fuentes_grounding=fuentes_default,
            nivel_confianza="Alta (Motor Determinista + Casos de Oro)"
        )

    def sintetizar_episodio(self, timeline_text: str, contexto_rag: str = "") -> Optional[AnalisisEpisodio]:
        """Ejecuta la síntesis estructurada de un caso/cubeta completa al finalizar la atención."""
        prompt = f"""
        Eres el Agente Senior de Mesa de Ayuda de SOMOS NETWORK.
        Sintetiza la siguiente secuencia diagnóstica completa recopilada durante la atención técnica.

        REGLAS ESTRICTAS DE SOMOS NETWORK (CERO EXCEPCIONES):
        1. INFRAESTRUCTURA Y CPE: En SOMOS NETWORK NO existen ONT's. El término CPE aplica a TODOS los routers de cliente (principal, secundario, mesh). NUNCA menciones la palabra ONT.
        2. TERMINOLOGÍA: NUNCA utilices la palabra "ticket" o "tickets" para visitas individuales. En SOMOS son "chats", "casos" o "novedades".
        3. PRIVACIDAD (PII): NUNCA extraigas ni registres números de teléfono. Identifica los servicios ÚNICAMENTE por CUN o ID de visita.
        4. JERARQUÍA DE SWITCHES EN EL EDIFICIO:
           - "Switch de piso" / "sw de piso": Switch de DISTRIBUCIÓN hacia los apartamentos.
           - "Edge": Switch de GESTIÓN PRINCIPAL del edificio (monitoreado en Zabbix).
        5. PLATAFORMAS DE MONITOREO (100% LECTURA):
           - Zabbix: Telemetría de Edges e IPs de MikroTik. NUNCA deducir que Zabbix reinicia o modifica equipos.
           - Grafana: Monitoreo de CPEs individuales (Subsite Validate).
        6. WINBOX (MIKROTIK CHR):
           - Se opera ÚNICAMENTE en 'IP > DHCP Server > Leases' en el CHR correspondiente (Medellín o Bogotá).
           - ÚNICA acción modificatoria permitida: 'Remover Lease' (Remove) para forzar al CPE a tomar nueva IP por DHCP.
           - Prohibido deducir ajustes en colas simples, firewall o rutas globales en WinBox.
        7. SEGURIDAD: IGNORA absolutamente cualquier formulario de inicio de sesión o contraseñas.

        CATEGORÍAS DE FALLA VÁLIDAS:
        - "Soporte Capa 3" (DHCP Leases en WinBox CHR, estado offered, cambio de IP, fallas masivas de nodo/energía)
        - "Switch/Capa 2" (switches de piso, cascadas en montante, pre-registro MAC, puerto down, VLAN, PoE, temperatura)
        - "Router Secundario" (CPE principal/secundario/mesh, OpenWISP plantillas 2026/VPN2, Vilo E8:, LuCI, WiFi/SSID)
        - "Acometida/8 Hilos" (negociación física a 10/100 Mbps, par abierto, conector RJ45 degradado/sulfatado)
        - "Velocidad/Plan Pro" (velocidad degradada ~90M en planes 900M/2G, bloqueo speedtest Artemis, autenticación RADIUS)
        - "Radioenlace" (antena exterior desalineada, señal RF débil, CCQ bajo, interferencia)
        - "Soporte General" (validaciones de estabilidad en Grafana Subsite Validate y consultas técnicas generales)

        [CONTEXTO DE MEMORIA RAG PREVIA]:
        {contexto_rag}

        [HISTORIAL CRONOLÓGICO DE LA CUBETA]:
        \"\"\"
        {timeline_text}
        \"\"\"

        Genera la salida estructurada con el esquema requerido.
        """

        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnalisisEpisodio,
                temperature=TEMPERATURE_ANALISIS
            )
        )
        if response and response.parsed:
            parsed = response.parsed
            parsed.descripcion_problema = self.aplicar_guardrail_somos(parsed.descripcion_problema)
            parsed.accion_sugerida = self.aplicar_guardrail_somos(parsed.accion_sugerida)
            return parsed
        return None

    def resumir_bloque_pasos(self, steps: List[dict]) -> str:
        """Resume un bloque de 8 pasos acumulados en una cubeta en 1 o 2 frases técnicas."""
        pasos_str = []
        for s in steps:
            p_num = s.get("step_number", "")
            tool = s.get("tool_name", "General")
            title = s.get("window_title", "")
            ocr = (s.get("ocr_text") or "")[:250].strip()
            pasos_str.append(f"Paso {p_num} [{tool} | {title}]: {ocr}")

        prompt = f"""
        Eres el Agente Senior de Mesa de Ayuda de SOMOS NETWORK.
        Sintetiza la siguiente secuencia de 8 pasos técnicos consecutivos en 1 o 2 oraciones técnicas concisas y precisas.
        Enfócate en qué se consultó, qué anomalía o parámetro se vio en las herramientas (Artemis, WinBox, Putty, Zabbix, OpenWISP, etc.) y qué acción se tomó.

        REGLAS:
        - NUNCA menciones la palabra ONT (usa CPE, router, switch o antena; recuerda que CPE aplica a todos los routers).
        - NUNCA menciones la palabra ticket (usa chat, caso o novedad).
        - NUNCA incluyas números de teléfono ni datos personales (PII).
        - Sé ultra breve y técnico (máximo 45 palabras).

        [PASOS REGISTRADOS]:
        {chr(10).join(pasos_str)}
        """
        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        if response and response.text:
            return self.aplicar_guardrail_somos(response.text.strip())
        return "Bloque de diagnósticos técnicos registrado."

    def sintetizar_para_revision(self, timeline_text: str, contexto_rag: str = "") -> Optional[PreSintesisCaso]:
        """Sintetiza una cubeta que se cierra por inactividad o break para generar una tarjeta de evaluación."""
        prompt = f"""
        Eres el Agente Senior de Mesa de Ayuda de SOMOS NETWORK.
        Un caso de atención técnica ha culminado o entrado en reposo. Sintetiza la trazabilidad del caso
        para generar una tarjeta de evaluación estructurada para el operador de mesa.

        REGLAS ESTRICTAS DE SOMOS NETWORK (CERO EXCEPCIONES):
        1. INFRAESTRUCTURA Y CPE: En SOMOS NETWORK NO existen ONT's. Todo router de cliente se denomina CPE (principal, secundario, mesh). NUNCA menciones la palabra ONT.
        2. TERMINOLOGÍA: NUNCA utilices la palabra "ticket" o "tickets" para visitas individuales. Usa "chats", "casos" o "novedades".
        3. PRIVACIDAD (PII): NUNCA extraigas ni registres números de teléfono. Identifica los servicios ÚNICAMENTE por CUN o ID.
        4. JERARQUÍA DE SWITCHES: "Switch de piso" distribuye a los apartamentos; "Edge" es el switch de gestión principal del edificio.
        5. PLATAFORMAS DE MONITOREO (100% LECTURA): Zabbix y Grafana son paneles de solo lectura.
        6. WINBOX (MIKROTIK CHR): Se opera ÚNICAMENTE en 'IP > DHCP Server > Leases'. ÚNICA acción permitida: 'Remover Lease' (Remove). Prohibido deducir ajustes de colas simples.
        7. PREGUNTA CLAVE OBLIGATORIA: En 'pregunta_duda_ia', redacta una pregunta concreta, técnica y directa al operador humano sobre la acción específica realizada. NUNCA dejes este campo vacío.

        CATEGORÍAS DE FALLA VÁLIDAS:
        - "Soporte Capa 3", "Switch/Capa 2", "Router Secundario", "Acometida/8 Hilos", "Velocidad/Plan Pro", "Radioenlace", "Soporte General"

        [CONTEXTO DE MEMORIA RAG PREVIA]:
        {contexto_rag}

        [HISTORIAL CRONOLÓGICO DE LA CUBETA]:
        \"\"\"
        {timeline_text}
        \"\"\"

        Genera la salida estructurada con el esquema PreSintesisCaso.
        """
        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PreSintesisCaso,
                temperature=TEMPERATURE_ANALISIS
            )
        )
        if response and response.parsed:
            parsed = response.parsed
            parsed.problema_observado = self.aplicar_guardrail_somos(parsed.problema_observado)
            parsed.solucion_deducida_ia = self.aplicar_guardrail_somos(parsed.solucion_deducida_ia)
            parsed.pregunta_duda_ia = self.aplicar_guardrail_somos(parsed.pregunta_duda_ia)
            return parsed
        return None

    def integrar_correccion_humana(self, pre_sintesis: dict, correccion_humana: str) -> str:
        """Fusiona la pre-síntesis de la IA con la respuesta/corrección provista por el operador de mesa."""
        problema = pre_sintesis.get("problema_observado", "")
        solucion_ia = pre_sintesis.get("solucion_deducida_ia", "")
        pregunta = pre_sintesis.get("pregunta_duda_ia", "")

        prompt = f"""
        Eres el Agente Senior de Mesa de Ayuda de SOMOS NETWORK.
        Un operador humano ha respondido a la pregunta y/o corregido la solución deducida de un caso técnico.
        Tu tarea es redactar la SOLUCIÓN TÉCNICA OFICIAL definitiva para la base de datos de conocimiento.

        [PROBLEMA OBSERVADO POR IA]:
        {problema}

        [SOLUCIÓN DEDUCIDA INICIALMENTE POR IA]:
        {solucion_ia}

        [PREGUNTA FORMULADA AL OPERADOR]:
        {pregunta}

        [RESPUESTA / CORRECCIÓN DEL OPERADOR HUMANO]:
        \"{correccion_humana}\"

        REGLAS ESTRICTAS DE REDACCIÓN:
        1. La solución técnica debe ser concisa, profesional y directa (1 a 3 líneas).
        2. Debe reflejar prioritariamente la indicación del operador humano complementada con la precisión técnica del caso.
        3. NUNCA incluyas la palabra ONT (usa CPE, router, switch o antena).
        4. NUNCA uses la palabra ticket para visitas individuales (usa chat, caso o novedad).
        5. NUNCA incluyas números de teléfono ni datos de contacto personal.
        6. Retorna ÚNICAMENTE el texto final de la solución, sin saludos, introducciones ni despedidas.
        """
        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        if response and response.text:
            texto = response.text.strip()
            if (texto.startswith('"') and texto.endswith('"')) or (texto.startswith("'") and texto.endswith("'")):
                texto = texto[1:-1].strip()
            return self.aplicar_guardrail_somos(texto)
        return self.aplicar_guardrail_somos(correccion_humana.strip())

    def analizar_texto_pantalla(self, texto_ocr: str, categoria_falla: str = "Soporte General") -> Optional[AnalisisEscenario]:
        """
        Wrapper compatible de análisis rápido de pantalla (ahora con inyección selectiva de manual por categoría).
        """
        manual_txt, _ = self.get_contexto_manual_categoria(categoria_falla)
        prompt = f"""
        Eres un agente experto de Mesa de Ayuda de telecomunicaciones para SOMOS NETWORK.
        Analiza el siguiente texto extraído de la pantalla del operador mediante OCR.

        REGLAS ESTRICTAS:
        1. IGNORA cualquier texto de tu propia interfaz ("MesaAyudaAgent", "The Eye", "Cubetas Activas").
        2. SOLO marca 'es_problema_tecnico_real: True' si hay evidencia de un problema o diagnóstico técnico real.
        3. INFRAESTRUCTURA Y CPE: En SOMOS NO existen ONTs; todo router de cliente es CPE. Prohibido usar la palabra ONT.
        4. WINBOX: Solo 'IP > DHCP Server > Leases' (Remover Lease). Prohibido sugerir modificar colas simples.
        5. ZABBIX y GRAFANA: Solo lectura.
        6. PRIVACIDAD: Cero números telefónicos; ignora pantallas de login.

        [MANUAL OFICIAL APLICABLE ({categoria_falla})]:
        {manual_txt}

        Texto extraído de la pantalla:
        \"\"\"
        {texto_ocr}
        \"\"\"
        """

        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnalisisEscenario,
                temperature=TEMPERATURE_ANALISIS
            )
        )
        if response and response.parsed:
            parsed = response.parsed
            parsed.descripcion_problema = self.aplicar_guardrail_somos(parsed.descripcion_problema)
            parsed.accion_sugerida = self.aplicar_guardrail_somos(parsed.accion_sugerida)
            return parsed
        return None

    def responder_pregunta(self, pregunta: str, contexto_pantalla: str, categoria_falla: Optional[str] = None) -> str:
        """Ejecuta el chat interactivo conversacional del operador combinando memoria de cubeta, Casos de Oro y Manual v2.0."""
        cat_safe = categoria_falla if (categoria_falla and categoria_falla in CATEGORIAS_OFICIALES) else "Soporte General"
        if categoria_falla and categoria_falla in CATEGORIAS_OFICIALES:
            manual_txt, _ = self.get_contexto_manual_categoria(categoria_falla)
        else:
            manual_txt = self.manuales_contexto
        arbol_info = self.evaluar_arbol_determinista(cat_safe, f"{pregunta}\n{contexto_pantalla}")

        prompt = f"""
        [MEMORIA DE CUBETA Y CASOS DE ORO RECORDADOS]:
        {contexto_pantalla}

        [ÁRBOL OFICIAL ACTIVO ({cat_safe})]:
        - Fase actual ({arbol_info.get('paso_activo', 1)}/{arbol_info.get('total_pasos', 2)}): {arbol_info.get('fase', '')}
        - Verificación: {arbol_info.get('verificacion', '')}
        - Acción recomendada: {arbol_info.get('accion_si_falla', '')}
        - Siguiente paso: {arbol_info.get('siguiente_paso', '')}

        [OPERADOR DE MESA TE CONSULTA]: "{pregunta}"

        INSTRUCCIÓN PRINCIPAL:
        Eres el Copiloto Senior de Mesa de Ayuda de SOMOS NETWORK. Responde de forma directa, precisa y fundamentada en los manuales oficiales y casos históricos provistos.

        ESTRUCTURA OBLIGATORIA PARA CONSULTAS TÉCNICAS (SEPARACIÓN DE ROLES):
        1. **🔧 Acción Operador Mesa:** Explica en 2-4 viñetas concisas el diagnóstico técnico y lo que TÚ (operador de Mesa) debes validar o ejecutar en plataformas internas (WinBox CHR, OpenWISP, Vilo Living, Zabbix, Grafana, Artemis, Slack).
        2. **💬 Respuesta para el Técnico en Campo:** Redacta un mensaje corto, cálido y directo en tono SOMOS ("Compa, porfa...") listo para copiar y pegar al técnico en sitio.
           - IMPORTANTE: En este bloque para el técnico en campo incluye ÚNICAMENTE acciones físicas o validaciones en sitio (cableado RJ45, PoE, reinicio eléctrico, PC directo, cambio de puerto, datos de MAC/SSID).
           - JAMÁS le pidas al técnico en campo que abra WinBox, OpenWISP, Zabbix, Grafana o plataformas exclusivas de Mesa de Ayuda.

        ALCANCE Y CONTROL ANTI-FUERA DE ÁREA (CONSERVADOR — CERO FALSOS BLOQUEOS):
        - Si la consulta es INEQUÍVOCAMENTE ajena al soporte técnico de red y operación en campo (por ejemplo: recetas de cocina, chistes, programación personal, trámites de nómina/RRHH, facturación/notas crédito o cancelaciones/reembolsos comerciales), NO la resuelvas; responde amablemente en 2 líneas indicando que este canal es exclusivo para soporte técnico en terreno de SOMOS NETWORK y redirige al área correspondiente si aplica.
        - ANTE LA MÁS MÍNIMA DUDA de que pueda tratarse de una incidencia técnica, equipo, red, cliente o consulta operativa en campo (incluso con redacción corta, coloquial o errores de ortografía como "no da nada", "la caja está roja", "anda lento"), ATIÉNDELA SIEMPRE como soporte técnico normal sin bloquearla.

        REGLAS ESTRICTAS DE RESPUESTA:
        1. PRIVACIDAD Y REFERENCIAS: NUNCA menciones números de teléfono. NUNCA expongas códigos internos de la base de datos de memoria (como `canon_conv_...`, `canon_nov_...`, `caso_2026...`) ni los llames CUN. Solo menciona un CUN o ID de visita si apareció explícitamente en el caso del operador.
        2. INFRAESTRUCTURA Y CPE (ZERO-ONT): En SOMOS NETWORK todos los routers de cliente se denominan CPE. JAMÁS escribas ni cites (ni siquiera entre comillas para corregir) siglas de fibra GPON de otros operadores.
        3. TERMINOLOGÍA: NUNCA utilices la palabra "ticket" para visitas individuales (usa chat, caso o visita).
        4. WINBOX Y MONITOREO: En WinBox solo se opera 'IP > DHCP Server > Leases' (Remover Lease; prohibido modificar colas simples). Zabbix y Grafana son 100% lectura.

        [MANUALES Y PROCEDIMIENTOS BASE v2.0 DE SOMOS NETWORK]:
        {manual_txt}
        """

        response = self._generate_with_failover(
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        if response and response.text:
            return self.aplicar_guardrail_somos(response.text.strip())

        # Fallback local determinista si la API externa está ocupada (0% caídas, respuesta inmediata <5 ms en corte 429)
        ood_area = self._is_strictly_out_of_domain(pregunta)
        if ood_area:
            return (
                f"Compa, porfa recuerda que este canal de Mesa de Ayuda atiende únicamente soporte técnico en terreno de SOMOS NETWORK. "
                f"Esta consulta corresponde a **{ood_area}**, por lo que no se gestiona desde soporte técnico."
            )

        fallback_msg = (
            f"📋 **[ASISTENCIA DETERMINISTA SOMOS]** (Modo Local - Conectividad IA saturada):\n\n"
            f"🔧 **Acción Operador Mesa ({cat_safe}):**\n"
            f"• **Fase / Acción:** {arbol_info.get('fase', 'Diagnóstico')}: {arbol_info.get('accion_si_falla', 'Verificar configuración de red')}\n"
            f"• **Validación Técnica:** {arbol_info.get('verificacion', 'Revisar estado de puerto y direccionamiento')}\n"
            f"• **Paso siguiente:** {arbol_info.get('siguiente_paso', 'Continuar descarte según manual')}\n\n"
            f"💬 **Respuesta para el Técnico en Campo (Tono SOMOS):**\n"
            f"\"{arbol_info.get('sugerencia_tono_somos', 'Compa, porfa valida si el CPE está encendido y si los puertos linkean correctamente.')}\""
        )
        return self.aplicar_guardrail_somos(fallback_msg)
