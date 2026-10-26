"""HAT's config records as versioned JSON files under `HAT_DATA_ROOT/admin/config/`
(`records`: the kinds and their rules; `store`: the files; `unit`: what a session reads and
writes). The database holds no config (docs/architecture.md, Storage). Import the submodules:
`records` is a leaf the settings and the API read without the store or SQLAlchemy."""
