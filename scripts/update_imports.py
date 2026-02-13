"""
Update Import Paths
===================
Updates all import statements to use new directory structure.

Run: python scripts/update_imports.py
"""

import os
from pathlib import Path

# Import path mappings based on actual moves
IMPORT_MAPPINGS = {
    "from data_platform.pipelines.audit": "from data_platform.pipelines.audit",
    "from data_platform.pipelines.backfill": "from data_platform.pipelines.backfill",
    "from data_platform.pipelines.consolidate": "from data_platform.pipelines.consolidate",
    "from data_platform.pipelines.daily_update": "from data_platform.pipelines.daily_update",
    "from data_platform.processors.master_store_builder": "from data_platform.processors.master_store_builder",
    "from data_platform.processors.corporate_actions": "from data_platform.processors.corporate_actions",
    "from data_platform.processors.indicator_engine": "from data_platform.processors.indicator_engine",
    "from data_platform.validators.health_check": "from data_platform.validators.health_check",
    "from data_platform.validators.startup_check": "from data_platform.validators.startup_check",
}

def update_file_imports(filepath: Path):
    """Update imports in a single file"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return False

    original = content

    # Apply all mappings
    for old_import, new_import in IMPORT_MAPPINGS.items():
        content = content.replace(old_import, new_import)

    # Also handle 'import X as Y' if needed, but the above covers most cases

    # Only write if changed
    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ Updated: {filepath}")
        return True
    return False

def main():
    """Update all Python files"""
    search_dirs = [Path("backend"), Path("data_platform"), Path("scripts")]
    updated_count = 0

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for py_file in search_dir.rglob("*.py"):
            if update_file_imports(py_file):
                updated_count += 1

    print(f"\n✅ Updated {updated_count} files")

if __name__ == "__main__":
    main()
