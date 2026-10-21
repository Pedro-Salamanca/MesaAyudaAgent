import customtkinter as ctk
from typing import Optional, List, Dict
from core.logger import log

class AppWindow(ctk.CTk):
    """
    Ventana Principal de MesaAyudaAgent v3.0 (SOMOS NETWORK).
    Estructurada en 3 Pestañas Integradas:
    1. 'Observación y Cubetas': Visor en vivo de pantallas de soporte, pre-filtrado, hasta 5 cubetas concurrentes y chat.
    2. '📋 Evaluar Aprendizaje': Bandeja de tarjetas interactivas de pre-síntesis para validación humana diferida.
    3. '🏛️ Memoria Oficial': Repositorio relacional SQLite + vectorial de casos aprobados con búsqueda semántica.
    """
    def __init__(
        self,
        chat_callback=None,
        pause_callback=None,
        pause_break_callback=None,
        pause_eval_callback=None,
        toggle_learning_mode_callback=None,
        delete_callback=None,
        refresh_callback=None,
        force_callback=None,
        approve_callback=None,
        close_case_callback=None,
        discard_bucket_callback=None,
        select_bucket_callback=None,
        approve_staging_callback=None,
        correct_staging_callback=None,
        discard_staging_callback=None,
        refresh_staging_callback=None,
        tecnico_nombre="Técnico"
    ):
        super().__init__()
        self.chat_callback = chat_callback
        self.pause_callback = pause_callback
        self.pause_break_callback = pause_break_callback or pause_callback
        self.pause_eval_callback = pause_eval_callback
        self.toggle_learning_mode_callback = toggle_learning_mode_callback
        self.delete_callback = delete_callback
        self.refresh_callback = refresh_callback
        self.force_callback = force_callback
        self.approve_callback = approve_callback
        self.close_case_callback = close_case_callback
        self.discard_bucket_callback = discard_bucket_callback
        self.select_bucket_callback = select_bucket_callback
        self.approve_staging_callback = approve_staging_callback
        self.correct_staging_callback = correct_staging_callback
        self.discard_staging_callback = discard_staging_callback
        self.refresh_staging_callback = refresh_staging_callback
        self.tecnico_nombre = tecnico_nombre
        
        self.title(f"MesaAyudaAgent v3.0 - SOMOS NETWORK | Operador: {self.tecnico_nombre}")
        self.geometry("1060x860")
        self.minsize(960, 740)
        
        # Estados internos
        self.is_paused = False
        self.is_on_break = False
        self.is_learning_mode = False
        self.staging_count = 0

        # Sistema de 3 Pestañas
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=10, pady=10, fill="both", expand=True)
        
        self.tab_obs = self.tabview.add("Observación y Cubetas")
        self.tab_staging = self.tabview.add("📋 Evaluar Aprendizaje")
        self.tab_mem = self.tabview.add("🏛️ Memoria Oficial")
        
        self.pill_buttons = []
        self.staging_card_frames = []
        self.memory_frames = []

        self.setup_observation_tab()
        self.setup_staging_tab()
        self.setup_memory_tab()

    # =========================================================================
    # PESTAÑA 1: OBSERVACIÓN Y CUBETAS CONCURRENTES
    # =========================================================================
    def setup_observation_tab(self):
        self.tab_obs.grid_columnconfigure((0, 1), weight=1)
        self.tab_obs.grid_rowconfigure(0, weight=0) # Barra de píldoras
        self.tab_obs.grid_rowconfigure(1, weight=1) # Zona 1 y 2
        self.tab_obs.grid_rowconfigure(2, weight=1) # Zona 3 y 4

        # ---------------------------------------------------------------------
        # BARRA DE PÍLDORAS: CUBETAS CONCURRENTES (Hasta 5 técnicos simultáneos)
        # ---------------------------------------------------------------------
        self.frame_buckets_bar = ctk.CTkFrame(self.tab_obs, fg_color="#1B2631", corner_radius=8)
        self.frame_buckets_bar.grid(row=0, column=0, columnspan=2, padx=10, pady=(5, 8), sticky="ew")

        self.label_buckets_title = ctk.CTkLabel(
            self.frame_buckets_bar,
            text="Cubetas Activas:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#85929E"
        )
        self.label_buckets_title.pack(side="left", padx=(10, 8), pady=6)

        self.frame_pills_container = ctk.CTkFrame(self.frame_buckets_bar, fg_color="transparent")
        self.frame_pills_container.pack(side="left", fill="x", expand=True, padx=5, pady=4)

        self.label_no_buckets = ctk.CTkLabel(
            self.frame_pills_container,
            text="Esperando chats de Artemis / herramientas de campo...",
            font=ctk.CTkFont(size=11, slant="italic"),
            text_color="#5D6D7E"
        )
        self.label_no_buckets.pack(side="left", padx=5)

        # ---------------------------------------------------------------------
        # ZONA 1: Estado del Agente, Controles de Turno y Detalle de Cubeta
        # ---------------------------------------------------------------------
        self.frame_state = ctk.CTkFrame(self.tab_obs)
        self.frame_state.grid(row=1, column=0, padx=10, pady=5, sticky="nsew")
        
        self.label_state_title = ctk.CTkLabel(self.frame_state, text="Estado del Sistema", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_state_title.pack(pady=(6, 1))
        
        self.label_status = ctk.CTkLabel(self.frame_state, text="● Active", text_color="#2ECC71", font=ctk.CTkFont(size=19, weight="bold"))
        self.label_status.pack(pady=(1, 4))

        # TOGGLE: MODO SOLO APRENDIZAJE
        self.frame_learning_mode = ctk.CTkFrame(self.frame_state, fg_color="#1E272E", corner_radius=6)
        self.frame_learning_mode.pack(padx=10, pady=(2, 4), fill="x")
        
        self.switch_learning_mode = ctk.CTkSwitch(
            self.frame_learning_mode,
            text="🎓 Modo Solo Aprendizaje",
            command=self._toggle_learning_mode,
            font=ctk.CTkFont(size=11, weight="bold"),
            progress_color="#27AE60"
        )
        self.switch_learning_mode.pack(padx=10, pady=5)

        # CONTENEDOR DE LOS 2 BOTONES DISTINTOS DE PAUSA
        self.frame_pause_buttons = ctk.CTkFrame(self.frame_state, fg_color="transparent")
        self.frame_pause_buttons.pack(padx=10, pady=3, fill="x")

        # BOTÓN 1: Pausa de Turno / Comida (Break) - Permanece en la pantalla actual
        self.btn_break_pause = ctk.CTkButton(
            self.frame_pause_buttons,
            text="⏸️ Pausa de Turno (Break)",
            command=self._toggle_break_pause,
            fg_color="#D35400",
            hover_color="#BA4A00",
            height=28,
            font=ctk.CTkFont(size=11, weight="bold")
        )
        self.btn_break_pause.pack(fill="x", pady=2)

        # BOTÓN 2: Pausar y Evaluar Aprendizaje - Pausa y abre la pestaña de tarjetas
        self.btn_eval_pause = ctk.CTkButton(
            self.frame_pause_buttons,
            text="📋 Pausar y Evaluar Aprendizaje",
            command=self._pause_and_evaluate,
            fg_color="#8E44AD",
            hover_color="#71368A",
            height=28,
            font=ctk.CTkFont(size=11, weight="bold")
        )
        self.btn_eval_pause.pack(fill="x", pady=2)

        # Card de Información de la Cubeta en Foco
        self.frame_bucket_info = ctk.CTkFrame(self.frame_state, fg_color="#212F3D", corner_radius=6)
        self.frame_bucket_info.pack(padx=10, pady=(6, 6), fill="x")

        self.label_info_header = ctk.CTkLabel(
            self.frame_bucket_info,
            text="DETALLE DE CUBETA EN FOCO",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#3498DB"
        )
        self.label_info_header.pack(pady=(4, 2))

        self.label_bucket_detail = ctk.CTkLabel(
            self.frame_bucket_info,
            text="Técnico: Esperando...\nSitio / CUN: -\nPasos registrados: 0\nHerramientas: -",
            justify="left",
            font=ctk.CTkFont(size=11)
        )
        self.label_bucket_detail.pack(padx=8, pady=(2, 6))

        # ---------------------------------------------------------------------
        # ZONA 2: The Eye (OCR y Detección de Herramientas)
        # ---------------------------------------------------------------------
        self.frame_eye = ctk.CTkFrame(self.tab_obs)
        self.frame_eye.grid(row=1, column=1, padx=10, pady=5, sticky="nsew")
        
        self.label_eye_title = ctk.CTkLabel(self.frame_eye, text="The Eye (OCR & Recorte Artemis)", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_eye_title.pack(pady=(6, 4))
        
        self.textbox_eye = ctk.CTkTextbox(self.frame_eye, wrap="word")
        self.textbox_eye.pack(padx=10, pady=(0, 8), fill="both", expand=True)
        self.textbox_eye.insert("0.0", "Esperando lectura de pantalla...")
        self.textbox_eye.configure(state="disabled")
        
        # ---------------------------------------------------------------------
        # ZONA 3: Inferencia (Diagnóstico de IA y Estado de Compresión)
        # ---------------------------------------------------------------------
        self.frame_inference = ctk.CTkFrame(self.tab_obs)
        self.frame_inference.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")
        
        self.label_inference_title = ctk.CTkLabel(self.frame_inference, text="Inferencia (Diagnóstico de IA)", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_inference_title.pack(pady=(6, 4))
        
        self.textbox_inference = ctk.CTkTextbox(self.frame_inference, wrap="word")
        self.textbox_inference.pack(padx=10, pady=(0, 8), fill="both", expand=True)
        self.textbox_inference.insert("0.0", "Buscando anomalías y correlacionando cubetas...")
        self.textbox_inference.configure(state="disabled")
        
        # ---------------------------------------------------------------------
        # ZONA 4: Chat de Interacción y Botonera Ergonómica de Cierre
        # ---------------------------------------------------------------------
        self.frame_chat = ctk.CTkFrame(self.tab_obs)
        self.frame_chat.grid(row=2, column=1, padx=10, pady=5, sticky="nsew")
        
        self.label_chat_title = ctk.CTkLabel(self.frame_chat, text="Chat & Acciones del Caso", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_chat_title.pack(pady=(6, 4))
        
        # Contenedor inferior de input y botones
        self.frame_chat_input = ctk.CTkFrame(self.frame_chat, fg_color="transparent")
        self.frame_chat_input.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        
        self.entry_chat_input = ctk.CTkEntry(
            self.frame_chat_input,
            placeholder_text="Escribe la solución oficial, indicación técnica o consulta...",
            height=34,
            font=ctk.CTkFont(size=12)
        )
        self.entry_chat_input.pack(side="top", fill="x", pady=(0, 6))
        self.entry_chat_input.bind("<Return>", lambda event: self._send_message())
        
        # Botonera de control integral
        self.frame_buttons = ctk.CTkFrame(self.frame_chat_input, fg_color="transparent")
        self.frame_buttons.pack(side="bottom", fill="x")
        
        self.button_chat_send = ctk.CTkButton(
            self.frame_buttons, text="Enviar Chat", command=self._send_message,
            width=85, height=30
        )
        self.button_chat_send.pack(side="left", padx=(0, 4))
        
        self.button_approve = ctk.CTkButton(
            self.frame_buttons, text="Aprobar Sugerencia", command=self._approve_message,
            fg_color="#27AE60", hover_color="#1E8449", width=125, height=30
        )
        self.button_approve.pack(side="left", padx=(0, 4))
        
        self.button_close_case = ctk.CTkButton(
            self.frame_buttons, text="🏁 Finalizar Caso", command=self._close_case_message,
            fg_color="#8E44AD", hover_color="#71368A", width=115, height=30
        )
        self.button_close_case.pack(side="left", padx=(0, 4))

        self.button_force_save = ctk.CTkButton(
            self.frame_buttons, text="Forzar Pantalla", command=self._force_message,
            fg_color="#D48806", hover_color="#AD6800", width=100, height=30
        )
        self.button_force_save.pack(side="left", padx=(0, 4))

        self.button_discard = ctk.CTkButton(
            self.frame_buttons, text="✕ Descartar", command=self._discard_message,
            fg_color="#922B21", hover_color="#78241C", width=80, height=30
        )
        self.button_discard.pack(side="right")
        
        self.textbox_chat_output = ctk.CTkTextbox(self.frame_chat, wrap="word")
        self.textbox_chat_output.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
        self.textbox_chat_output.insert("0.0", "Agente SOMOS v3.0: Listo para apoyar la atención de campo.\nCada técnico tendrá su propia cubeta aislada y auto-cierre a 10 min de inactividad.\n\n")
        self.textbox_chat_output.configure(state="disabled")

    # =========================================================================
    # PESTAÑA 2: BANDEJA DE EVALUACIÓN DE APRENDIZAJE (TARJETAS STAGING)
    # =========================================================================
    def setup_staging_tab(self):
        self.tab_staging.grid_columnconfigure(0, weight=1)
        self.tab_staging.grid_rowconfigure(1, weight=1)

        # Barra superior con contador y refresco
        top_bar = ctk.CTkFrame(self.tab_staging, fg_color="#1B2631", corner_radius=8)
        top_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(5, 10))

        self.label_staging_count = ctk.CTkLabel(
            top_bar,
            text="📋 0 Casos Aprendidos Pendientes de Evaluación",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#3498DB"
        )
        self.label_staging_count.pack(side="left", padx=15, pady=8)

        btn_refresh_staging = ctk.CTkButton(
            top_bar,
            text="🔄 Refrescar Tarjetas",
            command=self._refresh_staging,
            width=140,
            height=28,
            fg_color="#2980B9",
            hover_color="#1F618D"
        )
        btn_refresh_staging.pack(side="right", padx=10, pady=8)

        # Contenedor desplazable de tarjetas de evaluación
        self.scroll_staging = ctk.CTkScrollableFrame(self.tab_staging, fg_color="transparent")
        self.scroll_staging.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def update_staging_count(self, count: int):
        """Actualiza el contador de tarjetas en la interfaz."""
        self.staging_count = count
        if hasattr(self, 'label_staging_count'):
            sufijo = "s" if count != 1 else ""
            self.label_staging_count.configure(
                text=f"📋 {count} Caso{sufijo} Aprendido{sufijo} Pendiente{sufijo} de Evaluación"
            )
        try:
            # Actualizar botón de pestaña si el componente segmented button está disponible
            if "📋 Evaluar Aprendizaje" in self.tabview._segmented_button._buttons_dict:
                btn = self.tabview._segmented_button._buttons_dict["📋 Evaluar Aprendizaje"]
                btn.configure(text=f"📋 Evaluar Aprendizaje ({count})")
        except Exception:
            pass

    def populate_staging_cards(self, staging_cases: List[Dict]):
        """Renderiza las tarjetas interactivas de pre-síntesis para revisión humana."""
        for card in self.staging_card_frames:
            card.destroy()
        self.staging_card_frames.clear()

        self.update_staging_count(len(staging_cases))

        if not staging_cases:
            empty_frame = ctk.CTkFrame(self.scroll_staging, fg_color="#1E272E", corner_radius=8)
            empty_frame.pack(fill="x", padx=10, pady=30)
            self.staging_card_frames.append(empty_frame)

            ctk.CTkLabel(
                empty_frame,
                text="🎉 ¡Excelente! No hay casos pendientes de evaluación en este momento.\n\n"
                     "Las atenciones se cierran automáticamente tras 10 min de inactividad\n"
                     "y se presentarán aquí como tarjetas para que las apruebes o corrijas.",
                font=ctk.CTkFont(size=13),
                text_color="#85929E",
                justify="center"
            ).pack(pady=30)
            return

        for case in staging_cases:
            c_id = case["id_cubeta"]
            tec = case.get("tecnico_campo") or "Técnico en Sitio"
            cun = case.get("id_servicio_cun") or "N/A"
            sitio = case.get("codigo_sitio") or "N/A"
            cat = case.get("categoria_deducida") or "Soporte General"
            tools = case.get("herramientas_usadas") or "General"
            fecha = case.get("fecha_cierre") or ""
            obs = case.get("problema_observado") or "No especificado"
            sol_ia = case.get("solucion_deducida_ia") or "No deducida"
            pregunta = case.get("pregunta_duda_ia") or "¿Qué acción técnica específica se realizó en este caso?"

            card_frame = ctk.CTkFrame(self.scroll_staging, fg_color="#1B2631", corner_radius=8, border_width=1, border_color="#2E4053")
            card_frame.pack(fill="x", padx=10, pady=8)
            self.staging_card_frames.append(card_frame)

            # 1. Cabecera de la Tarjeta
            header_frame = ctk.CTkFrame(card_frame, fg_color="#212F3D", corner_radius=6)
            header_frame.pack(fill="x", padx=8, pady=(8, 6))

            ctk.CTkLabel(
                header_frame,
                text=f"👤 Técnico: {tec} | 🏢 Sitio: {sitio} | CUN: {cun}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="#F4D03F"
            ).pack(side="left", padx=10, pady=5)

            ctk.CTkLabel(
                header_frame,
                text=f"🛠️ {tools} | 🏷️ {cat} | 🕒 {fecha}",
                font=ctk.CTkFont(size=11),
                text_color="#85929E"
            ).pack(side="right", padx=10, pady=5)

            # 2. Bloque: Lo que la IA observó
            ctk.CTkLabel(
                card_frame,
                text="👁️ Lo que el Agente Observó:",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="#5DADE2"
            ).pack(anchor="w", padx=12, pady=(4, 1))

            ctk.CTkLabel(
                card_frame,
                text=obs,
                font=ctk.CTkFont(size=11),
                justify="left",
                wraplength=880
            ).pack(anchor="w", padx=14, pady=(0, 6))

            # 3. Bloque: Solución Deducida por IA
            ctk.CTkLabel(
                card_frame,
                text="💡 Solución Técnica Deducida por IA:",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="#58D68D"
            ).pack(anchor="w", padx=12, pady=(2, 1))

            ctk.CTkLabel(
                card_frame,
                text=sol_ia,
                font=ctk.CTkFont(size=11),
                justify="left",
                wraplength=880
            ).pack(anchor="w", padx=14, pady=(0, 6))

            # 4. Bloque Destacado: Pregunta Clave de la IA al Operador
            q_box = ctk.CTkFrame(card_frame, fg_color="#283747", corner_radius=6)
            q_box.pack(fill="x", padx=12, pady=4)

            ctk.CTkLabel(
                q_box,
                text=f"❓ Pregunta Clave de la IA al Operador:\n{pregunta}",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="#F39C12",
                justify="left",
                wraplength=860
            ).pack(anchor="w", padx=10, pady=6)

            # 5. Entrada para respuesta / corrección del operador
            entry_answer = ctk.CTkEntry(
                card_frame,
                placeholder_text="Escribe tu respuesta, aclaración o solución oficial para este caso...",
                height=32,
                font=ctk.CTkFont(size=11)
            )
            entry_answer.pack(fill="x", padx=12, pady=6)

            # 6. Botonera de Acción
            action_bar = ctk.CTkFrame(card_frame, fg_color="transparent")
            action_bar.pack(fill="x", padx=12, pady=(0, 8))

            # Botón 1: Aprobar directamente
            btn_app = ctk.CTkButton(
                action_bar,
                text="✅ Aprobar Aprendizaje",
                command=lambda bid=c_id, sol=sol_ia: self._on_approve_staging(bid, sol),
                fg_color="#27AE60",
                hover_color="#1E8449",
                height=28,
                font=ctk.CTkFont(size=11, weight="bold")
            )
            btn_app.pack(side="left", padx=(0, 6))

            # Botón 2: Responder / Corregir y Guardar
            btn_corr = ctk.CTkButton(
                action_bar,
                text="✏️ Responder / Corregir y Guardar",
                command=lambda bid=c_id, ent=entry_answer, c_dict=case: self._on_correct_staging(bid, ent.get(), c_dict),
                fg_color="#2980B9",
                hover_color="#1F618D",
                height=28,
                font=ctk.CTkFont(size=11, weight="bold")
            )
            btn_corr.pack(side="left", padx=(0, 6))

            # Botón 3: Descartar
            btn_disc = ctk.CTkButton(
                action_bar,
                text="🗑️ Descartar",
                command=lambda bid=c_id: self._on_discard_staging(bid),
                fg_color="#922B21",
                hover_color="#78241C",
                height=28,
                font=ctk.CTkFont(size=11)
            )
            btn_disc.pack(side="right")

    # =========================================================================
    # PESTAÑA 3: MEMORIA OFICIAL (SQL + VECTORIAL)
    # =========================================================================
    def setup_memory_tab(self):
        self.tab_mem.grid_columnconfigure(0, weight=1)
        self.tab_mem.grid_rowconfigure(1, weight=1)
        
        top_frame = ctk.CTkFrame(self.tab_mem, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(5, 10))
        
        ctk.CTkLabel(
            top_frame,
            text="Base de Datos Oficial de Casos Aprendidos (memory_v3.db)",
            font=ctk.CTkFont(size=15, weight="bold")
        ).pack(side="left")

        ctk.CTkButton(
            top_frame,
            text="🔄 Actualizar Lista",
            command=self._refresh_memory,
            width=130,
            height=28
        ).pack(side="right")
        
        self.scroll_memory = ctk.CTkScrollableFrame(self.tab_mem)
        self.scroll_memory.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def populate_memory(self, cases):
        """Carga y renderiza los casos validados almacenados en la base de datos oficial."""
        for frame in self.memory_frames:
            frame.destroy()
        self.memory_frames.clear()
        
        if not cases:
            lbl = ctk.CTkLabel(self.scroll_memory, text="No hay casos registrados en memory_v3.db.", text_color="gray")
            lbl.pack(pady=20)
            self.memory_frames.append(lbl)
            return

        for case in cases:
            c_id, context, action, timestamp = case
            
            f = ctk.CTkFrame(self.scroll_memory)
            f.pack(fill="x", pady=4, padx=5)
            self.memory_frames.append(f)
            
            header = f"CASO: {c_id} | Fecha: {timestamp}"
            ctk.CTkLabel(f, text=header, text_color="#85929E", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=10, pady=(4, 0))
            
            text_disp = f"Falla / Contexto:\n{context}\n\nSolución Oficial Validada:\n{action}"
            lbl = ctk.CTkLabel(f, text=text_disp, justify="left", wraplength=820, font=ctk.CTkFont(size=11))
            lbl.pack(anchor="w", padx=10, pady=4)
            
            btn_del = ctk.CTkButton(
                f, text="Borrar de BD", fg_color="#E74C3C", hover_color="#C0392B",
                width=100, height=22, font=ctk.CTkFont(size=10),
                command=lambda cid=c_id: self._delete_case(cid)
            )
            btn_del.pack(anchor="e", padx=10, pady=(0, 4))

    # =========================================================================
    # GESTIÓN DE CUBETAS CONCURRENTES EN INTERFAZ
    # =========================================================================
    def update_buckets_pills(self, buckets_summary: List[Dict]):
        """
        Renderiza dinámicamente las píldoras de las hasta 5 cubetas activas en la barra superior.
        summary: [{'id': str, 'tecnico': str, 'sitio': str, 'steps_count': int, 'inactive_min': int, 'is_focused': bool}]
        """
        for btn in self.pill_buttons:
            btn.destroy()
        self.pill_buttons.clear()

        if not buckets_summary:
            self.label_no_buckets.pack(side="left", padx=5)
            return

        self.label_no_buckets.pack_forget()

        for b in buckets_summary:
            b_id = b["id"]
            inac = b.get("inactive_min", 0)
            label_text = f"● {b['tecnico']} ({b['sitio']}) | {b['steps_count']}p ({inac}m)"
            
            fg = "#2980B9" if b["is_focused"] else "#34495E"
            hover = "#1F618D" if b["is_focused"] else "#2C3E50"

            btn = ctk.CTkButton(
                self.frame_pills_container,
                text=label_text,
                command=lambda bid=b_id: self._select_bucket(bid),
                fg_color=fg,
                hover_color=hover,
                height=26,
                font=ctk.CTkFont(size=11, weight="bold" if b["is_focused"] else "normal")
            )
            btn.pack(side="left", padx=3)
            self.pill_buttons.append(btn)

    def update_bucket_info(self, info_text: str):
        """Actualiza el cuadro de información de la cubeta en foco en la Zona 1."""
        self.label_bucket_detail.configure(text=info_text)

    # =========================================================================
    # NAVEGACIÓN Y CONTROLADORES DE EVENTOS
    # =========================================================================
    def switch_to_tab(self, tab_name: str):
        """Cambia el foco visual a una pestaña específica."""
        try:
            self.tabview.set(tab_name)
        except Exception as e:
            log.warning(f"Error cambiando a pestaña {tab_name}: {e}")

    def _toggle_learning_mode(self):
        """Activa o desactiva el Modo Solo Aprendizaje."""
        self.is_learning_mode = bool(self.switch_learning_mode.get())
        if self.toggle_learning_mode_callback:
            self.toggle_learning_mode_callback(self.is_learning_mode)

    def _toggle_break_pause(self):
        """Pausa de Turno / Comida (Break): Mantiene la pantalla actual y sintetiza en segundo plano."""
        self.is_on_break = not self.is_on_break
        if self.is_on_break:
            self.is_paused = True
            self.set_state("break")
            if self.pause_break_callback:
                self.pause_break_callback(True)
        else:
            self.is_paused = False
            self.set_state("active")
            if self.pause_break_callback:
                self.pause_break_callback(False)

    def _pause_and_evaluate(self):
        """Pausa la observación y abre directamente la pestaña de evaluación de tarjetas."""
        self.is_paused = True
        self.set_state("paused")
        if self.pause_eval_callback:
            self.pause_eval_callback()
        self.switch_to_tab("📋 Evaluar Aprendizaje")

    def _toggle_pause(self):
        """Callback genérico de pausa para compatibilidad."""
        self._toggle_break_pause()

    def _select_bucket(self, bucket_id: str):
        if self.select_bucket_callback:
            self.select_bucket_callback(bucket_id)

    def set_state(self, state: str):
        state_low = state.lower()
        if state_low == "active":
            self.label_status.configure(text="● Active", text_color="#2ECC71")
            self.btn_break_pause.configure(text="⏸️ Pausa de Turno (Break)", fg_color="#D35400", hover_color="#BA4A00")
            self.is_paused = False
            self.is_on_break = False
        elif state_low == "break":
            self.label_status.configure(text="● Pausa Turno (Break)", text_color="#E67E22")
            self.btn_break_pause.configure(text="▶️ Reanudar Turno", fg_color="#27AE60", hover_color="#1E8449")
            self.is_paused = True
            self.is_on_break = True
        elif state_low == "paused":
            self.label_status.configure(text="● Paused", text_color="#E74C3C")
            self.btn_break_pause.configure(text="▶️ Reanudar Observación", fg_color="#27AE60", hover_color="#1E8449")
            self.is_paused = True
        elif state_low == "asking":
            self.label_status.configure(text="● Asking", text_color="#F39C12")

    def update_eye_output(self, text: str):
        self.textbox_eye.configure(state="normal")
        self.textbox_eye.delete("0.0", "end")
        self.textbox_eye.insert("0.0", text)
        self.textbox_eye.configure(state="disabled")

    def update_inference_output(self, text: str):
        self.textbox_inference.configure(state="normal")
        self.textbox_inference.delete("0.0", "end")
        self.textbox_inference.insert("0.0", text)
        self.textbox_inference.configure(state="disabled")

    def append_chat(self, sender: str, text: str):
        self.textbox_chat_output.configure(state="normal")
        self.textbox_chat_output.insert("end", f"{sender}: {text}\n\n")
        self.textbox_chat_output.see("end")
        self.textbox_chat_output.configure(state="disabled")

    def _send_message(self):
        msg = self.entry_chat_input.get().strip()
        if msg:
            self.append_chat(self.tecnico_nombre, msg)
            self.entry_chat_input.delete(0, "end")
            if self.chat_callback:
                self.chat_callback(msg)

    def _force_message(self):
        msg = self.entry_chat_input.get().strip()
        if msg:
            self.append_chat(f"{self.tecnico_nombre} (Forzar Pantalla)", msg)
            self.entry_chat_input.delete(0, "end")
            if self.force_callback:
                self.force_callback(msg)

    def _approve_message(self):
        if self.approve_callback:
            self.approve_callback()

    def _close_case_message(self):
        msg = self.entry_chat_input.get().strip()
        self.entry_chat_input.delete(0, "end")
        if self.close_case_callback:
            self.close_case_callback(msg)

    def _discard_message(self):
        if self.discard_bucket_callback:
            self.discard_bucket_callback()

    def _delete_case(self, cid):
        if self.delete_callback:
            self.delete_callback(cid)

    def _refresh_memory(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _refresh_staging(self):
        if self.refresh_staging_callback:
            self.refresh_staging_callback()

    def _on_approve_staging(self, id_cubeta: str, solucion: str):
        if self.approve_staging_callback:
            self.approve_staging_callback(id_cubeta, solucion)

    def _on_correct_staging(self, id_cubeta: str, operator_text: str, case_dict: dict):
        if self.correct_staging_callback:
            self.correct_staging_callback(id_cubeta, operator_text, case_dict)

    def _on_discard_staging(self, id_cubeta: str):
        if self.discard_staging_callback:
            self.discard_staging_callback(id_cubeta)

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    app = AppWindow()
    app.mainloop()
