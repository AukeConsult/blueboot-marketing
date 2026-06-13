/**
 * functions-auth/index.js
 *
 * Non-blocking Firebase Auth event triggers.
 * These run asynchronously AFTER the auth operation completes — they never
 * delay or block sign-in.
 *
 * Firestore path: settings/users/users/{normalizedEmail}
 *   Document ID  = email.toLowerCase().trim()
 *   Fields: uid · email · displayName · photoURL · providers[] · createdAt · updatedAt
 *
 * Deploy: firebase deploy --only functions:auth
 */

const functions = require('firebase-functions');
const admin     = require('firebase-admin');

if (!admin.apps.length) admin.initializeApp();

const db         = admin.firestore();
const USERS_COLL = 'settings/users/users';
const REGION     = 'us-central1';

function normalizeEmail(email) {
  return (email || '').toLowerCase().trim();
}

function buildDoc(user, { isNew = false } = {}) {
  const now = new Date().toISOString();
  return {
    uid:         user.uid,
    email:       user.email        || '',
    displayName: user.displayName  || '',
    photoURL:    user.photoURL     || '',
    providers:   (user.providerData || []).map(p => p.providerId),
    updatedAt:   now,
    ...(isNew ? { createdAt: now } : {}),
  };
}

// ── onCreate ────────────────────────────────────────────────────────────────
// Fires after a new Firebase Auth account is created (any provider).
exports.onUserCreated = functions
  .region(REGION)
  .auth.user()
  .onCreate(async (user) => {
    const key = normalizeEmail(user.email);
    if (!key) return;
    await db.doc(`${USERS_COLL}/${key}`).set(buildDoc(user, { isNew: true }), { merge: true });
  });

// ── onDelete ─────────────────────────────────────────────────────────────────
// Fires after a Firebase Auth account is deleted (console or Admin SDK).
exports.onUserDeleted = functions
  .region(REGION)
  .auth.user()
  .onDelete(async (user) => {
    const key = normalizeEmail(user.email);
    if (!key) return;
    await db.doc(`${USERS_COLL}/${key}`).delete();
  });
