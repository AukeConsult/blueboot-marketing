"""Tests for the mail trailer: cleaning, loading defaults, and how render_mail adds it."""
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smart_mail import mail_trailer as mt
from smart_mail.outreach_render_mail import render_mail, MailStep

TRAILER = {"enabled": True, "html": "<p>Hi {{name}}, trailer</p>", "text": "Hi {{name}}, trailer text"}
CONTACT = {"name": "Ann", "email": "ann@example.com"}


class Snap:
    def __init__(self, d): self._d, self.exists = d, d is not None
    def to_dict(self): return dict(self._d or {})

class FakeDb:
    def __init__(self, d): self._d = d
    def collection(self, _): return self
    def document(self, _): return self
    def get(self): return Snap(self._d)


class TrailerTests(unittest.TestCase):
    def test_load_defaults_when_nothing_saved(self):
        t = mt.load_trailer(FakeDb(None))
        self.assertFalse(t["enabled"])
        self.assertIn("Who Is BlueSearch Built For?", t["html"])
        self.assertIn("Who Is BlueSearch Built For?", t["text"])

    def test_load_saved_values(self):
        t = mt.load_trailer(FakeDb({"enabled": True, "html": "<p>x</p>", "text": "x"}))
        self.assertEqual(t, {"enabled": True, "html": "<p>x</p>", "text": "x"})

    def test_clean_html_removes_scripts_and_handlers(self):
        dirty = '<p onclick="x()">a</p><script>alert(1)</script><a href="javascript:evil()">l</a>'
        out = mt.clean_html(dirty)
        for bad in ("script", "onclick", "javascript"):
            self.assertNotIn(bad, out)
        self.assertIn("<p>a</p>", out)

    def test_html_mail_gets_html_and_text_versions(self):
        r = render_mail(MailStep(0, "intro", "S", "<p>Body</p>", ""), CONTACT, TRAILER)
        self.assertTrue(r.html_body.startswith("<p>Body</p>"))
        self.assertTrue(r.html_body.endswith("<p>Hi Ann, trailer</p>"))
        self.assertTrue(r.text_body.endswith("Hi Ann, trailer text"))
        self.assertNotIn("<", r.text_body)

    def test_plain_mail_gets_text_version_only(self):
        r = render_mail(MailStep(0, "intro", "S", "", "Body"), CONTACT, TRAILER)
        self.assertIsNone(r.html_body)
        self.assertEqual(r.text_body, "Body\n\n---\nHi Ann, trailer text")

    def test_disabled_or_missing_trailer_changes_nothing(self):
        step = MailStep(0, "intro", "S", "<p>Body</p>", "")
        plain = render_mail(step, CONTACT)
        self.assertEqual(render_mail(step, CONTACT, {**TRAILER, "enabled": False}), plain)
        self.assertEqual(render_mail(step, CONTACT, None), plain)


if __name__ == "__main__":
    unittest.main()
