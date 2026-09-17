import os

APP_NAME = "finworld"
HOME_DIR = os.path.join(os.path.expanduser("~"), ".finworld")
CACHE_DIR = os.path.join(HOME_DIR, "cache")
DB_PATH = os.environ.get("FINWORLD_DB", os.path.join(HOME_DIR, "finworld.db"))
USER_AGENT = "finworld/0.1 (public financial data research CLI)"
HOST_RATE_LIMIT_SECONDS = 1.5
CACHE_TTL_SECONDS = 6 * 3600
HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 2 * 1024 * 1024
FETCH_PAGE_SIZE = 100
FETCH_MAX_PAGES = 10
FETCH_MAX_LIMIT = 1000
SOURCES = {
    "GLEIF": "https://api.gleif.org/api/v1/lei-records",
    "FDIC": "https://api.fdic.gov/banks/institutions",
    "WB_INDICATORS": "https://api.worldbank.org/v2",
    "OSFI": "https://open.canada.ca/data/api/3/action/datastore_search",
}
OSFI_RESOURCE_ID = "945045fa-2de0-47d4-aad2-144d69467824"
OSFI_DATASET_URL = "https://open.canada.ca/data/en/dataset/b27ec3ef-7338-4e76-a6fd-128339a92df5"
REGISTRY_SCHEMA_VERSION = 3
