# prompts.py  —  keep this SHORT. Every extra token = extra seconds on a 7B local model.

SURGICAL_DIAGNOSIS_PROMPT = """Fix the bug below. ONE minimal change. No comments, no refactors, no extra output.

BUG: {bug_description}

FILE CONTEXT: {file_content}"""
