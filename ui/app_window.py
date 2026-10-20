import customtkinter as ctk
from typing import Optional, List, Dict

class AppWindow(ctk.CTk):
    """
    Ventana Principal de MesaAyudaAgent v3.0 con soporte para hasta 5 Cubetas Concurrentes.
    Permite visualizar y gestionar múltiples atenciones a técnicos de campo simultáneamente.
    """
    def __init__(
        self,
        chat_callback=None,
        pause_callback=None,
        delete_callback=None,
        refresh_callback=None,
        force_callback=None,
        approve_callback=None,
        close_case_callback=None,
        discard_bucket_callback=None,
        select_bucket_callback=None,
        tecnico_nombre="Técnico"
    ):
        super().__init__()
        self.chat_callback = chat_callback
        self.pause_callback = pause_callback
        self.delete_callback = delete_callback
        self.refresh_callback = refresh_callback
        self.force_callback = force_callback
        self.approve_callback = approve_callback
        self.close_case_callback = close_case_callback
        self.discard_bucket_callback = discard_bucket_callback
        self.select_bucket_callback = select_bucket_callback
        self.tecnico_nombre = tecnico_nombre
        
        self.title(f"MesaAyudaAgent v3.0 - SOMOS NETWORK | Operador: {self.tecnico_nombre}")
        self.geometry("1000x820")
        self.minsize(920, 720)
        
        # Sistema de Pestañas
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=10, pady=10, fill="both", expand=True)
        
        self.tab_obs = self.tabview.add("Observación y Cubetas")
        self.tab_mem = self.tabview.add("Memoria y Base de Datos (SQL+Vector)")
        
        self.pill_buttons = []
        self.setup_observation_tab()
        self.setup_memory_tab()

    def setup_observation_tab(self):
        # Configurar rejilla de la pestaña de observación
        self.tab_obs.grid_columnconfigure((0, 1), weight=1)
        self.tab_obs.grid_rowconfigure(0, weight=0) # Barra de píldoras de cubetas concurrentes
        self.tab_obs.grid_rowconfigure(1, weight=1) # Zona 1 y 2
        self.tab_obs.grid_rowconfigure(2, weight=1) # Zona 3 y 4

        # =====================================================================
        # BARRA DE PÍLDORAS: CUBETAS CONCURRENTES (Hasta 5 técnicos en simultáneo)
        # =====================================================================
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

        # =====================================================================
        # ZONA 1: Estado del Agente y Datos de la Cubeta Enfocada
        # =====================================================================
        self.frame_state = ctk.CTkFrame(self.tab_obs)
        self.frame_state.grid(row=1, column=0, padx=10, pady=5, sticky="nsew")
        
        self.label_state_title = ctk.CTkLabel(self.frame_state, text="Estado del Sistema", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_state_title.pack(pady=(8, 2))
        
        self.label_status = ctk.CTkLabel(self.frame_state, text="● Active", text_color="#2ECC71", font=ctk.CTkFont(size=20, weight="bold"))
        self.label_status.pack(pady=(2, 6))
        
        self.btn_pause = ctk.CTkButton(self.frame_state, text="Pausar Observación", command=self._toggle_pause, fg_color="#E74C3C", hover_color="#C0392B", height=28)
        self.btn_pause.pack(pady=4)
        
        self.is_paused = False

        # Card de Información de la Cubeta en Foco
        self.frame_bucket_info = ctk.CTkFrame(self.frame_state, fg_color="#212F3D", corner_radius=6)
        self.frame_bucket_info.pack(padx=10, pady=(10, 8), fill="x")

        self.label_info_header = ctk.CTkLabel(self.frame_bucket_info, text="DETALLE DE CUBETA EN FOCO", font=ctk.CTkFont(size=11, weight="bold"), text_color="#3498DB")
        self.label_info_header.pack(pady=(6, 2))

        self.label_bucket_detail = ctk.CTkLabel(
            self.frame_bucket_info,
            text="Técnico: Esperando...\nSitio / CUN: -\nPasos registrados: 0\nHerramientas: -",
            justify="left",
            font=ctk.CTkFont(size=11)
        )
        self.label_bucket_detail.pack(padx=8, pady=(2, 8))

        # =====================================================================
        # ZONA 2: The Eye (OCR y Detección de Herramientas)
        # =====================================================================
        self.frame_eye = ctk.CTkFrame(self.tab_obs)
        self.frame_eye.grid(row=1, column=1, padx=10, pady=5, sticky="nsew")
        
        self.label_eye_title = ctk.CTkLabel(self.frame_eye, text="The Eye (OCR & Pre-Filtrado)", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_eye_title.pack(pady=(8, 4))
        
        self.textbox_eye = ctk.CTkTextbox(self.frame_eye, wrap="word")
        self.textbox_eye.pack(padx=10, pady=(0, 8), fill="both", expand=True)
        self.textbox_eye.insert("0.0", "Esperando lectura de pantalla...")
        self.textbox_eye.configure(state="disabled")
        
        # =====================================================================
        # ZONA 3: Inferencia (Razonamiento del Agente y Diagnóstico)
        # =====================================================================
        self.frame_inference = ctk.CTkFrame(self.tab_obs)
        self.frame_inference.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")
        
        self.label_inference_title = ctk.CTkLabel(self.frame_inference, text="Inferencia (Diagnóstico de IA)", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_inference_title.pack(pady=(8, 4))
        
        self.textbox_inference = ctk.CTkTextbox(self.frame_inference, wrap="word")
        self.textbox_inference.pack(padx=10, pady=(0, 8), fill="both", expand=True)
        self.textbox_inference.insert("0.0", "Buscando anomalías y correlacionando cubetas...")
        self.textbox_inference.configure(state="disabled")
        
        # =====================================================================
        # ZONA 4: Chat de Interacción y Botonera Ergonómica de Cierre
        # =====================================================================
        self.frame_chat = ctk.CTkFrame(self.tab_obs)
        self.frame_chat.grid(row=2, column=1, padx=10, pady=5, sticky="nsew")
        
        self.label_chat_title = ctk.CTkLabel(self.frame_chat, text="Chat & Acciones del Caso", font=ctk.CTkFont(size=15, weight="bold"))
        self.label_chat_title.pack(pady=(8, 4))
        
        # Contenedor inferior de input y botones
        self.frame_chat_input = ctk.CTkFrame(self.frame_chat, fg_color="transparent")
        self.frame_chat_input.pack(side="bottom", fill="x", padx=8, pady=(0, 10))
        
        # Entrada de texto del operador
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
        
        # BOTÓN 1: Aprobar Sugerencia (SOLO valida recomendación intermedia, NO cierra la cubeta)
        self.button_approve = ctk.CTkButton(
            self.frame_buttons, text="Aprobar Sugerencia", command=self._approve_message,
            fg_color="#27AE60", hover_color="#1E8449", width=125, height=30
        )
        self.button_approve.pack(side="left", padx=(0, 4))
        
        # BOTÓN 2: Finalizar Caso (BOTÓN EXPLÍCITO DE CIERRE Y PERSISTENCIA)
        self.button_close_case = ctk.CTkButton(
            self.frame_buttons, text="🏁 Finalizar Caso", command=self._close_case_message,
            fg_color="#8E44AD", hover_color="#71368A", width=115, height=30
        )
        self.button_close_case.pack(side="left", padx=(0, 4))

        # BOTÓN 3: Forzar Pantalla
        self.button_force_save = ctk.CTkButton(
            self.frame_buttons, text="Forzar Pantalla", command=self._force_message,
            fg_color="#D48806", hover_color="#AD6800", width=100, height=30
        )
        self.button_force_save.pack(side="left", padx=(0, 4))

        # BOTÓN 4: Descartar Cubeta
        self.button_discard = ctk.CTkButton(
            self.frame_buttons, text="✕ Descartar", command=self._discard_message,
            fg_color="#922B21", hover_color="#78241C", width=80, height=30
        )
        self.button_discard.pack(side="right")
        
        # Historial de Chat
        self.textbox_chat_output = ctk.CTkTextbox(self.frame_chat, wrap="word")
        self.textbox_chat_output.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
        self.textbox_chat_output.insert("0.0", "Agente SOMOS v3.0: Listo para apoyar la atención de campo.\nCada técnico tendrá su propia cubeta aislada.\n\n")
        self.textbox_chat_output.configure(state="disabled")

    def setup_memory_tab(self):
        self.tab_mem.grid_columnconfigure(0, weight=1)
        self.tab_mem.grid_rowconfigure(1, weight=1)
        
        top_frame = ctk.CTkFrame(self.tab_mem, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(5, 10))
        
        ctk.CTkLabel(
            top_frame,
            text="Base de Datos SQL + Vectorial de Casos Aprendidos (memory_v3.db)",
            font=ctk.CTkFont(size=15, weight="bold")
        ).pack(side="left")

        ctk.CTkButton(
            top_frame,
            text="Actualizar Lista",
            command=self._refresh_memory,
            width=120,
            height=28
        ).pack(side="right")
        
        self.scroll_memory = ctk.CTkScrollableFrame(self.tab_mem)
        self.scroll_memory.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        
        self.memory_frames = []

    def update_buckets_pills(self, buckets_summary: List[Dict]):
        """
        Renderiza dinámicamente las píldoras de las hasta 5 cubetas activas en la barra superior.
        summary: [{'id': str, 'tecnico': str, 'sitio': str, 'steps_count': int, 'is_focused': bool}]
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
            label_text = f"● {b['tecnico']} ({b['sitio']}) | {b['steps_count']}p"
            
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

    def populate_memory(self, cases):
        """Carga y renderiza los casos almacenados en la base de datos."""
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
            lbl = ctk.CTkLabel(f, text=text_disp, justify="left", wraplength=760, font=ctk.CTkFont(size=11))
            lbl.pack(anchor="w", padx=10, pady=4)
            
            btn_del = ctk.CTkButton(
                f, text="Borrar de BD", fg_color="#E74C3C", hover_color="#C0392B",
                width=100, height=22, font=ctk.CTkFont(size=10),
                command=lambda cid=c_id: self._delete_case(cid)
            )
            btn_del.pack(anchor="e", padx=10, pady=(0, 4))

    def _delete_case(self, cid):
        if self.delete_callback:
            self.delete_callback(cid)

    def _refresh_memory(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _toggle_pause(self):
        if self.pause_callback:
            self.pause_callback()

    def _select_bucket(self, bucket_id: str):
        if self.select_bucket_callback:
            self.select_bucket_callback(bucket_id)

    def set_state(self, state: str):
        if state.lower() == "active":
            self.label_status.configure(text="● Active", text_color="#2ECC71")
            self.btn_pause.configure(text="Pausar Observación", fg_color="#E74C3C", hover_color="#C0392B")
            self.is_paused = False
        elif state.lower() == "paused":
            self.label_status.configure(text="● Paused", text_color="#E74C3C")
            self.btn_pause.configure(text="Reanudar Observación", fg_color="#27AE60", hover_color="#1E8449")
            self.is_paused = True
        elif state.lower() == "asking":
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

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    app = AppWindow()
    app.mainloop()
