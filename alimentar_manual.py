"""
MESAAYUDAAGENT v3.2 - GESTIÓN Y ALIMENTACIÓN DE MANUALES BASE (SQL VECTORIAL)
Permite listar, agregar, actualizar o eliminar manuales y procedimientos operativos
directamente dentro de la base de datos memory_v3.db (tabla: manuales_base).
Genera automáticamente el vector semántico (embedding de 3072 dimensiones) con Gemini.
"""

import os
import sys
import argparse
from datetime import datetime

# Asegurar path raíz del proyecto
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from core.database import HybridVectorDB
from core.llm_client import LLMClient
from core.logger import log

def listar_manuales(db: HybridVectorDB):
    """Muestra una tabla con los manuales actualmente activos en SQLite."""
    manuales = db.get_all_manuales()
    print("=" * 85)
    print(f" MANUALES OPERATIVOS ACTIVOS EN LA MEMORIA VECTORIAL ({len(manuales)} registrados)")
    print("=" * 85)
    if not manuales:
        print(" (No hay manuales registrados actualmente en manuales_base)")
    else:
        print(f"{'ID':25s} | {'CATEGORÍA':12s} | {'VER':5s} | {'FECHA':19s} | {'TÍTULO'}")
        print("-" * 85)
        for m in manuales:
            mid = m['id_manual'][:25]
            cat = m.get('categoria', 'General')[:12]
            ver = m.get('version', '1.0')[:5]
            fec = m.get('fecha_actualizacion', '-')[:19]
            tit = m.get('titulo', '-')
            print(f"{mid:25s} | {cat:12s} | {ver:5s} | {fec:19s} | {tit}")
    print("=" * 85)

def importar_o_actualizar_manual(
    db: HybridVectorDB,
    llm: LLMClient,
    ruta_archivo: str,
    id_manual: str = None,
    titulo: str = None,
    categoria: str = "General",
    version: str = "1.0"
) -> bool:
    """Lee un archivo de texto, genera su vector con Gemini y lo inserta en SQLite."""
    if not os.path.exists(ruta_archivo):
        print(f"❌ Error: El archivo especificado no existe: {ruta_archivo}")
        return False

    try:
        with open(ruta_archivo, "r", encoding="utf-8") as f:
            contenido = f.read().strip()
    except Exception as e:
        print(f"❌ Error leyendo archivo: {e}")
        return False

    if not contenido:
        print("❌ Error: El archivo está vacío.")
        return False

    # Inferir ID si no se proporcionó
    if not id_manual:
        base_name = os.path.splitext(os.path.basename(ruta_archivo))[0]
        id_manual = base_name.lower().replace(" ", "_").replace("-", "_")

    # Inferir Título de la primera línea si no se proporcionó
    if not titulo:
        primer_renglon = contenido.split("\n")[0].replace("#", "").strip()
        titulo = primer_renglon if len(primer_renglon) > 3 else id_manual.replace("_", " ").title()

    print(f"\nGenerando embedding semántico con Gemini para [{id_manual}]...")
    vector = llm.generar_embedding(f"{titulo}\n{categoria}\n{contenido[:2500]}")
    if vector:
        print(f"✓ Vector generado exitosamente ({len(vector)} dimensiones).")
    else:
        print("⚠️ Advertencia: No se pudo generar el vector semántico (se guardará sin embedding).")

    exito = db.insert_or_update_manual(
        id_manual=id_manual,
        titulo=titulo,
        categoria=categoria,
        contenido=contenido,
        version=version,
        vector=vector
    )

    if exito:
        print(f"✅ ¡Manual '{titulo}' [{id_manual}] guardado con éxito en memory_v3.db!\n")
        return True
    else:
        print(f"❌ Falló la inserción en la base de datos.")
        return False

def migrar_manuales_legados(db: HybridVectorDB, llm: LLMClient):
    """Migra los archivos .txt de la carpeta legacy Manuales_Base hacia la base de datos."""
    ruta_legada = r"G:\Mi unidad\MesaAyuda_Memoria\Manuales_Base"
    if not os.path.exists(ruta_legada):
        print(f"No se encontró carpeta legacy en: {ruta_legada}")
        return

    archivos = [f for f in os.listdir(ruta_legada) if f.endswith(".txt")]
    print(f"\nDetectados {len(archivos)} archivo(s) legado(s) en Manuales_Base. Migrando a SQL...")
    for arch in archivos:
        path = os.path.join(ruta_legada, arch)
        mid = os.path.splitext(arch)[0].lower().replace(" ", "_")
        importar_o_actualizar_manual(
            db=db,
            llm=llm,
            ruta_archivo=path,
            id_manual=mid,
            categoria="Capa 1 / Protocolo Sitio",
            version="1.0"
        )

def main():
    parser = argparse.ArgumentParser(description="Alimentador de Manuales para MesaAyudaAgent (SQL Vectorial)")
    parser.add_argument("--archivo", type=str, help="Ruta del archivo .txt o .md a importar")
    parser.add_argument("--id", type=str, help="ID único del manual (ej: checklist_sitio)")
    parser.add_argument("--titulo", type=str, help="Título descriptivo del procedimiento")
    parser.add_argument("--categoria", type=str, default="General", help="Categoría (ej: Capa 1, Capa 2, Capa 3, Protocolos)")
    parser.add_argument("--version", type=str, default="1.0", help="Versión del manual (ej: 1.1)")
    parser.add_argument("--listar", action="store_true", help="Listar todos los manuales activos")
    parser.add_argument("--eliminar", type=str, help="ID del manual a eliminar")
    parser.add_argument("--migrar-legados", action="store_true", help="Migrar manuales desde la carpeta Manuales_Base")

    args = parser.parse_args()

    db = HybridVectorDB()
    llm = LLMClient()

    if args.listar:
        listar_manuales(db)
    elif args.eliminar:
        if db.delete_manual(args.eliminar):
            print(f"✅ Manual [{args.eliminar}] eliminado de la base de datos.")
            listar_manuales(db)
        else:
            print(f"❌ No se pudo eliminar el manual [{args.eliminar}].")
    elif args.archivo:
        importar_o_actualizar_manual(
            db=db,
            llm=llm,
            ruta_archivo=args.archivo,
            id_manual=args.id,
            titulo=args.titulo,
            categoria=args.categoria,
            version=args.version
        )
        listar_manuales(db)
    elif args.migrar_legados:
        migrar_manuales_legados(db, llm)
        listar_manuales(db)
    else:
        # Por defecto: migrar si no hay manuales y listar
        manuales_actuales = db.get_all_manuales()
        if not manuales_actuales:
            migrar_manuales_legados(db, llm)
        listar_manuales(db)

if __name__ == "__main__":
    main()
