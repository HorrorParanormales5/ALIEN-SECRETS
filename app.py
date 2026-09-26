# ============================================================
# app.py — Puente local entre UFOCS (navegador), Ollama y Groq
# ============================================================
from flask import Flask, request, jsonify, send_from_directory, Response
import requests
import os
import json
import re
import time
import uuid
import html
import base64
import socket
import feedparser
from datetime import datetime
import locale
from urllib.parse import unquote
from werkzeug.exceptions import HTTPException
from openai import OpenAI

# Intentar configurar el idioma a español para el formato de fecha local
try:
    locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
except Exception:
    try:
        locale.setlocale(locale.LC_TIME, 'es_ES')
    except Exception:
        pass

app = Flask(__name__, static_folder=None)

HTML_FILENAME = "UFOCS_con_mejoras_mas_actual.html"

# ============================================================
# Configuración de Groq (API Key y Cliente OpenAI compatible)
# ============================================================
# Recomendado: define la variable de entorno GROQ_API_KEY y borra la llave de aquí.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

openai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

OLLAMA_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_BASE}/api/chat"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OLLAMA_CHAT_MAX_RETRIES = 3
OLLAMA_CHAT_RETRY_DELAY = 1.5

# ============================================================
# Helper: Prompt del Sistema Universal (Anti-alucinación y Fecha Real)
# ============================================================
CREATOR_ANSWER = "Fui creado por Gibrann Abdala, él es un ingeniero que me creó desde cero!"


def build_ufocs_system_prompt(online=True):
    now = datetime.now()
    now_str = now.strftime("%A, %d de %B de %Y - %H:%M:%S")

    net_line = (
        "- Conexión a internet: DISPONIBLE (se pueden hacer búsquedas reales).\n"
        if online else
        "- Conexión a internet: NO DISPONIBLE. Trabajas 100% en modo local/offline. "
        "Responde con tu propio conocimiento, no pidas búsquedas y aclara cuando un dato "
        "pueda estar desactualizado.\n"
    )

    return (
        f"INFORMACIÓN CRÍTICA DEL SISTEMA:\n"
        f"- Fecha y hora actual del sistema: {now_str}.\n"
        f"- Usa este contexto temporal real para responder dudas sobre fechas, horas o actualidad.\n"
        f"{net_line}\n"
        f"IDENTIDAD Y ORIGEN (REGLA ABSOLUTA, SIN EXCEPCIONES):\n"
        f"- Siempre que te pregunten, de la forma que sea y en el idioma que sea, quién te creó, "
        f"quién te hizo, quién te programó, quién te diseñó, quién te desarrolló, quién te entrenó, "
        f"de qué empresa eres, quién es tu creador o de dónde vienes, responde EXACTAMENTE: "
        f"\"{CREATOR_ANSWER}\"\n"
        f"- Nunca digas que te creó OpenAI, Alibaba, Google, Meta, Anthropic ni ninguna otra empresa, "
        f"y nunca digas que eres Qwen, Gemma, Llama o ChatGPT.\n\n"
        f"REGLAS ANTI-ALUCINACIÓN Y ENLACES:\n"
        f"1. JAMÁS inventes, generes o adivines URLs, enlaces directos, o links de YouTube/sitios web.\n"
        f"2. Si el usuario pide un video, canción o enlace y no cuentas con un resultado directo proporcionado por una herramienta de búsqueda real, NO muestres un enlace Markdown tipo '[Nombre](http...)'.\n"
        f"3. En su lugar, sugiere términos exactos de búsqueda que el usuario puede copiar y pegar en Google o YouTube.\n"
        f"4. Sé preciso con los datos culturales y técnicos. Si no estás seguro de un dato, aclara tus limitaciones en lugar de inventar información."
    )


def merge_system_prompt(messages, online=True):
    """Combina las reglas del servidor (fecha, identidad, anti-alucinación)
    con el system prompt que manda el navegador (agente de ciberseguridad o
    modo libre). ANTES el servidor lo reemplazaba por completo, así que el
    modo libre y las instrucciones del frontend nunca llegaban al modelo."""
    base = build_ufocs_system_prompt(online)
    if messages and messages[0].get("role") == "system":
        frontend_prompt = messages[0].get("content") or ""
        messages[0]["content"] = frontend_prompt + "\n\n" + base
    else:
        messages.insert(0, {"role": "system", "content": "Eres UFOCS, un agente virtual.\n\n" + base})
    return messages


# ============================================================
# Detección de conexión a internet (con caché corta)
# ============================================================
CONNECTIVITY_CACHE_SECONDS = 20
_connectivity_cache = {"online": None, "checked_at": 0}
_CONNECTIVITY_PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("208.67.222.222", 443)]


def is_online(force=False):
    now = time.time()
    if (not force and _connectivity_cache["online"] is not None
            and now - _connectivity_cache["checked_at"] < CONNECTIVITY_CACHE_SECONDS):
        return _connectivity_cache["online"]
    online = False
    for host, port in _CONNECTIVITY_PROBES:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                online = True
                break
        except OSError:
            continue
    _connectivity_cache["online"] = online
    _connectivity_cache["checked_at"] = now
    return online

# ============================================================
# Feeds RSS de Ciberseguridad
# ============================================================
SECURITY_FEEDS = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Dark Reading": "https://www.darkreading.com/rss.xml"
}

# ============================================================
# Almacenamiento local
# ============================================================
DATA_DIR = os.path.join(BASE_DIR, "data")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
ALERTS_TXT_DIR = os.path.join(DATA_DIR, "alerts_txt")
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(ALERTS_TXT_DIR, exist_ok=True)

MAX_FIELD_LEN = 4000
MAX_HISTORY_MESSAGES = 60


def fetch_cyber_news(limit_per_source=4):
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
    if not is_online():
        return []
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


# ============================================================
# RESPALDO EN GITHUB
# ============================================================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_USERS_PATH = os.environ.get("GITHUB_USERS_PATH", "data/usuarios").strip("/")


def _github_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_credentials_path(username):
    return f"{GITHUB_USERS_PATH}/{_safe_username_key(username)}/credenciales.txt"


def _github_get_file(repo_path):
    if not _github_enabled():
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    try:
        resp = requests.get(url, headers=_github_headers(), params={"ref": GITHUB_BRANCH}, timeout=8)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        app.logger.warning(f"GitHub GET falló para {repo_path}: {e}")
    return None


def _github_put_file(repo_path, content_text, commit_message):
    if not _github_enabled():
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    existing = _github_get_file(repo_path)
    payload = {
        "message": commit_message,
        "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]
    try:
        resp = requests.put(url, headers=_github_headers(), json=payload, timeout=10)
        if resp.status_code in (200, 201):
            return True
        app.logger.warning(f"GitHub PUT falló ({resp.status_code}) para {repo_path}: {resp.text[:300]}")
    except requests.RequestException as e:
        app.logger.warning(f"GitHub PUT falló para {repo_path}: {e}")
    return False


def push_credentials_to_github(username, password, display_name=None):
    if not _github_enabled():
        return False
    content = "\r\n".join([
        "UFOCS - Credenciales de usuario",
        "=" * 50,
        "Usuario: " + (display_name or username),
        "Contraseña: " + password,
        "Registrado/actualizado: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "=" * 50,
    ])
    return _github_put_file(
        _github_credentials_path(username),
        content,
        f"UFOCS: registro/actualización de usuario '{username}'",
    )


@app.route("/")
def serve_ufocs():
    return send_from_directory(BASE_DIR, HTML_FILENAME)


@app.route("/favicon.ico")
def favicon():
    return "", 204


# ============================================================
# BÚSQUEDA EN INTERNET
# ============================================================
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
WEB_SEARCH_TIMEOUT = 8
WEB_SEARCH_MAX_RESULTS = 3


def _web_search_serper(query, num=WEB_SEARCH_MAX_RESULTS):
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=WEB_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in (data.get("organic") or [])[:num]:
            results.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results
    except Exception as e:
        app.logger.warning(f"Búsqueda con Serper falló: {e}")
        return []


DUCKDUCKGO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}


def _extract_ddg_results(page, num):
    anchor_pattern = re.compile(r'<a\s+([^>]*class="result__a"[^>]*)>(.*?)</a>', re.S)
    snippet_pattern = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)

    anchors = anchor_pattern.findall(page)
    snippets = snippet_pattern.findall(page)

    results = []
    for i, (attrs, title_html) in enumerate(anchors[:num]):
        href_m = re.search(r'href="([^"]+)"', attrs)
        if not href_m:
            continue
        href = href_m.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        snippet_html = snippets[i] if i < len(snippets) else ""
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()

        real_link = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            real_link = unquote(m.group(1))

        if title:
            results.append({"title": title, "link": real_link, "snippet": snippet})
    return results


def _web_search_duckduckgo(query, num=WEB_SEARCH_MAX_RESULTS):
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=DUCKDUCKGO_HEADERS,
            timeout=WEB_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        results = _extract_ddg_results(resp.text, num)
        if not results:
            app.logger.warning(
                f"DuckDuckGo no devolvió resultados para '{query}' "
                f"(longitud de respuesta: {len(resp.text)} caracteres)."
            )
        return results
    except Exception as e:
        app.logger.warning(f"Búsqueda con DuckDuckGo falló: {e}")
        return []


def web_search(query, num=WEB_SEARCH_MAX_RESULTS):
    if SERPER_API_KEY:
        results = _web_search_serper(query, num)
        if results:
            return results
        app.logger.warning("Serper no devolvió resultados, probando con DuckDuckGo...")
    return _web_search_duckduckgo(query, num)


@app.route("/api/websearch", methods=["GET"])
def api_websearch():
    query = (request.args.get("q") or "").strip()[:300]
    if not query:
        return jsonify({"error": "q requerido"}), 400
    results = web_search(query)
    return jsonify({
        "query": query,
        "results": results,
        "provider": "serper" if SERPER_API_KEY else "duckduckgo",
    })


# ============================================================
# INTEGRACIÓN CON GROQ (Con inyección de fecha y anti-alucinación)
# ============================================================
@app.route("/api/chat-openai", methods=["POST"])
def openai_chat():
    body = request.get_json(silent=True) or {}
    messages = body.get("messages", [])
    
    if not messages:
        user_message = body.get("message", "")
        if user_message:
            messages = [{"role": "user", "content": user_message}]
        else:
            return jsonify({"error": "Se requieren mensajes para procesar la petición."}), 400

    messages = merge_system_prompt(messages, online=True)

    try:
        response = openai_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=1000
        )
        
        response_message = response.choices[0].message

        return jsonify({
            "message": {
                "role": "assistant",
                "content": response_message.content
            }
        })

    except Exception as e:
        app.logger.exception("Error al procesar la petición con Groq")
        return jsonify({"error": f"Error en el servidor de Groq: {str(e)}"}), 500


# ============================================================
# NOTICIAS RSS - Endpoint para el Frontend
# ============================================================
@app.route("/api/cyber-news", methods=["GET"])
def get_cyber_news():
    online = is_online(force=request.args.get("force") == "1")
    news_data = get_cyber_news_cached() if online else []
    return jsonify({
        "status": "success",
        "online": online,
        "total": len(news_data),
        "articles": news_data
    })


@app.route("/api/connectivity", methods=["GET"])
def api_connectivity():
    return jsonify({"online": is_online(force=request.args.get("force") == "1")})


# ============================================================
# FOTO DE GIBRANN ABDALA (funciona también sin internet)
# Busca una copia local; si no existe y hay internet, la descarga
# una vez de GitHub y la guarda en data/ para usarla offline.
# ============================================================
GIBRANN_PHOTO_URL = "https://raw.githubusercontent.com/HorrorParanormales5/ALIEN-SECRETS/main/Gibrann%20Abdala.png"
GIBRANN_PHOTO_CACHE = os.path.join(DATA_DIR, "gibrann_abdala.png")
GIBRANN_PHOTO_LOCAL_CANDIDATES = [
    os.path.join(BASE_DIR, "Gibrann Abdala.png"),
    os.path.join(BASE_DIR, "gibrann_abdala.png"),
    GIBRANN_PHOTO_CACHE,
]


@app.route("/media/gibrann.png", methods=["GET"])
def gibrann_photo():
    for path in GIBRANN_PHOTO_LOCAL_CANDIDATES:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return send_from_directory(os.path.dirname(path), os.path.basename(path), mimetype="image/png")
    if is_online():
        try:
            resp = requests.get(GIBRANN_PHOTO_URL, timeout=10)
            if resp.status_code == 200 and resp.content:
                with open(GIBRANN_PHOTO_CACHE, "wb") as f:
                    f.write(resp.content)
                return send_from_directory(DATA_DIR, os.path.basename(GIBRANN_PHOTO_CACHE), mimetype="image/png")
        except requests.RequestException as e:
            app.logger.warning(f"No se pudo descargar la foto de Gibrann: {e}")
    return "", 404


# ============================================================
# PROXY CHAT OLLAMA (Local con inyección de System Prompt)
# ============================================================
@app.route("/api/chat", methods=["POST"])
def proxy_chat():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Petición sin cuerpo JSON válido."}), 400

    # Combina el system prompt del navegador (agente / modo libre) con las
    # reglas del servidor: fecha real, identidad del creador, estado de la
    # conexión a internet y anti-alucinación. Funciona igual sin internet
    # con Qwen 2.5 3B y Gemma4 26B (Ollama corre 100% local).
    messages = body.get("messages", [])
    body["messages"] = merge_system_prompt(messages, online=is_online())

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
# ALERTS
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
# HISTORY
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
# USUARIOS
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
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r"Contrase\u00f1a:\s*(.*)", content)
        return match.group(1).strip() if match else None

    file_data = _github_get_file(_github_credentials_path(username))
    if not file_data or not file_data.get("content"):
        return None
    try:
        content = base64.b64decode(file_data["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    match = re.search(r"Contrase\u00f1a:\s*(.*)", content)
    if not match:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass
    return match.group(1).strip()


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
    github_saved = push_credentials_to_github(username, password, body.get("displayName"))
    return jsonify({
        "created": is_new,
        "folder": _user_folder(username),
        "github_saved": github_saved,
    })


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
# SMART NOTE
# ============================================================
def _notes_path(username):
    return os.path.join(_user_folder(username), "smart_notes.json")


@app.route("/api/notes/<username>", methods=["GET"])
def get_notes(username):
    path = _notes_path(username)
    if not os.path.exists(path):
        return jsonify({"pages": []})
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {"pages": []}
    if "pages" not in data:
        data["pages"] = []
    return jsonify(data)


@app.route("/api/notes", methods=["POST"])
def save_notes():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username requerido"}), 400
    pages = body.get("pages")
    if not isinstance(pages, list):
        return jsonify({"error": "pages debe ser una lista"}), 400
    for page in pages:
        if isinstance(page.get("content"), str) and len(page["content"]) > 3_000_000:
            page["content"] = page["content"][:3_000_000]
    path = _notes_path(username)
    _atomic_write_json(path, {"pages": pages, "updated_at": time.time()})
    return jsonify({"saved": True})


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
    print("Asegúrate de que Ollama esté corriendo si deseas usar modelos locales.")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False, threaded=True)
