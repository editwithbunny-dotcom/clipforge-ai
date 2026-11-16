import os, sys, tempfile, time, json, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
tmp = tempfile.mkdtemp()
os.environ.update(CLIPFORGE_ENV="test", DATA_DIR=tmp, SECRET_KEY="x" * 40)
from app import config, db, security


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.reset_settings(); config.get_settings(); db.init_db()

    def test_password_roundtrip(self):
        h = security.hash_password("correct horse battery")
        self.assertTrue(security.verify_password("correct horse battery", h))
        self.assertFalse(security.verify_password("wrong", h))
        self.assertFalse(security.verify_password("x", "garbage"))

    def test_credentials_validation(self):
        with self.assertRaises(ValueError): security.validate_credentials("bad", "longenoughpw")
        with self.assertRaises(ValueError): security.validate_credentials("a@b.co", "short")
        security.validate_credentials("a@b.co", "longenoughpw")

    def test_token(self):
        t = security.create_token(7)
        self.assertEqual(security.read_token(t), 7)
        self.assertIsNone(security.read_token(t + "x"))

    def test_api_key_encryption(self):
        enc = security.encrypt_secret("sk-test-123456789")
        self.assertNotIn("sk-test", enc)
        self.assertEqual(security.decrypt_secret(enc), "sk-test-123456789")

    def test_ssrf(self):
        for bad in ["http://example.com/a.mp4", "https://127.0.0.1/a.mp4", "https://localhost/a.mp4",
                    "https://169.254.169.254/latest", "https://10.0.0.5/x", "https://user:pw@example.com/x", "file:///etc/passwd"]:
            with self.assertRaises(security.UnsafeURL, msg=bad): security.check_public_https_url(bad)

    def test_users_keys_jobs(self):
        uid = db.create_user("T@Example.com", security.hash_password("longenoughpw"))
        self.assertIsNone(db.create_user("t@example.com", "x"))  # duplicate, case-insensitive
        db.set_api_key(uid, "openai", security.encrypt_secret("sk-abc"), "sk-…abc")
        db.set_api_key(uid, "openai", security.encrypt_secret("sk-def"), "sk-…def")
        self.assertEqual(len(db.list_api_keys(uid)), 1)
        jid = db.create_job(uid, "/tmp/x.mp4", "x.mp4", {"a": 1})
        self.assertEqual(db.count_active_jobs(uid), 1)
        j = db.claim_next_job(); self.assertEqual(j["id"], jid)
        self.assertIsNone(db.claim_next_job())  # nothing else queued
        self.assertEqual(db.requeue_interrupted(), 1)
        self.assertEqual(db.get_job(jid)["status"], "queued")
        j = db.claim_next_job(); db.update_job(jid, status="completed")
        self.assertEqual(db.count_active_jobs(uid), 0)
        self.assertIsNone(db.get_job(jid, uid + 99))  # isolation
        db.add_clip(jid, 0, "Comedy", "t", "r", 9.1, 1, 31, "f.mp4", {})
        self.assertEqual(db.clip_owner("f.mp4")["user_id"], uid)
        self.assertIsNone(db.clip_owner("missing.mp4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
