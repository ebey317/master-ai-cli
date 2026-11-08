# Task 06

Implement `parse_log_line` in `parser.py`. Each line has the format:
```
[LEVEL] timestamp=ISO key1=value1;key2=value2 ... freeform message
```
Return a dict with keys: `level`, `timestamp`, `fields` (dict of key/value pairs), and `message` (the remaining freeform text). Semicolons separate fields; spaces separate the message tail from the last field.

Repo: `repo/`
