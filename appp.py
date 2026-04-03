import os
from sqlalchemy import create_engine

# Step 1: Get DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db.sqlite3")

# Step 2: Print it (for verification)
print("DATABASE URL:", DATABASE_URL)

# Step 3: Create engine
engine = create_engine(DATABASE_URL)