"""Local stand-in for the OpenAI and Gemini REST APIs (response *shapes* only; it cannot prove live API acceptance)."""
import json, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = []  # (text) per 4-second segment
_lines = {
    3: "haha that was honestly the funniest thing I have ever seen lol",
    7: "wait what really happened next is a total shock you will not believe it",
    12: "the key lesson here is remember this important rule because it solves the problem",
}
for i in range(17):
    SCRIPT.append(_lines.get(i, f"this is regular filler talk number {i} about nothing in particular today"))


def segments():
    return [{"start": i * 4.0, "end": i * 4.0 + 3.8, "text": t} for i, t in enumerate(SCRIPT)]


def words():
    out = []
    for sg in segments():
        toks = sg["text"].split(); d = (sg["end"] - sg["start"]) / len(toks)
        for k, t in enumerate(toks):
            out.append({"word": t, "start": sg["start"] + k * d, "end": sg["start"] + (k + 1) * d})
    return out


def score_from_prompt(prompt):
    cands = json.loads(prompt.split("Candidates:\n", 1)[1])
    res = []
    for c in cands:
        t = c["text"].lower()
        res.append({"id": c["id"], "comedy": 9 if "haha" in t else 1, "engagement": 9 if "wait what" in t else 2,
                    "important": 9 if "key lesson" in t else 1, "title": "Test title", "reason": "fake scorer"})
    return {"scores": res}


class H(BaseHTTPRequestHandler):
    log = []
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        H.log.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/v1/models"):
            if self.headers.get("Authorization") != "Bearer sk-good": return self._send({"error": {"message": "bad key"}}, 401)
            return self._send({"data": [{"id": i} for i in ["whisper-1", "gpt-4o-mini", "gpt-4.1", "text-embedding-3-small", "gpt-4o-mini-transcribe", "o3"]]})
        if self.path.startswith("/v1beta/models"):
            if self.headers.get("x-goog-api-key") != "gm-good": return self._send({"error": {"message": "bad key"}}, 403)
            return self._send({"models": [{"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]},
                                          {"name": "models/text-embedding", "supportedGenerationMethods": ["embedContent"]}]})
        self._send({}, 404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        H.log.append(("POST", self.path, dict(self.headers)))
        if self.path.endswith("/audio/transcriptions"):
            assert self.headers.get("Authorization") == "Bearer sk-good"
            assert b"timestamp_granularities[]" in body and b"verbose_json" in body
            return self._send({"text": " ".join(SCRIPT), "words": words(), "segments": segments()})
        if self.path.endswith("/chat/completions"):
            j = json.loads(body); prompt = j["messages"][0]["content"]
            return self._send({"choices": [{"message": {"content": json.dumps(score_from_prompt(prompt))}}]})
        if ":generateContent" in self.path:
            assert self.headers.get("x-goog-api-key") == "gm-good"
            j = json.loads(body); parts = j["contents"][0]["parts"]
            if any("inline_data" in p for p in parts):
                out = {"segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments()]}
            else:
                out = score_from_prompt(parts[0]["text"])
            return self._send({"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}]})
        self._send({}, 404)


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
