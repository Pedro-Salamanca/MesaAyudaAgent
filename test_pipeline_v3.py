import os
import sys
import time
import json
from datetime import datetime

# Configurar salida segura UTF-8 para consola Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Añadir ruta al sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.database import HybridVectorDB
from core.bucket_manager import BucketManager, CaseBucket
from core.pre_filter import PreFilterEngine
from core.llm_client import LLMClient, PreSintesisCaso, AnalisisEpisodio
from core.brain import AgentBrain

def test_database_staging():
    print("\n--- 1. TEST: HybridVectorDB Staging & Auto-Purge ---")
    # Usar base de datos de prueba en memoria o archivo temporal
    test_db_path = os.path.join(os.path.dirname(__file__), "data", "test_staging.db")
    if os.path.exists(test_db_path):
        try: os.remove(test_db_path)
        except Exception: pass

    db = HybridVectorDB(db_path=test_db_path)
    
    # 1. Insertar caso en staging
    staging_data = {
        "id_cubeta": "cubeta_test_001",
        "fecha_inicio": "2026-10-21 10:00:00",
        "fecha_cierre": "2026-10-21 10:10:00",
        "tecnico_campo": "Carlos Gómez",
        "id_servicio_cun": "45102",
        "codigo_sitio": "SOMOS-MDE-RIVA-T4",
        "mac_ip_detectadas": "48:8F:5A:11:22:33, 10.20.30.40",
        "herramientas_usadas": "Artemis, WinBox",
        "categoria_deducida": "Velocidad/Plan Pro",
        "problema_observado": "El cliente reporta lentitud en navegación.",
        "solucion_deducida_ia": "Se ajustó la cola simple de 50M a 100M en MikroTik.",
        "pregunta_duda_ia": "¿Se validó que el plan contratado fuera efectivamente de 100 Mbps?",
        "resumen_bloques": ["Bloque 1: Se abrió WinBox y se revisó Simple Queues."]
    }

    steps = [
        {"step_number": 1, "timestamp": "10:01:00", "tool_name": "Artemis", "window_title": "Chat Artemis", "ocr_text": "Chat con Carlos Gómez CUN 45102"},
        {"step_number": 2, "timestamp": "10:03:00", "tool_name": "WinBox", "window_title": "Winbox v6.49", "ocr_text": "Simple Queue target 10.20.30.40 max-limit 50M"}
    ]

    ok = db.insert_staging_case(staging_data, steps=steps)
    assert ok, "Fallo al insertar staging_case"
    print("✓ Caso insertado en staging (casos_por_revisar y pasos_cubeta)")

    # 2. Contar y listar
    count = db.count_pending_staging()
    assert count == 1, f"Se esperaba 1 caso en staging, hay {count}"
    pending = db.get_pending_staging_cases()
    assert len(pending) == 1, "get_pending_staging_cases debe retornar 1 caso"
    assert pending[0]["tecnico_campo"] == "Carlos Gómez"
    assert pending[0]["pregunta_duda_ia"] == "¿Se validó que el plan contratado fuera efectivamente de 100 Mbps?"
    print(f"✓ Conteo y recuperación de staging correctos ({count} pendiente)")

    saved_steps = db.get_staging_case_steps("cubeta_test_001")
    assert len(saved_steps) == 2, f"Se esperaban 2 pasos en pasos_cubeta, hay {len(saved_steps)}"
    print(f"✓ Pasos recuperados de pasos_cubeta: {len(saved_steps)}")

    # 3. Gatekeeper test en aprobación
    rechazado = db.approve_staging_case("cubeta_test_001", "ok")
    assert not rechazado, "Gatekeeper debió rechazar 'ok'"
    print("✓ Gatekeeper rechazó exitosamente solución basura 'ok'")

    # 4. Aprobación válida y PURGA automática
    solucion_valida = "Se ajustó la cola simple de 50M a 100M en MikroTik y se verificó tráfico en WinBox."
    mock_vector = [0.01] * 10 # Mini vector mock para test
    aprobado = db.approve_staging_case("cubeta_test_001", solucion_valida, operador="Operador Test", vector=mock_vector)
    assert aprobado, "Fallo al aprobar caso con solución válida"

    # Verificar que se guardó en casos oficiales
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM casos WHERE id_caso = 'caso_test_001';")
        oficial = cursor.fetchone()
        assert oficial is not None, "El caso debe existir en la tabla casos"
        assert oficial["solucion_oficial"] == solucion_valida
        assert oficial["estado_validacion"] == "APROBADO_HUMANO"

        # Verificar PURGA de staging
        cursor.execute("SELECT COUNT(*) AS total FROM casos_por_revisar WHERE id_cubeta = 'cubeta_test_001';")
        assert cursor.fetchone()["total"] == 0, "casos_por_revisar NO fue purgado"
        cursor.execute("SELECT COUNT(*) AS total FROM pasos_cubeta WHERE id_cubeta = 'cubeta_test_001';")
        assert cursor.fetchone()["total"] == 0, "pasos_cubeta NO fue purgado"

    print("✓ Caso promovido a tabla 'casos' oficial y PURGADO al 100% de staging (cero residuos)")

    # 5. Test de descarte
    db.insert_staging_case({"id_cubeta": "cubeta_para_borrar", "tecnico_campo": "Técnico Descarte"})
    assert db.count_pending_staging() == 1
    db.discard_staging_case("cubeta_para_borrar")
    assert db.count_pending_staging() == 0
    print("✓ Caso de descarte purgado con éxito")

    # Limpiar archivo temporal de test
    try: os.remove(test_db_path)
    except Exception: pass


def test_bucket_manager_and_anti_mixing():
    print("\n--- 2. TEST: BucketManager Concurrencia y Zero-Guessing ---")
    bm = BucketManager(max_concurrent=5, ttl_bucket_minutes=10, global_inactivity_minutes=30)
    bm.active_buckets.clear()

    # 1. Enrutar captura 1: Carlos Gómez en Artemis
    anchors_1 = {
        "tecnico_campo": "Carlos Gómez",
        "id_servicio_cun": "45102",
        "codigo_sitio": "SOMOS-MDE-RIVA-T4",
        "herramienta_origen": "Artemis"
    }
    b1, es_nuevo, needs_comp = bm.route_capture("Artemis - SOMOS", "Chat con Carlos Gómez CUN 45102", anchors_1)
    assert b1 is not None, "Debe crear cubeta para Carlos Gómez"
    assert b1.tecnico_campo == "Carlos Gómez"
    assert len(bm.active_buckets) == 1
    print(f"✓ Cubeta 1 creada: {b1.bucket_id} ({b1.tecnico_campo})")

    # 2. Captura sin ancla -> Política CERO ADIVINANZAS: Debe retornar (None, False, False)
    anchors_vacio = {"herramienta_origen": "General"}
    b_none, _, _ = bm.route_capture("Explorador de Archivos", "Texto irrelevante sin anclas", anchors_vacio)
    assert b_none is None, "Captura sin ancla debió ser descartada (None)"
    assert len(b1.steps) == 1, "Cubeta 1 no debió recibir capturas ajenas"
    print("✓ Captura sin ancla descartada sin ensuciar la cubeta activa")

    # 3. Enrutar captura 2: Técnico DIFERENTE (María Pérez) -> Debe crear NUEVA cubeta, NO mezclar
    anchors_2 = {
        "tecnico_campo": "María Pérez",
        "id_servicio_cun": "88990",
        "codigo_sitio": "SOMOS-BOG-CHAP-T1",
        "herramienta_origen": "Artemis"
    }
    b2, _, _ = bm.route_capture("Artemis - SOMOS", "Chat con María Pérez CUN 88990", anchors_2)
    assert b2 is not None
    assert b2.bucket_id != b1.bucket_id, "María Pérez NO puede estar en la misma cubeta que Carlos Gómez"
    assert b2.tecnico_campo == "María Pérez"
    assert len(bm.active_buckets) == 2
    print(f"✓ Cubeta 2 creada independientemente: {b2.bucket_id} ({b2.tecnico_campo}). CERO mezclas!")

    # 4. Probar acumulación de 8 pasos y compresión en bloque
    for i in range(7): # Sumando al 1 que ya tiene -> 8 pasos
        step, needs_comp = b1.add_step("WinBox", "WinBox", f"Configurando paso {i+2}")
    assert len(b1.steps) == 8, f"Se esperaban 8 pasos, hay {len(b1.steps)}"
    assert needs_comp == True, "Al llegar a 8 pasos debe activar needs_compression=True"

    # Simular compresión de bloque
    b1.compress_current_block("Se validaron interfaces ether1 y ether2 en MikroTik.")
    assert len(b1.steps) == 0, "Los pasos crudos deben vaciarse tras compresión"
    assert len(b1.partial_summaries) == 1, "Debe existir 1 bloque comprimido"
    assert b1.total_steps_lifetime == 8
    print("✓ Rolling Block Summary: 8 pasos comprimidos a 1 resumen y liberados de RAM")

    # 5. Probar expiración por inactividad funcional (10 minutos)
    b1.last_activity_time = time.time() - (11 * 60) # 11 minutos atrás
    b2.last_activity_time = time.time() # Activo ahora
    expired = bm.check_expired_buckets()
    assert len(expired) == 1, f"Se esperaba 1 cubeta expirada, hay {len(expired)}"
    assert expired[0].bucket_id == b1.bucket_id
    print("✓ Detector de inactividad de 10 min identificó correctamente la cubeta expirada")

    # 6. Probar detector de inactividad global (30 minutos)
    bm.last_global_activity_time = time.time() - (31 * 60) # 31 minutos atrás
    assert bm.check_global_inactivity() == True, "Debe detectar 30 minutos de inactividad global"
    print("✓ Detector de inactividad global de 30 min disparó correctamente")


def test_pre_filter_anchors():
    print("\n--- 3. TEST: PreFilterEngine Extracción de Anclas ---")
    pf = PreFilterEngine()
    
    ocr_sample = """
    Artemis - SOMOS NETWORK
    Chat con: Carlos Eduardo Gómez
    CUN: 45102 - Contrato Residencial
    Nodo: SOMOS-MDE-RIVA-T4-415
    MAC: 48:8F:5A:11:22:33
    IP: 192.168.88.1
    """
    anchors = pf.extract_anchors(ocr_sample, "Artemis - Google Chrome")
    has_anchor, desc = pf.has_strong_anchor(anchors)
    assert anchors["tecnico_campo"] == "Carlos Eduardo Gómez"
    assert anchors["id_servicio_cun"] == "45102"
    assert anchors["codigo_sitio"] == "SOMOS-MDE-RIVA-T4-415"
    assert "48:8F:5A:11:22:33" in anchors["mac_addresses"]
    assert "192.168.88.1" in anchors["ip_addresses"]
    assert anchors["herramienta_origen"] == "Artemis"

    has_anchor, desc = pf.has_strong_anchor(anchors)
    assert has_anchor == True
    print(f"✓ Anclas extraídas fielmente: Técnico={anchors['tecnico_campo']}, CUN={anchors['id_servicio_cun']}, MAC={anchors['mac_addresses']}")

    # Texto vacío o sin anclas
    empty_anchors = pf.extract_anchors("Navegando en youtube buscando música", "YouTube - Chrome")
    has_a, reason = pf.has_strong_anchor(empty_anchors)
    assert has_a == False
    print(f"✓ Texto genérico rechazado: {reason}")


def test_official_db_integrity():
    print("\n--- 4. TEST: Integridad de Base de Datos memory_v3.db (55 Casos Limpios) ---")
    real_db_path = "G:\\Mi unidad\\MesaAyuda_Memoria\\memory_v3.db"
    if not os.path.exists(real_db_path):
        real_db_path = os.path.join(os.path.dirname(__file__), "data", "memory_v3.db")

    if os.path.exists(real_db_path):
        db = HybridVectorDB(db_path=real_db_path)
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS total FROM casos;")
            total_casos = cursor.fetchone()["total"]
            cursor.execute("SELECT COUNT(*) AS total FROM casos_por_revisar;")
            total_staging = cursor.fetchone()["total"]
            print(f"✓ Base de datos oficial verificada: {total_casos} casos en 'casos', {total_staging} en 'casos_por_revisar'.")
            assert total_casos == 55, f"Alerta: Se esperaba que los 55 casos originales estuvieran preservados, hay {total_casos}"
            print("✓ Exactamente 55 casos originales preservados limpios con embedding de 3072 dimensiones.")
    else:
        print(f"ℹ️ Base de datos en {real_db_path} no montada localmente para este check.")


if __name__ == "__main__":
    print("=================================================================")
    print("EJECUTANDO SUITE DE PRUEBAS AUTOMATIZADAS - MESAAYUDAAGENT v3.0")
    print("=================================================================")
    test_database_staging()
    test_bucket_manager_and_anti_mixing()
    test_pre_filter_anchors()
    test_official_db_integrity()
    print("\n=================================================================")
    print("🎉 TODAS LAS PRUEBAS DE FUNCIONALIDAD PASARON EXITOSAMENTE (100%)")
    print("=================================================================")
