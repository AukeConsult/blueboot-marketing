"""An HTML mail with its own plain-text version sends that text; without one the text is derived."""
import sys, os, types, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smart_mail.outreach_render_mail import render_mail, MailStep

CONTACT = {"email": "a@b.no", "name": "Anna"}


def _step(html="", text=""):
    return MailStep(index=0, mail_type="intro", subject="Hi", body_html=html, body_text=text)


class Render(unittest.TestCase):
    def test_own_text_version_is_used(self):
        r = render_mail(_step("<p>HTML {{name}}</p>", "Own text {{name}}"), CONTACT)
        self.assertIn("<p>HTML Anna</p>", r.html_body)
        self.assertEqual(r.text_body, "Own text Anna")

    def test_text_derived_from_html_when_no_text_version(self):
        r = render_mail(_step("<p>HTML Anna</p>", ""), CONTACT)
        self.assertEqual(r.text_body, "HTML Anna")

    def test_plain_only_mail(self):
        r = render_mail(_step("", "Just text"), CONTACT)
        self.assertIsNone(r.html_body)
        self.assertEqual(r.text_body, "Just text")


if __name__ == "__main__":
    unittest.main()
