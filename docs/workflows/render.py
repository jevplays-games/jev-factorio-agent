"""Split the complete Mermaid workflow and render high-resolution PNGs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET


SECTIONS = {
    "SUP": "01-campaign-supervision",
    "CTRL": "02-hierarchical-controller",
    "GOAL_TREE": "03-goals-and-bootstrap",
    "FACTORY": "04-factory-dependency-planner",
    "JEV": "05-jev-candidate-judgment",
    "NATIVE": "06-native-action-execution",
    "VERIFY": "07-pending-action-verification",
    "REPAIR": "08-automatic-codex-repair",
}
NODE = re.compile(r'\b([A-Z][A-Z0-9_]*)\s*(\["[^"]*"\]|\{"[^"]*"\})')


def split_sections(directory: Path, only: str | None = None, *,
                   output_directory: Path | None = None) -> list[Path]:
    if only is not None and only not in {"00-complete-workflow", *SECTIONS.values()}:
        raise ValueError("--only must name an existing workflow source")
    complete = directory / "mmd/00-complete-workflow.mmd"
    source = complete.read_text(encoding="utf-8")
    declared = NODE.findall(source)
    definitions = dict(declared)
    if len(declared) != len(definitions):
        raise ValueError("Workflow node IDs must be unique across sections")
    palette = "\n".join(line for line in source.splitlines() if line.startswith("classDef "))
    paths = [complete]
    for section, filename in SECTIONS.items():
        if only is not None and filename != only:
            continue
        match = re.search(
            rf"^subgraph {section}\[.*?^end\s*$", source, re.MULTILINE | re.DOTALL
        )
        if match is None:
            raise ValueError(f"Missing workflow section: {section}")
        body = match.group()
        local = {name for name, _ in NODE.findall(body)}
        without_labels = re.sub(r'"[^"]*"|\|[^|]*\|', "", body)
        referenced = set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", without_labels))
        external = sorted((referenced & definitions.keys()) - local)
        continuation = "\n".join(
            f"    {name}{definitions[name]}" for name in external
        )
        if external:
            continuation = (
                "\n" + continuation + "\n"
                + f"class {','.join(external)} continuation;\n"
            )
        section_style = "\n".join(line for line in source.splitlines()
                                  if line.startswith(f"style {section} "))
        path = (output_directory or directory) / "mmd" / f"{filename}.mmd"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(("flowchart TD\n\n" + body + "\n" + continuation
                         + "\n" + palette + "\n" + section_style).rstrip() + "\n",
                        encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def current_manifest_records(directory: Path, records: list[dict]) -> list[dict]:
    """Retain only records matching the currently published source and theme."""
    config_digest = hashlib.sha256((directory / "mermaid-config.json").read_bytes()).hexdigest()
    return [entry for entry in records
            if (directory / entry["source"]).is_file()
            and entry.get("source_sha256") == hashlib.sha256(
                (directory / entry["source"]).read_bytes()).hexdigest()
            and entry.get("config_sha256") == config_digest]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mmdc")
    parser.add_argument("--browser")
    parser.add_argument("--split-only", action="store_true",
                        help="Regenerate section sources without launching a browser")
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--only", help="Render just this source filename stem")
    arguments = parser.parse_args()
    if not math.isfinite(arguments.scale) or arguments.scale <= 0:
        parser.error("--scale must be positive and finite")
    if arguments.only and arguments.only not in {"00-complete-workflow", *SECTIONS.values()}:
        parser.error("--only must name an existing workflow source")
    if not arguments.split_only and (not arguments.mmdc or not arguments.browser):
        parser.error("--mmdc and --browser are required for rendering")
    directory = Path(__file__).resolve().parent
    if arguments.split_only:
        manifest = directory / "pngs/manifest.json"
        records = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
        if records is not None:
            current_manifest_records(directory, records)  # Validate inputs before source publication.
        # Parse and stage all selected derivatives before publishing any of
        # them. Splitting updates sources, so invalidate stale image evidence.
        scratch_root = directory.parents[1] / "runs/mermaid-render/tmp"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="split-", dir=scratch_root) as temporary:
            sources = split_sections(directory, arguments.only, output_directory=Path(temporary))
            for source in sources:
                if source.stem != "00-complete-workflow":
                    shutil.copyfile(source, directory / "mmd" / source.name)
        if records is not None:
            retained = current_manifest_records(directory, records)
            if retained != records:
                manifest.write_text(json.dumps(retained, indent=2) + "\n", encoding="utf-8", newline="\n")
        return
    mmdc = Path(arguments.mmdc).resolve()
    # Invoke the Node entry point directly on Windows, avoiding cmd quoting of
    # paths containing spaces. The entry also anchors Puppeteer's module lookup.
    if mmdc.suffix.lower() in {".cmd", ".bat"}:
        mmdc = mmdc.parent.parent / "@mermaid-js/mermaid-cli/src/cli.js"
    command = ["node", str(mmdc)] if mmdc.suffix == ".js" else [str(mmdc)]
    config = json.loads((directory / "mermaid-config.json").read_text(encoding="utf-8"))
    background = config["themeVariables"].get("background", "white")
    workspace = directory.parents[1]
    scratch_root = workspace / "runs/mermaid-render/tmp"
    scratch_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="render-", dir=scratch_root))
    # Stage derivatives and images together. A failed child render must leave
    # the published sources, images and manifest at their previous revision.
    sources = split_sections(directory, arguments.only, output_directory=temporary / "sources")
    output = directory / "pngs"
    output.mkdir(exist_ok=True)
    vectors = directory / "svgs"
    vectors.mkdir(exist_ok=True)
    staged_output = temporary / "pngs"
    staged_output.mkdir()
    staged_vectors = temporary / "svgs"
    staged_vectors.mkdir()
    publications = []
    rendered_records = []
    browser_config = temporary / "puppeteer.json"
    browser_config.write_text(json.dumps({
        "executablePath": str(Path(arguments.browser).resolve()),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        "timeout": 120000,
        "protocolTimeout": 240000,
    }))
    environment = {**os.environ, "TMPDIR": str(temporary)}
    libraries = workspace / "runs/mermaid-render/browser-libs"
    if libraries.exists():
        environment["LD_LIBRARY_PATH"] = os.pathsep.join([
            str(libraries / "usr/lib/x86_64-linux-gnu"),
            str(libraries / "lib/x86_64-linux-gnu"),
            environment.get("LD_LIBRARY_PATH", ""),
        ])
        fonts = ET.Element("fontconfig")
        ET.SubElement(fonts, "dir").text = str(libraries / "usr/share/fonts")
        ET.SubElement(fonts, "cachedir").text = str(temporary / "font-cache")
        font_config = temporary / "fonts.conf"
        ET.ElementTree(fonts).write(font_config, encoding="utf-8", xml_declaration=True)
        environment["FONTCONFIG_FILE"] = str(font_config)
    manifest = output / "manifest.json"
    records = json.loads(manifest.read_text(encoding="utf-8")) if arguments.only and manifest.exists() else []
    # A partial render must not republish stale evidence for an edited overview
    # or shared theme, even though it leaves unselected derivatives untouched.
    records = current_manifest_records(directory, records)
    for source in sources:
        if arguments.only and source.stem != arguments.only:
            continue
        config_path = directory / "mermaid-config.json"
        viewport_width = 2400
        if source.stem == "00-complete-workflow":
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["flowchart"]["useMaxWidth"] = True
            config_path = temporary / "complete-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
            viewport_width = 12000
        common = command + [
            "-i", str(source),
            "-c", str(config_path),
            "-p", str(browser_config), "-b", background,
            "-w", str(viewport_width), "-H", "1600",
        ]
        vector = temporary / f"{source.stem}.svg"
        subprocess.run(common + ["-o", str(vector)], check=True, env=environment, timeout=300)
        published_vector = staged_vectors / vector.name
        shutil.copyfile(vector, published_vector)
        viewbox = ET.parse(vector).getroot().attrib["viewBox"].split()
        width, height = map(float, viewbox[2:])
        scale = min(
            arguments.scale, 30000 / max(width, height),
            math.sqrt(160_000_000 / (width * height)),
        )
        raster = staged_output / f"{source.stem}.png"
        from PIL import Image

        dimensions = (math.ceil(width * scale), math.ceil(height * scale))
        tiles = temporary / f"{source.stem}-tiles"
        subprocess.run([
            "node", str(directory / "render-svg-tiles.mjs"),
            str(mmdc), str(vector), str(browser_config),
            str(tiles), str(dimensions[0]), str(dimensions[1]), background,
        ], check=True, env=environment, timeout=600)
        image = Image.new("RGB", dimensions, background)
        for tile in json.loads((tiles / "tiles.json").read_text()):
            with Image.open(tiles / tile["filename"]) as part:
                image.paste(part, (tile["left"], tile["top"]))
        image.save(raster)
        image.close()
        with raster.open("rb") as stream:
            header = stream.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Invalid PNG: {raster}")
        pixels = struct.unpack(">II", header[16:24])
        record = {
            "source": "mmd/" + source.name,
            "png": "pngs/" + raster.name,
            "svg": "svgs/" + published_vector.name,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256((directory / "mermaid-config.json").read_bytes()).hexdigest(),
            "effective_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "png_sha256": hashlib.sha256(raster.read_bytes()).hexdigest(),
            "svg_sha256": hashlib.sha256(published_vector.read_bytes()).hexdigest(),
            "width": pixels[0], "height": pixels[1],
            "scale": scale, "bytes": raster.stat().st_size,
        }
        records = [entry for entry in records if entry["source"] != record["source"]]
        records.append(record)
        if source.stem != "00-complete-workflow":
            publications.append((source, directory / "mmd" / source.name))
        publications.extend(((raster, output / raster.name),
                             (published_vector, vectors / published_vector.name)))
        rendered_records.append(record)
    for staged, published in publications:
        shutil.copyfile(staged, published)
    records.sort(key=lambda entry: entry["source"])
    manifest.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8", newline="\n")
    for record in rendered_records:
        print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
