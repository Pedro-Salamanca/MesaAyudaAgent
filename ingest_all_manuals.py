import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from core.database import HybridVectorDB
from core.llm_client import LLMClient
from alimentar_manual import importar_o_actualizar_manual, listar_manuales

db = HybridVectorDB()
llm = LLMClient()

manuals_to_ingest = [
    {
        "archivo": os.path.join("data", "manual_openwisp_phantom_orb.txt"),
        "id": "protocolo_openwisp_phantom_orb",
        "titulo": "GESTIÓN DE ROUTERS EN OPENWISP (PHANTOM Y ORB)",
        "categoria": "Capa 3 / CPE",
        "version": "1.0"
    },
    {
        "archivo": os.path.join("data", "manual_vilo_inventory.txt"),
        "id": "protocolo_vilo_inventory",
        "titulo": "GESTIÓN E INVENTARIO EN VILO LIVING (MESH)",
        "categoria": "Capa 3 / CPE",
        "version": "1.0"
    },
    {
        "archivo": os.path.join("data", "manual_grafana_monitoreo.txt"),
        "id": "protocolo_grafana_monitoreo",
        "titulo": "MONITOREO DE CAÍDAS EN GRAFANA (SUBSITE VALIDATE)",
        "categoria": "Monitoreo / SAC",
        "version": "1.0"
    },
    {
        "archivo": os.path.join("data", "manual_zabbix_monitoreo.txt"),
        "id": "protocolo_zabbix_monitoreo",
        "titulo": "MONITOREO DE INFRAESTRUCTURA, EDGES Y SWITCHES DE PISO EN ZABBIX",
        "categoria": "Monitoreo / Infra",
        "version": "1.1"
    },
    {
        "archivo": os.path.join("data", "manual_winbox_gestion_chr.txt"),
        "id": "protocolo_winbox_gestion_chr",
        "titulo": "GESTIÓN DE CHR Y LEASES DHCP EN WINBOX",
        "categoria": "Capa 3 / Enrutamiento",
        "version": "1.0"
    }
]

for m in manuals_to_ingest:
    print(f"\n==========================================")
    print(f"Ingestando: {m['id']} -> {m['titulo']}")
    print(f"==========================================")
    ok = importar_o_actualizar_manual(
        db=db,
        llm=llm,
        ruta_archivo=m["archivo"],
        id_manual=m["id"],
        titulo=m["titulo"],
        categoria=m["categoria"],
        version=m["version"]
    )
    if not ok:
        print(f"❌ Falló ingesta de {m['id']}")

print("\n" + "="*85)
listar_manuales(db)
