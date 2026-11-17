"""Section -> markdown for finalized canvas pages."""
import re
import html as _html

def _html_to_markdown_basic(html_text: str) -> str:
    """Convert simple HTML fragments to a clean Markdown string.
    Handles <ul>/<ol>/<li>, <p>, <br>, <strong>, <em>, <b>, <i>, and strips remaining tags.
    Preserves structure for nested content like headers followed by lists.
    """
    if not html_text:
        return ""
    text = html_text
    
    # Normalize breaks
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    
    # Handle bold/strong tags (convert to markdown bold)
    text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    
    # Handle italic/emphasis tags (convert to markdown italic)
    text = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    
    # Handle underline tags (convert to markdown bold as fallback since markdown doesn't have underline)
    text = re.sub(r"<u[^>]*>(.*?)</u>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    
    # Handle ordered lists: convert to numbered list
    def _convert_ol(match):
        inner = match.group(1)
        items = re.findall(r"<li[^>]*>(.*?)</li>", inner, flags=re.IGNORECASE | re.DOTALL)
        lines = []
        for idx, item in enumerate(items, start=1):
            clean = _strip_tags(item).strip()
            if clean:
                lines.append(f"{idx}. {clean}")
        return "\n" + "\n".join(lines) + "\n" if lines else ""
    text = re.sub(r"<ol[^>]*>([\s\S]*?)</ol>", _convert_ol, text, flags=re.IGNORECASE)
    
    # Handle unordered lists: convert to dash bullets
    def _convert_ul(match):
        inner = match.group(1)
        items = re.findall(r"<li[^>]*>(.*?)</li>", inner, flags=re.IGNORECASE | re.DOTALL)
        lines = []
        for item in items:
            clean = _strip_tags(item).strip()
            if clean:
                lines.append(f"- {clean}")
        return "\n" + "\n".join(lines) + "\n" if lines else ""
    text = re.sub(r"<ul[^>]*>([\s\S]*?)</ul>", _convert_ul, text, flags=re.IGNORECASE)
    
    # Handle headings (h1-h6) - convert to markdown headings
    for level in range(1, 7):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m: f"{'#' * level} {_strip_tags(m.group(1)).strip()}\n\n",
            text,
            flags=re.IGNORECASE | re.DOTALL
        )
    
    # Convert paragraphs: handle closing tag carefully
    # If paragraph is followed by a list, use single newline, otherwise double newline
    text = re.sub(r"</p>\s*(?=\n)", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    # Strip any remaining tags
    text = _strip_tags(text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text



def html_to_markdown(html_text: str) -> str:
    if not html_text:
        return ""

    try:
        from markdownify import markdownify as md
    except ImportError:  # optional dependency: fall back to the built-in converter
        return _html_to_markdown_basic(html_text)
    return md(
        html_text,
        heading_style="ATX",      # ### style headers
        bullets="-",              # dash bullets
    ).strip()
def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)

def process_section_to_markdown(section: dict) -> str:
    """Create a Markdown representation for a single section based on its type.
    This function is reusable and can be imported/used elsewhere.
    """
    section_name = section.get("section_name")  or "-"
    section_type = (section.get("section_type") or "").lower()
    header = f"## {section_name}".strip()

    # text_area: HTML string in data
    if section_type == "text_area":
        content_html = section.get("data") or ""
        if not content_html or content_html == "":
            return None
        content = f"""
        <p style="color: red;">
        {content_html}
        </p>

        """
        body = html_to_markdown(content_html)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}"
        
        #  if body else header

    # unordered_list / ordered_list: expect list of strings in data
    if section_type in ("unordered_list", "ordered_list"):
        # items = section.get("items") or []
        # if not items or items == []:
        #     return None
        # if not isinstance(items, list):
        #     return header
        # lines = []
        # if section_type == "unordered_list":
        #     for item in items:
        #         if isinstance(item, str) and item.strip():
        #             lines.append(f"- {item.strip()}")
        # else:
        #     idx = 1
        #     for item in items:
        #         if isinstance(item, str) and item.strip():
        #             lines.append(f"{idx}. {item.strip()}")
        #             idx += 1
        # body = "\n".join(lines)
        content_html = section.get("data")
        if not content_html:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        else:
            body = html_to_markdown(content_html)
        return f"{header}\n\n{body}" if body else header

    # checklist: expect list of objects with 'label' and 'checked' fields
    if section_type == "checklist":
        items = section.get("items") or []
        if not items or items == []:
            return None
        lines = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    label = item.get("label", "")
                    checked = item.get("checked", False)
                    checkbox = "[x]" if checked else "[ ]"
                    if label.strip():
                        lines.append(f"- {checkbox} {label.strip()}")
                elif isinstance(item, str) and item.strip():
                    # Fallback for simple string items
                    lines.append(f"- [x] {item.strip()}")
        elif isinstance(items, dict):
            # category -> [items] (alternative structure)
            for category, cat_items in items.items():
                lines.append(f"#### {category}")
                for item in cat_items or []:
                    if isinstance(item, dict):
                        label = item.get("label", "")
                        checked = item.get("checked", False)
                        checkbox = "[x]" if checked else "[ ]"
                        if label.strip():
                            lines.append(f"- {checkbox} {label.strip()}")
                    else:
                        lines.append(f"- [x] {str(item).strip()}")
        body = "\n".join(lines)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}"

    # tasks: expect list of objects with 'label'/'value' and 'checked' fields
    if section_type == "tasks":
        items = section.get("items") or []
        if not items or items == []:
            return None
        lines = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    label = item.get("label") or item.get("value", "")
                    checked = item.get("checked", False)
                    checkbox = "[x]" if checked else "[ ]"
                    if label.strip():
                        lines.append(f"- {checkbox} {label.strip()}")
        body = "\n".join(lines)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}"

    # regular_form: expect array of fields with optional 'value'
    if section_type == "regular_form":
        fields = section.get("data") or []
        if not fields or fields == []:
            return None
        items = []
        if isinstance(fields, list):
            for field in fields:
                label = field.get("label") or field.get("field_name")
                value = field.get("value")
                if value not in (None, "") and label:
                    items.append(f"{label} : **{value}**")
        body = "   |   ".join(items)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}" if body else header

    # table_form: expect list of row objects in data
    if section_type == "table_form":
        rows = section.get("data") or []
        if not rows or rows == []:
            return None
        columns = []
        cfg_cols = ((section.get("config") or {}).get("columns") or [])
        for col in cfg_cols:
            name = col.get("name")
            if name:
                columns.append(name)
        # Build markdown table if we have both columns and rows
        if columns and isinstance(rows, list) and rows:
            header_row = " | ".join(columns)
            sep_row = " | ".join(["---"] * len(columns))
            data_rows = []
            for r in rows:
                data_rows.append(" | ".join([str((r or {}).get(c, "")).strip() for c in columns]))
            body = "\n".join([header_row, sep_row] + data_rows)
            if not body:
                body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
            return f"{header}\n\n{body}"
        return f"{header}\n\n<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"

    if section_type == "calculator":
        data_obj = section.get("data") or {}
        if not data_obj:
            return f"{header}\n\n<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
            
        calculator_json = data_obj.get("calculator_json", {})
        inputs = calculator_json.get("inputs", [])
        output = calculator_json.get("output", {})
        
        submission_data = data_obj.get("submission_data", {})
        interpretation_data = data_obj.get("interpretation_data", {})
        
        items = []
        if isinstance(inputs, list):
            for field in inputs:
                label = field.get("label") or field.get("key", "")
                key = field.get("key", "")
                value = submission_data.get(key)
                
                if value not in (None, "") and label:
                    items.append(f"{label} : **{value}**")
                    
        result_value = interpretation_data.get("result")
        if result_value not in (None, ""):
            output_label = "Score"
            if isinstance(output, dict):
                output_label = output.get("label", "Score")
            items.append(f"{output_label} : **{result_value}**")
            
        interpretation = interpretation_data.get("interpretation")
        if interpretation:
            interpretation_str = ""
            if isinstance(interpretation, str):
                interpretation_str = interpretation
            elif isinstance(interpretation, dict):
                interpretation_str = interpretation.get("message", "")
                
            if interpretation_str:
                items.append(f"Interpretation : **{interpretation_str}**")

        body = "   \n   ".join(items)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}"

    # generic dict data: key-value render
    data_obj = section.get("data")
    if not data_obj or data_obj == {}:
        return None
    if isinstance(data_obj, dict) and data_obj:
        lines = [f"- {k}: {v}" for k, v in data_obj.items()]
        body = "\n".join(lines)
        if not body:
            body = f"<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"
        return f"{header}\n\n{body}"

    return f"{header}\n\n<span style='opacity:0.4;font-size:12px'>No {section_name} recorded</span>"

