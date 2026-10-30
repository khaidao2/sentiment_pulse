import sys
from pathlib import Path

# reverse_etl/ on the path so `skills` imports the same way it does in the image,
# where sync_jobs.py sits at /app next to the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
