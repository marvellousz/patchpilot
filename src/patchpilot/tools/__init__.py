"""Tools the agent can call, exposed in OpenAI function-calling format."""

import json

from patchpilot.tools.sandbox import run_command, run_tests
from patchpilot.tools.workspace import Workspace, WorkspaceError


def function_spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


STR = {"type": "string"}
INT = {"type": "integer"}

TOOL_SPECS = [
    function_spec("list_files", "List files in the repository (or under a sub-directory).",
                  {"path": STR}, []),
    function_spec("read_file", "Read a file with line numbers. Optionally pass a 1-based line range.",
                  {"path": STR, "start": INT, "end": INT}, ["path"]),
    function_spec("search_code", "Regex search across the repository. Returns file:line: text matches.",
                  {"pattern": STR, "path": STR}, ["pattern"]),
    function_spec("replace_in_file", "Replace one exact, unique occurrence of `old` with `new` in a file.",
                  {"path": STR, "old": STR, "new": STR}, ["path", "old", "new"]),
    function_spec("write_file", "Create or overwrite a file. Prefer replace_in_file for edits.",
                  {"path": STR, "content": STR}, ["path", "content"]),
    function_spec("run_tests", "Run pytest in the sandbox. Optionally target a file or test node id.",
                  {"target": STR}, []),
    function_spec("run_command", "Run a shell command in the sandbox (no network).",
                  {"command": STR}, ["command"]),
    function_spec("show_diff", "Show the current git diff of all changes made so far.", {}, []),
]


class ToolBox:
    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def call(self, name: str, arguments: str | dict) -> str:
        """Run a tool and return its result as text for the model. Errors are returned, not raised."""
        args = json.loads(arguments or "{}") if isinstance(arguments, str) else arguments
        try:
            match name:
                case "list_files":
                    return "\n".join(self.ws.list_files(args.get("path") or "."))
                case "read_file":
                    # Models often use line_start/line_end; accept both spellings.
                    start = args.get("start", args.get("line_start", 1))
                    end = args.get("end", args.get("line_end"))
                    return self.ws.read_file(args["path"], start, end)
                case "search_code":
                    return "\n".join(self.ws.search(args["pattern"], args.get("path", "."))) or "No matches."
                case "replace_in_file":
                    self.ws.replace_in_file(args["path"], args["old"], args["new"])
                    return f"Edited {args['path']}."
                case "write_file":
                    self.ws.write_file(args["path"], args["content"])
                    return f"Wrote {args['path']}."
                case "run_tests":
                    target = args.get("target") or args.get("path") or ""
                    return run_tests(self.ws.root, target).summary()
                case "run_command":
                    return run_command(self.ws.root, args["command"]).summary()
                case "show_diff":
                    return self.ws.diff() or "No changes."
                case _:
                    return f"Error: unknown tool {name}"
        except (WorkspaceError, KeyError, json.JSONDecodeError) as e:
            return f"Error: {e}"
