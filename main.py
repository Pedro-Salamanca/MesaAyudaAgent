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
    INTERVALO_POLL_LIGERO_SEGUNDOS,
    DEBOUNCE_ESTABILIDAD_SEGUNDOS,
    MAX_DEBOUNCE_CEILING_SEGUNDOS,
    MIN_FLUSH_TAB_SWITCH_SEGUNDOS,
    LONGITUD_MINIMA_OCR,
    MAX_CASOS_HISTORICOS_RAG,
    MODO_APRENDIZAJE,
    MODO_GUIA_CONTINUA,
    MODO_SILENCIOSO_ASISTIR
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
    Controlador Principal de MesaAyudaAgent v4.0 (SOMOS NETWORK).
    Orquesta:
    1. Motor Visual Ultraligero con Detección por Cuadrantes 4x4 + Debounce de 3.5s
       (techo 8.0s y flush de 1.5s al cambiar de pestaña: 0% OCR en pantalla quieta, 0 pérdida en misma página).
    2. Pirámide de Memoria Híbrida (8 Manuales v2.0 + 206 Casos de Oro float32 BLOB + 2,880 Casos en SQLite FTS5)
       con espejo local de lectura rápida en `%LOCALAPPDATA%\\MesaAyudaAgent\\cache_v4.db`.
    3. Pre-Clasificador Heurístico Local (<0.5 ms) hacia las 7 Categorías Oficiales de SOMOS.
    4. Triángulo de Grounding (Manual de Categoría + Árbol Determinista + Casos de Oro + Historial de Cubeta).
    5. Operación Tri-Modo:
       - `🧭 Guía Continua` (`continuous`)
       - `⚡ Silencioso + Asistir` (`on_demand`)
       - `🎓 Solo Aprendizaje` (`learning`)
    6. Botón `🧠 Asistir Caso Actual` + Atajo Nativo Win32 (`Ctrl+Shift+Espacio`) y Respuesta Sugerida con Tono SOMOS (`📋 Copiar`).
    """
    def __init__(self):
        self.tecnico_nombre = TECNICO_NOMBRE_DEFAULT
        self.last_problem_signature = ""

        # Componentes de Inteligencia, Visión y Memoria Piramidal v4.0
        # restore_previous_session=False garantiza que al abrir el agente inicie con 0 cubetas abiertas
        self.pre_filter = PreFilterEngine()
        self.bucket_manager = BucketManager(
            max_concurrent=5,
            ttl_bucket_minutes=10,
            global_inactivity_minutes=30,
            restore_previous_session=False,
            persist_checkpoint=True
        )
        self.brain = AgentBrain()
        self.llm = LLMClient(db=self.brain.db)
        self.search_engine = MemorySearchEngine(db=self.brain.db)
        self.vision = ScreenReader()

        # Cola de tareas en segundo plano para compresión y síntesis sin bloquear la interfaz
        self.bg_tasks_queue = queue.Queue()

        # Interfaz Gráfica con Callbacks Completos v4.0
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
            mode_change_callback=self.on_change_operation_mode,
            assist_now_callback=self.on_assist_current_case,
            tecnico_nombre=self.tecnico_nombre
        )

        # Variables de Control de Flujo y Modo Operativo
        self.is_paused = False
        self.is_asking = False
        self.operation_mode = MODO_GUIA_CONTINUA
        self.is_learning_mode = False
        self._assist_in_progress = False
        self.current_visual_context = ""
        self.current_agent_suggestion = ""
        self.last_valid_ocr = "Esperando lectura inicial..."
        self.last_valid_image = None
        self.last_valid_title = ""

        # Inicialización de estado y tarjetas en la UI
        self._refresh_pills_ui()
        self.refresh_staging_ui()
        self.refresh_memory_ui()

        # Hilos en segundo plano
        self.worker_thread = None
        self.vision_thread = None
        self.hotkey_thread = None

        log.info("Componentes de MesaAyudaAgent v4.0 preparados para iniciar.")

    def start_background_threads(self):
        """Inicia los hilos en segundo plano una vez que la interfaz gráfica y su mainloop están activos."""
        if not self.worker_thread or not self.worker_thread.is_alive():
            self.worker_thread = threading.Thread(target=self._background_worker_loop, daemon=True)
            self.worker_thread.start()

        if not self.vision_thread or not self.vision_thread.is_alive():
            self.vision_thread = threading.Thread(target=self.vision_loop, daemon=True)
            self.vision_thread.start()

        if not self.hotkey_thread or not self.hotkey_thread.is_alive():
            self.hotkey_thread = threading.Thread(target=self._win32_hotkey_listener, daemon=True)
            self.hotkey_thread.start()

        log.info("Agente MesaAyudaAgent v4.0: Hilos de visión (Debounce 3.5s), worker y atajo Win32 iniciados con éxito.")

    def _win32_hotkey_listener(self):
        """
        Escucha el atajo global nativo de Windows `Ctrl + Shift + Espacio` mediante Win32 API (`RegisterHotKey`),
        sin instalar librerías de hooking de teclado externas que disparen alertas de antivirus.
        """
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            MOD_CONTROL = 0x0002
            MOD_SHIFT = 0x0004
            MOD_NOREPEAT = 0x4000
            VK_SPACE = 0x20
            HOTKEY_ID = 104

            if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE):
                log.info("Atajo global Ctrl+Shift+Espacio no disponible (en uso por otra ventana); botón UI activo.")
                return

            log.info("Atajo global nativo Win32 'Ctrl+Shift+Espacio' registrado para Asistencia Inmediata.")
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:
                    self.app.after(0, self.on_assist_current_case)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.warning(f"Listener de atajo Win32 finalizado: {e}")

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
            cat_str = getattr(focused, "categoria_actual", "Soporte General")
            info_txt = (
                f"Técnico: {focused.tecnico_campo}\n"
                f"Sitio: {focused.codigo_sitio or 'N/A'} | ID/CUN: {focused.id_servicio_cun or 'N/A'}\n"
                f"Categoría: {cat_str}\n"
                f"Pasos acumulados: {focused.total_steps_lifetime} ({len(focused.steps)} en bloque)\n"
                f"Herramientas: {', '.join(tools)}"
            )
            if getattr(focused, "last_diagnosis_card", "") and self.operation_mode != MODO_APRENDIZAJE:
                self.app.after(0, self.app.update_inference_output, focused.last_diagnosis_card)
            if getattr(focused, "last_somos_suggestion", "") and self.operation_mode != MODO_APRENDIZAJE:
                self.app.after(0, self.app.update_somos_suggestion, focused.last_somos_suggestion)
        else:
            info_txt = "Técnico: Esperando interacción en Artemis/herramientas...\nSitio / ID: -\nCategoría: -\nPasos registrados: 0"

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
    # CONTROL DE PAUSAS Y MODOS DE TRABAJO (TRI-MODO v4.0)
    # =========================================================================
    def on_toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.app.set_state("paused")
        else:
            self._restore_mode_visual_state()

    def _restore_mode_visual_state(self):
        if self.operation_mode == MODO_APRENDIZAJE:
            self.app.set_state("learning")
        elif self.operation_mode == MODO_SILENCIOSO_ASISTIR:
            self.app.set_state("on_demand")
        else:
            self.app.set_state("active")

    def on_change_operation_mode(self, mode_code: str):
        """
        Conmuta entre los 3 modos operativos del Copiloto v4.0:
        - `continuous` (🧭 Guía Continua): Encubetado continuo + inferencia guiada automática en hitos técnicos.
        - `on_demand` (⚡ Silencioso + Asistir): Encubetado continuo con 0 llamadas a IA de fondo; asiste al pulsar el botón/atajo.
        - `learning` (🎓 Solo Aprendizaje): Encubetado silencioso orientado a tarjetas de evaluación.
        """
        self.operation_mode = mode_code
        self.is_learning_mode = (mode_code == MODO_APRENDIZAJE)
        self.is_asking = False

        if mode_code == MODO_APRENDIZAJE:
            self.app.set_state("learning")
            self.app.after(0, self.app.update_inference_output, "🎓 Modo Solo Aprendizaje activo.\nCapturando y encubetando atenciones en segundo plano (0 inferencias en vivo)...")
            self.app.after(0, self.app.append_chat, "Agente", "🎓 Modo Solo Aprendizaje ACTIVADO.")
        elif mode_code == MODO_SILENCIOSO_ASISTIR:
            self.app.set_state("on_demand")
            self.app.after(
                0,
                self.app.update_inference_output,
                "⚡ Modo Silencioso + Asistir activo.\n"
                "Estoy encubetando todo el trayecto del caso en segundo plano con 0% de consumo de IA.\n"
                "Cuando necesites el diagnóstico y la respuesta en tono SOMOS, pulsa '🧠 Asistir Caso Actual' o Ctrl+Shift+Espacio."
            )
            self.app.after(0, self.app.append_chat, "Agente", "⚡ Modo Silencioso + Asistir ACTIVADO. Encubetando en silencio; pulsa '🧠 Asistir Caso Actual' cuando requieras guía.")
        else:
            self.app.set_state("active")
            self.app.after(0, self.app.update_inference_output, "🧭 Modo Guía Continua activo.\nActualizando árbol de diagnóstico y respuesta sugerida automáticamente ante nuevos hitos técnicos.")
            self.app.after(0, self.app.append_chat, "Agente", "🧭 Modo Guía Continua ACTIVADO.")
        log.info(f"Modo de operación cambiado a: {mode_code}")

    def on_toggle_break_pause(self, is_break: bool):
        self.is_paused = is_break
        self.is_asking = False
        if is_break:
            self.app.set_state("break")
            self.app.after(0, self.app.update_inference_output, "☕ Pausa de turno (Break) activa. Captura visual detenida.")
            self.app.after(0, self.app.append_chat, "Agente", "☕ Turno en pausa (Break). No capturaré pantallas hasta que pulses '▶️ Reanudar Turno'.")
            log.info("Pausa de turno (Break) activada.")
        else:
            self._restore_mode_visual_state()
            self.app.after(0, self.app.append_chat, "Agente", "▶️ Turno reanudado. Retomando observación con Debounce de 3.5s.")
            log.info("Turno reanudado tras el break.")

    def on_pause_and_evaluate(self):
        self.is_paused = True
        self.is_asking = False
        self.app.set_state("paused")
        self.refresh_staging_ui()
        self.app.after(0, self.app.update_inference_output, "📋 Observación en pausa. Mostrando tarjetas de evaluación de aprendizaje...")
        self.app.after(0, self.app.append_chat, "Agente", "📋 Modo evaluación abierto. Revisa las tarjetas de los casos aprendidos.")
        log.info("Modo evaluación de aprendizaje activado por el operador.")

    def on_toggle_learning_mode(self, enabled: bool):
        """Compatibilidad con callback booleano de Modo Solo Aprendizaje."""
        self.on_change_operation_mode(MODO_APRENDIZAJE if enabled else MODO_GUIA_CONTINUA)

    # =========================================================================
    # MOTOR DE ASISTENCIA GUIADA (TRIÁNGULO DE GROUNDING + ÁRBOL + TONO SOMOS)
    # =========================================================================
    def on_assist_current_case(self):
        """
        Acción disparada por el botón '🧠 Asistir Caso Actual' o el atajo global 'Ctrl+Shift+Espacio'.
        Compila TODO el historial acumulado de la cubeta en foco (o la pantalla actual si aún no hay cubeta)
        y ejecuta el Triángulo de Grounding (Manual v2.0 + Árbol Determinista + Casos de Oro).
        """
        if self._assist_in_progress:
            return
        focused = self.bucket_manager.get_focused_bucket()
        raw_ctx = self.last_valid_ocr if self.last_valid_ocr else ""
        threading.Thread(
            target=self._execute_guided_assistance,
            args=(focused, raw_ctx, True),
            daemon=True
        ).start()

    def _execute_guided_assistance(self, bucket: Optional[CaseBucket], raw_text: str, triggered_by_user: bool = False):
        """
        Ejecuta la síntesis del Triángulo de Grounding en segundo plano y actualiza de forma silenciosa
        el panel de Diagnóstico Guiado (Zona 3) y la tarjeta de Respuesta Sugerida (Tono SOMOS).
        """
        if self._assist_in_progress:
            return
        self._assist_in_progress = True
        try:
            if bucket:
                timeline_text = bucket.compile_timeline_text()
                tec_name = bucket.tecnico_campo
                step_text = bucket.steps[-1].ocr_text if bucket.steps else raw_text
                win_title = bucket.steps[-1].window_title if bucket.steps else self.last_valid_title
            else:
                # Si el operador pulsa Asistir Ahora antes de que se haya creado cubeta, capturar al instante
                active_title = self.get_active_window_title()
                permitida, _ = self.pre_filter.is_window_allowed(active_title)
                if permitida and (not raw_text or raw_text == "Esperando lectura inicial..."):
                    img = self.vision.capture_frame(active_title)
                    raw_text = self.vision.extract_text(img) if img else ""
                timeline_text = raw_text
                tec_name = "Técnico en Sitio"
                step_text = raw_text
                win_title = self.last_valid_title or active_title

            if not step_text or len(step_text.strip()) < 10:
                if triggered_by_user:
                    self.app.after(
                        0,
                        self.app.update_inference_output,
                        "ℹ️ Aún no hay texto técnico suficiente en la cubeta o pantalla activa.\n"
                        "Abre el chat en Artemis/Selene o la consola técnica y vuelve a pulsar '🧠 Asistir Caso Actual'."
                    )
                return

            # 1. Pre-clasificación local instantánea (<0.5 ms) hacia las 7 Categorías Oficiales
            anchors = self.pre_filter.extract_anchors(step_text, win_title)
            cat_oficial, _ = self.pre_filter.classify_category(f"{timeline_text}\n{step_text}", win_title, anchors)
            if bucket:
                bucket.categoria_actual = cat_oficial

            self.app.after(
                0,
                self.app.update_inference_output,
                f"🧠 Sintetizando Guía Oficial SOMOS v4.0...\n"
                f"• Técnico: {tec_name} | Categoría pre-filtrada: [{cat_oficial}]\n"
                f"• Consultando Manual Base v2.0, Árbol Determinista y Casos de Oro..."
            )

            # 2. Recuperación Piramidal (Nivel 2 float32 BLOB pre-filtrado + Nivel 3 SQLite FTS5)
            query_for_rag = f"{cat_oficial} {step_text[:500]}"
            q_vec = self.llm.generar_embedding(query_for_rag)
            casos_similares = self.search_engine.search(
                query=query_for_rag,
                top_k=MAX_CASOS_HISTORICOS_RAG,
                query_vector=q_vec,
                categoria_falla=cat_oficial
            )

            # 3. Síntesis del Triángulo de Grounding
            guia = self.llm.asistir_caso_guiado(
                timeline_text=timeline_text,
                texto_pantalla_actual=step_text,
                categoria_preclasificada=cat_oficial,
                casos_rag=casos_similares,
                tecnico_nombre=tec_name
            )

            if guia and (guia.es_problema_tecnico_real or triggered_by_user):
                self.current_visual_context = guia.diagnostico_tecnico
                self.current_agent_suggestion = guia.mensaje_sugerido_somos

                card_text = (
                    f"🏷️ CATEGORÍA: {guia.categoria_falla}  |  📍 {guia.fase_actual}\n"
                    f"────────────────────────────────────────────────────\n"
                    f"🧠 DIAGNÓSTICO TÉCNICO (CAUSA RAÍZ):\n"
                    f"{guia.diagnostico_tecnico}\n\n"
                    f"🌲 PASO ACTUAL DEL ÁRBOL DE DESCARTE:\n"
                    f"▶ {guia.paso_arbol_actual}\n\n"
                    f"⏭️ SIGUIENTE VERIFICACIÓN EN COLA:\n"
                    f"• {guia.siguiente_descarte}\n"
                    f"────────────────────────────────────────────────────\n"
                    f"📚 FUENTES OFICIALES: {guia.fuentes_grounding} (Confianza: {guia.nivel_confianza})"
                )

                if bucket:
                    bucket.categoria_actual = guia.categoria_falla
                    bucket.last_inferred_step = bucket.total_steps_lifetime
                    bucket.last_diagnosis_card = card_text
                    bucket.last_somos_suggestion = guia.mensaje_sugerido_somos

                # Actualización silenciosa de HUD (sin cambiar foco ni robar teclado)
                self.app.after(0, self.app.update_inference_output, card_text)
                self.app.after(0, self.app.update_somos_suggestion, guia.mensaje_sugerido_somos)
                self._refresh_pills_ui()

                if triggered_by_user:
                    self.app.after(
                        0,
                        self.app.append_chat,
                        "Agente",
                        f"🧠 Guía generada para {tec_name} [{guia.categoria_falla}]:\n"
                        f"• Diagnóstico: {guia.diagnostico_tecnico}\n"
                        f"• Respuesta sugerida lista para copiar en el panel izquierdo."
                    )
        except Exception as e:
            log.error(f"Error en ejecución de asistencia guiada v4.0: {e}")
        finally:
            self._assist_in_progress = False

    # =========================================================================
    # GESTIÓN DE EVALUACIÓN DE TARJETAS (STAGING)
    # =========================================================================
    def on_approve_staging(self, id_cubeta: str, solucion: str):
        threading.Thread(target=self._async_approve_staging, args=(id_cubeta, solucion), daemon=True).start()

    def _async_approve_staging(self, id_cubeta: str, solucion: str):
        self.app.after(0, self.app.update_inference_output, f"Aprobando caso [{id_cubeta}]...")
        vector = self.llm.generar_embedding(solucion)
        ok = self.brain.approve_staging_case(id_cubeta, solucion, operador=self.tecnico_nombre, vector=vector)
        if ok:
            self.app.after(0, self.app.append_chat, "Agente", f"✅ Caso [{id_cubeta}] aprobado con éxito y guardado en memory_v3.db!")
            self.refresh_staging_ui()
            self.refresh_memory_ui()
        else:
            self.app.after(0, self.app.append_chat, "Agente", f"⚠️ No se pudo aprobar el caso [{id_cubeta}]. Verifica que la solución sea una acción técnica válida.")

    def on_correct_staging(self, id_cubeta: str, operator_text: str, case_dict: dict):
        if not operator_text or not operator_text.strip():
            self.app.after(0, self.app.append_chat, "Agente", "⚠️ Por favor escribe tu respuesta o corrección en el campo de texto antes de presionar 'Responder / Corregir y Guardar'.")
            return
        threading.Thread(target=self._async_correct_staging, args=(id_cubeta, operator_text, case_dict), daemon=True).start()

    def _async_correct_staging(self, id_cubeta: str, operator_text: str, case_dict: dict):
        self.app.after(0, self.app.update_inference_output, f"Integrando corrección del operador para [{id_cubeta}]...")
        solucion_final = self.llm.integrar_correccion_humana(case_dict, operator_text)
        vector = self.llm.generar_embedding(solucion_final)
        ok = self.brain.approve_staging_case(id_cubeta, solucion_final, operador=self.tecnico_nombre, vector=vector)
        if ok:
            self.app.after(0, self.app.append_chat, "Agente", f"✏️ Corrección integrada para [{id_cubeta}]. Solución oficial registrada:\n\"{solucion_final}\"")
            self.refresh_staging_ui()
            self.refresh_memory_ui()
        else:
            self.app.after(0, self.app.append_chat, "Agente", f"⚠️ Error registrando corrección para [{id_cubeta}].")

    def on_discard_staging(self, id_cubeta: str):
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
        self.brain.delete_case(case_id)
        self.refresh_memory_ui()

    def on_select_bucket(self, bucket_id: str):
        self.bucket_manager.set_focused_bucket(bucket_id)
        self._refresh_pills_ui()
        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            self.app.append_chat("Agente", f"🔄 Foco cambiado al caso de: {focused.tecnico_campo} (Sitio: {focused.codigo_sitio or 'N/A'} | Categoría: {focused.categoria_actual}).")

    def on_approve_suggestion(self):
        focused = self.bucket_manager.get_focused_bucket()
        if self.current_agent_suggestion:
            sug = self.current_agent_suggestion
            if focused:
                focused.approved_suggestions.append(sug)
                self.bucket_manager.save_checkpoint_to_disk()

            self.app.append_chat(self.tecnico_nombre, f"✅ Sugerencia técnica aprobada: '{sug}'")
            self.app.append_chat("Agente", "👍 Sugerencia guardada en el historial del caso. Cuando la atención concluya, presiona '🏁 Finalizar Caso'.")
            self.is_asking = False
            self._restore_mode_visual_state()
            self._refresh_pills_ui()
            log.info(f"Sugerencia aprobada para la cubeta actual: {sug}")
        else:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna sugerencia técnica activa para aprobar en este momento.")

    def on_close_case(self, user_solution: str = ""):
        focused = self.bucket_manager.get_focused_bucket()
        if not focused:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna cubeta técnica activa para finalizar.")
            return

        # Cerrar y retirar la cubeta de la barra superior DE INMEDIATO (0.01s) para evitar doble clic
        closed_bucket = self.bucket_manager.close_and_extract_bucket(focused.bucket_id)
        self._refresh_pills_ui()
        self.app.update_inference_output(f"🏁 Cerrando y sintetizando caso de {focused.tecnico_campo}...")
        threading.Thread(target=self._process_case_finalization, args=(closed_bucket, user_solution), daemon=True).start()

    def _process_case_finalization(self, bucket: CaseBucket, user_solution: str):
        b_id = bucket.bucket_id
        tec_name = bucket.tecnico_campo
        pasos_total = bucket.total_steps_lifetime

        self.app.after(0, self.app.append_chat, "Agente", f"🏁 Finalizando atención de {tec_name} ({pasos_total} pasos)... Sintetizando caso con IA...")

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
            categoria_falla = bucket.categoria_actual or "Soporte General"
            ciudad_nodo = bucket.codigo_sitio or "Desconocido"
            herramienta = "General"
            resumen_problema = f"Atención técnica con {tec_name}"
            if not solucion_candidata:
                solucion_candidata = self.current_agent_suggestion or "Validación técnica y verificación de servicio completada en plataformas SOMOS."

        valido, motivo = self.brain.db.validate_gatekeeper(solucion_candidata)
        if not valido:
            # Si Gatekeeper rechaza porque el operador escribió una palabra inválida, restaurar cubeta para que corrija
            bucket.status = "active"
            self.bucket_manager.active_buckets[b_id] = bucket
            self.bucket_manager.current_focused_bucket_id = b_id
            self._refresh_pills_ui()
            msg_alerta = f"⚠️ Gatekeeper: No se pudo guardar el caso porque la solución indicada no es válida ({motivo}). Por favor escribe la solución técnica en la caja de texto y pulsa '🏁 Finalizar Caso'."
            self.app.after(0, self.app.append_chat, "Agente", msg_alerta)
            self.app.after(0, self.app.update_inference_output, "Gatekeeper: Solución técnica requerida.")
            return

        self.app.after(0, self.app.update_inference_output, "Generando vector semántico float32 BLOB (gemini-embedding-001)...")
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
            self.is_asking = False
            self.current_agent_suggestion = ""
            self.last_problem_signature = ""

            ruta_pasos_str = episodio.ruta_diagnostico_pasos if episodio else "Síntesis directa de pasos de cubeta."
            sintesis_panel_txt = (
                f"🏁 SÍNTESIS OFICIAL DEL CASO FINALIZADO (Guardado en memory_v3.db)\n"
                f"────────────────────────────────────────────────────\n"
                f"👤 Técnico: {tec_name}  |  🏷️ Categoría: {categoria_falla}  |  🛠️ Herramienta: {herramienta}\n"
                f"📍 Nodo/Ciudad: {ciudad_nodo}  |  🔢 Pasos sintetizados: {pasos_total}\n\n"
                f"🔍 PROBLEMA SINTETIZADO POR IA:\n{resumen_problema}\n\n"
                f"🧭 RUTA DE DIAGNÓSTICO:\n{ruta_pasos_str}\n\n"
                f"✅ SOLUCIÓN OFICIAL REGISTRADA:\n{solucion_candidata}\n"
                f"────────────────────────────────────────────────────\n"
                f"📦 Vector Embedding: {'Generado (3,072d float32 BLOB)' if vector else 'Pendiente'} — Listo para próximas consultas RAG."
            )

            self.app.after(
                0,
                self.app.append_chat,
                "Agente",
                f"✅ Caso finalizado, sintetizado y almacenado con éxito en memory_v3.db!\n"
                f"• Técnico: {tec_name} ({pasos_total} pasos)\n"
                f"• Categoría: {categoria_falla}\n"
                f"• Problema Sintetizado: {resumen_problema}\n"
                f"• Solución Oficial: {solucion_candidata}\n"
                f"• Embedding: {'Generado (3072d float32 BLOB)' if vector else 'Pendiente'}"
            )
            self.app.after(0, self._restore_mode_visual_state)
            self.app.after(0, self.app.update_inference_output, sintesis_panel_txt)
            self._refresh_pills_ui()
            self.app.after(0, self.refresh_memory_ui)
        else:
            self.app.after(0, self.app.append_chat, "Agente", "❌ Ocurrió un error al intentar registrar el caso en la base de datos.")

    def on_discard_bucket(self):
        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            b_id = focused.bucket_id
            tec = focused.tecnico_campo
            self.bucket_manager.close_and_extract_bucket(b_id)
            self.is_asking = False
            self.current_agent_suggestion = ""
            self.last_problem_signature = ""
            self.app.append_chat("Agente", f"🗑️ Cubeta de {tec} descartada sin guardar.")
            self._restore_mode_visual_state()
            self._refresh_pills_ui()

    def on_force_save(self, msg: str):
        valido, motivo = self.brain.db.validate_gatekeeper(msg)
        if not valido:
            self.app.append_chat("Agente", f"⚠️ Solución no válida: {motivo}")
            return

        cat_det, _ = self.pre_filter.classify_category(f"{self.last_valid_ocr} {msg}", self.last_valid_title)
        extra = {
            "tecnico_campo": "Guardado Manual",
            "categoria_falla": cat_det,
            "estado_validacion": "FORZADO_MANUAL"
        }
        vec = self.llm.generar_embedding(f"{cat_det} | {self.last_valid_ocr[:300]} | {msg}")
        self.brain.save_case(
            visual_context=f"Guardado manual: {self.last_valid_ocr[:200]}...",
            action=msg,
            agent_suggestion="",
            image_obj=self.last_valid_image,
            active_title=self.last_valid_title,
            tecnico_nombre=self.tecnico_nombre,
            vector=vec,
            extra_metadata=extra
        )
        self.app.append_chat("Agente", f"¡Guardado Forzado exitoso en memory_v3.db (Categoría: {cat_det})!")
        self.refresh_memory_ui()

    def on_chat_message(self, msg: str):
        threading.Thread(target=self._process_chat_question, args=(msg,), daemon=True).start()

    def _process_chat_question(self, msg: str):
        """Ejecuta consulta RAG Piramidal combinando Pre-Filtro de Categoría + Casos de Oro BLOB + FTS5 + Manual v2.0."""
        focused = self.bucket_manager.get_focused_bucket()
        contexto_cubeta = focused.compile_timeline_text() if focused else self.last_valid_ocr[:1000]
        cat_consulta, _ = self.pre_filter.classify_category(f"{msg}\n{contexto_cubeta}", self.last_valid_title)

        self.app.after(0, self.app.append_chat, "Agente", f"🔍 Buscando en Pirámide RAG (Categoría: {cat_consulta})...")

        q_vec = self.llm.generar_embedding(msg)
        casos_similares = self.search_engine.search(
            msg,
            top_k=MAX_CASOS_HISTORICOS_RAG,
            query_vector=q_vec,
            categoria_falla=cat_consulta
        )

        memoria_contexto = ""
        if casos_similares:
            memoria_contexto = "\n[CASOS DE ORO E HISTÓRICOS FTS5 RELEVANTES]:\n"
            for c in casos_similares:
                cid = c.get("id", "caso")
                accion = c.get("action", "")
                sug = c.get("agent_suggestion", "")
                cat = c.get("categoria_falla", "General")
                memoria_contexto += f"- [{cid}] ({cat}): {accion} | Sugerencia Mesa: {sug}\n"

        contexto_completo = f"Línea de tiempo de la cubeta en curso:\n{contexto_cubeta}\n\n{memoria_contexto}"
        respuesta = self.llm.responder_pregunta(msg, contexto_completo, categoria_falla=cat_consulta)
        self.app.after(0, self.app.append_chat, "Agente", respuesta)

    def get_active_window_title(self) -> str:
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
        while True:
            try:
                task = self.bg_tasks_queue.get()
                task_type = task.get("type")

                if task_type == "compress_block":
                    bucket = task["bucket"]
                    steps_to_compress = list(task["steps"])
                    try:
                        resumen = self.llm.resumir_bloque_pasos(steps_to_compress)
                        bucket.compress_current_block(resumen)
                        self.bucket_manager.save_checkpoint_to_disk()
                        self._refresh_pills_ui()
                        log.info(f"Bloque de 8 pasos comprimido con éxito para {bucket.bucket_id}: {resumen[:80]}...")
                    finally:
                        bucket.is_compressing = False

                elif task_type == "auto_close_staging":
                    bucket: CaseBucket = task["bucket"]
                    b_id = bucket.bucket_id
                    timeline_text = bucket.compile_timeline_text()

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
                            "categoria_deducida": bucket.categoria_actual or "Soporte General",
                            "problema_observado": f"Atención registrada con {bucket.tecnico_campo}",
                            "solucion_deducida_ia": "Atención finalizada por inactividad funcional.",
                            "pregunta_duda_ia": "¿Qué procedimiento técnico específico se completó para este caso?",
                            "resumen_bloques": bucket.partial_summaries
                        }

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
    # PROCESAMIENTO DE FRAME ESTABILIZADO (POST-DEBOUNCE 3.5s)
    # =========================================================================
    def _process_stabilized_frame(self, active_title: str, current_img, trigger_reason: str = "Debounce 3.5s"):
        """
        Ejecuta Tesseract OCR en escala de grises UNA SOLA VEZ sobre el frame ya estabilizado,
        extrae anclas, pre-clasifica la categoría en <0.5 ms, alimenta la cubeta y despacha
        según el modo activo (`learning`, `on_demand` o `continuous`).
        """
        raw_text = self.vision.extract_text(current_img)
        if not raw_text:
            return

        # 1. Blindaje de Seguridad contra Pantallas de Login
        es_login, motivo_login = self.pre_filter.is_login_or_forbidden(active_title, raw_text)
        if es_login:
            self.last_valid_image = None
            self.last_valid_ocr = "Contexto omitido por seguridad (Login detectado)."
            self.app.after(0, self.app.update_eye_output, f"🔒 PANTALLA DE LOGIN DESCARTADA\n\n{motivo_login}")
            self.app.after(0, self.app.update_inference_output, "Seguridad activa: Pantalla de login descartada automáticamente.")
            log.info(f"Pantalla descartada por seguridad: {motivo_login}")
            return

        if raw_text.startswith("ERROR_OCR:"):
            self.app.after(0, self.app.update_eye_output, f"⚠️ Fallo OCR:\n{raw_text}")
            return

        if len(raw_text.strip()) <= LONGITUD_MINIMA_OCR:
            eye_text = (
                f"👁️ VENTANA: {active_title}\n"
                f"ESTADO: ⏸️ Contenido mínimo insuficiente ({len(raw_text.strip())} chars).\n"
                f"──────────────────────────────────────────────────\n"
                f"{raw_text.strip() if raw_text.strip() else '(Sin texto legible)'}"
            )
            self.app.after(0, self.app.update_eye_output, eye_text)
            return

        # 2. Pre-filtro Anti-Spam de texto duplicado (>92%)
        is_dup = self.pre_filter.is_duplicate_text(raw_text, threshold=0.92)
        self.last_valid_ocr = raw_text
        self.last_valid_image = current_img
        self.last_valid_title = active_title

        # 3. Extracción de Anclas + Pre-Clasificación Local (<0.5 ms)
        anchors = self.pre_filter.extract_anchors(raw_text, active_title)
        cat_oficial, _ = self.pre_filter.classify_category(raw_text, active_title, anchors)

        # 4. Enrutamiento a Cubeta Concurrente
        target_bucket, _, needs_block_summary = self.bucket_manager.route_capture(
            active_title, raw_text, anchors
        )

        if target_bucket is None:
            eye_text = (
                f"👁️ VENTANA OBSERVADA ({trigger_reason}): {active_title}\n"
                f"CATEGORÍA DETECTADA: [{cat_oficial}]\n"
                f"ESTADO: ⏸️ En espera de ancla de chat en Artemis/Selene o selección de cubeta\n"
                f"──────────────────────────────────────────────────\n"
                f"TEXTO OCR EXTRAÍDO ({len(raw_text)} caracteres):\n"
                f"{raw_text.strip()}"
            )
            self.app.after(0, self.app.update_eye_output, eye_text)
            return

        target_bucket.categoria_actual = cat_oficial
        self._refresh_pills_ui()

        if needs_block_summary:
            self.bg_tasks_queue.put({
                "type": "compress_block",
                "bucket": target_bucket,
                "steps": [s.to_dict() for s in target_bucket.steps]
            })

        pasos_info = f"{len(target_bucket.steps)}/8 en bloque ({target_bucket.total_steps_lifetime} acumulados)"
        cun_str = target_bucket.id_servicio_cun or "Sin ID/CUN"
        sitio_str = target_bucket.codigo_sitio or "Sin Sitio"
        herramientas_str = ", ".join(target_bucket.herramientas_usadas) if target_bucket.herramientas_usadas else "General"
        estado_str = "⏸️ Texto similar" if is_dup else f"✅ Paso registrado ({trigger_reason})"

        eye_text = (
            f"👁️ VENTANA: {active_title}\n"
            f"CUBETA ACTIVA: [{target_bucket.tecnico_campo}] | ID/CUN: {cun_str} | NODO: {sitio_str}\n"
            f"CATEGORÍA: [{cat_oficial}] | {estado_str} ({pasos_info}) | TOOLS: {herramientas_str}\n"
            f"──────────────────────────────────────────────────\n"
            f"TEXTO OCR EN VIVO ({len(raw_text)} caracteres):\n"
            f"{raw_text.strip()}"
        )
        self.app.after(0, self.app.update_eye_output, eye_text)

        # 5. Despacho según los 3 Modos Operativos v4.0
        if self.operation_mode == MODO_APRENDIZAJE or self.is_learning_mode:
            self.is_asking = False
            self.app.after(
                0,
                self.app.update_inference_output,
                f"🎓 Modo Solo Aprendizaje activo: Paso #{target_bucket.total_steps_lifetime} registrado para {target_bucket.tecnico_campo} [{cat_oficial}]."
            )
        elif self.operation_mode == MODO_SILENCIOSO_ASISTIR:
            # Modo 3: Encubetado continuo 100% silencioso (0 llamadas a Gemini en segundo plano)
            if not target_bucket.last_diagnosis_card:
                self.app.after(
                    0,
                    self.app.update_inference_output,
                    f"⚡ Modo Silencioso + Asistir: Cubeta [{target_bucket.tecnico_campo}] actualizada.\n"
                    f"• Pasos en memoria: {target_bucket.total_steps_lifetime} | Categoría pre-filtrada: [{cat_oficial}]\n"
                    f"• Herramientas vistas: {herramientas_str}\n\n"
                    f"👉 Pulsa '🧠 Asistir Caso Actual' (o Ctrl+Shift+Espacio) cuando desees generar el árbol de diagnóstico y la respuesta en tono SOMOS."
                )
        else:
            # Modo 2: Guía Continua (Solo dispara cuando hay señal técnica accionable nueva, nunca en saludos simples)
            if not is_dup and self.pre_filter.has_actionable_technical_signal(raw_text, active_title):
                threading.Thread(
                    target=self._execute_guided_assistance,
                    args=(target_bucket, raw_text, False),
                    daemon=True
                ).start()

    # =========================================================================
    # CICLO DE VISIÓN ULTRALIGERO CON DEBOUNCE 3.5s POR CUADRANTES 4x4
    # =========================================================================
    def vision_loop(self):
        """
        Ciclo Continuo Ultraligero v4.0 (Poll de 1.0s + Cuadrícula 4x4 + Debounce 3.5s):
        - Revisa cada 1.0s únicamente el título de ventana y la miniatura 120x80 (~3.5 ms, 0% OCR).
        - Detecta tanto cambios de pestaña en Chrome (Zabbix -> Selene -> LuCI) como cambios
          DENTRO de la misma página/título (nuevos mensajes en Selene o clics dentro de OpenWISP)
          mediante comparación por 16 cuadrantes (4x4).
        - Espera 3.5 segundos de estabilidad visual antes de disparar Tesseract en escala de grises.
        - Incluye Techo Máximo de 8.0s (si hay interacción continua sin pausa) y Flush de 1.5s
          al cambiar de pestaña para que NUNCA se pierda una consulta rápida.
        """
        prev_title = ""
        prev_poll_thumb = None
        last_ocr_thumb = None
        last_ocr_title = ""

        has_pending_change = False
        change_started_ts = 0.0
        last_motion_ts = 0.0
        pending_img = None
        pending_thumb = None
        pending_title = ""

        while True:
            try:
                now = time.time()
                if not self.is_paused:
                    active_title = self.get_active_window_title()
                    permitida, motivo_perm = self.pre_filter.is_window_allowed(active_title)

                    if not permitida:
                        # Si había un frame pendiente (>=1.5s) de una ventana técnica antes de pasar al Agente/otra app, procesarlo
                        if has_pending_change and pending_img is not None and (now - change_started_ts) >= MIN_FLUSH_TAB_SWITCH_SEGUNDOS:
                            self._process_stabilized_frame(pending_title, pending_img, trigger_reason="Flush cambio de ventana")
                            last_ocr_thumb = pending_thumb
                            last_ocr_title = pending_title
                            has_pending_change = False
                            pending_img = None

                        if "ventana propia del agente" in motivo_perm.lower():
                            self.app.after(
                                0,
                                self.app.update_eye_output,
                                "🛡️ REPOSO: Ventana del Agente en primer plano.\n"
                                "Monitoreo visual activo (Debounce 3.5s) esperando interacción en Artemis, Selene, OpenWISP, Zabbix o WinBox."
                            )
                        else:
                            self.app.after(0, self.app.update_eye_output, f"🙈 Ventana excluida:\n[{active_title}]\nMotivo: {motivo_perm}")
                    else:
                        # Si cambió el título de pestaña/ventana y teníamos un cambio pendiente visto >=1.5s en la pestaña anterior, hacer Flush
                        if prev_title and active_title != prev_title and has_pending_change and pending_img is not None:
                            if (now - change_started_ts) >= MIN_FLUSH_TAB_SWITCH_SEGUNDOS:
                                self._process_stabilized_frame(pending_title, pending_img, trigger_reason="Flush cambio de pestaña")
                                last_ocr_thumb = pending_thumb
                                last_ocr_title = pending_title
                                has_pending_change = False
                                pending_img = None

                        # Captura ligera en RAM (~10 ms, SIN Tesseract OCR)
                        current_img = self.vision.capture_frame(active_title)
                        curr_thumb = self.pre_filter.make_thumb(current_img)

                        if current_img is not None and curr_thumb is not None:
                            # ¿Se movió la pantalla en este último segundo (título o cualquiera de los 16 cuadrantes)?
                            moving_now = (
                                (active_title != prev_title) or
                                self.pre_filter.compare_thumbs(prev_poll_thumb, curr_thumb)
                            )
                            # ¿Es diferente respecto a la última vez que corrimos OCR?
                            diff_from_last_ocr = (
                                (last_ocr_thumb is None) or
                                (active_title != last_ocr_title) or
                                self.pre_filter.compare_thumbs(last_ocr_thumb, curr_thumb)
                            )

                            prev_title = active_title
                            prev_poll_thumb = curr_thumb

                            if moving_now and diff_from_last_ocr:
                                last_motion_ts = now
                                if not has_pending_change:
                                    has_pending_change = True
                                    change_started_ts = now
                                pending_img = current_img
                                pending_thumb = curr_thumb
                                pending_title = active_title

                            if has_pending_change and diff_from_last_ocr:
                                stable_elapsed = now - last_motion_ts
                                total_wait_elapsed = now - change_started_ts

                                if stable_elapsed >= DEBOUNCE_ESTABILIDAD_SEGUNDOS or total_wait_elapsed >= MAX_DEBOUNCE_CEILING_SEGUNDOS:
                                    reason = "Estable 3.5s" if stable_elapsed >= DEBOUNCE_ESTABILIDAD_SEGUNDOS else "Control 8.0s"
                                    self._process_stabilized_frame(active_title, current_img, trigger_reason=reason)
                                    last_ocr_thumb = curr_thumb
                                    last_ocr_title = active_title
                                    has_pending_change = False
                                    pending_img = None
                            else:
                                has_pending_change = False
                                pending_img = None

                # =================================================================
                # TEMPORIZADORES DE AUTO-CIERRE E INACTIVIDAD
                # =================================================================
                expired_buckets = self.bucket_manager.check_expired_buckets()
                for exp_b in expired_buckets:
                    log.info(f"Cubeta [{exp_b.bucket_id}] superó 10 min de inactividad. Moviendo a staging...")
                    closed_b = self.bucket_manager.close_and_extract_bucket(exp_b.bucket_id)
                    if closed_b:
                        self.bg_tasks_queue.put({
                            "type": "auto_close_staging",
                            "bucket": closed_b
                        })

                if self.is_learning_mode and self.bucket_manager.check_global_inactivity():
                    log.info("30 minutos de inactividad global detectados en Modo Solo Aprendizaje. Transicionando a evaluación...")
                    self.is_learning_mode = False
                    self.is_paused = True
                    self.app.after(0, self.app.set_state, "paused")
                    self.app.after(0, self.app.switch_to_tab, "📋 Evaluar Aprendizaje")
                    self.app.after(0, self.app.append_chat, "Agente", "⏰ 30 minutos de inactividad global completados. El Modo Solo Aprendizaje se ha cerrado automáticamente. Por favor revisa las tarjetas en '📋 Evaluar Aprendizaje'.")

                    for b_id in list(self.bucket_manager.active_buckets.keys()):
                        rem_b = self.bucket_manager.close_and_extract_bucket(b_id)
                        if rem_b:
                            self.bg_tasks_queue.put({
                                "type": "auto_close_staging",
                                "bucket": rem_b
                            })

            except Exception as e:
                log.error(f"Error recuperable en ciclo de visión v4.0: {e}")
                time.sleep(1.0)

            time.sleep(INTERVALO_POLL_LIGERO_SEGUNDOS)

    def run(self):
        """Inicia el loop principal de la interfaz gráfica CustomTkinter."""
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.app.after(300, self.start_background_threads)
        self.app.mainloop()


if __name__ == "__main__":
    check_and_install_tesseract()
    agent_app = MesaAyudaAgentApp()
    agent_app.run()
