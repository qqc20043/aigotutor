"""内置题包：经典算法题数据（原创题面 + 预验证测试用例）。

每题: dict(title, tags, difficulty, statement, input_desc, output_desc,
           constraints, solve: callable[[str], str], testcases: [(stdin, stdout)])
solve(stdin_lines_list) -> stdout_str，testcases 的 expected 已用 solver 验证。

seed_problems.py 导入这些题到数据库。
"""

from __future__ import annotations

import heapq
import json
import math
from collections import deque
from typing import Any

# ---------------------------------------------------------------- 求解函数


def solve_climb_stairs(stdin: str) -> str:
    n = int(stdin.strip())
    a, b = 1, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return f"{b}\n"


def solve_gcd_lcm(stdin: str) -> str:
    a, b = map(int, stdin.split())
    return f"{math.gcd(a, b)} {a * b // math.gcd(a, b)}\n"


def solve_sort_scores(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    n = int(lines[0])
    data = [(parts[0], int(parts[1])) for parts in (line.split() for line in lines[1 : n + 1])]
    data.sort(key=lambda x: (-x[1], x[0]))
    return "".join(f"{name} {score}\n" for name, score in data)


def solve_two_sum(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    nums = json.loads(lines[0])
    target = int(lines[1])
    seen = {}
    for i, x in enumerate(nums):
        if target - x in seen:
            return f"{seen[target - x]} {i}\n"
        seen[x] = i
    return "-1\n"


def solve_josephus(stdin: str) -> str:
    n, k = map(int, stdin.split())
    people = list(range(1, n + 1))
    idx = 0
    result = []
    while people:
        idx = (idx + k - 1) % len(people)
        result.append(people.pop(idx))
    return " ".join(map(str, result)) + "\n"


def solve_binary_search(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    arr = json.loads(lines[0])
    target = int(lines[1])
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return f"{mid}\n"
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return "-1\n"


def solve_interval_cover(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    n = int(lines[0])
    intervals = sorted((int(line.split()[0]), int(line.split()[1])) for line in lines[1 : n + 1])
    merged = 0
    last_r = float("-inf")
    for lo, hi in intervals:
        if lo > last_r:
            merged += 1
            last_r = hi
        else:
            last_r = max(last_r, hi)
    return f"{merged}\n"


def solve_prefix_sum(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    _n, q = map(int, lines[0].split())
    arr = list(map(int, lines[1].split()))
    prefix = [0]
    for x in arr:
        prefix.append(prefix[-1] + x)
    out = []
    for i in range(q):
        lo, hi = map(int, lines[2 + i].split())
        out.append(str(prefix[hi] - prefix[lo - 1]))
    return "\n".join(out) + "\n"


def solve_no_repeat_longest(stdin: str) -> str:
    s = stdin.strip()
    seen = {}
    start = best = 0
    for i, ch in enumerate(s):
        if ch in seen and seen[ch] >= start:
            start = seen[ch] + 1
        seen[ch] = i
        best = max(best, i - start + 1)
    return f"{best}\n"


def solve_bracket_count(stdin: str) -> str:
    n = int(stdin.strip())
    from math import comb

    return f"{comb(2 * n, n) // (n + 1)}\n"


def solve_lis(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    nums = list(map(int, lines[1].split()))
    tails: list[int] = []
    for x in nums:
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tails):
            tails.append(x)
        else:
            tails[lo] = x
    return f"{len(tails)}\n"


def solve_lcs(stdin: str) -> str:
    a, b = stdin.strip().splitlines()
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return f"{dp[m][n]}\n"


def solve_edit_distance(stdin: str) -> str:
    a, b = stdin.strip().splitlines()
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return f"{dp[m][n]}\n"


def solve_knapsack(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    n, capacity = map(int, lines[0].split())
    weights = list(map(int, lines[1].split()))
    values = list(map(int, lines[2].split()))
    dp = [0] * (capacity + 1)
    for i in range(n):
        for j in range(capacity, weights[i] - 1, -1):
            dp[j] = max(dp[j], dp[j - weights[i]] + values[i])
    return f"{dp[capacity]}\n"


def solve_sieve(stdin: str) -> str:
    n = int(stdin.strip())
    if n < 2:
        return "\n"
    is_p = [True] * (n + 1)
    is_p[0] = is_p[1] = False
    for i in range(2, int(n**0.5) + 1):
        if is_p[i]:
            for j in range(i * i, n + 1, i):
                is_p[j] = False
    return " ".join(str(i) for i in range(2, n + 1) if is_p[i]) + "\n"


def solve_grid_bfs(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    rows, cols = map(int, lines[0].split())
    grid = [[int(x) for x in lines[1 + r].split()] for r in range(rows)]
    dist = [[-1] * cols for _ in range(rows)]
    dist[0][0] = 1
    q = deque([(0, 0)])
    while q:
        r, c = q.popleft()
        for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == 0 and dist[nr][nc] == -1:
                dist[nr][nc] = dist[r][c] + 1
                q.append((nr, nc))
    return f"{dist[rows - 1][cols - 1]}\n"


def solve_island_count(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    rows, cols = map(int, lines[0].split())
    grid = [[int(x) for x in lines[1 + r].split()] for r in range(rows)]
    visited = [[False] * cols for _ in range(rows)]

    def dfs(r, c):
        stack = [(r, c)]
        while stack:
            cr, cc = stack.pop()
            if not (0 <= cr < rows and 0 <= cc < cols) or grid[cr][cc] == 0 or visited[cr][cc]:
                continue
            visited[cr][cc] = True
            stack.extend([(cr + dr, cc + dc) for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0))])

    count = 0
    for r in range(rows):
        for c in range(cols):
            if grid[r][c] == 1 and not visited[r][c]:
                count += 1
                dfs(r, c)
    return f"{count}\n"


def solve_n_queens(stdin: str) -> str:
    n = int(stdin.strip())
    count = 0

    def solve(row, cols, d1, d2):
        nonlocal count
        if row == n:
            count += 1
            return
        for col in range(n):
            if cols & (1 << col) or d1 & (1 << (row + col)) or d2 & (1 << (row - col + n)):
                continue
            solve(row + 1, cols | (1 << col), d1 | (1 << (row + col)), d2 | (1 << (row - col + n)))

    solve(0, 0, 0, 0)
    return f"{count}\n"


def solve_segment_query(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    _n, q = map(int, lines[0].split())
    arr = list(map(int, lines[1].split()))
    prefix = [0]
    for x in arr:
        prefix.append(prefix[-1] + x)
    out = []
    for i in range(q):
        lo, hi = map(int, lines[2 + i].split())
        out.append(str(prefix[hi] - prefix[lo - 1]))
    return "\n".join(out) + "\n"


def solve_topo_sort(stdin: str) -> str:
    lines = stdin.strip().splitlines()
    n, m = map(int, lines[0].split())
    adj: list[list[int]] = [[] for _ in range(n + 1)]
    in_deg = [0] * (n + 1)
    for i in range(m):
        u, v = map(int, lines[1 + i].split())
        adj[u].append(v)
        in_deg[v] += 1
    q = [i for i in range(1, n + 1) if in_deg[i] == 0]
    heapq.heapify(q)
    order = []
    while q:
        u = heapq.heappop(q)
        order.append(u)
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] == 0:
                heapq.heappush(q, v)
    return " ".join(map(str, order)) + "\n"


# ---------------------------------------------------------------- 题包
def P(title, tags, difficulty, statement, input_desc, output_desc, constraints, solve, testcases):
    return {
        "title": title,
        "tags": tags,
        "difficulty": difficulty,
        "statement": statement,
        "input_desc": input_desc,
        "output_desc": output_desc,
        "constraints": constraints,
        "solve": solve,
        "testcases": testcases,
    }


PROBLEMS: list[dict[str, Any]] = [
    P(
        "爬楼梯",
        ["动态规划", "数学"],
        "入门",
        "你正在爬一个有 n 阶台阶的楼梯，每次可以爬 1 或 2 阶。求有多少种不同的方法可以爬到楼顶。",
        "一行一个整数 n",
        "一行一个整数，表示方法数",
        "1 <= n <= 45",
        solve_climb_stairs,
        [("5\n", "8\n"), ("10\n", "89\n"), ("45\n", "1836311903\n")],
    ),
    P(
        "最大公约数与最小公倍数",
        ["数学"],
        "入门",
        "给定两个正整数 a 和 b，求它们的最大公约数 GCD 和最小公倍数 LCM。",
        "一行两个正整数 a b，空格分隔",
        "一行两个整数，分别为 GCD 和 LCM，空格分隔",
        "1 <= a, b <= 10^9",
        solve_gcd_lcm,
        [("12 18\n", "6 36\n"), ("100 75\n", "25 300\n"), ("7 13\n", "1 91\n")],
    ),
    P(
        "成绩排序",
        ["排序"],
        "入门",
        "给定 n 个学生的姓名和成绩，按成绩从高到低排序输出。成绩相同的按姓名字典序升序。",
        "第一行一个整数 n。接下来 n 行，每行一个字符串 name 和一个整数 score，空格分隔。",
        "n 行，每行一个字符串和一个整数，空格分隔。",
        "1 <= n <= 1000，成绩 0-100",
        solve_sort_scores,
        [
            ("3\n甲 90\n乙 85\n丙 90\n", "丙 90\n甲 90\n乙 85\n"),
            ("4\n丁 70\n甲 95\n乙 70\n丙 95\n", "丙 95\n甲 95\n丁 70\n乙 70\n"),
        ],
    ),
    P(
        "两数之和",
        ["哈希", "数组"],
        "普及-",
        "给定一个整数数组 nums 和目标值 target，找出数组中和为目标值的两个整数的下标（0-indexed）。数据保证恰好有一组解。",
        "第一行一个整数数组（JSON 格式）。第二行一个整数 target。",
        "一行两个整数，空格分隔，下标从小到大。",
        "2 <= len(nums) <= 2000",
        solve_two_sum,
        [("[2,7,11,15]\n9\n", "0 1\n"), ("[3,2,4]\n6\n", "1 2\n"), ("[3,3]\n6\n", "0 1\n")],
    ),
    P(
        "约瑟夫环",
        ["模拟"],
        "普及-",
        "n 个人围成一圈，从第 1 个人开始报数，每数到第 k 个人出列。输出全部出列顺序。",
        "一行两个整数 n k，空格分隔。",
        "一行 n 个整数，空格分隔，表示出列顺序。",
        "1 <= n <= 1000，1 <= k <= 1000",
        solve_josephus,
        [("5 3\n", "3 1 5 2 4\n"), ("7 2\n", "2 4 6 1 5 3 7\n"), ("1 1\n", "1\n")],
    ),
    P(
        "二分查找",
        ["二分", "数组"],
        "普及-",
        "给定一个升序排列的整数数组和目标值，用二分查找返回目标值的下标（0-indexed）。不存在则输出 -1。",
        "第一行一个升序整数数组（JSON 格式）。第二行一个整数 target。",
        "一行一个整数。",
        "1 <= len(arr) <= 10000，数组元素互不相同",
        solve_binary_search,
        [("[1,3,5,7,9,11]\n7\n", "3\n"), ("[1,3,5,7,9,11]\n4\n", "-1\n"), ("[5]\n5\n", "0\n")],
    ),
    P(
        "区间合并计数",
        ["贪心", "排序"],
        "普及-",
        "给定 n 个区间 [l, r]，合并所有重叠的区间，输出合并后剩余的区间数量。",
        "第一行一个整数 n。接下来 n 行，每行两个整数 l r。",
        "一行一个整数，表示合并后的区间数量。",
        "1 <= n <= 100000，-10^9 <= l <= r <= 10^9",
        solve_interval_cover,
        [
            ("4\n1 3\n2 6\n8 10\n15 18\n", "3\n"),
            ("3\n1 10\n2 5\n3 6\n", "1\n"),
            ("1\n1 5\n", "1\n"),
        ],
    ),
    P(
        "子段和查询",
        ["前缀和"],
        "普及-",
        "给定一个整数数组 arr 和 q 次查询，每次查询求 arr[l..r]（1-indexed 闭区间）的元素之和。",
        "第一行两个整数 n q。第二行 n 个整数。接下来 q 行，每行两个整数 l r。",
        "q 行，每行一个整数。",
        "1 <= n, q <= 100000，-10^4 <= arr[i] <= 10^4",
        solve_prefix_sum,
        [("5 2\n1 2 3 4 5\n1 3\n2 5\n", "6\n14\n"), ("3 1\n10 20 30\n1 3\n", "60\n")],
    ),
    P(
        "无重复字符的最长子串",
        ["双指针", "哈希"],
        "普及-",
        "给定一个字符串 s，求不含重复字符的最长子串的长度。",
        "一行一个字符串 s（仅含小写字母）。",
        "一行一个整数。",
        "0 <= len(s) <= 50000",
        solve_no_repeat_longest,
        [("abcabcbb\n", "3\n"), ("bbbbb\n", "1\n"), ("pwwkew\n", "3\n"), ("a\n", "1\n")],
    ),
    P(
        "合法括号序列计数",
        ["构造", "数学"],
        "普及-",
        "给定整数 n，求由 n 对括号组成的合法括号序列的数量。",
        "一行一个整数 n。",
        "一行一个整数。",
        "1 <= n <= 19",
        solve_bracket_count,
        [("3\n", "5\n"), ("4\n", "14\n"), ("10\n", "16796\n")],
    ),
    P(
        "最长上升子序列",
        ["动态规划", "二分"],
        "普及/提高-",
        "给定一个整数数组 nums，求最长严格上升子序列（LIS）的长度。",
        "第一行一个整数 n。第二行 n 个整数。",
        "一行一个整数。",
        "1 <= n <= 5000，-10^4 <= nums[i] <= 10^4",
        solve_lis,
        [("8\n10 9 2 5 3 7 101 18\n", "4\n"), ("6\n1 3 6 7 9 4\n", "5\n"), ("1\n5\n", "1\n")],
    ),
    P(
        "最长公共子序列",
        ["动态规划"],
        "普及/提高-",
        "给定两个字符串 a 和 b，求它们的最长公共子序列（LCS）的长度。",
        "第一行一个字符串 a。第二行一个字符串 b。",
        "一行一个整数。",
        "1 <= len(a), len(b) <= 1000，仅含小写字母",
        solve_lcs,
        [("abcde\nace\n", "3\n"), ("abc\nabc\n", "3\n"), ("abc\ndef\n", "0\n")],
    ),
    P(
        "编辑距离",
        ["动态规划"],
        "普及/提高-",
        "给定两个字符串 a 和 b，求将 a 转换为 b 所需的最少操作次数（插入/删除/替换）。",
        "第一行一个字符串 a。第二行一个字符串 b。",
        "一行一个整数。",
        "1 <= len(a), len(b) <= 500，仅含小写字母",
        solve_edit_distance,
        [("horse\nros\n", "3\n"), ("intention\nexecution\n", "5\n"), ("a\na\n", "0\n")],
    ),
    P(
        "零一背包",
        ["动态规划"],
        "普及/提高-",
        "有 n 件物品和一个容量为 C 的背包。第 i 件物品重量 w[i]、价值 v[i]。求装入背包的物品最大总价值（每件物品最多选一次）。",
        "第一行两个整数 n C。第二行 n 个整数表示重量。第三行 n 个整数表示价值。",
        "一行一个整数。",
        "1 <= n <= 100，1 <= C <= 1000",
        solve_knapsack,
        [("3 5\n1 2 3\n6 10 12\n", "22\n"), ("4 7\n1 3 4 5\n1 4 5 7\n", "9\n")],
    ),
    P(
        "质数筛",
        ["数学"],
        "普及/提高-",
        "给定整数 n，输出所有小于等于 n 的质数，空格分隔。",
        "一行一个整数 n。",
        "一行若干个质数，空格分隔。",
        "0 <= n <= 1000000",
        solve_sieve,
        [
            ("20\n", "2 3 5 7 11 13 17 19\n"),
            ("2\n", "2\n"),
            ("30\n", "2 3 5 7 11 13 17 19 23 29\n"),
        ],
    ),
    P(
        "01 网格最短路",
        ["搜索", "图论"],
        "普及+",
        "给定一个 R×C 的 01 网格（0 = 可走，1 = 墙），从左上角 (0,0) 走到右下角 (R-1,C-1)，每步可以向上下左右移动一格。求最短路径长度（含起点和终点）。保证起点和终点均为 0。",
        "第一行两个整数 R C。接下来 R 行，每行 C 个整数（0 或 1），空格分隔。",
        "一行一个整数，表示最短路径长度。",
        "1 <= R, C <= 100",
        solve_grid_bfs,
        [("3 3\n0 0 0\n0 1 0\n0 0 0\n", "5\n"), ("2 2\n0 0\n0 0\n", "3\n")],
    ),
    P(
        "岛屿数量",
        ["搜索", "图论"],
        "普及+",
        "给定一个 R×C 的 01 矩阵（1 = 陆地，0 = 水域），求岛屿的数量。岛屿由水平或垂直相邻的陆地组成。",
        "第一行两个整数 R C。接下来 R 行，每行 C 个整数（0 或 1），空格分隔。",
        "一行一个整数。",
        "1 <= R, C <= 300",
        solve_island_count,
        [
            ("4 5\n1 1 0 0 0\n1 1 0 0 0\n0 0 1 0 0\n0 0 0 1 1\n", "3\n"),
            ("3 3\n1 1 1\n1 1 1\n1 1 1\n", "1\n"),
        ],
    ),
    P(
        "N 皇后计数",
        ["搜索"],
        "普及+/提高",
        "给定整数 n，求在 n×n 的棋盘上放置 n 个皇后使得它们互不攻击的方案数。",
        "一行一个整数 n。",
        "一行一个整数，表示方案数。",
        "1 <= n <= 13",
        solve_n_queens,
        [("4\n", "2\n"), ("8\n", "92\n"), ("1\n", "1\n")],
    ),
    P(
        "拓扑排序",
        ["图论"],
        "普及+",
        "给定一个有向无环图，节点编号 1..n，输出其拓扑排序。若存在多个合法拓扑序，输出字典序最小的那个（即从左到右逐位比较编号序列，取最小者）。",
        "第一行两个整数 n m。接下来 m 行，每行两个整数 u v 表示有向边 u→v。",
        "一行 n 个整数，空格分隔。",
        "1 <= n <= 1000，0 <= m <= 5000",
        solve_topo_sort,
        [("4 3\n1 2\n1 3\n2 4\n", "1 2 3 4\n"), ("3 1\n2 1\n", "2 1 3\n")],
    ),
    P(
        "区间和查询",
        ["线段树", "前缀和"],
        "提高+/省选",
        "给定一个长度为 n 的整数数组，支持 q 次区间和查询 [l, r]（1-indexed 闭区间）。",
        "第一行两个整数 n q。第二行 n 个整数。接下来 q 行，每行两个整数 l r。",
        "q 行，每行一个整数。",
        "1 <= n, q <= 200000，-10^9 <= arr[i] <= 10^9",
        solve_segment_query,
        [
            ("5 3\n1 2 3 4 5\n1 3\n2 5\n1 5\n", "6\n14\n15\n"),
            ("3 2\n10 20 30\n1 1\n3 3\n", "10\n30\n"),
        ],
    ),
    P(
        "零一背包（进阶）",
        ["动态规划"],
        "提高+/省选",
        "有 n 件物品和一个容量为 C 的背包。第 i 件物品重 w[i]、价值 v[i]。求不超过容量的最大总价值。",
        "第一行两个整数 n C。第二行 n 个整数表示重量。第三行 n 个整数表示价值。",
        "一行一个整数。",
        "1 <= n <= 500，1 <= C <= 10000",
        solve_knapsack,
        [("5 10\n2 2 6 5 4\n6 3 5 4 6\n", "15\n"), ("3 4\n1 2 3\n1 3 4\n", "5\n")],
    ),
]
