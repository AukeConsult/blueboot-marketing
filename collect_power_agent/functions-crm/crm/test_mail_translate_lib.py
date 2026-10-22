"""Tests for the pure mail translation guard rails, using a fake AI client."""
import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crm.mail_translate_lib import translate_mail, language_name


class FakeClient:
    """Answers with whatever `reply(user_payload)` returns; records what it was sent."""
    def __init__(self, reply):
        self.reply, self.sent = reply, []
        self.chat = self; self.completions = self
    def create(self, **kw):
        payload = json.loads(kw["messages"][1]["content"])
        self.sent.append(payload)
        content = json.dumps(self.reply(payload))
        msg = type("M", (), {"content": content})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


HTML = '<p>Hi {{name}},</p><p>See <a href="https://blueboot.ai/x?ref=A">our site</a> or mail info@blueboot.ai</p>'


class TranslateTests(unittest.TestCase):
    def test_good_translation_is_returned(self):
        c = FakeClient(lambda p: {"subject": "Hej", "body": p["body"].replace("Hi", "Hej").replace("See", "Se")})
        out = translate_mail("Hello", HTML, "Danish", client=c)
        self.assertEqual(out["subject"], "Hej")
        self.assertIn("Hej {{name}}", out["body"])

    def test_changed_placeholder_is_refused(self):
        c = FakeClient(lambda p: {"subject": "Hej", "body": p["body"].replace("{{name}}", "{{navn}}")})
        with self.assertRaises(ValueError):
            translate_mail("Hello", HTML, "Danish", client=c)

    def test_changed_link_or_tag_is_refused(self):
        bad_url = FakeClient(lambda p: {"subject": "Hej", "body": p["body"].replace("ref=A", "ref=B")})
        with self.assertRaises(ValueError):
            translate_mail("Hello", HTML, "Danish", client=bad_url)
        bad_tag = FakeClient(lambda p: {"subject": "Hej", "body": p["body"].replace("</a>", "")})
        with self.assertRaises(ValueError):
            translate_mail("Hello", HTML, "Danish", client=bad_tag)

    def test_embedded_image_never_goes_to_ai_and_comes_back(self):
        body = '<p>Hi</p><img src="data:image/png;base64,AAAA==">'
        c = FakeClient(lambda p: {"subject": "", "body": p["body"].replace("Hi", "Hej")})
        out = translate_mail("", body, "Danish", client=c)
        self.assertNotIn("base64", c.sent[0]["body"])
        self.assertIn("data:image/png;base64,AAAA==", out["body"])

    def test_missing_language_or_empty_mail(self):
        c = FakeClient(lambda p: p)
        with self.assertRaises(ValueError):
            translate_mail("Hello", "x", " ", client=c)
        with self.assertRaises(ValueError):
            translate_mail("", " ", "Danish", client=c)

    def test_language_code_becomes_name_for_the_ai(self):
        self.assertIn("Danish", language_name("da"))
        self.assertIn("Danish", language_name("DA"))
        self.assertIn("xx", language_name("xx"))     # unknown code is passed on as it is


class SeparateVersions(unittest.TestCase):
    TEXT = "Hi {{name}},\n\nSee https://blueboot.ai/x?ref=A or mail info@blueboot.ai"

    def _tr(self, p):
        out = {"subject": "Hej"}
        if "body" in p:
            out["body"] = p["body"].replace("Hi", "Hej HTML")
        if "text" in p:
            out["text"] = p["text"].replace("Hi", "Hej TEXT")
        return out

    def test_html_and_text_translated_separately_into_their_own_fields(self):
        c = FakeClient(self._tr)
        out = translate_mail("Hello", HTML, "Danish", client=c, text=self.TEXT)
        self.assertEqual(c.sent[0]["text"], self.TEXT)
        self.assertIn("Hej HTML", out["body"])
        self.assertNotIn("Hej TEXT", out["body"])
        self.assertIn("Hej TEXT", out["text"])
        self.assertNotIn("<p>", out["text"])

    def test_empty_plain_text_is_not_translated(self):
        c = FakeClient(self._tr)
        out = translate_mail("Hello", HTML, "Danish", client=c, text="  ")
        self.assertNotIn("text", c.sent[0])
        self.assertNotIn("text", out)

    def test_empty_html_is_not_translated_only_the_text(self):
        c = FakeClient(self._tr)
        out = translate_mail("Hello", "", "Danish", client=c, text=self.TEXT)
        self.assertNotIn("body", c.sent[0])
        self.assertEqual(out["body"], "")
        self.assertIn("Hej TEXT", out["text"])

    def test_html_leaking_into_text_is_refused(self):
        c = FakeClient(lambda p: {"subject": "Hej", "body": p["body"], "text": "<p>Hej</p>"})
        with self.assertRaises(ValueError):
            translate_mail("Hello", HTML, "Danish", client=c, text="Hi there")

    def test_text_placeholder_must_survive(self):
        c = FakeClient(lambda p: {"subject": "Hej", "body": p["body"], "text": p["text"].replace("{{name}}", "{{navn}}")})
        with self.assertRaises(ValueError):
            translate_mail("Hello", HTML, "Danish", client=c, text=self.TEXT)


if __name__ == "__main__":
    unittest.main()
