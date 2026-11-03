import os
import json
from datetime import datetime
from typing import Optional, List, Tuple
from core.config import MEMORY_DIR, MAX_CASOS_UI_MEMORIA
from core.database import HybridVectorDB
from core.logger import log

class AgentBrain:
    """
    Cerebro de Almacenamiento y Persistencia para MesaAyudaAgent v3.2.
    - Utiliza HybridVectorDB (SQLite WAL + Vector RAG) como repositorio centralizado estructurado.
    - Opera 100% sobre memory_v3.db sin dependencias de carpetas de archivos sueltos.
    """
    def __init__(self, db_path=None):
        self.memory_dir = MEMORY_DIR
        self.db = HybridVectorDB(db_path=db_path)

    def init_db(self):
        """Inicializa o verifica las tablas relacionales."""
        self.db.init_tables()

    def save_case(
        self,
        visual_context: str,
        action: str,
        agent_suggestion: str = "",
        image_obj = None,
        active_title: str = "",
        tecnico_nombre: str = "Técnico",
        vector: Optional[List[float]] = None,
        extra_metadata: Optional[dict] = None
    ) -> bool:
        """
        Guarda un caso estructurado y su vector semántico directamente en SQLite.
        Aplica el filtro Gatekeeper para garantizar calidad técnica.
        """
        timestamp_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
        case_id = f"caso_{timestamp_slug}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        meta = extra_metadata or {}
        db_payload = {
            "id_caso": case_id,
            "fecha_creacion": now_str,
            "operador_mesa": tecnico_nombre,
            "tecnico_campo": meta.get("tecnico_campo", "Técnico en Sitio"),
            "id_servicio_cun": meta.get("id_servicio_cun", ""),
            "herramienta_origen": meta.get("herramienta_origen") or active_title,
            "categoria_falla": meta.get("categoria_falla", "Soporte General"),
            "ciudad_nodo": meta.get("ciudad_nodo", "Desconocido"),
            "estado_validacion": meta.get("estado_validacion", "APROBADO_IA"),
            "problema_detectado": visual_context,
            "ruta_diagnostico_json": meta.get("ruta_diagnostico_json", "[]"),
            "sugerencia_ia": agent_suggestion,
            "solucion_oficial": action
        }

        success = self.db.insert_case(db_payload, vector=vector)
        if success:
            log.info(f"Caso {case_id} guardado exitosamente en SQLite por {tecnico_nombre}.")
            self.db.sync_backup_to_drive()
        return success

    def get_recent_cases(self, limit: int = 10) -> List[Tuple]:
        """Obtiene los últimos N casos registrados desde SQLite."""
        return self.db.get_all_cases(limit=limit)

    def get_all_cases(self) -> List[Tuple]:
        """Obtiene los casos para la pestaña de memoria de la interfaz gráfica."""
        return self.db.get_all_cases(limit=MAX_CASOS_UI_MEMORIA)

    def delete_case(self, case_id: str) -> bool:
        """Elimina un caso directamente de SQLite."""
        return self.db.delete_case(case_id)

    def get_all_manuales(self) -> List[dict]:
        """Obtiene todos los manuales operativos almacenados en SQLite."""
        return self.db.get_all_manuales()

    def insert_or_update_manual(self, id_manual: str, titulo: str, categoria: str, contenido: str, version: str = "1.0", vector: Optional[List[float]] = None) -> bool:
        """Inserta o actualiza un manual operativo en SQLite."""
        return self.db.insert_or_update_manual(id_manual, titulo, categoria, contenido, version=version, vector=vector)

    def delete_manual(self, id_manual: str) -> bool:
        """Elimina un manual operativo de SQLite."""
        return self.db.delete_manual(id_manual)

    def insert_staging_case(self, staging_data: dict, steps: Optional[List[dict]] = None) -> bool:
        """Inserta un caso pendiente en la tabla de pre-síntesis/staging."""
        return self.db.insert_staging_case(staging_data, steps=steps)

    def get_pending_staging_cases(self) -> List[dict]:
        """Obtiene todas las pre-síntesis pendientes de evaluación."""
        return self.db.get_pending_staging_cases()

    def get_staging_case_steps(self, id_cubeta: str) -> List[dict]:
        """Obtiene los pasos crudos de una cubeta en revisión."""
        return self.db.get_staging_case_steps(id_cubeta)

    def count_pending_staging(self) -> int:
        """Retorna la cantidad de tarjetas pendientes."""
        return self.db.count_pending_staging()

    def approve_staging_case(
        self,
        id_cubeta: str,
        solucion_final: str,
        operador: str = "Operador",
        categoria: Optional[str] = None,
        vector: Optional[List[float]] = None
    ) -> bool:
        """Aprueba un caso de staging y lo promueve a la memoria oficial de SQLite y disco."""
        return self.db.approve_staging_case(id_cubeta, solucion_final, operador=operador, categoria=categoria, vector=vector)

    def discard_staging_case(self, id_cubeta: str) -> bool:
        """Descarta y elimina permanentemente una tarjeta de staging."""
        return self.db.discard_staging_case(id_cubeta)
