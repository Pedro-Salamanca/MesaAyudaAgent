import sys
import os
import json
import threading
import time
from datetime import datetime
import queue
import urllib.request
import subprocess
import ssl
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

# Asegurar importación de módulos internos tanto en desarrollo como en PyInstaller
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from ui.app_window import AppWindow
from core.vision import ScreenReader
from core.brain import AgentBrain
from core.llm_client import LLMClient
from core.search_engine import MemorySearchEngine
from core.pre_filter import PreFilterEngine
from core.bucket_manager import BucketManager, CaseBucket
from core.config import (
    TESSERACT_PATHS,
    TESSERACT_INSTALLER_URL,
    TECNICO_NOMBRE_DEFAULT,
    INTERVALO_VISION_SEGUNDOS,
    LONGITUD_MINIMA_OCR,
    MAX_CASOS_HISTORICOS_RAG
)
from core.logger import log

def check_and_install_tesseract() -> bool:
    """
    Verifica la existencia del binario de Tesseract OCR en las rutas estándar.
    Si no existe, descarga e instala silenciosamente la dependencia de Windows.
    """
    if any(os.path.exists(p) for p in TESSERACT_PATHS):
        return True
        
    log.warning("No se detectó Tesseract OCR. Iniciando instalador guiado...")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    
    msg = (
        "No se detectó el motor visual Tesseract-OCR en este equipo.\n\n"
        "El Agente SOMOS descargará e instalará esta dependencia automáticamente (~40MB).\n"
        "Puede tardar 1 o 2 minutos. Por favor presiona Aceptar, dale 'Sí' a los permisos "
        "de Administrador cuando Windows te lo pida y espera un momento."
    )
    messagebox.showinfo("Instalación Requerida", msg, parent=root)
    
    ssl._create_default_https_context = ssl._create_unverified_context
    installer_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "tesseract_installer.exe")
    
    try:
        urllib.request.urlretrieve(TESSERACT_INSTALLER_URL, installer_path)
        subprocess.run([installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"], check=True)
        messagebox.showinfo("Éxito", "¡Motor visual instalado correctamente! El Agente iniciará ahora.", parent=root)
        root.destroy()
        log.info("Tesseract instalado exitosamente.")
        return True
    except Exception as e:
        log.error(f"Fallo al instalar Tesseract automáticamente: {e}")
        messagebox.showerror("Error", f"Falló la instalación automática: {e}\n\nDeberás instalar Tesseract manualmente.", parent=root)
        root.destroy()
        return False

class MesaAyudaAgentApp:
    """
    Controlador Principal de MesaAyudaAgent v3.0 (SOMOS NETWORK).
    Orquesta:
    1. Pre-Filtrado Anti-Spam y Recorte de Artemis (24% lateral izquierdo y 8% pestañas).
    2. Aislamiento Estricto de Cubetas (Zero-Guessing: nunca mezclar técnicos).
    3. Resúmenes de Bloque de 8 pasos (Rolling Block Summary) para memoria ultraligera.
    4. Auto-cierre de cubetas a los 10 minutos de inactividad hacia la bandeja de tarjetas.
    5. Modo Solo Aprendizaje con transición automática a los 30 minutos de inactividad global.
    6. Dos botones de pausa: Break de turno (conserva pantalla) vs Pausar y Evaluar Aprendizaje.
    7. Bandeja interactiva de tarjetas efímeras con aprobación, corrección asistida y descarte.
    8. Base de datos oficial SQLite WAL + Embeddings (gemini-embedding-001 de 3072 dimensiones).
    """
    def __init__(self):
        self.tecnico_nombre = TECNICO_NOMBRE_DEFAULT
        self.last_problem_signature = ""

        # Componentes de Inteligencia, Visión y Memoria
        self.pre_filter = PreFilterEngine()
        self.bucket_manager = BucketManager(max_concurrent=5, ttl_bucket_minutes=10, global_inactivity_minutes=30)
        self.brain = AgentBrain()
        self.llm = LLMClient()
        self.search_engine = MemorySearchEngine(db=self.brain.db)
        self.vision = ScreenReader()

        # Cola de tareas en segundo plano para compresión y síntesis sin bloquear la interfaz
        self.bg_tasks_queue = queue.Queue()

        # Interfaz Gráfica con Callbacks Completos
        self.app = AppWindow(
            chat_callback=self.on_chat_message,
            pause_callback=self.on_toggle_pause,
            pause_break_callback=self.on_toggle_break_pause,
            pause_eval_callback=self.on_pause_and_evaluate,
            toggle_learning_mode_callback=self.on_toggle_learning_mode,
            delete_callback=self.on_delete_case,
            refresh_callback=self.refresh_memory_ui,
            force_callback=self.on_force_save,
            approve_callback=self.on_approve_suggestion,
            close_case_callback=self.on_close_case,
            discard_bucket_callback=self.on_discard_bucket,
            select_bucket_callback=self.on_select_bucket,
            approve_staging_callback=self.on_approve_staging,
            correct_staging_callback=self.on_correct_staging,
            discard_staging_callback=self.on_discard_staging,
            refresh_staging_callback=self.refresh_staging_ui,
            tecnico_nombre=self.tecnico_nombre
        )

        # Variables de Control de Flujo
        self.is_paused = False
        self.is_asking = False
        self.is_learning_mode = False
        self.current_visual_context = ""
        self.current_agent_suggestion = ""
        self.last_valid_ocr = "Esperando lectura inicial..."
        self.last_valid_image = None
        self.last_valid_title = ""

        # Inicialización de estado y tarjetas en la UI
        self._refresh_pills_ui()
        self.refresh_staging_ui()
        self.refresh_memory_ui()

        # Hilo de procesamiento en segundo plano (worker)
        self.worker_thread = threading.Thread(target=self._background_worker_loop, daemon=True)
        self.worker_thread.start()

        # Hilo del ciclo visual
        self.vision_thread = threading.Thread(target=self.vision_loop, daemon=True)
        self.vision_thread.start()

        log.info("Agente MesaAyudaAgent v3.0 inicializado y en ejecución.")

    # =========================================================================
    # REFRESCADO DE COMPONENTES UI
    # =========================================================================
    def _refresh_pills_ui(self):
        """Refresca las pestañas de cubetas activas y la tarjeta de detalle en la UI."""
        summary = self.bucket_manager.get_active_buckets_summary()
        self.app.after(0, self.app.update_buckets_pills, summary)

        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            tools = list(set([s.tool_name for s in focused.steps])) or ["General"]
            info_txt = (
                f"Técnico: {focused.tecnico_campo}\n"
                f"Sitio: {focused.codigo_sitio or 'N/A'} | CUN: {focused.id_servicio_cun or 'N/A'}\n"
                f"Pasos acumulados: {focused.total_steps_lifetime} ({len(focused.steps)} en bloque actual)\n"
                f"Bloques comprimidos: {len(focused.partial_summaries)}\n"
                f"Herramientas: {', '.join(tools)}"
            )
        else:
            info_txt = "Técnico: Esperando interacción en Artemis/herramientas...\nSitio / CUN: -\nPasos registrados: 0\nHerramientas: -"

        self.app.after(0, self.app.update_bucket_info, info_txt)

    def refresh_staging_ui(self):
        """Carga y renderiza las tarjetas de evaluación pendientes desde SQLite."""
        try:
            staging_cases = self.brain.get_pending_staging_cases()
            self.app.after(0, self.app.populate_staging_cards, staging_cases)
        except Exception as e:
            log.error(f"Error refrescando tarjetas de staging: {e}")

    def refresh_memory_ui(self):
        """Recarga la lista de casos guardados en la memoria oficial de SQLite."""
        try:
            cases = self.brain.get_all_cases()
            self.app.after(0, self.app.populate_memory, cases)
        except Exception as e:
            log.error(f"Error refrescando memoria oficial: {e}")

    # =========================================================================
    # CONTROL DE PAUSAS Y MODOS DE TRABAJO
    # =========================================================================
    def on_toggle_pause(self):
        """Toggle genérico de pausa."""
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.app.set_state("paused")
        else:
            self.app.set_state("active")

    def on_toggle_break_pause(self, is_break: bool):
        """
        BOTÓN 1: Pausa de Turno / Comida (Break).
        - Permanece en la pantalla actual de observación.
        - Detiene capturas de pantalla y consultas a IA.
        - En segundo plano, las cubetas inactivas se sintetizan y cierran sin interrumpir al operador.
        """
        self.is_paused = is_break
        self.is_asking = False
        if is_break:
            self.app.set_state("break")
            self.app.after(0, self.app.update_inference_output, "☕ Pausa de turno (Break) activa. Captura visual detenida.\nLas cubetas pendientes se sintetizarán en segundo plano.")
            self.app.after(0, self.app.append_chat, "Agente", "☕ Turno en pausa (Break/Comida). No capturaré pantallas hasta que pulses '▶️ Reanudar Turno'.")
            log.info("Pausa de turno (Break) activada.")
        else:
            if self.is_learning_mode:
                self.app.set_state("learning")
                self.app.after(0, self.app.update_inference_output, "🎓 Modo Solo Aprendizaje activo. Capturando y comprimiendo atenciones...")
            else:
                self.app.set_state("active")
                self.app.after(0, self.app.update_inference_output, "▶️ Turno reanudado. Observando y asociando cubetas activas...")
            self.app.after(0, self.app.append_chat, "Agente", "▶️ Turno reanudado. Retomando observación técnica en Artemis y herramientas.")
            log.info("Turno reanudado tras el break.")

    def on_pause_and_evaluate(self):
        """
        BOTÓN 2: Pausar y Evaluar Aprendizaje.
        - Pausa la observación visual.
        - Abre inmediatamente la pestaña de tarjetas '📋 Evaluar Aprendizaje'.
        """
        self.is_paused = True
        self.is_asking = False
        self.app.set_state("paused")
        self.refresh_staging_ui()
        self.app.after(0, self.app.update_inference_output, "📋 Observación en pausa. Mostrando tarjetas de evaluación de aprendizaje...")
        self.app.after(0, self.app.append_chat, "Agente", "📋 Modo evaluación abierto. Revisa las tarjetas de los casos aprendidos.")
        log.info("Modo evaluación de aprendizaje activado por el operador.")

    def on_toggle_learning_mode(self, enabled: bool):
        """
        Activa o desactiva el 'Modo Solo Aprendizaje'.
        En este modo, el agente no lanza preguntas inmediatas; simplemente captura, aísla por cubeta,
        comprime bloques cada 8 pasos y auto-cierra tras 10 min de inactividad para su posterior evaluación.
        """
        self.is_learning_mode = enabled
        self.is_asking = False
        self.current_agent_suggestion = ""
        self.last_problem_signature = ""
        if enabled:
            self.app.set_state("learning")
            self.app.after(0, self.app.append_chat, "Agente", "🎓 Modo Solo Aprendizaje ACTIVADO.\nObservaré en segundo plano sin interrumpirte. Toda atención se guardará como tarjeta para que la evalúes cuando desees.")
            self.app.after(0, self.app.update_inference_output, "🎓 Modo Solo Aprendizaje activo. Capturando y comprimiendo atenciones en segundo plano...")
            log.info("Modo Solo Aprendizaje activado.")
        else:
            self.app.set_state("active")
            self.app.after(0, self.app.append_chat, "Agente", "🎓 Modo Solo Aprendizaje DESACTIVADO. Asistencia interactiva en vivo restaurada.")
            self.app.after(0, self.app.update_inference_output, "Asistencia interactiva en vivo restaurada.")
            log.info("Modo Solo Aprendizaje desactivado.")

    # =========================================================================
    # GESTIÓN DE EVALUACIÓN DE TARJETAS (STAGING)
    # =========================================================================
    def on_approve_staging(self, id_cubeta: str, solucion: str):
        """
        Aprueba directamente la solución deducida por la IA en una tarjeta de aprendizaje.
        La promueve a la memoria oficial y purga la tarjeta de staging.
        """
        threading.Thread(target=self._async_approve_staging, args=(id_cubeta, solucion), daemon=True).start()

    def _async_approve_staging(self, id_cubeta: str, solucion: str):
        self.app.after(0, self.app.update_inference_output, f"Aprobando caso [{id_cubeta}]...")
        # Generar embedding semántico oficial (gemini-embedding-001)
        vector = self.llm.generar_embedding(solucion)
        
        ok = self.brain.approve_staging_case(id_cubeta, solucion, operador=self.tecnico_nombre, vector=vector)
        if ok:
            self.app.after(0, self.app.append_chat, "Agente", f"✅ Caso [{id_cubeta}] aprobado con éxito y guardado en memory_v3.db!")
            self.refresh_staging_ui()
            self.refresh_memory_ui()
        else:
            self.app.after(0, self.app.append_chat, "Agente", f"⚠️ No se pudo aprobar el caso [{id_cubeta}]. Verifica que la solución sea una acción técnica válida.")

    def on_correct_staging(self, id_cubeta: str, operator_text: str, case_dict: dict):
        """
        Toma la respuesta o corrección técnica ingresada por el operador, la fusiona con la
        pre-síntesis mediante Gemini, valida con Gatekeeper y persiste en memoria oficial.
        """
        if not operator_text or not operator_text.strip():
            self.app.after(0, self.app.append_chat, "Agente", "⚠️ Por favor escribe tu respuesta o corrección en el campo de texto antes de presionar 'Responder / Corregir y Guardar'.")
            return

        threading.Thread(target=self._async_correct_staging, args=(id_cubeta, operator_text, case_dict), daemon=True).start()

    def _async_correct_staging(self, id_cubeta: str, operator_text: str, case_dict: dict):
        self.app.after(0, self.app.update_inference_output, f"Integrando corrección del operador para [{id_cubeta}]...")
        
        # Integración inteligente con Gemini respetando lineamientos SOMOS
        solucion_final = self.llm.integrar_correccion_humana(case_dict, operator_text)

        # Generar vector semántico con gemini-embedding-001
        vector = self.llm.generar_embedding(solucion_final)

        ok = self.brain.approve_staging_case(id_cubeta, solucion_final, operador=self.tecnico_nombre, vector=vector)
        if ok:
            self.app.after(0, self.app.append_chat, "Agente", f"✏️ Corrección integrada para [{id_cubeta}]. Solución oficial registrada:\n\"{solucion_final}\"")
            self.refresh_staging_ui()
            self.refresh_memory_ui()
        else:
            self.app.after(0, self.app.append_chat, "Agente", f"⚠️ Error registrando corrección para [{id_cubeta}].")

    def on_discard_staging(self, id_cubeta: str):
        """Descarta y elimina permanentemente una tarjeta de evaluación de staging."""
        threading.Thread(target=self._async_discard_staging, args=(id_cubeta,), daemon=True).start()

    def _async_discard_staging(self, id_cubeta: str):
        ok = self.brain.discard_staging_case(id_cubeta)
        if ok:
            self.app.after(0, self.app.append_chat, "Agente", f"🗑️ Caso de aprendizaje [{id_cubeta}] descartado.")
            self.refresh_staging_ui()
        else:
            self.app.after(0, self.app.append_chat, "Agente", f"⚠️ No se pudo descartar el caso [{id_cubeta}].")

    # =========================================================================
    # ACCIONES DE CASO EN TAB 1 (OBSERVACIÓN Y CHAT)
    # =========================================================================
    def on_delete_case(self, case_id: str):
        """Elimina un caso específico de la memoria oficial tras solicitud del usuario."""
        self.brain.delete_case(case_id)
        self.refresh_memory_ui()

    def on_select_bucket(self, bucket_id: str):
        """Cambia el foco a otra de las cubetas activas al hacer clic en su píldora."""
        self.bucket_manager.set_focused_bucket(bucket_id)
        self._refresh_pills_ui()
        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            self.app.append_chat("Agente", f"🔄 Foco cambiado al caso de: {focused.tecnico_campo} (Sitio: {focused.codigo_sitio or 'N/A'}).")

    def on_approve_suggestion(self):
        """El técnico aprueba la sugerencia técnica del agente (no cierra la cubeta)."""
        focused = self.bucket_manager.get_focused_bucket()
        if self.current_agent_suggestion:
            sug = self.current_agent_suggestion
            if focused:
                focused.approved_suggestions.append(sug)
                self.bucket_manager.save_checkpoint_to_disk()

            self.app.append_chat(self.tecnico_nombre, f"✅ Sugerencia técnica aprobada: '{sug}'")
            self.app.append_chat("Agente", "👍 Sugerencia guardada en el historial del caso. La cubeta sigue abierta para más pasos o validaciones. Cuando la atención concluya, presiona '🏁 Finalizar Caso'.")
            
            self.is_asking = False
            self.app.set_state("active")
            self.app.update_inference_output("Sugerencia incorporada al caso. Continuando observación...")
            self._refresh_pills_ui()
            log.info(f"Sugerencia aprobada para la cubeta actual: {sug}")
        else:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna sugerencia técnica activa para aprobar en este momento.")

    def on_close_case(self, user_solution: str = ""):
        """Cierre explícito manual de la cubeta activa desde el botón '🏁 Finalizar Caso'."""
        focused = self.bucket_manager.get_focused_bucket()
        if not focused:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna cubeta técnica activa para finalizar.")
            return

        threading.Thread(target=self._process_case_finalization, args=(focused, user_solution), daemon=True).start()

    def _process_case_finalization(self, bucket: CaseBucket, user_solution: str):
        """Sintetiza la trazabilidad de la cubeta con Gemini, valida con Gatekeeper y persiste en BD oficial."""
        b_id = bucket.bucket_id
        tec_name = bucket.tecnico_campo
        pasos_total = bucket.total_steps_lifetime

        self.app.after(0, self.app.append_chat, "Agente", f"🏁 Finalizando atención de {tec_name} ({pasos_total} pasos)... Sintetizando caso con IA...")
        self.app.after(0, self.app.update_inference_output, f"Sintetizando episodio para {tec_name}...")

        timeline_text = bucket.compile_timeline_text()

        solucion_candidata = user_solution.strip()
        if not solucion_candidata and bucket.approved_suggestions:
            solucion_candidata = " | ".join(bucket.approved_suggestions)

        episodio = self.llm.sintetizar_episodio(timeline_text)

        if episodio:
            categoria_falla = episodio.categoria_falla
            ciudad_nodo = episodio.ciudad_nodo
            herramienta = episodio.herramienta_origen
            resumen_problema = episodio.descripcion_problema
            if not solucion_candidata:
                solucion_candidata = episodio.accion_sugerida
        else:
            categoria_falla = "Soporte General"
            ciudad_nodo = bucket.codigo_sitio or "Desconocido"
            herramienta = "General"
            resumen_problema = f"Atención técnica con {tec_name}"
            if not solucion_candidata:
                solucion_candidata = self.current_agent_suggestion or "Atención finalizada por el operador de Mesa de Ayuda."

        # Filtro Gatekeeper
        valido, motivo = self.brain.db.validate_gatekeeper(solucion_candidata)
        if not valido:
            msg_alerta = f"⚠️ Gatekeeper: No se pudo guardar el caso porque la solución indicada no es válida ({motivo}). Por favor escribe la solución técnica en la caja de texto y pulsa '🏁 Finalizar Caso'."
            self.app.after(0, self.app.append_chat, "Agente", msg_alerta)
            self.app.after(0, self.app.update_inference_output, "Gatekeeper: Solución técnica requerida.")
            return

        # Generación de Vector de Embedding
        self.app.after(0, self.app.update_inference_output, "Generando vector semántico (gemini-embedding-001)...")
        texto_para_vector = f"{resumen_problema} | {solucion_candidata} | {categoria_falla} | {herramienta}"
        vector = self.llm.generar_embedding(texto_para_vector)

        extra_meta = {
            "tecnico_campo": tec_name,
            "id_servicio_cun": bucket.id_servicio_cun,
            "herramienta_origen": herramienta,
            "categoria_falla": categoria_falla,
            "ciudad_nodo": ciudad_nodo,
            "estado_validacion": "APROBADO_HUMANO" if user_solution else "APROBADO_IA",
            "ruta_diagnostico_json": json.dumps(bucket.partial_summaries, ensure_ascii=False)
        }

        guardado = self.brain.save_case(
            visual_context=resumen_problema,
            action=solucion_candidata,
            agent_suggestion=episodio.accion_sugerida if episodio else "",
            image_obj=self.last_valid_image,
            active_title=self.last_valid_title,
            tecnico_nombre=self.tecnico_nombre,
            vector=vector,
            extra_metadata=extra_meta
        )

        if guardado:
            self.bucket_manager.close_and_extract_bucket(b_id)
            self.is_asking = False
            self.current_agent_suggestion = ""
            self.last_problem_signature = ""

            self.app.after(0, self.app.append_chat, "Agente", f"✅ Caso finalizado y almacenado con éxito en memory_v3.db!\n- Técnico: {tec_name}\n- Categoría: {categoria_falla}\n- Solución Oficial: {solucion_candidata}\n- Embedding: {'Generado (3072d)' if vector else 'Pendiente'}")
            self.app.after(0, self.app.set_state, "active")
            self.app.after(0, self.app.update_inference_output, "Caso cerrado con éxito. Observando nuevos eventos...")
            self._refresh_pills_ui()
            self.app.after(0, self.refresh_memory_ui)
        else:
            self.app.after(0, self.app.append_chat, "Agente", "❌ Ocurrió un error al intentar registrar el caso en la base de datos.")

    def on_discard_bucket(self):
        """Descarta la cubeta enfocada si se trató de una consulta falsa o cancelada."""
        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            b_id = focused.bucket_id
            tec = focused.tecnico_campo
            self.bucket_manager.close_and_extract_bucket(b_id)
            self.is_asking = False
            self.current_agent_suggestion = ""
            self.last_problem_signature = ""
            self.app.append_chat("Agente", f"🗑️ Cubeta de {tec} descartada sin guardar.")
            self.app.set_state("active")
            self._refresh_pills_ui()

    def on_force_save(self, msg: str):
        """Fuerza el guardado inmediato de la pantalla actual en la base de datos."""
        valido, motivo = self.brain.db.validate_gatekeeper(msg)
        if not valido:
            self.app.append_chat("Agente", f"⚠️ Solución no válida: {motivo}")
            return

        extra = {
            "tecnico_campo": "Guardado Manual",
            "categoria_falla": "Soporte General",
            "estado_validacion": "FORZADO_MANUAL"
        }
        self.brain.save_case(
            visual_context=f"Guardado manual: {self.last_valid_ocr[:200]}...",
            action=msg,
            agent_suggestion="",
            image_obj=self.last_valid_image,
            active_title=self.last_valid_title,
            tecnico_nombre=self.tecnico_nombre,
            extra_metadata=extra
        )
        self.app.append_chat("Agente", "¡Guardado Forzado exitoso en memory_v3.db!")
        self.refresh_memory_ui()

    def on_chat_message(self, msg: str):
        """Atiende consultas técnicas del operador con RAG híbrido y contexto activo."""
        threading.Thread(target=self._process_chat_question, args=(msg,), daemon=True).start()

    def _process_chat_question(self, msg: str):
        """Ejecuta consulta RAG combinando vector embedding + similitud semántica + Gemini."""
        self.app.after(0, self.app.append_chat, "Agente", "Buscando casos similares en base de datos híbrida...")

        q_vec = self.llm.generar_embedding(msg)
        casos_similares = self.search_engine.search(msg, top_k=MAX_CASOS_HISTORICOS_RAG, query_vector=q_vec)

        memoria_contexto = ""
        if casos_similares:
            memoria_contexto = "\n[CASOS HISTÓRICOS RELEVANTES EN MEMORIA]:\n"
            for c in casos_similares:
                accion = c.get("action", "")
                cat = c.get("categoria_falla", "General")
                memoria_contexto += f"- Categoría: {cat} | Solución: {accion}\n"

        focused = self.bucket_manager.get_focused_bucket()
        contexto_cubeta = focused.compile_timeline_text() if focused else self.last_valid_ocr[:1000]
        contexto_completo = f"Línea de tiempo de la cubeta en curso:\n{contexto_cubeta}\n\n{memoria_contexto}"

        self.app.after(0, self.app.append_chat, "Agente", "Pensando respuesta con Gemini...")
        respuesta = self.llm.responder_pregunta(msg, contexto_completo)

        self.app.after(0, self.app.append_chat, "Agente", respuesta)

    def get_active_window_title(self) -> str:
        """Obtiene el título de la ventana activa en Windows."""
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    # =========================================================================
    # COLA EN SEGUNDO PLANO (WORKER THREAD): COMPRESIÓN Y AUTO-CIERRE
    # =========================================================================
    def _background_worker_loop(self):
        """Procesa tareas asíncronas pesadas (resumen de 8 pasos y auto-cierre de cubetas a staging)."""
        while True:
            try:
                task = self.bg_tasks_queue.get()
                task_type = task.get("type")

                if task_type == "compress_block":
                    bucket = task["bucket"]
                    steps_to_compress = list(task["steps"])
                    resumen = self.llm.resumir_bloque_pasos(steps_to_compress)
                    bucket.compress_current_block(resumen)
                    self.bucket_manager.save_checkpoint_to_disk()
                    self._refresh_pills_ui()
                    log.info(f"Bloque de 8 pasos comprimido con éxito para {bucket.bucket_id}: {resumen[:80]}...")

                elif task_type == "auto_close_staging":
                    bucket: CaseBucket = task["bucket"]
                    b_id = bucket.bucket_id
                    timeline_text = bucket.compile_timeline_text()

                    # Sintetizar tarjeta con esquema PreSintesisCaso
                    pre_sintesis = self.llm.sintetizar_para_revision(timeline_text)

                    if pre_sintesis:
                        staging_data = {
                            "id_cubeta": b_id,
                            "fecha_inicio": bucket.created_at,
                            "fecha_cierre": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "tecnico_campo": pre_sintesis.nombre_tecnico_en_sitio if pre_sintesis.nombre_tecnico_en_sitio != "Desconocido" else bucket.tecnico_campo,
                            "id_servicio_cun": pre_sintesis.id_servicio_cun or bucket.id_servicio_cun,
                            "codigo_sitio": pre_sintesis.codigo_sitio if pre_sintesis.codigo_sitio != "Desconocido" else bucket.codigo_sitio,
                            "mac_ip_detectadas": ", ".join(bucket.mac_addresses + bucket.ip_addresses),
                            "herramientas_usadas": pre_sintesis.herramientas_usadas,
                            "categoria_deducida": pre_sintesis.categoria_falla,
                            "problema_observado": pre_sintesis.problema_observado,
                            "solucion_deducida_ia": pre_sintesis.solucion_deducida_ia,
                            "pregunta_duda_ia": pre_sintesis.pregunta_duda_ia,
                            "resumen_bloques": bucket.partial_summaries
                        }
                    else:
                        staging_data = {
                            "id_cubeta": b_id,
                            "fecha_inicio": bucket.created_at,
                            "fecha_cierre": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "tecnico_campo": bucket.tecnico_campo,
                            "id_servicio_cun": bucket.id_servicio_cun,
                            "codigo_sitio": bucket.codigo_sitio,
                            "mac_ip_detectadas": ", ".join(bucket.mac_addresses + bucket.ip_addresses),
                            "herramientas_usadas": "Artemis/Red",
                            "categoria_deducida": "Soporte General",
                            "problema_observado": f"Atención registrada con {bucket.tecnico_campo}",
                            "solucion_deducida_ia": "Atención finalizada por inactividad funcional.",
                            "pregunta_duda_ia": "¿Qué procedimiento técnico específico se completó para este caso?",
                            "resumen_bloques": bucket.partial_summaries
                        }

                    # Guardar en base de datos temporal
                    self.brain.insert_staging_case(staging_data, steps=[s.to_dict() for s in bucket.steps])
                    self.refresh_staging_ui()
                    self._refresh_pills_ui()

                    self.app.after(0, self.app.append_chat, "Agente", f"📋 Cubeta de {staging_data['tecnico_campo']} auto-cerrada por inactividad (10 min). Tarjeta lista en '📋 Evaluar Aprendizaje'.")
                    log.info(f"Cubeta [{b_id}] movida a staging de tarjetas.")

                self.bg_tasks_queue.task_done()
            except Exception as e:
                log.error(f"Error en worker loop en segundo plano: {e}")
                time.sleep(1.0)

    # =========================================================================
    # CICLO DE VISIÓN CONTINUO
    # =========================================================================
    def vision_loop(self):
        """
        Ciclo Continuo de Visión y Pre-Filtrado:
        1. Comprueba si la ventana está permitida.
        2. Captura imagen y extrae OCR aplicando recorte inteligente de Artemis (24% izquierda, 8% pestañas).
        3. Evalúa cambio de píxeles (>2%) y duplicidad de texto (>92%) para no consumir tokens en pantallas estáticas.
        4. Blindaje contra pantallas de Login.
        5. Extrae anclas unívocas (Técnico, CUN, Sitio, MAC).
        6. Regla de Oro: Si no hay ancla fuerte, descarta la captura para evitar mezclas erróneas.
        7. Enruta a cubeta correspondiente (score >= 0.70 o crea nueva con técnico/CUN diferente).
        8. Si la cubeta acumula 8 pasos, programa compresión de bloque en segundo plano.
        9. Revisa cubetas con más de 10 min de inactividad para moverlas a tarjetas de evaluación.
        10. Revisa inactividad global de 30 min en 'Modo Solo Aprendizaje' para pasar a evaluación.
        """
        while True:
            if not self.is_paused:
                active_title = self.get_active_window_title()

                # PASO 1: Validación básica de ventana permitida
                permitida, motivo_perm = self.pre_filter.is_window_allowed(active_title)
                if not permitida:
                    if "ventana propia del agente" in motivo_perm.lower():
                        self.app.after(0, self.app.update_eye_output, "🛡️ REPOSO: Ventana del Agente en primer plano.\nMonitoreo visual activo esperando interacción en Artemis o herramientas de red.")
                        self.app.after(0, self.app.update_inference_output, "Reposo: Agente en primer plano. Esperando ventana operativa.")
                    else:
                        self.app.after(0, self.app.update_eye_output, f"🙈 Ventana excluida:\n[{active_title}]\nMotivo: {motivo_perm}")
                        self.app.after(0, self.app.update_inference_output, f"Reposo: {motivo_perm}")
                else:
                    # PASO 2: Captura de pantalla con recorte de Artemis
                    raw_text = self.vision.capture_and_read(active_title)
                    current_img = self.vision.last_image

                    # PASO 2.1: Pre-filtro de cambio de píxeles y detección de cambio de ventana
                    window_changed = (active_title != getattr(self, "_last_seen_title", ""))
                    self._last_seen_title = active_title

                    if window_changed:
                        # Si el operador cambió de ventana, forzar evaluación inmediata
                        has_pixel_change = True
                    else:
                        has_pixel_change = self.pre_filter.has_significant_pixel_change(current_img, threshold=0.02)

                    if not has_pixel_change and not raw_text.startswith("ERROR_OCR:"):
                        # Pantalla estática en la misma ventana: ahorro del 100% de llamadas a IA
                        pass
                    else:
                        # PASO 3: Blindaje de Seguridad contra Pantallas de Login
                        es_login, motivo_login = self.pre_filter.is_login_or_forbidden(active_title, raw_text)
                        if es_login:
                            self.last_valid_image = None
                            self.last_valid_ocr = "Contexto omitido por seguridad (Login detectado)."
                            self.app.after(0, self.app.update_eye_output, f"🔒 PANTALLA DE LOGIN DESCARTADA\n\n{motivo_login}")
                            self.app.after(0, self.app.update_inference_output, "Seguridad activa: Pantalla de login descartada automáticamente.")
                            log.info(f"Pantalla descartada por seguridad: {motivo_login}")
                        elif raw_text.startswith("ERROR_OCR:"):
                            self.app.after(0, self.app.update_eye_output, f"⚠️ Fallo OCR:\n{raw_text}")
                        elif len(raw_text.strip()) > LONGITUD_MINIMA_OCR:
                            # PASO 4: Pre-filtro Anti-Spam de texto duplicado (>92%)
                            is_dup = self.pre_filter.is_duplicate_text(raw_text, threshold=0.92)
                            
                            self.last_valid_ocr = raw_text
                            self.last_valid_image = current_img
                            self.last_valid_title = active_title

                            # PASO 5: Extracción de Anclas
                            anchors = self.pre_filter.extract_anchors(raw_text, active_title)

                            # PASO 6: Enrutamiento Inteligente Concurrente (Nueva cubeta o caso en curso)
                            target_bucket, es_nuevo_paso, needs_block_summary = self.bucket_manager.route_capture(
                                active_title, raw_text, anchors
                            )

                            if target_bucket is None:
                                eye_text = (
                                    f"👁️ VENTANA OBSERVADA: {active_title}\n"
                                    f"ESTADO: ⏸️ En espera de atención técnica\n"
                                    f"DETALLE: Abre un chat con un técnico en Artemis o selecciona una cubeta activa en la barra superior para registrar diagnósticos en Winbox/PuTTY/MobaXterm."
                                )
                                self.app.after(0, self.app.update_eye_output, eye_text)
                                self.app.after(0, self.app.update_inference_output, "Esperando inicio de chat de soporte o selección de cubeta en curso.")
                            else:
                                self._refresh_pills_ui()

                                # Si se acumularon 8 pasos, programar resumen de bloque en segundo plano
                                if needs_block_summary:
                                    self.bg_tasks_queue.put({
                                        "type": "compress_block",
                                        "bucket": target_bucket,
                                        "steps": [s.to_dict() for s in target_bucket.steps]
                                    })

                                    pasos_info = f"{len(target_bucket.steps)}/8 en bloque ({target_bucket.total_steps_lifetime} acumulados)"
                                    cun_str = target_bucket.id_servicio_cun or "Sin CUN"
                                    sitio_str = target_bucket.codigo_sitio or "Sin Sitio"
                                    herramientas_str = ", ".join(target_bucket.herramientas_usadas) if target_bucket.herramientas_usadas else "General"

                                    if is_dup:
                                        eye_text = (
                                            f"👁️ VENTANA: {active_title}\n"
                                            f"CUBETA ACTIVA: [{target_bucket.tecnico_campo}] | CUN: {cun_str} | NODO: {sitio_str}\n"
                                            f"ESTADO: ⏸️ Pantalla sin cambios significativos ({pasos_info})\n"
                                            f"HERRAMIENTAS: {herramientas_str}"
                                        )
                                    else:
                                        # Extraer líneas significativas limpias del chat o consola
                                        lineas = [l.strip() for l in raw_text.split('\n') if len(l.strip()) > 8 and not any(w in l.lower() for w in ["conversaciones", "pendientes", "escribe un mensaje", "modo solo aprendizaje", "evaluar aprendizaje"])]
                                        extracto_limpio = "\n".join(lineas[-4:]) if lineas else "Atención técnica en curso."

                                        eye_text = (
                                            f"👁️ VENTANA: {active_title}\n"
                                            f"CUBETA ACTIVA: [{target_bucket.tecnico_campo}] | CUN: {cun_str} | NODO: {sitio_str}\n"
                                            f"ESTADO: ✅ Paso registrado ({pasos_info})\n"
                                            f"HERRAMIENTAS: {herramientas_str}\n"
                                            f"──────────────────────────────────────────────────\n"
                                            f"ÚLTIMA ACCIÓN OBSERVADA:\n"
                                            f"{extracto_limpio}"
                                        )

                                    self.app.after(0, self.app.update_eye_output, eye_text)

                                    # En modo Solo Aprendizaje: Cero interrupciones, cero llamadas a inferencia en vivo, cero 'Asking'
                                    if self.is_learning_mode:
                                        self.is_asking = False
                                        self.app.after(0, self.app.update_inference_output, f"🎓 Modo Solo Aprendizaje activo: Capturando en segundo plano ({target_bucket.tecnico_campo} | {target_bucket.total_steps_lifetime} pasos acumulados)...")
                                    elif not self.is_asking:
                                        self.app.after(0, self.app.update_inference_output, f"Analizando con Gemini (Cubeta: {target_bucket.tecnico_campo})...")
                                        analisis = self.llm.analizar_texto_pantalla(raw_text)

                                        if analisis and analisis.es_problema_tecnico_real:
                                            sig = f"{target_bucket.tecnico_campo}_{analisis.descripcion_problema}"
                                            if sig != self.last_problem_signature:
                                                self.last_problem_signature = sig
                                                self.is_asking = True
                                                self.current_visual_context = analisis.descripcion_problema
                                                self.current_agent_suggestion = analisis.accion_sugerida

                                                inf_text = (
                                                    f"¡Novedad detectada para {target_bucket.tecnico_campo}!\n"
                                                    f"Problema: {analisis.descripcion_problema}\n"
                                                    f"Sugerencia: {analisis.accion_sugerida}\n"
                                                    f"Confianza: {analisis.nivel_confianza}\n\n"
                                                    f"Nota: Presiona 'Aprobar Sugerencia' para validarla, o '🏁 Finalizar Caso' para cerrar la cubeta."
                                                )
                                                self.app.after(0, self.app.set_state, "asking")
                                                self.app.after(0, self.app.update_inference_output, inf_text)
                                                self.app.after(0, self.app.append_chat, "Agente", f"🚨 Detecté una novedad con {target_bucket.tecnico_campo}: {analisis.descripcion_problema}.\nMi sugerencia es: '{analisis.accion_sugerida}'.")
                                            else:
                                                self.app.after(0, self.app.update_inference_output, f"Actividad normal en {target_bucket.tecnico_campo}. Observando...")
                        else:
                            eye_text = (
                                f"👁️ VENTANA: {active_title}\n"
                                f"ESTADO: ⏸️ En reposo (Contenido técnico mínimo insuficiente para análisis).\n"
                                f"DETALLE: Esperando contenido visible o selección de chat activo."
                            )
                            self.app.after(0, self.app.update_eye_output, eye_text)

            # =================================================================
            # TEMPORIZADORES DE AUTO-CIERRE E INACTIVIDAD
            # =================================================================
            # 1. Cierre individual de cubeta por 10 minutos de inactividad
            expired_buckets = self.bucket_manager.check_expired_buckets()
            for exp_b in expired_buckets:
                log.info(f"Cubeta [{exp_b.bucket_id}] superó 10 min de inactividad. Moviendo a staging...")
                closed_b = self.bucket_manager.close_and_extract_bucket(exp_b.bucket_id)
                if closed_b:
                    self.bg_tasks_queue.put({
                        "type": "auto_close_staging",
                        "bucket": closed_b
                    })

            # 2. Cierre global del Modo Solo Aprendizaje tras 30 minutos de inactividad
            if self.is_learning_mode and self.bucket_manager.check_global_inactivity():
                log.info("30 minutos de inactividad global detectados en Modo Solo Aprendizaje. Transicionando a evaluación...")
                self.is_learning_mode = False
                self.is_paused = True
                self.app.after(0, self.app.set_state, "paused")
                self.app.after(0, self.app.switch_to_tab, "📋 Evaluar Aprendizaje")
                self.app.after(0, self.app.append_chat, "Agente", "⏰ 30 minutos de inactividad global completados. El Modo Solo Aprendizaje se ha cerrado automáticamente. Por favor revisa las tarjetas en '📋 Evaluar Aprendizaje'.")
                
                # Cerrar cualquier cubeta remanente hacia staging
                for b_id in list(self.bucket_manager.active_buckets.keys()):
                    rem_b = self.bucket_manager.close_and_extract_bucket(b_id)
                    if rem_b:
                        self.bg_tasks_queue.put({
                            "type": "auto_close_staging",
                            "bucket": rem_b
                        })

            time.sleep(INTERVALO_VISION_SEGUNDOS)

    def run(self):
        """Inicia el loop principal de la interfaz gráfica CustomTkinter."""
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.app.mainloop()

if __name__ == "__main__":
    check_and_install_tesseract()
    agent_app = MesaAyudaAgentApp()
    agent_app.run()
