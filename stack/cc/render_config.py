#!/usr/bin/env python3
"""Render a ${VAR} JSON template from the environment.

Exit codes: 0 rendered; 2 a placeholder had no environment variable;
3 the rendered text is not valid JSON; 4 a placeholder is not ${NAME}.
Values are never printed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from string import Template


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render_config.py TEMPLATE OUTPUT", file=sys.stderr)
        return 1
    template_path, output_path = Path(argv[1]), Path(argv[2])
    template = Template(template_path.read_text())
    try:
        rendered = template.substitute(os.environ)
    except KeyError as missing:
        print(f"render_config: missing environment variable {missing.args[0]}", file=sys.stderr)
        return 2
    except ValueError as bad:
        # string.Template has no ${VAR:-default}. Writing one is an easy mistake — it is
        # what docker compose uses two lines away in the same stack — and it used to come
        # out as an uncaught traceback in a container that then restarted forever.
        print(f"render_config: {bad}. Placeholders are ${{NAME}} only; there is no default syntax here, " f"so give the variable a value in the environment instead.", file=sys.stderr)
        return 4
    try:
        json.loads(rendered)
    except json.JSONDecodeError as err:
        print(f"render_config: rendered template is not valid JSON at line {err.lineno}", file=sys.stderr)
        return 3
    output_path.write_text(rendered)
    os.chmod(output_path, 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
