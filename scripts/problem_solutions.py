"""内置题包的官方题解（markdown：思路/复杂度/参考代码）。

每段参考代码都是完整可提交程序，与 problem_pack.py 的 solve 同逻辑，
并且**全部经真实沙箱判题 AC**（tests/test_problem_pack.py 逐题强制验证
每个 ```python 块），不是"看起来对"的示意代码。
"""

from __future__ import annotations

SOLUTIONS: dict[str, str] = {
    "爬楼梯": """\
## 思路
到第 n 阶的最后一步要么从 n-1 阶跨 1 阶、要么从 n-2 阶跨 2 阶，
故 f(n) = f(n-1) + f(n-2)，即斐波那契数列。边界 f(1)=1、f(2)=2。
用两个变量滚动递推即可，无需数组。
## 复杂度
时间 O(n)，空间 O(1)。
## 参考代码
```python
import sys

n = int(sys.stdin.read().strip())
a, b = 1, 2
for _ in range(n - 2):
    a, b = b, a + b
print(b)
```""",
    "最大公约数与最小公倍数": """\
## 思路
辗转相除法（欧几里得算法）：gcd(a, b) = gcd(b, a mod b)，
直到 b 为 0，此时 a 即最大公约数。最小公倍数用恒等式
lcm(a, b) = a / gcd(a, b) * b（先除后乘避免溢出）。
## 复杂度
时间 O(log min(a, b))，空间 O(1)。
## 参考代码
```python
import math
import sys

a, b = map(int, sys.stdin.read().split())
g = math.gcd(a, b)
print(g, a // g * b)
```""",
    "成绩排序": """\
## 思路
多关键字排序：主关键字成绩降序，次关键字姓名升序（字符串按
Unicode 码点逐字符比较，所以"丙"排在"甲"前面）。Python 的 sort
稳定且支持元组键，用 key=lambda x: (-score, name) 一次完成。
## 复杂度
时间 O(n log n)，空间 O(n)。
## 参考代码
```python
import sys

lines = sys.stdin.read().split()
n = int(lines[0])
students = [(lines[1 + 2 * i], int(lines[2 + 2 * i])) for i in range(n)]
students.sort(key=lambda s: (-s[1], s[0]))
print("\\n".join(f"{name} {score}" for name, score in students))
```""",
    "两数之和": """\
## 思路
暴力枚举两两组合是 O(n^2)。用哈希表把"是否存在 target - x"
变成 O(1) 查询：遍历数组，对每个 x 先查 target - x 是否已出现
（出现即得答案），再把 x 存入表。一次遍历完成。
## 复杂度
时间 O(n)，空间 O(n)。
## 参考代码
```python
import json
import sys

lines = sys.stdin.read().split()
nums = json.loads(lines[0])
target = int(lines[1])
seen = {}
for i, x in enumerate(nums):
    if target - x in seen:
        print(seen[target - x], i)
        break
    seen[x] = i
else:
    print(-1)
```""",
    "约瑟夫环": """\
## 思路
n 个人围成圈，从 1 开始报数，报到 k 的人出列，下一个人重新从 1 报起。
用队列模拟：每次把前 k-1 个人搬到队尾，队首即本轮出列者；重复 n 次
得到完整出列顺序。也可用递推公式求最后存活者，但本题要输出
出列序列，队列模拟最直接。
## 复杂度
时间 O(n·k)，空间 O(n)。
## 参考代码
```python
import sys
from collections import deque

n, k = map(int, sys.stdin.read().split())
q = deque(range(1, n + 1))
out = []
while q:
    for _ in range(k - 1):
        q.append(q.popleft())
    out.append(str(q.popleft()))
print(" ".join(out))
```""",
    "二分查找": """\
## 思路
数组已升序。维护闭区间 [lo, hi]，每次取 mid 与 target 比较：
小于则查右半 [mid+1, hi]，大于则查左半 [lo, mid-1]，相等返回下标。
区间为空仍未命中则不存在，返回 -1。注意循环条件是 lo <= hi，
边界 ±1 写错是最常见 bug。
## 复杂度
时间 O(log n)，空间 O(1)。
## 参考代码
```python
import json
import sys

lines = sys.stdin.read().split()
nums = json.loads(lines[0])
target = int(lines[1])
lo, hi = 0, len(nums) - 1
while lo <= hi:
    mid = (lo + hi) // 2
    if nums[mid] == target:
        print(mid)
        break
    if nums[mid] < target:
        lo = mid + 1
    else:
        hi = mid - 1
else:
    print(-1)
```""",
    "区间合并计数": """\
## 思路
先按左端点排序，再从左往右扫：当前区间的左端点 > 已合并块的
最右端时开新块；否则与当前块相交（或相接），只需把块右端延伸为
两者较大值。扫完统计块数即为答案。
## 复杂度
时间 O(n log n)（排序），空间 O(n)。
## 参考代码
```python
import sys

lines = sys.stdin.read().split()
n = int(lines[0])
intervals = sorted(
    (int(lines[1 + 2 * i]), int(lines[2 + 2 * i])) for i in range(n)
)
merged = 0
last_r = float("-inf")
for lo, hi in intervals:
    if lo > last_r:
        merged += 1
        last_r = hi
    else:
        last_r = max(last_r, hi)
print(merged)
```""",
    "子段和查询": """\
## 思路
每次查询都现算是 O(n·q)。前缀和预处理：prefix[i] = a[1] + ... + a[i]，
则区间 [l, r] 的和 = prefix[r] - prefix[l-1]，单次查询 O(1)。
适合静态数组多次查询；若还要支持单点修改则需要树状数组/线段树。
## 复杂度
预处理 O(n)，每次查询 O(1)，空间 O(n)。
## 参考代码
```python
import sys

data = sys.stdin.read().split()
n, q = int(data[0]), int(data[1])
prefix = [0]
for i in range(n):
    prefix.append(prefix[-1] + int(data[2 + i]))
out = []
for i in range(q):
    lo = int(data[2 + n + 2 * i])
    hi = int(data[3 + n + 2 * i])
    out.append(str(prefix[hi] - prefix[lo - 1]))
print("\\n".join(out))
```""",
    "无重复字符的最长子串": """\
## 思路
滑动窗口：右指针扩张，用哈希表记录窗口内每个字符最后出现的位置。
当右指针遇到重复字符 c 时，把左指针直接跳到"c 上次出现位置 + 1"
（比逐格收缩快）。每步用 r - l + 1 更新答案。
## 复杂度
时间 O(n)，空间 O(字符集大小)。
## 参考代码
```python
import sys

s = sys.stdin.read().strip()
last = {}
best = left = 0
for right, ch in enumerate(s):
    if ch in last and last[ch] >= left:
        left = last[ch] + 1
    last[ch] = right
    best = max(best, right - left + 1)
print(best)
```""",
    "合法括号序列计数": """\
## 思路
这是 Catalan 数：C(n) = (2n)! / ((n+1)! · n!)，即 1, 1, 2, 5, 14...
组合意义：每个合法序列在任意前缀中左括号数都 ≥ 右括号数。
直接用 math.comb 计算二项式再除以 n+1。
## 复杂度
时间 O(n)（大整数运算按位数另计），空间 O(1)。
## 参考代码
```python
import math
import sys

n = int(sys.stdin.read().strip())
print(math.comb(2 * n, n) // (n + 1))
```""",
    "最长上升子序列": """\
## 思路
朴素 DP 是 O(n^2)。更优的是贪心 + 二分：tails[k] 维护"长度为 k+1
的上升子序列的最小结尾"，tails 单调递增。对每个元素用 bisect_left
找它应放的位置：越界则 LIS 延长，否则替换使结尾更小（给后续元素
留机会）。答案为 len(tails)。注意替换不改变当前长度，只是让
"更小结尾"这一状态更优。
## 复杂度
时间 O(n log n)，空间 O(n)。
## 参考代码
```python
import sys
from bisect import bisect_left

data = sys.stdin.read().split()
nums = list(map(int, data[1 : 1 + int(data[0])]))
tails = []
for x in nums:
    pos = bisect_left(tails, x)
    if pos == len(tails):
        tails.append(x)
    else:
        tails[pos] = x
print(len(tails))
```""",
    "最长公共子序列": """\
## 思路
经典二维 DP：f[i][j] 表示 a 前 i 个字符与 b 前 j 个字符的 LCS 长度。
a[i] == b[j] 时 f[i][j] = f[i-1][j-1] + 1；否则取
max(f[i-1][j], f[i][j-1])（丢掉 a 的末字符或 b 的末字符两者较优者）。
逐行滚动可把空间优化到 O(min(m, n))，本题直接保留全表。
## 复杂度
时间 O(m·n)，空间 O(m·n)（可滚动优化到 O(min(m,n))）。
## 参考代码
```python
import sys

a = sys.stdin.readline().strip()
b = sys.stdin.readline().strip()
m, n = len(a), len(b)
f = [[0] * (n + 1) for _ in range(m + 1)]
for i in range(1, m + 1):
    for j in range(1, n + 1):
        if a[i - 1] == b[j - 1]:
            f[i][j] = f[i - 1][j - 1] + 1
        else:
            f[i][j] = max(f[i - 1][j], f[i][j - 1])
print(f[m][n])
```""",
    "编辑距离": """\
## 思路
二维 DP：f[i][j] 表示 a 前 i 个字符变成 b 前 j 个字符的最少操作数。
a[i] == b[j] 时无需操作，f[i][j] = f[i-1][j-1]；否则在
插入 f[i][j-1]、删除 f[i-1][j]、替换 f[i-1][j-1] 三者中取最小 + 1。
边界：f[i][0] = i（全删）、f[0][j] = j（全插）。
## 复杂度
时间 O(m·n)，空间 O(m·n)。
## 参考代码
```python
import sys

a = sys.stdin.readline().strip()
b = sys.stdin.readline().strip()
m, n = len(a), len(b)
f = [[0] * (n + 1) for _ in range(m + 1)]
for i in range(m + 1):
    f[i][0] = i
for j in range(n + 1):
    f[0][j] = j
for i in range(1, m + 1):
    for j in range(1, n + 1):
        if a[i - 1] == b[j - 1]:
            f[i][j] = f[i - 1][j - 1]
        else:
            f[i][j] = min(f[i - 1][j], f[i][j - 1], f[i - 1][j - 1]) + 1
print(f[m][n])
```""",
    "零一背包": """\
## 思路
每件物品只能选一次。f[j] 表示容量 j 下的最大价值：对每件物品
(w, v)，f[j] = max(f[j], f[j-w] + v)。注意 j 必须从大到小遍历——
倒序保证 f[j-w] 还是"未考虑本件物品"的旧值；正序会变成
每件物品可选无限次的完全背包。
## 复杂度
时间 O(n·C)，空间 O(C)。
## 参考代码
```python
import sys

data = sys.stdin.read().split()
n, c = int(data[0]), int(data[1])
weights = list(map(int, data[2 : 2 + n]))
values = list(map(int, data[2 + n : 2 + 2 * n]))
f = [0] * (c + 1)
for w, v in zip(weights, values):
    for j in range(c, w - 1, -1):
        f[j] = max(f[j], f[j - w] + v)
print(f[c])
```""",
    "质数筛": """\
## 思路
埃拉托斯特尼筛：从 2 开始，把每个质数的倍数全部标记为合数。
优化两点：内层从 i*i 开始标记（更小的倍数已被更小的质数处理过）；
也可只筛到 sqrt(n) 就能完成全表标记。线性筛（欧拉筛）可做到
每个合数只被最小质因子标记一次，n 更大时更优。
## 复杂度
时间 O(n log log n)，空间 O(n)。
## 参考代码
```python
import sys

n = int(sys.stdin.read().strip())
sieve = [True] * (n + 1)
primes = []
for i in range(2, n + 1):
    if sieve[i]:
        primes.append(i)
        for j in range(i * i, n + 1, i):
            sieve[j] = False
print(" ".join(map(str, primes)))
```""",
    "01 网格最短路": """\
## 思路
网格无权图求最短路径，BFS 天然按层扩展、第一次到达终点即最短。
从起点出发把相邻可通过的格子入队并记路径长度，注意起点本身
算 1 格（输出是路径格子数不是移动次数）。可通行判定：
在界内、值为 0、未访问过。
## 复杂度
时间 O(R·C)，空间 O(R·C)。
## 参考代码
```python
import sys
from collections import deque

data = sys.stdin.read().split()
r, c = int(data[0]), int(data[1])
g = [data[2 + i * c : 2 + (i + 1) * c] for i in range(r)]
dist = [[-1] * c for _ in range(r)]
dist[0][0] = 1
q = deque([(0, 0)])
while q:
    x, y = q.popleft()
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < r and 0 <= ny < c and g[nx][ny] == "0" and dist[nx][ny] == -1:
            dist[nx][ny] = dist[x][y] + 1
            q.append((nx, ny))
print(dist[r - 1][c - 1])
```""",
    "岛屿数量": """\
## 思路
连通块计数。扫每个格子，遇到未访问的陆地就把整个连通块标记掉
（DFS/BFS flood fill 均可），标记完一个块计数 +1。四连通：
上下左右算相邻，斜角不算。用栈式 DFS 避免深网格递归爆栈。
## 复杂度
时间 O(R·C)，空间 O(R·C)。
## 参考代码
```python
import sys

data = sys.stdin.read().split()
r, c = int(data[0]), int(data[1])
g = [data[2 + i * c : 2 + (i + 1) * c] for i in range(r)]
count = 0
for i in range(r):
    for j in range(c):
        if g[i][j] == "1":
            count += 1
            stack = [(i, j)]
            g[i][j] = "0"
            while stack:
                x, y = stack.pop()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < r and 0 <= ny < c and g[nx][ny] == "1":
                        g[nx][ny] = "0"
                        stack.append((nx, ny))
print(count)
```""",
    "N 皇后计数": """\
## 思路
逐行放置（每行必恰一个皇后），只需记录已占的列、主对角线
（row - col 为定值）、副对角线（row + col 为定值）三个集合。
放满 n 行即得一个解，回溯统计总数。用集合判占用的写法
比位数组更直观；n=8 时解有 92 个。
## 复杂度
时间 O(n!)（剪枝后远小于），空间 O(n)。
## 参考代码
```python
import sys

n = int(sys.stdin.read().strip())
cols: set[int] = set()
d1: set[int] = set()
d2: set[int] = set()


def solve(row: int) -> int:
    if row == n:
        return 1
    total = 0
    for col in range(n):
        if col in cols or (row - col) in d1 or (row + col) in d2:
            continue
        cols.add(col)
        d1.add(row - col)
        d2.add(row + col)
        total += solve(row + 1)
        cols.remove(col)
        d1.remove(row - col)
        d2.remove(row + col)
    return total


print(solve(0))
```""",
    "拓扑排序": """\
## 思路
Kahn 算法：维护每个点的入度，入度为 0 的点入小根堆；每次取
编号最小的出堆、删掉它的所有出边（后继入度减 1，减到 0 入堆）。
用堆而不是普通队列，保证输出的是字典序最小的拓扑序——这正是
本题题面要求的确定性输出。若最后输出点数 < n 则图中有环。
## 复杂度
时间 O(n log n + m)，空间 O(n + m)。
## 参考代码
```python
import heapq
import sys

data = sys.stdin.read().split()
n, m = int(data[0]), int(data[1])
adj = [[] for _ in range(n + 1)]
indeg = [0] * (n + 1)
idx = 2
for _ in range(m):
    u, v = int(data[idx]), int(data[idx + 1])
    idx += 2
    adj[u].append(v)
    indeg[v] += 1
heap = [i for i in range(1, n + 1) if indeg[i] == 0]
heapq.heapify(heap)
order = []
while heap:
    u = heapq.heappop(heap)
    order.append(u)
    for v in adj[u]:
        indeg[v] -= 1
        if indeg[v] == 0:
            heapq.heappush(heap, v)
print(" ".join(map(str, order)))
```""",
    "区间和查询": """\
## 思路
静态数组区间和用前缀和即可 O(1) 查询；本题练习线段树：
每个节点维护一段区间的和，查询 O(log n)，且天然支持单点/区间
修改（前缀和不支持，这是选线段树的理由）。build 自底向上建树，
query 递归分治——区间不交返回 0、完整覆盖返回节点值、
否则拆给左右子树。
## 复杂度
建树 O(n)，每次查询 O(log n)，空间 O(n)。
## 参考代码
```python
import sys

data = sys.stdin.read().split()
n, q = int(data[0]), int(data[1])
a = list(map(int, data[2 : 2 + n]))
size = 1
while size < n:
    size *= 2
tree = [0] * (2 * size)
for i, x in enumerate(a):
    tree[size + i] = x
for i in range(size - 1, 0, -1):
    tree[i] = tree[2 * i] + tree[2 * i + 1]


def query(node: int, node_lo: int, node_hi: int, lo: int, hi: int) -> int:
    if hi < node_lo or node_hi < lo:
        return 0
    if lo <= node_lo and node_hi <= hi:
        return tree[node]
    mid = (node_lo + node_hi) // 2
    return query(2 * node, node_lo, mid, lo, hi) + query(
        2 * node + 1, mid + 1, node_hi, lo, hi
    )


out = []
for i in range(q):
    lo = int(data[2 + n + 2 * i])
    hi = int(data[3 + n + 2 * i])
    out.append(str(query(1, 0, size - 1, lo - 1, hi - 1)))
print("\\n".join(out))
```""",
    "零一背包（进阶）": """\
## 思路
与基础版同型，但数据规模大（n ≤ 500、C ≤ 10000），O(n·C) 约
5×10^6 次运算，倒序一维滚动完全够快。考察点：循环顺序
（倒序 = 零一背包，正序 = 完全背包）与数组复用的正确性。
用 if 判优代替 max() 可省一次函数调用。
## 复杂度
时间 O(n·C)，空间 O(C)。
## 参考代码
```python
import sys

data = sys.stdin.read().split()
n, c = int(data[0]), int(data[1])
weights = list(map(int, data[2 : 2 + n]))
values = list(map(int, data[2 + n : 2 + 2 * n]))
f = [0] * (c + 1)
for w, v in zip(weights, values):
    for j in range(c, w - 1, -1):
        cand = f[j - w] + v
        if cand > f[j]:
            f[j] = cand
print(f[c])
```""",
}
