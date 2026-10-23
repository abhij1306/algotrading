"""The portfolio layer (05): universes, regime v1, sleeve health, the allocator and the sleeve
lifecycle. Pure rules live in `universe`, `regime`, `health`, `allocator` and `lifecycle`; the
database operations are in `universe_service`, `regime_service`, `sleeve_service` and
`allocation_service`, and the two nightly jobs (`regime`, `allocate`) in `jobs`.
"""
