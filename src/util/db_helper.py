from database.sqlite_helper import SQLiteDB
from database.supabase_helper import SupabaseDB
from util.logger import logger

# global variable initialized by runtime entrypoints such as run/proposal.py
db = None

def db_initialize(use_local_db: bool = False):
    """Initialize the database connection based on the local-db flag."""
    global db
    if use_local_db:
        from database.sqlite_setup import init_database
        init_database()  # Create tables if they don't exist
        _db = SQLiteDB()
        logger.info("SQLite database initialized")
    else:
        _db = SupabaseDB()
        logger.info("Supabase database initialized")
    db = _db
    
def get_db():
    """Get the database instance."""
    return db

