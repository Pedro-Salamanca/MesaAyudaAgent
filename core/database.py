import os
import json
import math
import sqlite3
import shutil
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from core.config import MEMORY_DIR, CASES_DIR, IMAGES_DIR, MAX_CASOS_UI_MEMORIA
from core.logger import log

# Frases basura o de cortesía que el Gatekeeper debe rechazar para evitar contaminar la base vectorial
FRASES_BASURA_GATEKEEPER = [
    "no aplica", "n/a", "na", "omitir", "ok", "vale", "listo", "gracias",
    "muchas gracias", "enterado", "entendido", "bueno", "bien", "de acuerdo",
    "nada", "ninguna", "ninguno", "test", "prueba", "probando", "ignorar",
    "cerrar", "siguiente", "ninguna solucion", "ninguna solución"
]

class HybridVectorDB:
    """
    Base de Datos Híbrida (Relacional SQL + Vectorial Semántica) para MesaAyudaAgent v3.0.
    - Almacenamiento primario en SQLite (`memory_v3.db`) con modo WAL (concurrencia y cero corrupción).
    - Vectores de embedding JSON embebidos por fila para búsqueda semántica por similitud de coseno.
    - Columnas delimitadas para trazabilidad completa de casos técnicos de SOMOS NETWORK.
    - Mecanismo Gatekeeper de validación de calidad antes de persistir.
    - Migración automática de casos legados en formato JSON.
    - Sincronización y volcado de respaldo hacia Google Drive.
    """
    def __init__(self, db_path: Optional[str] = None):
        self.memory_dir = MEMORY_DIR
        self.cases_dir = CASES_DIR
        self.images_dir = IMAGES_DIR
        self.backup_dir = os.path.join(self.memory_dir, "Backup_DB")

        # Asegurar directorios de almacenamiento
        for path in [self.memory_dir, self.cases_dir, self.images_dir, self.backup_dir]:
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass

        # Ruta de la base de datos principal
        if db_path:
            self.db_path = db_path
        else:
            if os.path.exists(self.memory_dir):
                self.db_path = os.path.join(self.memory_dir, "memory_v3.db")
            else:
                # Fallback a directorio local ./data si Google Drive no está disponible
                local_dir = os.path.join(os.getcwd(), "data")
                os.makedirs(local_dir, exist_ok=True)
                self.db_path = os.path.join(local_dir, "memory_v3.db")

        self.init_tables()

    def get_connection(self) -> sqlite3.Connection:
        """Obtiene una conexión SQLite con modo WAL habilitado para soporte concurrente."""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        return conn

    def init_tables(self):
        """Crea las tablas relacionales y los índices requeridos."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # Tabla principal de Casos Técnicos Delimitados
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
                    sincronizado_backup INTEGER DEFAULT 0
                );
                """)

                # Tabla temporal de pre-síntesis para evaluación diferida (Staging)
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

                # Tabla de pasos crudos de trazabilidad de cubetas
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

                # Tabla de cola transaccional para respaldo seguro
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_caso TEXT NOT NULL,
                    operacion TEXT NOT NULL,
                    fecha TEXT NOT NULL,
                    estado TEXT DEFAULT 'PENDIENTE'
                );
                """)

                # Índices para búsquedas de alta velocidad
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_categoria ON casos(categoria_falla);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_tecnico ON casos(tecnico_campo);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_cun ON casos(id_servicio_cun);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_casos_fecha ON casos(fecha_creacion DESC);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_staging_estado ON casos_por_revisar(estado_revision);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_pasos_cubeta_id ON pasos_cubeta(id_cubeta);")
                conn.commit()
                log.info(f"Base de datos SQLite inicializada exitosamente en: {self.db_path}")
        except Exception as e:
            log.error(f"Error inicializando tablas en {self.db_path}: {e}")

    @staticmethod
    def validate_gatekeeper(solucion: str) -> Tuple[bool, str]:
        """
        Filtro de calidad estricto: evita registrar ruido, saludos o respuestas triviales
        como soluciones técnicas definitivas en la memoria del agente.
        """
        if not solucion or not solucion.strip():
            return False, "La solución está vacía."

        texto_limpio = solucion.strip().lower()

        # Validación 1: Longitud mínima sustancial
        if len(texto_limpio) < 8:
            return False, f"La solución es demasiado corta ({len(texto_limpio)} caracteres). Debe ser una acción técnica clara."

        # Validación 2: Coincidencia con frases de cortesía o vacías
        if texto_limpio in FRASES_BASURA_GATEKEEPER:
            return False, f"La solución '{solucion}' es una frase de cortesía o descarte, no un procedimiento técnico."

        for basura in FRASES_BASURA_GATEKEEPER:
            if texto_limpio == basura or texto_limpio.startswith(basura + " ") or texto_limpio.endswith(" " + basura):
                if len(texto_limpio) < 25:
                    return False, f"La solución contiene texto no procesable ('{basura}')."

        return True, ""

    def insert_case(self, case_data: dict, vector: Optional[List[float]] = None) -> bool:
        """
        Inserta o actualiza un caso en la base de datos de manera atómica.
        Aplica Gatekeeper sobre la solución oficial y serializa el embedding.
        """
        solucion = case_data.get("solucion_oficial") or case_data.get("action") or ""
        valido, motivo = self.validate_gatekeeper(solucion)
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
        ciudad_nodo = case_data.get("ciudad_nodo") or "Desconocido"
        estado_validacion = case_data.get("estado_validacion") or "APROBADO_IA"
        problema_detectado = case_data.get("problema_detectado") or case_data.get("visual_context") or ""
        sugerencia_ia = case_data.get("sugerencia_ia") or case_data.get("agent_suggestion") or ""
        
        # Serializar ruta de pasos si viene como lista o dict
        ruta_diagnostico = case_data.get("ruta_diagnostico_json") or case_data.get("steps") or "[]"
        if not isinstance(ruta_diagnostico, str):
            ruta_diagnostico = json.dumps(ruta_diagnostico, ensure_ascii=False)

        embedding_json = json.dumps(vector) if vector else None

        sql = """
        INSERT OR REPLACE INTO casos (
            id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
            herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
            problema_detectado, ruta_diagnostico_json, sugerencia_ia, solucion_oficial,
            embedding_json, sincronizado_backup
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
        """

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (
                    id_caso, fecha_creacion, operador_mesa, tecnico_campo, id_servicio_cun,
                    herramienta_origen, categoria_falla, ciudad_nodo, estado_validacion,
                    problema_detectado, ruta_diagnostico, sugerencia_ia, solucion,
                    embedding_json
                ))
                cursor.execute("""
                INSERT INTO sync_queue (id_caso, operacion, fecha, estado)
                VALUES (?, 'INSERT', ?, 'PENDIENTE');
                """, (id_caso, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                log.info(f"Caso [{id_caso}] insertado con éxito en SQL+Vector DB (Categoría: {categoria_falla}).")
                return True
        except Exception as e:
            log.error(f"Error al insertar caso {id_caso} en SQLite: {e}")
            return False

    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Calcula la similitud de coseno pura entre dos vectores flotantes sin dependencias pesadas."""
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

    def search_hybrid(
        self,
        query_text: str,
        query_vector: Optional[List[float]] = None,
        top_k: int = 3,
        filters: Optional[Dict[str, str]] = None
    ) -> List[Dict]:
        """
        Búsqueda Híbrida:
        - Si hay query_vector: calcula similitud semántica de coseno contra todos los casos vectorizados.
        - Si no hay vector o falla la API: usa búsqueda léxica SQL con filtros delimitados (categoría, herramienta, ciudad).
        - Retorna los top_k casos con mayor relevancia técnica.
        """
        results = []
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query_sql = "SELECT * FROM casos"
                params = []
                where_clauses = []

                if filters:
                    if filters.get("categoria_falla"):
                        where_clauses.append("categoria_falla = ?")
                        params.append(filters["categoria_falla"])
                    if filters.get("herramienta_origen"):
                        where_clauses.append("herramienta_origen LIKE ?")
                        params.append(f"%{filters['herramienta_origen']}%")
                    if filters.get("ciudad_nodo"):
                        where_clauses.append("ciudad_nodo = ?")
                        params.append(filters["ciudad_nodo"])

                if where_clauses:
                    query_sql += " WHERE " + " AND ".join(where_clauses)

                cursor.execute(query_sql, params)
                rows = [dict(r) for r in cursor.fetchall()]

                if not rows:
                    return []

                # Si se dispone de vector semántico, ordenar por similitud de coseno
                if query_vector:
                    scored_rows = []
                    for row in rows:
                        emb_str = row.get("embedding_json")
                        sim = 0.0
                        if emb_str:
                            try:
                                emb = json.loads(emb_str)
                                sim = self.cosine_similarity(query_vector, emb)
                            except Exception:
                                sim = 0.0
                        scored_rows.append((sim, row))
                    scored_rows.sort(key=lambda x: x[0], reverse=True)
                    results = [r for sim, r in scored_rows[:top_k] if sim > 0.40]
                    if results:
                        return results

                # Búsqueda por coincidencia léxica sobre problema_detectado y solucion_oficial
                terms = [w.lower() for w in query_text.split() if len(w) > 3]
                scored_lexical = []
                for row in rows:
                    text_blob = f"{row.get('problema_detectado', '')} {row.get('solucion_oficial', '')} {row.get('categoria_falla', '')}".lower()
                    score = sum(1 for term in terms if term in text_blob)
                    scored_lexical.append((score, row))

                scored_lexical.sort(key=lambda x: x[0], reverse=True)
                results = [r for score, r in scored_lexical[:top_k] if score > 0]
                return results or [r for _, r in scored_lexical[:top_k]]
        except Exception as e:
            log.error(f"Error en búsqueda híbrida de casos: {e}")
            return []

    def get_all_cases(self, limit: int = MAX_CASOS_UI_MEMORIA) -> List[Tuple]:
        """
        Retorna la lista de casos en el formato de tuplas para la pestaña de memoria de la UI:
        [(id_caso, visual_context, action, timestamp)]
        """
        cases = []
        try:
            with self.get_connection() as conn:
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
        """Elimina un caso tanto de la base de datos relacional como del disco."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos WHERE id_caso = ?;", (case_id,))
                cursor.execute("""
                INSERT INTO sync_queue (id_caso, operacion, fecha, estado)
                VALUES (?, 'DELETE', ?, 'PENDIENTE');
                """, (case_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()

            # También eliminar JSON e imagen legados si existen
            json_file = os.path.join(self.cases_dir, f"{case_id}.json")
            img_file = os.path.join(self.images_dir, f"{case_id}.png")
            if os.path.exists(json_file):
                try: os.remove(json_file)
                except Exception: pass
            if os.path.exists(img_file):
                try: os.remove(img_file)
                except Exception: pass

            log.info(f"Caso [{case_id}] eliminado con éxito.")
            return True
        except Exception as e:
            log.error(f"Error al eliminar caso {case_id}: {e}")
            return False

    def migrate_legacy_jsons(self) -> int:
        """
        Escanea la carpeta de casos legados (Cases) e ingesta automáticamente
        todos los archivos caso_*.json que no estén aún en SQLite.
        """
        migrated_count = 0
        if not os.path.exists(self.cases_dir):
            return 0

        archivos = [f for f in os.listdir(self.cases_dir) if f.endswith(".json")]
        log.info(f"Iniciando inspección de migración sobre {len(archivos)} archivos JSON...")

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
                        if not solucion or len(solucion) < 3:
                            continue

                        # Deducir categoría y herramienta
                        w_title = data.get("window_title", "")
                        herramienta = "General"
                        if "artemis" in w_title.lower(): herramienta = "Artemis"
                        elif "putty" in w_title.lower(): herramienta = "Putty"
                        elif "winbox" in w_title.lower(): herramienta = "WinBox"
                        elif "zabbix" in w_title.lower(): herramienta = "Zabbix"

                        v_ctx = data.get("visual_context", "")
                        categoria = "Soporte General"
                        if "switch" in v_ctx.lower() or "puerto" in v_ctx.lower():
                            categoria = "Switch/Capa 2"
                        elif "router" in v_ctx.lower() or "dhcp" in v_ctx.lower():
                            categoria = "Router Secundario"
                        elif "acometida" in v_ctx.lower() or "hilo" in v_ctx.lower():
                            categoria = "Acometida/8 Hilos"
                        elif "velocidad" in v_ctx.lower() or "mbps" in v_ctx.lower():
                            categoria = "Velocidad/Plan Pro"
                        elif "radio" in v_ctx.lower() or "antena" in v_ctx.lower() or "cpe" in v_ctx.lower():
                            categoria = "Radioenlace"

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
                log.info(f"Migración completada con éxito: {migrated_count} casos JSON nuevos importados a SQLite.")
        except Exception as e:
            log.error(f"Error en ciclo de migración JSON a SQLite: {e}")

        return migrated_count

    def sync_backup_to_drive(self) -> bool:
        """
        Ejecuta un punto de control WAL (checkpoint) y genera una copia de respaldo segura
        en la carpeta Backup_DB de Google Drive para garantizar redundancia total.
        """
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            with self.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

            backup_file = os.path.join(self.backup_dir, "memory_v3_backup.db")
            shutil.copy2(self.db_path, backup_file)
            log.info(f"Respaldo de memoria sincronizado correctamente en: {backup_file}")
            return True
        except Exception as e:
            log.error(f"Error generando respaldo a Google Drive: {e}")
            return False

    def insert_staging_case(self, staging_data: dict, steps: Optional[List[dict]] = None) -> bool:
        """
        Inserta una pre-síntesis de caso en 'casos_por_revisar' y opcionalmente sus pasos crudos en 'pasos_cubeta'.
        """
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
            with self.get_connection() as conn:
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
                log.info(f"Pre-síntesis de cubeta [{id_cubeta}] guardada en 'casos_por_revisar' con éxito.")
                return True
        except Exception as e:
            log.error(f"Error insertando caso en staging: {e}")
            return False

    def get_pending_staging_cases(self) -> List[Dict]:
        """Obtiene todas las pre-síntesis pendientes de revisión humana."""
        try:
            with self.get_connection() as conn:
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
            with self.get_connection() as conn:
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
        """Retorna el número de tarjetas pendientes de revisión."""
        try:
            with self.get_connection() as conn:
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
        """
        Aprueba y promueve un caso de 'casos_por_revisar' a la tabla oficial 'casos'.
        Una vez persistido en la memoria oficial, purga automáticamente el registro temporal
        de 'casos_por_revisar' y 'pasos_cubeta' para mantener la base de datos limpia.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                row = cursor.fetchone()
                if not row:
                    log.warning(f"No se encontró caso en staging con ID [{id_cubeta}].")
                    return False
                r_dict = dict(row)

            # Validar con Gatekeeper
            valido, motivo = self.validate_gatekeeper(solucion_final)
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

            # Insertar en tabla oficial 'casos'
            inserted = self.insert_case(case_data, vector=vector)
            if not inserted:
                return False

            # Purgar de staging efímero
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                cursor.execute("DELETE FROM pasos_cubeta WHERE id_cubeta = ?;", (id_cubeta,))
                conn.commit()

            log.info(f"Caso [{id_cubeta}] promovido a casos oficiales y purgado de staging efímero con éxito.")
            return True
        except Exception as e:
            log.error(f"Error aprobando caso staging {id_cubeta}: {e}")
            return False

    def discard_staging_case(self, id_cubeta: str) -> bool:
        """
        Descarta y purga una pre-síntesis de 'casos_por_revisar' y 'pasos_cubeta'.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM casos_por_revisar WHERE id_cubeta = ?;", (id_cubeta,))
                cursor.execute("DELETE FROM pasos_cubeta WHERE id_cubeta = ?;", (id_cubeta,))
                conn.commit()
            log.info(f"Caso de staging [{id_cubeta}] descartado y purgado con éxito.")
            return True
        except Exception as e:
            log.error(f"Error descartando caso staging {id_cubeta}: {e}")
            return False
