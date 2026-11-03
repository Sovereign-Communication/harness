#!/usr/bin/env python3
import os
import re
import sys

# Search for lucas_gate_v2 in the codebase
found = []
for root, dirs, files in os.walk('.'):
    for f in files:
        filepath = os.path.join(root, f)
        try:
            with open(filepath) as tf:
                content = tf.read()
                if 'lucas_gate_v2' in content:
                    found.append(filepath)
        except:
            pass

print('Files with lucas_gate_v2:', found)

# Check tool count
sys.path.insert(0, '.')
import harness.mcp_schemas as ms
print('TOOL_SCHEMAS count:', len(ms.TOOL_SCHEMAS))

# Check gate-related tool names
with open('harness/mcp_schemas.py') as tf:
    content = tf.read()
gate_tools = re.findall(r'"name":\s*"([^"]+)"', content)
gate_names = [t for t in gate_tools if any(kw in t.lower() for kw in ['gate', 'lucas', 'verify', 'plan'])]
print('Gate/verify/lucas tool names:', gate_names)