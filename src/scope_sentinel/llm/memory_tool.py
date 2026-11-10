"""Executor for Claude's client-side memory tool (`memory_20250818`) over any MemoryBackend.

Claude sees a `/memories` directory. We map it onto the backend root, allow reads everywhere, and allow
writes only under WRITABLE prefixes (least privilege: the agent may curate /project/, never rewrite the
machine-written story dossiers). Return strings follow the tool's documented formats.
"""
from __future__ import annotations

from ..memory.backends import MemoryBackend, norm_path

ROOT = "/memories"
WRITABLE = ("/project/",)


class MemoryToolExecutor:
    def __init__(self, backend: MemoryBackend, writable: tuple[str, ...] = WRITABLE):
        self.b = backend
        self.writable = writable
        self.calls: list[dict] = []

    # ---- helpers
    def _map(self, p: str) -> str:
        p = (p or "").strip()
        if not (p == ROOT or p.startswith(ROOT + "/")):
            raise ValueError(f"The path {p} does not exist. Please provide a valid path.")
        if "%2e" in p.lower() or ".." in p.split("/"):
            raise ValueError(f"The path {p} does not exist. Please provide a valid path.")
        return norm_path(p[len(ROOT):] or "/")

    def _can_write(self, bp: str) -> bool:
        return any(bp.startswith(w) for w in self.writable)

    def run(self, cmd: dict) -> str:
        self.calls.append(cmd)
        c = cmd.get("command")
        try:
            return getattr(self, "_" + c)(cmd)
        except (ValueError, FileNotFoundError, FileExistsError, PermissionError) as e:
            return f"Error: {e}"
        except AttributeError:
            return f"Error: unknown command {c!r}"

    # ---- commands
    def _view(self, cmd):
        p = cmd["path"]
        bp = self._map(p)
        entries = self.b.list("/")
        body = self.b.read(bp) if bp != "/" else None
        if body is not None:
            lines = body.splitlines()
            vr = cmd.get("view_range")
            start = 1
            if vr:
                start, end = int(vr[0]), int(vr[1])
                lines = lines[start - 1: end]
            out = "\n".join(f"{i:6}\t{ln}" for i, ln in enumerate(lines, start))
            return f"Here's the content of {p} with line numbers:\n{out}"
        pre = bp.rstrip("/") + "/"
        under = [e for e in entries if e.path.startswith(pre)] if bp != "/" else entries
        if bp != "/" and not under:
            return f"The path {p} does not exist. Please provide a valid path."
        shown: dict[str, int] = {}
        for e in under:
            rel = e.path[len(pre):] if bp != "/" else e.path.lstrip("/")
            parts = rel.split("/")
            shown["/".join(parts[:2]) if len(parts) > 1 else parts[0]] = e.size
        lines = [f"Here're the files and directories up to 2 levels deep in {p}, excluding hidden items and node_modules:"]
        lines += [f"{sz}\t{p.rstrip('/')}/{name}" for name, sz in sorted(shown.items())]
        return "\n".join(lines)

    def _create(self, cmd):
        bp = self._map(cmd["path"])
        if not self._can_write(bp):
            raise PermissionError(f"{cmd['path']} is read-only. You may only write under {ROOT}/project/")
        if self.b.exists(bp):
            return f"Error: File {cmd['path']} already exists"
        self.b.write(bp, cmd.get("file_text", ""))
        return f"File created successfully at: {cmd['path']}"

    def _str_replace(self, cmd):
        bp = self._map(cmd["path"])
        if not self._can_write(bp):
            raise PermissionError(f"{cmd['path']} is read-only. You may only write under {ROOT}/project/")
        body = self.b.read(bp)
        if body is None:
            raise FileNotFoundError(f"The path {cmd['path']} does not exist. Please provide a valid path.")
        old, new = cmd["old_str"], cmd.get("new_str", "")
        n = body.count(old)
        if n == 0:
            return f"No replacement was performed, old_str `{old}` did not appear verbatim in {cmd['path']}."
        if n > 1:
            return f"No replacement was performed. Multiple occurrences of old_str `{old}`. Please ensure it is unique"
        self.b.write(bp, body.replace(old, new, 1))
        return "The memory file has been edited."

    def _insert(self, cmd):
        bp = self._map(cmd["path"])
        if not self._can_write(bp):
            raise PermissionError(f"{cmd['path']} is read-only. You may only write under {ROOT}/project/")
        body = self.b.read(bp)
        if body is None:
            raise FileNotFoundError(f"The path {cmd['path']} does not exist")
        lines = body.splitlines()
        n = int(cmd["insert_line"])
        if n < 0 or n > len(lines):
            return f"Error: Invalid `insert_line` parameter: {n}. It should be within the range of lines of the file: [0, {len(lines)}]"
        lines[n:n] = cmd.get("insert_text", "").splitlines()
        self.b.write(bp, "\n".join(lines) + "\n")
        return f"The file {cmd['path']} has been edited."

    def _delete(self, cmd):
        bp = self._map(cmd["path"])
        if not self._can_write(bp):
            raise PermissionError(f"{cmd['path']} is read-only. You may only write under {ROOT}/project/")
        if not self.b.delete(bp):
            raise FileNotFoundError(f"The path {cmd['path']} does not exist")
        return f"Successfully deleted {cmd['path']}"

    def _rename(self, cmd):
        o, n = self._map(cmd["old_path"]), self._map(cmd["new_path"])
        if not (self._can_write(o) and self._can_write(n)):
            raise PermissionError(f"You may only rename within {ROOT}/project/")
        if not self.b.exists(o):
            raise FileNotFoundError(f"The path {cmd['old_path']} does not exist")
        if self.b.exists(n):
            return f"Error: The destination {cmd['new_path']} already exists"
        self.b.rename(o, n)
        return f"Successfully renamed {cmd['old_path']} to {cmd['new_path']}"
