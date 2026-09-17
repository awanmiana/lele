import argparse
import csv
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import tempfile
from urllib.error import URLError

from ..analysis import engine
from ..core import importer, registry
from ..core.constants import DB_PATH, FETCH_MAX_LIMIT
from ..fetchers import sources
from ..fetchers.http import SourceError


COMMANDS = {
    "init": "Initialize registry",
    "sources": "Available data sources",
    "list": "Find entities",
    "show": "Show entity details",
    "stats": "Registry statistics",
    "countries": "List countries",
    "fetch": "Fetch official source data",
    "import": "Import local JSON",
    "analyze": "Analyze stored entity data",
    "sentiment": "Record supplied-text sentiment",
    "relationships": "Show recorded relationships",
    "export": "Export entity list",
    "menu": "Interactive menu",
}
MENU_LABELS = {k: v for k, v in COMMANDS.items() if k != "menu"} | {
    "help": "Command help", "back": "Back to menu", "quit": "Quit",
}
ENTITY_FIELDS = ("id", "key", "kind", "name", "country", "website", "lei", "notes", "created_at")


class CLIError(Exception):
    def __init__(self, message, status=1):
        super().__init__(message)
        self.status = status


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def _limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000") from None
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000")
    return number


def _eid(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ID must be a positive SQLite integer") from None
    if not 1 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("ID must be a positive SQLite integer")
    return number


def _path(value):
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("path must be nonempty and contain no NUL")
    return value


def _filters(parser):
    parser.add_argument("--query", default="", help="Name, key, LEI or notes search")
    parser.add_argument("--kind", default="", help="Exact entity kind")
    parser.add_argument("--country", default="", help="Country or jurisdiction search")
    parser.add_argument("--limit", type=_limit, default=50, help="Maximum rows, 1..1000 (default: 50)")


def build_parser():
    parser = Parser(
        prog="finworld", description="Financial institution registry and stored-data analysis.",
        epilog="Global --db PATH and --json must precede the command. With no command: menu on a TTY, help otherwise.",
    )
    parser.add_argument("--db", type=_path, default=DB_PATH, metavar="PATH", help="SQLite registry path (root position only)")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON results (root position only)")
    subs = parser.add_subparsers(dest="command", title="commands")
    parsers = {name: subs.add_parser(name, help=label, description=label) for name, label in COMMANDS.items()}
    _filters(parsers["list"])
    for command in ("show", "analyze", "sentiment", "relationships"):
        parsers[command].add_argument("id", type=_eid, metavar="ID")
    fetch = parsers["fetch"]
    fetch.add_argument("source", choices=tuple(item["id"] for item in sources.list_sources()))
    fetch.add_argument("--query", default="", help="Name or identifier; SEC: 10-digit CIK or previously stored SEC ticker (1..10 letters); OSFI: fulltext search, not bank-only; not supported by World Bank")
    fetch.add_argument("--country", default="", help="GLEIF: ISO2; FDIC: US; SEC: must be empty; OSFI: blank/CA/CAN (regulatory jurisdiction, not headquarters); World Bank: economy code or ALL")
    fetch.epilog = "OSFI: monthly public federal list, not all Canadian institutions or historical coverage; stored excludes the regulator. Representative offices carry no automatic supervision claim. Renames are not resolved; absent records are not deleted."
    fetch.add_argument("--limit", type=_limit, default=50, help="Input record budget; SEC: maximum recent filings stored per fetch, 1..1000 (default: 50)")
    fetch.add_argument("--indicator", help="World Bank only (default: NY.GDP.MKTP.CD)")
    fetch.add_argument("--category", choices=("FUND", "SOLE_PROPRIETOR"), default="", help="GLEIF only: entity category; FUND does not identify VC")
    fetch.add_argument("--financials", action="store_true", help="SEC only: latest us-gaap USD facts by max(end, filed), no form/frame preference; 20 MiB cap instead of default 2 MiB; failure aborts ingestion")
    parsers["import"].add_argument("path", type=_path, metavar="PATH", help="Importer JSON with entities, provenance and relationships")
    sentiment = parsers["sentiment"]
    text = sentiment.add_mutually_exclusive_group(required=True)
    text.add_argument("--text", help=f"Supplied English text, at most {engine.MAX_TEXT_LEN} characters")
    text.add_argument("--file", type=_path, metavar="PATH", help="UTF-8 text file")
    sentiment.add_argument("--source", required=True, help="Nonempty provenance label")
    sentiment.add_argument("--ref", default="", help="Optional reference")
    export = parsers["export"]
    export.add_argument("--format", choices=("json", "csv"), required=True)
    export.add_argument("--output", type=_path, required=True, metavar="PATH", help="Destination in an existing parent directory")
    export.add_argument("--force", action="store_true", help="Atomically replace an existing file")
    _filters(export)
    export.epilog = "Exports flat entity rows, not an importer backup. CSV formula-like cells are prefixed with an apostrophe."
    parsers["menu"].epilog = "Choose a number or command; supply arguments using CLI quoting. No shell is executed. Use back or quit at any prompt."
    return parser


def _validate_fetch(args):
    query, country = args.query.strip(), args.country.strip().upper()
    if len(query) > 200 or any(ord(c) < 32 for c in query):
        raise CLIError("fetch query must be at most 200 characters without controls", 2)
    if args.limit > FETCH_MAX_LIMIT:
        raise CLIError("fetch limit exceeds the source record budget", 2)
    if args.indicator is not None and args.source != "worldbank":
        raise CLIError("--indicator is supported only for worldbank", 2)
    if args.category and args.source != "gleif":
        raise CLIError("--category is supported only for gleif", 2)
    if args.financials and args.source != "sec":
        raise CLIError("--financials is supported only for sec", 2)
    if args.source == "sec":
        if country:
            raise CLIError("SEC --country must be empty", 2)
        if not re.fullmatch(r"(?:[A-Z]{1,10}|[0-9]{10})", query.upper()):
            raise CLIError("SEC --query must be a ticker (1..10 letters) or a 10-digit CIK", 2)
    if args.source == "gleif" and country and not re.fullmatch(r"[A-Z]{2}", country):
        raise CLIError("GLEIF country must be a two-letter code", 2)
    if args.source == "fdic" and country not in ("", "US", "USA"):
        raise CLIError("FDIC supports only US country coverage", 2)
    if args.source == "osfi" and country not in ("", "CA", "CAN"):
        raise CLIError("OSFI supports only CA country coverage", 2)
    if args.source == "worldbank":
        if query:
            raise CLIError("World Bank does not support --query; use --country and --indicator", 2)
        if country and not re.fullmatch(r"[A-Z0-9]{2,3}", country):
            raise CLIError("World Bank country must be a single economy code or ALL", 2)
        indicator = args.indicator.strip() if args.indicator is not None else "NY.GDP.MKTP.CD"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", indicator):
            raise CLIError("invalid World Bank indicator code", 2)


def _sentiment_text(args):
    for label, value, limit in (("source", args.source, engine.MAX_SOURCE_LEN), ("ref", args.ref, engine.MAX_REF_LEN)):
        if len(value) > limit or (label == "source" and not value.strip()) or "\x00" in value:
            raise CLIError(f"{label} must be valid text of at most {limit} characters", 2)
    if args.file is not None:
        try:
            with open(args.file, encoding="utf-8-sig") as stream:
                text = stream.read(engine.MAX_TEXT_LEN + 1)
        except UnicodeError:
            raise CLIError("sentiment file must be UTF-8 text", 2) from None
    else:
        text = args.text
    if not text.strip() or len(text) > engine.MAX_TEXT_LEN or "\x00" in text:
        raise CLIError(f"sentiment text must be nonempty and at most {engine.MAX_TEXT_LEN} characters without NUL", 2)
    return text


def _rows(conn, args):
    return [dict(row) for row in registry.find_entities(
        conn, q=args.query, kind=args.kind, country=args.country, limit=args.limit,
    )]


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _export_target(args):
    output = Path(args.output).absolute()
    if not output.parent.is_dir():
        raise CLIError("export parent directory must already exist")
    if os.path.lexists(output) and not args.force:
        raise CLIError("output already exists; use --force to replace it")
    if output.is_dir():
        raise CLIError("export output must be a file")
    if not args.force and not hasattr(os, "link"):
        raise CLIError("atomic no-overwrite export is unavailable on this runtime; use a Python runtime with os.link support")
    return output


def _export(rows, args):
    output = _export_target(args)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=output.parent, prefix=".finworld-", delete=False) as stream:
            temporary = Path(stream.name)
            if args.format == "json":
                json.dump(rows, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.write("\n")
            else:
                writer = csv.DictWriter(stream, fieldnames=ENTITY_FIELDS)
                writer.writeheader()
                writer.writerows({key: _csv_cell(row.get(key)) for key in ENTITY_FIELDS} for row in rows)
            stream.flush()
            os.fsync(stream.fileno())
        if args.force:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise CLIError("output already exists; use --force to replace it") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"exported": len(rows), "format": args.format, "output": args.output}


def _protect_database(args):
    output = Path(args.output).absolute()
    database = Path(args.db).resolve()
    for protected in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")):
        if output.resolve() == protected or (output.exists() and protected.exists() and output.samefile(protected)):
            raise CLIError("export output must not replace the registry or its journal files")


def _dispatch(args):
    command = args.command
    if command == "sources":
        return sources.list_sources()
    if command == "fetch":
        _validate_fetch(args)
    if command == "export":
        _protect_database(args)
        _export_target(args)
    if command == "init":
        registry.init_db(args.db)
        return {"initialized": True}
    with registry.get_conn(args.db) as conn:
        if command in ("show", "analyze", "sentiment", "relationships"):
            if registry.get_entity(conn, args.id) is None:
                raise CLIError(f"entity {args.id} not found")
        if command == "list":
            return _rows(conn, args)
        if command == "show":
            return registry.entity_payload(conn, args.id)
        if command == "stats":
            return registry.stats(conn)
        if command == "countries":
            return registry.list_countries(conn)
        if command == "fetch":
            return sources.fetch_source(conn, args.source, query=args.query, country=args.country,
                                        limit=args.limit, indicator=args.indicator if args.indicator is not None else "NY.GDP.MKTP.CD",
                                        category=args.category, financials=args.financials)
        if command == "import":
            return importer.import_json(conn, args.path)
        if command == "analyze":
            return engine.analyze_entity(conn, args.id)
        if command == "sentiment":
            return engine.record_sentiment(conn, args.id, _sentiment_text(args), args.source, args.ref)
        if command == "relationships":
            return registry.entity_payload(conn, args.id)["relationships"]
        if command == "export":
            rows = _rows(conn, args)
    if command == "export":
        return _export(rows, args)
    raise CLIError("unknown command", 2)


def _display_cell(value):
    text = "" if value is None else str(value)
    text = "".join(c if c.isprintable() else " " for c in text)
    return text if len(text) <= 60 else text[:57] + "..."


def _table(rows, fields):
    if not rows:
        print("No results.")
        return
    values = [[_display_cell(row.get(field)) for field in fields] for row in rows]
    widths = [max(len(field), *(len(row[i]) for row in values)) for i, field in enumerate(fields)]
    print("  ".join(field.upper().ljust(width) for field, width in zip(fields, widths)).rstrip())
    for row in values:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def _emit(result, args):
    if args.json:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    elif args.command == "list":
        _table(result, ("id", "kind", "name", "country"))
    elif args.command == "sources":
        _table(result, ("id", "name", "status", "coverage"))
    elif args.command == "relationships":
        _table(result, ("dir", "rel", "other_id", "other_name"))
    elif args.command == "countries":
        print("\n".join(_display_cell(country) for country in result) if result else "No results.")
    else:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))


def _prompt(message):
    print(message, end="", file=sys.stderr, flush=True)
    return input().strip()


def _menu(args, parser):
    prefix = ["--db", args.db] + (["--json"] if args.json else [])
    choices = list(MENU_LABELS)
    try:
        while True:
            print("\nFinworld menu", file=sys.stderr)
            for index, command in enumerate(choices, 1):
                print(f"{index:2}. {MENU_LABELS[command]} ({command})", file=sys.stderr)
            choice = _prompt("Choose a number or command: ").lower()
            if choice.isascii() and choice.isdecimal() and len(choice) < 4 and 1 <= int(choice) <= len(choices):
                choice = choices[int(choice) - 1]
            if choice in ("quit", "q", "exit"):
                return 0
            if choice in ("back", "", "b"):
                continue
            if choice == "help":
                parser.print_help(file=sys.stderr)
                continue
            if choice not in COMMANDS or choice == "menu":
                print("Unknown menu choice; use help or quit.", file=sys.stderr)
                continue
            tokens = []
            if choice not in ("init", "sources", "stats", "countries"):
                line = _prompt(f"Arguments for {choice} (--help for syntax; back/quit): ")
                if line.lower() in ("quit", "q", "exit"):
                    return 0
                if line.lower() in ("back", "b"):
                    continue
                try:
                    tokens = shlex.split(line)
                except ValueError:
                    print("Invalid quoting; operation cancelled.", file=sys.stderr)
                    continue
            main(prefix + [choice] + tokens)
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 0


def main(argv=None):
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.command is None:
            if sys.stdin.isatty() and not args.json:
                return _menu(args, parser)
            if args.json:
                print(json.dumps({"commands": COMMANDS, "menu_labels": MENU_LABELS, "usage": parser.format_usage().strip(), "global_options": "--db PATH and --json precede the command"}))
            else:
                parser.print_help()
            return 0
        if args.command == "menu":
            return _menu(args, parser)
        _emit(_dispatch(args), args)
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except CLIError as exc:
        print(f"finworld: {exc}", file=sys.stderr)
        return exc.status
    except (SourceError, URLError):
        print("finworld: source request failed; check connectivity, source availability and configuration", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print("finworld: storage or I/O operation failed; check paths, permissions and database health", file=sys.stderr)
        return 1
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        print("finworld: invalid data or registry schema; check the input and database version", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("finworld: operation cancelled", file=sys.stderr)
        return 1
