import os
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict
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
    Cubeta de Atención Concurrente para Mesa de Ayuda.
    Mantiene aislada la trazabilidad de un técnico de campo específico,
    agrupando capturas cronológicas desde Artemis hasta Putty/WinBox/Selene.
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
        approved_suggestions: Optional[List[str]] = None,
        created_at: Optional[str] = None,
        last_updated: Optional[str] = None,
        status: str = "active"
    ):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.bucket_id = bucket_id
        self.tecnico_campo = tecnico_campo or "Técnico Desconocido"
        self.id_servicio_cun = id_servicio_cun or ""
        self.codigo_sitio = codigo_sitio or ""
        self.mac_addresses = mac_addresses or []
        self.ip_addresses = ip_addresses or []
        self.steps = steps or []
        # IMPORTANTE: Las sugerencias aprobadas se registran aquí y NO cierran la cubeta
        self.approved_suggestions = approved_suggestions or []
        self.created_at = created_at or now_str
        self.last_updated = last_updated or now_str
        self.status = status # 'active', 'ready_to_close', 'closed'

    def add_step(self, window_title: str, tool_name: str, ocr_text: str, summary: str = "") -> CaptureStep:
        """Agrega un nuevo paso a la línea de tiempo de la cubeta y actualiza su timestamp."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        step_num = len(self.steps) + 1
        step = CaptureStep(
            step_number=step_num,
            timestamp=now_str,
            window_title=window_title,
            tool_name=tool_name,
            ocr_text=ocr_text,
            summary=summary
        )
        self.steps.append(step)
        self.last_updated = now_str
        return step

    def matches_anchors(self, anchors: dict) -> float:
        """
        Calcula un puntaje de afinidad (0.0 a 1.0) para asociar una captura entrante a esta cubeta:
        - Si coincide el técnico de campo exacto: +0.8
        - Si coincide una dirección MAC: +0.9
        - Si coincide el código de sitio (nodo/torre): +0.7
        - Si coincide el CUN/ID de servicio: +0.85
        - Si coincide la dirección IP: +0.6
        """
        score = 0.0

        if anchors.get("tecnico_campo") and self.tecnico_campo != "Técnico Desconocido":
            if anchors["tecnico_campo"].lower() in self.tecnico_campo.lower() or self.tecnico_campo.lower() in anchors["tecnico_campo"].lower():
                score = max(score, 0.85)

        if anchors.get("id_servicio_cun") and self.id_servicio_cun:
            if anchors["id_servicio_cun"] == self.id_servicio_cun:
                score = max(score, 0.90)

        if anchors.get("codigo_sitio") and self.codigo_sitio:
            if anchors["codigo_sitio"].lower() in self.codigo_sitio.lower() or self.codigo_sitio.lower() in anchors["codigo_sitio"].lower():
                score = max(score, 0.80)

        in_macs = set(anchors.get("mac_addresses", []))
        my_macs = set(self.mac_addresses)
        if in_macs and my_macs and (in_macs & my_macs):
            score = max(score, 0.95)

        in_ips = set(anchors.get("ip_addresses", []))
        my_ips = set(self.ip_addresses)
        if in_ips and my_ips and (in_ips & my_ips):
            score = max(score, 0.65)

        return score

    def update_anchors(self, anchors: dict):
        """Enriquece los metadatos de la cubeta con nuevas anclas detectadas en herramientas posteriores."""
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
        Genera un informe cronológico completo de la secuencia diagnóstica para la síntesis de IA y RAG.
        """
        lines = [
            f"=== HISTORIAL DE TRAZABILIDAD (CASO: {self.bucket_id}) ===",
            f"Técnico en campo: {self.tecnico_campo}",
            f"ID/CUN Servicio: {self.id_servicio_cun or 'No especificado'}",
            f"Sitio/Nodo: {self.codigo_sitio or 'No especificado'}",
            f"Equipos MAC: {', '.join(self.mac_addresses) if self.mac_addresses else 'N/A'}",
            f"IPs: {', '.join(self.ip_addresses) if self.ip_addresses else 'N/A'}",
            f"Inicio: {self.created_at} | Último paso: {self.last_updated}",
            f"Total de pasos registrados: {len(self.steps)}",
            "-" * 60
        ]

        for s in self.steps:
            snippet = s.ocr_text.strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "... [truncado]"
            lines.append(f"[Paso {s.step_number}] {s.timestamp} | Herramienta: {s.tool_name} | Ventana: {s.window_title}")
            lines.append(f"Contenido Visual Extraído:\n{snippet}")
            if s.summary:
                lines.append(f"Resumen del paso: {s.summary}")
            lines.append("-" * 40)

        if self.approved_suggestions:
            lines.append("Sugerencias de la IA Aprobadas durante el proceso:")
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
            "approved_suggestions": self.approved_suggestions,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
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
            approved_suggestions=data.get("approved_suggestions", []),
            created_at=data.get("created_at"),
            last_updated=data.get("last_updated"),
            status=data.get("status", "active")
        )


class BucketManager:
    """
    Gestor de hasta 5 Cubetas Concurrentes para el Operador de Mesa de Ayuda.
    Enruta automáticamente las capturas según anclas de red o selección del operador,
    mantiene persistencia contra pérdidas (checkpoint) y gestiona tiempos de inactividad (TTL).
    """
    def __init__(self, max_concurrent: int = 5, ttl_minutes: int = 15):
        self.max_concurrent_buckets = max_concurrent
        self.ttl_minutes = ttl_minutes
        self.active_buckets: Dict[str, CaseBucket] = {}
        self.current_focused_bucket_id: Optional[str] = None
        
        # Archivo local/Google Drive para checkpointing
        self.checkpoint_dir = MEMORY_DIR if os.path.exists(MEMORY_DIR) else os.path.join(os.getcwd(), "data")
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.checkpoint_file = os.path.join(self.checkpoint_dir, "active_buckets_checkpoint.json")

        self.load_checkpoint_from_disk()

    def route_capture(self, window_title: str, ocr_text: str, anchors: dict) -> tuple[CaseBucket, bool]:
        """
        Enruta una captura entrante a la cubeta adecuada:
        1. Si hay coincidencia de anclas (Técnico, MAC, Sitio, CUN) con alguna cubeta activa con score >= 0.70.
        2. Si no coincide pero hay una cubeta enfocada manualmente en la UI, asocia a ella.
        3. Si viene de Artemis con un técnico nuevo y hay espacio (<5 cubetas), crea una nueva cubeta.
        4. Si no hay cubetas activas, crea la primera cubeta de trabajo.
        
        Retorna (cubeta, es_nuevo_paso).
        """
        matched_bucket: Optional[CaseBucket] = None
        best_score = 0.0

        for b in self.active_buckets.values():
            if b.status == "active":
                score = b.matches_anchors(anchors)
                if score > best_score:
                    best_score = score
                    matched_bucket = b

        # Umbral de coincidencia de anclas
        if matched_bucket and best_score >= 0.70:
            matched_bucket.update_anchors(anchors)
            self.current_focused_bucket_id = matched_bucket.bucket_id
            target_bucket = matched_bucket
        elif self.current_focused_bucket_id and self.current_focused_bucket_id in self.active_buckets:
            target_bucket = self.active_buckets[self.current_focused_bucket_id]
            target_bucket.update_anchors(anchors)
        else:
            # Crear nueva cubeta si estamos dentro del límite de 5 concurrentes
            target_bucket = self._create_new_bucket(anchors)

        # Agregar el paso
        tool_name = anchors.get("herramienta_origen", "General")
        step = target_bucket.add_step(
            window_title=window_title,
            tool_name=tool_name,
            ocr_text=ocr_text
        )

        self.save_checkpoint_to_disk()
        return target_bucket, True

    def _create_new_bucket(self, anchors: dict) -> CaseBucket:
        """Crea una nueva cubeta con un ID único cronológico y respeta el límite máximo."""
        # Si se superan las 5 cubetas concurrentes, intentar limpiar cubetas inactivas
        if len(self.active_buckets) >= self.max_concurrent_buckets:
            expired = self.check_expired_buckets()
            if not expired and len(self.active_buckets) >= self.max_concurrent_buckets:
                # Si ninguna expiró, reutilizar la más antigua
                oldest_id = min(self.active_buckets.keys(), key=lambda k: self.active_buckets[k].last_updated)
                log.warning(f"Límite de {self.max_concurrent_buckets} cubetas alcanzado. Cerrando automáticamente la más antigua ({oldest_id}).")
                self.close_and_extract_bucket(oldest_id)

        now = datetime.now()
        timestamp_slug = now.strftime("%Y%m%d_%H%M%S")
        tec_slug = (anchors.get("tecnico_campo") or "caso").replace(" ", "_")[:15]
        bucket_id = f"cubeta_{timestamp_slug}_{tec_slug}"

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
        log.info(f"Nueva cubeta creada: [{bucket_id}] para {new_bucket.tecnico_campo}.")
        return new_bucket

    def set_focused_bucket(self, bucket_id: str):
        """Permite al operador cambiar el foco activo haciendo clic en una pestaña o píldora de la UI."""
        if bucket_id in self.active_buckets:
            self.current_focused_bucket_id = bucket_id
            log.info(f"Foco de cubeta cambiado a: {bucket_id}")

    def get_focused_bucket(self) -> Optional[CaseBucket]:
        """Obtiene la cubeta que tiene el foco actual en la interfaz."""
        if self.current_focused_bucket_id and self.current_focused_bucket_id in self.active_buckets:
            return self.active_buckets[self.current_focused_bucket_id]
        if self.active_buckets:
            # Retornar la primera activa si el foco no está definido
            first_key = list(self.active_buckets.keys())[0]
            self.current_focused_bucket_id = first_key
            return self.active_buckets[first_key]
        return None

    def get_active_buckets_summary(self) -> list[dict]:
        """
        Retorna la lista de cubetas activas formateadas para renderizar las píldoras en la interfaz de usuario:
        [{'id': str, 'tecnico': str, 'sitio': str, 'steps_count': int, 'is_focused': bool}]
        """
        summary = []
        for b_id, b in self.active_buckets.items():
            if b.status == "active":
                summary.append({
                    "id": b.bucket_id,
                    "tecnico": b.tecnico_campo if b.tecnico_campo != "Técnico Desconocido" else "Técnico",
                    "sitio": b.codigo_sitio if b.codigo_sitio else (b.id_servicio_cun or "Caso"),
                    "steps_count": len(b.steps),
                    "is_focused": (b_id == self.current_focused_bucket_id)
                })
        return summary

    def check_expired_buckets(self) -> list[CaseBucket]:
        """
        Identifica cubetas con más de ttl_minutes (15 min) sin recibir nuevos pasos.
        Permite sugerir cierre automático o consolidación en segundo plano.
        """
        now = datetime.now()
        expired = []
        for b_id, b in list(self.active_buckets.items()):
            try:
                last_dt = datetime.strptime(b.last_updated, "%Y-%m-%d %H:%M:%S")
                if now - last_dt > timedelta(minutes=self.ttl_minutes):
                    expired.append(b)
            except Exception:
                pass
        return expired

    def detect_closed_chats_in_artemis(self, ocr_text: str) -> list[str]:
        """
        Inspecciona el panel lateral de 'Conversaciones activas' de Artemis.
        Si una cubeta activa ya no figura en la lista visible de chats, se marca para notificación de cierre.
        """
        closed_candidate_ids = []
        ocr_lower = ocr_text.lower()

        # Solo aplica si la pantalla leída es efectivamente la lista de chats de Artemis
        if "conversaciones" in ocr_lower or "artemis" in ocr_lower or "pendientes" in ocr_lower:
            for b_id, b in self.active_buckets.items():
                if b.status == "active" and b.tecnico_campo != "Técnico Desconocido":
                    first_name = b.tecnico_campo.split()[0].lower()
                    if first_name not in ocr_lower:
                        closed_candidate_ids.append(b_id)

        return closed_candidate_ids

    def close_and_extract_bucket(self, bucket_id: str) -> Optional[CaseBucket]:
        """
        Cierra formalmente una cubeta, cambia su estado a 'closed' y la remueve de las activas
        para ser persistida en la Base de Datos SQLite/Vectorial.
        """
        if bucket_id in self.active_buckets:
            bucket = self.active_buckets.pop(bucket_id)
            bucket.status = "closed"
            if self.current_focused_bucket_id == bucket_id:
                self.current_focused_bucket_id = list(self.active_buckets.keys())[0] if self.active_buckets else None
            self.save_checkpoint_to_disk()
            log.info(f"Cubeta [{bucket_id}] cerrada y extraída para almacenamiento persistente.")
            return bucket
        return None

    def save_checkpoint_to_disk(self):
        """Guarda el estado actual de todas las cubetas en disco para garantizar 0 pérdida ante reinicios."""
        try:
            data = {b_id: b.to_dict() for b_id, b in self.active_buckets.items()}
            with open(self.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Error guardando checkpoint de cubetas en disco: {e}")

    def load_checkpoint_from_disk(self):
        """Recupera cubetas activas guardadas previamente en caso de caída o reinicio."""
        if not os.path.exists(self.checkpoint_file):
            return

        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for b_id, b_data in data.items():
                if b_data.get("status") == "active":
                    bucket = CaseBucket.from_dict(b_data)
                    self.active_buckets[b_id] = bucket
            if self.active_buckets:
                self.current_focused_bucket_id = list(self.active_buckets.keys())[0]
                log.info(f"Checkpoint cargado exitosamente: {len(self.active_buckets)} cubetas activas restauradas.")
        except Exception as e:
            log.warning(f"No se pudo cargar checkpoint previo de cubetas: {e}")
