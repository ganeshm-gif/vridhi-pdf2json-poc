# Production-Grade PDF-to-JSON Data Extraction Pipeline
# --------------------------------------------------------------------------------
#Architecture:
#   1. PDF Parsing: Extract text blocks (coordinates + bold) via PyMuPDF (fitz)
#   2. Table Parsing: Extract tables (coordinates + merged cells) via pdfplumber
#   3. Overlap Filter: Filter out text blocks residing inside table boundaries
#   4. Intermediate Save: Save raw pages data to raw_extracted.json
#   5. Markdown Render: Convert pages structure to markdown (with coordinate annotations)
#   6. LLM Integration: Query OpenRouter (gemini-2.5-flash) with retry logic
#   7. Validation: Validate response format via Pydantic v2
#   8. Final Save: Write validated structured JSON to output.json
# Standard library imports
import os
import sys
import re
import json
import argparse
from pathlib import Path

# Typing imports (used for type hints)
from typing import Dict, Any, List, Optional, Union

# Third‑party library imports
from dotenv import load_dotenv
import fitz  # PyMuPDF for PDF text extraction
import pdfplumber  # PDF table extraction
from openai import OpenAI  # LLM client
from pydantic import BaseModel, RootModel, ValidationError


# ─── Load Environment & Configure Client ─────────────────────────────────────
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("OPENROUTER_MODEL")

# Setup Logging
def log(msg: str):
    print(f"[*] {msg}", flush=True)

def log_error(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)

def extract_bold_from_slashes(text: str) -> str:
    """
    Extracts the bolded option from a slash-separated list,
    or returns the cleaned bold text if the entire string is bolded.
    """
    stripped = text.strip()
    if not stripped:
        return text

    if "**" not in stripped:
        return text

    # Case 1: The entire string is a single bold segment
    if stripped.startswith("**") and stripped.endswith("**") and stripped.count("**") == 2:
        return stripped[2:-2].strip()

    # Case 2: The string contains slashes, check if any slash-separated option is bolded
    if "/" in stripped:
        parts = stripped.split("/")
        bold_parts = []
        for p in parts:
            p_stripped = p.strip()
            if "**" in p_stripped:
                cleaned = p_stripped.replace("**", "").strip()
                if cleaned and any(c.isalnum() for c in cleaned):
                    bold_parts.append(cleaned)
        if bold_parts:
            return " / ".join(bold_parts)

    # Case 3: General fallback - extract all bolded segments and join them
    matches = re.findall(r'\*\*(.*?)\*\*', stripped)
    cleaned_matches = []
    for m in matches:
        cleaned = m.strip()
        if cleaned and any(c.isalnum() for c in cleaned):
            cleaned_matches.append(cleaned)
    if cleaned_matches:
        return " ".join(cleaned_matches)

    return stripped.replace("**", "").strip()

def process_bold_text(text: str) -> str:
    if not text:
        return text
    if ":" in text:
        parts = text.split(":", 1)
        key = parts[0]
        value = parts[1]
        processed_key = key.replace("**", "").strip()
        processed_val = extract_bold_from_slashes(value)
        return f"{processed_key} : {processed_val}"
    else:
        return extract_bold_from_slashes(text)

def clean_schema_template(schema: Any) -> Any:
    """
    Recursively sets all non-dictionary, non-list leaf node values to None.
    This creates a clean template of the schema with the exact structure.
    """
    if isinstance(schema, dict):
        return {k: clean_schema_template(v) for k, v in schema.items()}
    elif isinstance(schema, list):
        return [clean_schema_template(item) for item in schema]
    else:
        return None

def _normalize_llm_keys(schema_keys: set, llm_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes LLM output keys to match schema keys when possible.
    For example, if the schema has key 'FF' and the LLM returned 'FF(Proposed)',
    this maps 'FF(Proposed)' → 'FF' so the merge can match them.
    
    Rules:
    - Strip parenthetical suffixes: 'FF(Proposed)' → 'FF'
    - Strip bracketed suffixes: 'GF [Existing]' → 'GF'
    - Case-insensitive prefix matching: if the base matches a schema key, remap it.
    - If a normalized key already exists in llm_data, merge the dicts (if both are dicts)
      rather than overwriting.
    """
    normalized = {}
    for llm_key, llm_val in llm_data.items():
        if llm_key in schema_keys:
            # Exact match — keep as-is
            if llm_key in normalized and isinstance(normalized[llm_key], dict) and isinstance(llm_val, dict):
                normalized[llm_key].update(llm_val)
            else:
                normalized[llm_key] = llm_val
            continue

        # Try stripping parenthetical/bracketed suffixes to find a match
        base_key = re.sub(r'\s*[\(\[].*?[\)\]]', '', llm_key).strip()
        
        matched_schema_key = None
        if base_key in schema_keys:
            matched_schema_key = base_key
        else:
            # Case-insensitive match
            for sk in schema_keys:
                if sk.lower() == base_key.lower():
                    matched_schema_key = sk
                    break

        if matched_schema_key:
            # Merge into the schema key
            if matched_schema_key in normalized and isinstance(normalized[matched_schema_key], dict) and isinstance(llm_val, dict):
                normalized[matched_schema_key].update(llm_val)
            else:
                normalized[matched_schema_key] = llm_val
        else:
            # No match found — keep original key
            normalized[llm_key] = llm_val

    return normalized


def merge_and_update_schema(schema_template: Any, llm_data: Any) -> Any:
    """
    Recursively merges llm_data into schema_template.
    - Keys in schema_template are kept. If present in llm_data, recursively merge values.
    - Keys in llm_data that are NOT in schema_template are added (new keys from the document).
    - If both are lists, and the first element of schema_template is a dict, recursively
      merge each element of llm_data against that template dict.
    - Otherwise, return llm_data if llm_data is not None, else schema_template.
    - LLM keys with parenthetical suffixes (e.g. 'FF(Proposed)') are normalized to match
      schema keys (e.g. 'FF') before merging.
    """
    if isinstance(schema_template, dict) and isinstance(llm_data, dict):
        # Normalize LLM keys to match schema keys where possible
        llm_data = _normalize_llm_keys(set(schema_template.keys()), llm_data)
        
        merged = {}
        for k, template_val in schema_template.items():
            if k in llm_data:
                merged[k] = merge_and_update_schema(template_val, llm_data[k])
            else:
                merged[k] = template_val
        for k, llm_val in llm_data.items():
            if k not in schema_template:
                merged[k] = llm_val
        return merged
    elif isinstance(schema_template, list) and isinstance(llm_data, list):
        if schema_template and isinstance(schema_template[0], dict):
            template_item = schema_template[0]
            merged_list = []
            for item in llm_data:
                if isinstance(item, dict):
                    merged_list.append(merge_and_update_schema(template_item, item))
                else:
                    merged_list.append(item)
            return merged_list
        return llm_data
    else:
        return llm_data if llm_data is not None else schema_template


def find_value_by_key_patterns(data: Any, patterns: List[str]) -> Optional[Any]:
    """
    Recursively searches a JSON-like object for any key matching one of the patterns (case-insensitive substring)
    and returns its value if it is not None/empty/NA.
    """
    if isinstance(data, dict):
        # First check immediate keys
        for k, v in data.items():
            k_lower = str(k).lower()
            for pattern in patterns:
                if pattern.lower() in k_lower:
                    if v is not None and str(v).strip().upper() not in ("", "NULL", "NA", "N/A", "-"):
                        return v
        # Then recurse
        for k, v in data.items():
            res = find_value_by_key_patterns(v, patterns)
            if res is not None:
                return res
    elif isinstance(data, list):
        for item in data:
            res = find_value_by_key_patterns(item, patterns)
            if res is not None:
                return res
    return None


def extract_output2_fields(final_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts latitude, longitude, construction price, and property price
    from the finalized populated JSON output.
    """
    val = final_json.get("valuation", {})
    add_data = final_json.get("additional_data", {})
    loc_details = final_json.get("location_details", {})

    # 1. Latitude
    lat = None
    if isinstance(loc_details, dict):
        lat = loc_details.get("latitude") or loc_details.get("latitute")

    # 2. Longitude
    lon = None
    if isinstance(loc_details, dict):
        lon = loc_details.get("longitude") or loc_details.get("langitute") or loc_details.get("langitude")

    # If coordinate direct values are missing, try lat_long
    if lat is None or lon is None:
        lat_long_val = None
        if isinstance(loc_details, dict):
            lat_long_val = loc_details.get("lat_long") or loc_details.get("lat/long") or loc_details.get("lat long")
        if lat_long_val is None:
            lat_long_val = find_value_by_key_patterns(final_json, ["lat_long", "lat/long", "lat long"])
            
        if isinstance(lat_long_val, str) and lat_long_val.strip():
            # Try to split by '/' or ','
            parts = re.split(r'[/,]', lat_long_val)
            if len(parts) >= 2:
                in_lat = parts[0].strip()
                in_lon = parts[1].strip()
                if lat is None:
                    lat = in_lat
                if lon is None:
                    lon = in_lon

    # Recursive fallback for coordinates if still None
    if lat is None:
        lat = find_value_by_key_patterns(final_json, ["latitude", "latitute"])
    if lon is None:
        lon = find_value_by_key_patterns(final_json, ["longitude", "langitute", "langitude"])

    # Clean coordinate strings
    def clean_coord(v):
        if v is None:
            return None
        v_str = str(v).strip()
        if any(c.isdigit() for c in v_str):
            return v_str
        return None

    lat = clean_coord(lat)
    lon = clean_coord(lon)

    # 3. Construction price
    construction_price = None
    if isinstance(val, dict):
        construction_price = val.get("cost_of_construction") or val.get("construction price")
        if construction_price is None:
            # check bua total_value or Value
            bua = val.get("line_items", {}).get("bua", {})
            if isinstance(bua, dict):
                construction_price = bua.get("total_value") or bua.get("Value")

    if construction_price is None and isinstance(add_data, dict):
        construction_price = (
            add_data.get("Total Construction Value") or
            add_data.get("Total Construction Value_2") or
            add_data.get("Cost of construction") or
            add_data.get("Cost of construction_2") or
            add_data.get("Total Proposed Construction Cost")
        )

    if construction_price is None:
        construction_price = find_value_by_key_patterns(final_json, [
            "construction price", "construction_price", "cost_of_construction", "cost of construction"
        ])

    # Clean construction price value
    if construction_price is not None:
        construction_price = str(construction_price).strip()

    # 4. Property price
    property_price = None
    if isinstance(val, dict):
        property_price = (
            val.get("property_value_as_on_date") or 
            val.get("property price") or
            val.get("property_price") or
            val.get("property_value_on_post_completion") or 
            val.get("fair_market_value") or 
            val.get("realizable_value") or
            val.get("realizable_value_on_date") or
            val.get("distress_value")
        )

    if property_price is None:
        property_price = find_value_by_key_patterns(final_json, [
            "property price", "property_price", "property_value", "property value",
            "fair_market_value", "fair market value", "realizable_value", "realizable value"
        ])

    # Clean property price value
    if property_price is not None:
        property_price = str(property_price).strip()

    return {
        "latitude": lat,
        "longitude": lon,
        "construction price": construction_price,
        "property price": property_price
    }



# ─── Pydantic Validation Models (V2) ──────────────────────────────────────────
# A recursive model representing the dynamic JSON structure from the LLM
JSONValue = Union[str, int, float, bool, None, List[Any], Dict[str, Any]]

class ExtractedDataModel(RootModel[Dict[str, JSONValue]]):
    """
    Validates that the root is a JSON dictionary containing valid nested types.
    Enforces that the dictionary is non-empty.
    """
    def check_non_empty(self) -> "ExtractedDataModel":
        if not self.root:
            raise ValueError("The extracted JSON object cannot be empty.")
        return self


# ─── Step 1: Text Block Extraction (PyMuPDF) ─────────────────────────────────
def extract_text_blocks(page) -> List[Dict[str, Any]]:
    """
    Extracts text blocks from a page, detecting font flags for bold text,
    and returns a list of dictionaries with text, bbox, and vertical center.
    """
    blocks = []
    text_page = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    
    for b in text_page.get("blocks", []):
        if b.get("type") != 0:  # Skip images and non-text elements
            continue
        
        lines_text = []
        for line in b.get("lines", []):
            line_parts = []
            current_spans = []
            current_is_bold = None
            
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text:
                    continue
                
                # Check for bold fonts (bit 4 of flags is bold, or font name has 'bold' variants)
                font_flags = span.get("flags", 0)
                is_bold = bool(font_flags & 16) or any(
                    k in span.get("font", "").lower() 
                    for k in ("bold", "heavy", "black", "demi", "semibold")
                )
                
                if current_is_bold is None:
                    current_is_bold = is_bold
                    current_spans.append(text)
                elif current_is_bold == is_bold:
                    current_spans.append(text)
                else:
                    # Flush previous group
                    group_text = "".join(current_spans).strip()
                    if group_text:
                        if current_is_bold:
                            line_parts.append(f"**{group_text}**")
                        else:
                            line_parts.append(group_text)
                    current_is_bold = is_bold
                    current_spans = [text]
                    
            if current_spans:
                group_text = "".join(current_spans).strip()
                if group_text:
                    if current_is_bold:
                        line_parts.append(f"**{group_text}**")
                    else:
                        line_parts.append(group_text)
                        
            if line_parts:
                lines_text.append(process_bold_text(" ".join(line_parts)))
        
        if lines_text:
            block_text = "\n".join(lines_text).strip()
            bbox = b.get("bbox", [0, 0, 0, 0])
            blocks.append({
                "text": block_text,
                "bbox": [round(coord, 2) for coord in bbox]
            })
            
    return blocks


def merge_close_coordinates(coords: Union[set, List[float]], threshold: float = 1.0) -> List[float]:
    """
    Merges coordinates that are very close to each other to handle float rounding errors.
    """
    if not coords:
        return []
    sorted_coords = sorted(list(coords))
    merged = [sorted_coords[0]]
    for val in sorted_coords[1:]:
        if val - merged[-1] <= threshold:
            continue
        else:
            merged.append(val)
    return merged


def extract_cell_text_with_bold(cell_page) -> str:
    """
    Extracts text from a cropped pdfplumber page (representing a cell),
    wrapping bold characters/words in ** markers.
    Handles:
      - Cross-cell character leakage (height < 2pt filter)
      - Synthetic bold detection (double-struck character deduplication)
      - Font-name based bold detection (e.g. 'Cambria-Bold')
    """
    chars = cell_page.chars
    if not chars:
        return ""

    # Filter out characters that are tiny overlaps from adjacent cell edges.
    # When pdfplumber crops a cell, characters from neighboring rows that
    # overlap the boundary by a fraction of a point get included with a
    # cropped height of < 1pt. Real characters are always >= 4pt tall.
    chars = [c for c in chars if (c["bottom"] - c["top"]) >= 2.0]
    if not chars:
        return ""

    # Sort characters by vertical position first, then horizontal
    sorted_chars = sorted(chars, key=lambda c: (c["top"], c["x0"]))

    # Deduplicate synthetic bold (double-struck) characters.
    # PDFs simulate bold by drawing the same character twice at nearly the
    # same position with a tiny offset — this makes the text look thicker/
    # darker. We detect these duplicates and mark the kept character as bold.
    deduped_chars = []
    for c in sorted_chars:
        is_dup = False
        for prev in reversed(deduped_chars[-15:]):
            if abs(c["top"] - prev["top"]) < 1.0 and abs(c["x0"] - prev["x0"]) < 1.0:
                if c["text"].lower() == prev["text"].lower():
                    is_dup = True
                    prev["is_bold_override"] = True
                    break
        if not is_dup:
            c["is_bold_override"] = False
            deduped_chars.append(c)

    sorted_chars = deduped_chars

    lines = []
    current_line = []
    current_top = -1.0

    for c in sorted_chars:
        # Group characters on the same line (allowing small vertical differences)
        if current_line and c["top"] > current_top + 3:
            current_line.sort(key=lambda char: char["x0"])
            lines.append(current_line)
            current_line = [c]
            current_top = c["top"]
        else:
            current_line.append(c)
            if current_top == -1.0:
                current_top = c["top"]
            else:
                current_top = max(current_top, c["top"])

    if current_line:
        current_line.sort(key=lambda char: char["x0"])
        lines.append(current_line)

    formatted_lines = []
    for line in lines:
        line_parts = []
        current_group = []
        current_is_bold = None

        for c in line:
            text = c["text"]
            fontname = c.get("fontname", "").lower()
            # Bold if: synthetic bold detected (double-struck) OR font name contains bold keyword
            is_bold = c.get("is_bold_override", False) or any(
                k in fontname for k in ("bold", "heavy", "black", "demi", "semibold")
            )

            if current_is_bold is None:
                current_is_bold = is_bold
                current_group.append(text)
            elif current_is_bold == is_bold:
                current_group.append(text)
            else:
                group_text = "".join(current_group).strip()
                if group_text:
                    if current_is_bold:
                        line_parts.append(f"**{group_text}**")
                    else:
                        line_parts.append(group_text)
                current_is_bold = is_bold
                current_group = [text]

        if current_group:
            group_text = "".join(current_group).strip()
            if group_text:
                if current_is_bold:
                    line_parts.append(f"**{group_text}**")
                else:
                    line_parts.append(group_text)

        if line_parts:
            formatted_lines.append(process_bold_text(" ".join(line_parts)))

    return "\n".join(formatted_lines).strip()


# ─── Step 2: Table Extraction (pdfplumber) ───────────────────────────────────
def extract_tables_with_pdfplumber(pdf_path: str) -> Dict[int, List[Dict[str, Any]]]:
    """
    Extracts tables page-by-page from the PDF using pdfplumber.
    Preserves row-column relationships and cell structures, expanding merged cells.
    """
    tables_by_page = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            page_no = page_idx + 1
            tables_by_page[page_no] = []
            
            found_tables = page.find_tables()
            for t in found_tables:
                # Group and clean row/col boundaries from physical cell coordinates
                x_coords = set()
                y_coords = set()
                for cell in t.cells:
                    x_coords.add(cell[0])
                    x_coords.add(cell[2])
                    y_coords.add(cell[1])
                    y_coords.add(cell[3])
                    
                col_edges = merge_close_coordinates(x_coords, threshold=1.0)
                row_edges = merge_close_coordinates(y_coords, threshold=1.0)
                
                num_rows = len(row_edges) - 1
                num_cols = len(col_edges) - 1
                
                if num_rows <= 0 or num_cols <= 0:
                    continue
                
                row_midpoints = []
                for r in range(num_rows):
                    row_midpoints.append((row_edges[r] + row_edges[r+1]) / 2.0)
                    
                col_midpoints = []
                for c in range(num_cols):
                    col_midpoints.append((col_edges[c] + col_edges[c+1]) / 2.0)
                
                # Initialize grid with empty strings
                expanded_rows = [["" for _ in range(num_cols)] for _ in range(num_rows)]
                
                for cell in t.cells:
                    cx0, cy0, cx1, cy1 = cell
                    # Crop page to the cell boundaries to extract style-aware text
                    cell_page = page.crop((cx0, cy0, cx1, cy1))
                    val_str = extract_cell_text_with_bold(cell_page)
                    
                    # Find all grid coordinates (r, c) covered by this physical cell
                    coords_in_cell = []
                    for r in range(num_rows):
                        y_mid = row_midpoints[r]
                        if cy0 - 0.5 <= y_mid <= cy1 + 0.5:
                            for c in range(num_cols):
                                x_mid = col_midpoints[c]
                                if cx0 - 0.5 <= x_mid <= cx1 + 0.5:
                                    coords_in_cell.append((r, c))
                                    
                    for r, c in coords_in_cell:
                        expanded_rows[r][c] = val_str
                
                bbox = t.bbox  # (x0, top, x1, bottom)
                tables_by_page[page_no].append({
                    "bbox": [round(coord, 2) for coord in bbox],
                    "rows": expanded_rows
                })
    return tables_by_page


# ─── Step 3: Overlap Checking ────────────────────────────────────────────────
def is_inside_table(block_bbox: List[float], tables: List[Dict[str, Any]]) -> bool:
    """
    Returns True if the text block resides inside any of the table bboxes.
    Prevents duplicate text extraction.
    """
    bx0, by0, bx1, by1 = block_bbox
    for t in tables:
        tx0, ty0, tx1, ty1 = t["bbox"]
        # Allow a small 5pt padding window for edge overlaps
        if bx0 >= tx0 - 5 and bx1 <= tx1 + 5 and by0 >= ty0 - 5 and by1 <= ty1 + 5:
            return True
    return False


# ─── Step 4: Build Intermediate Structure ────────────────────────────────────
def build_intermediate_json(pdf_path: str) -> Dict[str, Any]:
    """
    Combines text blocks and tables page-by-page into a structured dict.
    Funnels coordinates, values, and merged structures accurately.
    """
    log("Parsing tables using pdfplumber...")
    tables_by_page = extract_tables_with_pdfplumber(pdf_path)
    
    log("Parsing text layouts using PyMuPDF...")
    doc = fitz.open(pdf_path)
    pages_list = []
    
    for page_idx, page in enumerate(doc):
        page_no = page_idx + 1
        tables = tables_by_page.get(page_no, [])
        
        # Extract text blocks
        raw_blocks = extract_text_blocks(page)
        
        # Filter blocks that overlap table structures
        filtered_blocks = []
        for b in raw_blocks:
            if not is_inside_table(b["bbox"], tables):
                filtered_blocks.append({
                    "text": b["text"],
                    "bbox": b["bbox"]
                })
        
        # Cleaned tables representation
        cleaned_tables = []
        for t in tables:
            cleaned_tables.append({
                "bbox": t["bbox"],
                "rows": t["rows"]
            })
            
        pages_list.append({
            "page_no": page_no,
            "text_blocks": filtered_blocks,
            "tables": cleaned_tables
        })
        
    doc.close()
    return {"pages": pages_list}


# ─── Step 5: Convert to Markdown ─────────────────────────────────────────────
def table_to_markdown(rows: List[List[str]]) -> str:
    """Formats a grid of table rows to standard GFM Markdown."""
    if not rows:
        return ""
    max_cols = max(len(row) for row in rows)
    
    # Ensure all rows have equal column length
    padded_rows = []
    for r in rows:
        padded = r + [""] * (max_cols - len(r))
        padded = [c.replace("\n", " ").strip() for c in padded]
        padded_rows.append(padded)
        
    md_lines = []
    header = padded_rows[0]
    md_lines.append("| " + " | ".join(header) + " |")
    md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
    for r in padded_rows[1:]:
        md_lines.append("| " + " | ".join(r) + " |")
        
    return "\n".join(md_lines)

def convert_to_markdown(intermediate_data: Dict[str, Any]) -> str:
    """
    Renders structured layout into a clean markdown document.
    Sorts all items top-to-bottom by y0 coordinate.
    """
    md_parts = []
    for page in intermediate_data["pages"]:
        page_no = page["page_no"]
        md_parts.append(f"\n# Page {page_no}\n")
        
        elements = []
        for b in page["text_blocks"]:
            elements.append(("text", b["bbox"][1], b))
        for t in page["tables"]:
            elements.append(("table", t["bbox"][1], t))
            
        # Sort vertically to preserve reading order
        elements.sort(key=lambda x: x[1])
        
        for el_type, _, el_data in elements:
            bbox_str = f"bbox: {el_data['bbox']}"
            if el_type == "text":
                md_parts.append(f"\n[{bbox_str}]\n{el_data['text']}\n")
            elif el_type == "table":
                md_parts.append(f"\n[{bbox_str}]\n" + table_to_markdown(el_data["rows"]) + "\n")
                
    return "\n".join(md_parts)


# ─── Step 6 & 7: OpenRouter & Validation Retry ───────────────────────────────
def query_openrouter_with_retry(markdown_content: str, schema_template: Dict[str, Any], max_retries: int = 3) -> Dict[str, Any]:
    """
    Sends the markdown document to OpenRouter, processes it, and validates
    via Pydantic. Retries up to max_retries on failure.
    """
    if not OPENROUTER_API_KEY:
        log_error("OPENROUTER_API_KEY is not defined in the environment variables.")
        sys.exit(1)
        
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )
    

    system_prompt = (
        "You are an expert document AI data normalizer. Your task is to convert the provided Markdown representation "
        "of a PDF valuation report (which contains page headers, bounding boxes [bbox], formatted text blocks, and tables) "
        "into a single structured JSON object conforming to the Master Schema structure provided below.\n\n"

        "CRITICAL RULES:\n"

        "1. Structure Alignment: The output JSON must conform to the keys and structure of the Master Schema template. "
        "For every key in the Master Schema, populate its value from the document. If the value is not present/found "
        "in the document, set it to null. NEVER omit a key that exists in the Master Schema — all keys must appear.\n"

        "2. ABSOLUTE ZERO DATA LOSS: You MUST extract EVERY piece of data from the markdown. "
        "If you find any additional information, table rows, keys or sections in the document that do not map to any existing "
        "keys in the Master Schema, you MUST still capture them. Add these new keys/values under the most appropriate "
        "nested section, or under 'additional_data' at the root level if no better section exists. "
        "DO NOT skip or ignore any text or table value from the markdown — the output JSON must account for ALL content.\n"

        "3. Do not summarize, condense, or truncate any field values. Copy them verbatim from the document.\n"

        "4. Do not hallucinate or infer missing values. Only extract values that are literally present in the markdown.\n"

        "5. do not Preserve all unit annotations exactly (e.g. sqft, sqyd, sqm, smts, kms, %, years, rupees, Rs,₹,>,ft.) and remove the symbols while generating the output.json and output2.json.\n"

        "6. Preserve addresses, names, numbers, and IDs exactly as written — do not correct spelling or format.\n"

        "7. BOLD VALUE EXTRACTION (CRITICAL): In the markdown, bold text is wrapped in **double asterisks**. "
        "Bold text represents the SELECTED / ACTUAL value for a field. "
        "For multi-option fields formatted as a slash-separated list (e.g. 'Poor / Fair / **Good** / Excellent', "
        "'Yes / **No**', 'Freehold / **Leasehold**'), extract ONLY the bold option as the value — "
        "do NOT return the full list of options. Strip the ** markers from the extracted value. "
        "If a key's entire value is bolded (e.g. '**Under Construction**'), extract just that text without asterisks. "
        "If NO option in a slash-separated list is bolded, return the full list as-is.\n"

        "8. TABLE MERGE HANDLING: When a table cell covers multiple rows (rowspan) or columns (colspan), "
        "its value applies to ALL covered rows/columns. Do NOT leave covered cells blank. "
        "Example: if 'Residential' spans Ground Floor and First Floor rows, both rows get 'Residential'.\n"

        "9. Return a fully expanded logical table — inherited merged-cell values must be repeated in every affected row.\n"

        "10. TABLE COMPLETENESS: For every table in the markdown, extract EVERY row and EVERY column. "
        "Do not skip rows that appear empty or repetitive. If a row is part of the markdown it must appear in the JSON.\n"

        "11. Do not wrap output in markdown fences (e.g. do not write ```json ... ```) or include comments or explanations. "
        "Return raw valid JSON starting with '{' and ending with '}'.\n"

        "12. EXACT SCHEMA KEY USAGE AND FLOOR NAME NORMALIZATION: Use the EXACT keys provided in the Master Schema. "
        "For sections keyed by floor names (e.g. 'GF', 'FF', 'SF', 'TF', 'PH'), map the document's floor data "
        "to those EXACT keys. Apply these normalizations: "
        "'Ground Floor' → 'GF', 'First Floor' → 'FF', 'Second Floor' → 'SF', 'Third Floor' → 'TF'. "
        "CRITICALLY: Floor names with parenthetical suffixes like 'FF(Proposed)', 'GF(Existing)', "
        "'SF (Under Construction)', 'FF (U/C)' etc. MUST be mapped to their base floor key — "
        "'FF(Proposed)' → 'FF', 'GF(Existing)' → 'GF', etc. Strip the parenthetical qualifier "
        "and use the base floor abbreviation as the key. Extract ALL values from these rows including zeros. "
        "Do NOT skip a floor row just because it has a suffix or qualifier in parentheses. "
        "Do NOT create duplicate entries under synonym keys. "
        "Map 'Realizable value on completion' or 'Realizable value' from the document "
        "to the 'realizable_value' key, and 'Realizable value on date' to 'realizable_value_on_date'.\n\n"

        f"MASTER SCHEMA TEMPLATE:\n{json.dumps(schema_template, indent=2)}\n"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "Here is the complete markdown representation of the PDF. "
            "Extract ALL data — every field, every table row, every value — into a structured JSON conforming to the Master Schema. "
            "Every piece of information in the markdown below MUST appear somewhere in the output JSON. "
            "Do not omit any data:\n\n" + markdown_content
        )}
    ]
    
    temperature = 0.0
    for attempt in range(1, max_retries + 1):
        try:
            log(f"Calling OpenRouter ({MODEL}) - Attempt {attempt}/{max_retries}...")
            
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"} if "gemini" in MODEL or "gpt" in MODEL else None
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Clean markdown code fences if output by LLM
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Pydantic schema validation
            validated = ExtractedDataModel.model_validate_json(response_text)
            validated.check_non_empty()
            
            return validated.root
            
        except (json.JSONDecodeError, ValidationError, Exception) as e:
            log_error(f"Validation failed on attempt {attempt}: {str(e)}")
            if attempt == max_retries:
                raise e
            
            # Adjust temperature and feed back validation error to the assistant
            temperature = min(0.2, temperature + 0.1)
            messages.append({"role": "assistant", "content": response_text if 'response_text' in locals() else ""})
            messages.append({"role": "user", "content": f"The previous output was invalid due to: {str(e)}. Please output ONLY valid JSON without fences or comments."})





# ─── Main Execution Pipeline ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF Valuation Report to Structured JSON with Zero Data Loss"
    )
    parser.add_argument("pdf", help="Path to input valuation PDF file")
    parser.add_argument(
        "--output",
        default="output.json",
        help="Path to save final output JSON (default: output.json)",
    )
    parser.add_argument(
        "--schema",
        default="master_schema.json",
        help="Path to master schema JSON template (default: master_schema.json)",
    )
    args = parser.parse_args()
    
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        log_error(f"Input PDF file not found: {args.pdf}")
        sys.exit(1)
        
    # Load Master Schema
    schema_path = Path(args.schema)
    if not schema_path.exists():
        log_error(f"Schema file not found: {args.schema}")
        sys.exit(1)
        
    log(f"Loading master schema: {schema_path.name}")
    with open(schema_path, "r", encoding="utf-8") as f:
        master_schema = json.load(f)
        
    # Create a clean/null template from the master schema
    clean_template = clean_schema_template(master_schema)
        
    log(f"Processing PDF: {pdf_path.name}")
    
    # 1. Parse PDF structures (kept in memory only; no intermediate JSON file)
    intermediate_data = build_intermediate_json(str(pdf_path))
    
    # 2. Format intermediate to Markdown
    markdown_content = convert_to_markdown(intermediate_data)
    
    # 3. Call LLM normalizer with retry validation
    try:
        final_raw_json = query_openrouter_with_retry(markdown_content, clean_template)
    except Exception as e:
        log_error(f"Failed to extract and validate JSON after multiple retries: {e}")
        sys.exit(1)
        
    # 4. Merge LLM output into the master schema template to preserve keys and add new ones
    # 4a. Merge LLM output INTO the original master schema so all schema keys appear (with null if missing)
    #     and any new keys from the LLM are added alongside
    merged_with_schema = merge_and_update_schema(master_schema, final_raw_json)

    # 4b. Create a null-value template of the fully merged schema (preserves structure, all values → null)
    updated_master_schema = clean_schema_template(merged_with_schema)

    # Write the updated master schema template back toACA master_schema.json
    # This persistently adds any new keys from this PDF for future runs
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(updated_master_schema, f, indent=2, ensure_ascii=False)
    log(f"Updated master schema template saved to: {schema_path}")

    # 4c. Generate the final populated output by merging LLM data over the updated schema
    #     This ensures every schema key is present (null if not in LLM output) AND
    #     every new key from the LLM is also included with its actual value
    final_json = merge_and_update_schema(updated_master_schema, final_raw_json)

    # 5. Save final JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(final_json, f, indent=2, ensure_ascii=False)
    log(f"Saved final normalized JSON to: {args.output}")

    # 6. Extract output2 fields and save to output2.json
    output2_data = extract_output2_fields(final_json)
    output2_path = Path(args.output).parent / "output2.json"
    with open(output2_path, "w", encoding="utf-8") as f:
        json.dump(output2_data, f, indent=2, ensure_ascii=False)
    log(f"Saved output2 JSON to: {output2_path}")
   
    

if __name__ == "__main__":
    main()