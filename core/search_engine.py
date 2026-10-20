import json
import os
from typing import Optional, List, Dict
from rank_bm25 import BM25Okapi
from core.config import CASES_DIR, UMBRAL_SIMILITUD_BM25
from core.database import HybridVectorDB
from core.logger import log

class MemorySearchEngine:
    """
    Motor de Búsqueda Híbrido (BM25Okapi + Base Vectorial SQLite).
    Permite recuperar casos históricos tanto por palabras clave como por afinidad semántica.
    """
    def __init__(self, db: Optional[HybridVectorDB] = None):
        self.cases_dir = CASES_DIR
        self.db = db or HybridVectorDB()
        self.corpus = []
        self.raw_cases = []
        self.bm25 = None
        self._load_and_index()

    def _load_and_index(self):
        """Indexa los casos desde SQLite para BM25 y memoria rápida."""
        self.corpus = []
        self.raw_cases = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT id_caso as id, fecha_creacion as timestamp, operador_mesa as tecnico_resolutor,
                       herramienta_origen as window_title, problema_detectado as visual_context,
                       sugerencia_ia as agent_suggestion, solucion_oficial as action,
                       categoria_falla, ciudad_nodo
                FROM casos
                ORDER BY fecha_creacion DESC
                LIMIT 500;
                """)
                rows = [dict(r) for r in cursor.fetchall()]

            for data in rows:
                texto_completo = (
                    f"{data.get('window_title', '')} "
                    f"{data.get('categoria_falla', '')} "
                    f"{data.get('visual_context', '')} "
                    f"{data.get('agent_suggestion', '')} "
                    f"{data.get('action', '')}"
                ).lower()
                self.raw_cases.append(data)
                self.corpus.append(texto_completo.split())

            if self.corpus:
                self.bm25 = BM25Okapi(self.corpus)
                log.info(f"Índice BM25 cargado con {len(self.corpus)} casos desde SQLite.")
        except Exception as e:
            log.warning(f"Fallo cargando BM25 desde SQLite ({e}), probando desde archivos JSON...")
            self._load_from_json_files()

    def _load_from_json_files(self):
        """Fallback secundario para cargar directamente desde archivos JSON."""
        if not os.path.exists(self.cases_dir):
            return
        try:
            archivos = [f for f in os.listdir(self.cases_dir) if f.endswith('.json')]
            for arch in archivos:
                try:
                    with open(os.path.join(self.cases_dir, arch), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        texto_completo = (
                            f"{data.get('window_title', '')} "
                            f"{data.get('visual_context', '')} "
                            f"{data.get('agent_suggestion', '')} "
                            f"{data.get('action', '')}"
                        ).lower()
                        self.raw_cases.append(data)
                        self.corpus.append(texto_completo.split())
                except Exception:
                    pass
            if self.corpus:
                self.bm25 = BM25Okapi(self.corpus)
        except Exception as e:
            log.error(f"Error cargando casos desde JSON: {e}")

    def search(self, query: str, top_k: int = 3, query_vector: Optional[List[float]] = None) -> List[Dict]:
        """
        Búsqueda de alta relevancia:
        1. Si hay query_vector disponible, intenta búsqueda semántica en la base de datos vectorial.
        2. Si no hay vector o no hay resultados con umbral alto, ejecuta BM25Okapi local.
        """
        if query_vector:
            vector_results = self.db.search_hybrid(query_text=query, query_vector=query_vector, top_k=top_k)
            if vector_results:
                # Normalizar keys para compatibilidad
                formatted = []
                for r in vector_results:
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

        # Búsqueda BM25
        self._load_and_index()
        if not self.bm25:
            return []

        tokenized_query = query.lower().split()
        doc_scores = self.bm25.get_scores(tokenized_query)
        indexed_scores = list(enumerate(doc_scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for i, score in indexed_scores[:top_k]:
            if score > UMBRAL_SIMILITUD_BM25:
                results.append(self.raw_cases[i])

        return results
