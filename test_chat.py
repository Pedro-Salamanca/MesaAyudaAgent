import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
try:
    client = genai.Client()
    resp = client.models.generate_content(model="gemini-3.5-flash-lite", contents="Hola")
    print(resp.text)
except Exception as e:
    print("Error:", e)
