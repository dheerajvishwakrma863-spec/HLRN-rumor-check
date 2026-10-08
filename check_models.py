import os
from dotenv import load_dotenv
from google import genai

# .env file se load karein (ya yahan seedhe string me apni API key daal sakte hain)
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

# Agar aap chahein toh yahan apni key seedhe bhi likh sakte hain test karne ke liye:
# api_key = "AIzaSy...apni_asli_key_yahan_daal_sakte_hain"

if not api_key:
    print("Error: API Key nahi mili!")
    exit()

# Client initialize karein
client = genai.Client(api_key=api_key)

print("--- Aapke account par available Gemini models ---")
try:
    for model in client.models.list():
        # Model ka poora naam print karte hain
        print(f"Model Name: {model.name}")
except Exception as e:
    print(f"Error aagya: {e}")