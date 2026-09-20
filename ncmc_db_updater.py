
#!/usr/bin/env python3

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

OPERATORS_CSV = Path("operators.csv")
STATION_DIRECTORY = Path(".")

OPERATOR_HEADERS = [
    "acquirer_id",
    "operator_id",
    "operator_name",
    "terminal_info",
]

STATION_HEADERS = [
    "terminal_id",
    "station_name",
    "comments",
]


# ============================================================
# ID handling
# ============================================================

def parse_id(value, assume_hex=False):
    """
    Parse an ID.

    Examples:
        11       -> 11 (decimal)
        "11"     -> 11 (decimal)
        "0x0B"   -> 11
        "0B"     -> 11 when assume_hex=True
        "177D"   -> 6013 when assume_hex=True
        "6013"   -> 6013 when assume_hex=False
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not value.is_integer():
            return None

        return int(value)

    value = str(value).strip()

    if not value:
        return None

    try:
        if value.lower().startswith("0x"):
            return int(value[2:], 16)

        if assume_hex:
            return int(value, 16)

        return int(value, 10)

    except ValueError:
        return None


def normalize_acquirer(value, from_csv=False):
    number = parse_id(value, assume_hex=from_csv)

    if number is None or not 0 <= number <= 0xFF:
        return None

    return f"{number:02X}"


def normalize_operator(value, from_csv=False):
    number = parse_id(value, assume_hex=from_csv)

    if number is None or not 0 <= number <= 0xFFFF:
        return None

    return f"{number:04X}"


def normalize_terminal(value, from_csv=False):
    number = parse_id(value, assume_hex=from_csv)

    if number is None or not 0 <= number <= 0xFFFFFF:
        return None

    return f"{number:06X}"


def hex_string(value, width):
    number = parse_id(value)

    if number is None:
        return "Unknown"

    return f"0x{number:0{width}X}"


def station_filename(acquirer_hex, operator_hex):
    """
    Generate filenames such as:
        0B177D.csv
        043630.csv
    """

    return f"{acquirer_hex}{operator_hex}.csv"


# ============================================================
# JSON handling
# ============================================================

def find_ncmc_application(data):
    """Find the NCMC application recursively."""

    if isinstance(data, list):
        if (
            len(data) == 2
            and data[0] == "ncmc"
            and isinstance(data[1], dict)
        ):
            return data[1]

        for item in data:
            result = find_ncmc_application(item)

            if result is not None:
                return result

    elif isinstance(data, dict):
        for value in data.values():
            result = find_ncmc_application(value)

            if result is not None:
                return result

    return None


def extract_transactions(ncmc_application):
    """
    Extract the top-level transactions list.

    Nested 'entry' objects are not treated as separate
    transactions.
    """

    transactions = ncmc_application.get(
        "transactions",
        [],
    )

    if not isinstance(transactions, list):
        return []

    return [
        transaction
        for transaction in transactions
        if isinstance(transaction, dict)
    ]


# ============================================================
# Operator CSV handling
# ============================================================

def normalize_csv_value(value):
    """Normalize a CSV value for comparison."""

    if value is None:
        return ""

    return str(value).strip().upper()


def csv_rows_match(row_a, row_b, fields):
    """
    Compare two CSV rows using the specified fields.
    """

    return all(
        normalize_csv_value(row_a.get(field))
        == normalize_csv_value(row_b.get(field))
        for field in fields
    )

def load_operators():
    """
    Load operators.csv.

    CSV IDs are interpreted as hexadecimal, including:
        0x0B
        0B
        177D
        0001

    Returns:
        operators:
            Dictionary indexed by:
                (acquirer_hex, operator_hex)

        rows:
            Existing rows retained in memory.
    """

    operators = {}
    rows = []

    if not OPERATORS_CSV.exists():
        print(
            f"[!] Missing operator file: "
            f"{OPERATORS_CSV}"
        )

        return operators, rows

    with OPERATORS_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(
            file,
            skipinitialspace=True,
        )

        if reader.fieldnames is None:
            print("[!] operators.csv has no header.")
            return operators, rows

        # Normalize headers before reading rows.
        reader.fieldnames = [
            field.strip().lower()
            if field is not None
            else ""
            for field in reader.fieldnames
        ]

        for line_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {}

            for key, value in raw_row.items():
                if key is None:
                    continue

                normalized_key = key.strip().lower()

                if isinstance(value, str):
                    value = value.strip()

                row[normalized_key] = value

            acquirer_hex = normalize_acquirer(
                row.get("acquirer_id"),
                from_csv=True,
            )

            operator_hex = normalize_operator(
                row.get("operator_id"),
                from_csv=True,
            )

            if acquirer_hex is None or operator_hex is None:
                print(
                    f"[!] Skipping invalid operator row "
                    f"on line {line_number}: {raw_row}"
                )

                continue

            row["acquirer_id"] = f"0x{acquirer_hex}"
            row["operator_id"] = f"0x{operator_hex}"

            row.setdefault("operator_name", "")
            row.setdefault("terminal_info", "")

            key = (
                acquirer_hex,
                operator_hex,
            )

            if key in operators:
                print(
                    f"[!] Duplicate operator in CSV "
                    f"on line {line_number}: "
                    f"0x{acquirer_hex}/0x{operator_hex}"
                )

                # Preserve the first occurrence.
                continue

            operators[key] = row
            rows.append(row)

            print(
                f"[CSV] Loaded operator: "
                f"acquirer=0x{acquirer_hex}, "
                f"operator=0x{operator_hex}, "
                f"name={row['operator_name']!r}"
            )

    print(
        f"[*] Loaded {len(operators)} operators "
        f"from {OPERATORS_CSV}"
    )

    return operators, rows

def append_new_operators(new_operators):
    """
    Append only genuinely new operator rows.

    Existing rows are preserved.
    """

    if not new_operators:
        return

    existing_rows = []

    if OPERATORS_CSV.exists():
        with OPERATORS_CSV.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            reader = csv.DictReader(
                file,
                skipinitialspace=True,
            )

            if reader.fieldnames:
                reader.fieldnames = [
                    field.strip().lower()
                    for field in reader.fieldnames
                ]

                existing_rows = list(reader)

    file_is_empty = (
        not OPERATORS_CSV.exists()
        or OPERATORS_CSV.stat().st_size == 0
    )

    appended_count = 0

    with OPERATORS_CSV.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=OPERATOR_HEADERS,
        )

        if file_is_empty:
            writer.writeheader()

        for operator in new_operators:
            new_row = {
                "acquirer_id": operator.get(
                    "acquirer_id",
                    "",
                ),
                "operator_id": operator.get(
                    "operator_id",
                    "",
                ),
                "operator_name": operator.get(
                    "operator_name",
                    "",
                ),
                "terminal_info": operator.get(
                    "terminal_info",
                    "",
                ),
            }

            # Check whether the complete operator row exists.
            duplicate = any(
                csv_rows_match(
                    existing_row,
                    new_row,
                    OPERATOR_HEADERS,
                )
                for existing_row in existing_rows
            )

            if duplicate:
                continue

            writer.writerow(new_row)
            existing_rows.append(new_row)
            appended_count += 1

    print(
        f"[*] Appended {appended_count} "
        "new operator row(s)"
    )

def update_operator_terminal_info(
    operator_row,
    acquirer_hex,
    operator_hex,
):
    """
    If terminal_info is missing, assign the generated
    station filename and update operators.csv.

    Existing operators and rows are preserved.
    """

    current_filename = (
        operator_row.get("terminal_info") or ""
    ).strip()

    if current_filename:
        return current_filename

    generated_filename = station_filename(
        acquirer_hex,
        operator_hex,
    )

    operator_row["terminal_info"] = generated_filename

    if not OPERATORS_CSV.exists():
        return generated_filename

    # Load all existing rows.
    with OPERATORS_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(
            file,
            skipinitialspace=True,
        )

        if reader.fieldnames is None:
            return generated_filename

        rows = []

        for raw_row in reader:
            row = {}

            for key, value in raw_row.items():
                if key is None:
                    continue

                key = key.strip().lower()

                if isinstance(value, str):
                    value = value.strip()

                row[key] = value

            rows.append(row)

    target_key = (
        acquirer_hex,
        operator_hex,
    )

    # Update only the matching operator.
    for row in rows:
        row_acquirer = normalize_acquirer(
            row.get("acquirer_id"),
            from_csv=True,
        )

        row_operator = normalize_operator(
            row.get("operator_id"),
            from_csv=True,
        )

        if (
            row_acquirer,
            row_operator,
        ) == target_key:
            row["terminal_info"] = generated_filename

    # Rewrite the complete file while preserving all rows.
    temporary_path = OPERATORS_CSV.with_suffix(
        ".csv.tmp"
    )

    with temporary_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=OPERATOR_HEADERS,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow({
                "acquirer_id": row.get(
                    "acquirer_id",
                    "",
                ),
                "operator_id": row.get(
                    "operator_id",
                    "",
                ),
                "operator_name": row.get(
                    "operator_name",
                    "",
                ),
                "terminal_info": row.get(
                    "terminal_info",
                    "",
                ),
            })

    temporary_path.replace(OPERATORS_CSV)

    print(
        f"[UPDATED OPERATOR] "
        f"0x{acquirer_hex}/0x{operator_hex} "
        f"-> {generated_filename}"
    )

    return generated_filename

def add_operator_if_missing(
    operators,
    new_operators,
    acquirer_hex,
    operator_hex,
):
    """
    Return an existing operator or create a new one
    in memory.

    Only genuinely new operators are added to
    new_operators.
    """

    key = (
        acquirer_hex,
        operator_hex,
    )

    if key in operators:
        return operators[key], False

    filename = station_filename(
        acquirer_hex,
        operator_hex,
    )

    new_row = {
        "acquirer_id": f"0x{acquirer_hex}",
        "operator_id": f"0x{operator_hex}",
        "operator_name": "",
        "terminal_info": filename,
    }

    operators[key] = new_row
    new_operators.append(new_row)

    return new_row, True


# ============================================================
# Station CSV handling
# ============================================================

def load_station_file(path):
    """
    Read a station CSV.

    Returns:
        rows:
            Existing station rows.

        terminal_ids:
            Existing terminal IDs, including masks.
    """

    rows = []
    terminal_ids = set()

    if not path.exists():
        return rows, terminal_ids

    if path.stat().st_size == 0:
        return rows, terminal_ids

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(
            file,
            skipinitialspace=True,
        )

        if reader.fieldnames is None:
            return rows, terminal_ids

        reader.fieldnames = [
            field.strip().lower()
            if field is not None
            else ""
            for field in reader.fieldnames
        ]

        for raw_row in reader:
            row = {}

            for key, value in raw_row.items():
                if key is None:
                    continue

                key = key.strip().lower()

                if isinstance(value, str):
                    value = value.strip()

                row[key] = value

            terminal_value = row.get(
                "terminal_id",
                "",
            )

            if terminal_value:
                terminal_value = (
                    str(terminal_value)
                    .strip()
                    .upper()
                    .replace("0X", "")
                )

                terminal_ids.add(terminal_value)

            rows.append(row)

    return rows, terminal_ids

def create_empty_station_file(path):
    """Create a station file containing only its header."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=STATION_HEADERS,
        )

        writer.writeheader()

def append_terminal_to_station_file(
    station_path,
    terminal_hex,
):
    """
    Append a terminal ID safely.

    - Ensures the existing final row ends with a newline.
    - Supports X-masked terminal IDs.
    - Prevents duplicate entries.
    - Preserves existing rows.
    """

    station_path = Path(station_path)

    terminal_hex = (
        terminal_hex.upper()
        .replace("0X", "")
    )

    new_row = {
        "terminal_id": f"0x{terminal_hex}",
        "station_name": "",
        "comments": "",
    }

    # --------------------------------------------------------
    # Create a new file if missing or empty.
    # --------------------------------------------------------

    if (
        not station_path.exists()
        or station_path.stat().st_size == 0
    ):
        create_empty_station_file(station_path)

        with station_path.open(
            "a",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=STATION_HEADERS,
            )

            writer.writerow(new_row)

        return True

    # --------------------------------------------------------
    # Check existing station IDs and wildcard masks.
    # --------------------------------------------------------

    existing_rows, existing_terminal_ids = (
        load_station_file(station_path)
    )

    for existing_terminal_hex in existing_terminal_ids:
        if terminal_matches_mask(
            terminal_hex=terminal_hex,
            station_hex=existing_terminal_hex,
        ):
            return False

    # Complete row comparison.
    for existing_row in existing_rows:
        if csv_rows_match(
            existing_row,
            new_row,
            STATION_HEADERS,
        ):
            return False

    # --------------------------------------------------------
    # Ensure the existing file ends with a newline.
    # --------------------------------------------------------

    with station_path.open(
        "rb"
    ) as file:
        file.seek(0, 2)

        file_size = file.tell()

        if file_size > 0:
            file.seek(-1, 2)
            last_byte = file.read(1)

            needs_newline = (
                last_byte not in (b"\n", b"\r")
            )
        else:
            needs_newline = False

    # --------------------------------------------------------
    # Append the new row on a separate line.
    # --------------------------------------------------------

    with station_path.open(
        "ab"
    ) as file:

        if needs_newline:
            file.write(b"\n")

    with station_path.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=STATION_HEADERS,
            lineterminator="\n",
        )

        writer.writerow(new_row)

    return True

def terminal_matches_mask(
    terminal_hex,
    station_hex,
):
    """
    Compare a terminal ID against a station ID mask.

    Example:
        terminal_hex = "123456"
        station_hex  = "123XXX"
        Result       = True

    X characters are wildcards.
    Comparison is case-insensitive.
    """

    terminal_hex = terminal_hex.upper().replace(
        "0X", ""
    )

    station_hex = station_hex.upper().replace(
        "0X", ""
    )

    if len(terminal_hex) != len(station_hex):
        return False

    for terminal_char, station_char in zip(
        terminal_hex,
        station_hex,
    ):
        if station_char == "X":
            continue

        if terminal_char != station_char:
            return False

    return True

def get_station_path(
    operator_row,
    acquirer_hex,
    operator_hex,
):
    """
    Resolve the station file and update terminal_info
    when it is missing.
    """

    configured_filename = (
        operator_row.get("terminal_info") or ""
    ).strip()

    if configured_filename:
        configured_path = (
            STATION_DIRECTORY / configured_filename
        )

        if configured_path.exists():
            return configured_path

    # Missing terminal_info: assign and persist filename.
    if not configured_filename:
        configured_filename = (
            update_operator_terminal_info(
                operator_row=operator_row,
                acquirer_hex=acquirer_hex,
                operator_hex=operator_hex,
            )
        )

    return (
        STATION_DIRECTORY / configured_filename
    )

# ============================================================
# Date/time handling
# ============================================================

def parse_effective_date(value):
    """
    Parse YYMMDD into a datetime at midnight.
    """

    if not isinstance(value, str):
        return None

    value = value.strip()

    if len(value) != 6 or not value.isdigit():
        return None

    try:
        return datetime.strptime(
            value,
            "%y%m%d",
        )

    except ValueError:
        return None


def calculate_transaction_datetime(
    effective_datetime,
    minutes_elapsed,
):
    minutes = parse_id(minutes_elapsed)

    if effective_datetime is None:
        return None

    if minutes is None or minutes < 0:
        return None

    return effective_datetime + timedelta(
        minutes=minutes
    )


# ============================================================
# History printing
# ============================================================

def print_history(
    ncmc_application,
    transactions,
    operators,
):
    print("\n" + "=" * 72)
    print("NCMC TRANSACTION HISTORY")
    print("=" * 72)

    effective_date = ncmc_application.get(
        "effectiveDate"
    )

    effective_datetime = parse_effective_date(
        effective_date
    )

    if effective_datetime is None:
        print(
            f"Effective date: {effective_date!r} "
            "(invalid or missing)"
        )

    else:
        print(
            "Effective date: "
            f"{effective_datetime.strftime('%Y-%m-%d')}"
        )

    print(
        f"Transactions detected: "
        f"{len(transactions)}"
    )

    if not transactions:
        print("No transactions found.")
        return

    for index, transaction in enumerate(
        transactions,
        start=1,
    ):
        acquirer_id = transaction.get(
            "acquirerId"
        )

        operator_id = transaction.get(
            "operatorId"
        )

        terminal_id = transaction.get(
            "terminalId"
        )

        acquirer_hex = normalize_acquirer(
            acquirer_id
        )

        operator_hex = normalize_operator(
            operator_id
        )

        operator = operators.get(
            (
                acquirer_hex,
                operator_hex,
            )
        )

        if operator:
            operator_name = (
                operator.get("operator_name") or ""
            ).strip()

            if not operator_name:
                operator_name = "Unnamed operator"

        else:
            operator_name = "Unknown operator"

        transaction_time = (
            calculate_transaction_datetime(
                effective_datetime,
                transaction.get(
                    "minutesElapsed"
                ),
            )
        )

        print(f"\nTransaction #{index}")
        print("-" * 72)

        if transaction_time is None:
            print("Date/time: Unknown")

        else:
            print(
                "Date/time: "
                f"{transaction_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        print(f"Operator: {operator_name}")

        print(
            f"Acquirer ID: "
            f"{hex_string(acquirer_id, 2)}"
        )

        print(
            f"Operator ID: "
            f"{hex_string(operator_id, 4)}"
        )

        print(
            f"Terminal ID: "
            f"{hex_string(terminal_id, 6)}"
        )

        print(
            f"Minutes elapsed: "
            f"{transaction.get('minutesElapsed', 'Unknown')}"
        )

        print(
            f"Transaction sequence: "
            f"{transaction.get('transactionSequence', 'Unknown')}"
        )

        print(
            f"Amount units: "
            f"{transaction.get('amountUnits', 'Unknown')}"
        )

        print(
            f"Balance units: "
            f"{transaction.get('balanceUnits', 'Unknown')}"
        )

        print(
            f"Status code: "
            f"{transaction.get('statusCode', 'Unknown')}"
        )

        print(
            f"RFU: "
            f"{transaction.get('rfu', 'Unknown')}"
        )

        entry = transaction.get("entry")

        if isinstance(entry, dict):
            print("\nEntry details:")

            print(
                f"  Acquirer ID: "
                f"{hex_string(entry.get('acquirerId'), 2)}"
            )

            print(
                f"  Operator ID: "
                f"{hex_string(entry.get('operatorId'), 4)}"
            )

            print(
                f"  Terminal ID: "
                f"{hex_string(entry.get('terminalId'), 6)}"
            )

            print(
                f"  Minutes elapsed: "
                f"{entry.get('minutesElapsed', 'Unknown')}"
            )


# ============================================================
# Discovery and database update
# ============================================================

def discover_and_update(
    transactions,
    operators,
):
    new_operators = []
    new_stations = []
    unprocessed_records = []

    added_operator_keys = set()
    added_station_keys = set()
    station_files_updated = set()

    for transaction in transactions:
        acquirer_hex = normalize_acquirer(
            transaction.get("acquirerId")
        )

        operator_hex = normalize_operator(
            transaction.get("operatorId")
        )

        terminal_hex = normalize_terminal(
            transaction.get("terminalId")
        )

        reasons = []

        if acquirer_hex is None:
            reasons.append("invalid acquirer ID")

        if operator_hex is None:
            reasons.append("invalid operator ID")

        if terminal_hex is None:
            reasons.append("invalid terminal ID")

        if reasons:
            unprocessed_records.append({
                "acquirerId": transaction.get(
                    "acquirerId"
                ),
                "operatorId": transaction.get(
                    "operatorId"
                ),
                "terminalId": transaction.get(
                    "terminalId"
                ),
                "amountUnits": transaction.get(
                    "amountUnits"
                ),
                "reason": "; ".join(reasons),
            })

            continue

        key = (
            acquirer_hex,
            operator_hex,
        )

        station_key = (
            acquirer_hex,
            operator_hex,
            terminal_hex,
        )

        # ----------------------------------------------------
        # Find or add operator.
        # ----------------------------------------------------

        operator_row, was_added = (
            add_operator_if_missing(
                operators=operators,
                new_operators=new_operators,
                acquirer_hex=acquirer_hex,
                operator_hex=operator_hex,
            )
        )

        if was_added:
            print(
                f"[NEW OPERATOR] "
                f"Acquirer: 0x{acquirer_hex}, "
                f"Operator: 0x{operator_hex}"
            )

            added_operator_keys.add(key)

        # ----------------------------------------------------
        # Resolve station file.
        # ----------------------------------------------------

        station_path = get_station_path(
            operator_row=operator_row,
            acquirer_hex=acquirer_hex,
            operator_hex=operator_hex,
        )

        # ----------------------------------------------------
        # Add terminal without deleting existing IDs.
        # ----------------------------------------------------

        was_added = append_terminal_to_station_file(
            station_path=station_path,
            terminal_hex=terminal_hex,
        )

        if was_added:
            station_files_updated.add(
                station_path.name
            )

            if station_key not in added_station_keys:
                new_stations.append({
                    "acquirer_id": f"0x{acquirer_hex}",
                    "operator_id": f"0x{operator_hex}",
                    "terminal_id": f"0x{terminal_hex}",
                    "file": station_path.name,
                })

                added_station_keys.add(station_key)

    # IMPORTANT:
    # Append only new operators.
    # Existing operators are never rewritten.
    append_new_operators(new_operators)

    return (
        new_operators,
        new_stations,
        unprocessed_records,
        station_files_updated,
    )


# ============================================================
# Summary
# ============================================================

def print_summary(
    new_operators,
    new_stations,
    unprocessed_records,
    station_files_updated,
):
    print("\n" + "=" * 72)
    print("NEW OPERATORS ADDED")
    print("=" * 72)

    if not new_operators:
        print("None")

    for operator in new_operators:
        print(
            f"- Acquirer ID: "
            f"{operator['acquirer_id']}, "
            f"Operator ID: "
            f"{operator['operator_id']}"
        )

    print("\n" + "=" * 72)
    print("STATION FILES CREATED OR UPDATED")
    print("=" * 72)

    if not station_files_updated:
        print("None")

    for filename in sorted(
        station_files_updated
    ):
        print(f"- {filename}")

    print("\n" + "=" * 72)
    print("NEW TERMINAL IDs")
    print("=" * 72)

    if not new_stations:
        print("None")

    for station in new_stations:
        print(
            f"- Acquirer ID: "
            f"{station['acquirer_id']}, "
            f"Operator ID: "
            f"{station['operator_id']}, "
            f"Terminal ID: "
            f"{station['terminal_id']}, "
            f"File: "
            f"{station['file']}"
        )

    print("\n" + "=" * 72)
    print("UNPROCESSED RECORDS")
    print("=" * 72)

    if not unprocessed_records:
        print("None")

    for index, record in enumerate(
        unprocessed_records,
        start=1,
    ):
        print(f"\nRecord #{index}")
        print(f"Reason: {record['reason']}")
        print(
            f"Acquirer ID: "
            f"{record['acquirerId']}"
        )
        print(
            f"Operator ID: "
            f"{record['operatorId']}"
        )
        print(
            f"Terminal ID: "
            f"{record['terminalId']}"
        )
        print(
            f"Amount units: "
            f"{record['amountUnits']}"
        )


# ============================================================
# Main
# ============================================================

def main():
    if len(sys.argv) != 2:
        print(
            f"Usage: "
            f"{Path(sys.argv[0]).name} <input.json>"
        )

        sys.exit(1)

    json_path = Path(sys.argv[1])

    if not json_path.exists():
        print(
            f"[!] JSON file not found: "
            f"{json_path}"
        )

        sys.exit(1)

    try:
        with json_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

    except json.JSONDecodeError as error:
        print(
            f"[!] Invalid JSON: {error}"
        )

        sys.exit(1)

    ncmc_application = find_ncmc_application(
        data
    )

    if ncmc_application is None:
        print(
            "[!] NCMC application not found."
        )

        sys.exit(1)

    transactions = extract_transactions(
        ncmc_application
    )

    operators, _ = load_operators()

    # Print history before modifying CSV files.
    print_history(
        ncmc_application=ncmc_application,
        transactions=transactions,
        operators=operators,
    )

    (
        new_operators,
        new_stations,
        unprocessed_records,
        station_files_updated,
    ) = discover_and_update(
        transactions=transactions,
        operators=operators,
    )

    print_summary(
        new_operators=new_operators,
        new_stations=new_stations,
        unprocessed_records=unprocessed_records,
        station_files_updated=station_files_updated,
    )


if __name__ == "__main__":
    main()