"""Dry-run test for maint_firestore_snapshot — searches for 'wordpress', limit 1."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from maint_firestore_snapshot import main
sys.argv = ['maint_firestore_snapshot.py', 'wordpress', '--limit', '1']
main()
