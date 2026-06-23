'use strict';
// crm-defs.js — shared CRM follow-up definitions.
// Single source of truth for follow-up statuses, importance levels, and contact
// channels. Load this BEFORE any page script that references these constants
// (crm-follow-*.js, crm-contact.js).

// Follow-up status options (value + label).
const FU_STATUSES = [
  { value: '',               label: '— none —' },
  { value: 'in_work',        label: 'In-work' },
  { value: 'contacted',      label: 'Contacted' },
  { value: 'received',       label: 'Received' },
  { value: 'replied',        label: 'Replied' },
  { value: 'meeting',        label: 'Meeting' },
  { value: 'offer',          label: 'Offer' },
  { value: 'not_interested', label: 'Not-interested' },
];

// Importance levels (value + label + css class).
const IMPORTANCE_LEVELS = [
  { value: '',       label: '— —',    cls: 'imp-none'   },
  { value: 'low',    label: 'Low',    cls: 'imp-low'    },
  { value: 'medium', label: 'Medium', cls: 'imp-medium' },
  { value: 'high',   label: 'High',   cls: 'imp-high'   },
];

// Contact channels (field name + icon + placeholder). googlechat carries extra
// button metadata used by the follow-up side panel.
const CHANNELS = [
  { field: 'linkedin',   icon: 'ti-brand-linkedin',   ph: 'LinkedIn URL or username' },
  { field: 'twitter',    icon: 'ti-brand-twitter',    ph: 'Twitter / X handle or URL' },
  { field: 'facebook',   icon: 'ti-brand-facebook',   ph: 'Facebook profile URL' },
  { field: 'instagram',  icon: 'ti-brand-instagram',  ph: 'Instagram handle or URL' },
  { field: 'whatsapp',   icon: 'ti-brand-whatsapp',   ph: 'WhatsApp number (with country code)' },
  { field: 'teams',      icon: 'ti-brand-teams',      ph: 'Teams email or meeting URL' },
  { field: 'telegram',   icon: 'ti-brand-telegram',   ph: 'Telegram handle or URL' },
  { field: 'googlechat', icon: 'ti-brand-google',     ph: 'Google Chat email or URL', btnIcon: 'ti-messages', btnTitle: 'Open Chat', logChat: true },
  { field: 'messenger',  icon: 'ti-brand-messenger',  ph: 'Messenger username or URL' },
];

// Per-channel href builders (value -> URL).
const _CHANNEL_HREF = {
  linkedin:   v => v.startsWith('http') ? v : 'https://linkedin.com/in/' + v,
  twitter:    v => v.startsWith('http') ? v : 'https://x.com/' + v.replace(/^@/,''),
  facebook:   v => v.startsWith('http') ? v : 'https://facebook.com/' + v,
  instagram:  v => v.startsWith('http') ? v : 'https://instagram.com/' + v.replace(/^@/,''),
  whatsapp:   v => 'https://wa.me/' + v.replace(/[^0-9]/g,''),
  teams:      v => v.includes('@') ? 'https://teams.microsoft.com/l/chat/0/0?users=' + encodeURIComponent(v) : (v.startsWith('http') ? v : '#'),
  telegram:   v => v.startsWith('http') ? v : 'https://t.me/' + v.replace(/^@/,''),
  googlechat: v => v.startsWith('http') ? v : 'https://mail.google.com/chat/u/0/#dm/' + encodeURIComponent(v),
  messenger:  v => v.startsWith('http') ? v : 'https://m.me/' + v,
};

// Convenience helper: safe href for a channel field (returns '#' when empty).
function channelHref(field, val) {
  const v = String(val || '').trim();
  if (!v) return '#';
  const fn = _CHANNEL_HREF[field];
  return fn ? fn(v) : v;
}
