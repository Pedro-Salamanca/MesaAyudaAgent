from typing import Optional, List, Dict
from core.database import HybridVectorDB
from core.logger import log


class MemorySearchEngine:
    """
    Motor de Búsqueda Piramidal Híbrido v4.0 (Caché Vectorial float32 + Pre-Filtro SQL + SQLite FTS5 en C).
    - Elimina la recarga repetitiva de BM25Okapi en Python en cada consulta (0 sobrecosto de RAM/CPU).
    - Combina Nivel 2 (~206 Casos Canónicos de Oro vectorizados en float32 BLOB) con
      Nivel 3 (~2,880 Casos Históricos indexados en SQLite FTS5 BM25 nativo en <2.1 ms).
    """
    def __init__(self, db: Optional[HybridVectorDB] = None):
        self.db = db or HybridVectorDB()

    def search(
        self,
        query: str,
        top_k: int = 3,
        query_vector: Optional[List[float]] = None,
        categoria_falla: Optional[str] = None
    ) -> List[Dict]:
        """
        Recupera los top_k casos más relevantes aplicando pre-filtrado por categoría oficial:
        1. Búsqueda vectorial ultrarrápida (0.06 a 5 ms) sobre Casos de Oro (Nivel 2).
        2. Respaldo y complemento automático sobre el índice histórico SQLite FTS5 (Nivel 3).
        """
        filters = {"categoria_falla": categoria_falla} if categoria_falla else None
        raw_results = self.db.search_hybrid(
            query_text=query,
            query_vector=query_vector,
            top_k=top_k,
            filters=filters
        )

        formatted = []
        for r in raw_results:
            formatted.append({
                "id": r.get("id_caso"),
                "timestamp": r.get("fecha_creacion"),
                "tecnico_resolutor": r.get("operador_mesa"),
                "window_title": r.get("herramienta_origen"),
                "visual_context": r.get("problema_detectado"),
                "agent_suggestion": r.get("sugerencia_ia"),
                "action": r.get("solucion_oficial"),
                "categoria_falla": r.get("categoria_falla")
            })
        return formatted
