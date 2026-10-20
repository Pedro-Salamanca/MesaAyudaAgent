import json
import time
from core.database import HybridVectorDB
from core.llm_client import LLMClient
from core.logger import log

def main():
    print("=== INICIANDO VECTORIZACIÓN COMPLETA DE BASE DE DATOS ===")
    db = HybridVectorDB()
    llm = LLMClient()

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id_caso, problema_detectado, solucion_oficial, categoria_falla, embedding_json FROM casos")
        rows = cursor.fetchall()
        print(f"Total de casos en la base de datos: {len(rows)}")

        updated = 0
        already_vectorized = 0

        for r in rows:
            cid = r["id_caso"]
            emb = r["embedding_json"]
            if emb:
                already_vectorized += 1
                continue

            prob = r["problema_detectado"] or ""
            sol = r["solucion_oficial"] or ""
            cat = r["categoria_falla"] or ""
            text_to_embed = f"Problema: {prob} | Solucion: {sol} | Categoria: {cat}"

            vec = llm.generar_embedding(text_to_embed)
            if vec:
                cursor.execute("UPDATE casos SET embedding_json = ? WHERE id_caso = ?", (json.dumps(vec), cid))
                updated += 1
                print(f"[{updated + already_vectorized}/{len(rows)}] Vectorizado: {cid} ({len(vec)} dim)")
                if updated % 5 == 0:
                    conn.commit()
                time.sleep(0.4) # Respetar rate limits
            else:
                print(f"Advertencia: no se pudo vectorizar {cid}")

        conn.commit()
        print(f"\nResumen: {updated} nuevos casos vectorizados, {already_vectorized} ya contaban con vector. Total: {len(rows)}")

    # Sincronizar copia de respaldo a Google Drive
    db.sync_backup_to_drive()
    print("Base de datos y respaldo en Google Drive sincronizados exitosamente.")

if __name__ == "__main__":
    main()
