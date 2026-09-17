"""examples/mcp_editor.py, driven over MCP JSON-RPC in-process."""

from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest

micromcp = pytest.importorskip("micromcp")
pytest.importorskip("jsonpath_rfc9535")

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMDOC_EDITOR_DIR", str(tmp_path / "documents"))
    monkeypatch.syspath_prepend(str(EXAMPLES))
    sys.modules.pop("mcp_editor", None)
    example = importlib.import_module("mcp_editor")
    server = micromcp.Server(example.mcp)

    def rpc(method, params):
        params = {**params, "_meta": {micromcp.META_VER: micromcp.PROTOCOL, micromcp.META_CAPS: {}}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw), "HTTP_MCP_PROTOCOL_VERSION": micromcp.PROTOCOL,
               "HTTP_MCP_METHOD": method}
        if "name" in params:
            env["HTTP_MCP_NAME"] = params["name"]
        box = {}
        body = b"".join(server(env, lambda status, headers: box.update(status=status)))
        return int(box["status"].split()[0]), json.loads(body)

    def tool(name, **arguments):
        status, body = rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in body:
            return status, body["error"]
        result = body["result"]
        return result.get("isError", False), result.get("structuredContent")

    tool.rpc = rpc
    yield tool
    sys.modules.pop("mcp_editor", None)


def test_the_tools_are_listed(call):
    status, body = call.rpc("tools/list", {})
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {"document_schema", "read_document", "edit_document", "complete_task"}
    assert tools["read_document"]["annotations"]["readOnlyHint"] is True
    assert tools["edit_document"]["annotations"]["destructiveHint"] is True
    assert "JSONPath" in tools["edit_document"]["description"]


def test_read_edit_and_a_stale_version(call):
    error, read = call("read_document", document="demo", path="$..tasks[?@.title=='deploy']", depth=0)
    assert not error and read["matched"] == 1
    error, edited = call("edit_document", document="demo", version=read["version"], ops=[
        {"op": "set", "path": "$..tasks[?@.title=='deploy'].status", "value": "in_progress"},
        {"op": "insert", "path": "$.milestones[?@.name=='launch'].tasks",
         "value": {"$type": "Task", "title": "smoke test"}, "after": "$..tasks[?@.title=='deploy']"}])
    assert not error and edited["changed"] and len(edited["changes"]) == 2
    error, refused = call("edit_document", document="demo", version=read["version"],
                          ops=[{"op": "delete", "path": "$..tasks[?@.title=='deploy']"}])
    assert error and refused["error"]["code"] == "conflict"
    error, titles = call("read_document", document="demo",
                         path="$.milestones[?@.name=='launch'].tasks[*].title")
    assert [r["value"] for r in titles["results"]] == ["docs", "deploy", "smoke test", "announce"]


def test_frozen_values_through_the_tool(call):
    error, _ = call("edit_document", document="demo", ops=[
        {"op": "set", "path": "$..tasks[?@.title=='ci'].estimate", "value": {"days": 1}}])
    assert not error
    error, refused = call("edit_document", document="demo", ops=[
        {"op": "set", "path": "$..tasks[?@.title=='ci'].estimate.days", "value": 3}])
    assert error and refused["error"]["code"] == "not_settable"
    assert refused["error"]["current"] == {"days": 1.0, "confidence": "high"}


def test_the_domain_tool_meets_the_documents_rule(call):
    _, read = call("read_document", document="demo", path="$..tasks[?@.title=='repo']", depth=0)
    repo = read["results"][0]["$id"]
    error, refused = call("complete_task", document="demo", task=repo)
    assert error and refused["error"]["code"] == "invalid_document"
    assert "needs an estimate" in refused["error"]["message"]
    error, done = call("complete_task", document="demo", task=repo, days=0.5)
    assert not error and done["changed"]


def test_a_malformed_operation_never_reaches_the_document(call):
    status, error = call("edit_document", document="demo", ops=[{"op": "rename", "path": "$"}])
    assert status == 400 and error["code"] == -32602


def test_the_schema_tool(call):
    error, schema = call("document_schema", document="demo")
    assert not error and schema["root_type"] == "Project"
    assert schema["node_types"]["Task"]["fields"]["assignee"]["references"] == "Person"
