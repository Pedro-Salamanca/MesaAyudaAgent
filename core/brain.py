import os
import json
from datetime import datetime
from typing import Optional, List, Tuple
from core.config import CASES_DIR, IMAGES_DIR, MEMORY_DIR, MAX_CASOS_UI_MEMORIA
from core.database import HybridVectorDB
from core.logger import log

class AgentBrain:
    """
    Cerebro de Almacenamiento y Persistencia para MesaAyudaAgent v3.0.
    - Utiliza HybridVectorDB (SQLite WAL + Vector RAG) como repositorio primario estructurado.
    - Mantiene sincronización complementaria con JSON y PNG en Google Drive para compatibilidad total.
    """
    def __init__(self, db_path=None):
        self.memory_dir = MEMORY_DIR
        self.cases_dir = CASES_DIR
        self.images_dir = IMAGES_DIR
        self.db = HybridVectorDB(db_path=db_path)

        # Ingestar automáticamente casos legados si existen en el disco
        try:
            self.db.migrate_legacy_jsons()
        except Exception as e:
            log.warning(f"Error en verificación inicial de migración: {e}")

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
        Guarda un caso tanto en la base de datos estructurada SQLite como en el backup JSON + PNG.
        Aplica el filtro Gatekeeper para garantizar calidad técnica.
        """
        timestamp_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
        case_id = f"caso_{timestamp_slug}"
        image_filename = f"{case_id}.png"
        json_filename = f"{case_id}.json"

        # 1. Guardar imagen PNG si existe
        if image_obj:
            try:
                os.makedirs(self.images_dir, exist_ok=True)
                image_path = os.path.join(self.images_dir, image_filename)
                image_obj.save(image_path, "PNG")
            except Exception as e:
                log.error(f"Error guardando imagen {image_filename}: {e}")
                image_filename = ""
        else:
            image_filename = ""

        # 2. Guardar JSON legado para compatibilidad con exploradores de archivos
        legacy_data = {
            "id": case_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tecnico_resolutor": tecnico_nombre,
            "window_title": active_title,
            "visual_context": visual_context,
            "agent_suggestion": agent_suggestion,
            "action": action,
            "image_file": image_filename
        }
        try:
            os.makedirs(self.cases_dir, exist_ok=True)
            with open(os.path.join(self.cases_dir, json_filename), "w", encoding="utf-8") as f:
                json.dump(legacy_data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            log.warning(f"Error guardando JSON legado: {e}")

        # 3. Guardar en SQLite estructurado + Vector
        meta = extra_metadata or {}
        db_payload = {
            "id_caso": case_id,
            "fecha_creacion": legacy_data["timestamp"],
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
            log.info(f"Caso {case_id} guardado exitosamente en SQLite y disco por {tecnico_nombre}.")
            # Respaldo seguro asíncrono
            self.db.sync_backup_to_drive()
        return success

    def get_recent_cases(self, limit: int = 10) -> List[Tuple]:
        """Obtiene los últimos N casos registrados desde SQLite."""
        return self.db.get_all_cases(limit=limit)

    def get_all_cases(self) -> List[Tuple]:
        """Obtiene los casos para la pestaña de memoria de la interfaz gráfica."""
        return self.db.get_all_cases(limit=MAX_CASOS_UI_MEMORIA)

    def delete_case(self, case_id: str) -> bool:
        """Elimina un caso de SQLite, del JSON y de la imagen en disco."""
        return self.db.delete_case(case_id)

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
