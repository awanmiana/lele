"""Names, paths and the endpoint allowlist.

The rename from finworld to lele renamed the package, the console script, the
home directory and every environment variable. A rename is only correct if it
loses nothing, so the old names are still honoured and the new ones win:

* `LELE_DB` replaces `FINWORLD_DB`; a registry at the old default path is still
  found rather than silently abandoned, and `DB_PATH_SOURCE` says which was
  used so `doctor` can report it.
* the `LELE_*` tuning variables replace the `FINWORLD_*` ones the same way.

Nothing is copied or moved. Silently relocating a database behind a user's back
is how a rename destroys work, so the old path keeps being used until it is
moved deliberately.
"""
import os

APP_NAME = "lele"
APP_VERSION = "0.2.0"
PREVIOUS_APP_NAME = "finworld"

HOME_DIR = os.path.join(os.path.expanduser("~"), ".lele")
LEGACY_HOME_DIR = os.path.join(os.path.expanduser("~"), ".finworld")
CACHE_DIR = os.path.join(HOME_DIR, "cache")
LEGACY_CACHE_DIR = os.path.join(LEGACY_HOME_DIR, "cache")
DB_FILENAME = "lele.db"
LEGACY_DB_FILENAME = "finworld.db"


def _first_env(*names):
    """The first environment variable that is set and non-empty."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    return None, None


def _default_registry(new_home=HOME_DIR, legacy_home=LEGACY_HOME_DIR):
    """Where the registry lives when the environment does not say.

    The new path wins when it exists, or when the old one does not, so a fresh
    install is clean. An existing registry at the old path is still used rather
    than reported as missing, and which path was chosen is returned so the
    reason is visible instead of mysterious.

    The homes are parameters so this is a decision that can be tested directly,
    rather than one that can only be exercised by moving files around a real
    home directory.
    """
    new = os.path.join(new_home, DB_FILENAME)
    legacy = os.path.join(legacy_home, LEGACY_DB_FILENAME)
    if os.path.isfile(new) or not os.path.isfile(legacy):
        return new, "default"
    return legacy, "previous_name"


_, _db_env = _first_env("LELE_DB", "FINWORLD_DB")
if _db_env:
    DB_PATH = _db_env
    DB_PATH_SOURCE = "environment"
else:
    DB_PATH, DB_PATH_SOURCE = _default_registry()


def env(name, default):
    """Read a setting, accepting both the current and the previous prefix."""
    _, value = _first_env(f"LELE_{name}", f"FINWORLD_{name}")
    return default if value is None else value


# Providers see this string, so it is the current name only, with no contact
# detail: inventing one to look polite would be a false claim.
USER_AGENT = env("USER_AGENT", f"{APP_NAME}/{APP_VERSION} "
                               "(public financial data research CLI)")
# The wordmark.
#
# Generated from letter blocks rather than typed as a block of characters: a
# hand-typed wordmark loses its trailing spaces to every editor, every diff and
# every copy-paste, and a wordmark with a missing column is a different letter.
# Six columns per letter, one space between, so the rows cannot drift out of
# alignment and the result survives a proportional font.
_WORDMARK_LETTERS = {
    "L": [" _    ", "| |   ", "| |   ", "| |___", "|____|"],
    "E": [" ___  ", "| __| ", "| _|  ", "| __| ", "|___| "],
    "O": [" ___  ", "|  _| ", "| | | ", "| |_| ", "|___| "],
    " ": ["      ", "      ", "      ", "      ", "      "],
}


def _wordmark(text: str) -> str:
    """Render `text` in the block face used for the logo."""
    rows = [" ".join(_WORDMARK_LETTERS[letter][row] for letter in text)
            for row in range(5)]
    return "\n".join(rows)


LOGO = _wordmark("LELE")

TAGLINE = "a public-source financial research registry"

HOST_RATE_LIMIT_SECONDS = 1.5
HOST_MIN_RATES = {
    "data.sec.gov": HOST_RATE_LIMIT_SECONDS,
    "api.gdeltproject.org": 5.5,
    "comtradeapi.un.org": 1.0,
    "api.census.gov": 1.0,
    "api.eia.gov": 1.0,
    "api.bls.gov": 1.0,
    "opensky-network.org": 1.0,
    "api.alternative.me": 1.0,
    "stablecoins.llama.fi": 1.0,
    "api.coingecko.com": 2.5,
    "news.google.com": 2.0,
}
CACHE_TTL_SECONDS = 6 * 3600
HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 2 * 1024 * 1024
FETCH_PAGE_SIZE = 100
FETCH_MAX_PAGES = 10
FETCH_MAX_LIMIT = 1000
SOURCES = {
    "SEC_SUBMISSIONS": "https://data.sec.gov/submissions",
    "SEC_FACTS": "https://data.sec.gov/api/xbrl/companyfacts",
    "SEC_ARCHIVES": "https://www.sec.gov/Archives/edgar/data",
    "USASPENDING_RECIPIENT": "https://api.usaspending.gov/api/v2/recipient",
    "USASPENDING_AWARDS": "https://api.usaspending.gov/api/v2/search/spending_by_award",
    "SENATE_LDA_FILINGS": "https://lda.gov/api/v1/filings",
    "TREASURY_FISCAL_DATA": "https://api.fiscaldata.treasury.gov/services/api/fiscal_service",
    "FEDERAL_REGISTER": "https://www.federalregister.gov/api/v1/documents",
    "GLEIF": "https://api.gleif.org/api/v1/lei-records",
    "FDIC": "https://api.fdic.gov/banks/institutions",
    "WB_INDICATORS": "https://api.worldbank.org/v2",
    "OSFI": "https://open.canada.ca/data/api/3/action/datastore_search",
    "UN_COMTRADE": "https://comtradeapi.un.org",
    "US_CENSUS": "https://api.census.gov/data/timeseries/intltrade",
    "EIA": "https://api.eia.gov/v2",
    "BLS": "https://api.bls.gov/publicAPI/v2/timeseries/data",
    "OPENSKY": "https://opensky-network.org/api",
    "SEC_IAPD": "https://api.adviserinfo.sec.gov",
    "FEAR_GREED": "https://api.alternative.me",
    "DEFILLAMA_STABLECOINS": "https://stablecoins.llama.fi",
    "COINGECKO": "https://api.coingecko.com/api/v3",
    "GOOGLE_NEWS": "https://news.google.com/rss/search",
}
PRICE_ENDPOINTS = {
    "BTCUSDT": "https://api.binance.com/api/v3/klines",
    "PAXGUSDT": "https://api.binance.com/api/v3/klines",
    "GC=F": "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
    "CL=F": "https://query1.finance.yahoo.com/v8/finance/chart/CL%3DF",
    "RB=F": "https://query1.finance.yahoo.com/v8/finance/chart/RB%3DF",
    "AAPL": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
}
EVIDENCE_ENDPOINTS = {
    "KLINE": "https://fapi.binance.com/fapi/v1/klines",
    "FUNDING": "https://fapi.binance.com/fapi/v1/fundingRate",
    "OPEN_INTEREST": "https://fapi.binance.com/futures/data/openInterestHist",
    "DEPTH": "https://fapi.binance.com/fapi/v1/depth",
    "GLOBAL_LONG_SHORT": "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
    "TOP_LONG_SHORT": "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
    "COT": "https://publicreporting.cftc.gov/resource/6dca-aqww.json",
    "SHORT_INTEREST": "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
    "GDELT": "https://api.gdeltproject.org/api/v2/doc/doc",
    "UN_COMTRADE": "https://comtradeapi.un.org/public/v1/preview/reporter",
    "US_CENSUS_TRADE": "https://api.census.gov/data/timeseries/intltrade",
    "EIA": "https://api.eia.gov/v2",
    "BLS": "https://api.bls.gov/publicAPI/v2/timeseries/data",
    "OPENSKY": "https://opensky-network.org/api",
}
SANCTIONS_ENDPOINTS = {
    "OFAC_SDN_CSV": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
    "UN_CONSOLIDATED_XML": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
    "UK_OFSI_CONLIST_CSV": ("https://ofsistorage.blob.core.windows.net/publishlive/2022format"
                            "/ConList.csv"),
    "EU_CONSOLIDATED_XML": ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
                            "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"),
}
REDIRECT_ENDPOINTS = {
    "OFAC_PUBLISHED_S3": ("https://wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com"
                          "/Published/"),
    "UN_PUBLIC_BLOB": "https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/",
}
OSFI_RESOURCE_ID = "945045fa-2de0-47d4-aad2-144d69467824"
OSFI_DATASET_URL = "https://open.canada.ca/data/en/dataset/b27ec3ef-7338-4e76-a6fd-128339a92df5"
REGISTRY_SCHEMA_VERSION = 20
