#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler

# Follow the ai-controller's active voice instead of a hardcoded static voice.
# Same resolution pattern as ai-controller/scripts/hermes_tts_generate.py:
# voice_toggle.load_voice() / get_voice() decide what is active; the active
# voice pack's .onnx model is used when it exists, otherwise fall back to the
# original stock lessac model.
PIPER_MODEL = os.path.expanduser("~/scripts/voices/en_US-lessac-medium.onnx")

AI_CONTROLLER_SCRIPTS = os.path.expanduser("~/ai-controller/scripts")

def _resolve_active_piper_model(default=PIPER_MODEL):
    """Return the .onnx model path for the ai-controller's active voice.

    Falls back to `default` when the controller state is unreadable, the
    active voice is not a piper voice, or its model file is missing.
    """
    try:
        if AI_CONTROLLER_SCRIPTS not in sys.path:
            sys.path.insert(0, AI_CONTROLLER_SCRIPTS)
        import voice_toggle  # noqa: PLC0415 - deliberate lazy import
        voice = voice_toggle.get_voice(voice_toggle.load_voice())
        model = (voice or {}).get("model")
        if (voice or {}).get("engine") == "piper" and model and os.path.isfile(model):
            return model
    except Exception:
        pass
    return default

PIPER_BIN = shutil.which("piper") or os.path.expanduser("~/.local/bin/piper")
APLAY_BIN = shutil.which("aplay")

class TTSHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/health'):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin','*')
            self.send_header('Content-Type','text/plain')
            self.end_headers()
            self.wfile.write(b'ok')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == '/speak':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                text = json.loads(body).get('text', '').strip()
                if not text:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'missing text')
                    return
                # Resolved per-request (not once at import time) so a voice
                # switch via voice_manager.py takes effect on the very next
                # /speak call, no server restart needed.
                piper_model = _resolve_active_piper_model()
                if not os.path.exists(PIPER_BIN) or not os.path.exists(piper_model):
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'piper unavailable')
                    return
                tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                tmp.close()
                cleanup_tmp = True
                try:
                    # Piper CLI flag is `-f` (was `--output_file` in older builds).
                    proc = subprocess.run(
                        [PIPER_BIN, '-m', piper_model, '-f', tmp.name],
                        input=text.encode(), capture_output=True, timeout=30
                    )
                    if proc.returncode != 0 or not os.path.getsize(tmp.name):
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write((proc.stderr or b'piper failed')[:500])
                        return
                    audio = open(tmp.name, 'rb').read()
                    if APLAY_BIN:
                        cleanup_tmp = False
                        subprocess.Popen([
                            'bash', '-c',
                            '"$1" "$2" >/dev/null 2>&1; rm -f "$2"',
                            'tts-play', APLAY_BIN, tmp.name
                        ])
                    print(f"[TTS] {text[:60]}...")
                finally:
                    if cleanup_tmp:
                        try:
                            os.unlink(tmp.name)
                        except Exception:
                            pass
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin','*')
                self.send_header('Content-Type','audio/wav')
                self.send_header('Content-Length', str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode()[:500])
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()
    def log_message(self,*a): pass

HTTPServer(('0.0.0.0',5050),TTSHandler).serve_forever()
