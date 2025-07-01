# app.py
import os
import re
import json
import csv
import shutil
import requests
import spacy
import spacy.cli
import importlib
import pdfplumber
import pytesseract
import pandas as pd
from PIL import Image
from docx2pdf import convert
from flask import Flask, request, render_template, send_from_directory
from werkzeug.utils import secure_filename
from docx import Document

UPLOAD_FOLDER = "uploads"
TEMP_FOLDER = "temp"
RESULTS_FOLDER = "results"
RESULT_CSV = os.path.join(RESULTS_FOLDER, "resultats.csv")
RESULT_EXCEL = os.path.join(RESULTS_FOLDER, "resultats.xlsx")
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "llama3"
ALLOWED_EXTENSIONS = {"pdf", "docx"}

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

try:
    nlp = spacy.load("fr_core_news_lg")
except OSError:
    spacy.cli.download("fr_core_news_lg")
    importlib.invalidate_caches()
    nlp = spacy.load("fr_core_news_lg")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_docx_to_pdf(docx_path):
    try:
        convert(docx_path, TEMP_FOLDER)
        return os.path.join(TEMP_FOLDER, os.path.basename(docx_path).replace(".docx", ".pdf"))
    except Exception as e:
        print(f"Erreur conversion DOCX: {e}")
        return None

def extract_text_from_pdf(pdf_path):
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                content = page.extract_text()
                if content and len(content.strip()) > 30:
                    text += content + "\n"
                else:
                    image = page.to_image(resolution=300).original
                    text += pytesseract.image_to_string(image, lang="eng+fra") + "\n"
    except Exception as e:
        print(f"Erreur PDF: {e}")
    return text.strip()

def extract_text_from_docx(docx_path):
    try:
        doc = Document(docx_path)
        return "\n".join([para.text for para in doc.paragraphs])
    except Exception as e:
        print(f"Erreur lecture DOCX brut: {e}")
        return ""

def clean_text_before_chunking(text):
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        if re.search(r'^(t[eé]l|tel|email|adresse|mobile|phone)\s*:?', line.strip(), re.IGNORECASE):
            continue
        if len(line.strip()) <= 2:
            continue
        cleaned.append(line.strip())
    return "\n".join(cleaned)

def chunk_text(text, max_chars=2000):
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

def clean_name(nom):
    if not nom:
        return ""
    nom = re.sub(r"\b(dr|mr|mme|mrs|m)\b[\.]?", "", nom, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", nom.strip())

def extract_domain(text):
    match = re.search(r"(expert|sp[ée]cialiste|consultant|responsable) en ([^\n\.:]+)", text, re.IGNORECASE)
    return match.group(2).strip() if match else ""

def estimate_experience(from_year, to_year=2025):
    try:
        return max(0, to_year - int(from_year))
    except:
        return 0

def ask_ollama(prompt):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }
    try:
        res = requests.post(OLLAMA_URL, json=payload)
        res.raise_for_status()
        return res.json()["message"]["content"]
    except Exception as e:
        print(f"❌ Erreur Ollama : {e}")
        return ""

def extract_full_name(text):
    doc = nlp(text)
    persons = [ent.text.strip() for ent in doc.ents if ent.label_ == "PER"]
    return clean_name(" ".join(persons[:2])) if persons else ""

def find_earliest_diploma_year(text):
    pattern = r"(doctorat|mast[èe]re|licence|ing[ée]nieur|master|formation).{0,100}(\b(19|20)\d{2}\b)|" \
              r"(\b(19|20)\d{2}\b).{0,100}(doctorat|mast[èe]re|licence|ing[ée]nieur|master|formation)"
    matches = re.findall(pattern, text, re.IGNORECASE)
    years = [int(item) for match in matches for item in match if re.match(r"\b(19|20)\d{2}\b", item)]
    return min(years) if years else None

def extract_data_from_text(text):
    cleaned = clean_text_before_chunking(text)
    chunks = chunk_text(cleaned)
    final = {
        "nom_complet": "",
        "domaine_expertise": "",
        "date_diplome_principal": "",
        "annees_experience": 0,
        "nationalite": "",
        "diplomes": []
    }
    for chunk in chunks:
        result = extract_from_chunk(chunk)
        for key in final:
            if key == "diplomes":
                final[key].extend(result.get(key, []))
            elif not final[key] and result.get(key):
                final[key] = result[key]

    if not final["nom_complet"]:
        final["nom_complet"] = extract_full_name(text)
    if not final["domaine_expertise"]:
        final["domaine_expertise"] = extract_domain(text)

    for ent in nlp(text).ents:
        if ent.label_ == "LOC" and ent.text.lower() not in final["nom_complet"].lower():
            final["nationalite"] = ent.text
            break

    if not final["nationalite"]:
        match = re.search(r"Nationalit[ée]\s*[:\-]?\s*(\w+)", text, re.IGNORECASE)
        if match:
            final["nationalite"] = match.group(1).strip()

    year = find_earliest_diploma_year(text)
    if year:
        final["date_diplome_principal"] = str(year)
        final["annees_experience"] = estimate_experience(year)

    final["nom_complet"] = clean_name(final["nom_complet"])
    final["diplomes"] = list(filter(None, set(final["diplomes"])))
    return final

def extract_from_chunk(chunk):
    prompt = f"""
    Voici un extrait de CV :

    ====================
    {chunk}
    ====================

    Retourne uniquement ce JSON strict :
    {{
        "nom_complet": "",
        "domaine_expertise": "",
        "date_diplome_principal": "",
        "annees_experience": 0,
        "nationalite": "",
        "diplomes": []
    }}
    """
    response = ask_ollama(prompt)
    try:
        json_match = re.search(r'\{[\s\S]*?\}', response)
        if not json_match:
            print("⚠️ Aucun JSON trouvé.")
            return {}
        data = json.loads(json_match.group())
        if "date_diplome_principal" in data:
            year_match = re.search(r"\b(19|20)\d{2}\b", data["date_diplome_principal"])
            if year_match:
                data["date_diplome_principal"] = year_match.group()
                data["annees_experience"] = estimate_experience(data["date_diplome_principal"])
        return data
    except Exception as e:
        print(f"⚠️ Erreur JSON : {e}")
        return {}

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("cv")
        question = request.form.get("question", "").strip()
        answer = ""
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            # 🟩 Ajouter ceci pour convertir les .docx en PDF
            if filename.endswith(".docx"):
                pdf_path = convert_docx_to_pdf(filepath)
                if pdf_path:
                    # copie le PDF converti dans le dossier uploads pour affichage ultérieur
                    shutil.copy(pdf_path, os.path.join(UPLOAD_FOLDER, os.path.basename(pdf_path)))

            # 📄 Extraction du texte
            text = extract_text_from_docx(filepath) if filename.endswith(".docx") else extract_text_from_pdf(filepath)
            if not text:
                return "❌ Texte non extractible."

            # 🔍 Analyse du contenu
            data = extract_data_from_text(text)
            data["fichier"] = filename
            data["lien_fichier"] = f'=HYPERLINK("uploads/{filename}", "{filename}")'

            if question:
                prompt = f"Voici le contenu d’un CV :\n\n{text}\n\nRéponds à cette question :\n➡️ {question}\n\nRéponse concise :"
                answer = ask_ollama(prompt)

            df_new = pd.DataFrame([{**data, "diplomes": ", ".join(data["diplomes"]), "lien_cv": data["lien_fichier"]}])
            if os.path.exists(RESULT_EXCEL):
                df_old = pd.read_excel(RESULT_EXCEL)
                df_final = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_final = df_new

            try:
                df_final.to_excel(RESULT_EXCEL, index=False, engine='openpyxl')
            except PermissionError:
                return "❌ Fermez Excel et réessayez."

            return render_template("result.html", info=data, cv_text=text, question=question, answer=answer)

    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    question = request.form.get("question", "")
    cv_text = request.form.get("cv_text", "")
    data_raw = request.form.get("data", "{}")
    try:
        data = json.loads(data_raw)
    except json.JSONDecodeError as e:
        return f"Erreur JSON : {e}<br><br><pre>{data_raw}</pre>"
    prompt = f"Voici le contenu d’un CV :\n\n{cv_text}\n\nRéponds à cette question :\n➡️ {question}\n\nRéponse claire :"
    answer = ask_ollama(prompt)
    return render_template("result.html", info=data, cv_text=cv_text, question=question, answer=answer)

@app.route("/uploads/<filename>")
def serve_cv(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)
@app.route("/search", methods=["GET", "POST"])
def search():
    results = []
    domaines = []

    if os.path.exists(RESULT_EXCEL):
        df = pd.read_excel(RESULT_EXCEL)
        domaines = sorted(df["domaine_expertise"].dropna().unique())

    if request.method == "POST":
        nom = request.form.get("nom", "").strip().lower()
        domaine = request.form.get("domaine", "").strip()

        if os.path.exists(RESULT_EXCEL):
            df = pd.read_excel(RESULT_EXCEL)

            if nom:
                df = df[df["nom_complet"].str.lower().str.contains(nom, na=False)]
            if domaine:
                df = df[df["domaine_expertise"] == domaine]

            results = df.to_dict(orient="records")

    return render_template("search.html", domaines=domaines, results=results)

if __name__ == "__main__":
    app.run(debug=True)
