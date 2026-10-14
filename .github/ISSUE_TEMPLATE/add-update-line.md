name: Add or update line
description: Add a new line, or change the name or colour of an existing one
title: "[Line] <operator> - <line name>"
labels: ["line"]
body:
  - type: markdown
    attributes:
      value: |
        Only lines that are **open to the public** can be added. Do not request a line that is under construction, being tested or announced, and do not change an existing station's lines until the new section opens.

        Check `export/lines.csv` (or run `python scripts/ncmc_db.py lines "<operator>"`) before filing.
  - type: dropdown
    id: action
    attributes:
      label: What would you like to do?
      options:
        - Add a new line
        - Update the name of an existing line
        - Update the colour of an existing line
        - Remove a line that has permanently closed
    validations:
      required: true
  - type: input
    id: operator
    attributes:
      label: Operator
      description: Name or 6-digit ID of an operator that is already in the database.
      placeholder: Chennai Metro (0x0B177D)
    validations:
      required: true
  - type: input
    id: line_name
    attributes:
      label: Line name
      description: Public name as shown on maps and signage, in Title Case, matching the operator's naming style. For an update, give the new name.
      placeholder: Blue Line
    validations:
      required: true
  - type: input
    id: current_name
    attributes:
      label: Current line name (updates and removals only)
      description: How the line is named in the database today.
  - type: input
    id: colour
    attributes:
      label: Colour
      description: RGB hex value from the operator's official map, e.g. `#0072BC`. Leave empty if you are not sure; please do not guess.
      placeholder: "#0072BC"
  - type: input
    id: source
    attributes:
      label: Source
      description: Link to the operator's official line map or announcement that shows the name, colour and opening.
    validations:
      required: true
  - type: textarea
    id: stations
    attributes:
      label: Open stations on the line (optional)
      description: Stations that are open today and officially listed under this line. Interchanges should list every open line. Ignore inter-corridor (through) services.
  - type: textarea
    id: notes
    attributes:
      label: Notes
      description: Date the section opened, reason for a change or removal, and any doubts.
  - type: checkboxes
    id: checks
    attributes:
      label: Checklist
      options:
        - label: At least part of this line is open to passengers today.
          required: true
        - label: The line does not already exist for this operator under the same name (adds only).
          required: true
        - label: I have not attached any personal data.
          required: true
