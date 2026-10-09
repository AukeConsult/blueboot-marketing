"""contact_clean_lib.py -- clean scraped contact names and e-mail addresses.

    from crm.contact_clean_lib import clean_name, clean_email, deobfuscate

    clean_name("“Email Adam Kenneman.”")      -> "Adam Kenneman"
    clean_name("Mejla Anna Lindqvist")                -> "Anna Lindqvist"
    clean_name("Contact us")                          -> ""
    clean_email("mailto:Info@Firma.se?subject=Hi")    -> "info@firma.se"

Used by the campaign scraper (crm.campaign_scrape_lib), site_agent / site_enrich_agent
(app/functions/utils.py) and the clean-up command (app/maint_clean_contacts.py).

Call-to-action words ("Email", "Mejla", "Send e-mail til" ...) are listed per language in
CTA_PHRASES.  To support another language add its phrases there -- no other change needed.
Unknown languages are still handled when an e-mail address is known: the words of the
text that match the address (adam.kenneman@ -> "Adam Kenneman") are kept, the rest dropped.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Language data
# ---------------------------------------------------------------------------

# Words/phrases that tell the reader to write to someone.  Lower case.
CTA_PHRASES: dict[str, list[str]] = {
    "en": ["send an email to", "send an e-mail to", "send email to", "send e-mail to",
           "send a message to", "send mail to", "email us at", "email", "e-mail", "mailto",
           "mail", "write to", "contact", "contact us", "get in touch with", "message",
           "reach out to", "reach"],
    "sv": ["skicka e-post till", "skicka epost till", "skicka mail till", "skicka e-brev till",
           "skicka ett mejl till", "mejla", "maila", "e-post", "epost", "e-posta", "mejl",
           "skriv till", "kontakta"],
    "da": ["send en e-mail til", "send e-mail til", "send email til", "send mail til",
           "e-mail til", "mail til", "skriv til", "kontakt"],
    "no": ["send en e-post til", "send e-post til", "send epost til", "send en epost til",
           "e-post til", "epost til", "skriv til", "kontakt", "kontakt oss"],
    "de": ["senden sie eine e-mail an", "senden sie eine email an", "e-mail senden an",
           "e-mail an", "email an", "mail an", "mailen an", "schreiben sie an",
           "schreiben an", "schreibe an", "kontaktieren sie", "kontaktiere", "kontakt"],
    "fr": ["envoyer un e-mail à", "envoyer un email à", "envoyer un courriel à",
           "envoyer un mail à", "écrire à", "ecrire à", "écrivez à", "contactez", "contacter",
           "courriel à", "e-mail à", "email à", "mail à", "courriel"],
    "es": ["enviar un correo electrónico a", "enviar un correo a", "enviar correo a",
           "enviar un email a", "enviar email a", "escribir a", "escribe a", "contactar con",
           "contactar a", "contactar", "correo electrónico a", "correo a", "correo",
           "email a", "e-mail a"],
    "it": ["invia una email a", "invia una e-mail a", "invia email a", "invia una mail a",
           "invia un'email a", "scrivere a", "scrivi a", "contattare", "contatta",
           "email a", "e-mail a", "mail a"],
    "nl": ["stuur een e-mail naar", "stuur een mail naar", "e-mail naar", "email naar",
           "mail naar", "mailen naar", "schrijf naar", "contacteer", "neem contact op met"],
    "fi": ["lähetä sähköpostia", "lähetä sähköposti", "lähetä viesti", "sähköpostia",
           "sähköposti", "ota yhteyttä"],
    "pt": ["enviar e-mail para", "enviar email para", "escrever para", "e-mail para",
           "email para", "contactar", "contatar"],
    "pl": ["wyślij e-mail do", "wyślij email do", "wyślij mail do", "napisz e-mail do",
           "napisz do", "e-mail do", "email do", "skontaktuj się z"],
}

# Titles / honorifics removed from the front of a name.
HONORIFICS = {
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "madam",
    "herr", "frau", "hr", "fru", "fröken", "fr", "monsieur", "madame", "mme", "mlle",
    "señor", "senor", "señora", "sr", "sra", "signor", "signora", "sig", "dott", "dottore",
    "dottoressa", "ing", "dipl", "mag", "mgr", "meneer", "mevrouw", "dhr", "mw", "pan", "pani",
}
POST_NOMINALS = {"phd", "md", "mba", "msc", "bsc", "ma", "ba", "cpa", "dds", "esq", "jr", "sr.", "ii", "iii"}

# Words that are never a person's name on their own (labels, buttons, roles).
GENERIC_WORDS = {
    "contact", "contacts", "us", "kontakt", "kontakta", "oss", "os", "uns", "nous", "nos",
    "click", "here", "read", "more", "info", "information", "team", "staff", "support",
    "sales", "admin", "office", "email", "e-mail", "mail", "mejla", "mejl", "e-post",
    "epost", "address", "adresse", "adress", "send", "write", "us.", "our", "the", "your",
    "klicka", "klik", "hier", "ici", "aqui", "aquí", "qui", "link", "website", "webmaster",
    "enquiries", "enquiry", "inquiries", "reception", "service", "customer", "kundeservice",
    "kundservice", "post", "postmaster", "hello", "hi", "hej", "hallo", "moi", "hola", "ciao",
    "name", "navn", "namn", "nombre", "nom", "naam", "nome", "imię", "nimi", "phone", "tel",
    "telefon", "tlf", "fax", "mobile", "mobil", "linkedin", "twitter", "facebook",
    "instagram", "undefined", "null", "none", "n/a", "unknown",
}
# Name particles that stay lower case inside a name.
PARTICLES = {"van", "von", "de", "der", "den", "ter", "ten", "af", "av", "da", "di", "du",
             "del", "della", "dos", "das", "la", "le", "bin", "al", "el", "zu", "y", "e", "of"}

# E-mail rules
ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "tif", "tiff", "heic",
              "heif", "jfif", "apng", "ico", "cur", "css", "scss", "less", "js", "mjs", "ts", "map",
              "woff", "woff2", "ttf", "otf", "eot", "pdf", "doc", "docx", "xls", "xlsx", "ppt",
              "pptx", "csv", "txt", "mp4", "m4v", "webm", "mov", "avi", "mkv", "mp3", "wav", "ogg",
              "zip", "gz", "rar", "json", "xml", "html", "htm", "php", "asp", "aspx", "wasm"}
# "logo@2x.png"-style retina / hashed asset names: a domain label like 2x, 3x or a build hash
_ASSET_LABEL_RE = re.compile(r"^(?:[1-4]x|[0-9a-f]{8,})$")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com",
                       "yourdomain.com", "yoursite.com", "yourcompany.com", "company.com",
                       "test.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
                       "mysite.com", "website.com", "site.com", "domene.no", "domain.se"}
PLACEHOLDER_LOCALS = {"your", "you", "yourname", "your.name", "name", "email", "user",
                      "username", "someone", "example", "test", "xxx", "navn", "nombre"}

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­‎‏"), None)
_QUOTES = "\"'`´‘’‚‛“”„‟«»‹›"
_EDGE = " \t\r\n" + _QUOTES + ".,;:!?()[]{}<>|/\\*•·–—-_=+~#"
_WORD_RE = re.compile(r"^[^\W\d_]+(?:['’.\-][^\W\d_]+)*\.?$", re.UNICODE)
_LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.\-]+$")
_LABEL_RE = re.compile(r"^(?:e-?mail|mail|name|navn|namn|kontakt|contact)\s*[:\-]\s*", re.I)


def _norm_text(s) -> str:
    if not isinstance(s, str):
        return ""
    s = _html.unescape(s)
    s = unicodedata.normalize("NFC", s).translate(_ZERO_WIDTH)
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.casefold())
                   if unicodedata.category(c) != "Mn")


def _phrase_regex(phrases) -> re.Pattern:
    alts = "|".join(re.escape(p).replace(r"\ ", r"\s+")
                    for p in sorted(set(phrases), key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)", re.I)


_ALL_CTA = [p for plist in CTA_PHRASES.values() for p in plist]
_CTA_LEAD = re.compile(r"^(?:" + _phrase_regex(_ALL_CTA).pattern + r")[\s:,\-–—]*", re.I)
_CTA_TAIL_WORDS = ["email", "e-mail", "mail", "e-post", "epost", "sähköposti", "courriel",
                   "correo", "contact", "kontakt", "mejla", "maila", "mejl"]
_CTA_TAIL = re.compile(r"[\s,:\-–—(\[]*(?:" + "|".join(map(re.escape, _CTA_TAIL_WORDS)) +
                       r")[\s)\]]*$", re.I)


def _strip_edges(s: str) -> str:
    return s.strip(_EDGE)


def _title_case(name: str) -> str:
    """Fix ALL CAPS / all lower case only; mixed-case names are left alone."""
    if not (name.isupper() or name.islower()):
        return name
    out = []
    for i, w in enumerate(name.split()):
        lw = w.lower()
        if i > 0 and lw in PARTICLES:
            out.append(lw)
            continue
        parts = re.split(r"([\-'’])", lw)
        w2 = "".join(p[:1].upper() + p[1:] if p not in ("-", "'", "’") else p for p in parts)
        if re.match(r"^Mc[a-z]", w2):
            w2 = "Mc" + w2[2].upper() + w2[3:]
        out.append(w2)
    return " ".join(out)


def _align_with_email(words: list[str], email: str) -> list[str]:
    """Keep only the span of words that match the e-mail local part."""
    local = email.split("@", 1)[0].lower()
    tokens = [t for t in re.split(r"[._\-+0-9]+", _fold(local)) if len(t) >= 2]
    if len(tokens) < 2:
        return words
    hits = [i for i, w in enumerate(words)
            if any(_fold(w).strip(".'") == t or (len(t) >= 3 and _fold(w).startswith(t))
                   for t in tokens)]
    if len(hits) >= 2:
        return words[hits[0]:hits[-1] + 1]
    return words


# ---------------------------------------------------------------------------
# Public: names
# ---------------------------------------------------------------------------

def clean_name(raw, email: str = "") -> str:
    """Return the person's name, or "" when the text is not a name."""
    s = _norm_text(raw)
    if not s:
        return ""
    s = re.sub(r"^mailto:", "", s, flags=re.I)
    if "@" in s or re.search(r"https?://|www\.", s, re.I) or re.search(r"u00[0-9a-f]{2}", s):
        return ""
    s = s.replace("’", "'")

    for _ in range(4):                       # peel quotes / labels / CTA words until stable
        before = s
        s = _strip_edges(s)
        s = _LABEL_RE.sub("", s)
        s = _CTA_LEAD.sub("", s)
        s = _CTA_TAIL.sub("", s)
        if s == before:
            break
    s = _strip_edges(s)
    s = re.sub(r",\s*(?:%s)\.?$" % "|".join(sorted(POST_NOMINALS)), "", s, flags=re.I)

    words = s.split()
    while words and _fold(words[0]).strip(".") in HONORIFICS and len(words) > 1:
        words = words[1:]
    while words and _fold(words[-1]).strip(".,") in POST_NOMINALS and len(words) > 1:
        words = words[:-1]

    if email:
        words = _align_with_email(words, email)

    words = [w.strip(_EDGE.replace("'", "").replace(".", "")) for w in words]
    words = [w for w in words if w]
    if not words or len(words) > 5:
        return ""
    if any(not _WORD_RE.match(w) for w in words):
        return ""
    if all(_fold(w).strip(".") in GENERIC_WORDS for w in words):
        return ""
    if len(words) == 1 and len(words[0].strip(".")) < 2:
        return ""
    return _title_case(" ".join(words))


def name_fits_email(name: str, email: str) -> bool:
    """True when the e-mail local part plausibly belongs to this person:
    initials (bs@ = Brian Stein, mht@ = Mark Thompson), first name, last name, first.last,
    flast ..."""
    words = [_fold(w).strip(".'") for w in name.split() if w.strip(".'")]
    if len(words) < 2:
        return False
    fold2 = lambda t: t.replace("ø", "o").replace("æ", "ae").replace("å", "a")
    words = [fold2(w) for w in words]
    local = fold2(_fold(email.split("@", 1)[0]))
    l2 = re.sub(r"[^a-z]", "", local)
    if not l2:
        return False
    first, last = words[0], words[-1]
    if 2 <= len(l2) <= 4 and l2[0] == first[0] and l2[-1] == last[0]:
        return True                                           # initials
    if len(first) >= 3 and (l2 == first or l2.startswith(first) or first in l2):
        return True
    if len(last) >= 3 and (l2 == last or l2.endswith(last) or last in l2):
        return True
    return l2 in (first[0] + last, first + last[0])


_BLOCK_BREAK = re.compile(r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|section|article|header|footer|"
                          r"h[1-6]|figure|figcaption|address|blockquote|span)\b[^>]*>|<img\b[^>]*>",
                          re.I)


def names_from_page_blocks(html: str, emails) -> dict:
    """{email: name} for e-mails that sit in plain text under/next to a person's name
    (team sections: <h3>Brian Stein</h3> CEO &lt;bs@firm.dk&gt;).  A name is accepted only
    when the e-mail local part fits it (see name_fits_email)."""
    if not html or not emails:
        return {}
    t = re.sub(r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    t = _BLOCK_BREAK.sub("\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    lines = [_norm_text(l) for l in t.split("\n")]
    lines = [l for l in lines if l]
    out: dict = {}
    for em in emails:
        for i, line in enumerate(lines):
            if em not in line.lower():
                continue
            same = line.lower().split(em)[0]
            cands = [same] + [lines[j] for j in range(i - 1, max(-1, i - 5), -1)]
            for c in cands:
                nm = clean_name(c, em)
                if nm and len(nm.split()) >= 2 and name_fits_email(nm, em):
                    out[em] = nm
                    break
            if em in out:
                break
    return out


# ---------------------------------------------------------------------------
# Public: e-mail
# ---------------------------------------------------------------------------

_OBFUSCATION = [
    (re.compile(r"\s*[\[\(\{]\s*(?:at|@|snabel-a|snabela|ät|arroba)\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*(?:dot|punkt|punto|point|prikk|punktum)\s*[\]\)\}]\s*", re.I), "."),
    (re.compile(r"&#0*64;|&commat;|&#x0*40;", re.I), "@"),
    (re.compile(r"&#0*46;|&#x0*2e;", re.I), "."),
]


def deobfuscate(text: str) -> str:
    """name [at] firma [dot] se  ->  name@firma.se (safe, bracketed forms only)."""
    if not text:
        return text
    for rx, rep in _OBFUSCATION:
        text = rx.sub(rep, text)
    return text


def clean_email(raw) -> str:
    """THE e-mail check used by every routine (scrape, imports, sync, exports, maintenance).

    Returns a normalised, plausible e-mail address, or "" if it is not a proper address.
    A contact whose email gives "" must not be created (and is deleted by Update info)."""
    if not isinstance(raw, str):
        return ""
    s = _norm_text(unquote(raw))
    s = re.sub(r"^mailto:", "", s, flags=re.I)
    s = re.split(r"[?#]", s, maxsplit=1)[0]
    toks = [t for t in re.split(r"[\s,;:<>()\[\]\"']+", s) if re.search(r"\w@\w", t)]
    if len(toks) == 1:                       # "Email adam@x.com." -> the token with the @
        s = toks[0]
    s = s.replace(" ", "").strip(_EDGE.replace("_", "").replace("-", "").replace("+", "")
                                 .replace("=", "").replace("~", "").replace("#", "")
                                 + "‑")
    s = s.lower()
    if s.count("@") != 1:
        return ""
    local, domain = s.split("@")
    m = re.match(r"^(\d{6,})([a-z][a-z0-9._\-]*)$", local)       # phone digits glued on
    if m:
        local = m.group(2)
    domain = domain.strip(".-")
    if not local or not domain or len(local) > 64 or len(domain) > 253:
        return ""
    if local.startswith(".") or local.endswith(".") or ".." in local or ".." in domain:
        return ""
    if not _LOCAL_RE.match(local):
        return ""
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    labels = domain.split(".")
    if any(_ASSET_LABEL_RE.fullmatch(l) for l in labels[:-1]):      # logo@2x.126ba3da.avif
        return ""
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9\-]{1,63}", l) or l.startswith("-")
                               or l.endswith("-") for l in labels):
        return ""
    tld = labels[-1]
    if tld in ASSET_TLDS or tld.isdigit() or not (tld.startswith("xn--") or
                                                  re.fullmatch(r"[a-z]{2,24}", tld)):
        return ""
    if domain in PLACEHOLDER_DOMAINS or any(domain.endswith("." + d) for d in
                                            ("wixpress.com", "sentry.io")):
        return ""
    if local in PLACEHOLDER_LOCALS and domain.split(".")[0] in ("domain", "email", "example",
                                                                  "yourdomain", "company"):
        return ""
    if len(local) >= 16 and re.fullmatch(r"[0-9a-f\-]+", local):
        return ""
    if re.search(r"u00[0-9a-f]{2}", local):
        return ""
    return f"{local}@{domain}"


# ---------------------------------------------------------------------------
# THE e-mail detector -- the only place in the system that finds addresses in text
# ---------------------------------------------------------------------------

_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def find_emails(*texts) -> list:
    """Every proper e-mail address in the given html / text pieces, normalised, de-duplicated,
    in order of appearance.  Decodes \\uXXXX escapes and HTML entities, undoes
    "name [at] firma [dot] se" obfuscation, and drops anything clean_email() rejects
    (asset names, placeholders, hashes ...).  Scrapers, imports and the reply matcher all use this."""
    out: list = []
    seen: set = set()
    for t in texts:
        if not isinstance(t, str) or "@" not in t and "at" not in t.lower():
            continue
        t = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), t)
        t = deobfuscate(_html.unescape(t))
        for m in _FIND_RE.finditer(t):
            e = clean_email(m.group(0))
            if e and e not in seen:
                seen.add(e)
                out.append(e)
    return out
