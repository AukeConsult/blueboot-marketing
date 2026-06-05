"""Shared sheet IDs and tab names. Overridable via env vars (defaults preserved)."""
import os

CONTACT_SHEET_ID  = os.getenv("CONTACT_SHEET_ID", "1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA")
TEMPLATE_SHEET_ID = os.getenv("TEMPLATE_SHEET_ID", "1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY")
CONTACT_TAB       = os.getenv("CONTACT_TAB", "contacts")
TEMPLATE_TAB      = os.getenv("TEMPLATE_TAB", "Outreach")

CRM_COLLECTION    = os.getenv("CRM_COLLECTION", "crm")
CRM_CONTACT_DOC   = os.getenv("CRM_CONTACT_DOC", "contact_select")
CRM_TEMPLATE_DOC  = os.getenv("CRM_TEMPLATE_DOC", "crm_template")
