"""单提交 REST 推送（push_api.py 的精简版，中文文件名场景专用）。

push_api.py 用 git ls-tree 构建 tree payload，中文路径被 quotepath 转义成
八进制 → GitHub 422 Invalid tree info。本脚本改为：每个变更文件单独传 blob，
路径直接写在 JSON 里（UTF-8 原文），绕开 ls-tree。

SHA 一致性要求 commit 五元组 (tree, parents, author, committer, message)
与本地对象逐字节一致——author/committer 的日期带时区偏移（+0800），
GitHub 会在计算 SHA 时保留它；message 用 cat-file partition 原文（%B 会
多一个分隔换行导致 SHA 漂移，踩过）。

用法：python scripts/_push_one.py <commit_sha> <file_path>...
（文件列表必须恰好覆盖该提交的全部变更，父提交从对象头读取）
"""

import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = "qqc20043/aigotutor"


def gh(args, body=None):
    cmd = ["gh", "api", args, "--input", "-"] if body is not None else ["gh", "api", args]
    inp = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = subprocess.run(cmd, input=inp, capture_output=True)
    if r.returncode != 0:
        print("FAIL:", r.stderr.decode("utf-8", "replace")[:300])
        raise SystemExit(1)
    out = r.stdout.decode("utf-8").strip()
    return json.loads(out) if out else {}


def split_ident(line: str) -> dict:
    """'Name <email> 1793547245 +0800' -> {'name', 'email', 'date': ISO}"""
    lt, gt = line.rindex("<"), line.rindex(">")
    name, email = line[:lt].strip(), line[lt + 1 : gt]
    ts, tz = line[gt + 1 :].split()
    iso = datetime.fromtimestamp(int(ts), timezone(timedelta(hours=int(tz) // 100))).isoformat()
    return {"name": name, "email": email, "date": iso}


def main() -> int:
    commit_sha, *paths = sys.argv[1:]
    raw = subprocess.run(
        ["git", "cat-file", "commit", commit_sha], capture_output=True
    ).stdout.decode("utf-8")
    headers, _, message = raw.partition("\n\n")
    tree = parent = author_line = committer_line = None
    for line in headers.splitlines():
        if line.startswith("tree "):
            tree = line.split()[1]
        elif line.startswith("parent "):
            parent = line.split()[1]
        elif line.startswith("author "):
            author_line = line[len("author ") :]
        elif line.startswith("committer "):
            committer_line = line[len("committer ") :]
    assert tree and parent and author_line and committer_line, "提交头解析失败"

    tree_entries = []
    for path in paths:
        with open(path, "rb") as f:
            content = f.read()
        blob = gh(
            f"/repos/{REPO}/git/blobs",
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # base_tree 用父提交的 tree：父 tree 一定在远端（父提交已推过）
    parent_tree = gh(f"/repos/{REPO}/git/commits/{parent}")["tree"]["sha"]
    new_tree = gh(f"/repos/{REPO}/git/trees", {"base_tree": parent_tree, "tree": tree_entries})
    if new_tree.get("sha") != tree:
        print(f"tree 不一致: api={new_tree.get('sha', '')[:10]} local={tree[:10]}")
        return 1

    commit = gh(
        f"/repos/{REPO}/git/commits",
        {
            "message": message,
            "tree": tree,
            "parents": [parent],
            "author": split_ident(author_line),
            "committer": split_ident(committer_line),
        },
    )
    if commit.get("sha") != commit_sha:
        print(f"commit 不一致: api={commit.get('sha', '')[:10]} local={commit_sha[:10]}")
        return 1

    gh(f"/repos/{REPO}/git/refs/heads/main", {"sha": commit["sha"], "force": False})
    print("PUSH-OK", commit["sha"][:7])
    return 0


if __name__ == "__main__":
    sys.exit(main())
