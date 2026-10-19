"""Trade guidance: model-based trade ideas computed from the live map payload.

build.py is deterministic (every price is a map level or a stated computation of map numbers),
template.py writes the plain narrative from it, ai.py optionally narrates it through the local
claude CLI with a validator, and api.py exposes /api/guidance and /api/guidance/narrate.
Not financial advice. Untested until the Scorecard gates pass.
"""
