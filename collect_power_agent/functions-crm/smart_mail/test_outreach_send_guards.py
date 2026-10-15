"""Guards around sending: broken mails are not sent, recording a sent mail is retried."""
import sys, os, unittest
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import types
for _name in ("google", "google.cloud", "google.cloud.firestore_v1", "google.cloud.firestore_v1.base_query"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["google.cloud"].firestore = types.ModuleType("google.cloud.firestore")
sys.modules["google.cloud.firestore_v1.base_query"].FieldFilter = object
_fc = types.ModuleType("smart_mail.firestore_client"); _fc.get_firestore = lambda: None
sys.modules.setdefault("smart_mail.firestore_client", _fc)

from smart_mail import outreach_sender as osd


def _case(email="a@b.no", subject="Hi", html="<p>x</p>", text="", r_subject=None, r_text="x", r_html="<p>x</p>"):
    contact = NS(email=email)
    step = NS(body_html=html, body_text=text)
    rendered = NS(subject=subject if r_subject is None else r_subject, text_body=r_text, html_body=r_html)
    return contact, step, rendered


class TestMailProblem(unittest.TestCase):
    def test_good_mail(self):
        self.assertEqual(osd._mail_problem(*_case()), "")

    def test_bad_email(self):
        self.assertIn("email", osd._mail_problem(*_case(email="nope")))

    def test_empty_subject(self):
        self.assertIn("subject", osd._mail_problem(*_case(subject="  ")))

    def test_empty_body(self):
        self.assertIn("body", osd._mail_problem(*_case(html="", text="")))

    def test_unfilled_tag(self):
        self.assertIn("{{campaign_link}}", osd._mail_problem(*_case(r_text="see {{campaign_link}}")))


class TestConfirmRetry(unittest.TestCase):
    def setUp(self):
        self._sleep, osd.time.sleep = osd.time.sleep, lambda s: None

    def tearDown(self):
        osd.time.sleep = self._sleep

    def test_retries_then_succeeds(self):
        calls = []
        def fn(**kw):
            calls.append(kw)
            if len(calls) < 3:
                raise RuntimeError("firestore down")
            return "ok"
        self.assertEqual(osd._confirm_with_retry(fn, a=1), "ok")
        self.assertEqual(len(calls), 3)

    def test_gives_up(self):
        def fn(**kw):
            raise RuntimeError("down")
        with self.assertRaises(RuntimeError):
            osd._confirm_with_retry(fn)


if __name__ == "__main__":
    unittest.main()
