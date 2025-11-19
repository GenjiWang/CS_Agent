"""
JSON utility functions for safe JSON serialization.
"""
import json


def json_dumps(obj: dict) -> str:
    """
    Safely serialize a dictionary to JSON string with Unicode support.
    
    Args:
        obj: Dictionary to serialize
        
    Returns:
        JSON string with ensure_ascii=False for proper Unicode handling
    """
    return json.dumps(obj, ensure_ascii=False)
