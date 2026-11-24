import os
import re
import json
import time
import difflib
import unicodedata
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple
from core.config import MEMORY_DIR, LOCAL_APPDATA_DIR, LOCAL_CHECKPOINT_PATH
from core.logger import log


def normalize_name_tokens(name: str) -> List[str]:
    """
    Normaliza un nombre de técnico eliminando tildes, prefijos ('TS', 'Técnico') y
    retornando la lista de palabras significativas en minúsculas.
    Permite comparar 'Diego Leon Hinestroza Escobar' con 'Hinestroza Escobar Diego Leon'.
    """
    if not name:
        return []
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z]{2,}", ascii_str)
    ignore = {"ts", "tecnico", "desconocido", "en", "sitio", "chat", "con", "de", "del", "la"}
    return [w for w in words if w not in ignore]


def are_same_technician(name_a: str, name_b: str) -> Tuple[bool, float]:
    """
    Compara dos nombres de técnico sin importar el orden (Nombres+Apellidos vs Apellidos+Nombres).
    Retorna (es_el_mismo_tecnico, score_afinidad):
    - Si comparten >= 2 palabras o >= 60% del nombre más corto -> (True, 0.92).
    - Si uno es de 1 sola palabra (ej. 'Solarte') y está incluido en el otro -> (True, 0.88).
    - Si no comparten palabras -> (False, 0.0).
    """
    tokens_a = set(normalize_name_tokens(name_a))
    tokens_b = set(normalize_name_tokens(name_b))
    if not tokens_a or not tokens_b:
        return False, 0.0

    common = tokens_a & tokens_b
    if not common:
        return False, 0.0

    min_len = min(len(tokens_a), len(tokens_b))
    ratio = len(common) / float(min_len)

    if len(common) >= 2 or ratio >= 0.60 or (min_len == 1 and len(common) == 1):
        return True, 0.92
    return False, 0.0


def normalize_site_code(site: str) -> str:
    """
    Corrige confusiones ópticas frecuentes de Tesseract en códigos de sitio SOMOS:
    - 'SOMOS-STA-ROB3-T1-1007-1' -> 'SOMOS-BTA-ROB3-T1-1007-1'
    - 'SOMOS-MDE-PYCT-11-9603-1' -> 'SOMOS-MDE-PYCT-T1-9603-1'
    - 'SOMOS-MDE-PYCT-TI'        -> 'SOMOS-MDE-PYCT-T1'
    """
    if not site:
        return ""
    s = site.strip().upper()
    s = re.sub(r"^SOMOS-STA-", "SOMOS-BTA-", s)
    s = re.sub(r"^(SOMOS-[A-Z]{3}-[A-Z0-9]{3,5})-1(\d)(-\d{3,4}(?:-[A-Z0-9]+)*)$", r"\1-T\2\3", s)
    s = re.sub(r"^(SOMOS-[A-Z]{3}-[A-Z0-9]{3,5})-TI(?=-|\b)", r"\1-T1", s)
    return s


def parse_site_structure(site: str) -> dict:
    """
    Extrae la huella estructural de un código de sitio SOMOS:
    - node: Código del edificio/nodo (ej. 'PYCT', 'ROB3', 'JACO')
    - apt: Número de apartamento si está presente (ej. '9603', '1007', '1804')
    - canonical: Código normalizado
    """
    s = normalize_site_code(site)
    if not s:
        return {"node": "", "apt": "", "canonical": ""}

    parts = s.split("-")
    node = ""
    apt = ""
    if parts[0] == "SOMOS" and len(parts) >= 3:
        node = parts[2]
        # Buscar segmento de apartamento (3 o 4 dígitos como 9603, 1402, 1007, 1804, 804)
        for p in parts[3:]:
            if re.match(r"^\d{3,4}$", p):
                apt = p
                break
    elif len(parts) >= 2:
        node = parts[0]
        for p in parts[1:]:
            if re.match(r"^\d{3,4}$", p):
                apt = p
                break

    return {"node": node, "apt": apt, "canonical": s}


def are_same_site(site_a: str, site_b: str) -> Tuple[bool, float]:
    """
    Evalúa si dos códigos de sitio corresponden al mismo lugar tolerando ruido óptico OCR:
    - Torre 'T1' leída como '11' o 'TI'
    - Ciudad 'BTA' leída como 'STA'
    - 1 carácter OCR confundido en el nodo cuando el apartamento coincide exactamente
      (ej. 'ROB3-T1-1007' vs 'R9B3-T1-1007', o 'JACO-T1-1804' vs 'VACO-T1-1804' / 'ACO-T1-1804').
    - Cola truncada sin apartamento cuando el nodo coincide exactamente.
    """
    fa = parse_site_structure(site_a)
    fb = parse_site_structure(site_b)
    if not fa["node"] or not fb["node"]:
        return False, 0.0

    nodes_exact = (fa["node"] == fb["node"])

    # Caso 1: Ambos tienen apartamento explícito
    if fa["apt"] and fb["apt"]:
        if fa["apt"] != fb["apt"]:
            return False, 0.0
        if nodes_exact:
            return True, 0.92
        # Mismo apartamento exacto (ej. 1007 == 1007 o 1804 == 1804): tolerar 1 error OCR en el código de 3-4 letras del edificio
        node_sim = difflib.SequenceMatcher(None, fa["node"], fb["node"]).ratio()
        if node_sim >= 0.72:
            return True, 0.88
        return False, 0.0

    # Caso 2: Uno de los dos está truncado sin apartamento (ej. SOMOS-MDE-PYCT-TI vs SOMOS-MDE-PYCT-T1-9603-1)
    if nodes_exact:
        return True, 0.82
    return False, 0.0

@dataclass
class CaptureStep:
    """Representa una captura individual dentro de la secuencia de atención técnica."""
    step_number: int
    timestamp: str
    window_title: str
    tool_name: str
    ocr_text: str
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CaptureStep":
        return cls(**data)


class CaseBucket:
    """
    Cubeta de Atención Concurrente para Mesa de Ayuda (SOMOS NETWORK).
    - Aislamiento estricto por técnico/CUN (Cero mezcla de datos).
    - Compresión por bloques de 8 pasos (Rolling Block Summary) para memoria ultraligera.
    - Temporizador de inactividad individual (auto-cierre a los 10 minutos).
    """
    def __init__(
        self,
        bucket_id: str,
        tecnico_campo: str = "Técnico Desconocido",
        id_servicio_cun: str = "",
        codigo_sitio: str = "",
        mac_addresses: Optional[List[str]] = None,
        ip_addresses: Optional[List[str]] = None,
        steps: Optional[List[CaptureStep]] = None,
        partial_summaries: Optional[List[str]] = None,
        approved_suggestions: Optional[List[str]] = None,
        created_at: Optional[str] = None,
        last_updated: Optional[str] = None,
        total_steps_lifetime: int = 0,
        status: str = "active",
        categoria_actual: str = "Soporte General",
        last_inferred_step: int = 0,
        last_somos_suggestion: str = "",
        last_diagnosis_card: str = ""
    ):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.bucket_id = bucket_id
        self.tecnico_campo = tecnico_campo or "Técnico Desconocido"
        self.id_servicio_cun = id_servicio_cun or ""
        self.codigo_sitio = codigo_sitio or ""
        self.mac_addresses = mac_addresses or []
        self.ip_addresses = ip_addresses or []
        self.steps: List[CaptureStep] = steps or []
        # Resúmenes temporales de bloques de 8 pasos
        self.partial_summaries: List[str] = partial_summaries or []
        self.approved_suggestions: List[str] = approved_suggestions or []
        self.created_at = created_at or now_str
        self.last_updated = last_updated or now_str
        self.last_activity_time: float = time.time()
        self.total_steps_lifetime = total_steps_lifetime or len(self.steps)
        self.status = status # 'active', 'ready_to_close', 'closed'
        self.categoria_actual = categoria_actual or "Soporte General"
        self.last_inferred_step = last_inferred_step
        self.last_somos_suggestion = last_somos_suggestion
        self.last_diagnosis_card = last_diagnosis_card
        self.is_compressing = False

    def add_step(self, window_title: str, tool_name: str, ocr_text: str, summary: str = "") -> Tuple[CaptureStep, bool]:
        """
        Agrega un nuevo paso a la cubeta.
        Retorna (paso, necesita_resumen_bloque) donde necesita_resumen_bloque es True al llegar a 8 pasos
        y NO hay ya una compresión en curso (evita resúmenes duplicados 'Pasos 12 a 11').
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.total_steps_lifetime += 1
        step = CaptureStep(
            step_number=self.total_steps_lifetime,
            timestamp=now_str,
            window_title=window_title,
            tool_name=tool_name,
            ocr_text=ocr_text,
            summary=summary
        )
        self.steps.append(step)
        self.last_updated = now_str
        self.last_activity_time = time.time()

        # Si llegamos a 8 pasos activos en crudo y no estamos comprimiendo ya, disparamos compresión una sola vez
        needs_compression = (len(self.steps) >= 8 and not self.is_compressing)
        if needs_compression:
            self.is_compressing = True
        return step, needs_compression

    def compress_current_block(self, summary_text: str):
        """
        Incorpora el resumen del bloque de 8 pasos y vacía la cola de pasos crudos
        para que la cubeta mantenga un peso ultraligero y el cierre final sea instantáneo.
        """
        try:
            if not self.steps:
                return
            start_step = self.steps[0].step_number
            end_step = self.steps[-1].step_number
            block_num = len(self.partial_summaries) + 1
            clean_summary = summary_text.strip()
            self.partial_summaries.append(f"Bloque {block_num} (Pasos {start_step} a {end_step}): {clean_summary}")
            # Vaciar pasos crudos una vez comprimidos
            self.steps.clear()
            log.info(f"Bloque #{block_num} comprimido para cubeta [{self.bucket_id}] (Pasos {start_step}-{end_step}).")
        finally:
            self.is_compressing = False

    @property
    def herramientas_usadas(self) -> List[str]:
        tools = []
        for s in self.steps:
            if s.tool_name and s.tool_name != "General" and s.tool_name not in tools:
                tools.append(s.tool_name)
        return tools

    def matches_anchors(self, anchors: dict) -> float:
        """
        Calcula el puntaje de afinidad con Tolerancia a Orden de Nombre (Nombre+Apellido vs Apellido+Nombre)
        y Tolerancia a Errores OCR en Sitio ('-T1-' vs '-11-' vs '-TI'):
        1. Si ambos tienen técnico y NO comparten palabras: Retorna 0.0 (CONFLICTO DURO).
        2. Si ES EL MISMO TÉCNICO (por conjunto de palabras): Retorna >= 0.92 (Un técnico en campo
           solo atiende un caso a la vez; jamás abrirle dos cubetas al mismo técnico).
        3. Si coincide Sitio estructural (Nodo + Apto), CUN o MAC: Retorna puntaje alto (>= 0.82).
        """
        in_tec = (anchors.get("tecnico_campo") or "").strip()
        in_cun = (anchors.get("id_servicio_cun") or "").strip()
        in_sitio = normalize_site_code(anchors.get("codigo_sitio") or "")
        in_macs = set(anchors.get("mac_addresses", []))
        in_ips = set(anchors.get("ip_addresses", []))

        same_tec = False
        tec_score = 0.0
        if in_tec and self.tecnico_campo != "Técnico Desconocido":
            same_tec, tec_score = are_same_technician(in_tec, self.tecnico_campo)
            if not same_tec:
                # REGLA DE CONFLICTO DURO: Nombres de técnicos completamente distintos -> 0.0
                return 0.0

        same_site = False
        site_score = 0.0
        if in_sitio and self.codigo_sitio:
            same_site, site_score = are_same_site(in_sitio, self.codigo_sitio)
            # Si NO hay técnico confirmado que los una y los sitios tienen nodos/aptos distintos -> Conflicto
            if not same_site and not same_tec:
                fa = parse_site_structure(in_sitio)
                fb = parse_site_structure(self.codigo_sitio)
                if fa["node"] and fb["node"] and (fa["node"] != fb["node"] or (fa["apt"] and fb["apt"] and fa["apt"] != fb["apt"])):
                    return 0.0

        # Si es el MISMO técnico confirmado por sus tokens de nombre, jamás dividirlo en otra cubeta
        if same_tec:
            return max(0.92, site_score)

        # Si no venía nombre de técnico pero ambos tienen CUN explícito distinto y no comparten sitio ni MAC
        if in_cun and self.id_servicio_cun and in_cun != self.id_servicio_cun:
            if not same_site and not (in_macs and set(self.mac_addresses) and (in_macs & set(self.mac_addresses))):
                return 0.0

        score = 0.0
        if in_macs and set(self.mac_addresses) and (in_macs & set(self.mac_addresses)):
            score = max(score, 0.95)

        if in_cun and self.id_servicio_cun and in_cun == self.id_servicio_cun:
            score = max(score, 0.90)

        if same_site:
            score = max(score, site_score)

        if in_ips and set(self.ip_addresses) and (in_ips & set(self.ip_addresses)):
            score = max(score, 0.65)

        return score

    def update_anchors(self, anchors: dict):
        """Enriquece metadatos de la cubeta conservando el formato más completo y canónico."""
        in_tec = (anchors.get("tecnico_campo") or "").strip()
        if in_tec:
            clean_in_tec = " ".join(re.sub(r"^(?:ts|t[ée]cnico)\b[:\s]*", "", in_tec, flags=re.I).split()).title()
            if self.tecnico_campo == "Técnico Desconocido":
                self.tecnico_campo = clean_in_tec
            else:
                # Si el nuevo nombre es del mismo técnico pero tiene más palabras (ej. nombre completo vs un solo apellido), enriquecerlo
                same_t, _ = are_same_technician(clean_in_tec, self.tecnico_campo)
                if same_t and len(normalize_name_tokens(clean_in_tec)) > len(normalize_name_tokens(self.tecnico_campo)):
                    self.tecnico_campo = clean_in_tec

        if anchors.get("id_servicio_cun") and not self.id_servicio_cun:
            self.id_servicio_cun = anchors["id_servicio_cun"]

        in_sitio = normalize_site_code(anchors.get("codigo_sitio") or "")
        if in_sitio:
            if not self.codigo_sitio:
                self.codigo_sitio = in_sitio
            else:
                # Si es el mismo sitio pero el nuevo tiene apartamento completo (ej. SOMOS-MDE-PYCT-T1-9603-1 vs SOMOS-MDE-PYCT-T1), mejorarlo
                same_s, _ = are_same_site(in_sitio, self.codigo_sitio)
                if same_s and len(in_sitio) > len(self.codigo_sitio):
                    self.codigo_sitio = in_sitio

        for mac in anchors.get("mac_addresses", []):
            if mac not in self.mac_addresses:
                self.mac_addresses.append(mac)

        for ip in anchors.get("ip_addresses", []):
            if ip not in self.ip_addresses:
                self.ip_addresses.append(ip)

    def compile_timeline_text(self) -> str:
        """
        Consolida la trazabilidad completa combinando los resúmenes de bloques de 8 pasos
        con los últimos pasos activos restantes.
        """
        lines = [
            f"=== HISTORIAL DE ATENCIÓN (CUBETA: {self.bucket_id}) ===",
            f"Técnico en campo: {self.tecnico_campo}",
            f"ID/CUN Servicio: {self.id_servicio_cun or 'No especificado'}",
            f"Sitio/Nodo: {self.codigo_sitio or 'No especificado'}",
            f"Equipos MAC: {', '.join(self.mac_addresses) if self.mac_addresses else 'N/A'}",
            f"IPs: {', '.join(self.ip_addresses) if self.ip_addresses else 'N/A'}",
            f"Total de capturas acumuladas: {self.total_steps_lifetime}",
            "-" * 60
        ]

        # 1. Agregar resúmenes de bloques previos de 8 pasos
        if self.partial_summaries:
            lines.append("HISTORIAL DE BLOQUES RESUMIDOS:")
            for p_sum in self.partial_summaries:
                lines.append(f"• {p_sum}")
            lines.append("-" * 40)

        # 2. Agregar los últimos pasos crudos
        if self.steps:
            lines.append("PASOS FINALES DE LA ATENCIÓN:")
            for s in self.steps:
                snippet = s.ocr_text.strip()
                if len(snippet) > 300:
                    snippet = snippet[:300] + "... [resumido]"
                lines.append(f"[Paso {s.step_number}] {s.timestamp} | {s.tool_name} | {s.window_title}")
                lines.append(f"Texto: {snippet}")
                lines.append("-" * 30)

        if self.approved_suggestions:
            lines.append("Sugerencias intermedias aprobadas:")
            for idx, sug in enumerate(self.approved_suggestions, 1):
                lines.append(f"  {idx}. {sug}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "bucket_id": self.bucket_id,
            "tecnico_campo": self.tecnico_campo,
            "id_servicio_cun": self.id_servicio_cun,
            "codigo_sitio": self.codigo_sitio,
            "mac_addresses": self.mac_addresses,
            "ip_addresses": self.ip_addresses,
            "steps": [s.to_dict() for s in self.steps],
            "partial_summaries": self.partial_summaries,
            "approved_suggestions": self.approved_suggestions,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "total_steps_lifetime": self.total_steps_lifetime,
            "status": self.status,
            "categoria_actual": self.categoria_actual,
            "last_inferred_step": self.last_inferred_step,
            "last_somos_suggestion": self.last_somos_suggestion,
            "last_diagnosis_card": self.last_diagnosis_card
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaseBucket":
        steps_objs = [CaptureStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            bucket_id=data["bucket_id"],
            tecnico_campo=data.get("tecnico_campo", "Técnico Desconocido"),
            id_servicio_cun=data.get("id_servicio_cun", ""),
            codigo_sitio=data.get("codigo_sitio", ""),
            mac_addresses=data.get("mac_addresses", []),
            ip_addresses=data.get("ip_addresses", []),
            steps=steps_objs,
            partial_summaries=data.get("partial_summaries", []),
            approved_suggestions=data.get("approved_suggestions", []),
            created_at=data.get("created_at"),
            last_updated=data.get("last_updated"),
            total_steps_lifetime=data.get("total_steps_lifetime", len(steps_objs)),
            status=data.get("status", "active"),
            categoria_actual=data.get("categoria_actual", "Soporte General"),
            last_inferred_step=data.get("last_inferred_step", 0),
            last_somos_suggestion=data.get("last_somos_suggestion", ""),
            last_diagnosis_card=data.get("last_diagnosis_card", "")
        )


class BucketManager:
    """
    Gestor de hasta 5 Cubetas Concurrentes con Enrutamiento Estricto Anti-Mezcla.
    - Cierre automático de cubeta por inactividad funcional a los 10 minutos.
    - Cierre automático del modo Solo Aprendizaje por inactividad global a los 30 minutos.
    - Descarte inmediato de capturas sin ancla verificada.
    """
    def __init__(
        self,
        max_concurrent: int = 5,
        ttl_bucket_minutes: int = 10,
        global_inactivity_minutes: int = 30,
        restore_previous_session: bool = False,
        persist_checkpoint: bool = True
    ):
        self.max_concurrent_buckets = max_concurrent
        self.ttl_bucket_minutes = ttl_bucket_minutes
        self.global_inactivity_minutes = global_inactivity_minutes
        self.persist_checkpoint = persist_checkpoint
        self.active_buckets: Dict[str, CaseBucket] = {}
        self.current_focused_bucket_id: Optional[str] = None
        self.last_global_activity_time: float = time.time()

        # AISLAMIENTO ESTRICTO MULTI-OPERADOR v4.0:
        # Los checkpoints de cubetas JAMÁS se guardan en Google Drive (MEMORY_DIR) para que ningún
        # compañero vea ni interfiera con las cubetas de otro operador. Residen 100% en %LOCALAPPDATA%.
        self.checkpoint_dir = LOCAL_APPDATA_DIR
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.checkpoint_file = LOCAL_CHECKPOINT_PATH

        # Eliminar cualquier archivo legado que hubiese quedado en Google Drive
        legacy_drive_chk = os.path.join(MEMORY_DIR, "active_buckets_checkpoint.json")
        if os.path.exists(legacy_drive_chk):
            try:
                os.remove(legacy_drive_chk)
            except Exception:
                pass

        # Regla de Arranque Limpio: Al abrir el agente, inicia con 0 cubetas abiertas
        # hasta que ocurra la primera interacción real en Artemis/Selene en el turno actual.
        if restore_previous_session:
            self.load_checkpoint_from_disk()
        elif self.persist_checkpoint:
            if os.path.exists(self.checkpoint_file):
                try:
                    os.remove(self.checkpoint_file)
                except Exception:
                    pass

    def route_capture(self, window_title: str, ocr_text: str, anchors: dict) -> Tuple[Optional[CaseBucket], bool, bool]:
        """
        Enruta una captura entrante aplicando la política estricta de CERO CONTAMINACIÓN:
        1. Si coincide positivamente con una cubeta existente (score >= 0.70): Asocia a esa cubeta.
        2. Si no coincide pero trae un técnico/CUN/Sitio nuevo: Abre una NUEVA cubeta.
        3. Si no coincide con ninguna y no trae ancla clara: RETORNA (None, False, False)
           (Se descarta para no ensuciar ninguna cubeta).
        
        Retorna (cubeta_objetivo, es_nuevo_paso, necesita_resumen_bloque).
        """
        self.last_global_activity_time = time.time()

        # 1. Buscar coincidencia estricta entre cubetas activas
        matched_bucket: Optional[CaseBucket] = None
        best_score = 0.0

        for b in self.active_buckets.values():
            if b.status == "active":
                score = b.matches_anchors(anchors)
                if score > best_score:
                    best_score = score
                    matched_bucket = b

        # 2. Si hay coincidencia sólida, asignamos a esa cubeta
        if matched_bucket and best_score >= 0.70:
            target_bucket = matched_bucket
            target_bucket.update_anchors(anchors)
            self.current_focused_bucket_id = target_bucket.bucket_id
        else:
            # 3. GÉNESIS DE CUBETAS: ÚNICAMENTE DESDE CHATS REALES EN ARTEMIS / SELENE CON TÉCNICO VALIDADO
            # Nunca se crea una cubeta fantasma ("Técnico Desconocido") desde OpenWISP, LuCI, Zabbix o páginas de citas sin técnico.
            tool_name = (anchors.get("herramienta_origen") or "").lower()
            w_low = window_title.lower()
            o_low = (ocr_text or "").lower()

            es_herramienta_red = (
                tool_name in ["openwisp", "zabbix", "grafana", "winbox", "putty", "mobaxterm", "viloliving"] or
                any(k in w_low for k in ["openwisp", "luci", "change device", "zabbix", "grafana", "winbox"])
            )
            es_canal_atencion = (not es_herramienta_red) and (
                tool_name in ["artemis", "selene"] or
                any(k in w_low for k in ["artemis", "selene", "conversations"]) or
                any(k in o_low for k in ["cola asignada", "mesa de ayuda técnicos", "admin.somosinternet.com/conversation", "chat con "])
            )

            has_valid_technician = bool((anchors.get("tecnico_campo") or "").strip())

            if es_canal_atencion and has_valid_technician:
                target_bucket = self._create_new_bucket(anchors)
            else:
                # 4. ATRIBUCIÓN CONTINUA AL CASO EN CURSO (Herramientas técnicas y de diagnóstico):
                # Si no es un nuevo chat en Artemis, buscamos si hay una cubeta activa en foco
                focused_b = self.get_focused_bucket()
                
                # Lista estricta de herramientas técnicas y diagnósticas reconocidas
                HERRAMIENTAS_TECNICAS = [
                    "winbox", "putty", "mobaxterm", "moba", "zabbix", "selene", 
                    "viloliving", "openwisp", "luci", "terminal", "powershell", "cmd",
                    "artemis", "somos", "mikrotik", "sheets", "spreadsheet", "grafana"
                ]
                t_name = (anchors.get("herramienta_origen") or "").lower()
                es_herramienta_tecnica = any(h in w_low or h in t_name for h in HERRAMIENTAS_TECNICAS)

                if focused_b and focused_b.status == "active" and es_herramienta_tecnica:
                    # 4.1 Protección contra conflicto duro de técnico (tolerante a orden Nombre/Apellido)
                    in_tec = (anchors.get("tecnico_campo") or "").strip()
                    if in_tec and focused_b.tecnico_campo != "Técnico Desconocido":
                        same_t, _ = are_same_technician(in_tec, focused_b.tecnico_campo)
                        if not same_t:
                            log.info(f"Conflicto de técnico detectado ('{in_tec}' vs '{focused_b.tecnico_campo}'). Descartando para no mezclar.")
                            return None, False, False

                    # 4.2 Protección contra conflicto duro de sitio en herramientas de monitoreo/gestión (Grafana / Zabbix / OpenWISP / LuCI)
                    in_sitio = normalize_site_code(anchors.get("codigo_sitio") or "")
                    es_herramienta_equipo = any(m in w_low or m in t_name for m in ["grafana", "zabbix", "openwisp", "luci"])
                    if es_herramienta_equipo and in_sitio and focused_b.codigo_sitio:
                        same_s, _ = are_same_site(in_sitio, focused_b.codigo_sitio)
                        if not same_s:
                            log.info(f"Herramienta en sitio ajeno descartada ('{in_sitio}' vs caso en foco '{focused_b.codigo_sitio}').")
                            return None, False, False

                    # Atribuir paso diagnóstico a la cubeta en curso
                    target_bucket = focused_b
                    target_bucket.update_anchors(anchors)
                else:
                    if not focused_b:
                        log.info(f"Captura en [{window_title}] en espera: no hay cubeta activa iniciada por un chat con técnico en Artemis/Selene.")
                    else:
                        log.info(f"Captura en [{window_title}] descartada por no ser herramienta técnica.")
                    return None, False, False

        # Agregar paso a la cubeta objetivo
        tool_name = anchors.get("herramienta_origen", "General")
        step, needs_block_summary = target_bucket.add_step(
            window_title=window_title,
            tool_name=tool_name,
            ocr_text=ocr_text
        )

        self.save_checkpoint_to_disk()
        return target_bucket, True, needs_block_summary

    def _create_new_bucket(self, anchors: dict) -> CaseBucket:
        """Crea una cubeta independiente garantizando no exceder el límite de 5 concurrentes."""
        if len(self.active_buckets) >= self.max_concurrent_buckets:
            # Si se supera el límite de 5, cerramos la más inactiva
            oldest_id = min(self.active_buckets.keys(), key=lambda k: self.active_buckets[k].last_activity_time)
            log.warning(f"Límite de 5 cubetas alcanzado. Cerrando la más inactiva [{oldest_id}].")
            self.close_and_extract_bucket(oldest_id)

        now = datetime.now()
        timestamp_slug = now.strftime("%Y%m%d_%H%M%S")
        tec_raw = anchors.get("tecnico_campo") or "caso"
        tec_clean = "".join([c for c in tec_raw if c.isalnum() or c == "_"])[:15]
        bucket_id = f"cubeta_{timestamp_slug}_{tec_clean}"

        raw_tec = anchors.get("tecnico_campo") or "Técnico Desconocido"
        clean_tec = " ".join(re.sub(r"^(?:ts|t[ée]cnico)\b[:\s]*", "", raw_tec, flags=re.I).split()).title() if raw_tec != "Técnico Desconocido" else raw_tec
        clean_sitio = normalize_site_code(anchors.get("codigo_sitio") or "")

        new_bucket = CaseBucket(
            bucket_id=bucket_id,
            tecnico_campo=clean_tec,
            id_servicio_cun=anchors.get("id_servicio_cun", ""),
            codigo_sitio=clean_sitio,
            mac_addresses=anchors.get("mac_addresses", []),
            ip_addresses=anchors.get("ip_addresses", []),
            created_at=now.strftime("%Y-%m-%d %H:%M:%S")
        )

        self.active_buckets[bucket_id] = new_bucket
        self.current_focused_bucket_id = bucket_id
        log.info(f"Nueva cubeta creada con éxito: [{bucket_id}] para {new_bucket.tecnico_campo}.")
        return new_bucket

    def set_focused_bucket(self, bucket_id: str):
        """Cambia el foco del operador en la interfaz gráfica."""
        if bucket_id in self.active_buckets:
            self.current_focused_bucket_id = bucket_id

    def get_focused_bucket(self) -> Optional[CaseBucket]:
        """Obtiene la cubeta activa seleccionada en la interfaz."""
        if self.current_focused_bucket_id and self.current_focused_bucket_id in self.active_buckets:
            return self.active_buckets[self.current_focused_bucket_id]
        if self.active_buckets:
            first_key = list(self.active_buckets.keys())[0]
            self.current_focused_bucket_id = first_key
            return self.active_buckets[first_key]
        return None

    def get_active_buckets_summary(self) -> List[Dict]:
        """
        Retorna la lista de cubetas para la barra superior de píldoras:
        [{'id': str, 'tecnico': str, 'sitio': str, 'blocks_count': int, 'steps_count': int, 'inactive_min': int, 'is_focused': bool}]
        """
        summary = []
        now_ts = time.time()
        for b_id, b in self.active_buckets.items():
            if b.status == "active":
                inactive_minutes = int((now_ts - b.last_activity_time) / 60)
                summary.append({
                    "id": b.bucket_id,
                    "tecnico": b.tecnico_campo if b.tecnico_campo != "Técnico Desconocido" else "Técnico",
                    "sitio": b.codigo_sitio if b.codigo_sitio else (b.id_servicio_cun or "Caso"),
                    "blocks_count": len(b.partial_summaries),
                    "steps_count": len(b.steps),
                    "total_steps": b.total_steps_lifetime,
                    "inactive_min": inactive_minutes,
                    "is_focused": (b_id == self.current_focused_bucket_id)
                })
        return summary

    def check_expired_buckets(self) -> List[CaseBucket]:
        """
        Identifica cubetas con más de 10 minutos de inactividad funcional real
        para auto-cerrarlas y enviarlas a la bandeja de tarjetas de evaluación.
        """
        now_ts = time.time()
        expired = []
        threshold_seconds = self.ttl_bucket_minutes * 60

        for b_id, b in list(self.active_buckets.items()):
            if b.status == "active" and (now_ts - b.last_activity_time >= threshold_seconds):
                expired.append(b)

        return expired

    def check_global_inactivity(self) -> bool:
        """
        Verifica si la sesión general lleva 30 minutos continuos sin actividad
        para pasar automáticamente al modo de evaluación de aprendizaje.
        """
        now_ts = time.time()
        return (now_ts - self.last_global_activity_time >= self.global_inactivity_minutes * 60)

    def close_and_extract_bucket(self, bucket_id: str) -> Optional[CaseBucket]:
        """Cierra y extrae una cubeta de la memoria activa."""
        if bucket_id in self.active_buckets:
            bucket = self.active_buckets.pop(bucket_id)
            bucket.status = "closed"
            if self.current_focused_bucket_id == bucket_id:
                self.current_focused_bucket_id = list(self.active_buckets.keys())[0] if self.active_buckets else None
            self.save_checkpoint_to_disk()
            return bucket
        return None

    def save_checkpoint_to_disk(self):
        """Persiste el estado activo en el disco local exclusivo del operador (%LOCALAPPDATA%)."""
        if not getattr(self, "persist_checkpoint", True):
            return
        try:
            data = {b_id: b.to_dict() for b_id, b in self.active_buckets.items()}
            with open(self.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Error guardando checkpoint local de cubetas: {e}")

    def load_checkpoint_from_disk(self):
        """Restaura cubetas activas previas."""
        if not os.path.exists(self.checkpoint_file):
            return
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            for b_id, b_data in data.items():
                if b_data.get("status") == "active":
                    self.active_buckets[b_id] = CaseBucket.from_dict(b_data)
            if self.active_buckets:
                self.current_focused_bucket_id = list(self.active_buckets.keys())[0]
        except Exception as e:
            log.warning(f"No se pudo cargar checkpoint de cubetas: {e}")
