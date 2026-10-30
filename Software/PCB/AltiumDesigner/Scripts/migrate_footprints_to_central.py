#!/usr/bin/env python3
"""
Migrate Database *.xlsx Footprint Path to central package-type PcbLib libraries,
and copy missing footprints from the previous per-category libraries.
"""

from __future__ import annotations

import copy
import glob
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "ad_connector_composer" / "src"))

from ad_connector_composer import (  # noqa: E402
    _ole_read_all,
    _writer_add_tree,
    parse_height_from_params,
    read_u32,
    update_component_params_toc,
    update_library_data,
    write_u32,
)
from compoundfiles import CompoundFileWriter  # noqa: E402

ELECTRONICS = Path(r"C:\Electronics")
CENTRAL = ELECTRONICS / "Software" / "PCB" / "AltiumDesigner" / "Components" / "Footprints"
REL_PREFIX = r"..\..\..\..\Software\PCB\AltiumDesigner\Components\Footprints"

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^CAPC", re.I), "CAPC"),
    (re.compile(r"^RESC", re.I), "RESC"),
    (re.compile(r"^DO[-_]", re.I), "DO"),
    (re.compile(r"^SOD[-_]", re.I), "SOD"),
    (re.compile(r"^SOT[-_]", re.I), "SOT"),
    (re.compile(r"^TO[-_]", re.I), "TO"),
    (re.compile(r"^(LQFP|TQFP|PQFP|QFP)([-_]|$)", re.I), "QFP"),
    (re.compile(r"^(QFN|UQFN|UFQFPN|AQFN|VQFN)([-_(]|$)", re.I), "QFN"),
    (re.compile(r"^(DFN|WFDFN|VDFPN|UDFN)([-_(]|$)", re.I), "DFN"),
    (re.compile(r"^(SON|WSON|VSON|USON)([-_(]|$)", re.I), "SON"),
    (re.compile(r"^SOIC", re.I), "SOIC"),
    (re.compile(r"^(SOP|SSOP|TSSOP|MSOP|HTSSOP|QSOP|VSOP)([-_]|$)", re.I), "SOP"),
    (re.compile(r"^BGA", re.I), "BGA"),
    (re.compile(r"^LGA", re.I), "LGA"),
]

SKIP_STORAGE = {"Library", "FileHeader", "FileVersionInfo"}


def map_ref(ref: str) -> str | None:
    for rx, lib in RULES:
        if rx.search(ref):
            return lib
    return None


def footprint_names(tree: dict) -> set[str]:
    return {
        n
        for n, v in tree.items()
        if isinstance(v, dict) and "Data" in v and n not in SKIP_STORAGE
    }


def parse_models_data(blob: bytes) -> list[dict]:
    entries: list[dict] = []
    pos = 0
    while pos + 4 <= len(blob):
        ln, pos = read_u32(blob, pos)
        if ln < 0 or pos + ln > len(blob):
            break
        chunk = blob[pos : pos + ln]
        pos += ln
        text = chunk.rstrip(b"\x00").decode("windows-1252", errors="replace")
        m = re.search(r"ID=(\{[^}]+\})", text)
        entries.append({"raw": chunk, "id": m.group(1) if m else None})
    return entries


def model_ids_in_footprint(data: bytes) -> set[str]:
    return {
        m.group(1).decode("ascii")
        for m in re.finditer(br"MODELID=(\{[^}]+\})", data)
    }


def ensure_models(dst_tree: dict, src_tree: dict, needed_ids: set[str]) -> list[str]:
    models = dst_tree["Library"].setdefault("Models", {})
    if not isinstance(models, dict):
        return []
    dst_entries = parse_models_data(models.get("Data", b""))
    have = {e["id"] for e in dst_entries if e["id"]}
    src_models = src_tree.get("Library", {}).get("Models", {})
    if not isinstance(src_models, dict):
        return [f"no src models for {sorted(needed_ids)}"]
    src_entries = parse_models_data(src_models.get("Data", b""))
    src_by_id = {e["id"]: (i, e) for i, e in enumerate(src_entries) if e["id"]}
    log: list[str] = []
    for mid in sorted(needed_ids):
        if mid in have:
            continue
        if mid not in src_by_id:
            log.append(f"model {mid} missing in source")
            continue
        idx, entry = src_by_id[mid]
        num_keys = sorted(int(k) for k in models if str(k).isdigit())
        new_idx = (max(num_keys) + 1) if num_keys else 0
        bin_key = str(idx)
        if bin_key not in src_models:
            log.append(f"model binary {bin_key} missing for {mid}")
            continue
        models[str(new_idx)] = src_models[bin_key]
        dst_entries.append(entry)
        have.add(mid)
        log.append(f"model {mid} -> {new_idx}")
    blob = b"".join(write_u32(len(e["raw"])) + e["raw"] for e in dst_entries)
    models["Data"] = blob
    models["Header"] = write_u32(len(dst_entries))
    return log


def pad_count_from_toc(src_tree: dict, name: str) -> int:
    toc = src_tree.get("Library", {}).get("ComponentParamsTOC")
    if not (isinstance(toc, dict) and isinstance(toc.get("Data"), (bytes, bytearray))):
        return 0
    plen, _ = read_u32(toc["Data"], 0)
    text = toc["Data"][4 : 4 + plen].decode("windows-1252", errors="replace")
    for ln in text.split("\r\n"):
        if ln.startswith(f"Name={name}|"):
            m = re.search(r"Pad Count=([^|]*)", ln)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return 0
    return 0


def copy_footprint(dst_tree: dict, src_tree: dict, name: str) -> tuple[str, list[str]]:
    if name in footprint_names(dst_tree):
        return "exists", []
    if name not in src_tree or not isinstance(src_tree[name], dict):
        return "missing_in_source", []
    dst_tree[name] = copy.deepcopy(src_tree[name])
    data = src_tree[name].get("Data", b"")
    model_log = ensure_models(dst_tree, src_tree, model_ids_in_footprint(data))
    dst_tree["Library"]["Data"] = update_library_data(dst_tree["Library"]["Data"], name)
    toc = dst_tree["Library"].get("ComponentParamsTOC")
    if isinstance(toc, dict) and "Data" in toc:
        height = parse_height_from_params(src_tree[name].get("Parameters", b""))
        pads = pad_count_from_toc(src_tree, name)
        toc_new, nlines = update_component_params_toc(toc["Data"], name, pads, height)
        toc["Data"] = toc_new
        toc["Header"] = write_u32(nlines)
    return "copied", model_log


def write_overwrite(tree: dict, dst: Path) -> Path:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    with CompoundFileWriter(str(tmp)) as writer:
        _writer_add_tree(writer, writer.root, tree)
    bak = dst.with_suffix(dst.suffix + ".bak")
    if bak.exists():
        bak.unlink()
    shutil.move(str(dst), str(bak))
    shutil.move(str(tmp), str(dst))
    return bak


def collect_rows() -> tuple[list[dict], list[dict]]:
    files = [
        f
        for f in glob.glob(str(ELECTRONICS / "**" / "Database" / "*.xlsx"), recursive=True)
        if not os.path.basename(f).startswith("~$")
    ]
    mapped: list[dict] = []
    unmapped: list[dict] = []
    for f in files:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        try:
            pi = headers.index("Footprint Path")
            ri = headers.index("Footprint Ref")
        except ValueError:
            wb.close()
            continue
        db = Path(f).parent
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            p = row[pi] if pi < len(row) else None
            r = row[ri] if ri < len(row) else None
            if not r:
                continue
            ref = str(r)
            lib = map_ref(ref)
            abs_p = os.path.normpath(str(db / p)) if p else None
            item = {
                "xlsx": f,
                "row": row_idx,
                "ref": ref,
                "old_rel": p,
                "old_abs": abs_p,
                "lib": lib,
            }
            if lib:
                mapped.append(item)
            else:
                unmapped.append(item)
        wb.close()
    return mapped, unmapped


def main() -> int:
    dry = "--dry-run" in sys.argv
    mapped, unmapped = collect_rows()

    # unique copies needed: ref -> (target_lib, candidate source abs paths)
    sources_for_ref: dict[str, set[str]] = defaultdict(set)
    target_for_ref: dict[str, str] = {}
    for item in mapped:
        target_for_ref[item["ref"]] = item["lib"]
        if item["old_abs"]:
            sources_for_ref[item["ref"]].add(item["old_abs"])

    # cache loaded trees
    tree_cache: dict[str, dict] = {}
    dirty_targets: dict[str, dict] = {}

    def load(path: str) -> dict:
        if path not in tree_cache:
            tree_cache[path] = _ole_read_all(Path(path))
        return tree_cache[path]

    print("=== Footprint presence / copy plan ===")
    copy_actions: list[tuple[str, str, str]] = []  # ref, target, source
    for ref, lib in sorted(target_for_ref.items()):
        target_path = str(CENTRAL / f"{lib}.PcbLib")
        if not os.path.exists(target_path):
            print(f"ERROR: missing central lib {target_path}")
            continue
        dst = load(target_path)
        if ref in footprint_names(dst):
            print(f"OK exists: {ref} in {lib}.PcbLib")
            continue
        src_path = None
        for cand in sorted(sources_for_ref[ref]):
            if not os.path.exists(cand):
                continue
            try:
                src = load(cand)
            except Exception as exc:  # noqa: BLE001
                print(f"  warn cannot read {cand}: {exc}")
                continue
            if ref in footprint_names(src):
                src_path = cand
                break
        if not src_path:
            print(f"FAIL no source for missing {ref} -> {lib}.PcbLib")
            print(f"     candidates: {sorted(sources_for_ref[ref])}")
            continue
        print(f"COPY {ref}: {src_path} -> {lib}.PcbLib")
        copy_actions.append((ref, target_path, src_path))

    if not dry:
        for ref, target_path, src_path in copy_actions:
            if target_path not in dirty_targets:
                dirty_targets[target_path] = copy.deepcopy(load(target_path))
            status, model_log = copy_footprint(
                dirty_targets[target_path], load(src_path), ref
            )
            print(f"  {status}: {ref} models={model_log}")

        for target_path, tree in dirty_targets.items():
            bak = write_overwrite(tree, Path(target_path))
            print(f"Wrote {target_path} (backup {bak.name})")
            # refresh cache
            tree_cache[target_path] = _ole_read_all(Path(target_path))

    # Update xlsx paths
    by_xlsx: dict[str, list[dict]] = defaultdict(list)
    for item in mapped:
        by_xlsx[item["xlsx"]].append(item)

    print("\n=== XLSX path updates ===")
    xlsx_updated = 0
    cells_updated = 0
    for xlsx, items in sorted(by_xlsx.items()):
        if dry:
            for it in items:
                new_rel = f"{REL_PREFIX}\\{it['lib']}.PcbLib"
                if it["old_rel"] != new_rel:
                    print(f"WOULD {os.path.basename(xlsx)} row {it['row']}: {it['ref']}")
                    print(f"  {it['old_rel']}")
                    print(f"  -> {new_rel}")
                    cells_updated += 1
            continue
        try:
            wb = openpyxl.load_workbook(xlsx)
        except PermissionError:
            print(f"LOCKED skip: {xlsx}")
            continue
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        try:
            pi = headers.index("Footprint Path") + 1  # 1-based
            ri = headers.index("Footprint Ref") + 1
        except ValueError:
            wb.close()
            continue
        changed = False
        for it in items:
            new_rel = f"{REL_PREFIX}\\{it['lib']}.PcbLib"
            # verify still same ref on that row
            ref_cell = ws.cell(it["row"], ri).value
            if str(ref_cell) != it["ref"]:
                # fallback: scan
                found = False
                for row in range(2, ws.max_row + 1):
                    if str(ws.cell(row, ri).value) == it["ref"]:
                        old = ws.cell(row, pi).value
                        if old != new_rel:
                            ws.cell(row, pi).value = new_rel
                            cells_updated += 1
                            changed = True
                        found = True
                        break
                if not found:
                    print(f"  warn ref not found in {xlsx}: {it['ref']}")
                continue
            old = ws.cell(it["row"], pi).value
            if old != new_rel:
                ws.cell(it["row"], pi).value = new_rel
                cells_updated += 1
                changed = True
        if changed:
            try:
                wb.save(xlsx)
                xlsx_updated += 1
                print(f"Updated {xlsx}")
            except PermissionError:
                print(f"LOCKED skip save: {xlsx}")
        wb.close()

    print("\n=== UNMAPPED (path left unchanged) ===")
    seen = set()
    for it in unmapped:
        key = (it["ref"], it["old_rel"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {it['ref']}  <-  {it['old_rel']}")

    print(
        f"\nDone. dry_run={dry} copies={len(copy_actions)} "
        f"xlsx_files={xlsx_updated} cells={cells_updated} unmapped_unique={len(seen)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
