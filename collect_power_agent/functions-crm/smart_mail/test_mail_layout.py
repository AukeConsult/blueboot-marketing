import unittest

from smart_mail import mail_layout as ml
from smart_mail.outreach_render_mail import render_mail, MailStep

CONTACT = {"name": "Ann", "company": "Acme", "email": "ann@acme.no"}
WHO = dict(sender_name="Leif", sender_email="leif@firm.no")
URL = "https://x.test/crmApi/unsubscribe?t=a&b=c"


def settings(**kw):
    return ml.normalize(kw)


def with_part(kind, **part):
    base = ml.normalize({})["layout"]
    base[kind].update(part)
    return {"layout": base}


class Defaults(unittest.TestCase):
    def test_nothing_saved_gives_the_defaults(self):
        s = ml.normalize({})
        self.assertEqual(s["layout"], ml.DEFAULT_LAYOUT)

    def test_old_unsubscribe_footers_are_the_starting_unsubscribe_block(self):
        s = ml.normalize({}, {"html": '<p><a href="{url}">Stop</a></p>', "text": "Stop: {url}"})
        self.assertEqual(s["layout"]["text"]["unsubscribe"], "Stop: {{unsubscribe_url}}")
        self.assertIn('href="{{unsubscribe_url}}"', s["layout"]["html"]["unsubscribe"])
        # a saved unsubscribe block wins over the old footer
        saved = ml.normalize({"layout": {"text": {"unsubscribe": "Mine {{unsubscribe_url}}"}}}, {"text": "Old {url}"})
        self.assertEqual(saved["layout"]["text"]["unsubscribe"], "Mine {{unsubscribe_url}}")


class Compose(unittest.TestCase):
    def test_plain_default_has_company_address_and_unsubscribe(self):
        s = settings(company_name="Firm AS", company_address="Storgata 1\n0155 Oslo")
        out = ml.compose(s, "text", "Hi Ann", unsubscribe_url=URL, **WHO)
        self.assertEqual(out, "Hi Ann\n\n--\nFirm AS\nStorgata 1\n0155 Oslo\nleif@firm.no\n\n"
                              f"Do not want mail from us? Unsubscribe:\n{URL}")

    def test_empty_address_leaves_its_line_out_and_company_falls_back_to_sender(self):
        out = ml.compose(settings(), "text", "Hi", **WHO)
        self.assertEqual(out, "Hi\n\n--\nLeif\nleif@firm.no")

    def test_no_link_no_unsubscribe_block(self):
        self.assertNotIn("Unsubscribe", ml.compose(settings(), "text", "Hi", **WHO))
        self.assertNotIn("Unsubscribe", ml.compose(settings(), "html", "<p>Hi</p>", **WHO))

    def test_html_values_are_escaped_and_address_lines_become_breaks(self):
        s = settings(company_name="R&D <AS>", company_address="A 1\nB 2")
        out = ml.compose(s, "html", "<p>Hi</p>", unsubscribe_url=URL, **WHO)
        self.assertIn("R&amp;D &lt;AS&gt;", out)
        self.assertIn("A 1<br>B 2", out)
        self.assertIn('href="https://x.test/crmApi/unsubscribe?t=a&amp;b=c"', out)
        self.assertTrue(out.index("<p>Hi</p>") < out.index("R&amp;D") < out.index("Unsubscribe"))

    def test_frame_decides_where_the_parts_go(self):
        s = with_part("text", frame="==\n{{unsubscribe}}\n{{footer}}\n{{message}}\n==", footer="Bye {{sender_name}}", unsubscribe="Out {{unsubscribe_url}}")
        self.assertEqual(ml.compose(s, "text", "Hi", unsubscribe_url="U", **WHO), "==\nOut U\nBye Leif\nHi\n==")
        self.assertEqual(ml.compose(s, "text", "Hi", **WHO), "==\nBye Leif\nHi\n==")

    def test_a_frame_without_unsubscribe_keeps_the_link(self):
        s = with_part("text", frame="{{message}}", footer="", unsubscribe="{{unsubscribe_url}}")
        self.assertEqual(ml.compose(s, "text", "Hi", unsubscribe_url="U", **WHO), "Hi\n\nU")

    def test_message_is_never_taken_for_a_placeholder(self):
        s = with_part("text", frame="[{{message}}]", footer="F")
        self.assertEqual(ml.compose(s, "text", "see {{footer}}", **WHO), "[see {{footer}}]")

    def test_contact_placeholders_work_in_footer_and_unsubscribe(self):
        s = with_part("text", footer="To {{name}} at {{company}}")
        self.assertIn("To Ann at Acme", ml.compose(s, "text", "Hi", contact=CONTACT, **WHO))

    def test_html_mail_gets_layout_on_both_versions(self):
        text, html = ml.compose_mail(settings(), "Hi", "<p>Hi</p>", unsubscribe_url=URL, **WHO)
        self.assertIn("--\nLeif", text)
        self.assertIn("<p>Hi</p>", html)
        self.assertIsNone(ml.compose_mail(settings(), "Hi", None, **WHO)[1])


class Validate(unittest.TestCase):
    def test_frame_must_contain_message(self):
        with self.assertRaises(ValueError):
            ml.validate({"layout": {"html": {"frame": "<div>no message here</div>"}}})
        with self.assertRaises(ValueError):
            ml.validate({"layout": {"text": {"frame": "{{footer}}"}}})

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            ml.validate({"company_name": "x" * 101})
        with self.assertRaises(ValueError):
            ml.validate({"layout": {"text": {"footer": "x" * 1001}}})

    def test_clean_html_removes_scripts_and_handlers(self):
        dirty = '<p onclick="x()">a</p><script>alert(1)</script><a href="javascript:evil()">l</a>'
        out = ml.clean_html(dirty)
        for bad in ("script", "onclick", "javascript"):
            self.assertNotIn(bad, out)
        self.assertIn("<p>a</p>", out)

    def test_html_is_cleaned_and_missing_parts_keep_the_default(self):
        doc = ml.validate({"layout": {"html": {"footer": '<b onclick="x()">F</b><script>1</script>'}}})
        self.assertNotIn("script", doc["layout"]["html"]["footer"])
        self.assertNotIn("onclick", doc["layout"]["html"]["footer"])
        self.assertEqual(doc["layout"]["text"], ml.DEFAULT_LAYOUT["text"])


class RenderMailIntegration(unittest.TestCase):
    STEP = MailStep(0, "intro", "S", "<p>Body {{name}}</p>", "")

    def test_no_layout_given_changes_nothing(self):
        r = render_mail(self.STEP, CONTACT, layout=None)
        self.assertEqual(r.html_body, "<p>Body Ann</p>")

    def test_the_default_layout_is_used_even_when_nothing_is_saved(self):
        r = render_mail(self.STEP, CONTACT, layout=ml.normalize({}), sender={"name": "L", "email": "l@f.no"})
        self.assertIn("l@f.no", r.html_body)

    def test_layout_on_wraps_both_versions_and_replaces_the_old_footer(self):
        s = settings(company_name="Firm AS")
        r = render_mail(self.STEP, CONTACT, ("<p>OLD</p>", "OLD"), layout=s,
                        sender={"name": "Leif", "email": "leif@firm.no"}, unsubscribe_url=URL)
        self.assertIn("<p>Body Ann</p>", r.html_body)
        self.assertIn("Firm AS", r.html_body)
        self.assertIn("Unsubscribe", r.html_body)
        self.assertNotIn("OLD", r.html_body + r.text_body)
        self.assertIn("Firm AS", r.text_body)
        self.assertNotIn("<", r.text_body)

    def test_plain_step_goes_out_as_html_too_and_both_get_the_layout(self):
        r = render_mail(MailStep(0, "intro", "S", "", "Body {{name}}\n\nSee https://x.test/a?b=1&c=2."), CONTACT,
                        layout=settings(), sender={"name": "Leif", "email": "leif@firm.no"})
        self.assertIn("<p>Body Ann</p>", r.html_body)
        self.assertIn('<a href="https://x.test/a?b=1&amp;c=2">', r.html_body)
        self.assertIn("leif@firm.no", r.html_body)
        self.assertIn("\n--\nLeif", r.text_body)


if __name__ == "__main__":
    unittest.main()
