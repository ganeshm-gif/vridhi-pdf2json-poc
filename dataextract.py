# #!/usr/bin/env python
# """
# g.py
# ================================================================================
# Production-Grade PDF-to-JSON Data Extraction Pipeline
# --------------------------------------------------------------------------------
# Architecture:
#   1. PDF Parsing: Extract text blocks (coordinates + bold) via PyMuPDF (fitz)
# # Architecture Overview:
# #   1. PDF Parsing: Extract text blocks (coordinates + bold) via PyMuPDF (fitz)
# #   2. Table Parsing: Extract tables (coordinates + merged cells) via pdfplumber
# #   3. Overlap Filter: Filter out text blocks residing inside table boundaries
# #   4. Markdown Render: Convert pages structure to markdown (with coordinate annotations)
# #   5. LLM Integration: Query OpenRouter (gemini-2.5-flash) with retry logic
# #   6. Validation: Validate response format via Pydantic v2
# #   7. Final Save: Write validated structured JSON to output.json

# # Load environment variables (API keys etc.)
# from dotenv import load_dotenv
# # PDF handling libraries
# import fitz  # PyMuPDF
# import pdfplumber
# # OpenAI client for LLM calls
# from openai import OpenAI
# # Data validation with Pydantic
# from pydantic import BaseModel, RootModel, ValidationError
# ================================================================================
# """

# # Standard library imports
# import os
# import sys
# import json
# import argparse
# from pathlib import Path

# # Typing imports (used for type hints)
# from typing import Dict, Any, List, Optional, Union

# # Third‑party library imports
# from dotenv import load_dotenv
# import fitz  # PyMuPDF for PDF text extraction
# import pdfplumber  # PDF table extraction
# from openai import OpenAI  # LLM client
# from pydantic import BaseModel, RootModel, ValidationError


# # ─── Load Environment & Configure Client ─────────────────────────────────────
# load_dotenv()

# OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# MODEL = os.getenv("OPENROUTER_MODEL") or os.getenv("MODE") or "google/gemini-2.5-flash"

# # Setup Logging
# def log(msg: str):
#     print(f"[*] {msg}", flush=True)

# def log_error(msg: str):
#     print(f"[ERROR] {msg}", file=sys.stderr, flush=True)

# def clean_schema_template(schema: Any) -> Any:
#     """
#     Recursively sets all non-dictionary, non-list leaf node values to None.
#     This creates a clean template of the schema with the exact structure.
#     """
#     if isinstance(schema, dict):
#         return {k: clean_schema_template(v) for k, v in schema.items()}
#     elif isinstance(schema, list):
#         return [clean_schema_template(item) for item in schema]
#     else:
#         return None

# def merge_and_update_schema(schema_template: Any, llm_data: Any) -> Any:
#     """
#     Recursively merges llm_data into schema_template.
#     - Keys in schema_template are kept. If present in llm_data, recursively merge values.
#     - Keys in llm_data that are NOT in schema_template are added (new keys from the document).
#     - If both are lists, return llm_data.
#     - Otherwise, return llm_data if llm_data is not None, else schema_template.
#     """
#     if isinstance(schema_template, dict) and isinstance(llm_data, dict):
#         merged = {}
#         for k, template_val in schema_template.items():
#             if k in llm_data:
#                 merged[k] = merge_and_update_schema(template_val, llm_data[k])
#             else:
#                 merged[k] = template_val
#         for k, llm_val in llm_data.items():
#             if k not in schema_template:
#                 merged[k] = llm_val
#         return merged
#     elif isinstance(schema_template, list) and isinstance(llm_data, list):
#         return llm_data
#     else:
#         return llm_data if llm_data is not None else schema_template


# # ─── Pydantic Validation Models (V2) ──────────────────────────────────────────
# # A recursive model representing the dynamic JSON structure from the LLM
# JSONValue = Union[str, int, float, bool, None, List[Any], Dict[str, Any]]

# class ExtractedDataModel(RootModel[Dict[str, JSONValue]]):
#     """
#     Validates that the root is a JSON dictionary containing valid nested types.
#     Enforces that the dictionary is non-empty.
#     """
#     def check_non_empty(self) -> "ExtractedDataModel":
#         if not self.root:
#             raise ValueError("The extracted JSON object cannot be empty.")
#         return self


# # ─── Step 1: Text Block Extraction (PyMuPDF) ─────────────────────────────────
# def extract_text_blocks(page) -> List[Dict[str, Any]]:
#     """
#     Extracts text blocks from a page, detecting font flags for bold text,
#     and returns a list of dictionaries with text, bbox, and vertical center.
#     """
#     blocks = []
#     text_page = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    
#     for b in text_page.get("blocks", []):
#         if b.get("type") != 0:  # Skip images and non-text elements
#             continue
        
#         lines_text = []
#         for line in b.get("lines", []):
#             line_parts = []
#             for span in line.get("spans", []):
#                 text = span.get("text", "")
#                 if not text:
#                     continue
                
#                 # Check for bold fonts (bit 4 of flags is bold, or font name has 'bold' variants)
#                 font_flags = span.get("flags", 0)
#                 is_bold = bool(font_flags & 16) or any(
#                     k in span.get("font", "").lower() 
#                     for k in ("bold", "heavy", "black", "demi", "semibold")
#                 )
                
#                 cleaned_text = text.strip()
#                 if cleaned_text:
#                     if is_bold:
#                         line_parts.append(f"**{cleaned_text}**")
#                     else:
#                         line_parts.append(cleaned_text)
            
#             if line_parts:
#                 lines_text.append(" ".join(line_parts))
        
#         if lines_text:
#             block_text = "\n".join(lines_text).strip()
#             bbox = b.get("bbox", [0, 0, 0, 0])
#             blocks.append({
#                 "text": block_text,
#                 "bbox": [round(coord, 2) for coord in bbox]
#             })
            
#     return blocks


# # ─── Step 2: Table Extraction (pdfplumber) ───────────────────────────────────
# def extract_tables_with_pdfplumber(pdf_path: str) -> Dict[int, List[Dict[str, Any]]]:
#     """
#     Extracts tables page-by-page from the PDF using pdfplumber.
#     Preserves row-column relationships and cell structures.
#     """
#     tables_by_page = {}
#     with pdfplumber.open(pdf_path) as pdf:
#         for page_idx, page in enumerate(pdf.pages):
#             page_no = page_idx + 1
#             tables_by_page[page_no] = []
            
#             found_tables = page.find_tables()
#             for t in found_tables:
#                 rows = t.extract()
#                 clean_rows = []
#                 for row in rows:
#                     clean_row = []
#                     for cell in row:
#                         if cell is None:
#                             clean_row.append("")
#                         else:
#                             clean_row.append(cell.strip())
#                     clean_rows.append(clean_row)
                
#                 bbox = t.bbox  # (x0, top, x1, bottom)
#                 tables_by_page[page_no].append({
#                     "bbox": [round(coord, 2) for coord in bbox],
#                     "rows": clean_rows
#                 })
#     return tables_by_page


# # ─── Step 3: Overlap Checking ────────────────────────────────────────────────
# def is_inside_table(block_bbox: List[float], tables: List[Dict[str, Any]]) -> bool:
#     """
#     Returns True if the text block resides inside any of the table bboxes.
#     Prevents duplicate text extraction.
#     """
#     bx0, by0, bx1, by1 = block_bbox
#     for t in tables:
#         tx0, ty0, tx1, ty1 = t["bbox"]
#         # Allow a small 5pt padding window for edge overlaps
#         if bx0 >= tx0 - 5 and bx1 <= tx1 + 5 and by0 >= ty0 - 5 and by1 <= ty1 + 5:
#             return True
#     return False


# # ─── Step 4: Build Intermediate Structure ────────────────────────────────────
# def build_intermediate_json(pdf_path: str) -> Dict[str, Any]:
#     """
#     Combines text blocks and tables page-by-page into a structured dict.
#     Funnels coordinates, values, and merged structures accurately.
#     """
#     log("Parsing tables using pdfplumber...")
#     tables_by_page = extract_tables_with_pdfplumber(pdf_path)
    
#     log("Parsing text layouts using PyMuPDF...")
#     doc = fitz.open(pdf_path)
#     pages_list = []
    
#     for page_idx, page in enumerate(doc):
#         page_no = page_idx + 1
#         tables = tables_by_page.get(page_no, [])
        
#         # Extract text blocks
#         raw_blocks = extract_text_blocks(page)
        
#         # Filter blocks that overlap table structures
#         filtered_blocks = []
#         for b in raw_blocks:
#             if not is_inside_table(b["bbox"], tables):
#                 filtered_blocks.append({
#                     "text": b["text"],
#                     "bbox": b["bbox"]
#                 })
        
#         # Cleaned tables representation
#         cleaned_tables = []
#         for t in tables:
#             cleaned_tables.append({
#                 "bbox": t["bbox"],
#                 "rows": t["rows"]
#             })
            
#         pages_list.append({
#             "page_no": page_no,
#             "text_blocks": filtered_blocks,
#             "tables": cleaned_tables
#         })
        
#     doc.close()
#     return {"pages": pages_list}


# # ─── Step 5: Convert to Markdown ─────────────────────────────────────────────
# def table_to_markdown(rows: List[List[str]]) -> str:
#     """Formats a grid of table rows to standard GFM Markdown."""
#     if not rows:
#         return ""
#     max_cols = max(len(row) for row in rows)
    
#     # Ensure all rows have equal column length
#     padded_rows = []
#     for r in rows:
#         padded = r + [""] * (max_cols - len(r))
#         padded = [c.replace("\n", " ").strip() for c in padded]
#         padded_rows.append(padded)
        
#     md_lines = []
#     header = padded_rows[0]
#     md_lines.append("| " + " | ".join(header) + " |")
#     md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
#     for r in padded_rows[1:]:
#         md_lines.append("| " + " | ".join(r) + " |")
        
#     return "\n".join(md_lines)

# def convert_to_markdown(intermediate_data: Dict[str, Any]) -> str:
#     """
#     Renders structured layout into a clean markdown document.
#     Sorts all items top-to-bottom by y0 coordinate.
#     """
#     md_parts = []
#     for page in intermediate_data["pages"]:
#         page_no = page["page_no"]
#         md_parts.append(f"\n# Page {page_no}\n")
        
#         elements = []
#         for b in page["text_blocks"]:
#             elements.append(("text", b["bbox"][1], b))
#         for t in page["tables"]:
#             elements.append(("table", t["bbox"][1], t))
            
#         # Sort vertically to preserve reading order
#         elements.sort(key=lambda x: x[1])
        
#         for el_type, _, el_data in elements:
#             bbox_str = f"bbox: {el_data['bbox']}"
#             if el_type == "text":
#                 md_parts.append(f"\n[{bbox_str}]\n{el_data['text']}\n")
#             elif el_type == "table":
#                 md_parts.append(f"\n[{bbox_str}]\n" + table_to_markdown(el_data["rows"]) + "\n")
                
#     return "\n".join(md_parts)


# # ─── Step 6 & 7: OpenRouter & Validation Retry ───────────────────────────────
# def query_openrouter_with_retry(markdown_content: str, schema_template: Dict[str, Any], max_retries: int = 3) -> Dict[str, Any]:
#     """
#     Sends the markdown document to OpenRouter, processes it, and validates
#     via Pydantic. Retries up to max_retries on failure.
#     """
#     if not OPENROUTER_API_KEY:
#         log_error("OPENROUTER_API_KEY is not defined in the environment variables.")
#         sys.exit(1)
        
#     client = OpenAI(
#         base_url="https://openrouter.ai/api/v1",
#         api_key=OPENROUTER_API_KEY,
#     )
    
#     system_prompt = (
#         "You are an expert document AI data normalizer. Your task is to convert the provided Markdown representation "
#         "of a PDF valuation report (which contains page headers, bounding boxes [bbox], formatted text blocks, and tables) "
#         "into a single structured JSON object conforming to the Master Schema structure provided below.\n\n"

#         "CRITICAL RULES:\n"

#         "1. Structure Alignment: The output JSON must conform to the keys and structure of the Master Schema template. "
#         "For every key in the Master Schema, populate its value from the document. If the value is not present/found "
#         "in the document, set it to null.\n"

#         "2. Zero Data Loss & Dynamic Expansion: If you find any additional information, tables, keys or sections in the "
#         "document that do not map to any existing keys in the Master Schema, you MUST still extract them. Add these new "
#         "keys/values at their most appropriate nested locations, or at the root level of the JSON.\n"

#         "3. Do not summarize or truncate fields.\n"

#         "4. Do not hallucinate or infer missing values. If a value is not in the source text and not in the Master Schema, "
#         "do not invent it.\n"

#         "5. Preserve all unit annotations exactly (e.g. sqft, sqyd, smts, kms, %, years, rupees).\n"

#         "6. Preserve addresses and names exactly as written.\n"

#         "7. For multi-option fields formatted as a slash-separated list (e.g. 'Poor / Fair / **Good** / Excellent'), "
#         "the term marked in bold (**Good**) represents the selected option. Extract ONLY the selected text string.\n"

#         "8. TABLE MERGE HANDLING: When extracting tables, respect merged cells (rowspan/colspan). "
#         "If a value appears in a merged cell spanning multiple rows or columns, propagate that value to every applicable "
#         "row/column covered by the merge. For example, if 'Residential' appears once in a merged cell covering Ground Floor "
#         "and First Floor rows, then both rows must have 'Residential' as their value. Do not leave covered cells blank or null "
#         "when the source table indicates the value applies through a merged cell.\n"

#         "9. When reconstructing tables, return a fully expanded logical table where inherited values from merged cells "
#         "are repeated in each affected row.\n"

#         "10. Do not wrap output in markdown fences (e.g. do not write ```json ... ```) or include comments or explanations. "
#         "Return raw valid JSON starting with '{' and ending with '}'.\n\n"

#         f"MASTER SCHEMA TEMPLATE:\n{json.dumps(schema_template, indent=2)}\n"
#     )
#     messages = [
#         {"role": "system", "content": system_prompt},
#         {"role": "user", "content": f"Here is the markdown representation of the PDF. Extract all details into a clean JSON structure conforming to the Master Schema:\n\n{markdown_content}"}
#     ]
    
#     temperature = 0.0
#     for attempt in range(1, max_retries + 1):
#         try:
#             log(f"Calling OpenRouter ({MODEL}) - Attempt {attempt}/{max_retries}...")
            
#             response = client.chat.completions.create(
#                 model=MODEL,
#                 messages=messages,
#                 temperature=temperature,
#                 response_format={"type": "json_object"} if "gemini" in MODEL or "gpt" in MODEL else None
#             )
            
#             response_text = response.choices[0].message.content.strip()
            
#             # Clean markdown code fences if output by LLM
#             if response_text.startswith("```json"):
#                 response_text = response_text[7:]
#             if response_text.startswith("```"):
#                 response_text = response_text[3:]
#             if response_text.endswith("```"):
#                 response_text = response_text[:-3]
#             response_text = response_text.strip()
            
#             # Pydantic schema validation
#             validated = ExtractedDataModel.model_validate_json(response_text)
#             validated.check_non_empty()
            
#             return validated.root
            
#         except (json.JSONDecodeError, ValidationError, Exception) as e:
#             log_error(f"Validation failed on attempt {attempt}: {str(e)}")
#             if attempt == max_retries:
#                 raise e
            
#             # Adjust temperature and feed back validation error to the assistant
#             temperature = min(0.2, temperature + 0.1)
#             messages.append({"role": "assistant", "content": response_text if 'response_text' in locals() else ""})
#             messages.append({"role": "user", "content": f"The previous output was invalid due to: {str(e)}. Please output ONLY valid JSON without fences or comments."})





# # ─── Main Execution Pipeline ────────────────────────────────────────────────
# def main():
#     parser = argparse.ArgumentParser(
#         description="Convert PDF Valuation Report to Structured JSON with Zero Data Loss"
#     )
#     parser.add_argument("pdf", help="Path to input valuation PDF file")
#     parser.add_argument(
#         "--output",
#         default="output.json",
#         help="Path to save final output JSON (default: output.json)",
#     )
#     parser.add_argument(
#         "--schema",
#         default="master_schema.json",
#         help="Path to master schema JSON template (default: master_schema.json)",
#     )
#     args = parser.parse_args()
    
#     pdf_path = Path(args.pdf)
#     if not pdf_path.exists():
#         log_error(f"Input PDF file not found: {args.pdf}")
#         sys.exit(1)
        
#     # Load Master Schema
#     schema_path = Path(args.schema)
#     if not schema_path.exists():
#         log_error(f"Schema file not found: {args.schema}")
#         sys.exit(1)
        
#     log(f"Loading master schema: {schema_path.name}")
#     with open(schema_path, "r", encoding="utf-8") as f:
#         master_schema = json.load(f)
        
#     # Create a clean/null template from the master schema
#     clean_template = clean_schema_template(master_schema)
        
#     log(f"Processing PDF: {pdf_path.name}")
    
#     # 1. Parse PDF structures
#     intermediate_data = build_intermediate_json(str(pdf_path))
    
#     # 2. Format intermediate to Markdown
#     markdown_content = convert_to_markdown(intermediate_data)
    
#     # 3. Call LLM normalizer with retry validation
#     try:
#         final_raw_json = query_openrouter_with_retry(markdown_content, clean_template)
#     except Exception as e:
#         log_error(f"Failed to extract and validate JSON after multiple retries: {e}")
#         sys.exit(1)
        
#     # 4. Merge LLM output into the master schema template to preserve keys and add new ones
#     # 4a. First, update the master schema template with any new keys (with null values)
#     cleaned_raw_json = clean_schema_template(final_raw_json)
#     updated_master_schema = clean_schema_template(merge_and_update_schema(master_schema, cleaned_raw_json))
    
#     # Write the updated master schema template back to master_schema.json
#     with open(schema_path, "w", encoding="utf-8") as f:
#         json.dump(updated_master_schema, f, indent=2, ensure_ascii=False)
#     log(f"Updated master schema template saved to: {schema_path}")
    
#     # 4b. Generate populated output by merging the LLM output with the updated master schema
#     final_json = merge_and_update_schema(updated_master_schema, final_raw_json)
    
#     # 5. Save final JSON
#     with open(args.output, "w", encoding="utf-8") as f:
#         json.dump(final_json, f, indent=2, ensure_ascii=False)
#     log(f"Saved final normalized JSON to: {args.output}")
   
    

# if __name__ == "__main__":
#     main()



#!/usr/bin/env python
"""
g.py
================================================================================
Production-Grade PDF-to-JSON Data Extraction Pipeline
--------------------------------------------------------------------------------
Architecture:
  1. PDF Parsing: Extract text blocks (coordinates + bold) via PyMuPDF (fitz)
# Architecture Overview:
#   1. PDF Parsing: Extract text blocks (coordinates + bold) via PyMuPDF (fitz)
#   2. Table Parsing: Extract tables (coordinates + merged cells) via pdfplumber
#   3. Overlap Filter: Filter out text blocks residing inside table boundaries
#   4. Intermediate Save: Save raw pages data to raw_extracted.json
#   5. Markdown Render: Convert pages structure to markdown (with coordinate annotations)
#   6. LLM Integration: Query OpenRouter (gemini-2.5-flash) with retry logic
#   7. Validation: Validate response format via Pydantic v2
#   8. Final Save: Write validated structured JSON to output.json

# Load environment variables (API keys etc.)
from dotenv import load_dotenv
# PDF handling libraries
import fitz  # PyMuPDF
import pdfplumber
# OpenAI client for LLM calls
from openai import OpenAI
# Data validation with Pydantic
from pydantic import BaseModel, RootModel, ValidationError
================================================================================
"""

# Standard library imports
import os
import sys
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
MODEL = os.getenv("OPENROUTER_MODEL") or os.getenv("MODE") or "google/gemini-2.5-flash"

# Setup Logging
def log(msg: str):
    print(f"[*] {msg}", flush=True)

def log_error(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)

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

def merge_and_update_schema(schema_template: Any, llm_data: Any) -> Any:
    """
    Recursively merges llm_data into schema_template.
    - Keys in schema_template are kept. If present in llm_data, recursively merge values.
    - Keys in llm_data that are NOT in schema_template are added (new keys from the document).
    - If both are lists, return llm_data.
    - Otherwise, return llm_data if llm_data is not None, else schema_template.
    """
    if isinstance(schema_template, dict) and isinstance(llm_data, dict):
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
        return llm_data
    else:
        return llm_data if llm_data is not None else schema_template


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
                
                cleaned_text = text.strip()
                if cleaned_text:
                    if is_bold:
                        line_parts.append(f"**{cleaned_text}**")
                    else:
                        line_parts.append(cleaned_text)
            
            if line_parts:
                lines_text.append(" ".join(line_parts))
        
        if lines_text:
            block_text = "\n".join(lines_text).strip()
            bbox = b.get("bbox", [0, 0, 0, 0])
            blocks.append({
                "text": block_text,
                "bbox": [round(coord, 2) for coord in bbox]
            })
            
    return blocks


# ─── Step 2: Table Extraction (pdfplumber) ───────────────────────────────────
def extract_tables_with_pdfplumber(pdf_path: str) -> Dict[int, List[Dict[str, Any]]]:
    """
    Extracts tables page-by-page from the PDF using pdfplumber.
    Preserves row-column relationships and cell structures.
    """
    tables_by_page = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            page_no = page_idx + 1
            tables_by_page[page_no] = []
            
            found_tables = page.find_tables()
            for t in found_tables:
                rows = t.extract()
                clean_rows = []
                for row in rows:
                    clean_row = []
                    for cell in row:
                        if cell is None:
                            clean_row.append("")
                        else:
                            clean_row.append(cell.strip())
                    clean_rows.append(clean_row)
                
                bbox = t.bbox  # (x0, top, x1, bottom)
                tables_by_page[page_no].append({
                    "bbox": [round(coord, 2) for coord in bbox],
                    "rows": clean_rows
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
    
    # system_prompt = (
    #     "You are an expert document AI data normalizer. Your task is to convert the provided Markdown representation "
    #     "of a PDF valuation report (which contains page headers, bounding boxes [bbox], formatted text blocks, and tables) "
    #     "into a single structured JSON object conforming to the Master Schema structure provided below.\n\n"
    #     "CRITICAL RULES:\n"
    #     "1. Structure Alignment: The output JSON must conform to the keys and structure of the Master Schema template. For every key in the Master Schema, "
    #     "populate its value from the document. If the value is not present/found in the document, set it to null.\n"
    #     "2. Zero Data Loss & Dynamic Expansion: If you find any additional information, tables, keys or sections in the document that do not map to any existing keys in the Master Schema, "
    #     "you MUST still extract them. Add these new keys/values at their most appropriate nested locations, or at the root level of the JSON.\n"
    #     "3. Do not summarize or truncate fields.\n"
    #     "4. Do not hallucinate or infer missing values. If a value is not in the source text and not in the Master Schema, do not invent it.\n"
    #     "5. Preserve all unit annotations exactly (e.g. sqft, sqyd, smts, kms, %, years, rupees).\n"
    #     "6. Preserve addresses and names exactly as written.\n"
    #     "7. For multi-option fields formatted as a slash-separated list (e.g. 'Poor / Fair / **Good** / Excellent'), "
    #     "the term marked in bold (**Good**) represents the selected option. Extract ONLY the selected text string.\n"
    #     "8. Do not wrap output in markdown fences (e.g. do not write ```json ... ```) or include comments or explanations. "
    #     "Return raw valid JSON starting with '{' and ending with '}'.\n\n"
    #     f"MASTER SCHEMA TEMPLATE:\n{json.dumps(schema_template, indent=2)}\n"
    # )
    system_prompt = (
        "You are an expert document AI data normalizer. Your task is to convert the provided Markdown representation "
        "of a PDF valuation report (which contains page headers, bounding boxes [bbox], formatted text blocks, and tables) "
        "into a single structured JSON object conforming to the Master Schema structure provided below.\n\n"

        "CRITICAL RULES:\n"

        "1. Structure Alignment: The output JSON must conform to the keys and structure of the Master Schema template. "
        "For every key in the Master Schema, populate its value from the document. If the value is not present/found "
        "in the document, set it to null.\n"

        "2. Zero Data Loss & Dynamic Expansion: If you find any additional information, tables, keys or sections in the "
        "document that do not map to any existing keys in the Master Schema, you MUST still extract them. Add these new "
        "keys/values at their most appropriate nested locations, or at the root level of the JSON.\n"

        "3. Do not summarize or truncate fields.\n"

        "4. Do not hallucinate or infer missing values. If a value is not in the source text and not in the Master Schema, "
        "do not invent it.\n"

        "5. Preserve all unit annotations exactly (e.g. sqft, sqyd, smts, kms, %, years, rupees).\n"

        "6. Preserve addresses and names exactly as written.\n"

        "7. For multi-option fields formatted as a slash-separated list (e.g. 'Poor / Fair / **Good** / Excellent'), "
        "the term marked in bold (**Good**) represents the selected option. Extract ONLY the selected text string.\n"

        "8. TABLE MERGE HANDLING: When extracting tables, respect merged cells (rowspan/colspan). "
        "If a value appears in a merged cell spanning multiple rows or columns, propagate that value to every applicable "
        "row/column covered by the merge. For example, if 'Residential' appears once in a merged cell covering Ground Floor "
        "and First Floor rows, then both rows must have 'Residential' as their value. Do not leave covered cells blank or null "
        "when the source table indicates the value applies through a merged cell.\n"

        "9. When reconstructing tables, return a fully expanded logical table where inherited values from merged cells "
        "are repeated in each affected row.\n"

        "10. Do not wrap output in markdown fences (e.g. do not write ```json ... ```) or include comments or explanations. "
        "Return raw valid JSON starting with '{' and ending with '}'.\n\n"

        f"MASTER SCHEMA TEMPLATE:\n{json.dumps(schema_template, indent=2)}\n"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Here is the markdown representation of the PDF. Extract all details into a clean JSON structure conforming to the Master Schema:\n\n{markdown_content}"}
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
    parser.add_argument(
        "--raw-output",
        default="raw_extracted.json",
        help="Path to save intermediate raw JSON (default: raw_extracted.json)",
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
    
    # 1. Parse PDF structures
    intermediate_data = build_intermediate_json(str(pdf_path))
    
    # 2. Save intermediate structure
    with open(args.raw_output, "w", encoding="utf-8") as f:
        json.dump(intermediate_data, f, indent=2, ensure_ascii=False)
    log(f"Saved intermediate structure to: {args.raw_output}")
    
    # 3. Format intermediate to Markdown
    markdown_content = convert_to_markdown(intermediate_data)
    
    # 4. Call LLM normalizer with retry validation
    try:
        final_raw_json = query_openrouter_with_retry(markdown_content, clean_template)
    except Exception as e:
        log_error(f"Failed to extract and validate JSON after multiple retries: {e}")
        sys.exit(1)
        
    # 5. Merge LLM output into the master schema template to preserve keys and add new ones
    # 5a. First, update the master schema template with any new keys (with null values)
    cleaned_raw_json = clean_schema_template(final_raw_json)
    updated_master_schema = clean_schema_template(merge_and_update_schema(master_schema, cleaned_raw_json))
    
    # Write the updated master schema template back to master_schema.json
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(updated_master_schema, f, indent=2, ensure_ascii=False)
    log(f"Updated master schema template saved to: {schema_path}")
    
    # 5b. Generate populated output by merging the LLM output with the updated master schema
    final_json = merge_and_update_schema(updated_master_schema, final_raw_json)
    
    # 6. Save final JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(final_json, f, indent=2, ensure_ascii=False)
    log(f"Saved final normalized JSON to: {args.output}")
    print("=" * 100)
    print(markdown_content)
    print("=" * 100)
    

if __name__ == "__main__":
    main()
