# UFOCS — Agente Virtual de Ciberseguridad

Servidor puente basado en **Flask** que conecta el panel web de UFOCS con una instancia local o remota de **Ollama** a través de tunneling seguro.

---

### Requisitos Previos

* Python 3.10 o superior
* Ollama instalado y en ejecución
* Cloudflare (`cloudflared`) o ngrok para exposición de puertos (opcional para uso remoto)

---

### Instala las dependencias

Abre tu terminal en la carpeta del proyecto y ejecuta:

```bash
pip install flask requests
