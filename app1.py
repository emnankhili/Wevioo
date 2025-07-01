# app.py
import os
import re
import json
import pdfplumber
import pytesseract
import pandas as pd
from PIL import Image
from docx2pdf import convert
from flask import Flask, request, render_template, send_from_directory, redirect, url_for
from werkzeug.utils import secure_filename
from docx import Document
from openai import OpenAI
from flask import jsonify


UPLOAD_FOLDER = "uploads"
TEMP_FOLDER = "temp"
RESULTS_FOLDER = "results"
RESULT_EXCEL = os.path.join(RESULTS_FOLDER, "resultats.xlsx")
ALLOWED_EXTENSIONS = {"pdf", "docx"}
MODEL = "qwen/qwen3-32b"

client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_docx_to_pdf(docx_path):
    try:
        pythoncom.CoInitialize()  # Initialiser COM
        convert(docx_path, TEMP_FOLDER)
        return os.path.join(TEMP_FOLDER, os.path.basename(docx_path).replace(".docx", ".pdf"))
    except Exception as e:
        print(f"Erreur conversion DOCX: {e}")
        return None
    finally:
        pythoncom.CoUninitialize()
        
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

def estimate_experience(from_year, to_year=2025):
    try:
        return max(0, to_year - int(from_year))
    except:
        return 0

def ask_groq(prompt):
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ Groq API error: {e}")
        return ""

def extract_from_chunk(chunk):
    prompt = f"""
    Tu es un expert en ressources humaines.
    Analyse le texte suivant, un extrait de CV, et renvoie UNIQUEMENT ce JSON strictement valide :

    ====================
    {chunk}
    ====================

    {{
        "nom_complet": "",
        "domaine_expertise": "",
        "date_diplome_principal": "",
        "annees_experience": 0,
        "nationalite": "",
        "diplomes": []
    }}

    - Ne fais aucun commentaire.
    - Tous les champs doivent être renseignés si possible.
    - "date_diplome_principal" doit correspondre au diplôme de niveau ingénieur ou master (ignore les licences ou formations courtes).
    - "annees_experience" doit être calculé à partir de "date_diplome_principal" ou bien a partir de la premiere annee ou il a entammer son cursus professionnel.
    - "diplomes" doit contenir une liste de diplômes mentionnés dans le texte.
    -le nom complet peut etre precede par non de l'expert et il est obligatoire de l'extraire ya pas un cv sans nom de l'expert .
    """
    response = ask_groq(prompt)
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
    final["diplomes"] = list(filter(None, set(final["diplomes"])))
    return final

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("cv")
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            if filename.endswith(".docx"):
                text = extract_text_from_docx(filepath)
                pdf_path = convert_docx_to_pdf(filepath)
                pdf_filename = os.path.basename(pdf_path) if pdf_path else ""
                if pdf_path and not os.path.exists(pdf_path):
                    print(f"❌ PDF converti introuvable : {pdf_path}")
                    pdf_filename = ""

            else:
                text = extract_text_from_pdf(filepath)
                pdf_filename = filename

            if not text:
                return "❌ Texte non extractible."

            data = extract_data_from_text(text)
            data["fichier"] = filename
            data["lien_fichier"] = f'=HYPERLINK("uploads/{filename}", "{filename}")'

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

            return render_template("result.html", info=data, cv_text=text, question="", answer="", pdf_filename=pdf_filename)

    return render_template("index.html")

@app.route("/ask", methods=["POST"])
def ask():
    question = request.form.get("question", "").strip()
    cv_text = request.form.get("cv_text", "")
    data = json.loads(request.form.get("data", "{}"))

    if not question or not cv_text:
        return redirect(url_for("index"))

    def chunk_text(text, max_chars=2000):
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    chunks = chunk_text(cv_text)
    answer = ""

    for chunk in chunks:
        prompt = f"""Réponds brièvement à la question suivante concernant ce CV :

➡️ {question}

Voici un extrait du CV :
=====================
{chunk}
=====================

Réponse brève, sans raisonnement ni analyse, uniquement une réponse directe :"""

        response = ask_groq(prompt)
        if response:
            answer = response.strip()
            if len(answer) > 2:  # si une vraie réponse a été générée
                break  # on arrête dès qu'on a une réponse utile

    return render_template("result.html", info=data, cv_text=cv_text, question=question, answer=answer, pdf_filename=data.get("fichier", ""))

@app.route("/uploads/<filename>")
def serve_cv(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/temp/<filename>")
def serve_temp_pdf(filename):
    return send_from_directory(TEMP_FOLDER, filename)
@app.route("/search", methods=["GET", "POST"])
def search():
    results = []
    domaines = []
    mots_analysés = set()

    # Lire les domaines dès le début
    if os.path.exists(RESULT_EXCEL):
        df = pd.read_excel(RESULT_EXCEL)
        domaines = sorted(df["domaine_expertise"].dropna().unique())

        # Si POST → filtrer les résultats
        if request.method == "POST":
            nom = request.form.get("nom", "").strip().lower()
            domaine = request.form.get("domaine", "").strip().lower()
            mot_cle = request.form.get("mot_cle", "").strip().lower()

            if mot_cle:
                for file in os.listdir(UPLOAD_FOLDER):
                    if not allowed_file(file):
                        continue
                    file_path = os.path.join(UPLOAD_FOLDER, file)

                    try:
                        if file.lower().endswith(".pdf"):
                            text = extract_text_from_pdf(file_path)
                        elif file.lower().endswith(".docx"):
                            text = extract_text_from_docx(file_path)
                        else:
                            continue

                        if mot_cle in text.lower() and file not in mots_analysés:
                            match = df[df["fichier"] == file]
                            if not match.empty:
                                results.append(match.iloc[0].to_dict())
                                mots_analysés.add(file)
                    except Exception as e:
                        print(f"Erreur lecture fichier {file}: {e}")

            else:
                def match(row):
                    return (
                        (not nom or nom in str(row["nom_complet"]).lower()) and
                        (not domaine or domaine in str(row["domaine_expertise"]).lower())
                    )

                results = df[df.apply(match, axis=1)].drop_duplicates(subset="fichier").to_dict(orient="records")

    return render_template("search.html", results=results, domaines=domaines)

@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("query", "").lower()
    if not query or not os.path.exists(RESULT_EXCEL):
        return jsonify([])

    df = pd.read_excel(RESULT_EXCEL)
    noms = df["nom_complet"].dropna().unique()
    suggestions = [n for n in noms if query in str(n).lower()]
    return jsonify(suggestions[:10])  # Limiter à 10 résultats

if __name__ == "__main__":
    app.run(debug=True)
