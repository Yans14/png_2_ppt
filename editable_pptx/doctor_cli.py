from __future__ import annotations

import argparse
import json
import shutil
import sys

from .openai_responses import resolve_api_key
from .powerpoint import available_powerpoint_adapter
from .service_config import ServiceSettings
from .version import __version__


def capability_report() -> dict[str, object]:
    settings = ServiceSettings()
    optional_api = {}
    for module in ("fastapi", "uvicorn", "multipart", "httpx"):
        try:
            __import__(module)
            optional_api[module] = True
        except ImportError:
            optional_api[module] = False
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "node": shutil.which("node"),
        "libreoffice": shutil.which("soffice") or shutil.which("libreoffice"),
        "pdftoppm": shutil.which("pdftoppm"),
        "powerpoint_adapter": available_powerpoint_adapter(),
        "openai_key_configured": bool(resolve_api_key()),
        "api_dependencies": optional_api,
        "service_home": str(settings.home),
        "model": settings.model,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="editable-pptx-doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = capability_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    required = (
        report["node"],
        report["libreoffice"],
        report["pdftoppm"],
        report["openai_key_configured"],
        all(report["api_dependencies"].values()),
    )
    return 0 if all(required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
