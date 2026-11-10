"""Build STEP variants of the part that shares this script's name.

The script, the part, and the parameter table sit in one folder:

    Discrete_Capacitors_Ceramic_SMD.py
    Discrete_Capacitors_Ceramic_SMD.ipt
    Discrete_Capacitors_Ceramic_SMD.xlsx

The table has one row per variant. Column File is the STEP name to write.
Other columns, except Model, are Inventor parameters. Sheet Parameters
holds the type of each parameter. One run writes every row.

Existing STEP files whose name starts with the part name are sent to the
Recycle Bin before the new ones are written. The master .ipt is not saved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import win32com.client
from openpyxl import load_workbook
from win32com.shell import shell, shellcon

STEP_TRANSLATOR_ID = "{90AF7F40-0C01-11D5-8E83-0010B541CD80}"
K_FILE_BROWSE_IO = 13059
STEP_AP214 = 3
K_PART_DOCUMENT = 12290
K_ASSEMBLY_DOCUMENT = 12291
SKIP_COLUMNS = {"model", "file", "font"}
DEFAULT_FONT = "GOST type A"


def as_editable_document(doc):
    kind = int(doc.DocumentType)
    if kind == K_PART_DOCUMENT:
        return win32com.client.CastTo(doc, "PartDocument")
    if kind == K_ASSEMBLY_DOCUMENT:
        return win32com.client.CastTo(doc, "AssemblyDocument")
    raise RuntimeError(f"unsupported Inventor document type {kind}")


def inventor_is_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Inventor.exe", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "Inventor.exe" in result.stdout


def connect_inventor():
    was_running = inventor_is_running()
    try:
        app = win32com.client.GetActiveObject("Inventor.Application")
    except Exception:
        app = win32com.client.Dispatch("Inventor.Application")
        app.Visible = True
    started = not was_running
    deadline = time.time() + 120
    last_error = None
    while time.time() < deadline:
        try:
            _ = app.Caption
            return app, started
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"Inventor did not become ready: {last_error}")


def find_parameter(doc, name: str):
    try:
        return doc.ComponentDefinition.Parameters.Item(name)
    except Exception:
        return None


def expression_for(param, type_code: str, value: str) -> str:
    units = ""
    try:
        units = str(param.Units or "")
    except Exception:
        units = ""
    kind = type_code.strip().lower()
    units_key = units.strip().lower()
    if kind == "string" or units_key == "text":
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    if kind == "boolean" or units_key in {"boolean", "bool"}:
        return "True" if value.strip().lower() in {"true", "1", "yes"} else "False"
    if not value:
        raise ValueError("empty expression")
    return value


def parameter_value(param) -> str:
    try:
        value = param.Value
    except Exception:
        value = param.Expression
    return str(value).strip().strip('"')


def sketch_texts(doc) -> dict[str, list]:
    grouped: dict[str, list] = {}
    sketches = doc.ComponentDefinition.Sketches
    for index in range(1, sketches.Count + 1):
        sketch = sketches.Item(index)
        for text_index in range(1, sketch.TextBoxes.Count + 1):
            text_box = sketch.TextBoxes.Item(text_index)
            label = str(text_box.Text).strip()
            if label:
                grouped.setdefault(label, []).append(text_box)
    return grouped


def font_size_literals(font_cm: float) -> list[str]:
    size = format(float(font_cm), ".8f").rstrip("0").rstrip(".")
    if "." not in size:
        size += ".0"
    return [size.replace(".", ","), size]


def set_text_font_cm(
    text_box, font_cm: float, label: str | None = None, font_name: str = DEFAULT_FONT
) -> None:
    label = str(text_box.Text if label is None else label)
    face = (font_name or DEFAULT_FONT).strip() or DEFAULT_FONT
    if any(char in face for char in "<>'\"&"):
        raise RuntimeError(f"font name cannot contain quotes or brackets: {face}")
    last_error = None
    for size in font_size_literals(font_cm):
        formatted = (
            "<StyleOverride Font='"
            + face
            + "' FontSize='"
            + size
            + "' Bold='True'>"
            + label
            + "</StyleOverride>"
        )
        try:
            text_box.FormattedText = formatted
            return
        except Exception as exc:
            last_error = exc
    raise last_error


def marking_owner(feature_name: str, marks: set[str]) -> str | None:
    if feature_name in marks:
        return feature_name
    owner = None
    for code in sorted(marks, key=len, reverse=True):
        if feature_name.startswith(code + "_") and (owner is None or len(code) > len(owner)):
            owner = code
    return owner


def contact_gap_cm(doc) -> float:
    """Clear span between the two end contacts: L - 2*BW, with BW = vl1 and vl2."""
    length = length_mm(doc, "vL")
    left = length_mm(doc, "vl1")
    right = length_mm(doc, "vl2")
    if length is None or left is None or right is None:
        raise RuntimeError("vL, vl1 and vl2 are required to fit the marking")
    gap_mm = length - left - right
    if gap_mm <= 0.05:
        raise RuntimeError(f"no room between contacts: L-2*BW = {gap_mm:.3g} mm")
    return gap_mm / 10.0


def fit_text_width(
    text_box, mark: str, font_cm: float, target_cm: float, font_name: str
) -> float:
    """Largest font whose text width still fits in the contact gap.

    Inventor rounds FontSize, so a proportional step can jump from just inside
    the gap to just outside it. A search keeps the largest size that fits.
    """
    low = target_cm / 30.0
    high = max(target_cm, font_cm if font_cm > 0 else target_cm)
    best = low
    for _ in range(14):
        mid = (low + high) / 2.0
        set_text_font_cm(text_box, mid, mark, font_name)
        width_cm = float(text_box.Width)
        if width_cm <= target_cm:
            best = mid
            low = mid
        else:
            high = mid
    set_text_font_cm(text_box, best, mark, font_name)
    return best


def apply_text_marking(doc, font_name: str) -> float | None:
    font = find_parameter(doc, "vFont")
    label = find_parameter(doc, "vTextSize")
    if font is None or label is None:
        return None
    mark = parameter_value(label)
    texts = sketch_texts(doc)
    if not mark or not texts:
        print(f"WARNING  no sketch text for vTextSize={mark!r}")
        return None

    gap_cm = contact_gap_cm(doc)
    font_cm = float(font.Value)
    if mark in texts:
        boxes = texts[mark]
        switch_features = len(texts) > 1
    elif len(texts) == 1:
        boxes = next(iter(texts.values()))
        switch_features = False
    else:
        print(f"WARNING  no sketch text for vTextSize={mark!r}")
        return None

    fitted = font_cm
    width_cm = 0.0
    for text_box in boxes:
        fitted = fit_text_width(text_box, mark, font_cm, gap_cm, font_name)
        width_cm = float(text_box.Width)
    font_mm = fitted * 10.0
    literal = f"{font_mm:.4f}".rstrip("0").rstrip(".")
    font.Expression = f"{literal} mm"
    print(
        f"TEXT  {mark}  face {font_name or DEFAULT_FONT}  font {font_mm:.3g} mm  "
        f"width {width_cm * 10:.3g} mm  gap {gap_cm * 10:.3g} mm"
    )
    if not switch_features:
        return font_mm

    features = doc.ComponentDefinition.Features
    marks = set(texts)
    for index in range(1, features.Count + 1):
        feature = features.Item(index)
        owner = marking_owner(str(feature.Name), marks)
        if owner is None:
            continue
        feature.Suppressed = owner != mark
    return font_mm


def apply_parameters(doc, rows: list[tuple[str, str, str]]) -> None:
    missing = []
    for name, type_code, value in rows:
        param = find_parameter(doc, name)
        if param is None:
            missing.append(name)
            continue
        expression = expression_for(param, type_code, value)
        try:
            param.Expression = expression
        except Exception as exc:
            raise RuntimeError(f"cannot set {name} = {expression}") from exc
    if missing:
        raise RuntimeError("parameters not found in the part: " + ", ".join(missing))


def export_step(app, doc, destination: Path) -> None:
    addin = app.ApplicationAddIns.ItemById(STEP_TRANSLATOR_ID)
    if not addin.Activated:
        addin.Activate()
    try:
        translator = win32com.client.CastTo(addin, "TranslatorAddIn")
    except Exception:
        translator = addin

    context = app.TransientObjects.CreateTranslationContext()
    context.Type = K_FILE_BROWSE_IO
    options = app.TransientObjects.CreateNameValueMap()
    if translator.HasSaveCopyAsOptions(doc, context, options):
        options.SetValue("ApplicationProtocolType", STEP_AP214)

    destination.parent.mkdir(parents=True, exist_ok=True)
    medium = app.TransientObjects.CreateDataMedium()
    medium.FileName = str(destination)
    translator.SaveCopyAs(doc, context, options, medium)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"STEP file was not written: {destination}")


def solid_count(doc) -> int:
    try:
        return int(doc.ComponentDefinition.SurfaceBodies.Count)
    except Exception:
        return 0


def length_mm(doc, name: str) -> float | None:
    param = find_parameter(doc, name)
    if param is None:
        return None
    return float(param.Value) * 10.0


def body_spans_mm(doc) -> list[tuple[str, list[float]]]:
    spans = []
    bodies = doc.ComponentDefinition.SurfaceBodies
    for index in range(1, bodies.Count + 1):
        body = bodies.Item(index)
        box = body.RangeBox
        span = sorted(
            (
                abs(box.MaxPoint.X - box.MinPoint.X) * 10.0,
                abs(box.MaxPoint.Y - box.MinPoint.Y) * 10.0,
                abs(box.MaxPoint.Z - box.MinPoint.Z) * 10.0,
            ),
            reverse=True,
        )
        spans.append((str(body.Name), span))
    return spans


def geometry_matches(doc) -> str:
    expected = [length_mm(doc, name) for name in ("vL", "vW", "vH")]
    if any(value is None for value in expected):
        return "no vL/vW/vH"
    target = sorted(expected, reverse=True)
    best = None
    for name, span in body_spans_mm(doc):
        error = sum(abs(span[i] - target[i]) for i in range(3))
        if best is None or error < best[0]:
            best = (error, name, span)
    if best is None or best[0] > 0.15:
        measured = ", ".join(
            f"{name} {' x '.join(f'{value:.3g}' for value in span)}"
            for name, span in body_spans_mm(doc)
        )
        raise RuntimeError(
            "part size did not follow vL/vW/vH "
            f"({' x '.join(f'{value:.3g}' for value in target)} mm). Bodies: {measured}"
        )
    return f"{best[1]} {' x '.join(f'{value:.3g}' for value in best[2])} mm"


def recycle(paths: list[Path]) -> None:
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return
    source = "\0".join(str(path.resolve()) for path in existing) + "\0\0"
    flags = (
        shellcon.FOF_ALLOWUNDO
        | shellcon.FOF_NOCONFIRMATION
        | shellcon.FOF_SILENT
        | shellcon.FOF_NOERRORUI
    )
    result = shell.SHFileOperation(
        (0, shellcon.FO_DELETE, source, None, flags, None, None)
    )
    code = result[0] if isinstance(result, tuple) else result
    aborted = result[1] if isinstance(result, tuple) and len(result) > 1 else False
    if code or aborted:
        raise RuntimeError(f"could not move files to the Recycle Bin ({code})")
    for path in existing:
        print(f"RECYCLE  {path.name}")


def read_variants(xlsx: Path) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    workbook = load_workbook(xlsx, data_only=True)
    types: dict[str, str] = {}
    if "Parameters" in workbook.sheetnames:
        sheet = workbook["Parameters"]
        header = [cell.value for cell in sheet[1]]
        name_index = header.index("Parameter")
        type_index = header.index("Type")
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if not row[name_index]:
                continue
            types[str(row[name_index]).strip()] = str(row[type_index] or "").strip()

    models = workbook["Models"]
    header = ["" if cell.value is None else str(cell.value).strip() for cell in models[1]]
    try:
        file_index = next(index for index, name in enumerate(header) if name.lower() == "file")
    except StopIteration as exc:
        raise RuntimeError(f"{xlsx.name} has no File column") from exc

    variants: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    font_index = next((index for index, name in enumerate(header) if name.lower() == "font"), None)
    for row in models.iter_rows(min_row=2, values_only=True):
        filename = row[file_index]
        if filename is None or not str(filename).strip():
            continue
        filename = str(filename).strip()
        if not filename.lower().endswith(".step"):
            filename += ".step"
        font_name = DEFAULT_FONT
        if font_index is not None and row[font_index] is not None and str(row[font_index]).strip():
            font_name = str(row[font_index]).strip()
        parameters: list[tuple[str, str, str]] = []
        for index, name in enumerate(header):
            if not name or name.lower() in SKIP_COLUMNS:
                continue
            value = row[index]
            if value is None or not str(value).strip():
                continue
            parameters.append((name, types.get(name, ""), str(value).strip()))
        if not parameters:
            raise RuntimeError(f"{filename} has no parameter values")
        variants.append((filename, font_name, parameters))
    if not variants:
        raise RuntimeError(f"no variants in {xlsx.name}")
    return variants


def write_font(xlsx: Path, filename: str, font_mm: float) -> None:
    workbook = load_workbook(xlsx)
    sheet = workbook["Models"]
    header = ["" if cell.value is None else str(cell.value).strip() for cell in sheet[1]]
    file_index = header.index("File") + 1
    font_index = header.index("vFont") + 1
    literal = f"{font_mm:.4f}".rstrip("0").rstrip(".") + " mm"
    for row in range(2, sheet.max_row + 1):
        current = sheet.cell(row, file_index).value
        if current is not None and str(current).strip() == filename:
            cell = sheet.cell(row, font_index, literal)
            cell.number_format = "@"
            break
    workbook.save(xlsx)


def export_variants(app, ipt: Path, xlsx: Path, variants: list[tuple[str, str, list[tuple[str, str, str]]]]) -> list[Path]:
    written: list[Path] = []
    work_dir = Path(tempfile.mkdtemp(prefix="ipt-step-"))
    work_ipt = work_dir / ipt.name
    shutil.copy2(ipt, work_ipt)
    signature = (ipt.stat().st_size, ipt.stat().st_mtime_ns)
    try:
        for filename, font_name, rows in variants:
            print(f"OPEN  {filename}")
            opened = app.Documents.Open(str(work_ipt), False)
            doc = as_editable_document(opened)
            try:
                apply_parameters(doc, rows)
                font_mm = apply_text_marking(doc, font_name)
                doc.Update2(True)
                if font_mm is not None:
                    write_font(xlsx, filename, font_mm)
                if solid_count(doc) < 1:
                    raise RuntimeError(f"{filename}: part has no solid body after rebuild")
                print(f"SIZE  {geometry_matches(doc)}")
                destination = ipt.parent / filename
                export_step(app, doc, destination)
                written.append(destination)
                print(f"STEP  {destination.name}")
            finally:
                doc.Close(True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        after = (ipt.stat().st_size, ipt.stat().st_mtime_ns)
        if after != signature:
            print(f"WARNING  master part changed on disk: {ipt}", file=sys.stderr)
    return written


def main() -> int:
    script = Path(__file__).resolve()
    folder = script.parent
    stem = script.stem
    ipt = folder / f"{stem}.ipt"
    xlsx = folder / f"{stem}.xlsx"
    if not ipt.is_file():
        print(f"part not found: {ipt.name}", file=sys.stderr)
        return 1
    if not xlsx.is_file():
        print(f"table not found: {xlsx.name}", file=sys.stderr)
        return 1

    variants = read_variants(xlsx)
    previous = sorted(
        path for path in folder.glob(f"{stem}*.step") if path.is_file()
    )
    recycle(previous)

    app, started = connect_inventor()
    previous_silent = bool(app.SilentOperation)
    app.SilentOperation = True
    written: list[Path] = []
    failed = False
    try:
        print(f"Inventor  {app.Caption}")
        print(f"PART  {ipt.name}")
        try:
            written = export_variants(app, ipt, xlsx, variants)
        except Exception as exc:
            failed = True
            print(f"FAILED  {ipt.name}: {exc}", file=sys.stderr)
            traceback.print_exc()
    finally:
        try:
            app.SilentOperation = previous_silent
        except Exception:
            pass
        if started:
            try:
                app.Quit()
            except Exception as exc:
                print(f"WARNING  could not close Inventor: {exc}", file=sys.stderr)

    print(f"done  {len(written)} STEP")
    return 1 if failed or not written else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
