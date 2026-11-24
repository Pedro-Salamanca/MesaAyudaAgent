import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from core.database import HybridVectorDB
from core.llm_client import LLMClient
from alimentar_manual import importar_o_actualizar_manual, listar_manuales

db = HybridVectorDB()
llm = LLMClient(db=db)

manuals_to_ingest = [
    {
        "archivo": os.path.join("data", "checklist_validaciones_sitio.txt"),
        "id": "checklist_validaciones_sitio",
        "titulo": "CHECKLIST PREVIO – VALIDACIONES EN SITIO",
        "categoria": "Acometida/8 Hilos",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_openwisp_phantom_orb.txt"),
        "id": "protocolo_openwisp_phantom_orb",
        "titulo": "GESTIÓN DE ROUTERS EN OPENWISP (PHANTOM Y ORB)",
        "categoria": "Router Secundario",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_vilo_inventory.txt"),
        "id": "protocolo_vilo_inventory",
        "titulo": "GESTIÓN E INVENTARIO EN VILO LIVING (MESH)",
        "categoria": "Router Secundario",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_zabbix_monitoreo.txt"),
        "id": "protocolo_zabbix_monitoreo",
        "titulo": "MONITOREO DE INFRAESTRUCTURA, EDGES Y SWITCHES DE PISO EN ZABBIX",
        "categoria": "Switch/Capa 2",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_switches_montante_poe.txt"),
        "id": "protocolo_switches_montante_poe",
        "titulo": "GESTIÓN Y DIAGNÓSTICO DE SWITCHES DE PISO, CASCADAS EN MONTANTE Y POE",
        "categoria": "Switch/Capa 2",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_grafana_monitoreo.txt"),
        "id": "protocolo_grafana_monitoreo",
        "titulo": "MONITOREO DE CAÍDAS EN GRAFANA (SUBSITE VALIDATE)",
        "categoria": "Soporte General",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_winbox_gestion_chr.txt"),
        "id": "protocolo_winbox_gestion_chr",
        "titulo": "GESTIÓN DE CHR Y LEASES DHCP EN WINBOX",
        "categoria": "Soporte Capa 3",
        "version": "2.0"
    },
    {
        "archivo": os.path.join("data", "manual_aprovisionamiento_radius_artemis.txt"),
        "id": "protocolo_aprovisionamiento_radius_artemis",
        "titulo": "APROVISIONAMIENTO, AUTENTICACIÓN RADIUS, APP ARTEMIS Y PLANES DE VELOCIDAD",
        "categoria": "Velocidad/Plan Pro",
        "version": "2.0"
    }
]

if __name__ == "__main__":
    print("=== INGESTANDO / ACTUALIZANDO LOS 8 MANUALES BASE v2.0 EN SQLITE VECTORIAL ===")
    for m in manuals_to_ingest:
        if os.path.exists(m["archivo"]):
            importar_o_actualizar_manual(
                db=db,
                llm=llm,
                ruta_archivo=m["archivo"],
                id_manual=m["id"],
                titulo=m["titulo"],
                categoria=m["categoria"],
                version=m["version"]
            )
    listar_manuales(db)
