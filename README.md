# MesaAyudaAgent v3.0 - SOMOS NETWORK

Asistente de inteligencia artificial y aprendizaje continuo (RAG Híbrido) para la optimización de la atención técnica de Mesa de Ayuda (Nivel 1 y 2) en **SOMOS NETWORK**.

## Características Principales

- **Atención Concurrente de Campo:** Manejo de hasta 5 cubetas simultáneas (`CaseBucket`) para atender a múltiples técnicos en terreno sin cruce de capturas ni consolas.
- **Pre-Filtrado Perceptual Anti-Spam (0 Tokens):** Detección de cambios de píxeles (<2%) y duplicidad léxica (>=92%) que evita el 85-90% de llamadas innecesarias a la IA.
- **Escudo de Privacidad y Logins:** Purga automática e instantánea de memoria RAM ante formularios de autenticación o contraseñas.
- **Base de Datos Híbrida (SQL + Vectorial):** Almacenamiento en SQLite con modo WAL (`memory_v3.db`) y vectores densos de 3072 dimensiones (`gemini-embedding-001`) con similitud de coseno.
- **Pool de Claves API con Failover:** Rotación automática ante límites de cuota (429/503) y tiempo de enfriamiento por clave.
- **Reglas Estrictas de Dominio SOMOS:** Prohibición absoluta de términos ONT, exclusión de "tickets" (uso de chats/casos), y protección total de datos de contacto (PII).

## Estructura del Código

```text
├── core/
│   ├── pre_filter.py       # Motor perceptual anti-spam y anclaje
│   ├── bucket_manager.py   # Gestión de 5 cubetas concurrentes
│   ├── database.py         # Base de datos SQL + Vectorial y Gatekeeper
│   ├── llm_client.py       # Cliente Gemini, APIKeyPool y embeddings
│   ├── brain.py            # Fachada de persistencia dual
│   ├── search_engine.py    # Motor RAG híbrido (Coseno + BM25)
│   ├── vision.py           # Captura física MSS y OCR Tesseract
│   ├── config.py           # Parámetros y listas de privacidad
│   └── logger.py           # Sistema de logging
├── ui/
│   └── app_window.py       # Interfaz gráfica CustomTkinter con barra de píldoras
├── main.py                 # Orquestador principal
├── compilar.bat            # Script de compilación a ejecutable (.exe)
├── requirements.txt        # Dependencias de Python
├── .env.example            # Plantilla de variables de entorno
└── .gitignore              # Blindaje contra subida de secretos y datos
```

## Requisitos e Instalación

1. Clonar el repositorio.
2. Crear un entorno virtual de Python:
   ```bash
   python -m venv venv
   .\venv\Scripts\activate
   ```
3. Instalar dependencias:
   ```bash
   pip install -r requirements.txt
   ```
4. Copiar `.env.example` a `.env` y configurar su `GEMINI_API_KEY`:
   ```bash
   copy .env.example .env
   ```
5. Ejecutar la aplicación:
   ```bash
   python main.py
   ```
6. O compilar el ejecutable independiente:
   ```bash
   .\compilar.bat
   ```
