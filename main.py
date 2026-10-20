import sys
import os
import threading
import time
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
    1. Pre-Filtrado Anti-Spam (0 tokens en pantallas idénticas o logins).
    2. Cubetas Concurrentes (hasta 5 técnicos simultáneos en Artemis, Putty, WinBox).
    3. Pool de Claves de API con failover automático.
    4. Base de Datos Híbrida SQL + Vectorial (memory_v3.db).
    5. Cierre explícito e independiente de casos.
    """
    def __init__(self):
        self.tecnico_nombre = TECNICO_NOMBRE_DEFAULT
        self.last_problem_signature = ""

        # Inicializar Componentes de Inteligencia y Datos
        self.pre_filter = PreFilterEngine()
        self.bucket_manager = BucketManager(max_concurrent=5, ttl_minutes=15)
        self.brain = AgentBrain()
        self.llm = LLMClient()
        self.search_engine = MemorySearchEngine(db=self.brain.db)
        self.vision = ScreenReader()

        # Inicializar Interfaz Gráfica con Callbacks de Control
        self.app = AppWindow(
            chat_callback=self.on_chat_message,
            pause_callback=self.on_toggle_pause,
            delete_callback=self.on_delete_case,
            refresh_callback=self.refresh_memory_ui,
            force_callback=self.on_force_save,
            approve_callback=self.on_approve_suggestion,
            close_case_callback=self.on_close_case,
            discard_bucket_callback=self.on_discard_bucket,
            select_bucket_callback=self.on_select_bucket,
            tecnico_nombre=self.tecnico_nombre
        )

        # Estados Internos
        self.is_paused = False
        self.is_asking = False
        self.current_visual_context = ""
        self.current_agent_suggestion = ""
        self.last_valid_ocr = "Esperando lectura inicial..."
        self.last_valid_image = None
        self.last_valid_title = ""

        # Actualizar estado inicial de la UI
        self._refresh_pills_ui()
        self.refresh_memory_ui()

        # Iniciar ciclo de visión en segundo plano
        self.vision_thread = threading.Thread(target=self.vision_loop, daemon=True)
        self.vision_thread.start()
        log.info("Agente MesaAyudaAgent v3.0 inicializado y en ejecución.")

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
                f"Pasos registrados: {len(focused.steps)}\n"
                f"Herramientas: {', '.join(tools)}\n"
                f"Sugerencias aprobadas: {len(focused.approved_suggestions)}"
            )
        else:
            info_txt = "Técnico: Ninguno activo\nSitio / CUN: -\nPasos registrados: 0\nHerramientas: -"

        self.app.after(0, self.app.update_bucket_info, info_txt)

    def refresh_memory_ui(self):
        """Recarga la lista de casos guardados desde la base de datos SQL."""
        cases = self.brain.get_all_cases()
        self.app.populate_memory(cases)

    def on_delete_case(self, case_id: str):
        """Elimina un caso específico de la memoria tras solicitud del usuario."""
        self.brain.delete_case(case_id)
        self.refresh_memory_ui()

    def on_toggle_pause(self):
        """Pausa o reanuda la observación visual y las consultas a la IA."""
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.is_asking = False
            self.app.set_state("paused")
            self.app.update_inference_output("Agente pausado por el operador. No se captura pantalla ni se consume API.")
            log.info("Agente puesto en pausa.")
        else:
            self.app.set_state("active")
            self.app.update_inference_output("Agente reanudado. Observando y asociando cubetas...")
            log.info("Agente reanudado a estado activo.")

    def on_select_bucket(self, bucket_id: str):
        """Cambia el foco a otra de las cubetas activas al hacer clic en su píldora."""
        self.bucket_manager.set_focused_bucket(bucket_id)
        self._refresh_pills_ui()
        focused = self.bucket_manager.get_focused_bucket()
        if focused:
            self.app.append_chat("Agente", f"🔄 Foco cambiado al caso de: {focused.tecnico_campo} (Sitio: {focused.codigo_sitio or 'N/A'}).")

    def on_approve_suggestion(self):
        """
        El técnico aprueba la sugerencia técnica del agente con 1 solo clic.
        REGLA CRÍTICA DE DISEÑO:
        Aprobar una sugerencia NO cierra la cubeta. La acumula en la lista de pasos
        y sugerencias aprobadas del caso, permitiendo continuar con diagnósticos
        adicionales hasta el cierre formal con 'Finalizar Caso'.
        """
        focused = self.bucket_manager.get_focused_bucket()
        if self.current_agent_suggestion:
            sug = self.current_agent_suggestion
            if focused:
                focused.approved_suggestions.append(sug)
                self.bucket_manager.save_checkpoint_to_disk()

            self.app.append_chat(self.tecnico_nombre, f"✅ Sugerencia técnica aprobada: '{sug}'")
            self.app.append_chat("Agente", "👍 Sugerencia guardada en el historial del caso. La cubeta sigue abierta para más pasos o validaciones. Cuando la atención concluya, presiona '🏁 Finalizar Caso'.")
            
            # Liberar estado asking para seguir monitoreando herramientas posteriores
            self.is_asking = False
            self.app.set_state("active")
            self.app.update_inference_output("Sugerencia incorporada al caso. Continuando observación...")
            self._refresh_pills_ui()
            log.info(f"Sugerencia aprobada para la cubeta actual: {sug}")
        else:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna sugerencia técnica activa para aprobar en este momento.")

    def on_close_case(self, user_solution: str = ""):
        """
        Cierre explícito de la cubeta activa y persistencia final en la base de datos SQL + Vectorial.
        Se ejecuta en un hilo separado para mantener la UI fluida mientras Gemini sintetiza el episodio.
        """
        focused = self.bucket_manager.get_focused_bucket()
        if not focused:
            self.app.append_chat("Agente", "ℹ️ No hay ninguna cubeta técnica activa para finalizar.")
            return

        threading.Thread(target=self._process_case_finalization, args=(focused, user_solution), daemon=True).start()

    def _process_case_finalization(self, bucket: CaseBucket, user_solution: str):
        """Sintetiza la trazabilidad de la cubeta con Gemini, valida con Gatekeeper y persiste en BD."""
        b_id = bucket.bucket_id
        tec_name = bucket.tecnico_campo
        pasos_total = len(bucket.steps)

        self.app.after(0, self.app.append_chat, "Agente", f"🏁 Finalizando atención de {tec_name} ({pasos_total} pasos)... Sintetizando caso con IA...")
        self.app.after(0, self.app.update_inference_output, f"Sintetizando episodio para {tec_name}...")

        timeline_text = bucket.compile_timeline_text()

        # Determinar solución oficial candidata
        solucion_candidata = user_solution.strip()
        if not solucion_candidata and bucket.approved_suggestions:
            solucion_candidata = " | ".join(bucket.approved_suggestions)

        # Síntesis estructurada con Gemini (AnalisisEpisodio)
        episodio = self.llm.sintetizar_episodio(timeline_text)

        if episodio:
            categoria_falla = episodio.categoria_falla
            ciudad_nodo = episodio.ciudad_nodo
            herramienta = episodio.herramienta_origen
            resumen_problema = episodio.descripcion_problema
            ruta_pasos = episodio.ruta_diagnostico_pasos
            if not solucion_candidata:
                solucion_candidata = episodio.accion_sugerida
        else:
            categoria_falla = "Soporte General"
            ciudad_nodo = "Desconocido"
            herramienta = "General"
            resumen_problema = f"Atención técnica con {tec_name}"
            ruta_pasos = f"Secuencia de {pasos_total} pasos registrados en herramientas SOMOS."
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
        self.app.after(0, self.app.update_inference_output, "Generando vector semántico (text-embedding-004)...")
        texto_para_vector = f"{resumen_problema} | {solucion_candidata} | {categoria_falla} | {herramienta}"
        vector = self.llm.generar_embedding(texto_para_vector)

        # Persistir en SQLite + Vectorial y JSON/PNG
        extra_meta = {
            "tecnico_campo": tec_name,
            "id_servicio_cun": bucket.id_servicio_cun,
            "herramienta_origen": herramienta,
            "categoria_falla": categoria_falla,
            "ciudad_nodo": ciudad_nodo,
            "estado_validacion": "APROBADO_HUMANO" if user_solution else "APROBADO_IA",
            "ruta_diagnostico_json": json.dumps([s.to_dict() for s in bucket.steps], ensure_ascii=False)
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

            self.app.after(0, self.app.append_chat, "Agente", f"✅ Caso finalizado y almacenado con éxito en memory_v3.db!\n- Técnico: {tec_name}\n- Categoría: {categoria_falla}\n- Solución Oficial: {solucion_candidata}\n- Embedding: {'Generado (768d)' if vector else 'Pendiente'}")
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
        """Ejecuta consulta RAG combinando vector embedding + BM25 + Gemini."""
        self.app.after(0, self.app.append_chat, "Agente", "Buscando casos similares en base de datos híbrida...")

        # Generar embedding de la pregunta si es posible
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

    def vision_loop(self):
        """
        Ciclo Continuo de Visión y Pre-Filtrado:
        1. Comprueba si la ventana está permitida.
        2. Captura imagen y evalúa cambio de píxeles (>2%). Si no cambió, duerme sin consumir OCR ni IA.
        3. Realiza OCR y verifica blindaje contra Login.
        4. Evalúa duplicidad de texto (>92%). Si es idéntico, duerme sin consumir IA.
        5. Extrae anclas y enruta a la cubeta correspondiente.
        6. Si detecta anomalía técnica real, emite recomendación y actualiza UI.
        """
        while True:
            if not self.is_paused:
                active_title = self.get_active_window_title()

                # PASO 1: Validación básica de ventana permitida
                permitida, motivo_perm = self.pre_filter.is_window_allowed(active_title)
                if not permitida:
                    if "mesaayudaagent" in active_title.lower():
                        self.app.after(0, self.app.update_inference_output, "Viendo panel del agente. Manteniendo estado de cubetas...")
                    else:
                        self.app.after(0, self.app.update_eye_output, f"🙈 Ventana excluida:\n[{active_title}]\nMotivo: {motivo_perm}")
                        self.app.after(0, self.app.update_inference_output, f"Reposo: {motivo_perm}")
                else:
                    # PASO 2: Captura de pantalla
                    raw_text = self.vision.capture_and_read()
                    current_img = self.vision.last_image

                    # PASO 2.1: Pre-filtro de cambio de píxeles
                    has_pixel_change = self.pre_filter.has_significant_pixel_change(current_img, threshold=0.02)
                    if not has_pixel_change and not raw_text.startswith("ERROR_OCR:"):
                        # Pantalla estática: ahorro del 100% de llamadas a IA
                        pass
                    else:
                        # PASO 3: Blindaje de Seguridad contra Pantallas de Login
                        es_login, motivo_login = self.pre_filter.is_login_or_forbidden(active_title, raw_text)
                        if es_login:
                            self.last_valid_image = None # Purga inmediata de RAM
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

                            # PASO 5: Extracción de Anclas y Enrutamiento a Cubeta
                            anchors = self.pre_filter.extract_anchors(raw_text, active_title)
                            target_bucket, _ = self.bucket_manager.route_capture(active_title, raw_text, anchors)
                            self._refresh_pills_ui()

                            if is_dup:
                                self.app.after(0, self.app.update_eye_output, f"[Texto sin cambios significativos | Cubeta: {target_bucket.tecnico_campo}]\n\n{raw_text[:300]}...")
                            else:
                                self.app.after(0, self.app.update_eye_output, raw_text)

                                # PASO 6: Inferencia con IA solo si no estamos en 'asking' para esa misma novedad
                                if not self.is_asking:
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

            # Revisión periódica de cubetas inactivas
            expired = self.bucket_manager.check_expired_buckets()
            if expired:
                for exp_b in expired:
                    log.info(f"Cubeta {exp_b.bucket_id} inactiva por más de 15 minutos.")

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
