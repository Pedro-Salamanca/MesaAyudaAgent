import os
import re
import json
import math
import struct
import sqlite3
import shutil
from contextlib import contextmanager
from array import array
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from core.config import (
    MEMORY_DIR,
    CASES_DIR,
    IMAGES_DIR,
    MAX_CASOS_UI_MEMORIA,
    LOCAL_APPDATA_DIR,
    LOCAL_CACHE_DB_PATH,
    LOCAL_STAGING_DB_PATH,
    CATEGORIAS_OFICIALES,
    CATEGORIA_A_MANUALES
)
from core.logger import log

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# Frases basura o de cortesía breves
FRASES_BASURA_GATEKEEPER = [
    "no aplica", "n/a", "na", "omitir", "ok", "vale", "listo", "gracias",
    "muchas gracias", "enterado", "entendido", "bueno", "bien", "de acuerdo",
    "nada", "ninguna", "ninguno", "test", "prueba", "probando", "ignorar",
    "cerrar", "siguiente", "ninguna solucion", "ninguna solución"
]

# Frases genéricas no accionables (relleno sin resolución técnica comprobada)
FRASES_GENERICAS_NO_ACCIONABLES = [
    "realizar seguimiento al cierre",
    "verificar el estado del servicio y dar continuidad",
    "continuar monitoreo del servicio",
    "monitorear el estado del servicio",
    "sin datos concluyentes",
    "realizar validacion tecnica del servicio segun reporte",
    "atención de caso de soporte general en sitio",
    "visualización de cubetas activas",
    "registro de cubeta inicial",
    "sin reporte de falla técnica específica"
]


class HybridVectorDB:
    """
    Base de Datos Híbrida Piramidal (Relacional SQL + BLOB Vectorial float32 + SQLite FTS5) v4.0:
    - Nivel 1 (`manuales_base`): 8 Manuales Base v2.0 con inyección selectiva por categoría.
    - Nivel 2 (`casos`): ~206 Casos Canónicos de Oro con vectores `float32 BLOB` pre-normalizados en caché RAM.
    - Nivel 3 (`historico_rag_fts` + `historico_rag_meta`): ~2,880 casos técnicos históricos indexados en C (FTS5 BM25 < 2.1 ms).
    - Espejo local de lectura rápida en `%LOCALAPPDATA%\\MesaAyudaAgent\\cache_v4.db` para cero conflictos WAL en Google Drive.
    """
    def __init__(self, db_path: Optional[str] = None):
        self.memory_dir = MEMORY_DIR
        self.cases_dir = CASES_DIR

        try:
            os.makedirs(self.memory_dir, exist_ok=True)
        except Exception:
            pass

        if db_path:
            self.db_path = db_path
        else:
            if os.path.exists(self.memory_dir):
                self.db_path = os.path.join(self.memory_dir, "memory_v3.db")
            else:
                local_dir = os.path.join(os.getcwd(), "data")
                os.makedirs(local_dir, exist_ok=True)
                self.db_path = os.path.join(local_dir, "memory_v3.db")

        # Espejo local de lectura rápida en SSD local y base local de Staging por Operador
        self.read_db_path = self.db_path
        self.staging_db_path = self.db_path
        self._use_local_mirror = False
        try:
            os.makedirs(LOCAL_APPDATA_DIR, exist_ok=True)
            # Si no es una DB de test temporal explicita, usar espejo local y staging local aislado por operador
            if db_path is None:
                self.staging_db_path = LOCAL_STAGING_DB_PATH
                if os.path.abspath(self.db_path).lower() != os.path.abspath(LOCAL_CACHE_DB_PATH).lower():
                    self._use_local_mirror = True
                    self.read_db_path = LOCAL_CACHE_DB_PATH
        except Exception:
            self._use_local_mirror = False
            self.read_db_path = self.db_path
            self.staging_db_path = self.db_path

        # Caché en memoria RAM de vectores float32 de Nivel 2 (Casos de Oro) agrupados por categoría
        self._vector_cache_loaded = False
        self._vector_cache_by_cat: Dict[str, List[Tuple[object, dict]]] = {}
        self._all_vector_cache: List[Tuple[object, dict]] = []

        self.init_tables()
        self.sync_local_read_mirror()

    @contextmanager
    def get_connection(self):
        """Conexión maestra de lectura/escritura hacia memory_v3.db con cierre garantizado (0 fugas de RAM)."""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def get_read_connection(self):
        """
        Conexión rápida de solo lectura desde el espejo local SSD (`cache_v4.db`)
        con cierre automático garantizado al salir del bloque `with` (0 fugas de memoria).
        """
        target = self.read_db_path if (self._use_local_mirror and os.path.exists(self.read_db_path)) else self.db_path
        conn = sqlite3.connect(target, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def get_staging_connection(self):
        """
        Conexión 100% local por PC/Operador (`staging_local_<OPERADOR_PC>.db`) para tarjetas de evaluación
        pendientes (`casos_por_revisar` y `pasos_cubeta`), garantizando que ningún compañero vea ni modifique
        las tarjetas de evaluación en curso de otro operador.
        """
        conn = sqlite3.connect(self.staging_db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def sync_local_read_mirror(self) -> bool:
        """Sincroniza atómicamente la base maestra hacia el espejo local de lectura rápida."""
        self._invalidate_vector_cache()
        if not self._use_local_mirror or not os.path.exists(self.db_path):
            return False
        try:
            src = sqlite3.connect(self.db_path, timeout=10.0)
            dst = sqlite3.connect(self.read_db_path, timeout=10.0)
            src.backup(dst)
            dst.close()
            src.close()
            return True
        except Exception as e:
            log.warning(f"No se pudo actualizar espejo local cache_v4.db (usando maestra): {e}")
            return False

    def _invalidate_vector_cache(self):
        self._vector_cache_loaded = False
        self._vector_cache_by_cat.clear()
        self._all_vector_cache.clear()

    @staticmethod
    def vector_to_blob(vector: Optional[List[float]]) -> Optional[bytes]:
        """Convierte una lista de floats en un BLOB binario float32 normalizado a longitud unitaria (12 KB para 3072d)."""
        if not vector:
            return None
        try:
            norm = math.sqrt(sum(x * x for x in vector))
            if norm > 0:
                normed = [x / norm for x in vector]
            else:
                normed = list(vector)
            return struct.pack(f'{len(normed)}f', *normed)
        except Exception as e:
            log.warning(f"Error serializando vector a float32 BLOB: {e}")
            return None

    @staticmethod
    def _prepare_query_vec(vec: List[float]):
        """Pre-normaliza el vector de consulta a unidad para que la similitud de coseno sea un producto punto directo."""
        norm = math.sqrt(sum(x * x for x in vec))
        normed = [x / norm for x in vec] if norm > 0 else list(vec)
        if _HAS_NUMPY:
            return np.array(normed, dtype=np.float32)
        return array('f', normed)

    def _ensure_vector_cache(self):
        """Carga los ~206 Casos de Oro de Nivel 2 en caché RAM float32 (~2.5 MB RAM)."""
        if self._vector_cache_loaded:
            return
        self._vector_cache_by_cat.clear()
        self._all_vector_cache.clear()
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
                           herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
                           problema_detectado, ruta_diagnostico_json, sugerencia_ia, solucion_oficial,
                           embedding_blob, embedding_json
                    FROM casos;
                """)
                for r in cursor.fetchall():
                    row = dict(r)
                    blob = row.pop("embedding_blob", None)
                    emb_json = row.pop("embedding_json", None)
                    vec_obj = None
                    if blob:
                        try:
                            if _HAS_NUMPY:
                                vec_obj = np.frombuffer(blob, dtype=np.float32)
                            else:
                                arr = array('f')
                                arr.frombytes(blob)
                                vec_obj = arr
                        except Exception:
                            vec_obj = None
                    elif emb_json:
                        try:
                            raw_v = json.loads(emb_json)
                            vec_obj = self._prepare_query_vec(raw_v)
                        except Exception:
                            vec_obj = None

                    if vec_obj is not None:
                        cat = row.get("categoria_falla") or "Soporte General"
                        self._vector_cache_by_cat.setdefault(cat, []).append((vec_obj, row))
                        self._all_vector_cache.append((vec_obj, row))
            self._vector_cache_loaded = True
        except Exception as e:
            log.error(f"Error cargando caché de vectores en RAM: {e}")

    def init_tables(self):
        """Crea o actualiza las tablas relacionales, columnas BLOB e índices FTS5."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS casos (
                    id_caso TEXT PRIMARY KEY,
                    fecha_creacion TEXT NOT NULL,
                    operador_mesa TEXT,
                    tecnico_campo TEXT,
                    id_servicio_cun TEXT,
                    herramienta_origen TEXT,
                    categoria_falla TEXT,
                    ciudad_nodo TEXT,
                    estado_validacion TEXT,
                    problema_detectado TEXT,
                    ruta_diagnostico_json TEXT,
                    sugerencia_ia TEXT,
                    solucion_oficial TEXT NOT NULL,
                    embedding_json TEXT,
                    sincronizado_backup INTEGER DEFAULT 0,
                    embedding_blob BLOB,
                    es_canonico INTEGER DEFAULT 0
                );
                """)

                # Evolución segura si la tabla casos existía de v3.2
                casos_cols = [r[1] for r in cursor.execute("PRAGMA table_info(casos);").fetchall()]
                if "embedding_blob" not in casos_cols:
                    cursor.execute("ALTER TABLE casos ADD COLUMN embedding_blob BLOB;")
                if "es_canonico" not in casos_cols:
                    cursor.execute("ALTER TABLE casos ADD COLUMN es_canonico INTEGER DEFAULT 0;")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS casos_por_revisar (
                    id_cubeta TEXT PRIMARY KEY,
                    fecha_inicio TEXT,
                    fecha_cierre TEXT,
                    tecnico_campo TEXT,
                    id_servicio_cun TEXT,
                    codigo_sitio TEXT,
                    mac_ip_detectadas TEXT,
                    herramientas_usadas TEXT,
                    categoria_deducida TEXT,
                    problema_observado TEXT,
                    solucion_deducida_ia TEXT,
                    pregunta_duda_ia TEXT,
                    resumen_bloques_json TEXT,
                    estado_revision TEXT DEFAULT 'PENDIENTE_REVISION'
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS pasos_cubeta (
                    id_paso INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_cubeta TEXT,
                    numero_paso INTEGER,
                    timestamp TEXT,
                    herramienta TEXT,
                    titulo_ventana TEXT,
                    texto_ocr_limpio TEXT
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS manuales_base (
                    id_manual TEXT PRIMARY KEY,
                    titulo TEXT NOT NULL,
                    categoria TEXT DEFAULT 'General',
                    contenido TEXT NOT NULL,
                    version TEXT DEFAULT '2.0',
                    fecha_actualizacion TEXT NOT NULL,
                    embedding_json TEXT,
                    embedding_blob BLOB
                );
                """)
                man_cols = [r[1] for r in cursor.execute("PRAGMA table_info(manuales_base);").fetchall()]
                if "embedding_blob" not in man_cols:
                    cursor.execute("ALTER TABLE manuales_base ADD COLUMN embedding_blob BLOB;")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS historico_rag_meta (
                    id_caso TEXT PRIMARY KEY,
                    id_conversacion INTEGER,
                    fecha TEXT,
                    ciudad TEXT,
                    tipo_cita TEXT,
                    categoria_falla TEXT,
                    problema TEXT,
                    respuestas_mesa TEXT,
                    solucion_sintetizada TEXT,
                    origen_dataset TEXT
                );
                """)

                cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS historico_rag_fts USING fts5(
                    id_caso UNINDEXED,
                    categoria_falla,
                    problema,
                    respuestas_mesa,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_caso TEXT NOT NULL,
                    operacion TEXT NOT NULL,
                    fecha TEXT NOT NULL,
                    estado TEXT DEFAULT 'PENDIENTE'
                );
                """)

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_categoria ON casos(categoria_falla);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_tecnico ON casos(tecnico_campo);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_cun ON casos(id_servicio_cun);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_fecha ON casos(fecha_creacion DESC);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_staging_estado ON casos_por_revisar(estado_revision);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_pasos_cubeta_id ON pasos_cubeta(id_cubeta);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_manuales_categoria ON manuales_base(categoria);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist_meta_cat ON historico_rag_meta(categoria_falla);")
                conn.commit()
                log.info(f"Base de datos SQLite v4.0 inicializada exitosamente en: {self.db_path}")

            # Asegurar tablas de staging en la base local aislada del operador
            if self.staging_db_path != self.db_path:
                with self.get_staging_connection() as sconn:
                    scur = sconn.cursor()
                    scur.execute("""
                    CREATE TABLE IF NOT EXISTS casos_por_revisar (
                        id_cubeta TEXT PRIMARY KEY,
                        fecha_inicio TEXT,
                        fecha_cierre TEXT,
                        tecnico_campo TEXT,
                        id_servicio_cun TEXT,
                        codigo_sitio TEXT,
                        mac_ip_detectadas TEXT,
                        herramientas_usadas TEXT,
                        categoria_deducida TEXT,
                        problema_observado TEXT,
                        solucion_deducida_ia TEXT,
                        pregunta_duda_ia TEXT,
                        resumen_bloques_json TEXT,
                        estado_revision TEXT DEFAULT 'PENDIENTE_REVISION'
                    );
                    """)
                    scur.execute("""
                    CREATE TABLE IF NOT EXISTS pasos_cubeta (
                        id_paso INTEGER PRIMARY KEY AUTOINCREMENT,
                        id_cubeta TEXT,
                        numero_paso INTEGER,
                        timestamp TEXT,
                        herramienta TEXT,
                        titulo_ventana TEXT,
                        texto_ocr_limpio TEXT
                    );
                    """)
                    scur.execute("CREATE INDEX IF NOT EXISTS idx_staging_estado ON casos_por_revisar(estado_revision);")
                    scur.execute("CREATE INDEX IF NOT EXISTS idx_pasos_cubeta_id ON pasos_cubeta(id_cubeta);")
                    sconn.commit()
        except Exception as e:
            log.error(f"Error inicializando tablas en {self.db_path}: {e}")

    @staticmethod
    def validate_gatekeeper(solucion: str, problema: str = "") -> Tuple[bool, str]:
        """
        Filtro de calidad estricto: evita registrar ruido, saludos, respuestas triviales
        o frases genéricas sin acción técnica real en la memoria oficial del agente.
        """
        if not solucion or not solucion.strip():
            return False, "La solución está vacía."

        texto_limpio = solucion.strip().lower()

        if len(texto_limpio) < 12:
            return False, f"La solución es demasiado corta ({len(texto_limpio)} caracteres). Debe ser una acción técnica clara."

        for basura in FRASES_BASURA_GATEKEEPER:
            if texto_limpio == basura or texto_limpio.startswith(basura + " ") or texto_limpio.endswith(" " + basura):
                return False, f"La solución contiene texto no procesable ('{basura}')."

        for generico in FRASES_GENERICAS_NO_ACCIONABLES:
            if generico in texto_limpio:
                return False, f"La solución es un texto genérico de relleno ('{generico}') sin acción técnica concreta."

        texto_completo = f"{problema} {solucion}".lower()
        terminos_agente = ["cubetas activas", "modo solo aprendizaje", "evaluar aprendizaje", "the eye", "ocr & recorte", "mesa de ayuda"]
        for tag in terminos_agente:
            if tag in texto_completo:
                return False, f"Caso descartado: Detectada captura autorreferencial del propio agente ('{tag}')."

        return True, ""

    def insert_case(self, case_data: dict, vector: Optional[List[float]] = None) -> bool:
        """
        Inserta o actualiza un caso en la base de datos de manera atómica.
        Aplica Gatekeeper sobre la solución oficial y serializa el embedding en float32 BLOB.
        """
        solucion = case_data.get("solucion_oficial") or case_data.get("action") or ""
        problema = case_data.get("problema_detectado") or ""
        valido, motivo = self.validate_gatekeeper(solucion, problema)
        if not valido:
            log.warning(f"Gatekeeper rechazó el caso {case_data.get('id_caso')}: {motivo}")
            return False

        id_caso = case_data.get("id_caso") or case_data.get("id") or f"caso_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        fecha_creacion = case_data.get("fecha_creacion") or case_data.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        operador_mesa = case_data.get("operador_mesa") or case_data.get("tecnico_resolutor") or "Operador"
        tecnico_campo = case_data.get("tecnico_campo") or "Técnico Desconocido"
        id_servicio_cun = case_data.get("id_servicio_cun") or ""
        herramienta_origen = case_data.get("herramienta_origen") or case_data.get("window_title") or "General"
        categoria_falla = case_data.get("categoria_falla") or "Soporte General"
        if categoria_falla not in CATEGORIAS_OFICIALES:
            categoria_falla = "Soporte General"
        ciudad_nodo = case_data.get("ciudad_nodo") or "Desconocido"
        estado_validacion = case_data.get("estado_validacion") or "APROBADO_IA"
        problema_detectado = case_data.get("problema_detectado") or case_data.get("visual_context") or ""
        sugerencia_ia = case_data.get("sugerencia_ia") or case_data.get("agent_suggestion") or ""

        ruta_diagnostico = case_data.get("ruta_diagnostico_json") or case_data.get("steps") or "[]"
        if not isinstance(ruta_diagnostico, str):
            ruta_diagnostico = json.dumps(ruta_diagnostico, ensure_ascii=False)

        embedding_blob = self.vector_to_blob(vector) if vector else None
        embedding_json = json.dumps(vector) if vector else None

        sql = """
        INSERT OR REPLACE INTO casos (
            id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
            herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
            problema_detectado, ruta_diagnostico_json, sugerencia_ia, solucion_oficial,
            embedding_json, sincronizado_backup, embedding_blob, es_canonico
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1);
        """

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (
                    id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
                    herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
                    problema_detectado, ruta_diagnostico, sugerencia_ia, solucion,
                    embedding_json, embedding_blob
                ))
                cursor.execute("""
                INSERT INTO sync_queue (id_caso, operacion, fecha, estado)
                VALUES (?, 'INSERT', ?, 'PENDIENTE');
                """, (id_caso, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            self.sync_local_read_mirror()
            log.info(f"Caso [{id_caso}] insertado con éxito en SQL+BLOB DB (Categoría: {categoria_falla}).")
            return True
        except Exception as e:
            log.error(f"Error al insertar caso {id_caso} en SQLite: {e}")
            return False

    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Calcula la similitud de coseno pura entre dos vectores flotantes."""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0
        dot = 0.0
        norm1 = 0.0
        norm2 = 0.0
        for a, b in zip(vec1, vec2):
            dot += a * b
            norm1 += a * a
            norm2 += b * b
        if norm1 <= 0.0 or norm2 <= 0.0:
            return 0.0
        return dot / (math.sqrt(norm1) * math.sqrt(norm2))

    @staticmethod
    def _fast_dot(q_vec, cached_vec) -> float:
        """Producto punto ultrarrápido sobre vectores float32 pre-normalizados (0.06 ms con NumPy / 2 ms con array)."""
        try:
            if _HAS_NUMPY and isinstance(q_vec, np.ndarray) and isinstance(cached_vec, np.ndarray):
                if q_vec.shape == cached_vec.shape:
                    return float(np.dot(q_vec, cached_vec))
                return 0.0
            if len(q_vec) != len(cached_vec):
                return 0.0
            return float(sum(a * b for a, b in zip(q_vec, cached_vec)))
        except Exception:
            return 0.0

    def search_fts5(
        self,
        query_text: str,
        categoria_falla: Optional[str] = None,
        top_k: int = 3
    ) -> List[Dict]:
        """
        Búsqueda BM25 en C nativo sobre Nivel 3 (`historico_rag_fts`, ~2,880 casos históricos depurados)
        en ~2 ms con 0 consumo de RAM de embeddings.
        """
        if not query_text or not query_text.strip():
            return []

        # Extraer tokens alfanuméricos relevantes (>2 chars) para construir consulta MATCH segura
        raw_tokens = re.findall(r'[a-zA-ZáéíóúñÁÉÍÓÚÑ0-9]{3,}', query_text.lower())
        stop = {
            "para", "como", "este", "esta", "estos", "pero", "porfa", "favor", "compa",
            "buen", "buenos", "dias", "tardes", "hola", "tiene", "tengo", "puede", "puedes"
        }
        tokens = []
        for t in raw_tokens:
            if t not in stop and t not in tokens:
                tokens.append(t)
            if len(tokens) >= 12:
                break

        if not tokens:
            return []

        fts_query = " OR ".join(f'"{t}"' for t in tokens)
        results = []
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                if categoria_falla and categoria_falla in CATEGORIAS_OFICIALES:
                    cursor.execute("""
                        SELECT m.id_caso, m.fecha as fecha_creacion, m.origen_dataset as operador_mesa,
                               m.ciudad as ciudad_nodo, m.tipo_cita as herramienta_origen,
                               m.categoria_falla, m.problema as problema_detectado,
                               m.respuestas_mesa as sugerencia_ia, m.solucion_sintetizada as solucion_oficial,
                               bm25(historico_rag_fts) as rank_score
                        FROM historico_rag_fts f
                        JOIN historico_rag_meta m ON f.id_caso = m.id_caso
                        WHERE historico_rag_fts MATCH ? AND m.categoria_falla = ?
                        ORDER BY rank_score
                        LIMIT ?;
                    """, (fts_query, categoria_falla, top_k))
                    rows = [dict(r) for r in cursor.fetchall()]
                else:
                    rows = []

                if len(rows) < top_k:
                    cursor.execute("""
                        SELECT m.id_caso, m.fecha as fecha_creacion, m.origen_dataset as operador_mesa,
                               m.ciudad as ciudad_nodo, m.tipo_cita as herramienta_origen,
                               m.categoria_falla, m.problema as problema_detectado,
                               m.respuestas_mesa as sugerencia_ia, m.solucion_sintetizada as solucion_oficial,
                               bm25(historico_rag_fts) as rank_score
                        FROM historico_rag_fts f
                        JOIN historico_rag_meta m ON f.id_caso = m.id_caso
                        WHERE historico_rag_fts MATCH ?
                        ORDER BY rank_score
                        LIMIT ?;
                    """, (fts_query, top_k))
                    seen = {r["id_caso"] for r in rows}
                    for r in cursor.fetchall():
                        rd = dict(r)
                        if rd["id_caso"] not in seen:
                            rows.append(rd)
                            seen.add(rd["id_caso"])
                        if len(rows) >= top_k:
                            break
                results = rows[:top_k]
        except Exception as e:
            log.warning(f"Error en búsqueda FTS5 Nivel 3: {e}")
        return results

    def search_hybrid(
        self,
        query_text: str,
        query_vector: Optional[List[float]] = None,
        top_k: int = 3,
        filters: Optional[Dict[str, str]] = None
    ) -> List[Dict]:
        """
        Búsqueda Híbrida Piramidal v4.0:
        1. Nivel 2 (Casos Canónicos de Oro): Pre-filtra por `categoria_falla` en la caché RAM `float32 BLOB`
           y calcula similitud de coseno en <1 ms (0.06 ms con NumPy).
        2. Nivel 3 (Histórico FTS5): Si faltan resultados o no hay vector, complementa en ~2 ms con SQLite FTS5.
        """
        cat_filter = (filters or {}).get("categoria_falla")
        results = []

        try:
            if query_vector:
                self._ensure_vector_cache()
                q_obj = self._prepare_query_vec(query_vector)

                # 1. Pre-filtro por categoría en caché RAM (~30-48 casos en 0.06 ms)
                candidate_pool = []
                if cat_filter and cat_filter in self._vector_cache_by_cat:
                    candidate_pool = self._vector_cache_by_cat[cat_filter]
                else:
                    candidate_pool = self._all_vector_cache

                scored = []
                for vec_obj, row in candidate_pool:
                    sim = self._fast_dot(q_obj, vec_obj)
                    scored.append((sim, row))

                scored.sort(key=lambda x: x[0], reverse=True)
                results = [r for sim, r in scored[:top_k] if sim > 0.38]

                # Si el pre-filtro por categoría dio menos de top_k con buen score, ampliar al resto de Casos de Oro
                if len(results) < top_k and cat_filter:
                    seen_ids = {r["id_caso"] for r in results}
                    all_scored = [(self._fast_dot(q_obj, v), r) for v, r in self._all_vector_cache if r["id_caso"] not in seen_ids]
                    all_scored.sort(key=lambda x: x[0], reverse=True)
                    for sim, r in all_scored:
                        if sim > 0.42:
                            results.append(r)
                        if len(results) >= top_k:
                            break

            # 2. Complementar con Nivel 3 (SQLite FTS5 BM25 sobre los 2,880 casos históricos)
            if len(results) < top_k:
                fts_hits = self.search_fts5(query_text, categoria_falla=cat_filter, top_k=top_k)
                seen_ids = {r.get("id_caso") for r in results}
                for fh in fts_hits:
                    if fh.get("id_caso") not in seen_ids:
                        results.append(fh)
                        seen_ids.add(fh.get("id_caso"))
                    if len(results) >= top_k:
                        break

            if results:
                return results[:top_k]

            # 3. Fallback léxico directo sobre tabla casos
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                if cat_filter:
                    cursor.execute("SELECT * FROM casos WHERE categoria_falla = ? LIMIT 60;", (cat_filter,))
                else:
                    cursor.execute("SELECT * FROM casos LIMIT 100;")
                rows = [dict(r) for r in cursor.fetchall()]

                terms = [w.lower() for w in query_text.split() if len(w) > 3]
                scored_lexical = []
                for row in rows:
                    text_blob = f"{row.get('problema_detectado', '')} {row.get('solucion_oficial', '')} {row.get('categoria_falla', '')}".lower()
                    score = sum(1 for term in terms if term in text_blob)
                    scored_lexical.append((score, row))
                scored_lexical.sort(key=lambda x: x[0], reverse=True)
                return [r for score, r in scored_lexical[:top_k] if score > 0] or [r for _, r in scored_lexical[:top_k]]
        except Exception as e:
            log.error(f"Error en búsqueda híbrida piramidal v4.0: {e}")
            return []

    def get_all_cases(self, limit: int = MAX_CASOS_UI_MEMORIA) -> List[Tuple]:
        """Retorna la lista de casos de Oro en el formato de tuplas para la pestaña Memoria Oficial de la UI."""
        cases = []
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT id_caso, problema_detectado, solucion_oficial, fecha_creacion
                FROM casos
                ORDER BY fecha_creacion DESC
                LIMIT ?;
                """, (limit,))
                for row in cursor.fetchall():
                    cases.append((
                        row["id_caso"],
                        row["problema_detectado"],
                        row["solucion_oficial"],
                        row["fecha_creacion"]
                    ))
        except Exception as e:
            log.error(f"Error obteniendo casos para la UI: {e}")
        return cases

    def delete_case(self, case_id: str) -> bool:
        """Elimina un caso de la base de datos maestra y actualiza el espejo local."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos WHERE id_caso = ?;", (case_id,))
                cursor.execute("""
                INSERT INTO sync_queue (id_caso, operacion, fecha, estado)
                VALUES (?, 'DELETE', ?, 'PENDIENTE');
                """, (case_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            self.sync_local_read_mirror()
            log.info(f"Caso [{case_id}] eliminado con éxito de SQLite.")
            return True
        except Exception as e:
            log.error(f"Error al eliminar caso {case_id}: {e}")
            return False

    def migrate_legacy_jsons(self) -> int:
        """
        Escanea la carpeta de casos legados (Cases) e ingesta archivos caso_*.json que no estén aún en SQLite.
        (Corregido bug de dominio: 'cpe' ya NO se clasifica erróneamente como Radioenlace).
        """
        migrated_count = 0
        if not os.path.exists(self.cases_dir):
            return 0

        archivos = [f for f in os.listdir(self.cases_dir) if f.endswith(".json")]
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id_caso FROM casos;")
                existing_ids = set(r["id_caso"] for r in cursor.fetchall())

                for fname in archivos:
                    cid = fname.replace(".json", "")
                    if cid in existing_ids:
                        continue

                    fpath = os.path.join(self.cases_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)

                        solucion = data.get("action", "").strip()
                        v_ctx = data.get("visual_context", "")
                        valido, _ = self.validate_gatekeeper(solucion, v_ctx)
                        if not valido:
                            continue

                        w_title = data.get("window_title", "")
                        herramienta = "General"
                        if "artemis" in w_title.lower(): herramienta = "Artemis"
                        elif "putty" in w_title.lower(): herramienta = "Putty"
                        elif "winbox" in w_title.lower(): herramienta = "WinBox"
                        elif "zabbix" in w_title.lower(): herramienta = "Zabbix"

                        v_low = v_ctx.lower()
                        categoria = "Soporte General"
                        if "radioenlace" in v_low or "antena" in v_low or "ccq" in v_low:
                            categoria = "Radioenlace"
                        elif "acometida" in v_low or "8 hilos" in v_low or "100 mbps" in v_low or "rj45" in v_low:
                            categoria = "Acometida/8 Hilos"
                        elif "switch" in v_low or "puerto" in v_low or "montante" in v_low:
                            categoria = "Switch/Capa 2"
                        elif "velocidad" in v_low or "speedtest" in v_low or "radius" in v_low:
                            categoria = "Velocidad/Plan Pro"
                        elif "router" in v_low or "cpe" in v_low or "openwisp" in v_low or "wifi" in v_low:
                            categoria = "Router Secundario"

                        cursor.execute("""
                        INSERT OR IGNORE INTO casos (
                            id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
                            herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
                            problema_detectado, ruta_diagnostico_json, sugerencia_ia, solucion_oficial,
                            embedding_json, sincronizado_backup
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1);
                        """, (
                            cid,
                            data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            data.get("tecnico_resolutor", "Operador"),
                            "Técnico en Sitio",
                            "",
                            herramienta,
                            categoria,
                            "Desconocido",
                            "LEGACY_JSON",
                            v_ctx,
                            "[]",
                            data.get("agent_suggestion", ""),
                            solucion
                        ))
                        migrated_count += 1
                    except Exception as err_file:
                        log.warning(f"Error migrando {fname}: {err_file}")

                conn.commit()
            if migrated_count > 0:
                self.sync_local_read_mirror()
        except Exception as e:
            log.error(f"Error en ciclo de migración JSON a SQLite: {e}")

        return migrated_count

    def sync_backup_to_drive(self) -> bool:
        """Ejecuta checkpoint WAL pasivo y sincroniza espejo local."""
        try:
            with self.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            self.sync_local_read_mirror()
            return True
        except Exception as e:
            log.error(f"Error ejecutando checkpoint WAL: {e}")
            return False

    # =========================================================================
    # GESTIÓN DE MANUALES Y PROCEDIMIENTOS BASE v2.0 (INYECCIÓN SELECTIVA)
    # =========================================================================
    def insert_or_update_manual(
        self,
        id_manual: str,
        titulo: str,
        categoria: str,
        contenido: str,
        version: str = "2.0",
        vector: Optional[List[float]] = None
    ) -> bool:
        """Inserta o actualiza un manual operativo con su vector semántico en float32 BLOB y JSON."""
        try:
            embedding_str = json.dumps(vector) if vector else None
            embedding_blob = self.vector_to_blob(vector) if vector else None
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                INSERT INTO manuales_base (id_manual, titulo, categoria, contenido, version, fecha_actualizacion, embedding_json, embedding_blob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id_manual) DO UPDATE SET
                    titulo = excluded.titulo,
                    categoria = excluded.categoria,
                    contenido = excluded.contenido,
                    version = excluded.version,
                    fecha_actualizacion = excluded.fecha_actualizacion,
                    embedding_json = COALESCE(excluded.embedding_json, manuales_base.embedding_json),
                    embedding_blob = COALESCE(excluded.embedding_blob, manuales_base.embedding_blob);
                """, (id_manual, titulo, categoria, contenido, version, now_str, embedding_str, embedding_blob))
                conn.commit()
            self.sync_local_read_mirror()
            log.info(f"Manual [{id_manual}] '{titulo}' guardado/actualizado exitosamente en manuales_base.")
            return True
        except Exception as e:
            log.error(f"Error insertando manual [{id_manual}]: {e}")
            return False

    def get_all_manuales(self) -> List[dict]:
        """Retorna todos los manuales almacenados en manuales_base desde el espejo de lectura."""
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id_manual, titulo, categoria, contenido, version, fecha_actualizacion FROM manuales_base ORDER BY categoria, titulo;")
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error(f"Error obteniendo manuales base: {e}")
            return []

    def get_manuales_by_categoria(self, categoria_falla: str) -> List[dict]:
        """
        Retorna únicamente el/los manuales base asociados a la categoría oficial detectada,
        ahorrando entre 75% y 83% de tokens de entrada en cada llamada a Gemini.
        """
        manual_ids = CATEGORIA_A_MANUALES.get(categoria_falla, ["checklist_validaciones_sitio"])
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                placeholders = ",".join("?" for _ in manual_ids)
                cursor.execute(
                    f"SELECT id_manual, titulo, categoria, contenido, version FROM manuales_base WHERE id_manual IN ({placeholders});",
                    tuple(manual_ids)
                )
                rows = [dict(r) for r in cursor.fetchall()]
                if rows:
                    return rows
                return self.get_all_manuales()[:2]
        except Exception as e:
            log.error(f"Error obteniendo manuales para categoría {categoria_falla}: {e}")
            return []

    def get_manual_by_id(self, id_manual: str) -> Optional[dict]:
        """Obtiene un manual por su identificador único."""
        try:
            with self.get_read_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id_manual, titulo, categoria, contenido, version, fecha_actualizacion FROM manuales_base WHERE id_manual = ?;", (id_manual,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            log.error(f"Error obteniendo manual [{id_manual}]: {e}")
            return None

    def delete_manual(self, id_manual: str) -> bool:
        """Elimina un manual de la base de datos."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM manuales_base WHERE id_manual = ?;", (id_manual,))
                conn.commit()
            self.sync_local_read_mirror()
            log.info(f"Manual [{id_manual}] eliminado de manuales_base.")
            return True
        except Exception as e:
            log.error(f"Error eliminando manual [{id_manual}]: {e}")
            return False

    def insert_staging_case(self, staging_data: dict, steps: Optional[List[dict]] = None) -> bool:
        """Inserta una pre-síntesis de caso en la base local aislada del operador ('casos_por_revisar' y 'pasos_cubeta')."""
        id_cubeta = staging_data.get("id_cubeta")
        if not id_cubeta:
            log.warning("insert_staging_case: Falta id_cubeta.")
            return False

        sql = """
        INSERT OR REPLACE INTO casos_por_revisar (
            id_cubeta, fecha_inicio, fecha_cierre, tecnico_campo, id_servicio_cun,
            codigo_sitio, mac_ip_detectadas, herramientas_usadas, categoria_deducida,
            problema_observado, solucion_deducida_ia, pregunta_duda_ia,
            resumen_bloques_json, estado_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDIENTE_REVISION');
        """
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                bloques_json = staging_data.get("resumen_bloques_json")
                if not bloques_json:
                    bloques = staging_data.get("resumen_bloques", [])
                    bloques_json = json.dumps(bloques, ensure_ascii=False) if isinstance(bloques, (list, dict)) else "[]"

                cursor.execute(sql, (
                    id_cubeta,
                    staging_data.get("fecha_inicio", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    staging_data.get("fecha_cierre", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    staging_data.get("tecnico_campo", "Técnico en Sitio"),
                    staging_data.get("id_servicio_cun", ""),
                    staging_data.get("codigo_sitio", ""),
                    staging_data.get("mac_ip_detectadas", ""),
                    staging_data.get("herramientas_usadas", "General"),
                    staging_data.get("categoria_deducida", "Soporte General"),
                    staging_data.get("problema_observado", ""),
                    staging_data.get("solucion_deducida_ia", ""),
                    staging_data.get("pregunta_duda_ia", "¿Qué acción técnica específica se realizó en este caso?"),
                    bloques_json
                ))

                if steps:
                    for s in steps:
                        cursor.execute("""
                        INSERT INTO pasos_cubeta (id_cubeta, numero_paso, timestamp, herramienta, titulo_ventana, texto_ocr_limpio)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """, (
                            id_cubeta,
                            s.get("step_number", 0),
                            s.get("timestamp", ""),
                            s.get("tool_name", "General"),
                            s.get("window_title", ""),
                            s.get("ocr_text", "")
                        ))

                conn.commit()
            log.info(f"Pre-síntesis de cubeta [{id_cubeta}] guardada en staging local aislado del operador.")
            return True
        except Exception as e:
            log.error(f"Error insertando caso en staging: {e}")
            return False

    def get_pending_staging_cases(self) -> List[Dict]:
        """Obtiene todas las pre-síntesis pendientes de revisión humana exclusivas de este operador/PC."""
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT * FROM casos_por_revisar
                WHERE estado_revision = 'PENDIENTE_REVISION'
                ORDER BY fecha_cierre DESC;
                """)
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error(f"Error obteniendo pre-síntesis pendientes: {e}")
            return []

    def get_staging_case_steps(self, id_cubeta: str) -> List[Dict]:
        """Obtiene los pasos de atención asociados a una cubeta en revisión."""
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT * FROM pasos_cubeta
                WHERE id_cubeta = ?
                ORDER BY numero_paso ASC;
                """, (id_cubeta,))
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error(f"Error obteniendo pasos de cubeta {id_cubeta}: {e}")
            return []

    def count_pending_staging(self) -> int:
        """Retorna el número de tarjetas pendientes de revisión en este PC."""
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) AS total FROM casos_por_revisar WHERE estado_revision = 'PENDIENTE_REVISION';")
                row = cursor.fetchone()
                return row["total"] if row else 0
        except Exception:
            return 0

    def approve_staging_case(
        self,
        id_cubeta: str,
        solucion_final: str,
        operador: str = "Operador",
        categoria: Optional[str] = None,
        vector: Optional[List[float]] = None
    ) -> bool:
        """Aprueba un caso del staging local del operador y lo promueve a la tabla oficial compartida 'casos'."""
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                row = cursor.fetchone()
                if not row:
                    log.warning(f"No se encontró caso en staging local con ID [{id_cubeta}].")
                    return False
                r_dict = dict(row)

            valido, motivo = self.validate_gatekeeper(solucion_final, r_dict.get("problema_observado", ""))
            if not valido:
                log.warning(f"Aprobación rechazada por Gatekeeper para [{id_cubeta}]: {motivo}")
                return False

            case_data = {
                "id_caso": f"caso_{id_cubeta.replace('cubeta_', '')}",
                "fecha_creacion": r_dict.get("fecha_cierre") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "operador_mesa": operador,
                "tecnico_campo": r_dict.get("tecnico_campo") or "Técnico en Sitio",
                "id_servicio_cun": r_dict.get("id_servicio_cun") or "",
                "herramienta_origen": r_dict.get("herramientas_usadas") or "General",
                "categoria_falla": categoria or r_dict.get("categoria_deducida") or "Soporte General",
                "ciudad_nodo": r_dict.get("codigo_sitio") or "Desconocido",
                "estado_validacion": "APROBADO_HUMANO",
                "problema_detectado": r_dict.get("problema_observado") or "",
                "ruta_diagnostico_json": r_dict.get("resumen_bloques_json") or "[]",
                "sugerencia_ia": r_dict.get("solucion_deducida_ia") or "",
                "solucion_oficial": solucion_final
            }

            inserted = self.insert_case(case_data, vector=vector)
            if not inserted:
                return False

            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                cursor.execute("DELETE FROM pasos_cubeta WHERE id_cubeta = ?;", (id_cubeta,))
                conn.commit()
            self.sync_local_read_mirror()
            log.info(f"Caso [{id_cubeta}] promovido a casos oficiales y purgado de staging local con éxito.")
            return True
        except Exception as e:
            log.error(f"Error aprobando caso staging {id_cubeta}: {e}")
            return False

    def discard_staging_case(self, id_cubeta: str) -> bool:
        """Descarta y purga una pre-síntesis del staging local del operador."""
        try:
            with self.get_staging_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                cursor.execute("DELETE FROM pasos_cubeta WHERE id_cubeta = ?;", (id_cubeta,))
                conn.commit()
            log.info(f"Caso de staging [{id_cubeta}] descartado y purgado con éxito.")
            return True
        except Exception as e:
            log.error(f"Error descartando caso staging {id_cubeta}: {e}")
            return False
