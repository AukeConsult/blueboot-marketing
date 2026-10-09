"""test_contact_clean.py -- unit tests for crm.contact_clean_lib (names and e-mail).

Run:  python app/test_contact_clean.py     (or: python -m unittest app.test_contact_clean)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "functions-crm"))
from crm.contact_clean_lib import clean_email, clean_name, deobfuscate  # noqa: E402

NAMES = [
    # (raw, email, expected)
    ("“Email Adam Kenneman.”", "", "Adam Kenneman"),
    ("Email Adam Kenneman.", "adam.kenneman@x.com", "Adam Kenneman"),
    ("“Mejla”", "", ""),
    ("Mejla Anna Lindqvist", "", "Anna Lindqvist"),
    ("Maila Erik Svensson", "", "Erik Svensson"),
    ("Skicka e-post till Karin Berg", "", "Karin Berg"),
    ("Send e-mail til Lars Hansen", "", "Lars Hansen"),
    ("Send e-post til Ola Nordmann", "", "Ola Nordmann"),
    ("E-Mail an Hans Müller", "", "Hans Müller"),
    ("Schreiben Sie an Petra Schmidt", "", "Petra Schmidt"),
    ("Contactez Marie Dupont", "", "Marie Dupont"),
    ("Envoyer un e-mail à Jean Martin", "", "Jean Martin"),
    ("Escribir a José García", "", "José García"),
    ("Scrivi a Giulia Rossi", "", "Giulia Rossi"),
    ("Mail naar Jan de Vries", "", "Jan de Vries"),
    ("Lähetä sähköpostia Matti Virtanen", "", "Matti Virtanen"),
    ("Napisz do Jan Kowalski", "", "Jan Kowalski"),
    ("Enviar e-mail para Ana Silva", "", "Ana Silva"),
    ("Email: John Smith", "", "John Smith"),
    ("John Smith - email", "", "John Smith"),
    ("Dr. Sarah Connor", "", "Sarah Connor"),
    ("Herr Dr. Klaus Meyer", "", "Klaus Meyer"),
    ("Jane Doe, PhD", "", "Jane Doe"),
    ("JOHN SMITH", "", "John Smith"),
    ("anna lindqvist", "", "Anna Lindqvist"),
    ("PATRICK O'NEIL", "", "Patrick O'Neil"),
    ("Ludwig VAN BEETHOVEN", "", "Ludwig VAN BEETHOVEN"),   # mixed case is left alone
    ("Mary-Jane Watson", "", "Mary-Jane Watson"),
    ("Åse Sørensen", "", "Åse Sørensen"),
    ("J. R. Tolkien", "", "J. R. Tolkien"),
    # unknown language: fall back on the e-mail address
    ("Skrivte till Anna Lindqvist", "anna.lindqvist@firma.se", "Anna Lindqvist"),
    # not names
    ("Contact us", "", ""),
    ("Kontakta oss", "", ""),
    ("Click here", "", ""),
    ("info@firma.se", "", ""),
    ("https://firma.se/team", "", ""),
    ("Tel 070 123 45 67", "", ""),
    ("", "", ""),
    (None, "", ""),
    ("a b c d e f g", "", ""),
]

EMAILS = [
    ("mailto:Info@Firma.se?subject=Hello", "info@firma.se"),
    ("  <Anna.Lindqvist@Firma.SE>.  ", "anna.lindqvist@firma.se"),
    ("anna%40firma.se", "anna@firma.se"),
    ("070123456anna@firma.se", "anna@firma.se"),
    ("logo@2x.png", ""),
    ("icon@firma.svg", ""),
    ("your@email.com", ""),
    ("name@example.com", ""),
    ("bfb679c754744c58a7374ee6e25cfc13@sentry.wixpress.com", ""),
    ("anna@firma", ""),
    ("anna@@firma.se", ""),
    ("a..b@firma.se", ""),
    ("anna@firma.c", ""),
    ("jørgen@firma.dk", "jørgen@firma.dk".lower() if False else ""),   # non-ascii local rejected
    ("anna@müller.de", "anna@xn--mller-kva.de"),
    (None, ""),
    ("", ""),
]


class CleanName(unittest.TestCase):
    def test_names(self):
        bad = [(r, e, w, clean_name(r, e)) for r, e, w in NAMES if clean_name(r, e) != w]
        self.assertEqual(bad, [])


class CleanEmail(unittest.TestCase):
    def test_emails(self):
        bad = [(r, w, clean_email(r)) for r, w in EMAILS if clean_email(r) != w]
        self.assertEqual(bad, [])

    def test_deobfuscate(self):
        self.assertEqual(deobfuscate("anna [at] firma [dot] se"), "anna@firma.se")
        self.assertEqual(deobfuscate("anna&#64;firma.se"), "anna@firma.se")
        self.assertEqual(clean_email(deobfuscate("anna(at)firma(dot)se")), "anna@firma.se")

    def test_names_from_team_blocks(self):
        from crm.contact_clean_lib import names_from_page_blocks, name_fits_email
        html = ("<h3>Brian Stein</h3>CEO<br>&lt;bs@adtention.dk&gt;"
                "<h3>Dorte Knold</h3><p>Client Manager</p><p>&lt;dk@adtention.dk&gt;</p>"
                "<h3>Mark Thompson</h3><p>Team lead</p><p>&lt;mht@adtention.dk&gt;</p>")
        got = names_from_page_blocks(html, ["bs@adtention.dk", "dk@adtention.dk", "mht@adtention.dk"])
        self.assertEqual(got, {"bs@adtention.dk": "Brian Stein", "dk@adtention.dk": "Dorte Knold",
                               "mht@adtention.dk": "Mark Thompson"})
        self.assertFalse(name_fits_email("Client Manager", "dk@adtention.dk"))

    def test_find_emails_single_detector(self):
        from crm.contact_clean_lib import find_emails
        t = ("Email adam@x.com. logo@2x.126ba3da.avif anna [at] firma [dot] se bs&#64;adtention.dk "
             "<a href='mailto:Info@Foo.com?subject=x'>x</a> \\u003ejk@a.dk\\u003c 20km@6.7l adam@x.com")
        self.assertEqual(find_emails(t), ["adam@x.com", "anna@firma.se", "bs@adtention.dk",
                                          "info@foo.com", "jk@a.dk"])

    def test_linkedin_profiles(self):
        from crm.contact_clean_lib import find_linkedin_profiles
        html = ('<h3>Rasmus Kjærgaard Rossen</h3><h4>Ejer</h4><p>Rasmus er ejer.</p>'
                '<a href="https://www.linkedin.com/in/rasmus-kjaergaard-rossen-1a2b/"></a>'
                '<h3>Steffen Rasmussen</h3><h4>Konsulent</h4><p>Steffen sælger.</p>'
                '<a href="https://dk.linkedin.com/in/sr77x?x=1">Forbind med Steffen på LinkedIn</a>'
                '<a href="https://www.linkedin.com/company/hh">Firma</a>')
        got = [(p["name"], p["url"]) for p in find_linkedin_profiles(html)]
        self.assertEqual(got, [("Rasmus Kjærgaard Rossen", "https://www.linkedin.com/in/rasmus-kjaergaard-rossen-1a2b"),
                               ("Steffen Rasmussen", "https://www.linkedin.com/in/sr77x")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
