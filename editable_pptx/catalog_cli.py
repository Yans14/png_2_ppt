from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service_config import ServiceSettings
from .service_ops import render_all_slides
from .template_catalog import TemplateCatalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="editable-pptx-templates")
    parser.add_argument("--home", help="Service home; defaults to EDITABLE_PPTX_HOME")
    commands = parser.add_subparsers(dest="command", required=True)
    import_command = commands.add_parser("import")
    import_command.add_argument("pptx")
    import_command.add_argument("--name")
    commands.add_parser("list")
    commands.add_parser("families")
    delete_command = commands.add_parser("delete")
    delete_command.add_argument("template_id")
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("template_id")
    commands.add_parser("reindex")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = ServiceSettings()
    if args.home:
        settings.home = Path(args.home).expanduser()
    catalog = TemplateCatalog(settings.template_catalog_home)
    if args.command == "import":
        source = Path(args.pptx).expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="editable-pptx-template-cli-") as temporary:
            previews = render_all_slides(source, Path(temporary) / "render")
            template, duplicate = catalog.import_deck(
                source,
                preview_paths=[item for item in previews if item.suffix == ".png"],
                name=args.name,
            )
        payload = {"template": template.model_dump(mode="json"), "duplicate": duplicate}
    elif args.command == "list":
        payload = [item.model_dump(mode="json") for item in catalog.list_templates()]
    elif args.command == "families":
        payload = [item.model_dump(mode="json") for item in catalog.list_families()]
    elif args.command == "delete":
        payload = catalog.soft_delete(args.template_id).model_dump(mode="json")
    elif args.command == "restore":
        payload = catalog.restore(args.template_id).model_dump(mode="json")
    else:
        payload = catalog.reindex()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
