"""HTTP-layer tests (FastAPI TestClient). Requires requirements-dev.txt.  Run:  cd backend && python -m unittest tests.test_api -v
NOTE: these could not be executed in the authoring sandbox (no network to install FastAPI) — run them before deploying."""
import os, sys, tempfile, shutil, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import fake_providers
srv, base = fake_providers.start()
tmp = tempfile.mkdtemp()
os.environ.update(CLIPFORGE_ENV="test", DATA_DIR=tmp, SECRET_KEY="k" * 40, OPENAI_BASE_URL=base + "/v1", GEMINI_BASE_URL=base + "/v1beta", ALLOW_REGISTRATION="1")
from fastapi.testclient import TestClient
import main
from app import db, worker, config
import make_video

H = {"X-Requested-With": "ClipForge"}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app); cls.c = cls.ctx.__enter__()
        worker.STOP.set()  # stop background workers; tests drive worker.process() directly
        cls.video = Path(tmp) / "v.mp4"; make_video.make(cls.video)

    @classmethod
    def tearDownClass(cls): cls.ctx.__exit__(None, None, None)

    def client(self): return TestClient(main.app)

    def signup(self, c, email):
        r = c.post("/api/auth/register", json={"email": email, "password": "a-long-password"}, headers=H); self.assertEqual(r.status_code, 200, r.text); return r

    def test_auth_required_and_csrf_header(self):
        c = self.client()
        self.assertEqual(c.get("/api/jobs").status_code, 401)
        self.assertEqual(c.post("/api/auth/login", json={"email": "a@b.co", "password": "x"}).status_code, 403)  # missing X-Requested-With
        self.assertEqual(c.post("/api/auth/login", json={"email": "a@b.co", "password": "x"}, headers={**H, "Origin": "https://evil.example"}).status_code, 403)

    def test_security_headers_and_health(self):
        r = self.c.get("/health"); self.assertEqual(r.json()["status"], "ok")
        for h in ("content-security-policy", "x-content-type-options", "x-frame-options"): self.assertIn(h, r.headers)
        self.assertIn("ClipForge", self.c.get("/").text)

    def test_register_login_logout_me(self):
        c = self.client(); self.signup(c, "u1@example.com")
        self.assertEqual(c.get("/api/auth/me").json()["email"], "u1@example.com")
        self.assertEqual(c.post("/api/auth/register", json={"email": "u1@example.com", "password": "a-long-password"}, headers=H).status_code, 409)
        c2 = self.client(); self.assertEqual(c2.post("/api/auth/login", json={"email": "u1@example.com", "password": "wrong-password"}, headers=H).status_code, 401)
        self.assertEqual(c2.post("/api/auth/login", json={"email": "u1@example.com", "password": "a-long-password"}, headers=H).status_code, 200)
        c2.post("/api/auth/logout", headers=H); self.assertEqual(c2.get("/api/auth/me").status_code, 401)

    def test_keys_and_models_and_settings(self):
        c = self.client(); self.signup(c, "u2@example.com")
        self.assertEqual(c.put("/api/keys/openai", json={"key": "sk-bad-key-123456"}, headers=H).status_code, 400)
        self.assertEqual(c.put("/api/keys/openai", json={"key": "sk-good"}, headers=H).status_code, 400)  # < 10 chars: rejected as implausible
        # the fake server only accepts exactly "sk-good" (7 chars), so store it directly and exercise the models endpoint:
        from app import security
        db.set_api_key(db.get_user_by_email("u2@example.com")["id"], "openai", security.encrypt_secret("sk-good"), "sk-…good")
        m = c.get("/api/models/openai").json(); self.assertIn("whisper-1", m["transcription"]); self.assertEqual(m["defaults"]["transcription"], "whisper-1")
        self.assertNotIn("sk-good", c.get("/api/auth/me").text)  # raw key never returned
        self.assertEqual(c.put("/api/settings", json={"format": "4:3"}, headers=H).status_code, 400)
        ok = c.put("/api/settings", json={"format": "1:1", "framing": "blur"}, headers=H); self.assertEqual(ok.status_code, 200)
        self.assertEqual(c.get("/api/auth/me").json()["settings"]["format"], "1:1")

    def test_job_validation_ssrf_and_non_video(self):
        from app import security
        c = self.client(); self.signup(c, "u3@example.com"); uid = db.get_user_by_email("u3@example.com")["id"]
        db.set_api_key(uid, "openai", security.encrypt_secret("sk-good"), "x")
        import json as j
        opts = j.dumps({"transcription_model": "whisper-1", "analysis_model": "gpt-4o-mini"})
        self.assertEqual(c.post("/api/jobs", data={"options": opts, "video_url": "http://example.com/a.mp4"}, headers=H).status_code, 400)
        self.assertEqual(c.post("/api/jobs", data={"options": opts, "video_url": "https://127.0.0.1/a.mp4"}, headers=H).status_code, 400)
        self.assertEqual(c.post("/api/jobs", data={"options": opts}, headers=H).status_code, 400)  # neither file nor url
        self.assertEqual(c.post("/api/jobs", data={"options": opts}, files={"file": ("x.mp4", b"not a video", "video/mp4")}, headers=H).status_code, 400)
        self.assertEqual(c.post("/api/jobs", data={"options": opts}, files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=H).status_code, 400)

    def test_full_flow_and_isolation(self):
        from app import security
        a = self.client(); self.signup(a, "owner@example.com"); uid = db.get_user_by_email("owner@example.com")["id"]
        db.set_api_key(uid, "openai", security.encrypt_secret("sk-good"), "x")
        import json as j
        opts = j.dumps({"transcription_model": "whisper-1", "analysis_model": "gpt-4o-mini", "min_duration": 15, "max_duration": 25, "quality": 720})
        with open(self.video, "rb") as f:
            r = a.post("/api/jobs", data={"options": opts}, files={"file": ("v.mp4", f, "video/mp4")}, headers=H)
        self.assertEqual(r.status_code, 200, r.text); jid = r.json()["job_id"]
        worker.process(db.claim_next_job())
        job = a.get(f"/api/jobs/{jid}").json(); self.assertEqual(job["status"], "completed", job)
        self.assertEqual([c["category"] for c in job["clips"]], ["Comedy", "Engagement", "Important Moment"])
        url = job["clips"][0]["url"]
        v = a.get(url); self.assertEqual(v.status_code, 200); self.assertEqual(v.headers["content-type"], "video/mp4")
        self.assertEqual(a.get(url, headers={"Range": "bytes=0-99"}).status_code, 206)
        self.assertIn("attachment", a.get(url + "?download=1").headers["content-disposition"])
        b = self.client(); self.signup(b, "intruder@example.com")
        self.assertEqual(b.get(url).status_code, 404); self.assertEqual(b.get(f"/api/jobs/{jid}").status_code, 404)
        self.assertEqual(self.client().get(url).status_code, 401)
        self.assertEqual(a.delete(f"/api/jobs/{jid}", headers=H).status_code, 200); self.assertEqual(a.get(url).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
