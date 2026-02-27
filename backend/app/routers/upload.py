"""
Bhavcopy Upload Router
=====================
API endpoint to upload and process bhavcopy CSV files.
"""

import logging
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..utils.errors import handle_api_error

router = APIRouter()
logger = logging.getLogger(__name__)
BHAVCOPY_FILE = File(...)

# Upload directory
UPLOAD_DIR = Path(__file__).parent.parent.parent / "data_system" / "01_sources" / "nse_bhavcopy"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload/bhavcopy")
async def upload_bhavcopy(file: UploadFile = BHAVCOPY_FILE):
    """
    Upload a bhavcopy CSV file and process it.

    The file is saved to the bhavcopy directory and then loaded into the database.
    """
    try:
        # Check file extension
        if not file.filename.endswith(".csv"):
            raise HTTPException(status_code=400, detail="Only CSV files are allowed")

        # Save the file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"uploaded_{timestamp}_{file.filename}"
        filepath = UPLOAD_DIR / filename

        # Read and save file content
        content = await file.read()
        await asyncio.to_thread(filepath.write_bytes, content)

        logger.info(f"Saved uploaded bhavcopy to: {filepath}")

        # Process the file (import and run)

        from backend.scripts.load_bhavcopy import load_bhavcopy

        # Parse date from filename
        target_date = None
        try:
            # Try to extract date from original filename
            date_str = file.filename.replace("sec_bhavdata_full_", "").replace(".csv", "")
            target_date = datetime.strptime(date_str, "%d%m%Y").date()
        except ValueError:
            pass

        # Load the bhavcopy
        stats = load_bhavcopy(str(filepath), target_date)

        return {
            "success": True,
            "message": f"Successfully uploaded and processed {file.filename}",
            "filename": filename,
            "stats": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading bhavcopy: {e}", exc_info=True)
        raise handle_api_error(e, "Failed to process bhavcopy file")


@router.get("/upload/status")
def get_upload_status():
    """Get list of uploaded bhavcopy files."""
    try:
        files = []
        for f in UPLOAD_DIR.glob("*.csv"):
            files.append(
                {
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                }
            )
        return {"files": files, "count": len(files)}
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return {"files": [], "count": 0, "error": str(e)}
