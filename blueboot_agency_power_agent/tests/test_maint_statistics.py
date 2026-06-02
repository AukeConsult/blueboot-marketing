"""Dry-run test for maint_statistics — reads leads, skips all writes."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from maint_statistics import main
# --no-excel: skip Excel output
# --no-writeback: skip writing back to leads
# --no-overview: skip overview (fastest)
# --only priority: run just one aggregation
sys.argv = ['maint_statistics.py', '--no-excel', '--no-writeback', '--no-overview', '--only', 'priority']
main()
