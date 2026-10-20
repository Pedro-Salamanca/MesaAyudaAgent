import os
import time
from typing import Optional, List, Dict
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from core.config import MODEL_NAME, TEMPERATURE_ANALISIS, MANUALS_DIR, GEMINI_API_KEY
from core.logger import log

class APIKeyPool:
    """
    Gestor de Pool de Claves de API con Rotación Automática y Failover.
    - Soporta múltiples API Keys para que cada técnico/sesión use su propia cuota (15 RPM independiente).
    - Detecta errores 429 (ResourceExhausted) o 503 y conmuta a claves de respaldo con enfriamiento de 60s.
    """
    def __init__(self):
        self.keys = []
        
        # 1. Clave primaria desde .env
        primary = os.getenv("GEMINI_API_KEY", "").strip()
        if primary:
            self.keys.append(primary)

        # 2. Claves de respaldo (separadas por comas en GEMINI_BACKUP_KEYS)
        backups = os.getenv("GEMINI_BACKUP_KEYS", "").strip()
        if backups:
            for k in backups.split(","):
                k_clean = k.strip()
                if k_clean and k_clean not in self.keys:
                    self.keys.append(k_clean)

        self.current_index = 0
        self.cooldowns: Dict[str, float] = {} # api_key -> timestamp hasta cuando está enfriando
        self.clients_cache: Dict[str, genai.Client] = {}

        log.info(f"Pool de API Keys inicializado con {len(self.keys)} clave(s) disponible(s).")

    def get_active_key(self) -> Optional[str]:
        """Selecciona la primera clave disponible fuera de enfriamiento."""
        if not self.keys:
            return None

        now = time.time()
        # Probar desde el índice actual
        for i in range(len(self.keys)):
            idx = (self.current_index + i) % len(self.keys)
            k = self.keys[idx]
            if now >= self.cooldowns.get(k, 0):
                self.current_index = idx
                return k

        # Si todas están en enfriamiento, retornar la que expire más pronto
        soonest_key = min(self.keys, key=lambda k: self.cooldowns.get(k, 0))
        return soonest_key

    def get_client(self) -> Optional[genai.Client]:
        """Obtiene o crea una instancia de genai.Client con la clave activa."""
        key = self.get_active_key()
        if not key:
            return None

        if key not in self.clients_cache:
            try:
                self.clients_cache[key] = genai.Client(api_key=key)
            except Exception as e:
                log.error(f"Error instanciando genai.Client con key terminada en ...{key[-4:] if len(key)>4 else key}: {e}")
                return None

        return self.clients_cache[key]

    def mark_rate_limited(self, cooldown_seconds: int = 60):
        """Pone la clave actual en enfriamiento temporal y avanza a la siguiente."""
        key = self.get_active_key()
        if key:
            self.cooldowns[key] = time.time() + cooldown_seconds
            log.warning(f"API Key (...{key[-4:] if len(key)>4 else key}) alcanzó límite de cuota (429/503). Entrando en enfriamiento por {cooldown_seconds}s. Conmutando...")
            # Rotar al siguiente índice
            self.current_index = (self.current_index + 1) % len(self.keys)


class AnalisisEscenario(BaseModel):
    """Esquema de salida estructurada JSON para el análisis visual instantáneo."""
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str = Field(description="Nombre del técnico si aparece 'Chat con [Nombre]'. Si no hay, 'Desconocido'.")
    descripcion_problema: str
    accion_sugerida: str
    nivel_confianza: str = Field(description="Alta, Media, Baja")


class AnalisisEpisodio(BaseModel):
    """
    Esquema de salida estructurada JSON para la síntesis de episodio completo (Cubeta finalizada).
    Clasifica parámetros delimitados para poblar la base de datos SQL y generar RAG preciso.
    """
    es_problema_tecnico_real: bool
    nombre_tecnico_en_sitio: str
    id_servicio_cun: str
    ciudad_nodo: str = Field(description="Nodo o ciudad (ej. MDE, BOG, RIVA, o Desconocido)")
    categoria_falla: str = Field(description="Categoría exacta: 'Router Secundario', 'Switch/Capa 2', 'Acometida/8 Hilos', 'Velocidad/Plan Pro', 'Radioenlace', 'Soporte General'")
    herramienta_origen: str = Field(description="Herramienta principal: Artemis, Putty, WinBox, Selene, Vilo, Zabbix, etc.")
    descripcion_problema: str
    ruta_diagnostico_pasos: str = Field(description="Resumen conciso de los pasos y herramientas seguidas en la cubeta")
    accion_sugerida: str
    nivel_confianza: str


class LLMClient:
    """
    Cliente de integración con Google Gemini y Embeddings de SOMOS NETWORK v3.0.
    - Soporta APIKeyPool para balanceo de carga y failover continuo.
    - Generación de vectores de embedding con `text-embedding-004`.
    - Síntesis de secuencias diagnósticas (Episodios) con esquema AnalisisEpisodio.
    - Compatibilidad total con análisis de texto de pantalla y chat interactivo con RAG.
    """
    def __init__(self):
        self.pool = APIKeyPool()
        self.model_name = MODEL_NAME
        self.embedding_model = "gemini-embedding-001"
        self.manuales_contexto = ""
        self.cargar_manuales()

        # Chat session local
        self.chat_session = None
        self._init_chat_session()

    def _init_chat_session(self):
        client = self.pool.get_client()
        if client:
            try:
                self.chat_session = client.chats.create(model=self.model_name)
                log.info(f"Sesión conversacional creada con el modelo {self.model_name}.")
            except Exception as e:
                log.warning(f"No se pudo crear la sesión de chat ({e}).")
                self.chat_session = None

    def cargar_manuales(self):
        """Lee todos los archivos .txt de la carpeta Manuales_Base e inyecta su contenido."""
        self.manuales_contexto = ""
        try:
            os.makedirs(MANUALS_DIR, exist_ok=True)
            archivos = [f for f in os.listdir(MANUALS_DIR) if f.endswith(".txt")]
            for arch in archivos:
                ruta_completa = os.path.join(MANUALS_DIR, arch)
                with open(ruta_completa, "r", encoding="utf-8") as f:
                    self.manuales_contexto += f"\n--- MANUAL: {arch} ---\n{f.read()}\n"
            log.info(f"Cargados {len(archivos)} manuales base desde {MANUALS_DIR}.")
        except Exception as e:
            log.error(f"Error cargando manuales operativos: {e}")

    def generar_embedding(self, texto: str) -> Optional[List[float]]:
        """
        Genera el vector semántico (embedding de 768 dimensiones) usando text-embedding-004.
        Incluye conmutación a claves de respaldo si ocurre saturación 429.
        """
        if not texto or not texto.strip():
            return None

        # Probar hasta 2 veces si hay rotación de clave
        for intento in range(max(1, len(self.pool.keys))):
            client = self.pool.get_client()
            if not client:
                return None

            try:
                clean_text = texto[:2000] # Limitar a 2000 caracteres para eficiencia
                response = client.models.embed_content(
                    model=self.embedding_model,
                    contents=clean_text
                )
                if response and hasattr(response, "embeddings") and response.embeddings:
                    values = response.embeddings[0].values
                    return list(values)
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "resource_exhausted" in err_msg or "503" in err_msg:
                    self.pool.mark_rate_limited(60)
                    time.sleep(0.5)
                    continue
                else:
                    log.warning(f"Fallo no recuperable generando embedding: {e}")
                    return None

        return None

    def sintetizar_episodio(self, timeline_text: str, contexto_rag: str = "") -> Optional[AnalisisEpisodio]:
        """
        Ejecuta la síntesis estructurada de un caso/cubeta completa.
        Analiza todos los pasos recopilados (Artemis -> Putty/WinBox -> Acciones) y clasifica
        parámetros delimitados bajo las reglas estrictas de SOMOS NETWORK.
        """
        prompt = f"""
        Eres el Agente Senior de Mesa de Ayuda de SOMOS NETWORK.
        Sintetiza la siguiente secuencia diagnóstica completa recopilada durante la atención técnica.
        
        REGLAS ESTRICTAS DE SOMOS NETWORK (CERO EXCEPCIONES):
        1. INFRAESTRUCTURA: En SOMOS NETWORK NO existen ONT's. Todo equipo de cliente es router, switch, antena o CPE. NUNCA menciones la palabra ONT.
        2. TERMINOLOGÍA: NUNCA utilices la palabra "ticket" o "tickets". En SOMOS NETWORK son "chats", "casos" o "novedades".
        3. PRIVACIDAD (PII): NUNCA extraigas ni registres números de teléfono ni de contacto personal. Identifica los servicios ÚNICAMENTE por CUN o ID.
        4. SEGURIDAD: IGNORA absolutamente cualquier formulario de inicio de sesión o contraseñas.
        
        CATEGORÍAS DE FALLA VÁLIDAS:
        - "Router Secundario" (problemas de DHCP, doble NAT, WiFi, puertos LAN en equipos del abonado)
        - "Switch/Capa 2" (puerto down, VLAN desalineada, PoE, switch bloqueado)
        - "Acometida/8 Hilos" (negociación a 10/100 Mbps, par abierto, conector rj45 degradado)
        - "Velocidad/Plan Pro" (perfil de ancho de banda, cola simple mikrotik, saturación)
        - "Radioenlace" (antena desalineada, señal débil, CCQ bajo, interferencia)
        - "Soporte General" (otras consultas o validaciones generales)
        
        [CONTEXTO DE MEMORIA RAG PREVIA]:
        {contexto_rag}
        
        [HISTORIAL CRONOLÓGICO DE LA CUBETA]:
        \"\"\"
        {timeline_text}
        \"\"\"
        
        Genera la salida estructurada con el esquema requerido.
        """

        for intento in range(max(1, len(self.pool.keys))):
            client = self.pool.get_client()
            if not client:
                return None

            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=AnalisisEpisodio,
                        temperature=TEMPERATURE_ANALISIS
                    )
                )
                return response.parsed
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "resource_exhausted" in err_msg or "503" in err_msg:
                    self.pool.mark_rate_limited(60)
                    time.sleep(0.5)
                    continue
                else:
                    log.error(f"Error al sintetizar episodio con Gemini: {e}")
                    return None

        return None

    def analizar_texto_pantalla(self, texto_ocr: str) -> Optional[AnalisisEscenario]:
        """
        Ejecuta el análisis visual instantáneo de la captura de pantalla con esquema AnalisisEscenario.
        """
        prompt = f"""
        Eres un agente experto de Mesa de Ayuda de telecomunicaciones (Nivel 1 y 2) para la empresa SOMOS NETWORK.
        Analiza el siguiente texto extraído de la pantalla del técnico mediante OCR.
        
        CONTEXTO DE HERRAMIENTAS: La empresa usa plataformas como Admin Somos, Monitoreo (Zabbix), ISP Somos / ISP ViloLiving, Security Somos, Reports Somos, UNF y macros de Google Apps Script, además de WinBox, MobaXterm, Putty y Artemis.
        
        Reglas IMPORTANTES (Anti-Bucle):
        1. IGNORA ABSOLUTAMENTE cualquier texto que provenga de tu propia interfaz (palabras como "MesaAyudaAgent", "Observation Panel", "The Eye", "Inference", "Chat", "Pausar", "Reanudar", "Agente:").
        2. IGNORA conversaciones de chat irrelevantes, pestañas del navegador ajenas al trabajo o búsquedas web comunes.
        3. SOLO marca 'es_problema_tecnico_real' como True si ves evidencia de un entorno de red real: logs claros de error, fallos en las plataformas mencionadas, alarmas, IPs caídas, puertos down, etc.
        
        Reglas de DOMINIO y PRIVACIDAD (Estrictas):
        - PRIVACIDAD (PII): NUNCA extraigas, analices ni menciones números de contacto ni teléfonos. Para identificar un caso, chat o cliente, utiliza ÚNICAMENTE el ID proporcionado por el técnico.
        - INFRAESTRUCTURA SOMOS: En la red de SOMOS NETWORK NO se utilizan ONT's. JAMÁS sugieras revisar, reiniciar o configurar una ONT. Refiérete a los equipos como routers, antenas o CPE, pero la palabra "ONT" está prohibida en tus diagnósticos.
        - TERMINOLOGÍA: NUNCA utilices la palabra "ticket" o "tickets" para referirte a la atención de los técnicos de campo. En SOMOS NETWORK los tickets son para otro tipo de escalamientos. Refiérete siempre a estas interacciones como "chats", "casos" o "novedades".
        - SEGURIDAD Y LOGINS: IGNORA ABSOLUTAMENTE cualquier formulario de inicio de sesión, login, ingreso de contraseñas o pantallas de autenticación. Si el texto en pantalla es un login o solicitud de credenciales, márcalo obligatoriamente con 'es_problema_tecnico_real: False'.
        
        [MANUALES Y PROCEDIMIENTOS BASE DE SOMOS NETWORK]
        {self.manuales_contexto}
        
        Texto extraído de la pantalla:
        \"\"\"
        {texto_ocr}
        \"\"\"
        """

        for intento in range(max(1, len(self.pool.keys))):
            client = self.pool.get_client()
            if not client:
                return None

            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=AnalisisEscenario,
                        temperature=TEMPERATURE_ANALISIS
                    )
                )
                return response.parsed
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "resource_exhausted" in err_msg or "503" in err_msg:
                    self.pool.mark_rate_limited(60)
                    time.sleep(0.5)
                    continue
                else:
                    log.error(f"Error en consulta estructurada a Gemini: {e}")
                    return None

        return None

    def responder_pregunta(self, pregunta: str, contexto_pantalla: str) -> str:
        """
        Ejecuta el chat interactivo conversacional del técnico combinando memoria visual y RAG.
        """
        prompt = f"""
        [MEMORIA VISUAL Y CASOS RAG RECORDADOS]:
        {contexto_pantalla}
        
        [TÉCNICO TE DICE]: "{pregunta}"
        
        INSTRUCCIÓN: Eres el asistente experto de soporte (Nivel 1 y 2). Responde de forma natural a la pregunta.
        
        REGLAS ESTRICTAS DE RESPUESTA:
        1. PRIVACIDAD: NUNCA menciones números de teléfono ni de contacto. Identifica los casos ÚNICAMENTE por su CUN o ID.
        2. INFRAESTRUCTURA: En SOMOS NETWORK NO hay ONT's. Jamás des una solución que implique revisar o configurar una ONT.
        3. TERMINOLOGÍA: NUNCA utilices la palabra "ticket" o "tickets" en tus respuestas. Usa siempre "chats", "casos" o "novedades".
        4. SEGURIDAD Y LOGINS: NUNCA proceses ni sugieras nada relacionado con pantallas de inicio de sesión, credenciales personales, usuarios o contraseñas.
        
        [MANUALES Y PROCEDIMIENTOS BASE DE SOMOS NETWORK]
        {self.manuales_contexto}
        """

        for intento in range(max(1, len(self.pool.keys))):
            client = self.pool.get_client()
            if not client:
                return "Error: No hay conexión con la IA (Falta API Key o cuotas agotadas)."

            try:
                # Si no hay chat session abierta con este cliente, abrir una
                if not self.chat_session:
                    self.chat_session = client.chats.create(model=self.model_name)

                response = self.chat_session.send_message(prompt)
                return response.text
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "resource_exhausted" in err_msg or "503" in err_msg:
                    self.pool.mark_rate_limited(60)
                    self.chat_session = None # Forzar nueva sesión con nueva clave
                    time.sleep(0.5)
                    continue
                else:
                    log.error(f"Error en chat conversacional de Gemini: {e}")
                    return "Lo siento, tuve un problema de conexión al intentar pensar la respuesta."

        return "🚨 La cuota de Gemini en todas las API Keys disponibles está temporalmente ocupada (429). Por favor espera un momento."
