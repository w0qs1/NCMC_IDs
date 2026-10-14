name: Add or update station
description: Add a station or reader ID, or correct an existing mapping
title: "[Station] <operator> - <station name>"
labels: ["station"]
body:
  - type: markdown
    attributes:
      value: |
        Before filing, check whether the reader ID is already covered, including by a wildcard mask such as `0x123???`. Run `python scripts/ncmc_db.py find "<operator>" <reader ID>` or search `export/stations.csv`.

        Only stations that are **open to the public** can be added. Please do not estimate IDs for stations that have not opened.

        **Do not attach Metrodroid JSON dumps or card screenshots.** They contain personal data. Share only the IDs asked for below.
  - type: dropdown
    id: action
    attributes:
      label: What would you like to do?
      options:
        - Add a new station / reader ID
        - Correct the name of an existing station
        - Correct the reader ID of an existing station
        - Add or change the lines of a station
        - Remove a station that has permanently closed
    validations:
      required: true
  - type: input
    id: operator
    attributes:
      label: Operator
      description: Name or 6-digit ID of an operator that is already in the database. If it is not there yet, please file "Add new operator" first.
      placeholder: Hyderabad Metro (0x043630)
    validations:
      required: true
  - type: input
    id: station_name
    attributes:
      label: Station name
      description: Official name in Title Case, without co-branding (e.g. `Ameerpet`, not `Phonepe Ameerpet`). Shorten very long names and say so in the comments. Drop the "Metro" suffix unless a railway station, major bus station or airport shares the name.
      placeholder: Ameerpet
    validations:
      required: true
  - type: input
    id: reader_id
    attributes:
      label: Reader ID
      description: 3-byte reader ID as `0x` plus 6 characters (hex digits, or `?` for any digit), e.g. `0x001140` or `0x126???`. Leave empty if the station is known but its ID has not been decoded. Please do not invent IDs.
      placeholder: "0x126???"
  - type: input
    id: lines
    attributes:
      label: Line(s)
      description: Lines the operator officially lists the station under; separate with commas for an interchange. Ignore inter-corridor (through) services. Leave empty if you cannot tell.
      placeholder: Blue Line, Green Line
  - type: input
    id: current_entry
    attributes:
      label: Existing entry (updates and removals only)
      description: Current station name and reader ID in the database.
  - type: dropdown
    id: evidence
    attributes:
      label: Evidence
      options:
        - Directly observed on an NCMC card
        - Estimated from known IDs, line map or station sequence
        - Name or line from the operator's official map only
        - Other (explain below)
    validations:
      required: true
  - type: textarea
    id: observed
    attributes:
      label: Observed reader IDs (optional)
      description: Full reader IDs you saw, so reviewers can check the mask. Station sequence alone is not enough to confirm an ID.
      placeholder: |
        0x126001
        0x126002
  - type: textarea
    id: notes
    attributes:
      label: Notes
      description: Mark anything uncertain (estimated ID, shortened name, masked gate, unverified name). Explain why when correcting an existing mapping.
  - type: checkboxes
    id: checks
    attributes:
      label: Checklist
      options:
        - label: The station is open to the public today.
          required: true
        - label: The reader ID is not already covered by an existing exact entry or wildcard mask (adds only).
          required: true
        - label: I have not attached any personal data (card number, UID, name, balance, raw JSON dump).
          required: true
