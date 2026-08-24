# ============================================================
# app.py — Puente local / nube entre UFOCS (navegador) y Ollama
# ============================================================
# Qué hace este servidor:
# 1) Sirve tu archivo HTML de UFOCS en la raíz
# 2) Recibe las peticiones de chat del navegador en /api/chat
#    y las reenvía a Ollama a través del túnel público de Cloudflare.
# 3) Como este reenvío ocurre servidor-a-servidor (Flask -> Ollama),
#    NO pasa por CORS del navegador. El navegador solo habla con
#    Flask, que está en su mismo origen, así que tampoco hay bloqueo.

from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import json
import re
import time
import uuid

app = Flask(__name__, static_folder=None)

HTML_FILENAME = "UFOCS_con_mejoras_mas_actual.html"   # <-- cambia esto si tu archivo tiene otro nombre

# Lee dinámicamente la URL desde las variables de entorno de Render (OLLAMA_URL)
# Si no existe en el entorno, usa por defecto la URL de Cloudflare especificada.
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", 
    "https://karen-reflect-sec-prospects.trycloudflare.com/api/chat"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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


def _atomic_write_json(path, data):
    """Escribe un archivo JSON de forma atómica (a un temporal y luego renombra),
    para que una petición a medias o un reinicio del servidor nunca deje el
    archivo corrupto o a medio escribir."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _safe_filename(text):
    text = re.sub(r"[^a-zA-Z0-9_\-]+", "_", text or "alerta").strip("_")
    return (text or "alerta")[:60]


def _write_alert_txt(alert):
    """Guarda/actualiza una copia legible en texto plano (estilo Notepad) de la
    alerta, además del JSON. Sirve como respaldo de recursos fácil de abrir."""
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


@app.route("/api/chat", methods=["POST"])
def proxy_chat():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Petición sin cuerpo JSON válido."}), 400

    target_url = os.environ.get("OLLAMA_URL", OLLAMA_URL)

    try:
        # Sin timeout: modelos grandes pueden tardar varios minutos en procesar.
        resp = requests.post(target_url, json=body, timeout=None)
    except requests.exceptions.ConnectionError:
        return jsonify({
            "error": "No se pudo conectar con Ollama a través del túnel de Cloudflare. "
                     "Verifica que 'cloudflared' y 'ollama serve' estén activos."
        }), 502

    # Reenviamos tal cual el status y el cuerpo que devolvió Ollama
    try:
        return jsonify(resp.json()), resp.status_code
    except ValueError:
        return (resp.text, resp.status_code)


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
        # Solo se puede editar si eres el dueño (alerta "user") o si es "general"
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
    history = history[-MAX_HISTORY_MESSAGES:]  # evita que crezca sin límite
    path = os.path.join(HISTORY_DIR, _safe_username_key(username) + ".json")
    _atomic_write_json(path, history)
    return jsonify({"saved": True})


# ============================================================
# Manejo de errores
# ============================================================
@app.errorhandler(Exception)
def handle_any_error(e):
    app.logger.exception("Error no manejado")
    return jsonify({"error": f"Error interno del servidor: {e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Sirviendo {HTML_FILENAME} en puerto {port}")
    print("Conectado mediante el túnel a Ollama en Cloudflare.")
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
