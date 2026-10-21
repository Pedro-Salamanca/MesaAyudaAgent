import os
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple
from core.config import MEMORY_DIR
from core.logger import log

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
        status: str = "active"
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

    def add_step(self, window_title: str, tool_name: str, ocr_text: str, summary: str = "") -> Tuple[CaptureStep, bool]:
        """
        Agrega un nuevo paso a la cubeta.
        Retorna (paso, necesita_resumen_bloque) donde necesita_resumen_bloque es True al llegar a 8 pasos.
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

        # Si llegamos a 8 pasos activos en crudo, activamos la bandera para compresión
        needs_compression = (len(self.steps) >= 8)
        return step, needs_compression

    def compress_current_block(self, summary_text: str):
        """
        Incorpora el resumen del bloque de 8 pasos y vacía la cola de pasos crudos
        para que la cubeta mantenga un peso ultraligero y el cierre final sea instantáneo.
        """
        block_num = len(self.partial_summaries) + 1
        clean_summary = summary_text.strip()
        self.partial_summaries.append(f"Bloque {block_num} (Pasos {self.total_steps_lifetime - len(self.steps) + 1} a {self.total_steps_lifetime}): {clean_summary}")
        # Vaciar pasos crudos una vez comprimidos
        self.steps.clear()
        log.info(f"Bloque #{block_num} comprimido para cubeta [{self.bucket_id}]. Pasos crudos liberados.")

    @property
    def herramientas_usadas(self) -> List[str]:
        tools = []
        for s in self.steps:
            if s.tool_name and s.tool_name != "General" and s.tool_name not in tools:
                tools.append(s.tool_name)
        return tools

    def matches_anchors(self, anchors: dict) -> float:
        """
        Calcula el puntaje de afinidad con Detección de Conflicto Duro:
        - Si el técnico entrante es DIFERENTE a este técnico: Retorna 0.0 (CONFLICO ABSOLUTO).
        - Si el CUN entrante es DIFERENTE a este CUN: Retorna 0.0 (CONFLICTO ABSOLUTO).
        - Si coincide Técnico, CUN, MAC o Sitio: Retorna puntaje positivo alto (>0.70).
        """
        in_tec = (anchors.get("tecnico_campo") or "").strip()
        in_cun = (anchors.get("id_servicio_cun") or "").strip()
        in_sitio = (anchors.get("codigo_sitio") or "").strip()
        in_macs = set(anchors.get("mac_addresses", []))
        in_ips = set(anchors.get("ip_addresses", []))

        # REGLA DE CONFLICTO DURO: Nunca mezclar técnicos distintos
        if in_tec and self.tecnico_campo != "Técnico Desconocido":
            # Si ambos tienen nombres definidos y no coinciden
            if in_tec.lower() not in self.tecnico_campo.lower() and self.tecnico_campo.lower() not in in_tec.lower():
                return 0.0

        # REGLA DE CONFLICTO DURO: Nunca mezclar CUNs distintos
        if in_cun and self.id_servicio_cun:
            if in_cun != self.id_servicio_cun:
                return 0.0

        # EVALUACIÓN DE AFINIDAD POSITIVA
        score = 0.0

        if in_macs and set(self.mac_addresses) and (in_macs & set(self.mac_addresses)):
            score = max(score, 0.95)

        if in_cun and self.id_servicio_cun and in_cun == self.id_servicio_cun:
            score = max(score, 0.90)

        if in_tec and self.tecnico_campo != "Técnico Desconocido":
            if in_tec.lower() in self.tecnico_campo.lower() or self.tecnico_campo.lower() in in_tec.lower():
                score = max(score, 0.85)

        if in_sitio and self.codigo_sitio:
            if in_sitio.lower() in self.codigo_sitio.lower() or self.codigo_sitio.lower() in in_sitio.lower():
                score = max(score, 0.80)

        if in_ips and set(self.ip_addresses) and (in_ips & set(self.ip_addresses)):
            score = max(score, 0.65)

        return score

    def update_anchors(self, anchors: dict):
        """Enriquece metadatos de la cubeta con nuevas anclas secundarias."""
        if anchors.get("tecnico_campo") and self.tecnico_campo == "Técnico Desconocido":
            self.tecnico_campo = anchors["tecnico_campo"]
        if anchors.get("id_servicio_cun") and not self.id_servicio_cun:
            self.id_servicio_cun = anchors["id_servicio_cun"]
        if anchors.get("codigo_sitio") and not self.codigo_sitio:
            self.codigo_sitio = anchors["codigo_sitio"]

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
            "status": self.status
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
            status=data.get("status", "active")
        )


class BucketManager:
    """
    Gestor de hasta 5 Cubetas Concurrentes con Enrutamiento Estricto Anti-Mezcla.
    - Cierre automático de cubeta por inactividad funcional a los 10 minutos.
    - Cierre automático del modo Solo Aprendizaje por inactividad global a los 30 minutos.
    - Descarte inmediato de capturas sin ancla verificada.
    """
    def __init__(self, max_concurrent: int = 5, ttl_bucket_minutes: int = 10, global_inactivity_minutes: int = 30):
        self.max_concurrent_buckets = max_concurrent
        self.ttl_bucket_minutes = ttl_bucket_minutes
        self.global_inactivity_minutes = global_inactivity_minutes
        self.active_buckets: Dict[str, CaseBucket] = {}
        self.current_focused_bucket_id: Optional[str] = None
        self.last_global_activity_time: float = time.time()
        
        # Archivo local/Google Drive para checkpointing
        self.checkpoint_dir = MEMORY_DIR if os.path.exists(MEMORY_DIR) else os.path.join(os.getcwd(), "data")
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.checkpoint_file = os.path.join(self.checkpoint_dir, "active_buckets_checkpoint.json")

        self.load_checkpoint_from_disk()

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
            # 3. Si no coincidió pero trae ancla nueva identificable, creamos una cubeta nueva
            has_new_identity = bool(anchors.get("tecnico_campo") or anchors.get("id_servicio_cun") or anchors.get("codigo_sitio"))
            if has_new_identity:
                target_bucket = self._create_new_bucket(anchors)
            else:
                # 4. ATRIBUCIÓN CONTINUA AL CASO EN CURSO (Herramientas técnicas y scroll de chat):
                # Si no trae ancla nueva pero hay una cubeta activa en foco (o una única cubeta activa):
                focused_b = self.get_focused_bucket()
                
                # Lista estricta de herramientas técnicas y diagnósticas reconocidas
                HERRAMIENTAS_TECNICAS = [
                    "winbox", "putty", "mobaxterm", "moba", "zabbix", "selene", 
                    "viloliving", "openwisp", "terminal", "powershell", "cmd",
                    "artemis", "somos", "mikrotik", "sheets", "spreadsheet"
                ]
                w_low = window_title.lower()
                t_name = (anchors.get("herramienta_origen") or "").lower()
                es_herramienta_tecnica = any(h in w_low or h in t_name for h in HERRAMIENTAS_TECNICAS)

                if focused_b and focused_b.status == "active" and es_herramienta_tecnica:
                    # Verificar que NO haya conflicto duro con otro técnico distinto
                    in_tec = (anchors.get("tecnico_campo") or "").strip()
                    if in_tec and focused_b.tecnico_campo != "Técnico Desconocido":
                        if in_tec.lower() not in focused_b.tecnico_campo.lower() and focused_b.tecnico_campo.lower() not in in_tec.lower():
                            log.info(f"Conflicto de técnico detectado ('{in_tec}' vs '{focused_b.tecnico_campo}'). Descartando para no mezclar.")
                            return None, False, False

                    # Atribuir paso diagnóstico a la cubeta en curso
                    target_bucket = focused_b
                    target_bucket.update_anchors(anchors)
                else:
                    log.info(f"Captura en [{window_title}] descartada por no haber cubeta activa o no ser herramienta técnica.")
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

        new_bucket = CaseBucket(
            bucket_id=bucket_id,
            tecnico_campo=anchors.get("tecnico_campo", "Técnico Desconocido"),
            id_servicio_cun=anchors.get("id_servicio_cun", ""),
            codigo_sitio=anchors.get("codigo_sitio", ""),
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
        """Persiste el estado activo en disco para tolerancia a caídas."""
        try:
            data = {b_id: b.to_dict() for b_id, b in self.active_buckets.items()}
            with open(self.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Error guardando checkpoint de cubetas: {e}")

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
