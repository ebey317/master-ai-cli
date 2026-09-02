#!/usr/bin/env python3
"""Phase 6 E2E smoke tests for MCP catalog and profile isolation."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai
import sensei_mcp_client


def test_mcp_add_remove():
    server_script = """import sys, json
while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    if req.get('method') == 'initialize':
        res = {'jsonrpc':'2.0','id':req['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'smoke','version':'1.0'}}}
    elif req.get('method') == 'tools/list':
        res = {'jsonrpc':'2.0','id':req['id'],'result':{'tools':[{'name':'smoke_tool','description':'x','inputSchema':{'type':'object'}}]}}
    else:
        res = {'jsonrpc':'2.0','id':req.get('id'),'result':{}}
    print(json.dumps(res))
    sys.stdout.flush()
"""
    tmpdir = Path(tempfile.mkdtemp())
    script = tmpdir / "mcp_smoke.py"
    script.write_text(server_script)
    target = f"{sys.executable} {script}"
    name = "phase6_smoke"

    sensei_mcp_client.remove_server(name)
    res = sensei_mcp_client.add_server(name, target, "stdio")
    assert res["ok"], f"add failed: {res.get('message')}"
    cat = sensei_mcp_client._load_catalog()
    assert name in cat["servers"], "server not added"
    assert cat["servers"][name]["enabled"], "server not enabled after probe"
    assert "smoke_tool" in cat["servers"][name]["tool_names"], "tool missing"

    rem = sensei_mcp_client.remove_server(name)
    assert rem["ok"], "remove failed"
    cat = sensei_mcp_client._load_catalog()
    assert name not in cat["servers"], "server not removed"
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ mcp add/remove e2e passed")


def test_profile_switch_isolation():
    base = Path.home() / ".master_ai_profiles"
    for p in ["phase6_a", "phase6_b"]:
        shutil.rmtree(base / p, ignore_errors=True)
        (base / p).mkdir(parents=True, exist_ok=True)

    (base / "phase6_a" / "config.json").write_text(json.dumps({"name": "phase6_a"}))
    (base / "phase6_b" / "config.json").write_text(json.dumps({"name": "phase6_b"}))

    mem_file_a = base / "phase6_a" / "memory.jsonl"
    mem_file_b = base / "phase6_b" / "memory.jsonl"
    mem_file_a.write_text(json.dumps({"text": "profile A secret"}) + "\n")
    mem_file_b.write_text(json.dumps({"text": "profile B secret"}) + "\n")

    try:
        master_ai._activate_profile("phase6_a")
        a_mem = str(master_ai._pfile("memory.jsonl"))
        assert "phase6_a" in a_mem and Path(a_mem).exists(), "profile A memory path wrong"

        master_ai._activate_profile("phase6_b")
        b_mem = str(master_ai._pfile("memory.jsonl"))
        assert "phase6_b" in b_mem and Path(b_mem).exists(), "profile B memory path wrong"

        b_text = Path(b_mem).read_text()
        assert "profile A secret" not in b_text, "profile isolation broken"
        print("✓ profile switch isolation e2e passed")
    finally:
        master_ai._activate_profile("default")
        for p in ["phase6_a", "phase6_b"]:
            shutil.rmtree(base / p, ignore_errors=True)


if __name__ == "__main__":
    test_mcp_add_remove()
    test_profile_switch_isolation()
    print("Phase 6 E2E checks OK")
