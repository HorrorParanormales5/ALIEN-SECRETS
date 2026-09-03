# ============================================================
# app.py — Puente local entre UFOCS (navegador) y Ollama
# ============================================================
# Qué hace este servidor:
# 1) Sirve tu archivo HTML de UFOCS en http://localhost:5000/
# 2) Recibe las peticiones de chat del navegador en /api/chat
#    y las reenvía a Ollama (http://localhost:11434/api/chat).
# 3) Servidor de alertas, almacenamiento de usuario e historial.
# 4) Endpoint /api/cyber-news para alimentar la pestaña de noticias RSS.
#
# CÓMO USARLO:
#   1. Coloca este archivo (app.py) en la MISMA carpeta que tu
#      archivo HTML de UFOCS.
#   2. Si tu HTML no se llama "UFOCS APP.html", cambia el
#      nombre en la constante HTML_FILENAME más abajo.
#   3. Instala las dependencias (una sola vez):
#        pip install flask requests feedparser
#   4. Asegúrate de que Ollama esté corriendo ("ollama serve").
#   5. Arranca este servidor:
#        python app.py
#   6. Abre en el navegador: http://localhost:5000/

from flask import Flask, request, jsonify, send_from_directory, Response
import requests
import os
import json
import re
import time
import uuid
import html
import feedparser
from werkzeug.exceptions import HTTPException

app = Flask(__name__, static_folder=None)

HTML_FILENAME = "UFOCS_APP.html"   # <-- cambia esto si tu archivo tiene otro nombre

# Lee la URL de Render (Environment) o usa localhost como respaldo local
OLLAMA_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_BASE}/api/chat"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Reintentos SILENCIOSOS: solo aplican si la conexión con Ollama falla
# ANTES de que le hayamos mandado ni un solo pedazo de texto al navegador.
OLLAMA_CHAT_MAX_RETRIES = 3
OLLAMA_CHAT_RETRY_DELAY = 1.5  # segundos entre reintentos

# ============================================================
# Feeds RSS de Ciberseguridad
# ============================================================
SECURITY_FEEDS = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Dark Reading": "https://www.darkreading.com/rss.xml"
}

# ============================================================
# Almacenamiento local (archivos JSON en disco, sin base de datos externa)
# ============================================================
DATA_DIR = os.path.join(BASE_DIR, "data")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
ALERTS_TXT_DIR = os.path.join(DATA_DIR, "alerts_txt")  # respaldo legible tipo Notepad
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(ALERTS_TXT_DIR, exist_ok=True)

MAX_FIELD_LEN = 4000
MAX_HISTORY_MESSAGES = 60  # cuántos mensajes recientes se guardan como "memoria"


def fetch_cyber_news(limit_per_source=4):
    """Extrae las noticias más recientes desde las fuentes RSS configuradas."""
    all_news = []
    for source_name, url in SECURITY_FEEDS.items():
        try:
            resp = requests.get(
                url,
                timeout=8,
                headers={"User-Agent": "UFOCS-CyberNews/1.0"}
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            app.logger.error(f"Error al obtener noticias de {source_name}: {e}")
            continue

        for entry in feed.entries[:limit_per_source]:
            summary_raw = entry.get("summary", "") or ""
            summary_text = re.sub(r"<[^>]+>", " ", summary_raw)
            summary_text = html.unescape(summary_text)
            summary_text = re.sub(r"\s+", " ", summary_text).strip()[:400]

            image_url = None
            if entry.get("media_thumbnail"):
                image_url = entry["media_thumbnail"][0].get("url")
            elif entry.get("media_content"):
                image_url = entry["media_content"][0].get("url")
            elif entry.get("links"):
                for link in entry["links"]:
                    if str(link.get("type", "")).startswith("image/"):
                        image_url = link.get("href")
                        break
            if not image_url:
                match = re.search(r'<img[^>]+src="([^"]+)"', summary_raw)
                if match:
                    image_url = match.group(1)

            all_news.append({
                "source": source_name,
                "title": html.unescape(entry.get("title", "Sin título")),
                "link": entry.get("link", "#"),
                "published": entry.get("published", "Reciente"),
                "summary": summary_raw,
                "summary_text": summary_text or "Sin descripción disponible.",
                "image": image_url,
            })

    return all_news


CYBER_NEWS_CACHE_SECONDS = 600
_cyber_news_cache = {"articles": [], "fetched_at": 0}


def get_cyber_news_cached():
    now = time.time()
    if now - _cyber_news_cache["fetched_at"] > CYBER_NEWS_CACHE_SECONDS or not _cyber_news_cache["articles"]:
        fresh = fetch_cyber_news()
        if fresh:
            _cyber_news_cache["articles"] = fresh
            _cyber_news_cache["fetched_at"] = now
    return _cyber_news_cache["articles"]


def _atomic_write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _safe_filename(text):
    text = re.sub(r"[^a-zA-Z0-9_\-]+", "_", text or "alerta").strip("_")
    return (text or "alerta")[:60]


def _write_alert_txt(alert):
    filename = _safe_filename(alert.get("name")) + "_" + alert.get("id", "")[:8] + ".txt"
    path = os.path.join(ALERTS_TXT_DIR, filename)
    lines = [
        "UFOCS - Alerta",
        "=" * 50,
        "Nombre: " + (alert.get("name") or ""),
        "Ámbito: " + ("General (compartida)" if alert.get("scope") == "general" else "Personal (" + str(alert.get("owner")) + ")"),
        "Creada por: " + str(alert.get("created_by") or ""),
        "",
        "--- Descripción ---",
        alert.get("descripcion") or "(vacío)",
        "",
        "--- Analysis ---",
        alert.get("analysis") or "(vacío)",
        "",
        "--- Artifacts ---",
        alert.get("artifacts") or "(vacío)",
        "",
        "--- Solution ---",
        alert.get("solution") or "(vacío)",
        "",
        "=" * 50,
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines))


def _delete_alert_txt(alert):
    filename = _safe_filename(alert.get("name")) + "_" + alert.get("id", "")[:8] + ".txt"
    path = os.path.join(ALERTS_TXT_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_alerts(alerts):
    _atomic_write_json(ALERTS_FILE, alerts)


def _safe_username_key(username):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", (username or "anon").lower()) or "anon"


@app.route("/")
def serve_ufocs():
    return send_from_directory(BASE_DIR, HTML_FILENAME)


@app.route("/favicon.ico")
def favicon():
    return "", 204


# ============================================================
# NOTICIAS RSS - Endpoint para el Frontend
# ============================================================
@app.route("/api/cyber-news", methods=["GET"])
def get_cyber_news():
    news_data = get_cyber_news_cached()
    return jsonify({
        "status": "success",
        "total": len(news_data),
        "articles": news_data
    })


@app.route("/api/chat", methods=["POST"])
def proxy_chat():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Petición sin cuerpo JSON válido."}), 400

    resp = None
    last_error = None
    for attempt in range(1, OLLAMA_CHAT_MAX_RETRIES + 1):
        try:
            resp = requests.post(OLLAMA_URL, json=body, stream=True, timeout=(10, None))
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            resp = None
            if attempt < OLLAMA_CHAT_MAX_RETRIES:
                time.sleep(OLLAMA_CHAT_RETRY_DELAY)
                continue

    if resp is None:
        return jsonify({
            "error": "No se pudo conectar con Ollama tras "
                     f"{OLLAMA_CHAT_MAX_RETRIES} intentos. Verifica que Ollama "
                     f"esté corriendo. Detalle: {last_error}"
        }), 502

    if resp.status_code != 200:
        content_type = resp.headers.get("Content-Type", "application/json")
        return Response(resp.content, status=resp.status_code, mimetype=content_type)

    def generate():
        try:
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                yield line + "\n"
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout):
            yield json.dumps({
                "error": "La conexión con Ollama se interrumpió a mitad de la respuesta. Vuelve a preguntar."
            }) + "\n"
        except Exception as e:
            app.logger.exception("Error inesperado durante el streaming de /api/chat")
            yield json.dumps({
                "error": f"Error inesperado del servidor al leer la respuesta de Ollama: {e}"
            }) + "\n"
        finally:
            resp.close()

    return Response(generate(), mimetype="application/x-ndjson")


# ============================================================
# ALERTS — guardar/leer alertas por usuario o generales (compartidas)
# ============================================================
@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    username = (request.args.get("username") or "").strip()
    alerts = _load_alerts()
    general = [a for a in alerts if a.get("scope") == "general"]
    mine = [a for a in alerts if a.get("scope") == "user" and a.get("owner") == username]
    general.sort(key=lambda a: a.get("updated_at", 0), reverse=True)
    mine.sort(key=lambda a: a.get("updated_at", 0), reverse=True)
    return jsonify({"general": general, "mine": mine})


@app.route("/api/alerts", methods=["POST"])
def save_alert():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    scope = body.get("scope")

    if not username:
        return jsonify({"error": "username requerido"}), 400
    if scope not in ("user", "general"):
        return jsonify({"error": "scope debe ser 'user' o 'general'"}), 400

    name = (body.get("name") or "Alerta sin nombre").strip()[:200] or "Alerta sin nombre"
    descripcion = (body.get("descripcion") or "")[:MAX_FIELD_LEN]
    analysis = (body.get("analysis") or "")[:MAX_FIELD_LEN]
    artifacts = (body.get("artifacts") or "")[:MAX_FIELD_LEN]
    solution = (body.get("solution") or "")[:MAX_FIELD_LEN]

    alerts = _load_alerts()
    alert_id = body.get("id")
    now = time.time()

    if alert_id:
        existing = next((a for a in alerts if a.get("id") == alert_id), None)
        if existing and (existing.get("scope") == "general" or existing.get("owner") == username):
            existing.update({
                "name": name,
                "scope": scope,
                "owner": username if scope == "user" else None,
                "descripcion": descripcion,
                "analysis": analysis,
                "artifacts": artifacts,
                "solution": solution,
                "updated_at": now,
            })
            _save_alerts(alerts)
            _write_alert_txt(existing)
            return jsonify(existing)

    new_alert = {
        "id": str(uuid.uuid4()),
        "name": name,
        "scope": scope,
        "owner": username if scope == "user" else None,
        "created_by": username,
        "descripcion": descripcion,
        "analysis": analysis,
        "artifacts": artifacts,
        "solution": solution,
        "created_at": now,
        "updated_at": now,
    }
    alerts.append(new_alert)
    _save_alerts(alerts)
    _write_alert_txt(new_alert)
    return jsonify(new_alert)


@app.route("/api/alerts/<alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    username = (request.args.get("username") or "").strip()
    alerts = _load_alerts()
    target = next((a for a in alerts if a.get("id") == alert_id), None)
    if not target:
        return jsonify({"error": "No encontrada"}), 404
    if target.get("scope") == "user" and target.get("owner") != username:
        return jsonify({"error": "No autorizado para borrar esta alerta"}), 403
    alerts = [a for a in alerts if a.get("id") != alert_id]
    _save_alerts(alerts)
    _delete_alert_txt(target)
    return jsonify({"deleted": True})


# ============================================================
# HISTORY — memoria de conversación persistente por usuario
# ============================================================
@app.route("/api/history/<username>", methods=["GET"])
def get_history(username):
    path = os.path.join(HISTORY_DIR, _safe_username_key(username) + ".json")
    if not os.path.exists(path):
        return jsonify({"history": []})
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify({"history": json.load(f)})
    except (json.JSONDecodeError, OSError):
        return jsonify({"history": []})


@app.route("/api/history", methods=["POST"])
def save_history():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username requerido"}), 400
    history = body.get("history") or []
    history = history[-MAX_HISTORY_MESSAGES:]
    path = os.path.join(HISTORY_DIR, _safe_username_key(username) + ".json")
    _atomic_write_json(path, history)
    _write_user_history_txt(username, history)
    return jsonify({"saved": True})


# ============================================================
# USUARIOS — carpeta y "base de datos" por usuario
# ============================================================
USERS_DIR = os.path.join(DATA_DIR, "usuarios")
os.makedirs(USERS_DIR, exist_ok=True)


def _user_folder(username):
    folder = os.path.join(USERS_DIR, _safe_username_key(username))
    os.makedirs(folder, exist_ok=True)
    return folder


def _credentials_path(username):
    return os.path.join(_user_folder(username), "credenciales.txt")


def _write_credentials(username, password, display_name=None):
    path = _credentials_path(username)
    lines = [
        "UFOCS - Credenciales de usuario",
        "=" * 50,
        "Usuario: " + (display_name or username),
        "Contraseña: " + password,
        "Creado: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "=" * 50,
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines))


def _read_credentials(username):
    path = _credentials_path(username)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r"Contrase\u00f1a:\s*(.*)", content)
    return match.group(1).strip() if match else None


def _write_user_history_txt(username, history):
    try:
        folder = _user_folder(username)
        path = os.path.join(folder, "historial_chat.txt")
        lines = [
            "UFOCS - Historial de chat de: " + username,
            "Actualizado: " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "=" * 50,
            "",
        ]
        for msg in history:
            role = "Usuario" if msg.get("role") == "user" else "UFOCS"
            content = msg.get("content") or ""
            lines.append(f"[{role}]")
            lines.append(content)
            lines.append("-" * 50)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines))
    except OSError:
        pass


@app.route("/api/register", methods=["POST"])
def register_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username y password son requeridos"}), 400

    cred_path = _credentials_path(username)
    is_new = not os.path.exists(cred_path)
    _write_credentials(username, password, body.get("displayName"))
    return jsonify({"created": is_new, "folder": _user_folder(username)})


@app.route("/api/login", methods=["POST"])
def login_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    stored = _read_credentials(username)
    if stored is None:
        return jsonify({"ok": False, "error": "Usuario no encontrado en el servidor"}), 404
    if stored != password:
        return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 401
    return jsonify({"ok": True})


@app.route("/api/users", methods=["GET"])
def list_users():
    users = []
    if os.path.isdir(USERS_DIR):
        for key in sorted(os.listdir(USERS_DIR)):
            cred_path = os.path.join(USERS_DIR, key, "credenciales.txt")
            if os.path.exists(cred_path):
                with open(cred_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                users.append({"username": key, "credentials_file": cred_path, "raw": raw})
    return jsonify({"users": users})


# ============================================================
# Manejo de errores
# ============================================================
@app.errorhandler(Exception)
def handle_any_error(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Error no manejado")
    return jsonify({"error": f"Error interno del servidor: {e}"}), 500


if __name__ == "__main__":
    print(f"Sirviendo {HTML_FILENAME} en http://localhost:5000/")
    print("Asegúrate de que Ollama esté corriendo (ollama serve).")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False, threaded=True)
