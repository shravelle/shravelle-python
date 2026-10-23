# Loaded automatically by Python when shravelle-run puts this directory on
# PYTHONPATH: configures Shravelle before the application imports anything,
# so frameworks are instrumented without code changes.
try:
    import shravelle

    shravelle.configure()
except Exception as e:  # never prevent the application from starting
    import sys

    print("shravelle: failed to start: %s" % e, file=sys.stderr)
