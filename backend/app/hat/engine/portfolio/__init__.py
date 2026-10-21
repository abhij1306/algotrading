"""The portfolio layer (05): universes, regime v1, sleeve health, the allocator and the sleeve
lifecycle. Pure rules live in `universe`, `regime`, `health`, `allocator` and `lifecycle`; the
database operations and the two nightly jobs (`regime`, `allocate`) are in `service` and `jobs`.
"""
