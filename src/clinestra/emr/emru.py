"""EMRU (EMR update) syntax and template text."""
import logging
custom_error_logger = logging.getLogger("clinestra.emr")

def template_to_text(template_json):
    lines = []
    metadata = template_json.get('template_metadata',{})
    lines.append(f"Template: {metadata.get('name', 'N/A')}")
    lines.append(f"Key: {metadata.get('key', 'N/A')}")
    lines.append(f"Description: {metadata.get('description', 'N/A')}")
    lines.append("=" * 100)
    lines.append("")
    
    for section in template_json.get('sections', []):
        section_id = section.get('section_id', 'N/A')
        section_name = section.get('section_name', 'N/A')
        section_description = section.get('section_description', 'N/A')
        section_type = section.get('section_type', 'N/A')
        
        lines.append(f"{section_id} : {section_name} -- {section_description} -- {section_type}")
        
        if section_type == 'regular_form':
            for field in section.get('data', []):
                lines.append(_format_field(field))
        
        elif section_type == 'table_form':
            for column in section.get('config', {}).get('columns', []):
                lines.append(_format_field(column))
        
        elif section_type == 'checklist':
            for category in section.get('config', {}).get('categories', []):
                lines.append(f"---- Category: {category.get('category', 'N/A')}")
                for item in category.get('items', []):
                    # Robust handling for string or dictionary items
                    item_text = item.get('label', item.get('value', str(item))) if isinstance(item, dict) else str(item)
                    item_id = item_text.replace(" ","_").lower()
                    lines.append(f"-------- {item_id} : {item_text}")
        
        elif section_type == 'calculator':
            for input_field in section.get('data', {}).get('inputs', []):
                key = input_field.get('key', 'N/A')
                label = input_field.get('label', 'N/A')
                field_description = input_field.get('field_description', 'N/A')
                field_type = input_field.get('type', 'N/A')
                
                line = f"---- {key} : {label} -- {field_description} -- {field_type}"
                
                if field_type == 'select':
                    options = input_field.get('options', [])
                    if options:
                        option_values = [opt.get('value', 'N/A') for opt in options]
                        line += f" -- [{', '.join(option_values)}]"
                
                lines.append(line)
        
        lines.append("")
    
    return '\n'.join(lines)

def _format_field(field):
    field_id = field.get('field_name', field.get('name', 'N/A'))
    field_name = field.get('label', field.get('field_name', field.get('name', 'N/A')))
    field_description = field.get('field_description', field.get('description', 'N/A'))
    field_type = field.get('type', 'text')
    
    line = f"---- {field_id} : {field_name} -- {field_description} -- {field_type}"
    
    if field_type in ['numerical', 'number', 'numeric']:
        min_val = field.get('min', 'N/A')
        max_val = field.get('max', 'N/A')
        unit = field.get('unit', '')
        
        line += f" -- (min: {min_val} & max: {max_val}"
        
        ref_min = field.get('reference_range_min')
        ref_max = field.get('reference_range_max')
        if ref_min is not None or ref_max is not None:
            line += f" | ref: {ref_min} - {ref_max}"
        
        line += ")"
        
        if unit:
            line += f" -- units: {unit}"
    
    elif field_type == 'select':
        options = field.get('options', [])
        if options:
            if isinstance(options[0], dict):
                option_values = [opt.get('value', opt.get('label', str(opt))) for opt in options]
            else:
                option_values = options
            
            line += f" -- [{', '.join(str(opt) for opt in option_values)}]"
    
    return line

def emru_syntax_parser(emru_syntax):
    try:
        parameters = emru_syntax.split("//")
        if len(parameters) == 4:
            core,section_id,field_id,update_type = parameters
        else:
            core,section_id,update_type = parameters
            field_id = None
        if "::" in core:
            props = core.split("::")
            core_content = props[0]
            properties = {}
            for prop in props[1:]:
                prop_name,prop_value = prop.split(":")
                properties[prop_name] = prop_value
        else:
            core_content = core
            properties = {}
        UPDATE_TYPES = {
            "pop": "Replace all",
            "upd": "Modify",
            "app": "Append",
            "add": "Add",
            "rem": "Delete"
        }
        update_type = update_type.lower().replace("~","")
        update_core_data = core_content.replace('\n','').replace('```','').strip()
        update_summary = f"{UPDATE_TYPES.get(update_type, '')} {update_core_data}"
        return {"section_id":section_id, "field_id":field_id,"data":update_core_data, "update_type":update_type, "properties":properties,"update_summary":update_summary}

    except Exception as e:
        print("Error in EMRU Syntax Parser:", str(e))
        custom_error_logger.error(msg="Error in EMRU Syntax Parser", extra={"data":{"error":str(e), "type":"emru_syntax_parser"}})



