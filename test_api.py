import os
from google import genai
from dotenv import load_dotenv
import time

load_dotenv()

try:
    client = genai.Client()
    
    modelos_a_probar = ['gemini-3.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-3.5-flash-lite']
    
    for modelo in modelos_a_probar:
        print(f"\nProbando cuota de {modelo}...")
        try:
            response = client.models.generate_content(
                model=modelo,
                contents='Di "Test"'
            )
            print(f"EXITO con {modelo}: {response.text}")
            break # Encontramos uno que funciona
        except Exception as e:
            if "RESOURCE_EXHAUSTED" in str(e):
                print(f"Cuota agotada para {modelo}.")
            else:
                print(f"Error con {modelo}: {e}")
        time.sleep(2)
        
except Exception as e:
    print(f"Error general: {e}")
