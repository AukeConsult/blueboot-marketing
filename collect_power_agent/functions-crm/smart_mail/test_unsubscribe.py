import sys
import types
import unittest

# The VM running tests may lack google-cloud-firestore; stub ArrayUnion.
if "google.cloud.firestore_v1" not in sys.modules:
    try:
        import google.cloud.firestore_v1  # noqa: F401
    except Exception:
        for _n in ("google", "google.cloud", "google.cloud.firestore_v1"):
            sys.modules.setdefault(_n, types.ModuleType(_n))
        sys.modules["google.cloud.firestore_v1"].ArrayUnion = lambda v: ("ArrayUnion", v)

from smart_mail import unsubscribe as u
from smart_mail.outreach_render_mail import render_mail, MailStep

CFG = {"secret": "s3cret", "base_url": "https://x.test/crmApi", "mailto": "",
       "footer_html": u.DEFAULT_FOOTER_HTML, "footer_text": u.DEFAULT_FOOTER_TEXT}


class Tokens(unittest.TestCase):
    def test_roundtrip(self):
        t = u.make_token("k", "NO_HOT", "abc_def")
        self.assertEqual(u.parse_token("k", t), ("NO_HOT", "abc_def"))

    def test_tampered_or_wrong_secret_rejected(self):
        t = u.make_token("k", "NO_HOT", "abc_def")
        self.assertIsNone(u.parse_token("other", t))
        payload, sig = t.split(".")
        forged = u.make_token("k", "NO_HOT", "someone_else").split(".")[0] + "." + sig
        self.assertIsNone(u.parse_token("k", forged))
        self.assertIsNone(u.parse_token("k", "garbage"))
        self.assertIsNone(u.parse_token("k", ""))

    def test_url_and_headers(self):
        url = u.unsubscribe_url(CFG, "C", "d")
        self.assertTrue(url.startswith("https://x.test/crmApi/unsubscribe?t="))
        lu, post = u.list_headers(CFG, url, "moez@blueboot.ai")
        self.assertIn("<mailto:moez@blueboot.ai?subject=unsubscribe>", lu)
        self.assertIn(f"<{url}>", lu)
        self.assertEqual(post, "List-Unsubscribe=One-Click")


class Footer(unittest.TestCase):
    def test_footer_added_last(self):
        step = MailStep(index=0, mail_type="intro", subject="Hi", body_html="<p>Hello</p>", body_text="")
        r = render_mail(step, {"email": "a@b.no"}, None, u.footers(CFG, "https://u/x"))
        self.assertTrue(r.html_body.endswith("</p>"))
        self.assertIn('href="https://u/x"', r.html_body)
        self.assertIn("Unsubscribe: https://u/x", r.text_body)

    def test_no_footer_when_none(self):
        step = MailStep(index=0, mail_type="intro", subject="Hi", body_html="", body_text="Hello")
        r = render_mail(step, {"email": "a@b.no"})
        self.assertNotIn("nsubscribe", r.text_body)


class Requests(unittest.TestCase):
    def test_detects_stop_requests(self):
        for s, b in [("Re: offer", "Please unsubscribe me"), ("STOP", "stop sending me mail"),
                     ("Re: x", "Meld meg av listen"), ("Opt out", ""), ("Re: x", "remove me from your list")]:
            self.assertTrue(u.is_unsubscribe_request(s, b), (s, b))

    def test_normal_replies_are_not_stop_requests(self):
        self.assertFalse(u.is_unsubscribe_request("Re: BlueSearch", "Sounds interesting, call me tomorrow"))
        self.assertFalse(u.is_unsubscribe_request("Re: x", "Thanks.\n\n" + "line\n" * 20 + "unsubscribe"))


class Doc:
    def __init__(self, d): self.d, self.exists = d, d is not None
    def to_dict(self): return self.d


class Ref:
    def __init__(self, d): self.d, self.updates = d, []
    def get(self): return Doc(self.d)
    def update(self, upd): self.updates.append(upd)
    def collection(self, _n): return self
    def document(self, _i): return self


class Db(Ref):
    pass


class Apply(unittest.TestCase):
    def test_excludes_contact(self):
        db = Db({"email": "a@b.no", "status": "pending"})
        self.assertEqual(u.apply_unsubscribe(db, "C", "d"), "ok")
        upd = db.updates[0]
        self.assertEqual(upd["status"], "excluded")
        self.assertEqual(upd["followup_status"], "not_interested")
        self.assertTrue(upd["unsubscribed"])

    def test_repeat_and_missing(self):
        self.assertEqual(u.apply_unsubscribe(Db({"unsubscribed": True}), "C", "d"), "already")
        self.assertEqual(u.apply_unsubscribe(Db(None), "C", "d"), "missing")


if __name__ == "__main__":
    unittest.main()
