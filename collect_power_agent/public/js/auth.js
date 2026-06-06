/* auth.js -- Firebase Authentication for Blueboot CRM
 *
 * SETUP (one-time):
 *   1. Go to Firebase Console → Project Settings → Your apps → Add web app
 *      (or use existing). Copy the firebaseConfig object into FIREBASE_CONFIG below.
 *   2. Enable Authentication in the Firebase Console:
 *      Authentication → Sign-in method → enable Google and/or Email/Password.
 *   3. Add your Firebase Hosting domain to the Authorised domains list
 *      (it's added automatically for *.firebaseapp.com and *.web.app).
 *
 * Usage (every protected page already gets this via crm-common.js):
 *   requireAuth()          -- redirects to login.html if not signed in
 *   signOutUser()          -- signs out and redirects to login.html
 *   getAuthToken()         -- returns Promise<string|null> (ID token or null)
 *   window._authUser       -- current Firebase User object once resolved
 */

// ---------------------------------------------------------------------------
// Config is loaded from public/js/firebase-config.js (gitignored).
// See firebase-config.example.js for the template.
// ---------------------------------------------------------------------------
if (typeof window.FIREBASE_CONFIG === 'undefined') {
  console.error('[auth.js] window.FIREBASE_CONFIG is not defined. '
    + 'Copy public/js/firebase-config.example.js → firebase-config.js and fill in your values.');
}

// ---------------------------------------------------------------------------
// Init (idempotent — safe to load on multiple pages)
// ---------------------------------------------------------------------------
if (!firebase.apps.length) {
  firebase.initializeApp(window.FIREBASE_CONFIG || {});
}
const _auth = firebase.auth();

// Expose the current user globally so nav / pages can read it.
window._authUser = null;

// ---------------------------------------------------------------------------
// requireAuth()
//   Call on page load. Waits for Firebase to resolve the auth state, then:
//     • signed in  → sets window._authUser, resolves promise with user
//     • signed out → redirects to login.html?next=<current page>
//
//   Returns a Promise<FirebaseUser> so pages can await it if they want
//   to use the user object (e.g. show the email, get an ID token).
// ---------------------------------------------------------------------------
function requireAuth() {
  return new Promise(resolve => {
    _auth.onAuthStateChanged(user => {
      if (user) {
        window._authUser = user;
        resolve(user);
      } else {
        const next = encodeURIComponent(location.pathname.split('/').pop() + location.search);
        location.replace('login.html?next=' + next);
      }
    });
  });
}

// ---------------------------------------------------------------------------
// signOutUser()  -- signs out and returns to login.html
// ---------------------------------------------------------------------------
async function signOutUser() {
  await _auth.signOut();
  location.href = 'login.html';
}

// ---------------------------------------------------------------------------
// getAuthToken()  -- returns the current user's Firebase ID token, or null
//   Use this to attach an Authorization header to API calls:
//     const token = await getAuthToken();
//     fetch(url, { headers: { Authorization: 'Bearer ' + token } });
// ---------------------------------------------------------------------------
async function getAuthToken() {
  const user = _auth.currentUser || window._authUser;
  if (!user) return null;
  return user.getIdToken();
}
