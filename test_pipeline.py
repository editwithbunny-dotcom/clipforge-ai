import json, os, shutil, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import fake_providers
srv, base = fake_providers.start()
tmp = tempfile.mkdtemp()
os.environ.update(CLIPFORGE_ENV="test", DATA_DIR=tmp, SECRET_KEY="k" * 40, OPENAI_BASE_URL=base + "/v1", GEMINI_BASE_URL=base + "/v1beta")
from app import config, db, security, providers, worker, video, options
import make_video

class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.reset_settings(); cls.s = config.get_settings(); db.init_db()
        cls.src = Path(tmp) / "src.mp4"; make_video.make(cls.src)
        cls.uid = db.create_user("a@b.co", security.hash_password("longenoughpw"))
        db.set_api_key(cls.uid, "openai", security.encrypt_secret("sk-good"), "x")
        db.set_api_key(cls.uid, "gemini", security.encrypt_secret("gm-good"), "x")

    def _run(self, provider, **over):
        o = options.validate_options({"provider": provider, "transcription_model": "whisper-1" if provider == "openai" else "gemini-flash",
                                      "analysis_model": "gpt-4o-mini" if provider == "openai" else "gemini-flash", "min_duration": 15, "max_duration": 25,
                                      "quality": 720, **over})
        copy = Path(tmp) / f"copy_{provider}_{len(os.listdir(tmp))}.mp4"; shutil.copy(self.src, copy)
        jid = db.create_job(self.uid, str(copy), "src.mp4", o)
        job = db.claim_next_job(); self.assertEqual(job["id"], jid)
        worker.process(job)
        return db.get_job(jid), db.job_clips(jid)

    def test_model_listing(self):
        m = providers.list_models("openai", "sk-good")
        self.assertEqual(m["transcription"], ["whisper-1"]); self.assertIn("gpt-4o-mini", m["analysis"]); self.assertNotIn("text-embedding-3-small", m["analysis"])
        self.assertEqual(providers.list_models("gemini", "gm-good")["analysis"], ["gemini-flash"])
        with self.assertRaises(providers.ProviderError): providers.list_models("openai", "sk-bad")

    def test_openai_full_pipeline_three_categories(self):
        job, clips = self._run("openai")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual([c["category"] for c in clips], ["Comedy", "Engagement", "Important Moment"])
        by = {c["category"]: c for c in clips}
        # fake scorer rewards these exact script lines (segments 3, 7, 12 => 12-16s, 28-32s, 48-52s)
        self.assertLessEqual(by["Comedy"]["start_s"], 16); self.assertGreaterEqual(by["Comedy"]["end_s"], 12)
        self.assertLessEqual(by["Engagement"]["start_s"], 32); self.assertGreaterEqual(by["Engagement"]["end_s"], 28)
        self.assertLessEqual(by["Important Moment"]["start_s"], 52); self.assertGreaterEqual(by["Important Moment"]["end_s"], 48)
        for c in clips:
            p = self.s.outputs_dir / c["filename"]; self.assertTrue(p.exists())
            i = video.probe(p)
            self.assertEqual((i["width"], i["height"]), (720, 1280)); self.assertTrue(i["has_audio"])
            self.assertTrue(15 - 0.6 <= i["duration"] <= 25.6, i["duration"])
            m = json.loads(c["meta_json"]); self.assertEqual(m["selection"], "ai"); self.assertGreater(m["captions"], 0); self.assertEqual(m["framing_used"], "track")
        self.assertFalse((self.s.uploads_dir / job["id"]).exists())  # temp work cleaned

    def test_gemini_pipeline_and_settings_effect(self):
        job, clips = self._run("gemini", categories=["comedy"], format="1:1", quality=720, framing="blur", caption_style="none", audio="off")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(len(clips), 1)
        i = video.probe(self.s.outputs_dir / clips[0]["filename"])
        self.assertEqual((i["width"], i["height"]), (720, 720))
        m = json.loads(clips[0]["meta_json"]); self.assertEqual(m["captions"], 0); self.assertEqual(m["framing_used"], "blur")

    def test_missing_key_and_bad_key_fail_cleanly(self):
        uid2 = db.create_user("c@d.co", security.hash_password("longenoughpw"))
        o = options.validate_options({"transcription_model": "whisper-1", "analysis_model": "gpt-4o-mini"})
        copy = Path(tmp) / "nokey.mp4"; shutil.copy(self.src, copy)
        jid = db.create_job(uid2, str(copy), "s", o); worker.process(db.claim_next_job())
        j = db.get_job(jid); self.assertEqual(j["status"], "failed"); self.assertIn("API key", j["error"])

    def test_cancel(self):
        o = options.validate_options({"transcription_model": "whisper-1", "analysis_model": "gpt-4o-mini", "min_duration": 15, "max_duration": 25, "quality": 720})
        copy = Path(tmp) / "cancel.mp4"; shutil.copy(self.src, copy)
        jid = db.create_job(self.uid, str(copy), "s", o); job = db.claim_next_job()
        self.assertTrue(db.request_cancel(jid, self.uid)); worker.process(job)
        self.assertEqual(db.get_job(jid)["status"], "cancelled")

    def test_options_validation(self):
        for bad in [{"format": "4:3"}, {"quality": 480}, {"categories": []}, {"categories": ["x"]}, {"min_duration": 5}, {"min_duration": 50, "max_duration": 40}, {"framing": "zoom"}, {"audio": "loud"}, {"analysis_model": "a b;rm"}]:
            with self.assertRaises(options.OptionsError, msg=str(bad)): options.validate_options(bad)

if __name__ == "__main__":
    unittest.main(verbosity=2)
